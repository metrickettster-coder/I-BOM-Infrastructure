"""Live-state readers for drift detection.

Each reader turns one export into *snapshots*: what a source reported about one
I-BOM line (and optionally one serialized unit) at one time, in the same shape
as the line's `configuration` so the two can be compared key by key:

  {"ref": "hw-...", "unit": "7XK2Q35" | None, "source": "redfish", "at": "...",
   "state": {"firmware": {"bios": "1.8.2", "bmc": "7.10.50.00"}},
   "expected": {...}   # optional: what the source itself says was intended (Terraform refresh)
  }

A source only speaks for the top-level keys in its `state`; attributes it does
not report are not compared. Lines are found with ibom_ingest's match keys
(serial:, cloud:, iac:, nodegroup:), so no new identity scheme is needed.

Supported inputs (auto-detected):
  aws-ec2          aws ec2 describe-instances --output json
  eks-cluster      aws eks describe-cluster --name X
  eks-nodegroup    aws eks describe-nodegroup --cluster-name X --nodegroup-name Y
  terraform-drift  terraform plan -refresh-only -out p && terraform show -json p   (resource_drift)
  redfish          Redfish snapshot, as read by ibom_ingest.lifecycle.read_redfish
  facts            generic {"source", "collectedAt", "devices": [{"keys", "unit", "state"}]}
                   for gNMI/NETCONF/eAPI/SNMP/agent exports mapped to configuration keys
Plus the firmware ibom_ingest already stored on each line (observedFirmware).
"""
import json
import re
from pathlib import Path

from ibom_ingest import cloud, lifecycle
from ibom_ingest.lifecycle import EXT as LC_EXT


def camel(k):
    head, *rest = str(k).split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def norm(v):
    """Canonical shape for comparison: camelCase keys, single-item lists of objects unwrapped
    (Terraform nests blocks as [{...}]), so IaC, cloud API and hand-written values line up."""
    if isinstance(v, list) and len(v) == 1 and isinstance(v[0], dict):
        v = v[0]
    if isinstance(v, dict):
        return {camel(k): norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v


def detect(data):
    if isinstance(data, dict):
        if "Reservations" in data:
            return "aws-ec2"
        if isinstance(data.get("cluster"), dict) and "arn" in data["cluster"]:
            return "eks-cluster"
        if isinstance(data.get("nodegroup"), dict):
            return "eks-nodegroup"
        if "resource_drift" in data or ("format_version" in data and "resource_changes" in data):
            return "terraform-drift"
        if "devices" in data:
            return "facts"
        if "@odata.type" in data or any("@odata.type" in r for r in data.get("resources", []) if isinstance(r, dict)):
            return "redfish"
    raise ValueError("unrecognised live-state file; expected aws-ec2, eks-cluster, eks-nodegroup, "
                     "terraform-drift, redfish or facts JSON")


def read(builder, path, at, fmt=None):
    """Return (snapshots, unmatched_ids) for one export file."""
    data = json.loads(Path(path).read_text())
    fmt = fmt or detect(data)
    at = data.get("collectedAt") or data.get("_collectedAt") or at if isinstance(data, dict) else at
    snaps, unmatched = READERS[fmt](builder, data, at, path)
    for s in snaps:
        s["file"] = Path(path).name
    return snaps, unmatched


def _snap(comp, source, at, state, unit=None, expected=None):
    s = {"ref": comp["bom-ref"], "unit": unit, "source": source, "at": at, "state": norm(state)}
    if expected:
        s["expected"] = norm(expected)
    return s


def read_aws_ec2(builder, data, at, path):
    raw = {i["InstanceId"]: i for r in data.get("Reservations", []) for i in r.get("Instances", [])}
    snaps, unmatched = [], []
    for r in cloud.read_aws_ec2(data):
        comp = builder.find([f"cloud:{r['resourceId']}", f"cloud:{r['shortId']}"])
        if comp is None:
            unmatched.append(r["shortId"])  # shadow: ibom_ingest discover handles these
            continue
        if r.get("state") in ("terminated", "shutting-down"):
            continue
        state = {"instanceType": r.get("instanceType"), "tags": r.get("tags", {})}
        if raw[r["shortId"]].get("ImageId"):
            state["ami"] = raw[r["shortId"]]["ImageId"]
        snaps.append(_snap(comp, "cloud-api", at, state))
    return snaps, unmatched


def read_eks_cluster(builder, data, at, path):
    c = data["cluster"]
    comp = builder.find([f"cloud:{c['arn']}", f"cloud:{c.get('name', '')}"])
    if comp is None:
        return [], [c["arn"]]
    state = {"version": c.get("version")}
    if "tags" in c:
        state["tags"] = c["tags"]
    return [_snap(comp, "cloud-api", at, state)], []


def read_eks_nodegroup(builder, data, at, path):
    g = data["nodegroup"]
    keys = [f"cloud:{g.get('nodegroupArn', '')}", f"nodegroup:{g.get('nodegroupName', '')}"]
    comp = builder.find([k for k in keys if k.split(":", 1)[1]])
    if comp is None:
        return [], [g.get("nodegroupArn") or g.get("nodegroupName")]
    state = {"scalingConfig": g.get("scalingConfig", {})}
    if g.get("instanceTypes"):
        state["instanceTypes"] = g["instanceTypes"]
    if g.get("releaseVersion"):
        state["releaseVersion"] = g["releaseVersion"]
    return [_snap(comp, "cloud-api", at, state)], []


def read_terraform_drift(builder, data, at, path):
    """`resource_drift` lists resources whose real state no longer matches the last apply:
    `before` is what Terraform recorded, `after` is what the provider reports now."""
    tool = "opentofu" if "tofu" in str(data.get("terraform_version", "")) else "terraform"
    snaps, unmatched = [], []
    for d in data.get("resource_drift", []):
        comp = builder.find([f"iac:{tool}:{d['address']}", f"iac:terraform:{d['address']}"])
        if comp is None:
            unmatched.append(d["address"])
            continue
        ch = d.get("change", {})
        before, after = ch.get("before") or {}, ch.get("after") or {}
        if "delete" in ch.get("actions", []) or not after:
            state = {"exists": False}
            snaps.append(_snap(comp, "terraform-state", at, state, expected={"exists": True}))
            continue
        changed = {k for k in set(before) | set(after)
                   if norm(before.get(k)) != norm(after.get(k)) and k not in ("id", "arn", "tags_all")}
        state = {k: after.get(k) for k in changed}
        expected = {k: before.get(k) for k in changed}
        if state:
            snaps.append(_snap(comp, "terraform-state", at, state, expected=expected))
    return snaps, unmatched


def read_redfish(builder, data, at, path):
    collected, _endpoint, r = lifecycle.read_redfish(path)
    system = r.get("ComputerSystem")
    if not system:
        return [], []
    serial = system.get("SerialNumber", "").strip()
    comp = builder.find([f"serial:{serial}"]) if serial else None
    if comp is None:
        return [], [serial or "unknown"]
    fw = {}
    if system.get("BiosVersion"):
        fw["bios"] = system["BiosVersion"]
    if r.get("Manager", {}).get("FirmwareVersion"):
        fw["bmc"] = r["Manager"]["FirmwareVersion"]
    state = {"firmware": fw}
    pc = (r.get("Power", {}).get("PowerControl") or [{}])[0]
    if pc.get("PowerLimit", {}).get("LimitInWatts") is not None:
        state["powerCapW"] = pc["PowerLimit"]["LimitInWatts"]
    return [_snap(comp, "redfish", collected or at, state, unit=_unit_id(comp, serial))], []


def read_facts(builder, data, at, path):
    source = data.get("source", "agent")
    snaps, unmatched = [], []
    for dev in data["devices"]:
        comp = builder.find(dev.get("keys", []))
        if comp is None and dev.get("ref"):
            comp = builder.component(dev["ref"])
        if comp is None:
            unmatched.append(",".join(dev.get("keys", [])) or dev.get("ref", "?"))
            continue
        unit = dev.get("unit") or next((_unit_id(comp, k.split(":", 1)[1]) for k in dev.get("keys", [])
                                        if k.startswith("serial:")), None)
        snaps.append(_snap(comp, source, dev.get("collectedAt", at), dev["state"], unit=unit))
    return snaps, unmatched


def _unit_id(comp, serial):
    """Unit id for per-unit comparison: only lines with several serialized units need one."""
    return serial if len(comp.get("units", [])) > 1 else None


def from_bom(builder):
    """Firmware ibom_ingest already recorded per unit (Redfish, Ansible facts) as snapshots."""
    snaps = []
    for c in builder.doc["components"]:
        for unit, fw in c.get("extensions", {}).get(LC_EXT, {}).get("observedFirmware", {}).items():
            state = {"firmware": {k: v for k, v in fw.items() if k not in ("at", "source")}}
            snaps.append({"ref": c["bom-ref"], "unit": _unit_id(c, unit), "source": fw.get("source", "agent"),
                          "at": fw.get("at"), "state": state, "file": "I-BOM observedFirmware"})
    return snaps


READERS = {"aws-ec2": read_aws_ec2, "eks-cluster": read_eks_cluster, "eks-nodegroup": read_eks_nodegroup,
           "terraform-drift": read_terraform_drift, "redfish": read_redfish, "facts": read_facts}


def merge(snaps):
    """Latest report per (line, unit, top-level key) wins, keeping which source and when."""
    out = {}
    for s in sorted(snaps, key=lambda s: s.get("at") or ""):
        view = out.setdefault((s["ref"], s["unit"]), {})
        for k, v in s["state"].items():
            view[k] = {"value": v, "source": s["source"], "at": s["at"], "file": s.get("file"),
                       "expected": (s.get("expected") or {}).get(k)}
    return out


def slug_attr(pointer):
    return re.sub(r"[^A-Za-z0-9]+", "-", pointer).strip("-").lower()

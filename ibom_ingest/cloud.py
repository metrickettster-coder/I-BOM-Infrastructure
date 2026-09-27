"""Live cloud inventory -> reconciliation against the I-BOM (shadow infrastructure).

Reads what is actually running from a provider export, matches each resource to
an I-BOM line by resource id, and:
  * updates matched lines (running -> in-service, stopped -> offline, terminated -> decommissioned)
  * pulls unmatched resources in as `discovered-unmanaged` lines with an open
    `unmanaged` observation (shadow infrastructure), costed from the instance catalog
  * raises a `missing` observation for declared lines that discovery did not find

Supported exports:
  aws-ec2      `aws ec2 describe-instances --output json`
  azure-graph  `az graph query -q "Resources | where type =~ 'microsoft.compute/virtualmachines'"`
  gcp-asset    `gcloud asset list --asset-types=compute.googleapis.com/Instance --content-type=resource --format=json`
  generic      JSON list of {provider, resourceType, resourceId, region, name, instanceType, state, tags}
"""
import json
from pathlib import Path

from .core import money, slug

CATALOG = json.loads((Path(__file__).resolve().parent / "catalogs" / "instance_types.json").read_text())
HOURS_PER_MONTH = 730


def instance_spec(provider, itype):
    return CATALOG.get(provider, {}).get(itype or "", {})


def spec_fields(provider, itype):
    """virtual/compute/financial fragments for an instance type from the public catalog."""
    spec = instance_spec(provider, itype)
    virtual, compute, fin = {}, {}, {}
    if itype:
        virtual["instanceType"] = itype
    if spec.get("vcpus"):
        virtual["vcpus"] = spec["vcpus"]
    if spec.get("memoryGiB"):
        virtual["memoryGiB"] = spec["memoryGiB"]
    if spec.get("accelerator"):
        compute = {"acceleratorModel": spec["accelerator"], "acceleratorCount": spec["acceleratorCount"],
                   "acceleratorMemoryGiB": spec["acceleratorMemoryGiB"]}
    if spec.get("hourlyUsd"):
        fin = {"recurringCost": {"amount": money(spec["hourlyUsd"] * HOURS_PER_MONTH), "period": "monthly"},
               "costType": "list", "expenseType": "opex"}
    return virtual, compute, fin


def region_location(builder, provider, region, account=None):
    if not region:
        return None
    ref = f"loc-{provider}-{slug(region)}"
    extra = {"cloud": {"provider": provider, "region": region}}
    if account:
        extra["cloud"]["accountId"] = str(account)
    return builder.location(ref, "cloud-region", f"{provider.upper()} {region}", **extra)


# ---------- readers ----------

def read_aws_ec2(data):
    out = []
    for res in data.get("Reservations", []):
        owner = res.get("OwnerId", "")
        for i in res.get("Instances", []):
            az = i.get("Placement", {}).get("AvailabilityZone", "")
            region = az[:-1] if az and az[-1].isalpha() else az
            tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
            iid = i["InstanceId"]
            out.append({
                "provider": "aws", "resourceType": "aws_instance", "category": "vm",
                "resourceId": f"arn:aws:ec2:{region}:{owner}:instance/{iid}", "shortId": iid,
                "region": region, "accountId": owner, "name": tags.get("Name", iid),
                "instanceType": i.get("InstanceType"), "state": i.get("State", {}).get("Name"),
                "launchTime": i.get("LaunchTime"), "tags": tags,
            })
    return out


def read_azure_graph(data):
    rows = data.get("data", data) if isinstance(data, dict) else data
    out = []
    for r in rows:
        props = r.get("properties", {})
        power = (props.get("extended", {}).get("instanceView", {}).get("powerState", {}) or {}).get("code", "")
        out.append({
            "provider": "azure", "resourceType": r.get("type"), "category": "vm",
            "resourceId": r["id"], "shortId": r["id"].lower(), "region": r.get("location"),
            "accountId": r.get("subscriptionId"), "name": r.get("name"),
            "instanceType": props.get("hardwareProfile", {}).get("vmSize"),
            "state": {"PowerState/running": "running", "PowerState/deallocated": "stopped",
                      "PowerState/stopped": "stopped"}.get(power, "running"),
            "tags": r.get("tags") or {},
        })
    return out


def read_gcp_asset(data):
    out = []
    for a in data:
        d = a.get("resource", {}).get("data", {})
        zone = d.get("zone", "").rsplit("/", 1)[-1]
        out.append({
            "provider": "gcp", "resourceType": "google_compute_instance", "category": "vm",
            "resourceId": d.get("selfLink") or a["name"], "shortId": str(d.get("id", "")),
            "region": zone.rsplit("-", 1)[0] if zone else a.get("resource", {}).get("location"),
            "name": d.get("name"), "instanceType": d.get("machineType", "").rsplit("/", 1)[-1] or None,
            "state": {"RUNNING": "running", "TERMINATED": "stopped", "STOPPED": "stopped"}.get(d.get("status"), "running"),
            "tags": d.get("labels") or {},
        })
    return out


READERS = {"aws-ec2": read_aws_ec2, "azure-graph": read_azure_graph, "gcp-asset": read_gcp_asset,
           "generic": lambda d: d}


def detect_format(data):
    if isinstance(data, dict) and "Reservations" in data:
        return "aws-ec2"
    if isinstance(data, dict) and "data" in data:
        return "azure-graph"
    if isinstance(data, list) and data and "assetType" in data[0]:
        return "gcp-asset"
    return "generic"


def read(path, fmt=None):
    data = json.loads(Path(path).read_text())
    fmt = fmt or detect_format(data)
    return READERS[fmt](data)


# ---------- reconciliation ----------

def reconcile(builder, resources, observed_at, source="cloud-api", adopt=True):
    """Match discovered resources to the BOM. Returns {'matched': [...], 'shadow': [...], 'missing': [...]}."""
    result = {"matched": [], "shadow": [], "missing": []}
    seen_refs, scopes = set(), set()
    for r in resources:
        scopes.add((r["provider"], r.get("region"), r.get("category")))
        keys = [f"cloud:{r['resourceId']}"] + ([f"cloud:{r['shortId']}"] if r.get("shortId") else [])
        comp = builder.find(keys)
        virtual, compute, fin = spec_fields(r["provider"], r.get("instanceType"))
        group = r.get("tags", {}).get("eks:nodegroup-name")
        if comp is None and group and builder.find([f"nodegroup:{group}"]):
            # members of a declared auto-scaling node group are managed as a group, not shadow
            result.setdefault("groupMembers", []).append(r["resourceId"])
            continue
        if comp is None:
            if not adopt:
                continue
            ref = _adopt_shadow(builder, r, keys, observed_at, source, virtual, compute, fin)
            result["shadow"].append(ref)
            seen_refs.add(ref)
            continue
        ref = comp["bom-ref"]
        seen_refs.add(ref)
        result["matched"].append(ref)
        patch = {"virtual": {"resourceId": r["resourceId"], **virtual},
                 "identifiers": {"cloud": {"provider": r["provider"], "resourceId": r["resourceId"]}}}
        builder.upsert({"component": patch, "keys": keys, "source": {"kind": source, "id": r["resourceId"]}})
        _apply_state(builder, comp, r, observed_at, source)
        for o in builder.doc.get("observations", []):  # it is back: close an earlier "missing" finding
            if o["id"] == _obs_id("missing", ref) and o.get("status") == "open":
                o.update(status="remediated", observedAt=observed_at)

    # declared-but-not-found, only inside the provider/region/category the export covered
    for c in builder.doc["components"]:
        v = c.get("virtual", {})
        status = c.get("lifecycle", {}).get("status")
        if c["bom-ref"] in seen_refs or status in (None, "planned", "decommissioned", "disposed", "discovered-unmanaged"):
            continue
        if (v.get("provider"), v.get("region"), c.get("category")) in scopes:
            builder.observe({"id": _obs_id("missing", c["bom-ref"]), "observedAt": observed_at, "source": source,
                             "kind": "missing", "component": c["bom-ref"], "severity": "high", "status": "open",
                             "attribute": "/virtual/resourceId", "expected": v.get("resourceId", "declared"),
                             "actual": None})
            result["missing"].append(c["bom-ref"])
    return result


def _obs_id(kind, ref):
    return f"OBS-{kind.upper()}-{slug(ref, 60)}"


def _apply_state(builder, comp, r, at, source):
    state = (r.get("state") or "").lower()
    lc = comp.setdefault("lifecycle", {})
    if state == "running":
        builder.set_status(comp, "in-service", at, note=f"seen running by {source}")
        if lc.get("health", {}).get("status") in (None, "offline", "unknown"):
            lc["health"] = {"status": "healthy", "observedAt": at, "source": source}
    elif state in ("stopped", "stopping"):
        lc["health"] = {"status": "offline", "observedAt": at, "source": source}
    elif state in ("terminated", "shutting-down"):
        builder.set_status(comp, "decommissioned", at, force=True, note=f"terminated per {source}")
        lc.setdefault("dates", {})["decommissioned"] = at[:10]


def _adopt_shadow(builder, r, keys, at, source, virtual, compute, fin):
    loc = region_location(builder, r["provider"], r.get("region"), r.get("accountId"))
    c = {"class": "cloud-resource", "category": r.get("category", "other"),
         "name": f"{r.get('name') or r['resourceId']} (unmanaged)", "quantity": 1,
         "virtual": {"provider": r["provider"], "resourceType": r["resourceType"], "resourceId": r["resourceId"],
                     "region": r.get("region"), **virtual, "tags": r.get("tags", {})},
         "identifiers": {"cloud": {"provider": r["provider"], "resourceId": r["resourceId"]}},
         "notes": f"Found by {source} but not declared in IaC or purchasing records. Review and adopt or remove."}
    if not c["virtual"]["region"]:
        del c["virtual"]["region"]
    if not c["virtual"]["tags"]:
        del c["virtual"]["tags"]
    if loc:
        c["placement"] = {"location": loc}
    if compute:
        c["compute"] = compute
    if fin:
        c["financial"] = fin
    ref = builder.upsert({"component": c, "keys": keys, "refHint": f"shadow-{slug(r.get('shortId') or r['name'], 40)}",
                          "status": "discovered-unmanaged", "at": at, "force": True,
                          "note": f"discovered by {source}", "source": {"kind": source, "id": r["resourceId"]}})
    comp = builder.component(ref)
    _apply_state(builder, comp, r, at, source)
    discovered = {"class": "cloud-resource", "name": r.get("name"),
                  "virtual": {k: v for k, v in c["virtual"].items() if k in ("provider", "resourceType", "resourceId", "instanceType", "tags")}}
    if fin:
        discovered["estimatedMonthlyCost"] = fin["recurringCost"]["amount"]["amount"]
    builder.observe({"id": _obs_id("unmanaged", r.get("shortId") or r["resourceId"]), "observedAt": at,
                     "source": source, "kind": "unmanaged", "component": ref,
                     "severity": "high" if compute else "medium", "status": "open", "discovered": discovered})
    return ref

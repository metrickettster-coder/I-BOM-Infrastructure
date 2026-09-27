"""Automated drift detection: desired state vs approved baseline vs live state.

Three layers, all inside the I-BOM:

  configuration   what engineering declared for a line (set by IaC ingestion or `apply_desired`)
  baselines[]     approved snapshots of those values (`freeze_baseline`), with a digest
  live snapshots  what Redfish, cloud APIs, Terraform refresh, gNMI/agent exports report (live.py)

`detect` compares live values with the approved expectation for every tracked
attribute and writes the result as observations:

  kind "drift"  one per line / attribute / unit that differs; reopened if it comes back,
                marked remediated when the live value matches again
  kind "match"  one per line (and unit) whose tracked attributes all match

It also flags blueprint drift: a line's `configuration` edited after the
baseline without an approved change record. Each line's `drift` policy decides
what happens next (alert, ticket, auto-remediate hint, accept-and-update-bom
with a deviation change record, or ignore).
"""
import copy
import hashlib
import json
import re

from ibom_ingest.core import slug

from .live import merge, norm, slug_attr

DISCOVERY_ENUM = {"redfish", "ipmi", "snmp", "lldp", "gnmi", "netconf", "ssh", "dcgm", "kubernetes-api",
                  "cloud-api", "terraform-state", "cmdb", "dcim", "agent", "manual-audit"}
SOURCE_ALIASES = {"ansible-facts": "agent", "terraform-plan": "terraform-state", "eapi": "ssh"}
SKIP_STATUS = {"decommissioned", "disposed", "discovered-unmanaged", "planned", "quoted"}
SEVERITY = ["info", "low", "medium", "high", "critical"]
# attribute -> base severity; first matching pattern wins
SEVERITY_RULES = [
    (r"/(firmware|bios|bmc|ami|version|osVersion|eosVersion|nosVersion|releaseVersion)\b", "high"),
    (r"/(encrypt|versioning|publicAccess|tls|ssh|snmp|aaa|acl|exists)", "high"),
    (r"/tags/", "low"),
]


# ---------- JSON pointers ----------

def ptr_parts(pointer):
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")] if pointer else []


def ptr_get(obj, pointer):
    """Value at pointer, or KeyError if any step is absent."""
    for p in ptr_parts(pointer):
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        elif isinstance(obj, list) and p.isdigit() and int(p) < len(obj):
            obj = obj[int(p)]
        else:
            raise KeyError(pointer)
    return obj


def ptr_set(obj, pointer, value):
    parts = ptr_parts(pointer)
    for p in parts[:-1]:
        obj = obj.setdefault(p, {})
    obj[parts[-1]] = value


def leaves(obj, prefix=""):
    """JSON pointers of every scalar (or list) leaf under obj."""
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            yield from leaves(v, f"{prefix}/{k.replace('~', '~0').replace('/', '~1')}")
    else:
        yield prefix


# ---------- desired state ----------

def matches(builder, c, rule):
    m = rule.get("match", {})
    if "ref" in m and c["bom-ref"] not in ([m["ref"]] if isinstance(m["ref"], str) else m["ref"]):
        return False
    for f in ("class", "category"):
        if f in m and c.get(f) != m[f]:
            return False
    if "mpn" in m and not re.search(m["mpn"], c.get("mpn") or c.get("model") or "", re.I):
        return False
    if "name" in m and not re.search(m["name"], c.get("name", ""), re.I):
        return False
    if "resourceType" in m and c.get("virtual", {}).get("resourceType") != m["resourceType"]:
        return False
    if "provider" in m and c.get("virtual", {}).get("provider") != m["provider"]:
        return False
    return c.get("lifecycle", {}).get("status") not in ("decommissioned", "disposed", "discovered-unmanaged")


def apply_desired(builder, spec, at):
    """Merge a golden-configuration file into matching lines: configuration, drift policy, criticality.

    spec = {"id": "GOLDEN-2026-09", "rules": [{"match": {...}, "configuration": {...},
             "drift": {"onDrift": "alert", "tolerance": {...}}, "criticality": "high"}]}
    """
    touched = []
    for rule in spec["rules"]:
        for c in list(builder.doc["components"]):
            if not matches(builder, c, rule):
                continue
            part = {}
            if rule.get("configuration"):
                part["configuration"] = copy.deepcopy(rule["configuration"])
            if rule.get("drift"):
                part["drift"] = copy.deepcopy(rule["drift"])
            if rule.get("criticality"):
                part["criticality"] = rule["criticality"]
            builder.upsert({"component": part, "ref": c["bom-ref"],
                            "source": {"kind": "desired-state", "id": spec.get("id", "desired-state")}})
            if c["bom-ref"] not in touched:
                touched.append(c["bom-ref"])
    return touched


def tracked(c):
    """Pointers drift detection watches on a line: drift.attributes, else every configuration leaf."""
    pol = c.get("drift", {})
    if pol.get("tracked") is False or pol.get("onDrift") == "ignore":
        return []
    if pol.get("attributes"):
        return list(pol["attributes"])
    return [f"/configuration{p}" for p in leaves(norm(c.get("configuration", {}))) if p]


def _in_scope(c):
    return c.get("lifecycle", {}).get("status") not in SKIP_STATUS


# ---------- baselines ----------

def _digest(expected):
    canon = json.dumps(sorted(((e["component"], e["attribute"], e["value"]) for e in expected), key=str),
                       sort_keys=True, separators=(",", ":"))
    return {"alg": "SHA-256", "content": hashlib.sha256(canon.encode()).hexdigest()}


def current_values(doc, scope=None):
    out = []
    for c in doc["components"]:
        if scope and c["bom-ref"] not in scope or not _in_scope(c):
            continue
        cfg = {"configuration": norm(c.get("configuration", {}))}
        for p in tracked(c):
            try:
                out.append({"component": c["bom-ref"], "attribute": p, "value": ptr_get(cfg, p)})
            except KeyError:
                pass
    return out


def freeze_baseline(builder, at, name=None, scope=None, approved_by=None):
    """Append an approved baseline of the tracked values. Returns its id, or None if nothing changed
    since the latest baseline (so running it on a schedule is harmless)."""
    doc = builder.doc
    expected = current_values(doc, scope)
    digest = _digest(expected)
    latest = latest_baseline(doc)
    if latest and latest.get("digest") == digest and latest.get("scope", []) == sorted(scope or []):
        return None
    n = len(doc.get("baselines", [])) + 1
    bl = {"id": f"BL-{n:03d}", "name": name or f"Baseline {n}", "createdAt": at, "bomVersion": doc.get("version", 1),
          "scope": sorted(scope or []), "digest": digest, "expected": expected}
    if approved_by:
        bl["approvedBy"] = {"name": approved_by}
    doc.setdefault("baselines", []).append(bl)
    return bl["id"]


def latest_baseline(doc):
    bls = doc.get("baselines", [])
    return max(bls, key=lambda b: b["createdAt"]) if bls else None


def _approved_change(doc, ref, since):
    return any(ref in ch.get("affects", []) and ch["status"] in ("approved", "implemented")
               and (ch.get("effectiveDate") or "9999") >= since[:10]
               for ch in doc.get("changes", []))


def expectations(doc):
    """(ref, pointer) -> (expected value, baseline id | None, blueprint drift (baseline value) | None)."""
    bl = latest_baseline(doc)
    base = {}
    if bl:
        for e in bl.get("expected", []):
            base[(e["component"], e["attribute"])] = e["value"]
    out = {}
    for e in current_values(doc):
        key = (e["component"], e["attribute"])
        if key in base and base[key] != e["value"]:
            if _approved_change(doc, e["component"], bl["createdAt"]):
                out[key] = (e["value"], None, None)       # approved since the baseline: config wins
            else:
                out[key] = (base[key], bl["id"], e["value"])  # unapproved edit: baseline wins, flag it
        else:
            out[key] = (e["value"], bl["id"] if key in base else None, None)
    return out


# ---------- detection ----------

def _equal(expected, actual, tol=None):
    if tol is not None and isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(expected - actual) <= tol
    if isinstance(expected, (int, float)) and isinstance(actual, str):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(map(json.dumps, expected)) == sorted(map(json.dumps, actual))
    return expected == actual


def severity(c, pointer):
    sev = next((s for pat, s in SEVERITY_RULES if re.search(pat, pointer)), "medium")
    i = SEVERITY.index(sev)
    crit = c.get("criticality")
    if crit == "critical":
        i = min(i + 1, len(SEVERITY) - 1)
    elif crit == "low":
        i = max(i - 1, 1)
    return SEVERITY[i]


def _obs_id(kind, ref, pointer=None, unit=None):
    parts = [f"OBS-{kind.upper()}", slug(ref, 60)]
    if pointer:
        parts.append(slug_attr(pointer))
    if unit:
        parts.append(slug(unit, 30))
    return "-".join(parts)


def _existing(doc, oid):
    return next((o for o in doc.get("observations", []) if o["id"] == oid), None)


def detect(builder, snapshots, at):
    """Compare live snapshots with expectations; write observations and apply drift policies.

    Returns {"drift": [obs ids opened or still open], "remediated": [...], "accepted": [...],
             "matched": [refs], "blueprint": [...], "notCovered": [(ref, pointer)] tracked but not reported,
             "unmonitored": [refs] with tracked attributes and no live source at all}.
    """
    doc = builder.doc
    exp = expectations(doc)
    live = merge(snapshots)
    result = {"drift": [], "remediated": [], "accepted": [], "matched": [], "blueprint": [], "notCovered": []}

    # blueprint drift: configuration edited after the baseline without an approved change
    for (ref, p), (expected, bl_id, edited) in sorted(exp.items()):
        if edited is None:
            continue
        oid = _obs_id("drift", ref, p, "bom")
        _open(builder, {"id": oid, "observedAt": at, "source": "manual-audit", "kind": "drift", "component": ref,
                        "baseline": bl_id, "attribute": p, "expected": expected, "actual": edited,
                        "severity": "medium", "discovered": {"what": "configuration changed after baseline "
                                                             "without an approved change record"}})
        result["blueprint"].append(oid)

    by_ref = {}
    for (ref, unit), view in live.items():
        by_ref.setdefault(ref, []).append((unit, view))

    for c in doc["components"]:
        ref = c["bom-ref"]
        if not _in_scope(c) or ref not in by_ref:
            continue
        pol = c.get("drift", {})
        tol = pol.get("tolerance", {})
        pointers = set(tracked(c))
        seen_sources, covered = set(), set()
        for unit, view in by_ref[ref]:
            all_match, compared = True, 0
            # attributes a source says were intended (Terraform refresh) count even if not tracked
            # (unless the line already tracks something under that key, which then takes precedence)
            extra = {f"/configuration/{k}": v for k, v in view.items() if v.get("expected") is not None
                     and not any(q == f"/configuration/{k}" or q.startswith(f"/configuration/{k}/") for q in pointers)}
            for p in sorted(pointers | set(extra)):
                parts = ptr_parts(p)
                if len(parts) < 2 or parts[0] != "configuration" or parts[1] not in view:
                    continue  # no source reported this block for this line
                rep = view[parts[1]]
                try:
                    actual = ptr_get({parts[1]: rep["value"]}, "/" + "/".join(parts[1:]))
                except KeyError:
                    actual = None  # the source reports this block but the key is gone (e.g. a removed tag)
                if (ref, p) in exp:
                    expected, bl_id, _ = exp[(ref, p)]
                elif rep.get("expected") is not None:
                    try:
                        expected = ptr_get({parts[1]: rep["expected"]}, "/" + "/".join(parts[1:]))
                    except KeyError:
                        continue
                    bl_id = None
                else:
                    continue
                compared += 1
                covered.add(p)
                seen_sources.add(rep["source"])
                oid = _obs_id("drift", ref, p, unit)
                if _equal(expected, actual, tol.get(p)):
                    prev = _existing(doc, oid)
                    if prev and prev.get("status") != "remediated":  # live is back on the expected value
                        prev.update(status="remediated", observedAt=rep["at"], actual=actual, source=rep["source"])
                        result["remediated"].append(oid)
                    continue
                all_match = False
                obs = {"id": oid, "observedAt": rep["at"], "source": rep["source"], "kind": "drift", "component": ref,
                       "attribute": p, "expected": expected, "actual": actual, "severity": severity(c, p)}
                if bl_id:
                    obs["baseline"] = bl_id
                if unit:
                    obs["discovered"] = {"unit": unit}
                if pol.get("onDrift") == "accept-and-update-bom":
                    _accept(builder, c, p, expected, actual, rep, obs)
                    result["accepted"].append(oid)
                else:
                    _open(builder, obs)
                    result["drift"].append(oid)
            mid = _obs_id("match", ref, None, unit)
            if compared and all_match:
                srcs = sorted({view[k]["source"] for k in view})
                builder.observe({"id": mid, "observedAt": max(view[k]["at"] for k in view), "source": ",".join(srcs),
                                 "kind": "match", "component": ref, "severity": "info",
                                 **({"baseline": exp_baseline(exp, ref)} if exp_baseline(exp, ref) else {}),
                                 **({"discovered": {"unit": unit}} if unit else {})})
                if ref not in result["matched"]:
                    result["matched"].append(ref)
            elif compared:
                doc["observations"] = [o for o in doc.get("observations", []) if o["id"] != mid]
        if seen_sources:
            _record_policy(c, seen_sources)
        result["notCovered"] += [(ref, p) for p in sorted(pointers - covered)]
    result["unmonitored"] = [c["bom-ref"] for c in doc["components"]
                             if _in_scope(c) and tracked(c) and c["bom-ref"] not in by_ref]
    return result


def exp_baseline(exp, ref):
    return next((bl for (r, _), (_, bl, _) in exp.items() if r == ref and bl), None)


def _open(builder, obs):
    prev = _existing(builder.doc, obs["id"])
    obs = {k: v for k, v in obs.items() if v is not None or k in ("expected", "actual")}
    if prev is None:
        builder.observe({**obs, "status": "open"})
        return
    same = prev.get("actual") == obs.get("actual") and prev.get("expected") == obs.get("expected")
    if prev.get("status") in ("accepted", "false-positive") and same:
        return  # someone already triaged exactly this deviation
    status = prev.get("status", "open") if same and prev.get("status") in ("open", "acknowledged") else "open"
    prev.update({**obs, "status": status})


def _accept(builder, c, pointer, expected, actual, rep, obs):
    """Policy accept-and-update-bom: live wins. Update configuration, log a deviation, close the finding."""
    cfg = norm(c.get("configuration", {}))
    ptr_set({"configuration": cfg}, pointer, actual)
    c["configuration"] = cfg
    day = rep["at"][:10]
    chid = f"DEV-{slug(c['bom-ref'], 40)}-{slug_attr(pointer)}-{day}"
    changes = builder.doc.setdefault("changes", [])
    if not any(ch["id"] == chid for ch in changes):
        changes.append({"id": chid, "type": "deviation", "status": "implemented",
                        "title": f"Accepted live {pointer} on {c['bom-ref']}", "affects": [c["bom-ref"]],
                        "effectiveDate": day,
                        "reason": f"{rep['source']} reported {json.dumps(actual)} where {json.dumps(expected)} was "
                                  f"expected; drift policy accept-and-update-bom"})
    builder.add_event(c, {"at": rep["at"], "type": "config-change", "from": json.dumps(expected),
                          "to": json.dumps(actual), "changeRef": chid,
                          "note": f"{pointer} accepted from {rep['source']}"})
    builder.observe({**obs, "status": "accepted"})


def _record_policy(c, sources):
    pol = c.setdefault("drift", {})
    pol.setdefault("tracked", True)
    enum = sorted({SOURCE_ALIASES.get(s, s) for s in sources} & DISCOVERY_ENUM | set(pol.get("discoverySources", [])))
    if enum:
        pol["discoverySources"] = enum
    pol.setdefault("onDrift", "alert")


# ---------- remediation hints ----------

def remediation(doc, obs):
    """Plain-language next step for an open drift finding, from the line's policy and where it is managed."""
    c = next((x for x in doc["components"] if x["bom-ref"] == obs.get("component")), {})
    attr = obs.get("attribute", "")
    if obs.get("source") == "manual-audit":
        return "approve a change record for this edit and re-baseline, or revert the configuration"
    if c.get("virtual", {}).get("iac"):
        addr = c["virtual"]["iac"].get("address")
        return f"run terraform apply for {addr} to restore the declared value, or update the code if the change was intended"
    if "/firmware/" in attr:
        unit = obs.get("discovered", {}).get("unit")
        return (f"update {attr.rsplit('/', 1)[-1].upper()} to {obs.get('expected')}"
                + (f" on {unit}" if unit else "") + " through the BMC or the vendor's update tooling")
    key = attr.rsplit("/", 1)[-1]
    if "version" in key.lower():
        return (f"move {key} to {obs.get('expected')} through the vendor's upgrade process, "
                f"or raise a change to accept {obs.get('actual')}")
    return f"set {key} back to {json.dumps(obs.get('expected'))} or raise a change to accept it"

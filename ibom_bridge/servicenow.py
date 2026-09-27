"""ServiceNow CMDB: export I-BOM lines as CIs, read CMDB exports back, reconcile.

Export produces an Identification and Reconciliation Engine (IRE) payload for
POST /api/now/identifyreconcile, plus one CSV per CI class for Import Sets.
Each CI carries the I-BOM asset id in correlation_id, so the next export or
reconciliation finds the same record again without guessing.
"""
import base64
import json
import re
import os
import urllib.parse
import urllib.request
from pathlib import Path

from .common import (ACTIVE, RETIRED, finding, load_json, norm, normalize_rows, read_table, write_csv)

SYSTEM = "cmdb"
CSV_COLUMNS = ["correlation_id", "name", "serial_number", "asset_tag", "object_id", "manufacturer", "model_id",
               "install_status", "operational_status", "location", "cost_center", "po_number", "cost",
               "short_description", "discovery_source"]


def load_mapping(path=None):
    m = load_json("servicenow.json")
    if path:
        m.update(json.loads(Path(path).read_text()))
    return m


def ci_class(c, mapping):
    classes = mapping["ciClasses"]
    for key in (f"{c['class']}:{c.get('category', '')}", c.get("category", ""), c["class"]):
        if key in classes:
            return classes[key]
    return None


def _label(mapping, code):
    return mapping["installStatusLabels"].get(str(code), str(code))


def ci_values(a, mapping):
    """ServiceNow field values for one I-BOM asset."""
    c = a.c
    fin = c.get("financial") or {}
    erp = a.ids.get("erp") or {}
    cost = None
    if fin.get("unitCost"):
        cost = fin["unitCost"]["amount"] * (1 if a.u else c.get("quantity", 1))
    v = {
        "correlation_id": a.ref,
        "name": a.hostname or (c.get("name") if not a.u else f"{c.get('model') or c['name']} {a.serial}"),
        "serial_number": a.serial,
        "asset_tag": a.asset_tag,
        "object_id": a.cloud_id,
        "manufacturer": a.d.org_name(c.get("manufacturer")) if c.get("manufacturer") else None,
        "model_id": c.get("model") or c.get("mpn"),
        "install_status": mapping["installStatus"].get(a.status),
        "operational_status": mapping["operationalStatus"].get(a.status),
        "location": a.d.loc_name(a.location) if a.location else None,
        "cost_center": fin.get("costCenter"),
        "po_number": erp.get("purchaseOrder"),
        "cost": round(cost, 2) if cost is not None else None,
        "short_description": c.get("name"),
        "discovery_source": "I-BOM",
    }
    return {k: val for k, val in v.items() if val not in (None, "")}


def export(d, mapping, out_dir=None):
    """Build the IRE payload (and per-class CSVs when out_dir is given). Returns (payload, skipped)."""
    items, index, skipped = [], {}, []
    for a in d.assets():
        cls = ci_class(a.c, mapping)
        if not cls:
            skipped.append((a.ref, f"{a.c['class']}:{a.c.get('category', '')} is not a CI in the mapping"))
            continue
        if a.c["class"] == "hardware" and not a.serialized:
            skipped.append((a.ref, "hardware without a serial number cannot be identified in the CMDB"))
            continue
        if a.status in ("planned", "quoted"):
            skipped.append((a.ref, f"status {a.status}: not ordered yet"))
            continue
        index.setdefault(a.c["bom-ref"], []).append(len(items))
        items.append({"className": cls, "values": ci_values(a, mapping), "internal_id": a.ref})
    relations = []
    for r in d.doc.get("relationships", []):
        rel = mapping["relations"].get(r["type"])
        for p in index.get(r["from"], []) if rel else []:
            for ch in index.get(r["to"], []):
                relations.append({"type": rel, "parent": p, "child": ch})
    payload = {"items": items, "relations": relations}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "servicenow-ire.json").write_text(json.dumps(payload, indent=2) + "\n")
        by_class = {}
        for it in items:
            by_class.setdefault(it["className"], []).append(it["values"])
        for cls, rows in sorted(by_class.items()):
            write_csv(out / f"servicenow-{cls}.csv", rows, CSV_COLUMNS)
    return payload, skipped


# ---------- reading exports ----------

def _plain(v):
    """Table API values may be {"value", "display_value"} (sysparm_display_value=all) or {"link", "value"}."""
    if isinstance(v, dict):
        return v.get("display_value") or v.get("value") or ""
    return "" if v is None else str(v)


def _raw(v):
    return v.get("value", "") if isinstance(v, dict) else ("" if v is None else str(v))


def read_export(paths, mapping):
    """CI records from one or more CMDB exports (Table API JSON, CSV or XLSX)."""
    labels = {norm(v): int(k) for k, v in mapping["installStatusLabels"].items()}
    records = []
    for p in paths:
        cls_default = Path(p).stem if Path(p).stem.startswith("cmdb_ci") else ""
        raw_rows = read_table(p)
        for raw, r in zip(raw_rows, normalize_rows(raw_rows, mapping["exportFields"])):
            rec = {k: _plain(v) for k, v in r.items()}
            status = raw.get("install_status", r.get("install_status"))
            code = _raw(status) if isinstance(status, dict) else str(status or "")
            rec["install_status"] = int(code) if code.isdigit() else labels.get(norm(code))
            rec["sys_class_name"] = rec.get("sys_class_name") or cls_default
            rec["_source"] = Path(p).name
            records.append(rec)
    return records


# ---------- reconciliation ----------

def _model_tokens(v):
    return {t for t in re.split(r"[^A-Z0-9]+", str(v or "").upper()) if len(t) >= 4 and any(ch.isdigit() for ch in t)}


def _same_model(bom, cmdb):
    """Systems name models differently (R760XA-CTO-L40S vs PowerEdge R760xa); a shared model token is enough."""
    a, b = norm(bom), norm(cmdb)
    return not a or not b or a in b or b in a or bool(_model_tokens(bom) & _model_tokens(cmdb))


def cloud_key(resource_id):
    """Compare cloud ids by their last segment: an ARN in the I-BOM, a bare instance id in the CMDB."""
    return re.split(r"[/:]", str(resource_id or "").strip())[-1] if resource_id else ""


def _same_org(bom, cmdb):
    a, b = norm(bom), norm(cmdb)
    return not a or not b or a[:4] == b[:4]


def _compatible(mapping, expected, actual):
    return not actual or actual == expected or actual in mapping["compatibleClasses"].get(expected, [])


def reconcile(d, records, mapping):
    """Match I-BOM assets to CMDB records. Returns (findings, matches {asset ref: (asset, record)})."""
    findings, matches = [], {}
    by = {k: {} for k in ("sys_id", "correlation_id", "object_id", "serial_number", "name")}
    for i, r in enumerate(records):
        for k in by:
            key = norm(r.get(k)) if k in ("serial_number", "name") else (
                cloud_key(r.get(k)) if k == "object_id" else (r.get(k) or ""))
            if key:
                by[k].setdefault(key, []).append(i)
    used = set()

    def rec_label(r):
        return f"{r.get('sys_class_name') or 'CI'} {r.get('name') or r.get('sys_id')}"

    for a in d.assets():
        cls = ci_class(a.c, mapping)
        cm = a.ids.get("cmdb") or {}
        name = a.hostname or (a.c.get("name") if a.c["class"] in ("cloud-resource", "virtual") else None)
        probes = [("sys_id", cm.get("sysId")), ("correlation_id", a.ref), ("object_id", cloud_key(a.cloud_id)),
                  ("serial_number", norm(a.serial) if a.serial else None),
                  ("name", norm(name) if name else None)]
        hit, how = [], None
        for field, key in probes:
            if key and by[field].get(key):
                hit = [i for i in by[field][key] if i not in used]
                if hit:
                    how = field
                    break
        if not hit:
            if cls and a.status not in ("planned", "quoted") and (a.serialized or a.c["class"] != "hardware"):
                sev = "high" if a.status in ACTIVE else "medium"
                findings.append(finding(SYSTEM, "missing-in-cmdb", sev,
                                        f"{cls} for {a.c.get('name')} ({a.status}) has no CMDB record",
                                        "Create the CI from the export (servicenow-ire.json)", asset=a))
            continue
        # Other unclaimed CIs carrying the same serial number or cloud id are duplicates of this one.
        for field, key in (("serial_number", norm(a.serial) if a.serial else None),
                           ("object_id", cloud_key(a.cloud_id))):
            if key:
                hit += [i for i in by[field].get(key, []) if i not in used and i not in hit]
        if len(hit) > 1:
            dups = [records[i] for i in hit]
            findings.append(finding(SYSTEM, "duplicate-ci", "high",
                                    f"{len(hit)} CIs match {how.replace('_', ' ')} {a.serial or a.hostname or a.cloud_id}: "
                                    + ", ".join(f"{x.get('sys_id')} ({x.get('sys_class_name')})" for x in dups),
                                    "Merge the duplicates in the CMDB and keep the one with the I-BOM correlation_id",
                                    asset=a, record=", ".join(x.get("sys_id", "") for x in dups)))
            used.update(hit)
        i = hit[0]
        used.add(i)
        r = records[i]
        matches[a.ref] = (a, r)
        rid = r.get("sys_id", "")
        if how == "name":
            findings.append(finding(SYSTEM, "weak-match", "low",
                                    f"Matched {rec_label(r)} by name only", "Confirm, then set its serial "
                                    "number or correlation_id so the next run matches exactly", asset=a, record=rid))
        if a.status == "discovered-unmanaged":
            findings.append(finding(SYSTEM, "shadow-known-to-cmdb", "medium",
                                    f"{a.c.get('name')} is unmanaged in the I-BOM but the CMDB already tracks it as "
                                    f"{rec_label(r)}", "Adopt it into the I-BOM (owner, cost center, PO) or retire the CI",
                                    asset=a, record=rid))
        # field comparisons
        if cls and not _compatible(mapping, cls, r.get("sys_class_name")):
            findings.append(finding(SYSTEM, "class-mismatch", "low", f"I-BOM maps this to {cls}, CMDB has "
                                    f"{r.get('sys_class_name')}", "Reclassify the CI or adjust mappings/servicenow.json",
                                    asset=a, record=rid, field="sys_class_name", bom=cls, other=r.get("sys_class_name")))
        if a.serial and r.get("serial_number") and norm(a.serial) != norm(r["serial_number"]):
            findings.append(finding(SYSTEM, "field-mismatch", "high", "Serial number differs", "Check the physical "
                                    "label; one system has the wrong unit", asset=a, record=rid, field="serial_number",
                                    bom=a.serial, other=r["serial_number"]))
        want = mapping["installStatus"].get(a.status)
        got = r.get("install_status")
        if want and got and want != got:
            sev = "high" if (got == 7 and a.status in ACTIVE) or (a.status in RETIRED and got == 1) else "medium"
            findings.append(finding(SYSTEM, "status-mismatch", sev,
                                    f"I-BOM says {a.status} ({_label(mapping, want)}), CMDB says {_label(mapping, got)}",
                                    "Update the CMDB install status" if a.status not in ("discovered-unmanaged",)
                                    else "Confirm which system is right", asset=a, record=rid, field="install_status",
                                    bom=_label(mapping, want), other=_label(mapping, got)))
        model = a.c.get("model") or a.c.get("mpn")
        if r.get("model_id") and not _same_model(model, r["model_id"]):
            findings.append(finding(SYSTEM, "field-mismatch", "medium", "Model differs", "Correct the CI model",
                                    asset=a, record=rid, field="model_id", bom=model, other=r["model_id"]))
        mfr = d.org_name(a.c.get("manufacturer")) if a.c.get("manufacturer") else ""
        if r.get("manufacturer") and not _same_org(mfr, r["manufacturer"]):
            findings.append(finding(SYSTEM, "field-mismatch", "low", "Manufacturer differs", None, asset=a,
                                    record=rid, field="manufacturer", bom=mfr, other=r["manufacturer"]))
        cc = (a.c.get("financial") or {}).get("costCenter")
        if cc and r.get("cost_center") and norm(cc) != norm(r["cost_center"]):
            findings.append(finding(SYSTEM, "field-mismatch", "medium", "Cost center differs",
                                    "Align the CI cost center with the ERP/I-BOM value", asset=a, record=rid,
                                    field="cost_center", bom=cc, other=r["cost_center"]))
        loc = d.loc_name(a.location) if a.location else ""
        if loc and r.get("location") and norm(loc) != norm(r["location"]):
            findings.append(finding(SYSTEM, "field-mismatch", "info", "Location differs",
                                    "Update whichever is stale; the I-BOM placement may still be the receiving location",
                                    asset=a, record=rid, field="location", bom=loc, other=r["location"]))
        if a.asset_tag and r.get("asset_tag") and norm(a.asset_tag) != norm(r["asset_tag"]):
            findings.append(finding(SYSTEM, "field-mismatch", "medium", "Asset tag differs", None, asset=a,
                                    record=rid, field="asset_tag", bom=a.asset_tag, other=r["asset_tag"]))

    for i, r in enumerate(records):
        if i in used or r.get("install_status") == 7:
            continue
        findings.append(f := finding(SYSTEM, "orphan-ci", "medium",
                                f"{rec_label(r)} (serial {r.get('serial_number') or 'none'}) is in the CMDB but not in "
                                "the I-BOM", "Add it to the I-BOM if it belongs to this project, otherwise leave it "
                                "or retire it in the CMDB", record=r.get("sys_id", "")))
        if r.get("serial_number"):
            f["serial"] = r["serial_number"].strip()
    return findings, matches


def write_back(matches, mapping):
    """Store CMDB keys on matched assets. Returns the number of assets changed."""
    n = 0
    for a, r in matches.values():
        n += a.set_ids("cmdb", {"system": mapping.get("system", "ServiceNow"), "sysId": r.get("sys_id"),
                                "ciClass": r.get("sys_class_name"), "ciId": r.get("name")})
    return n


# ---------- live instance (optional) ----------

def _request(instance, path, method="GET", body=None, user=None, password=None):
    user = user or os.environ.get("SN_USER")
    password = password or os.environ.get("SN_PASSWORD")
    if not (user and password):
        raise SystemExit("set SN_USER and SN_PASSWORD (a ServiceNow account with CMDB write access)")
    base = instance if instance.startswith("https://") else f"https://{instance}.service-now.com"
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode() or "{}")


def pull(instance, classes, out, mapping, query=None, limit=10000):
    """Export CIs of the given classes through the Table API into one JSON file shaped like {"result": [...]}."""
    fields = ",".join(list(mapping["exportFields"]))
    result = []
    for cls in classes:
        q = {"sysparm_fields": fields, "sysparm_display_value": "all", "sysparm_limit": str(limit)}
        if query:
            q["sysparm_query"] = query
        data = _request(instance, f"/api/now/table/{cls}?" + urllib.parse.urlencode(q))
        for r in data.get("result", []):
            r.setdefault("sys_class_name", cls)
            result.append(r)
    Path(out).write_text(json.dumps({"result": result}, indent=2) + "\n")
    return len(result)


def push(instance, payload, data_source="ServiceNow"):
    """Send the IRE payload. IRE creates or updates CIs using the instance's identification rules."""
    q = urllib.parse.urlencode({"sysparm_data_source": data_source})
    return _request(instance, f"/api/now/identifyreconcile?{q}", "POST", payload)

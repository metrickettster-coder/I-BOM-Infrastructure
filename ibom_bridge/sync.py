"""One synchronized view: reconcile the I-BOM against ERP and CMDB together, write keys back, report."""
from collections import Counter

from . import erp, servicenow
from .common import SEVERITY_ORDER, norm, sort_findings, today

VIEW_COLUMNS = ["bom_ref", "serial", "name", "class", "category", "ibom_status", "quantity", "po", "po_line",
                "erp_po_qty", "erp_unit_price", "ibom_unit_cost", "erp_asset", "book_value", "cmdb_sys_id",
                "cmdb_class", "cmdb_install_status", "findings", "worst_severity", "notes"]


def run(d, cmdb_mapping=None, cmdb_records=None, erp_mapping=None, po=None, receipts=None, invoices=None,
        assets=None, write_back=False, erp_system=None, as_of=None):
    """Reconcile whichever sources are given. Returns a result dict; mutates d.doc when write_back is set."""
    findings, result = [], {"sources": []}
    cmdb_matches, erp_matches = {}, {"po": {}, "assets": []}
    if cmdb_records is not None:
        f, cmdb_matches = servicenow.reconcile(d, cmdb_records, cmdb_mapping)
        findings += f
        result["sources"].append(f"CMDB: {len(cmdb_records)} CIs")
    if any(x is not None for x in (po, receipts, invoices, assets)):
        f, erp_matches = erp.reconcile(d, erp_mapping, po, receipts, invoices, assets)
        findings += f
        for name, rows in (("PO lines", po), ("goods receipts", receipts), ("invoice lines", invoices),
                           ("fixed assets", assets)):
            if rows is not None:
                result["sources"].append(f"ERP: {len(rows)} {name}")
    _cross_reference(findings)
    findings = sort_findings(findings)
    written = 0
    if write_back:
        if cmdb_records is not None:
            written += servicenow.write_back(cmdb_matches, cmdb_mapping)
        written += erp.write_back(erp_matches["assets"], erp_system)
    result.update(findings=findings, view=view(d, findings, cmdb_matches, erp_matches, cmdb_mapping, erp_mapping, as_of),
                  summary=summary(findings), written=written)
    return result


def _cross_reference(findings):
    """An item missing from the I-BOM but present in both the CMDB and the ERP is almost certainly real equipment."""
    orphans = {norm(f["serial"]): f for f in findings if f["kind"] == "orphan-ci" and f.get("serial")}
    for f in findings:
        o = orphans.get(norm(f.get("serial"))) if f["kind"] == "asset-not-in-bom" and f.get("serial") else None
        if o:
            o["message"] += f"; the ERP also carries it as {f['record']}"
            f["message"] += f"; the CMDB also has it as CI {o['record']}"
            f["severity"] = o["severity"] = "high"
            f["action"] = o["action"] = ("Both systems know this unit: add it to the I-BOM if it serves this project, "
                                         "otherwise record where it belongs")


def summary(findings):
    return {"total": len(findings),
            "bySeverity": dict(sorted(Counter(f["severity"] for f in findings).items(),
                                      key=lambda kv: SEVERITY_ORDER[kv[0]])),
            "byKind": dict(Counter(f"{f['system']}:{f['kind']}" for f in findings).most_common())}


def view(d, findings, cmdb_matches, erp_matches, cmdb_mapping, erp_mapping=None, as_of=None):
    """One row per I-BOM asset with what the ERP and the CMDB say about it, then rows only other systems have."""
    per_asset, per_line = {}, {}
    for f in findings:
        if "bomRef" in f:
            key = (f["bomRef"], f.get("serial"))
            per_asset.setdefault(key, []).append(f)
            per_line.setdefault(f["bomRef"], []).append(f)
    po_by_ref = {}
    for key, (comps, r) in erp_matches["po"].items():
        for c in comps:
            po_by_ref[c["bom-ref"]] = r
    fa_by_ref = {a.ref: r for a, r in erp_matches["assets"]}
    nbv = {r["bom_ref"]: r.get("nbv") for r in erp.asset_rows(d, erp_mapping, as_of or today())[0]} if erp_mapping else {}
    labels = (cmdb_mapping or {}).get("installStatusLabels", {})
    rows = []
    for a in d.assets():
        c = a.c
        erp_ids = a.ids.get("erp") or {}
        po_row = po_by_ref.get(c["bom-ref"]) or {}
        fa = fa_by_ref.get(a.ref) or {}
        ci = (cmdb_matches.get(a.ref) or (None, {}))[1]
        fs = per_asset.get((c["bom-ref"], a.serial), [])
        if a.serial:  # line-level findings (PO price, quantity) apply to every unit on the line
            fs = fs + [f for f in per_line.get(c["bom-ref"], []) if "serial" not in f]
        worst = min((f["severity"] for f in fs), key=SEVERITY_ORDER.get, default="")
        rows.append({"bom_ref": c["bom-ref"], "serial": a.serial, "name": c.get("name"), "class": c["class"],
                     "category": c.get("category"), "ibom_status": a.status, "quantity": a.quantity,
                     "po": erp_ids.get("purchaseOrder"), "po_line": erp_ids.get("poLine"),
                     "erp_po_qty": po_row.get("quantity"), "erp_unit_price": po_row.get("unit_price"),
                     "ibom_unit_cost": ((c.get("financial") or {}).get("unitCost") or {}).get("amount"),
                     "erp_asset": fa.get("asset_number") or erp_ids.get("assetNumber"), "book_value": nbv.get(a.ref),
                     "cmdb_sys_id": ci.get("sys_id") or (a.ids.get("cmdb") or {}).get("sysId"),
                     "cmdb_class": ci.get("sys_class_name"),
                     "cmdb_install_status": labels.get(str(ci.get("install_status")), ci.get("install_status")),
                     "findings": len(fs), "worst_severity": worst,
                     "notes": "; ".join(f"{f['kind']}" + (f" ({f['field']})" if f.get("field") else "") for f in fs)})
    for f in findings:
        if "bomRef" not in f:
            rows.append({"bom_ref": "", "name": f["message"], "class": f"{f['system']} only",
                         "findings": 1, "worst_severity": f["severity"], "notes": f["kind"],
                         "cmdb_sys_id": f.get("record") if f["system"] == "cmdb" else None,
                         "erp_asset": f.get("record") if f["system"] == "erp" and f["kind"] == "asset-not-in-bom"
                         else None})
    return rows


def render(result, title="I-BOM / ERP / CMDB reconciliation"):
    s = result["summary"]
    lines = [f"# {title}", "", "Sources: " + "; ".join(result["sources"]), "",
             f"**{s['total']} findings**: " + ", ".join(f"{n} {sev}" for sev, n in s["bySeverity"].items()), ""]
    if result.get("written"):
        lines += [f"Wrote CMDB and ERP keys back onto {result['written']} I-BOM assets.", ""]
    lines += ["| Severity | System | Finding | I-BOM line | Record | Detail | Action |", "|---|---|---|---|---|---|---|"]
    for f in result["findings"]:
        ref = (f["bomRef"] + (f" #{f['serial']}" if f.get("serial") else "")) if f.get("bomRef") else "(not in I-BOM)"
        detail = f["message"]
        if f.get("field") and "bom" in f:
            detail += f" (I-BOM `{f['bom']}`, {f['system'].upper()} `{f.get('other')}`)"
        lines.append(f"| {f['severity']} | {f['system'].upper()} | {f['kind']} | {ref} | {f.get('record', '')} | "
                     f"{detail.replace('|', '/')} | {f.get('action', '')} |")
    lines += ["", "Finding counts by kind: " + ", ".join(f"{k} {n}" for k, n in s["byKind"].items()), ""]
    return "\n".join(lines)

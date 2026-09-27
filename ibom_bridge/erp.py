"""ERP: export PO lines, fixed assets, depreciation and recurring cloud spend; reconcile ERP exports.

Works with any ERP that can export and import CSV. Column layouts come from
mappings/erp.json profiles (generic, sap, oracle), and reading accepts any of
the aliases listed there, so an SAP ME2M/AR01 download, an Oracle FA export or
a hand-made spreadsheet all read the same way.
"""
import json
from datetime import date
from pathlib import Path

from .common import (ACTIVE, RECEIVED_OR_LATER, RETIRED, finding, in_service_date, load_json, norm,
                     normalize_rows, num, read_table, write_csv)

SYSTEM = "erp"
GOODS = {"hardware", "consumable"}
RETIRED_WORDS = {"RETIRED", "DEACTIVATED", "DISPOSED", "INACTIVE", "SCRAPPED", "SOLD", "X", "TRUE", "YES", "Y"}


def load_mapping(path=None):
    m = load_json("erp.json")
    if path:
        m.update(json.loads(Path(path).read_text()))
    return m


def line_key(po, line):
    po = str(po or "").strip()
    line = str(line or "").strip()
    if line.replace(".0", "").isdigit():
        line = str(int(float(line)))
    return (po, line)


# ---------- depreciation ----------

def _month_index(d):
    return d.year * 12 + d.month - 1


def depreciation(cost, salvage, months, method, start, as_of):
    """Depreciate from the capitalization month (full-month convention).

    Returns (accumulated, net book value, [(year, depreciation, closing NBV), ...] for the whole life).
    Declining-balance methods switch to straight-line once that gives the larger charge, and
    never go below salvage value.
    """
    base = max(cost - salvage, 0.0)
    factor = {"declining-balance": 1.5, "double-declining-balance": 2.0}.get(method)
    book, schedule, by_year, acc = cost, [], {}, 0.0
    first = _month_index(start)
    elapsed = _month_index(as_of) - first + 1 if as_of >= start.replace(day=1) else 0
    for m in range(months if method != "none" else 0):
        remaining = months - m
        sl = (book - salvage) / remaining if remaining else 0.0
        charge = base / months if factor is None else max(book * factor / months, sl)
        charge = min(charge, book - salvage)
        book -= charge
        year = (first + m) // 12
        by_year[year] = by_year.get(year, 0.0) + charge
        if m < elapsed:
            acc += charge
    running = cost
    for year in sorted(by_year):
        running -= by_year[year]
        schedule.append((year, round(by_year[year], 2), round(running, 2)))
    return round(acc, 2), round(cost - acc, 2), schedule


def _life(c, mapping):
    dep = (c.get("financial") or {}).get("depreciation") or {}
    life = mapping["defaults"]["usefulLifeMonths"]
    months = dep.get("usefulLifeMonths") or life.get(c.get("category", ""), life["*"])
    method = dep.get("method") or mapping["defaults"]["depreciationMethod"]
    salvage = (dep.get("salvageValue") or {}).get("amount", 0.0)
    start = dep.get("startDate")
    return months, method, salvage, start


def is_capex_asset(a, mapping):
    fin = a.c.get("financial") or {}
    return (a.c["class"] == "hardware" and fin.get("expenseType") == "capex" and "unitCost" in fin
            and fin["unitCost"]["amount"] >= mapping["defaults"].get("capitalizationThreshold", 0))


# ---------- export ----------

def po_rows(d):
    rows = []
    for c in d.doc.get("components", []):
        erp = (c.get("identifiers") or {}).get("erp") or {}
        if not erp.get("purchaseOrder"):
            continue
        fin = c.get("financial") or {}
        price = fin.get("unitCost") or {}
        rows.append({"po": erp["purchaseOrder"], "line": erp.get("poLine"), "vendor_id": d.vendor_id(c),
                     "material": erp.get("materialNumber") or c.get("partNumber"), "mpn": c.get("mpn"),
                     "description": c.get("name", "")[:40], "quantity": c.get("quantity"),
                     "uom": c.get("unitOfMeasure", "EA"), "unit_price": price.get("amount"),
                     "currency": price.get("currency"), "cost_center": fin.get("costCenter"),
                     "gl_account": fin.get("glAccount"), "wbs": fin.get("wbsElement"),
                     "expense_type": fin.get("expenseType"), "bom_ref": c["bom-ref"]})
    return sorted(rows, key=lambda r: (r["po"], int(r["line"]) if str(r["line"]).isdigit() else 0))


def asset_rows(d, mapping, as_of):
    """Fixed-asset lines for capex hardware that has been received, with depreciation as of `as_of`."""
    rows, schedule = [], []
    for a in d.assets():
        if not is_capex_asset(a, mapping) or a.status not in RECEIVED_OR_LATER:
            continue
        c, fin = a.c, a.c["financial"]
        erp = a.ids.get("erp") or {}
        qty = a.quantity
        cost = fin["unitCost"]["amount"] * qty
        months, method, salvage, start = _life(c, mapping)
        cap = start or in_service_date(c)
        carbon = ((c.get("sustainability") or {}).get("embodiedCarbon") or {})
        row = {"asset_number": erp.get("assetNumber"), "description": (c.get("model") or c["name"])[:50],
               "serial": a.serial, "asset_tag": a.asset_tag, "quantity": qty, "cap_date": cap,
               "acquisition_value": round(cost, 2), "currency": fin["unitCost"]["currency"],
               "useful_life_months": months, "method": method, "salvage": salvage or None,
               "cost_center": fin.get("costCenter"), "gl_account": fin.get("glAccount"),
               "location": d.loc_name(a.location) if a.location else None, "po": erp.get("purchaseOrder"),
               "line": erp.get("poLine"), "invoice": erp.get("invoice"), "vendor_id": d.vendor_id(c),
               "embodied_kgco2e": round(carbon["kgCO2e"] * qty, 1) if "kgCO2e" in carbon else None,
               "scope3_category": (carbon.get("scope3Categories") or [mapping["defaults"]["scope3Category"]])[0],
               "bom_ref": a.ref, "as_of": as_of}
        if cap:
            acc, nbv, sched = depreciation(cost, salvage, months, method, date.fromisoformat(cap),
                                           date.fromisoformat(as_of))
            row.update(accumulated=acc, nbv=nbv)
            schedule += [{"bom_ref": a.ref, "serial": a.serial, "year": y, "depreciation": dep, "closing_nbv": nbv_y}
                         for y, dep, nbv_y in sched]
        rows.append(row)
    return rows, schedule


def recurring_rows(d):
    rows = []
    per_month = {"hourly": 730, "monthly": 1, "quarterly": 1 / 3, "annual": 1 / 12}
    for c in d.doc.get("components", []):
        rc = (c.get("financial") or {}).get("recurringCost")
        if not rc:
            continue
        cloud = (c.get("identifiers") or {}).get("cloud") or {}
        rows.append({"bom_ref": c["bom-ref"], "description": c.get("name"), "provider": cloud.get("provider"),
                     "resource_id": cloud.get("resourceId"),
                     "monthly_cost": round(rc["amount"]["amount"] * per_month[rc["period"]], 2),
                     "currency": rc["amount"]["currency"], "cost_center": c["financial"].get("costCenter"),
                     "gl_account": c["financial"].get("glAccount"),
                     "status": (c.get("lifecycle") or {}).get("status")})
    return rows


def _columns(profile, table):
    return profile[table]


def _rename(rows, cols, method_codes=None):
    out = []
    for r in rows:
        r = dict(r)
        if method_codes and r.get("method"):
            r["method"] = method_codes.get(r["method"], r["method"])
        out.append({cols[k]: v for k, v in r.items() if k in cols})
    return out


def export(d, mapping, out_dir, profile="generic", as_of=None):
    """Write po-lines, fixed-assets, depreciation and recurring-costs CSVs. Returns row counts."""
    as_of = as_of or date.today().isoformat()
    prof = mapping["profiles"][profile]
    codes = mapping.get("methodCodes", {}).get(profile)
    out = Path(out_dir)
    pos = po_rows(d)
    assets, schedule = asset_rows(d, mapping, as_of)
    recurring = recurring_rows(d)
    for name, table, rows in (("po-lines", "poLines", pos), ("fixed-assets", "assets", assets),
                              ("recurring-costs", "recurring", recurring)):
        cols = _columns(prof, table)
        write_csv(out / f"erp-{profile}-{name}.csv", _rename(rows, cols, codes if table == "assets" else None),
                  list(cols.values()))
    write_csv(out / f"erp-{profile}-depreciation.csv", schedule,
              ["bom_ref", "serial", "year", "depreciation", "closing_nbv"])
    return {"po-lines": len(pos), "fixed-assets": len(assets), "depreciation-rows": len(schedule),
            "recurring-costs": len(recurring)}


# ---------- reading exports ----------

def read(paths, mapping):
    rows = []
    for p in paths or []:
        raw = read_table(p)
        for r in normalize_rows(raw, {k: [] for k in mapping["profiles"]["generic"]["poLines"]} | mapping["aliases"]):
            r["_source"] = Path(p).name
            rows.append(r)
    return rows


def _is_deleted(v):
    return norm(v) in RETIRED_WORDS | {"L"}


def _is_retired(r):
    s = str(r.get("status") or "").strip()
    return bool(s) and (norm(s) in RETIRED_WORDS or s[:2] in ("19", "20"))  # a deactivation date also means retired


# ---------- reconciliation ----------

def reconcile(d, mapping, po=None, receipts=None, invoices=None, assets=None):
    """Compare the I-BOM with ERP exports. Each argument is a list of rows from `read`. Returns (findings, matches)."""
    tol = mapping["defaults"]["priceTolerance"]
    findings, matches = [], {}
    bom_lines = {}
    for c in d.doc.get("components", []):
        erp = (c.get("identifiers") or {}).get("erp") or {}
        if erp.get("purchaseOrder"):
            bom_lines.setdefault(line_key(erp["purchaseOrder"], erp.get("poLine")), []).append(c)

    if po is not None:
        erp_lines = {}
        for r in po:
            if r.get("po") and not _is_deleted(r.get("deleted")):
                erp_lines.setdefault(line_key(r["po"], r.get("line")), r)
        erp_pos = {k[0] for k in erp_lines}
        gr = _sum_qty(receipts) if receipts is not None else {k: num(r.get("gr_quantity")) for k, r in erp_lines.items()
                                                              if num(r.get("gr_quantity")) is not None}
        ir = _sum_qty(invoices) if invoices is not None else {k: num(r.get("ir_quantity")) for k, r in erp_lines.items()
                                                              if num(r.get("ir_quantity")) is not None}
        ir_amount = _sum_amount(invoices) if invoices is not None else {}
        for key, comps in sorted(bom_lines.items()):
            label = f"PO {key[0]} line {key[1]}"
            r = erp_lines.get(key)
            if r is None:
                if key[0] in erp_pos:
                    findings.append(finding(SYSTEM, "bom-line-not-on-po", "high",
                                            f"I-BOM references {label}, which is not on the PO in the ERP export "
                                            "(deleted or never created)", "Recreate the PO line or remove the "
                                            "reference", bom_ref=comps[0]["bom-ref"], record=label))
                else:
                    findings.append(finding(SYSTEM, "po-not-in-export", "info",
                                            f"PO {key[0]} is not in the ERP export", "Include it in the next export",
                                            bom_ref=comps[0]["bom-ref"], record=label))
                continue
            matches[key] = (comps, r)
            c = comps[0]
            qty = sum(x.get("quantity", 0) for x in comps)
            fin = c.get("financial") or {}
            ref = c["bom-ref"]
            erp_qty = num(r.get("quantity"))
            if erp_qty is not None and abs(erp_qty - qty) > 1e-9:
                findings.append(finding(SYSTEM, "quantity-mismatch", "high", f"{label}: I-BOM needs {qty:g}, "
                                        f"PO orders {erp_qty:g}", "Change the PO quantity or the BOM line",
                                        bom_ref=ref, record=label, field="quantity", bom=qty, other=erp_qty))
            price, erp_price = (fin.get("unitCost") or {}).get("amount"), num(r.get("unit_price"))
            if price is not None and erp_price and abs(price - erp_price) > tol * erp_price:
                kind = "actual" if fin.get("costType") == "actual" else fin.get("costType", "")
                findings.append(finding(SYSTEM, "price-mismatch", "medium",
                                        f"{label}: I-BOM {kind} unit cost {price:,.2f} vs PO price {erp_price:,.2f} "
                                        f"({(price - erp_price) / erp_price:+.1%})",
                                        "Approve the variance or correct the PO/invoice", bom_ref=ref, record=label,
                                        field="unit_price", bom=price, other=erp_price))
            for field, bom_v, name in (("cost_center", fin.get("costCenter"), "Cost center"),
                                       ("gl_account", fin.get("glAccount"), "G/L account"),
                                       ("vendor_id", d.vendor_id(c), "Vendor"),
                                       ("material", ((c.get("identifiers") or {}).get("erp") or {}).get("materialNumber"),
                                        "Material")):
                erp_v = r.get(field)
                if bom_v and erp_v and norm(bom_v).lstrip("0") != norm(erp_v).lstrip("0"):
                    findings.append(finding(SYSTEM, "field-mismatch", "medium", f"{label}: {name} differs", None,
                                            bom_ref=ref, record=label, field=field, bom=bom_v, other=erp_v))
            if c["class"] in GOODS:
                status = (c.get("lifecycle") or {}).get("status", "planned")
                got = gr.get(key)
                if got is not None:
                    if status in RECEIVED_OR_LATER and got < qty:
                        findings.append(finding(SYSTEM, "receipt-not-posted", "medium",
                                                f"{label}: I-BOM shows {status}, ERP has goods receipt for {got:g} of "
                                                f"{qty:g}", "Post the goods receipt so the invoice can clear",
                                                bom_ref=ref, record=label, field="gr_quantity", bom=qty, other=got))
                    elif status not in RECEIVED_OR_LATER and got >= qty:
                        findings.append(finding(SYSTEM, "bom-behind-erp", "low",
                                                f"{label}: ERP shows all {got:g} received, I-BOM still says {status}",
                                                "Advance the I-BOM line to received", bom_ref=ref, record=label,
                                                field="gr_quantity", bom=status, other=got))
                billed = ir.get(key)
                if billed is not None and got is not None and billed > got:
                    findings.append(finding(SYSTEM, "invoiced-not-received", "high",
                                            f"{label}: invoiced {billed:g}, received {got:g}", "Block payment until "
                                            "the goods arrive (three-way match fails)", bom_ref=ref, record=label))
            amount = ir_amount.get(key)
            billed = ir.get(key)
            if amount and billed and erp_price and abs(amount / billed - erp_price) > tol * erp_price:
                findings.append(finding(SYSTEM, "invoice-price-variance", "medium",
                                        f"{label}: invoiced at {amount / billed:,.2f} per unit vs PO {erp_price:,.2f}",
                                        "Resolve with the supplier before payment", bom_ref=ref, record=label))
        bom_pos = {k[0] for k in bom_lines}
        for key, r in sorted(erp_lines.items()):
            if key in bom_lines or key[0] not in bom_pos:
                continue
            value = (num(r.get("quantity")) or 0) * (num(r.get("unit_price")) or 0)
            findings.append(finding(SYSTEM, "procured-not-in-bom", "high" if value >= 5000 else "medium",
                                    f"PO {key[0]} line {key[1]} ({r.get('description') or r.get('material')}, "
                                    f"{value:,.2f}) is not in the I-BOM", "Add it to the BOM or cancel the PO line",
                                    record=f"PO {key[0]} line {key[1]}"))

    asset_matches = []
    if assets is not None:
        f, asset_matches = _reconcile_assets(d, mapping, assets, matches)
        findings += f

    for c in d.doc.get("components", []):
        fin = c.get("financial") or {}
        rc = fin.get("recurringCost")
        status = (c.get("lifecycle") or {}).get("status")
        if rc and (status == "discovered-unmanaged" or not fin.get("costCenter")):
            amt = rc["amount"]
            findings.append(finding(SYSTEM, "unallocated-spend", "high" if status == "discovered-unmanaged" else "low",
                                    f"{c.get('name')} costs {amt['amount']:,.2f} {amt['currency']} {rc['period']} with "
                                    f"no cost center" + (" and is unmanaged (shadow)" if status == "discovered-unmanaged"
                                                         else ""),
                                    "Assign an owner and cost center, or shut it down", bom_ref=c["bom-ref"]))
    return findings, {"po": matches, "assets": asset_matches}


def _sum_qty(rows):
    out = {}
    for r in rows or []:
        q = num(r.get("quantity"))
        if q is None or not r.get("po"):
            continue
        if str(r.get("doc_type", "")).strip() in ("102", "122", "reversal", "return"):
            q = -abs(q)
        k = line_key(r["po"], r.get("line"))
        out[k] = out.get(k, 0.0) + q
    return out


def _sum_amount(rows):
    out = {}
    for r in rows or []:
        amt = num(r.get("invoice_amount"))
        if amt is not None and r.get("po"):
            k = line_key(r["po"], r.get("line"))
            out[k] = out.get(k, 0.0) + amt
    return out


def _reconcile_assets(d, mapping, rows, po_matches):
    tol = mapping["defaults"]["priceTolerance"]
    findings, used, matched = [], set(), []
    by_num_only = {}
    for i, r in enumerate(rows):
        if r.get("asset_number"):
            by_num_only.setdefault(norm(r["asset_number"]), i)
    by_ref = {r["bom_ref"]: i for i, r in enumerate(rows) if r.get("bom_ref")}
    by_serial = {norm(r["serial"]): i for i, r in enumerate(rows) if r.get("serial")}
    bom_pos = {((c.get("identifiers") or {}).get("erp") or {}).get("purchaseOrder")
               for c in d.doc.get("components", [])} - {None}
    for a in d.assets():
        if a.c["class"] == "hardware" and a.status == "discovered-unmanaged" and a.serial:
            i = by_serial.get(norm(a.serial))
            if i is not None and i not in used:
                used.add(i)
                r = rows[i]
                matched.append((a, r))
                findings.append(finding(SYSTEM, "shadow-known-to-erp", "medium",
                                        f"{a.c.get('name')} is unmanaged in the I-BOM but the ERP carries it as asset "
                                        f"{r.get('asset_number')} (cost center {r.get('cost_center') or 'none'})",
                                        "Adopt it into the I-BOM with that owner and cost center", asset=a,
                                        record=f"asset {r.get('asset_number')}"))
            continue
        if not is_capex_asset(a, mapping) or a.status not in RECEIVED_OR_LATER:
            continue
        erp = a.ids.get("erp") or {}
        i = None
        if erp.get("assetNumber"):
            i = by_num_only.get(norm(erp["assetNumber"]))
        if i is None:
            i = by_ref.get(a.ref)
        if i is None and a.serial:
            i = by_serial.get(norm(a.serial))
        if i is None or i in used:
            if a.status in ACTIVE:
                findings.append(finding(SYSTEM, "not-capitalized", "high",
                                        f"{a.c.get('model') or a.c['name']} is {a.status} but has no fixed asset",
                                        "Create the asset (row in erp-*-fixed-assets.csv) so depreciation starts",
                                        asset=a))
            else:
                findings.append(finding(SYSTEM, "pending-capitalization", "info",
                                        f"{a.c.get('model') or a.c['name']} is {a.status}; no fixed asset yet",
                                        "Capitalize when it goes into service", asset=a))
            continue
        used.add(i)
        r = rows[i]
        rec = f"asset {r.get('asset_number') or '(no number)'}"
        matched.append((a, r))
        if a.serial and r.get("serial") and norm(a.serial) != norm(r["serial"]):
            findings.append(finding(SYSTEM, "field-mismatch", "high", "Serial number on the fixed asset differs",
                                    "Correct the asset master", asset=a, record=rec, field="serial", bom=a.serial,
                                    other=r["serial"]))
        if _is_retired(r) and a.status in ACTIVE:
            findings.append(finding(SYSTEM, "asset-retired-but-in-service", "high",
                                    f"ERP retired this asset but the I-BOM shows it {a.status}",
                                    "Reverse the retirement or decommission the unit", asset=a, record=rec,
                                    field="status", bom=a.status, other=r.get("status")))
        if a.status in RETIRED and not _is_retired(r):
            findings.append(finding(SYSTEM, "retire-asset", "medium", f"Unit is {a.status} but the asset is still "
                                    "active in the ERP", "Post the asset retirement", asset=a, record=rec))
        cc = a.c["financial"].get("costCenter")
        if cc and r.get("cost_center") and norm(cc) != norm(r["cost_center"]):
            findings.append(finding(SYSTEM, "field-mismatch", "medium", "Cost center on the fixed asset differs",
                                    "Transfer the asset or fix the BOM", asset=a, record=rec, field="cost_center",
                                    bom=cc, other=r["cost_center"]))
        expected = a.c["financial"]["unitCost"]["amount"] * a.quantity
        value = num(r.get("acquisition_value"))
        if value is not None and abs(value - expected) > tol * max(expected, 1):
            findings.append(finding(SYSTEM, "field-mismatch", "medium",
                                    f"Acquisition value {value:,.2f} vs I-BOM cost {expected:,.2f}",
                                    "Post the value adjustment (or capitalize freight/labor deliberately)",
                                    asset=a, record=rec, field="acquisition_value", bom=expected, other=value))
    for i, r in enumerate(rows):
        if i in used or _is_retired(r):
            continue
        in_scope = r.get("po") in bom_pos or r.get("bom_ref")
        findings.append(f := finding(SYSTEM, "asset-not-in-bom", "medium" if in_scope else "low",
                                f"Fixed asset {r.get('asset_number')} ({r.get('description')}, serial "
                                f"{r.get('serial') or 'none'}) has no matching I-BOM line",
                                "Find the unit; if it is gone, retire the asset (ghost asset)",
                                record=f"asset {r.get('asset_number')}"))
        if r.get("serial"):
            f["serial"] = r["serial"]
    return findings, matched


def write_back(asset_matches, erp_system=None):
    """Store matched fixed-asset numbers on assets. Returns the number of assets changed."""
    n = 0
    for a, r in asset_matches:
        num_ = r.get("asset_number")
        if r.get("asset_sub") and str(r["asset_sub"]).strip("0"):
            num_ = f"{num_}-{r['asset_sub']}"
        n += a.set_ids("erp", {"assetNumber": num_, "system": erp_system})
    return n

#!/usr/bin/env python3
"""Validate an I-BOM document and print a rollup.

Checks JSON Schema conformance, then the rules JSON Schema cannot express:
unique bom-refs, every reference resolves, extendedCost = unitCost x quantity,
no rack-unit collisions, rack power budget. Exit code 1 on any error.

Usage: python3 ibom_validate.py <file.ibom.json> [--schema path] [--quiet]
Requires: pip install jsonschema
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "ibom.schema.json"


def walk_refs(obj, path=""):
    """Yield (json_path, value) for every field whose value must be a bom-ref."""
    ref_keys = {"manufacturer", "parent", "supplier", "provider", "recycler", "targetComponent",
                "location", "from", "to", "ref", "component"}
    ref_list_keys = {"licensedComponents", "sparesRef", "dependsOn", "affects", "scope"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            if k == "discovered":
                continue  # unmanaged items are by definition not in the BOM
            if k in ref_keys and isinstance(v, str):
                yield p, v
            elif k in ref_list_keys and isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, str):
                        yield f"{p}/{i}", item
            else:
                yield from walk_refs(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_refs(v, f"{path}/{i}")


def check_integrity(doc):
    errors, warnings = [], []
    refs = {}
    for section in ("organizations", "locations", "components"):
        for item in doc.get(section, []):
            r = item["bom-ref"]
            if r in refs:
                errors.append(f"duplicate bom-ref '{r}' in {section} (already in {refs[r]})")
            refs[r] = section

    for path, value in walk_refs({k: doc[k] for k in doc if k not in ("metadata", "$schema")}):
        # 'provider' is a vendor enum except under labor; event from/to are free-text states
        if path.endswith("/provider") and "/labor/" not in path:
            continue
        if "/events/" in path:
            continue
        if value not in refs:
            errors.append(f"{path}: unresolved bom-ref '{value}'")

    baseline_ids = {b["id"] for b in doc.get("baselines", [])}
    for o in doc.get("observations", []):
        if o.get("baseline") and o["baseline"] not in baseline_ids:
            errors.append(f"observation {o['id']}: unknown baseline '{o['baseline']}'")
        if o["kind"] != "unmanaged" and "component" not in o:
            errors.append(f"observation {o['id']}: kind '{o['kind']}' needs a component")

    for c in doc["components"]:
        fin = c.get("financial", {})
        if "unitCost" in fin and "extendedCost" in fin:
            u, e = fin["unitCost"], fin["extendedCost"]
            if u["currency"] != e["currency"]:
                errors.append(f"{c['bom-ref']}: unitCost/extendedCost currency mismatch")
            elif abs(u["amount"] * c["quantity"] - e["amount"]) > 0.01:
                errors.append(f"{c['bom-ref']}: extendedCost {e['amount']} != unitCost {u['amount']} x qty {c['quantity']}")
        if c.get("serialNumber") and c["quantity"] != 1:
            errors.append(f"{c['bom-ref']}: serialNumber set but quantity is {c['quantity']}; use units[]")
        if c.get("units") and len(c["units"]) > c["quantity"]:
            errors.append(f"{c['bom-ref']}: {len(c['units'])} units listed but quantity is {c['quantity']}")
        lic = c.get("licensing", {})
        if lic.get("validFrom") and lic.get("validTo") and lic["validTo"] < lic["validFrom"]:
            errors.append(f"{c['bom-ref']}: license validTo before validFrom")

    # rack-unit collisions and power budget
    locs = {l["bom-ref"]: l for l in doc.get("locations", [])}
    occupancy = defaultdict(dict)
    rack_power = defaultdict(float)
    placements = []
    for c in doc["components"]:
        if c.get("placement"):
            placements.append((c["bom-ref"], c["placement"], c))
        for u in c.get("units", []):
            if u.get("placement"):
                placements.append((f"{c['bom-ref']}#{u['serialNumber']}", u["placement"], None))
    for name, p, comp in placements:
        loc = p.get("location")
        if loc in locs and locs[loc]["type"] == "rack" and p.get("rackUnit"):
            for ru in range(p["rackUnit"], p["rackUnit"] + max(p.get("heightU", 1), 1)):
                key = (ru, p.get("face", "front"))
                if key in occupancy[loc]:
                    errors.append(f"rack {loc} U{ru} {key[1]}: {name} collides with {occupancy[loc][key]}")
                occupancy[loc][key] = name
            if locs[loc].get("rack", {}).get("heightU") and p["rackUnit"] + p.get("heightU", 1) - 1 > locs[loc]["rack"]["heightU"]:
                errors.append(f"{name}: extends beyond rack {loc} height")
        if comp and loc in locs:
            w = comp.get("physical", {}).get("power", {}).get("maxW")
            if w:
                rack_power[loc] += w * comp["quantity"]
    budget = doc["metadata"]["project"].get("budget")
    if budget:
        spent = sum(c.get("financial", {}).get("extendedCost", {}).get("amount", 0) for c in doc["components"]
                    if c.get("financial", {}).get("extendedCost", {}).get("currency") == budget["currency"])
        if spent > budget["amount"]:
            warnings.append(f"project cost {spent:,.0f} exceeds budget {budget['amount']:,.0f} {budget['currency']}")
    for loc, w in rack_power.items():
        budget = locs[loc].get("rack", {}).get("powerBudgetKw")
        if budget and w / 1000 > budget:
            warnings.append(f"rack {loc}: max power {w/1000:.1f} kW exceeds budget {budget} kW")
    return errors, warnings


def rollup(doc):
    by_class = defaultdict(float)
    currency = None
    carbon = 0.0
    risks, eol, health = [], [], []
    for c in doc["components"]:
        fin = c.get("financial", {})
        if "extendedCost" in fin:
            by_class[c["class"]] += fin["extendedCost"]["amount"]
            currency = fin["extendedCost"]["currency"]
        elif "unitCost" in fin:
            by_class[c["class"]] += fin["unitCost"]["amount"] * c["quantity"]
            currency = fin["unitCost"]["currency"]
        ec = c.get("sustainability", {}).get("embodiedCarbon")
        if ec:
            carbon += ec["kgCO2e"] * c["quantity"]
        r = c.get("supply", {}).get("risk")
        if r and r.get("level") in ("high", "critical"):
            risks.append((r["score"], c["bom-ref"], r["level"], r.get("recommendedAction", "")))
        vl = c.get("lifecycle", {}).get("vendorLifecycle", {})
        if vl.get("stage") not in (None, "active", "preview", "unknown"):
            eol.append((c["bom-ref"], vl["stage"], vl.get("successor", "")))
        h = c.get("lifecycle", {}).get("health", {})
        if h.get("status") in ("warning", "critical", "offline"):
            health.append((c["bom-ref"], h["status"]))
    obs = doc.get("observations", [])
    return {
        "cost_by_class": dict(by_class), "currency": currency, "total_cost": sum(by_class.values()),
        "embodied_kgCO2e": carbon, "supply_risks": sorted(risks, reverse=True), "eol": eol, "health": health,
        "drift_open": [o for o in obs if o["kind"] == "drift" and o.get("status") == "open"],
        "unmanaged_open": [o for o in obs if o["kind"] == "unmanaged" and o.get("status") == "open"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    schema = json.loads(Path(a.schema).read_text())
    doc = json.loads(Path(a.file).read_text())

    v = Draft202012Validator(schema, format_checker=FormatChecker())
    schema_errors = sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))
    for e in schema_errors:
        print(f"SCHEMA  /{'/'.join(map(str, e.absolute_path))}: {e.message}")
    if schema_errors:
        print(f"\nFAIL: {len(schema_errors)} schema error(s)")
        sys.exit(1)

    errors, warnings = check_integrity(doc)
    for e in errors:
        print(f"ERROR   {e}")
    for w in warnings:
        print(f"WARN    {w}")
    if errors:
        print(f"\nFAIL: {len(errors)} integrity error(s)")
        sys.exit(1)
    print(f"PASS: {a.file} is a valid I-BOM {doc['specVersion']} "
          f"({len(doc['components'])} components, {len(doc.get('relationships', []))} relationships)")
    if a.quiet:
        return
    r = rollup(doc)
    print(f"\nTotal cost: {r['total_cost']:,.0f} {r['currency']}")
    for k, val in sorted(r["cost_by_class"].items(), key=lambda x: -x[1]):
        print(f"  {k:<15}{val:>14,.0f}")
    print(f"Embodied carbon (Scope 3, where known): {r['embodied_kgCO2e']:,.0f} kgCO2e")
    if r["supply_risks"]:
        print("Supply risks:")
        for score, ref, level, action in r["supply_risks"]:
            print(f"  [{level} {score:.0f}] {ref}: {action}")
    if r["eol"]:
        print("Lifecycle/EOL flags:")
        for ref, stage, succ in r["eol"]:
            print(f"  {ref}: {stage}" + (f" -> {succ}" if succ else ""))
    if r["health"]:
        print("Health: " + ", ".join(f"{ref} {s}" for ref, s in r["health"]))
    print(f"Open drift: {len(r['drift_open'])}   Open shadow/unmanaged: {len(r['unmanaged_open'])}")


if __name__ == "__main__":
    main()

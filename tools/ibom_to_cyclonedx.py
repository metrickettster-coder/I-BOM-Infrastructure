#!/usr/bin/env python3
"""Export an I-BOM document to CycloneDX 1.6 JSON.

Native mappings: hardware -> device, firmware -> firmware, software -> operating-system /
device-driver / platform / application, cloud-resource and virtual -> services, parent -> nested
components, relationships -> dependencies, knownVulnerabilities -> vulnerabilities, ILM phase ->
metadata.lifecycles. I-BOM-only data (lifecycle, supply, sustainability, financial, identifiers,
drift, placement) is carried losslessly as an 'ibom:data' property holding compact JSON, and
non-material lines (license, labor, consumable, service, soft-cost) as 'ibom:line' properties on
the root component, so a CycloneDX consumer sees the security view and an I-BOM consumer can
round-trip.

Usage: python3 ibom_to_cyclonedx.py <in.ibom.json> [-o out.cdx.json]
"""
import argparse
import json
import sys
from pathlib import Path

PHASES = {"plan": "design", "acquire": "pre-build", "prepare": "build", "deploy": "post-build",
          "operate": "operations", "retire": "decommission"}
SW_TYPES = {"os": "operating-system", "hypervisor": "operating-system", "network-os": "operating-system",
            "driver": "device-driver", "orchestrator": "platform", "runtime": "platform"}
EXTREF = {"datasheet": "documentation", "manual": "documentation", "work-instruction": "documentation",
          "drawing": "documentation", "cad": "documentation", "sbom": "bom", "advisory": "advisories",
          "support": "support", "iac-source": "vcs", "certification": "certification-report",
          "compliance-report": "certification-report", "runbook": "documentation"}
VEX_STATE = {"affected": "exploitable", "not-affected": "not_affected", "fixed": "resolved",
             "mitigated": "resolved", "under-investigation": "in_triage"}
IBOM_ONLY = ("lifecycle", "supply", "sustainability", "financial", "identifiers", "drift", "placement",
             "physical", "compute", "network", "labor", "variants", "units", "configuration", "quality",
             "criticality", "effectivity", "procurementType", "extensions")
NON_MATERIAL = {"license", "labor", "consumable", "service", "soft-cost"}


def org_entity(orgs, ref):
    o = orgs.get(ref)
    if not o:
        return None
    ent = {"name": o["name"], "bom-ref": o["bom-ref"]}
    if o.get("url"):
        ent["url"] = [o["url"]]
    return ent


def prop(name, value):
    return {"name": name, "value": value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))}


def cdx_component(c, orgs):
    cls = c["class"]
    if cls == "hardware":
        ctype = "device"
    elif cls == "firmware":
        ctype = "firmware"
    else:
        ctype = SW_TYPES.get(c.get("software", {}).get("layer") or c.get("category"), "application")
    out = {"type": ctype, "bom-ref": c["bom-ref"], "name": c["name"]}
    m = org_entity(orgs, c.get("manufacturer"))
    if m:
        out["manufacturer"] = m
    sources = c.get("supply", {}).get("sources", [])
    pref = next((s for s in sources if s.get("preferred")), sources[0] if sources else None)
    if pref and org_entity(orgs, pref["supplier"]):
        out["supplier"] = org_entity(orgs, pref["supplier"])
    for k in ("version", "description", "purl", "cpe", "hashes"):
        if c.get(k):
            out[k] = c[k]
    if not out.get("version") and c.get("revision"):
        out["version"] = c["revision"]
    if c.get("model") or c.get("mpn"):
        out["group"] = c.get("model", c.get("mpn"))
    lic = c.get("licensing", {})
    if lic.get("spdxExpression"):
        out["licenses"] = [{"expression": lic["spdxExpression"]}]
    refs = [{"type": EXTREF.get(r["type"], "other"), "url": r["url"], **({"comment": r["comment"]} if r.get("comment") else {})}
            for r in c.get("externalReferences", [])]
    for s in c.get("security", {}).get("sbomRefs", []):
        refs.append({"type": "bom", "url": s.get("bomLink") or s["url"], "comment": f"{s['format']} {s.get('specVersion', '')}".strip()})
    if refs:
        out["externalReferences"] = refs
    props = [prop("ibom:class", cls), prop("ibom:quantity", str(c["quantity"]))]
    for k in ("category", "partNumber", "mpn", "serialNumber", "assetTag", "unitOfMeasure"):
        if c.get(k):
            props.append(prop(f"ibom:{k}", str(c[k])))
    sec = {k: v for k, v in c.get("security", {}).items() if k not in ("sbomRefs", "knownVulnerabilities")}
    data = {k: c[k] for k in IBOM_ONLY if k in c}
    if sec:
        data["security"] = sec
    if data:
        props.append(prop("ibom:data", data))
    props.extend(c.get("properties", []))
    out["properties"] = props
    return out


def cdx_service(c, orgs):
    v = c.get("virtual", {})
    out = {"bom-ref": c["bom-ref"], "name": c["name"]}
    if v.get("provider"):
        out["provider"] = {"name": v["provider"]}
    if c.get("description"):
        out["description"] = c["description"]
    if v.get("resourceId"):
        out["endpoints"] = [v["resourceId"]]
    data = {k: c[k] for k in IBOM_ONLY if k in c}
    data["virtual"] = v
    out["properties"] = [prop("ibom:class", c["class"]), prop("ibom:data", data)]
    return out


def convert(doc):
    orgs = {o["bom-ref"]: o for o in doc.get("organizations", [])}
    md = doc["metadata"]
    root_ref = "ibom-root"
    root = {"type": "platform", "bom-ref": root_ref, "name": md["project"]["name"], "version": str(doc["version"]),
            "properties": [prop("ibom:projectId", md["project"]["id"])]}
    if md["project"].get("description"):
        root["description"] = md["project"]["description"]

    by_ref, services, children = {}, [], {}
    for c in doc["components"]:
        if c["class"] in NON_MATERIAL:
            root["properties"].append(prop("ibom:line", c))
            continue
        if c["class"] in ("cloud-resource", "virtual") and c.get("virtual", {}).get("provider") not in (None, "vmware", "proxmox", "kubernetes"):
            services.append(cdx_service(c, orgs))
            continue
        by_ref[c["bom-ref"]] = cdx_component(c, orgs)
        if c.get("parent"):
            children.setdefault(c["parent"], []).append(c["bom-ref"])

    top = []
    for c in doc["components"]:
        ref = c["bom-ref"]
        if ref not in by_ref:
            continue
        if ref in children:
            by_ref[ref]["components"] = [by_ref[k] for k in children[ref]]
        if not c.get("parent") or c["parent"] not in by_ref:
            top.append(by_ref[ref])

    exported = set(by_ref) | {s["bom-ref"] for s in services}
    deps = {}
    for r in doc.get("relationships", []):
        if r["type"] in ("contains", "installed-by", "installed-in", "spare-for", "replaces"):
            continue
        if r["from"] in exported and r["to"] in exported:
            deps.setdefault(r["from"], set()).add(r["to"])
    for d in doc.get("dependencies", []):
        if d["ref"] in exported:
            deps.setdefault(d["ref"], set()).update(x for x in d.get("dependsOn", []) if x in exported)
    deps.setdefault(root_ref, set()).update(c["bom-ref"] for c in top)
    deps[root_ref].update(s["bom-ref"] for s in services)

    vulns = []
    for c in doc["components"]:
        for v in c.get("security", {}).get("knownVulnerabilities", []):
            if c["bom-ref"] not in exported:
                continue
            ent = {"bom-ref": f"vuln-{v['id']}-{c['bom-ref']}", "id": v["id"], "affects": [{"ref": c["bom-ref"]}]}
            rating = {k: val for k, val in (("severity", v.get("severity")), ("score", v.get("cvss"))) if val is not None}
            if rating:
                ent["ratings"] = [rating]
            if v.get("state"):
                ent["analysis"] = {"state": VEX_STATE[v["state"]]}
                if v.get("fixedIn"):
                    ent["analysis"]["detail"] = f"Fixed in {v['fixedIn']}"
            vulns.append(ent)

    meta = {"timestamp": md["timestamp"], "component": root,
            "tools": {"components": [{"type": "application", "name": "ibom_to_cyclonedx", "version": "0.1.0"}]},
            "properties": [prop("ibom:specVersion", doc["specVersion"]), prop("ibom:serialNumber", doc["serialNumber"])]}
    if md.get("authors"):
        meta["authors"] = [{k: a[k] for k in ("name", "email") if k in a} for a in md["authors"]]
    if md.get("lifecyclePhase"):
        meta["lifecycles"] = [{"phase": PHASES[md["lifecyclePhase"]]}]
    if md.get("bomViews"):
        meta["properties"].append(prop("ibom:bomViews", ",".join(md["bomViews"])))

    bom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "serialNumber": doc["serialNumber"], "version": doc["version"],
           "metadata": meta, "components": top}
    if services:
        bom["services"] = services
    bom["dependencies"] = [{"ref": k, "dependsOn": sorted(v)} for k, v in deps.items()]
    if vulns:
        bom["vulnerabilities"] = vulns
    return bom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    bom = convert(json.loads(Path(a.file).read_text()))
    text = json.dumps(bom, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()

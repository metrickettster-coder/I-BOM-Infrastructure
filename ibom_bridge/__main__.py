"""Command line for the I-BOM ERP/CMDB bridge.

  python3 -m ibom_bridge cmdb-export bom.json -o out/                  # ServiceNow IRE payload + CSV per CI class
  python3 -m ibom_bridge erp-export  bom.json -o out/ --profile sap    # PO lines, fixed assets, depreciation, cloud spend
  python3 -m ibom_bridge reconcile   bom.json --cmdb cmdb.json --erp-po po.csv --erp-gr gr.csv \\
                                     --erp-invoices ir.csv --erp-assets assets.csv \\
                                     --report report.md --view view.csv --write-back synced.ibom.json
  python3 -m ibom_bridge cmdb-pull   --instance dev12345 --classes cmdb_ci_server cmdb_ci_ip_switch -o cmdb.json
  python3 -m ibom_bridge cmdb-push   bom.json --instance dev12345 [--send]
  python3 -m ibom_bridge run         bridge.json                          # all of the above from one file

reconcile exits 1 when a finding at or above --fail-on (default: none) is open, so it can gate a pipeline.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import TOOL, erp, servicenow, sync
from .common import SEVERITY_ORDER, Doc, write_csv


def _doc_and_builder(path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ibom_ingest.core import Builder
    b = Builder.load(path)
    return Doc(b.doc), b


def cmd_cmdb_export(a):
    d = Doc.load(a.bom)
    payload, skipped = servicenow.export(d, servicenow.load_mapping(a.mapping), a.output)
    classes = sorted({i["className"] for i in payload["items"]})
    print(f"cmdb-export: {len(payload['items'])} CIs ({', '.join(classes)}), {len(payload['relations'])} relations, "
          f"{len(skipped)} lines not exported as CIs -> {a.output}")
    if a.verbose:
        for ref, why in skipped:
            print(f"  skip {ref}: {why}")


def cmd_erp_export(a):
    d = Doc.load(a.bom)
    counts = erp.export(d, erp.load_mapping(a.mapping), a.output, a.profile, a.as_of)
    print(f"erp-export ({a.profile}): " + ", ".join(f"{v} {k}" for k, v in counts.items()) + f" -> {a.output}")


def cmd_reconcile(a):
    d, b = _doc_and_builder(a.bom)
    cm = servicenow.load_mapping(a.cmdb_mapping)
    em = erp.load_mapping(a.erp_mapping)
    records = servicenow.read_export(a.cmdb, cm) if a.cmdb else None
    rd = lambda paths: erp.read(paths, em) if paths else None  # noqa: E731
    target = a.write_back or (a.bom if a.in_place else None)
    res = sync.run(d, cm, records, em, rd(a.erp_po), rd(a.erp_gr), rd(a.erp_invoices), rd(a.erp_assets),
                   write_back=bool(target), erp_system=a.erp_system, as_of=a.as_of)
    text = sync.render(res)
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(text)
    if a.view:
        write_csv(a.view, res["view"], sync.VIEW_COLUMNS)
    if a.json:
        Path(a.json).write_text(json.dumps({"tool": TOOL, "bom": d.doc.get("serialNumber"),
                                            "summary": res["summary"], "findings": res["findings"]}, indent=2) + "\n")
    if target:
        tools = b.doc["metadata"].setdefault("tools", [])
        if not any(t.get("name") == TOOL["name"] for t in tools):
            tools.append(dict(TOOL))
        b.save(target, validate=not a.no_validate, timestamp=a.at)
    s = res["summary"]
    print(f"reconcile: {s['total']} findings (" + ", ".join(f"{n} {k}" for k, n in s["bySeverity"].items()) + ")"
          + (f"; keys written to {res['written']} assets -> {target}" if target else ""))
    if not a.report:
        print(text)
    if a.fail_on:
        limit = SEVERITY_ORDER[a.fail_on]
        if any(SEVERITY_ORDER[f["severity"]] <= limit for f in res["findings"]):
            sys.exit(1)


def cmd_cmdb_pull(a):
    n = servicenow.pull(a.instance, a.classes, a.output, servicenow.load_mapping(a.mapping), a.query)
    print(f"cmdb-pull: {n} CIs -> {a.output}")


def cmd_cmdb_push(a):
    d = Doc.load(a.bom)
    payload, _ = servicenow.export(d, servicenow.load_mapping(a.mapping))
    if not a.send:
        print(json.dumps(payload, indent=2))
        print(f"dry run: {len(payload['items'])} CIs would be sent to {a.instance}; add --send to send", file=sys.stderr)
        return
    print(json.dumps(servicenow.push(a.instance, payload, a.data_source), indent=2))


def cmd_run(a):
    m = json.loads(Path(a.manifest).read_text())
    base = Path(a.manifest).resolve().parent
    rel = lambda p: str((base / p).resolve()) if p else None  # noqa: E731
    rels = lambda ps: [rel(p) for p in ps] if ps else None  # noqa: E731
    bom, out = rel(m["bom"]), rel(m["outputDir"])
    as_of = m.get("asOf") or date.today().isoformat()
    cmd_cmdb_export(argparse.Namespace(bom=bom, output=out, mapping=rel(m.get("cmdbMapping")), verbose=False))
    for profile in m.get("erpProfiles", ["generic"]):
        cmd_erp_export(argparse.Namespace(bom=bom, output=out, mapping=rel(m.get("erpMapping")), profile=profile,
                                          as_of=as_of))
    r = m.get("reconcile", {})
    cmd_reconcile(argparse.Namespace(
        bom=bom, cmdb=rels(r.get("cmdb")), erp_po=rels(r.get("erpPo")), erp_gr=rels(r.get("erpGr")),
        erp_invoices=rels(r.get("erpInvoices")), erp_assets=rels(r.get("erpAssets")),
        cmdb_mapping=rel(m.get("cmdbMapping")), erp_mapping=rel(m.get("erpMapping")),
        report=f"{out}/reconciliation.md", view=f"{out}/unified-view.csv", json=f"{out}/findings.json",
        write_back=rel(r.get("writeBack")), in_place=False, erp_system=r.get("erpSystem"), as_of=as_of,
        at=m.get("at"), no_validate=a.no_validate, fail_on=None))


def main(argv=None):
    p = argparse.ArgumentParser(prog="ibom_bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("cmdb-export", help="I-BOM -> ServiceNow IRE payload and per-class CSVs")
    s.add_argument("bom")
    s.add_argument("-o", "--output", required=True, help="output directory")
    s.add_argument("--mapping", help="JSON overriding mappings/servicenow.json keys")
    s.add_argument("-v", "--verbose", action="store_true", help="list lines that are not CIs")
    s.set_defaults(fn=cmd_cmdb_export)

    s = sub.add_parser("erp-export", help="I-BOM -> ERP CSVs (PO lines, fixed assets, depreciation, cloud spend)")
    s.add_argument("bom")
    s.add_argument("-o", "--output", required=True, help="output directory")
    s.add_argument("--profile", default="generic", choices=["generic", "sap", "oracle"])
    s.add_argument("--as-of", default=date.today().isoformat(), help="date for accumulated depreciation")
    s.add_argument("--mapping", help="JSON overriding mappings/erp.json keys")
    s.set_defaults(fn=cmd_erp_export)

    s = sub.add_parser("reconcile", help="compare the I-BOM with CMDB and ERP exports")
    s.add_argument("bom")
    s.add_argument("--cmdb", nargs="+", help="CMDB exports: Table API JSON, CSV or XLSX (one or more classes)")
    s.add_argument("--erp-po", nargs="+", help="ERP PO line export(s)")
    s.add_argument("--erp-gr", nargs="+", help="goods receipt export(s)")
    s.add_argument("--erp-invoices", nargs="+", help="invoice line export(s)")
    s.add_argument("--erp-assets", nargs="+", help="fixed asset register export(s)")
    s.add_argument("--cmdb-mapping")
    s.add_argument("--erp-mapping")
    s.add_argument("--report", help="write the Markdown report here")
    s.add_argument("--view", help="write the unified view CSV here")
    s.add_argument("--json", help="write findings JSON here")
    s.add_argument("--write-back", help="write an I-BOM with matched CMDB sys_ids and ERP asset numbers here")
    s.add_argument("--in-place", action="store_true", help="write the keys back into the input I-BOM")
    s.add_argument("--erp-system", help="ERP name to record, e.g. 'SAP S/4HANA'")
    s.add_argument("--as-of", default=date.today().isoformat(), help="date for book values in the unified view")
    s.add_argument("--at", help="timestamp for the written I-BOM (default now)")
    s.add_argument("--no-validate", action="store_true")
    s.add_argument("--fail-on", choices=list(SEVERITY_ORDER), help="exit 1 if a finding at this severity or worse")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("cmdb-pull", help="export CIs from a ServiceNow instance (SN_USER / SN_PASSWORD)")
    s.add_argument("--instance", required=True, help="instance name (dev12345) or https URL")
    s.add_argument("--classes", nargs="+", required=True)
    s.add_argument("--query", help="encoded query, e.g. correlation_idISNOTEMPTY")
    s.add_argument("--mapping")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_cmdb_pull)

    s = sub.add_parser("cmdb-push", help="send the IRE payload to a ServiceNow instance (dry run unless --send)")
    s.add_argument("bom")
    s.add_argument("--instance", required=True)
    s.add_argument("--mapping")
    s.add_argument("--data-source", default="ServiceNow", help="a discovery source registered on the instance")
    s.add_argument("--send", action="store_true")
    s.set_defaults(fn=cmd_cmdb_push)

    s = sub.add_parser("run", help="exports + reconciliation from a bridge manifest")
    s.add_argument("manifest")
    s.add_argument("--no-validate", action="store_true")
    s.set_defaults(fn=cmd_run)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

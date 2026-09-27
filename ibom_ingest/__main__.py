"""Command line for the I-BOM ingestion engine and lifecycle tracker.

  python3 -m ibom_ingest init       bom.json --project-id P1 --name "Pod B02"
  python3 -m ibom_ingest purchasing bom.json quote.csv po.csv invoice.pdf receipt.xlsx
  python3 -m ibom_ingest terraform  bom.json state.json [plan.json]
  python3 -m ibom_ingest ansible    bom.json --inventory hosts.ini --facts facts/
  python3 -m ibom_ingest discover   bom.json aws-ec2.json
  python3 -m ibom_ingest redfish    bom.json snapshots/
  python3 -m ibom_ingest eol        bom.json [--catalog my.json] [--endoflife ubuntu]
  python3 -m ibom_ingest report     bom.json [--json]
  python3 -m ibom_ingest build      manifest.json          # all of the above from one file

Every command except report updates the I-BOM in place (or writes -o), bumps
its version, and validates it against ibom.schema.json before writing.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import ansible, cloud, lifecycle, purchasing, terraform
from .core import Builder, now_iso


def _expand(paths, patterns=("*.json",)):
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            for pat in patterns:
                out += sorted(p.rglob(pat))
        else:
            out.append(p)
    return out


def cmd_init(a):
    b = Builder.new(a.project_id, a.name, a.type, a.author, a.currency, a.budget, a.at)
    b.save(a.bom, validate=not a.no_validate, timestamp=a.at)
    print(f"created {a.bom} ({b.doc['serialNumber']})")


def cmd_purchasing(a, b):
    mapping = purchasing.load_mapping(a.mapping)
    for f in purchasing.sort_documents(_expand(a.files, ("*.csv", "*.xlsx", "*.pdf", "*.txt")), mapping, a.type):
        dtype, number, refs = purchasing.parse(b, f, mapping, a.type, a.vendor, a.doc_number, a.date,
                                               a.currency, a.location)
        print(f"{f.name}: {dtype} {number}, {len(refs)} lines")


def cmd_terraform(a, b):
    for f in _expand(a.files):
        kind, refs, skipped = terraform.parse(b, f, a.source_url, a.at, a.all_resources, a.tool)
        print(f"{f.name}: terraform {kind}, {len(refs)} resources" + (f", {len(skipped)} skipped (networking/IAM)" if skipped else ""))


def cmd_ansible(a, b):
    s = ansible.parse(b, a.inventory, a.facts, a.at)
    print(f"ansible: {len(s['matched'])} matched by serial, {len(s['attached'])} attached to open slots, "
          f"{len(s['shadow'])} unmanaged, {len(s['virtual'])} VMs, {len(s['os'])} OS lines"
          + (f", no facts for {', '.join(s['no_facts'])}" if s["no_facts"] else ""))


def cmd_discover(a, b):
    for f in _expand(a.files):
        res = cloud.read(f, a.format)
        r = cloud.reconcile(b, res, a.at, a.source, adopt=not a.no_adopt)
        print(f"{f.name}: {len(res)} live resources, {len(r['matched'])} matched, "
              f"{len(r['shadow'])} shadow, {len(r['missing'])} declared but missing")


def cmd_redfish(a, b):
    s = lifecycle.ingest_redfish(b, _expand(a.files), a.at)
    print(f"redfish: {s['snapshots']} snapshots, {len(s['updated'])} lines updated, {s['firmwareChanges']} firmware changes, "
          f"{len(s['shadow'])} unmanaged")


def cmd_eol(a, b):
    entries = lifecycle.load_catalog(a.catalog)
    for spec in a.endoflife or []:
        product, _, pattern = spec.partition("=")
        cache = Path(a.cache) / f"{product}.json"
        if a.refresh or not cache.exists():
            cycles = lifecycle.fetch_endoflife(product, a.cache)
        else:
            cycles = json.loads(cache.read_text())
        entries = lifecycle.endoflife_entries(product, cycles, pattern or None) + entries
    today = a.today or a.at[:10]
    changed = lifecycle.apply_eol(b, entries, today, a.at)
    print(f"eol: {len(changed)} components changed lifecycle data")


def cmd_report(a):
    doc = json.loads(Path(a.bom).read_text())
    r = lifecycle.report(doc, a.today or date.today().isoformat(), a.horizon)
    print(json.dumps(r, indent=2) if a.json else lifecycle.render(r))


def cmd_build(a):
    m = json.loads(Path(a.manifest).read_text())
    base = Path(a.manifest).resolve().parent
    rel = lambda p: str((base / p).resolve()) if p else p  # noqa: E731
    out = a.output or rel(m["output"])
    at = m.get("at") or a.at
    p = m["project"]
    b = Builder.new(p["id"], p["name"], p.get("type", "other"), p.get("author"), p.get("currency", "USD"),
                    p.get("budget"), at)
    if m.get("serialNumber"):
        b.doc["serialNumber"] = m["serialNumber"]
    b.doc["version"] = 0
    for step in m["steps"]:
        (name, args), = step.items()
        s_at = args.get("at", at)
        ns = argparse.Namespace(at=s_at, **{k.replace("-", "_"): v for k, v in _defaults(name).items()})
        for k, v in args.items():
            k = k.replace("-", "_")
            if k in ("files",):
                v = [rel(x) for x in v]
            elif k in ("inventory", "facts", "mapping", "catalog", "cache"):
                v = rel(v)
            setattr(ns, k, v)
        print(f"== {name}")
        COMMANDS[name](ns, b)
    b.save(out, validate=not a.no_validate, timestamp=at)
    print(f"wrote {out}: {len(b.doc['components'])} components, version {b.doc['version']}")


def _defaults(name):
    d = {"purchasing": dict(mapping=None, type=None, vendor=None, doc_number=None, date=None, currency="USD", location=None),
         "terraform": dict(source_url=None, all_resources=False, tool="terraform"),
         "ansible": dict(inventory=None, facts=None),
         "discover": dict(format=None, source="cloud-api", no_adopt=False),
         "redfish": dict(),
         "eol": dict(catalog=None, endoflife=None, cache="eol-cache", refresh=False, today=None)}
    return d[name]


COMMANDS = {"purchasing": cmd_purchasing, "terraform": cmd_terraform, "ansible": cmd_ansible,
            "discover": cmd_discover, "redfish": cmd_redfish, "eol": cmd_eol}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ibom_ingest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", default=None, help="timestamp for events/metadata (default: now, UTC)")
    ap.add_argument("--no-validate", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create an empty I-BOM")
    s.add_argument("bom")
    s.add_argument("--project-id", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--type", default="other")
    s.add_argument("--author")
    s.add_argument("--currency", default="USD")
    s.add_argument("--budget", type=float)

    def with_bom(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("bom")
        s.add_argument("-o", "--output", help="write here instead of updating bom in place")
        return s

    s = with_bom("purchasing", "quotes, POs, invoices, receiving lists (csv, xlsx, pdf, txt)")
    s.add_argument("files", nargs="+")
    s.add_argument("--mapping", help="column/regex mapping JSON (default: built-in)")
    s.add_argument("--type", choices=["quote", "po", "invoice", "receipt"])
    s.add_argument("--vendor")
    s.add_argument("--doc-number")
    s.add_argument("--date")
    s.add_argument("--currency", default="USD")
    s.add_argument("--location", help="site or warehouse name for received hardware")

    s = with_bom("terraform", "terraform show -json state or plan output")
    s.add_argument("files", nargs="+")
    s.add_argument("--source-url", help="repo URL/path of the declaring configuration")
    s.add_argument("--all-resources", action="store_true", help="also record networking/IAM resources")
    s.add_argument("--tool", default="terraform", choices=["terraform", "opentofu"])

    s = with_bom("ansible", "Ansible inventory and gathered facts")
    s.add_argument("--inventory")
    s.add_argument("--facts", help="fact cache directory or `ansible -m setup` JSON")

    s = with_bom("discover", "live cloud inventory export (shadow infrastructure)")
    s.add_argument("files", nargs="+")
    s.add_argument("--format", choices=list(cloud.READERS))
    s.add_argument("--source", default="cloud-api")
    s.add_argument("--no-adopt", action="store_true", help="flag shadow resources without adding them as lines")

    s = with_bom("redfish", "Redfish snapshots (health, BIOS/BMC firmware)")
    s.add_argument("files", nargs="+")

    s = with_bom("eol", "apply vendor lifecycle / EOL data")
    s.add_argument("--catalog", help="catalog JSON (default: built-in sample catalog)")
    s.add_argument("--endoflife", action="append", metavar="PRODUCT[=REGEX]",
                   help="use endoflife.date data for a product, e.g. ubuntu or kubernetes=Kubernetes")
    s.add_argument("--cache", default="eol-cache", help="directory for endoflife.date JSON")
    s.add_argument("--refresh", action="store_true", help="re-download endoflife.date data")
    s.add_argument("--today", help="evaluate stages as of this date (default: --at date)")

    s = sub.add_parser("report", help="lifecycle report")
    s.add_argument("bom")
    s.add_argument("--json", action="store_true")
    s.add_argument("--today")
    s.add_argument("--horizon", type=int, default=365, help="days ahead to look for EOL/expiry")

    s = sub.add_parser("build", help="run a manifest of ingestion steps into a fresh I-BOM")
    s.add_argument("manifest")
    s.add_argument("-o", "--output")

    a = ap.parse_args(argv)
    a.at = a.at or now_iso()
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd == "report":
        return cmd_report(a)
    if a.cmd == "build":
        return cmd_build(a)
    b = Builder.load(a.bom)
    COMMANDS[a.cmd](a, b)
    if not b.changed():
        print("no changes")
        return
    b.save(a.output or a.bom, validate=not a.no_validate, timestamp=a.at)
    print(f"wrote {a.output or a.bom}: version {b.doc['version']}, {len(b.doc['components'])} components")


if __name__ == "__main__":
    sys.exit(main())

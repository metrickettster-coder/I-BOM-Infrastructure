"""Command line for drift detection, supply chain risk and the scorecard.

  python3 -m ibom_risk desired   bom.json golden-config.json      # declare desired state + drift policy
  python3 -m ibom_risk baseline  bom.json --name "Go-live" [--approved-by NAME] [--scope REF ...]
  python3 -m ibom_risk drift     bom.json live/ [--no-bom-firmware]  # compare live state, write observations
  python3 -m ibom_risk supply    bom.json [--signals s.json] [--need-by 2026-10-31] [--today D]
  python3 -m ibom_risk scorecard bom.json [--json | --markdown] [--today D]
  python3 -m ibom_risk build     manifest.json                     # run steps (and ingest.* steps) on an I-BOM

Every command except scorecard updates the I-BOM in place (or writes -o),
bumps its version and validates it against ibom.schema.json.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ibom_ingest import __main__ as ingest_cli
from ibom_ingest.__main__ import _expand
from ibom_ingest.core import Builder, now_iso

from . import TOOL, drift, live, scorecard, supply


def cmd_desired(a, b):
    spec = json.loads(Path(a.spec).read_text())
    refs = drift.apply_desired(b, spec, a.at)
    print(f"desired state {spec.get('id', a.spec)}: {len(refs)} lines updated")


def cmd_baseline(a, b):
    bl = drift.freeze_baseline(b, a.at, a.name, a.scope, a.approved_by)
    if bl:
        n = len(b.doc["baselines"][-1]["expected"])
        print(f"baseline {bl}: {n} tracked values frozen")
    else:
        print("baseline unchanged since the latest one")


def cmd_drift(a, b):
    snaps, unmatched = ([] if a.no_bom_firmware else live.from_bom(b)), []
    for f in _expand(a.files):
        s, u = live.read(b, f, a.at, a.format)
        snaps += s
        unmatched += u
        print(f"{f.name}: {len(s)} snapshots" + (f", {len(u)} not matched to a BOM line" if u else ""))
    r = drift.detect(b, snaps, a.at)
    print(f"drift: {len(r['drift'])} open, {len(r['remediated'])} remediated, {len(r['accepted'])} accepted by policy, "
          f"{len(r['blueprint'])} unapproved BOM edits, {len(r['matched'])} lines fully matching"
          + (f", {len(r['unmonitored'])} lines with no live source" if r["unmonitored"] else ""))
    for oid in r["drift"] + r["blueprint"]:
        o = next(x for x in b.doc["observations"] if x["id"] == oid)
        unit = o.get("discovered", {}).get("unit")
        print(f"  [{o['severity']}] {o['component']}{' ' + unit if unit and unit != 'bom' else ''} {o['attribute']}: "
              f"expected {o.get('expected')!r}, live {o.get('actual')!r} ({o['source']})")


def cmd_supply(a, b):
    today = a.today or a.at[:10]
    rows = supply.assess(b, today, a.at, a.need_by, supply.load(a.signals) if a.signals else {},
                         supply.load(a.subtier) if a.subtier else None,
                         supply.load(a.alternates) if a.alternates else None)
    print(f"supply risk as of {today}: {len(rows)} lines")
    for r in rows:
        dp = f", {r['delayProbability']:.0%} chance of missing {r['needBy']}" if r.get("delayProbability") is not None else ""
        print(f"  {r['score']:>3} {r['level']:<8} {r['ref']}{dp}")
        for w in r["why"]:
            print(f"        {w}")
        if r.get("recommendedAction"):
            print(f"        -> {r['recommendedAction']}")
    for s in supply.supplier_rows(b.doc):
        print(f"  supplier {s['name']}: score {s['score']}" + (f", {s['notes']}" if s.get("notes") else ""))


def cmd_scorecard(a):
    doc = json.loads(Path(a.bom).read_text())
    sc = scorecard.build(doc, a.today or date.today().isoformat(), a.horizon)
    print(json.dumps(sc, indent=2) if a.json else scorecard.render(sc, markdown=a.markdown))


def cmd_build(a):
    m = json.loads(Path(a.manifest).read_text())
    base = Path(a.manifest).resolve().parent
    rel = lambda p: str((base / p).resolve()) if p else p  # noqa: E731
    b = Builder.load(rel(m["input"]))
    out = a.output or rel(m["output"])
    at = m.get("at") or a.at
    for step in m["steps"]:
        (name, args), = step.items()
        if name.startswith("ingest."):  # reuse ibom_ingest steps (redfish, discover, eol, ...) mid-pipeline
            cmd, defaults = ingest_cli.COMMANDS[name[7:]], ingest_cli._defaults(name[7:])
        else:
            cmd, defaults = COMMANDS[name], _defaults(name)
        ns = argparse.Namespace(at=args.get("at", at), **{k.replace("-", "_"): v for k, v in defaults.items()})
        for k, v in args.items():
            k = k.replace("-", "_")
            if k == "files":
                v = [rel(x) for x in v]
            elif k in ("spec", "signals", "subtier", "alternates", "inventory", "facts", "mapping", "catalog", "cache"):
                v = rel(v)
            setattr(ns, k, v)
        print(f"== {name}")
        cmd(ns, b)
    save(b, out, a.no_validate, at)
    print(f"wrote {out}: version {b.doc['version']}")


def _defaults(name):
    return {"desired": dict(spec=None), "baseline": dict(name=None, scope=None, approved_by=None),
            "drift": dict(files=[], format=None, no_bom_firmware=False),
            "supply": dict(signals=None, subtier=None, alternates=None, need_by=None, today=None)}[name]


COMMANDS = {"desired": cmd_desired, "baseline": cmd_baseline, "drift": cmd_drift, "supply": cmd_supply}


def save(b, path, no_validate, at):
    tools = b.doc["metadata"].setdefault("tools", [])
    if not any(t.get("name") == TOOL["name"] for t in tools):
        tools.append(dict(TOOL))
    b.save(path, validate=not no_validate, timestamp=at)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ibom_risk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", default=None, help="timestamp for observations/metadata (default: now, UTC)")
    ap.add_argument("--no-validate", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_bom(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("bom")
        s.add_argument("-o", "--output", help="write here instead of updating bom in place")
        return s

    s = with_bom("desired", "apply a golden-configuration file (desired state, drift policy, criticality)")
    s.add_argument("spec")

    s = with_bom("baseline", "freeze the tracked configuration as an approved baseline")
    s.add_argument("--name")
    s.add_argument("--scope", nargs="*", help="bom-refs to cover (default: whole BOM)")
    s.add_argument("--approved-by")

    s = with_bom("drift", "compare live exports with the baseline and write drift/match observations")
    s.add_argument("files", nargs="*")
    s.add_argument("--format", choices=list(live.READERS))
    s.add_argument("--no-bom-firmware", action="store_true",
                   help="ignore firmware ibom_ingest already recorded on the lines")

    s = with_bom("supply", "score supply chain risk and predict delays")
    s.add_argument("--signals", help="market signals JSON")
    s.add_argument("--subtier", help="sub-tier dependency catalog (default: built-in)")
    s.add_argument("--alternates", help="alternates catalog (default: built-in)")
    s.add_argument("--need-by", help="date open lines are needed on site (default: each line's promised date)")
    s.add_argument("--today")

    s = sub.add_parser("scorecard", help="health and drift risk scorecard")
    s.add_argument("bom")
    s.add_argument("--json", action="store_true")
    s.add_argument("--markdown", action="store_true")
    s.add_argument("--today")
    s.add_argument("--horizon", type=int, default=365)

    s = sub.add_parser("build", help="run a manifest of steps on an existing I-BOM")
    s.add_argument("manifest")
    s.add_argument("-o", "--output")

    a = ap.parse_args(argv)
    a.at = a.at or now_iso()
    if a.cmd == "scorecard":
        return cmd_scorecard(a)
    if a.cmd == "build":
        return cmd_build(a)
    b = Builder.load(a.bom)
    COMMANDS[a.cmd](a, b)
    if not b.changed():
        print("no changes")
        return
    save(b, a.output or a.bom, a.no_validate, a.at)
    print(f"wrote {a.output or a.bom}: version {b.doc['version']}")


if __name__ == "__main__":
    sys.exit(main())

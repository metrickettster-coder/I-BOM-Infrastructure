"""Read-only audit: drop in whatever files you have, get an I-BOM and a scorecard.

This is the "asynchronous audit" entry point behind the scorecard web page. It
takes a loose pile of files, works out what each one is from its content (not
its name), replays them through the existing engines in a sensible order, and
returns the I-BOM plus the scorecard:

  existing I-BOM (.ibom.json)          starting point instead of an empty I-BOM
  quotes, POs, invoices, receipts      ibom_ingest purchasing (csv, xlsx, pdf, txt)
  Terraform state / plan JSON          ibom_ingest terraform (declared state)
  Ansible inventory (.ini) + facts     ibom_ingest ansible
  Redfish snapshots                    ibom_ingest redfish (health, firmware)
  cloud inventories (AWS/Azure/GCP)    ibom_ingest discover (shadow and missing)
  golden configuration                 ibom_risk desired (desired state, drift policy)
  live exports (AWS EC2, EKS, refresh-only plan, gNMI/agent facts)
                                       ibom_risk drift
  market signals                       ibom_risk supply
  then vendor EOL from the built-in catalog, supply risk on every purchased line,
  and the health and drift risk scorecard.

Nothing is written to disk and nothing is fetched from the network, so the same
code runs unchanged in a browser (Pyodide) with the files never leaving it.

  python3 -m ibom_risk audit files_or_dirs... [--today D] [--need-by D] [--json | --markdown] [-o bom.json]
"""
import argparse
import contextlib
import io
import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from ibom_ingest import __main__ as ingest_cli
from ibom_ingest import cloud, purchasing
from ibom_ingest.core import Builder

from . import __main__ as risk_cli
from . import live, scorecard

PURCHASING_EXT = (".csv", ".xlsx", ".xlsm", ".pdf", ".txt")

# what each kind means to a person looking at the upload list
LABELS = {
    "ibom": "existing I-BOM (starting point)",
    "purchasing": "purchasing document",
    "terraform-state": "Terraform state (declared)",
    "terraform-plan": "Terraform plan (declared changes)",
    "ansible-inventory": "Ansible inventory",
    "ansible-facts": "Ansible facts",
    "redfish": "Redfish snapshot (health, firmware)",
    "cloud": "cloud inventory (shadow check + live state)",
    "cloud-inventory": "cloud inventory (shadow check)",
    "live": "live state export (drift check)",
    "golden-config": "golden configuration (desired state)",
    "signals": "supply market signals",
    "unknown": "not recognised",
}


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return None


def classify(path):
    """Kind of one file, from its content. Returns (kind, detail)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".ini", ".cfg", ".hosts") or p.name in ("hosts", "inventory"):
        return "ansible-inventory", None
    if ext in PURCHASING_EXT:
        return "purchasing", None
    data = _json(p.read_bytes())
    if data is None:
        return "unknown", "not JSON, CSV, XLSX, PDF or an Ansible inventory"
    if isinstance(data, dict):
        if isinstance(data.get("components"), list) and ("specVersion" in data or "metadata" in data):
            return "ibom", None
        if "ansible_facts" in data or "ansible_hostname" in data:
            return "ansible-facts", None
        if "resource_drift" in data:
            return "live", "terraform-drift"
        if "resource_changes" in data:
            return "terraform-plan", None
        if isinstance(data.get("values"), dict) and "root_module" in data["values"]:
            return "terraform-state", None
        if isinstance(data.get("rules"), list) and all(isinstance(r, dict) and "match" in r for r in data["rules"]):
            return "golden-config", None
        if isinstance(data.get("signals"), list):
            return "signals", None
        if "Reservations" in data:
            return "cloud", "aws-ec2"
    try:
        fmt = live.detect(data)
        return ("redfish" if fmt == "redfish" else "live"), fmt
    except ValueError:
        pass
    fmt = cloud.detect_format(data)
    if fmt != "generic":
        return "cloud-inventory", fmt
    return "unknown", "JSON, but not a format the engines read"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ns(defaults, at, **kw):
    ns = argparse.Namespace(at=at, **{k.replace("-", "_"): v for k, v in defaults.items()})
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _collected_at(path):
    data = _json(Path(path).read_bytes())
    return (data.get("collectedAt") or data.get("_collectedAt")) if isinstance(data, dict) else None


def audit(paths, today=None, at=None, need_by=None, project_name="Uploaded files (audit)"):
    """Classify and replay `paths` (files or directories). Returns
    {"doc", "scorecard", "files": [{name, kind, label, status, note}], "log": str}.

    Files that carry no timestamp of their own (Ansible facts, Terraform state) are
    dated at the earliest snapshot that does, so a dated Redfish or cloud export
    always counts as newer than them rather than the other way round."""
    at = at or _now()
    today = today or at[:10]
    files = []
    for p in map(Path, paths):
        files += sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith(".")) if p.is_dir() else [p]
    rows = []
    for f in files:
        kind, detail = classify(f)
        rows.append({"name": f.name, "path": str(f), "kind": kind, "format": detail, "label": LABELS[kind],
                     "status": "skipped" if kind == "unknown" else "pending", "note": detail if kind == "unknown" else None})
    by = lambda *kinds: [r for r in rows if r["kind"] in kinds]  # noqa: E731
    dated = sorted(t for r in rows if r["kind"] not in ("unknown", "purchasing", "ansible-inventory")
                   for t in [_collected_at(r["path"])] if isinstance(t, str))
    undated_at = dated[0] if dated and dated[0] < at else at

    log = io.StringIO()
    base = by("ibom")
    if base:
        b = Builder(json.loads(Path(base[0]["path"]).read_text()))
        base[0]["status"] = "used"
        for r in base[1:]:
            r.update(status="skipped", note="only one existing I-BOM is used as the starting point")
    else:
        b = Builder.new("AUDIT", project_name, "hybrid", "ibom_risk audit", "USD", None, at)

    def run(name, cmd, ns, targets):
        print(f"== {name}", file=log)
        try:
            with contextlib.redirect_stdout(log):
                cmd(ns, b)
        except (Exception, SystemExit) as e:  # one bad file must not sink the whole audit
            print(f"   failed: {e}", file=log)
            for r in targets:
                r.update(status="error", note=str(e) or type(e).__name__)
            return False
        for r in targets:
            if r["status"] == "pending":
                r["status"] = "used"
        return True

    ing = lambda name: (ingest_cli.COMMANDS[name], ingest_cli._defaults(name))  # noqa: E731
    rk = lambda name: (risk_cli.COMMANDS[name], risk_cli._defaults(name))  # noqa: E731

    # declared state first: purchasing in business order, then IaC state before plans
    docs = by("purchasing")
    if docs:
        mapping = purchasing.load_mapping()
        readable = []
        for r in docs:
            try:
                header, lines = purchasing.read_document(Path(r["path"]), mapping)
            except (Exception, SystemExit) as e:
                r.update(status="error", note=str(e))
                continue
            if lines:
                readable.append(r)
            else:
                r.update(status="skipped", note="no line items found with the built-in column mapping")
        cmd, d = ing("purchasing")
        order = [str(x) for x in purchasing.sort_documents([Path(r["path"]) for r in readable], mapping)]
        for r in sorted(readable, key=lambda r: order.index(r["path"])):
            run(f"purchasing {r['name']}", cmd, _ns(d, at, files=[r["path"]]), [r])
    for kind in ("terraform-state", "terraform-plan"):
        cmd, d = ing("terraform")
        for r in by(kind):
            run(f"terraform {r['name']}", cmd, _ns(d, _collected_at(r["path"]) or undated_at, files=[r["path"]]), [r])
    inv, facts = by("ansible-inventory"), by("ansible-facts")
    if facts:
        # ansible.read_facts names each host after its file in a fact-cache directory
        fdir = Path(tempfile.mkdtemp(prefix="ibom-audit-facts-"))
        for r in facts:
            data = json.loads(Path(r["path"]).read_text())
            host = data.get("ansible_facts", data).get("ansible_hostname") or Path(r["name"]).stem
            (fdir / host).write_text(json.dumps(data))
        cmd, d = ing("ansible")
        run("ansible", cmd, _ns(d, undated_at, inventory=inv[0]["path"] if inv else None, facts=str(fdir)), inv[:1] + facts)
    for r in inv[1 if facts else 0:]:
        r.update(status="skipped", note="an inventory is only read together with Ansible facts")
    if by("redfish"):
        cmd, d = ing("redfish")
        run("redfish", cmd, _ns(d, at, files=[r["path"] for r in by("redfish")]), by("redfish"))
    for r in by("cloud", "cloud-inventory"):
        cmd, d = ing("discover")
        run(f"discover {r['name']}", cmd, _ns(d, at, files=[r["path"]], format=r["format"]), [r])
    cmd, d = ing("eol")
    run("eol (built-in sample catalog)", cmd, _ns(d, at, today=today), [])

    # desired state, then live state against it
    for r in by("golden-config"):
        cmd, d = rk("desired")
        run(f"desired {r['name']}", cmd, _ns(d, at, spec=r["path"]), [r])
    live_rows = by("cloud", "live")
    cmd, d = rk("drift")
    run("drift", cmd, _ns(d, at, files=[r["path"] for r in live_rows]), live_rows)

    sig = by("signals")
    for r in sig[1:]:
        r.update(status="skipped", note="only one market signals file is used")
    cmd, d = rk("supply")
    run("supply", cmd, _ns(d, at, today=today, need_by=need_by, signals=sig[0]["path"] if sig else None), sig[:1])

    doc = b.finalize(at)
    sc = scorecard.build(doc, today)
    for r in rows:
        r.pop("path")
    return {"doc": doc, "scorecard": sc, "files": rows, "log": log.getvalue()}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ibom_risk audit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--today", help="date to score as of (default: today)")
    ap.add_argument("--at", help="timestamp for observations (default: now, UTC)")
    ap.add_argument("--need-by", help="date open purchases are needed on site (default: each line's promised date)")
    ap.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("-o", "--output", help="also write the I-BOM built from the files here")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the engine log")
    a = ap.parse_args(argv)
    r = audit(a.files, a.today or (a.at[:10] if a.at else date.today().isoformat()), a.at, a.need_by)
    for f in r["files"]:
        print(f"{f['status']:<8} {f['name']}: {f['label']}" + (f" ({f['note']})" if f["note"] else ""))
    if a.verbose:
        print(r["log"])
    print()
    print(json.dumps(r["scorecard"], indent=2) if a.json else scorecard.render(r["scorecard"], markdown=a.markdown))
    if a.output:
        Path(a.output).write_text(json.dumps(r["doc"], indent=2, ensure_ascii=False) + "\n")

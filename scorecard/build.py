"""Regenerate the files the scorecard page loads from the repo's Python packages and samples.

  python3 scorecard/build.py           # write py/ibom.zip and demo/
  python3 scorecard/build.py --check   # exit 1 if they are out of date (run by tests/test_audit.py)

The page is static: it downloads Pyodide (CPython compiled to WebAssembly) from
a CDN, unpacks py/ibom.zip (ibom_ingest + ibom_risk, byte-for-byte the
package sources) and the vendored pure-Python wheels in py/wheels/, and runs
`ibom_risk.audit` on the files dropped on the page. Nothing is uploaded anywhere.
Re-run this script whenever ibom_ingest, ibom_risk or the demo samples change.
"""
import io
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PACKAGES = ["ibom_ingest", "ibom_risk"]
ZIP = HERE / "py" / "ibom.zip"
DEMO = HERE / "demo"

# The one-click demo: the synthetic pod B02 files, as someone would drop them.
DEMO_FILES = [
    "samples/pod-b02/purchasing/quote-Q-24117.csv",
    "samples/pod-b02/purchasing/po-4500018823.csv",
    "samples/pod-b02/purchasing/invoice-INV-88213.pdf",
    "samples/pod-b02/purchasing/receipt-GR-5000044120.xlsx",
    "samples/pod-b02/iac/terraform-state.json",
    "samples/pod-b02/ansible/inventory.ini",
    "samples/pod-b02/ansible/facts/inf-b02-01",
    "samples/pod-b02/ansible/facts/inf-b02-02",
    "samples/pod-b02/ansible/facts/mgmt-b02-01",
    "samples/pod-b02/redfish/2026-09-27/7XK2Q34.json",
    "samples/pod-b02/discovery/aws-ec2-describe-instances.json",
    "samples/pod-b02-ops/desired/golden-config.json",
    "samples/pod-b02-ops/live/2026-10-05/network-facts.json",
    "samples/pod-b02-ops/live/2026-10-05/eks-cluster.json",
    "samples/pod-b02-ops/live/2026-10-05/eks-nodegroup.json",
    "samples/pod-b02-ops/live/2026-10-12/aws-ec2.json",
    "samples/pod-b02-ops/live/2026-10-12/terraform-refresh.json",
    "samples/pod-b02-ops/live/2026-10-12/redfish/7XK2Q35.json",
    "samples/pod-b02-ops/supply/market-signals.json",
]
DEMO_OPTIONS = {"today": "2026-10-12", "needBy": "2026-10-31", "at": "2026-10-12T12:00:00Z",
                "project": "Inference pod B02 (sample data)"}


def bundle():
    """Deterministic zip of the package sources (fixed timestamps, sorted, no bytecode)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for pkg in PACKAGES:
            for f in sorted((ROOT / pkg).rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix in (".py", ".json", ".md"):
                    info = zipfile.ZipInfo(f.relative_to(ROOT).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    z.writestr(info, f.read_bytes())
    return buf.getvalue()


def demo_files():
    out = {}
    for rel in DEMO_FILES:
        out[DEMO / rel.removeprefix("samples/")] = (ROOT / rel).read_bytes()
    index = {**DEMO_OPTIONS, "files": [rel.removeprefix("samples/") for rel in DEMO_FILES]}
    out[DEMO / "demo.json"] = (json.dumps(index, indent=2) + "\n").encode()
    return out


def main(argv):
    check = "--check" in argv
    wanted = {ZIP: bundle(), **demo_files()}
    present = {f for f in DEMO.rglob("*") if f.is_file()} if DEMO.exists() else set()
    stale = [p for p, data in wanted.items() if not p.exists() or p.read_bytes() != data]
    extra = sorted(present - set(wanted))
    if check:
        for p in stale + extra:
            print(f"out of date: {p.relative_to(ROOT)}")
        if stale or extra:
            print("run: python3 scorecard/build.py")
            return 1
        print("scorecard bundle and demo files are up to date")
        return 0
    for p in stale:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(wanted[p])
        print(f"wrote {p.relative_to(ROOT)}")
    for p in extra:
        p.unlink()
        print(f"removed {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

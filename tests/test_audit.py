"""Tests for the read-only audit (any pile of files -> I-BOM + scorecard) behind the scorecard web page.
Run: python3 tests/test_audit.py

Needs jsonschema, pypdf and openpyxl (the demo includes a PDF invoice and an XLSX receipt).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ibom_ingest.core import validate_doc  # noqa: E402
from ibom_risk import audit  # noqa: E402

DEMO = ROOT / "scorecard" / "demo"
S = ROOT / "samples"


class Classify(unittest.TestCase):
    def test_kinds_from_content(self):
        cases = {
            "pod-b02/purchasing/po-4500018823.csv": "purchasing",
            "pod-b02/purchasing/invoice-INV-88213.pdf": "purchasing",
            "pod-b02/iac/terraform-state.json": "terraform-state",
            "pod-b02/iac/terraform-plan.json": "terraform-plan",
            "pod-b02/ansible/inventory.ini": "ansible-inventory",
            "pod-b02/ansible/facts/inf-b02-01": "ansible-facts",
            "pod-b02/redfish/2026-09-27/7XK2Q34.json": "redfish",
            "pod-b02/discovery/aws-ec2-describe-instances.json": "cloud",
            "pod-b02-ops/desired/golden-config.json": "golden-config",
            "pod-b02-ops/supply/market-signals.json": "signals",
            "pod-b02-ops/live/2026-10-12/terraform-refresh.json": "live",
            "pod-b02-ops/live/2026-10-05/eks-nodegroup.json": "live",
            "pod-b02-ops/live/2026-10-05/network-facts.json": "live",
        }
        for rel, kind in cases.items():
            self.assertEqual(audit.classify(S / rel)[0], kind, rel)
        self.assertEqual(audit.classify(ROOT / "examples" / "pod-b02.ibom.json")[0], "ibom")
        self.assertEqual(audit.classify(ROOT / "README.md")[0], "unknown")
        self.assertEqual(audit.classify(ROOT / "ibom_risk" / "catalogs" / "subtier.json")[0], "unknown")


class DemoAudit(unittest.TestCase):
    """The page's one-click demo: the pod B02 sample files dropped in as one pile."""

    @classmethod
    def setUpClass(cls):
        d = json.loads((DEMO / "demo.json").read_text())
        cls.r = audit.audit([DEMO / f for f in d["files"]], d["today"], d["at"], d["needBy"], d["project"])
        cls.sc = cls.r["scorecard"]

    def test_every_file_used(self):
        self.assertEqual([f for f in self.r["files"] if f["status"] != "used"], [])

    def test_output_is_a_valid_ibom(self):
        self.assertEqual(validate_doc(self.r["doc"]), [])

    def test_finds_what_the_samples_plant(self):
        drift = {(d["component"], d["attribute"], d["unit"]) for d in self.sc["drift"]}
        self.assertIn(("hw-dcs-7050cx3-32s", "/configuration/eosVersion", None), drift)
        self.assertIn(("hw-r760xa-cto-l40s", "/configuration/firmware/bmc", "7XK2Q35"), drift)
        self.assertIn(("tf-aws_s3_bucket.models", "/configuration/versioning/enabled", None), drift)
        self.assertAlmostEqual(self.sc["shadowMonthlyCost"], 965.79)
        top = {s["ref"]: s for s in self.sc["supply"]}
        self.assertEqual(top["hw-qsfp-100g-sr4"]["level"], "critical")
        self.assertEqual(top["hw-qsfp-100g-sr4"]["needBy"], "2026-10-31")
        self.assertEqual(self.sc["overall"]["grade"], "F")

    def test_undated_facts_do_not_override_newer_redfish(self):
        # Ansible facts carry no timestamp; they must not look newer than the dated Redfish snapshots
        fw = next(c for c in self.r["doc"]["components"] if c["bom-ref"] == "hw-r760xa-cto-l40s")
        seen = fw["extensions"]["ibom.dev/lifecycle"]["observedFirmware"]
        self.assertEqual(seen["7XK2Q35"]["source"], "redfish")
        self.assertNotIn(None, [d["actual"] for d in self.sc["drift"]])


class Robustness(unittest.TestCase):
    def test_bad_and_unknown_files_do_not_sink_the_audit(self):
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "broken.xlsx"
            bad.write_bytes(b"not really a spreadsheet")
            note = Path(t) / "notes.md"
            note.write_text("hello")
            r = audit.audit([bad, note, S / "pod-b02" / "purchasing" / "po-4500018823.csv"], "2026-10-12",
                            "2026-10-12T12:00:00Z")
            status = {f["name"]: f["status"] for f in r["files"]}
            self.assertEqual(status, {"broken.xlsx": "error", "notes.md": "skipped", "po-4500018823.csv": "used"})
            self.assertTrue(r["scorecard"]["supply"])

    def test_existing_ibom_is_the_starting_point(self):
        r = audit.audit([ROOT / "examples" / "pod-b02.ibom.json"], "2026-10-12", "2026-10-12T12:00:00Z")
        self.assertEqual(r["doc"]["serialNumber"], json.loads((ROOT / "examples" / "pod-b02.ibom.json").read_text())["serialNumber"])
        self.assertTrue(r["scorecard"]["supply"])

    def test_cli(self):
        r = subprocess.run([sys.executable, "-m", "ibom_risk", "audit", str(S / "pod-b02" / "purchasing" / "po-4500018823.csv"),
                            str(S / "pod-b02" / "iac"), "--today", "2026-10-12", "--json"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"overall"', r.stdout)


class SiteBundle(unittest.TestCase):
    def test_bundle_and_demo_are_up_to_date(self):
        r = subprocess.run([sys.executable, str(ROOT / "scorecard" / "build.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

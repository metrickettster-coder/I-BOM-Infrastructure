"""Tests for drift detection, supply chain risk and the scorecard. Run: python3 tests/test_risk.py

Needs jsonschema (and pypdf/openpyxl only because the pod B02 input example is rebuilt by test_ingest).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ibom_ingest.core import Builder, validate_doc  # noqa: E402
from ibom_risk import drift, live, scorecard, supply  # noqa: E402

OPS = ROOT / "samples" / "pod-b02-ops"
AT = "2026-10-01T00:00:00Z"


def obs(doc, oid):
    return next((o for o in doc.get("observations", []) if o["id"] == oid), None)


class OpsPipeline(unittest.TestCase):
    """Pod B02 two weeks after go-live: golden config, baseline, two drift runs, supply risk."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = Path(cls.tmp) / "assessed.ibom.json"
        r = subprocess.run([sys.executable, "-m", "ibom_risk", "build", str(OPS / "manifest.json"), "-o", str(cls.out)],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stdout + r.stderr)
        cls.doc = json.loads(cls.out.read_text())
        cls.c = {c["bom-ref"]: c for c in cls.doc["components"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_valid(self):
        self.assertEqual(validate_doc(self.doc), [])

    def test_committed_example_is_current(self):
        committed = json.loads((ROOT / "examples" / "pod-b02-assessed.ibom.json").read_text())
        self.assertEqual(committed, self.doc, "rerun: python3 -m ibom_risk build samples/pod-b02-ops/manifest.json")

    def test_baseline(self):
        (bl,) = self.doc["baselines"]
        self.assertEqual(bl["digest"]["alg"], "SHA-256")
        self.assertIn({"component": "hw-dcs-7050cx3-32s", "attribute": "/configuration/mtu", "value": 9214},
                      bl["expected"])

    def test_firmware_drift_per_unit(self):
        bios = obs(self.doc, "OBS-DRIFT-hw-r760xa-cto-l40s-configuration-firmware-bios-7xk2q35")
        bmc = obs(self.doc, "OBS-DRIFT-hw-r760xa-cto-l40s-configuration-firmware-bmc-7xk2q35")
        self.assertEqual((bios["status"], bios["actual"]), ("remediated", "1.9.1"))
        self.assertEqual((bmc["status"], bmc["actual"], bmc["severity"]), ("open", "7.10.50.00", "high"))
        self.assertEqual(bmc["discovered"], {"unit": "7XK2Q35"})
        self.assertIsNotNone(obs(self.doc, "OBS-MATCH-hw-r760xa-cto-l40s-7xk2q34"))

    def test_network_drift_severity_follows_criticality(self):
        o = obs(self.doc, "OBS-DRIFT-hw-dcs-7050cx3-32s-configuration-eosversion")
        self.assertEqual((o["severity"], o["source"], o["actual"]), ("critical", "gnmi", "4.31.1F"))
        self.assertIn("gnmi", self.c["hw-dcs-7050cx3-32s"]["drift"]["discoverySources"])

    def test_cloud_drift_opens_then_remediates(self):
        o = obs(self.doc, "OBS-DRIFT-tf-module.inference.aws_instance.gateway-0-configuration-instancetype")
        self.assertEqual((o["status"], o["expected"], o["actual"]), ("remediated", "g5.xlarge", "g5.xlarge"))
        tag = obs(self.doc, "OBS-DRIFT-tf-module.inference.aws_instance.gateway-1-configuration-tags-project")
        self.assertEqual(tag["status"], "remediated")

    def test_terraform_refresh_drift(self):
        o = obs(self.doc, "OBS-DRIFT-tf-aws_s3_bucket.models-configuration-versioning-enabled")
        self.assertEqual((o["status"], o["expected"], o["actual"]), ("open", True, False))
        self.assertIsNone(obs(self.doc, "OBS-DRIFT-tf-aws_s3_bucket.models-configuration-versioning"))

    def test_tolerance(self):
        self.assertIsNotNone(obs(self.doc, "OBS-MATCH-tf-module.inference.aws_eks_node_group.gpu"))

    def test_accept_and_update_bom(self):
        c = self.c["tf-module.inference.aws_eks_cluster.this"]
        self.assertEqual(c["configuration"]["version"], "1.31")
        o = obs(self.doc, "OBS-DRIFT-tf-module.inference.aws_eks_cluster.this-configuration-version")
        self.assertEqual(o["status"], "accepted")
        (ch,) = [x for x in self.doc["changes"] if c["bom-ref"] in x["affects"]]
        self.assertEqual((ch["type"], ch["status"]), ("deviation", "implemented"))
        self.assertTrue(any(e.get("changeRef") == ch["id"] for e in c["lifecycle"]["events"]))

    def test_unapproved_bom_edit(self):
        o = obs(self.doc, "OBS-DRIFT-hw-dcs-7050cx3-32s-configuration-mtu-bom")
        self.assertEqual((o["kind"], o["expected"], o["actual"], o["source"]), ("drift", 9214, 9000, "manual-audit"))

    def test_supply_risk(self):
        optic = self.c["hw-qsfp-100g-sr4"]["supply"]
        self.assertEqual(optic["risk"]["level"], "critical")
        self.assertEqual(optic["risk"]["needBy"], "2026-10-31")
        self.assertGreater(optic["risk"]["delayProbability"], 0.5)
        self.assertIn("logistics", optic["risk"]["drivers"])
        self.assertIn("demand-spike", optic["risk"]["drivers"])
        self.assertIn("alternate", optic["risk"]["recommendedAction"])
        self.assertEqual(optic["sources"][0]["leadTimeDays"]["predicted"], 71)
        self.assertFalse(self.c["hw-qsfp-100g-sr4"]["variants"]["alternates"][0]["qualified"])
        sw = self.c["hw-dcs-7050cx3-32s"]
        self.assertTrue(any(d["singleSource"] and "ASIC" in d["name"] for d in sw["supply"]["subTierDependencies"]))
        self.assertIn("eol-approaching", sw["supply"]["risk"]["drivers"])
        self.assertNotIn("delayProbability", sw["supply"]["risk"])  # already received
        self.assertNotIn("risk", self.c["tf-aws_s3_bucket.models"].get("supply", {}))

    def test_supplier_rating(self):
        nw = next(o for o in self.doc["organizations"] if o["name"].startswith("Northwind"))
        self.assertEqual(nw["risk"]["onTimeDeliveryRate"], 0)
        self.assertEqual(nw["risk"]["financialHealth"], "watch")

    def test_scorecard(self):
        sc = scorecard.build(self.doc, "2026-10-12")
        self.assertEqual([a["area"] for a in sc["areas"]],
                         ["Configuration drift", "Shadow infrastructure", "Supply chain", "Lifecycle and health"])
        self.assertTrue(0 <= sc["overall"]["score"] <= 100)
        self.assertEqual(sc["actions"][0]["priority"], "critical")
        self.assertTrue(any("llm-eval-notebook" in a["what"] for a in sc["actions"]))
        self.assertIn("Top actions", scorecard.render(sc, markdown=True))

    def test_committed_scorecard_is_current(self):
        md = scorecard.render(scorecard.build(self.doc, "2026-10-12"), markdown=True) + "\n"
        self.assertEqual((ROOT / "examples" / "pod-b02-scorecard.md").read_text(), md)


def server_bom():
    b = Builder.new("T-1", "drift unit test", timestamp=AT)
    b.upsert({"component": {"class": "hardware", "category": "server", "name": "srv", "mpn": "SRV-1", "quantity": 2,
                            "units": [{"serialNumber": "S1"}, {"serialNumber": "S2"}],
                            "configuration": {"firmware": {"bios": "2.0"}, "powerCapW": 1000}},
              "keys": ["part:SRV-1"], "refHint": "hw-srv", "status": "in-service", "at": AT,
              "rank": {"quantity": 1}})
    b.upsert({"component": {"class": "cloud-resource", "category": "vm", "name": "vm",
                            "virtual": {"provider": "aws", "resourceType": "aws_instance",
                                        "iac": {"tool": "terraform", "address": "aws_instance.vm"}},
                            "configuration": {"instanceType": "m7i.large"}},
              "keys": ["iac:terraform:aws_instance.vm"], "refHint": "vm", "status": "deployed", "at": AT})
    return b


def fw(unit, bios, at=AT, **extra):
    return {"ref": "hw-srv", "unit": unit, "source": "redfish", "at": at, "state": {"firmware": {"bios": bios}, **extra}}


class Drift(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(live.norm({"scaling_config": [{"desired_size": 2}]}), {"scalingConfig": {"desiredSize": 2}})

    def test_baseline_is_idempotent(self):
        b = server_bom()
        self.assertEqual(drift.freeze_baseline(b, AT), "BL-001")
        self.assertIsNone(drift.freeze_baseline(b, "2026-10-02T00:00:00Z"))

    def test_drift_match_and_rerun(self):
        b = server_bom()
        drift.freeze_baseline(b, AT)
        r = drift.detect(b, [fw("S1", "2.0"), fw("S2", "1.9")], AT)
        self.assertEqual(r["drift"], ["OBS-DRIFT-hw-srv-configuration-firmware-bios-s2"])
        self.assertEqual(r["matched"], ["hw-srv"])
        b = Builder(json.loads(json.dumps(b.finalize(AT))))  # as saved and reloaded by the CLI
        drift.detect(b, [fw("S1", "2.0"), fw("S2", "1.9")], AT)
        self.assertFalse(b.changed())

    def test_triaged_finding_stays_triaged_and_regression_reopens(self):
        b = server_bom()
        drift.detect(b, [fw("S2", "1.9")], AT)
        oid = "OBS-DRIFT-hw-srv-configuration-firmware-bios-s2"
        obs(b.doc, oid)["status"] = "false-positive"
        drift.detect(b, [fw("S2", "1.9")], AT)
        self.assertEqual(obs(b.doc, oid)["status"], "false-positive")
        drift.detect(b, [fw("S2", "2.0", "2026-10-02T00:00:00Z")], AT)
        self.assertEqual(obs(b.doc, oid)["status"], "remediated")
        drift.detect(b, [fw("S2", "1.8", "2026-10-03T00:00:00Z")], AT)
        self.assertEqual(obs(b.doc, oid)["status"], "open")

    def test_tolerance_and_ignore(self):
        b = server_bom()
        c = b.component("hw-srv")
        c["drift"] = {"tolerance": {"/configuration/powerCapW": 50}}
        r = drift.detect(b, [fw("S1", "2.0", powerCapW=1040)], AT)
        self.assertEqual(r["drift"], [])
        c["drift"]["onDrift"] = "ignore"
        r = drift.detect(b, [fw("S1", "9.9")], AT)
        self.assertEqual(r["drift"], [])

    def test_approved_change_moves_expectation(self):
        b = server_bom()
        drift.freeze_baseline(b, AT)
        b.component("hw-srv")["configuration"]["firmware"]["bios"] = "2.1"
        r = drift.detect(b, [fw("S1", "2.1")], "2026-10-05T00:00:00Z")
        self.assertEqual(len(r["blueprint"]), 1)
        self.assertEqual(len(r["drift"]), 1)  # live still compared with the approved baseline value 2.0
        b.doc["changes"] = [{"id": "CHG-1", "type": "rfc", "status": "approved", "affects": ["hw-srv"],
                             "effectiveDate": "2026-10-04"}]
        r = drift.detect(b, [fw("S1", "2.1")], "2026-10-05T00:00:00Z")
        self.assertEqual((r["blueprint"], r["drift"]), ([], []))

    def test_terraform_refresh_untracked_attribute(self):
        b = server_bom()
        plan = {"format_version": "1.2", "resource_drift": [
            {"address": "aws_instance.vm", "type": "aws_instance",
             "change": {"actions": ["update"], "before": {"instance_type": "m7i.large", "monitoring": True},
                        "after": {"instance_type": "m7i.large", "monitoring": False}}}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "refresh.json"
            p.write_text(json.dumps(plan))
            snaps, _ = live.read(b, p, AT)
        r = drift.detect(b, snaps, AT)
        o = obs(b.doc, r["drift"][0])
        self.assertEqual((o["attribute"], o["expected"], o["actual"]), ("/configuration/monitoring", True, False))
        self.assertIn("terraform apply", drift.remediation(b.doc, o))

    def test_desired_state(self):
        b = server_bom()
        spec = {"id": "G1", "rules": [{"match": {"mpn": "SRV"}, "configuration": {"firmware": {"bmc": "5"}},
                                       "criticality": "critical"}]}
        self.assertEqual(drift.apply_desired(b, spec, AT), ["hw-srv"])
        c = b.component("hw-srv")
        self.assertEqual(c["configuration"]["firmware"], {"bios": "2.0", "bmc": "5"})
        self.assertIn("/configuration/firmware/bmc", drift.tracked(c))
        self.assertEqual(validate_doc(b.finalize(AT)), [])


def optic_bom(status="ordered", promised="2026-10-20", suppliers=1, qualified=False):
    b = Builder.new("T-2", "supply unit test", timestamp=AT)
    sources = [{"supplier": b.org(f"Dist {i}", "distributor"), "tier": 1, "leadTimeDays": {"quoted": 30}}
               for i in range(suppliers)]
    c = {"class": "hardware", "category": "optic", "name": "optic", "mpn": "OPT-1", "quantity": 4,
         "supply": {"sources": sources},
         "lifecycle": {"dates": {"ordered": "2026-09-15", "promised": promised}}}
    if qualified:
        c["variants"] = {"alternates": [{"mpn": "OPT-2", "qualified": True}]}
    b.upsert({"component": c, "keys": ["part:OPT-1"], "refHint": "hw-opt", "status": status, "at": AT, "force": True})
    return b


class Supply(unittest.TestCase):
    def test_second_source_or_qualified_alternate(self):
        for kw in ({"suppliers": 2}, {"qualified": True}):
            b = optic_bom(**kw)
            supply.assess(b, "2026-10-01", AT, subtier={}, alternates={})
            self.assertFalse(b.component("hw-opt")["supply"]["singleSource"], kw)
        b = optic_bom()
        supply.assess(b, "2026-10-01", AT, subtier={}, alternates={})
        self.assertTrue(b.component("hw-opt")["supply"]["singleSource"])

    def test_delay_probability_falls_with_more_slack(self):
        ps = []
        for need_by in ("2026-10-10", "2026-10-20", "2026-11-30"):
            b = optic_bom()
            supply.assess(b, "2026-10-01", AT, need_by=need_by, subtier={}, alternates={})
            ps.append(b.component("hw-opt")["supply"]["risk"]["delayProbability"])
        self.assertGreater(ps[0], ps[1])
        self.assertGreater(ps[1], ps[2])

    def test_market_signal_and_late_line(self):
        b = optic_bom(promised="2026-09-20")
        sig = {"signals": [{"match": {"category": "optic"}, "leadTimeFactor": 2.0, "drivers": ["allocation"]}]}
        rows = supply.assess(b, "2026-10-01", AT, signals=sig, subtier={}, alternates={})
        r = b.component("hw-opt")["supply"]["risk"]
        self.assertEqual(rows[0]["predictedLeadDays"], 60)
        self.assertIn("allocation", r["drivers"])
        self.assertIn("logistics", r["drivers"])
        self.assertIn("lead-time-increasing", r["drivers"])
        self.assertEqual(validate_doc(b.finalize(AT)), [])

    def test_received_line_has_no_delay_probability(self):
        b = optic_bom(status="received")
        supply.assess(b, "2026-10-01", AT, need_by="2026-10-10", subtier={}, alternates={})
        self.assertNotIn("delayProbability", b.component("hw-opt")["supply"]["risk"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

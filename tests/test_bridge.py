"""Tests for the ERP/CMDB bridge. Run: python3 tests/test_bridge.py

Needs jsonschema (the write-back step validates the I-BOM it writes).
"""
import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ibom_bridge import erp, servicenow, sync  # noqa: E402
from ibom_bridge.common import Doc, read_table  # noqa: E402
from ibom_ingest.core import validate_doc  # noqa: E402

SAMPLES = ROOT / "samples" / "pod-b02"
BOM = json.loads((ROOT / "examples" / "pod-b02.ibom.json").read_text())
CM = servicenow.load_mapping()
EM = erp.load_mapping()


def doc():
    return Doc(copy.deepcopy(BOM))


def kinds(findings):
    return {(f["system"], f["kind"], f.get("bomRef", ""), f.get("serial", "")) for f in findings}


def cmdb_records():
    return servicenow.read_export([SAMPLES / "cmdb" / "servicenow-cmdb-export.json"], CM)


def erp_rows(name):
    return erp.read([SAMPLES / "erp" / name], EM)


class ServiceNowExport(unittest.TestCase):
    def test_ci_per_serialized_unit_and_cloud_resource(self):
        payload, skipped = servicenow.export(doc(), CM)
        corr = {i["values"]["correlation_id"]: i for i in payload["items"]}
        self.assertIn("hw-r760xa-cto-l40s#7XK2Q34", corr)
        self.assertIn("hw-r760xa-cto-l40s#7XK2Q35", corr)
        self.assertEqual(corr["hw-r760xa-cto-l40s#7XK2Q34"]["className"], "cmdb_ci_server")
        self.assertEqual(corr["hw-r760xa-cto-l40s#7XK2Q34"]["values"]["name"], "inf-b02-01")
        self.assertEqual(corr["hw-dcs-7050cx3-32s"]["values"]["install_status"], 6)  # received -> In Stock
        self.assertEqual(corr["tf-module.inference.aws_instance.router"]["className"], "cmdb_ci_vm_instance")
        skipped = dict(skipped)
        self.assertIn("hw-qsfp-100g-sr4", skipped)       # optics are not CIs
        self.assertIn("lab-nw-lab-rack", skipped)        # labor is not a CI
        self.assertIn("tf-module.batch.aws_instance.embed-0", skipped)  # only planned

    def test_csv_per_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            servicenow.export(doc(), CM, tmp)
            rows = read_table(Path(tmp) / "servicenow-cmdb_ci_server.csv")
            self.assertEqual({r["serial_number"] for r in rows}, {"7XK2Q34", "7XK2Q35", "4HJ8K21"})
            self.assertTrue((Path(tmp) / "servicenow-ire.json").exists())

    def test_relations_between_cis(self):
        d = doc()
        d.doc["relationships"] = [{"from": "tf-module.inference.aws_instance.router", "type": "depends-on",
                                   "to": "tf-module.inference.aws_eks_cluster.this"}]
        payload, _ = servicenow.export(Doc(d.doc), CM)
        self.assertEqual(len(payload["relations"]), 1)
        self.assertEqual(payload["relations"][0]["type"], "Depends on::Used by")


class ServiceNowReconcile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings, cls.matches = servicenow.reconcile(doc(), cmdb_records(), CM)
        cls.k = kinds(cls.findings)

    def test_planted_cmdb_issues_found(self):
        expect = {("cmdb", "duplicate-ci", "hw-r760xa-cto-l40s", "7XK2Q35"),
                  ("cmdb", "status-mismatch", "tf-module.inference.aws_instance.gateway-1", ""),
                  ("cmdb", "status-mismatch", "hw-dcs-7050cx3-32s", "JPE26210F1A"),
                  ("cmdb", "field-mismatch", "hw-dcs-7050cx3-32s", "JPE26210F1A"),
                  ("cmdb", "missing-in-cmdb", "tf-module.inference.aws_instance.router", ""),
                  ("cmdb", "missing-in-cmdb", "hw-pdu-sw-0u-17k", "PDU24A0193"),
                  ("cmdb", "missing-in-cmdb", "shadow-i-0fee1dead0c0ffee1", ""),
                  ("cmdb", "shadow-known-to-cmdb", "shadow-mgmt-b02-01", "4HJ8K21"),
                  ("cmdb", "weak-match", "tf-module.inference.aws_eks_cluster.this", ""),
                  ("cmdb", "orphan-ci", "", "9QW3L11")}
        self.assertEqual(expect - self.k, set())

    def test_retired_ci_is_high(self):
        f = next(f for f in self.findings if f["kind"] == "status-mismatch" and "gateway-1" in f["bomRef"])
        self.assertEqual(f["severity"], "high")

    def test_retired_orphans_ignored(self):
        self.assertFalse(any("2MN4P08" == f.get("serial") for f in self.findings))

    def test_matching_keys(self):
        # ARN in the I-BOM matches a bare instance id in the CMDB; model names differ but match.
        self.assertIn("tf-module.inference.aws_instance.gateway-0", self.matches)
        self.assertFalse(any(f.get("field") == "model_id" for f in self.findings))
        # correlation_id beats serial: the unit's CI is the one carrying the I-BOM id.
        self.assertEqual(self.matches["hw-r760xa-cto-l40s#7XK2Q34"][1]["sys_id"], "5f1d0c2a1b7e4a10a0000000000b0201")

    def test_display_value_records(self):
        rec = [{"sys_id": {"value": "abc", "display_value": "abc"}, "sys_class_name": "cmdb_ci_ip_switch",
                "serial_number": {"value": "JPE26210F1A", "display_value": "JPE26210F1A"},
                "install_status": {"value": "6", "display_value": "In Stock"}}]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.json"
            p.write_text(json.dumps({"result": rec}))
            records = servicenow.read_export([p], CM)
        self.assertEqual(records[0]["install_status"], 6)
        f, m = servicenow.reconcile(doc(), records, CM)
        self.assertIn("hw-dcs-7050cx3-32s", m)
        self.assertFalse(any(x["kind"] == "status-mismatch" and x["bomRef"] == "hw-dcs-7050cx3-32s" for x in f))

    def test_csv_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cmdb_ci_ip_switch.csv"
            p.write_text("Name,Serial number,Status\nleaf-b02-01,JPE26210F1A,Retired\n")
            records = servicenow.read_export([p], CM)
        self.assertEqual(records[0]["install_status"], 7)
        self.assertEqual(records[0]["sys_class_name"], "cmdb_ci_ip_switch")


class Depreciation(unittest.TestCase):
    def test_straight_line(self):
        acc, nbv, sched = erp.depreciation(66900, 0, 60, "straight-line", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((acc, nbv), (1115.0, 65785.0))
        self.assertEqual(sched[0], (2026, 4460.0, 62440.0))
        self.assertAlmostEqual(sum(y[1] for y in sched), 66900, places=2)

    def test_declining_balance_never_below_salvage(self):
        acc, nbv, sched = erp.depreciation(10000, 1000, 36, "double-declining-balance", date(2026, 1, 1),
                                           date(2040, 1, 1))
        self.assertAlmostEqual(nbv, 1000, places=2)
        self.assertGreater(sched[0][1], sched[-1][1])

    def test_not_started(self):
        acc, nbv, _ = erp.depreciation(5000, 0, 60, "straight-line", date(2027, 1, 1), date(2026, 12, 31))
        self.assertEqual((acc, nbv), (0, 5000))


class ErpExport(unittest.TestCase):
    def test_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = erp.export(doc(), EM, tmp, "sap", "2026-09-30")
            self.assertEqual(counts["po-lines"], 10)
            assets = read_table(Path(tmp) / "erp-sap-fixed-assets.csv")
            self.assertEqual(assets[0]["AFASL"], "LINA")
            serials = {r["SERNR"] for r in assets}
            # servers and PDUs (received, capex, above threshold); cables are expensed, optics not received yet
            self.assertEqual(serials, {"7XK2Q34", "7XK2Q35", "JPE26210F1A", "PDU24A0193", "PDU24A0194"})
            erp.export(doc(), EM, tmp, "oracle", "2026-09-30")
            self.assertIn("FIXED_ASSETS_COST", read_table(Path(tmp) / "erp-oracle-fixed-assets.csv")[0])

    def test_recurring_includes_shadow(self):
        rows = erp.recurring_rows(doc())
        self.assertIn("shadow-i-0fee1dead0c0ffee1", {r["bom_ref"] for r in rows})

    def test_export_reads_back_clean(self):
        """Round trip: the I-BOM's own ERP export reconciles with no PO or asset findings."""
        d = doc()
        with tempfile.TemporaryDirectory() as tmp:
            erp.export(d, EM, tmp, "sap", "2026-09-30")
            po = erp.read([Path(tmp) / "erp-sap-po-lines.csv"], EM)
            fa = erp.read([Path(tmp) / "erp-sap-fixed-assets.csv"], EM)
            for i, r in enumerate(fa):
                r["asset_number"] = str(200000 + i)
        f, _ = erp.reconcile(d, EM, po=po, assets=fa)
        self.assertEqual([x for x in f if x["kind"] != "unallocated-spend"], [])


class ErpReconcile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings, cls.matches = erp.reconcile(doc(), EM, erp_rows("po-lines-me2m.csv"),
                                                  erp_rows("goods-receipts-mb51.csv"), erp_rows("invoice-lines.csv"),
                                                  erp_rows("asset-register-ar01.csv"))
        cls.k = kinds(cls.findings)

    def test_planted_erp_issues_found(self):
        expect = {("erp", "quantity-mismatch", "hw-qsfp-100g-sr4", ""),
                  ("erp", "invoiced-not-received", "hw-qsfp-100g-sr4", ""),
                  ("erp", "invoiced-not-received", "hw-pdu-sw-0u-17k", ""),
                  ("erp", "receipt-not-posted", "hw-pdu-sw-0u-17k", ""),
                  ("erp", "field-mismatch", "hw-pdu-sw-0u-17k", ""),
                  ("erp", "invoice-price-variance", "hw-dcs-7050cx3-32s", ""),
                  ("erp", "price-mismatch", "soft-nw-frt", ""),
                  ("erp", "procured-not-in-bom", "", ""),
                  ("erp", "not-capitalized", "hw-r760xa-cto-l40s", "7XK2Q35"),
                  ("erp", "field-mismatch", "hw-dcs-7050cx3-32s", "JPE26210F1A"),
                  ("erp", "shadow-known-to-erp", "shadow-mgmt-b02-01", "4HJ8K21"),
                  ("erp", "asset-not-in-bom", "", "9QW3L11"),
                  ("erp", "unallocated-spend", "shadow-i-0fee1dead0c0ffee1", "")}
        self.assertEqual(expect - self.k, set())

    def test_deleted_line_and_backorder_are_quiet(self):
        self.assertFalse(any("line 120" in f.get("record", "") for f in self.findings))  # deleted (L)
        # optics: 6 of 10 received and the I-BOM says backordered, so no receipt finding
        self.assertFalse(any(f["kind"] in ("receipt-not-posted", "bom-behind-erp") and f.get("bomRef") ==
                             "hw-qsfp-100g-sr4" for f in self.findings))

    def test_retired_asset_ignored(self):
        self.assertFalse(any(f.get("serial") == "2MN4P08" for f in self.findings))

    def test_sap_line_numbers(self):
        self.assertIn(("4500018823", "10"), self.matches["po"])  # 00010 in the export

    def test_bom_behind_erp(self):
        d = doc()
        d.comps["hw-dcs-7050cx3-32s"]["lifecycle"]["status"] = "in-transit"
        f, _ = erp.reconcile(d, EM, erp_rows("po-lines-me2m.csv"), erp_rows("goods-receipts-mb51.csv"))
        self.assertIn(("erp", "bom-behind-erp", "hw-dcs-7050cx3-32s", ""), kinds(f))


class Sync(unittest.TestCase):
    def test_write_back_validates_and_matches_next_time(self):
        d = doc()
        res = sync.run(d, CM, cmdb_records(), EM, erp_rows("po-lines-me2m.csv"), None, None,
                       erp_rows("asset-register-ar01.csv"), write_back=True, erp_system="SAP (sample)",
                       as_of="2026-09-30")
        self.assertGreater(res["written"], 10)
        self.assertEqual(validate_doc(d.doc), [])
        unit = d.comps["hw-r760xa-cto-l40s"]["units"][0]
        self.assertEqual(unit["identifiers"]["cmdb"]["sysId"], "5f1d0c2a1b7e4a10a0000000000b0201")
        self.assertEqual(unit["identifiers"]["erp"]["assetNumber"], "100045")
        self.assertEqual(d.comps["hw-pdu-sw-0u-17k"]["units"][1]["identifiers"]["erp"]["assetNumber"], "100048-1")
        # A renamed CI is still found by the sys_id written back.
        recs = cmdb_records()
        recs[0].update(name="renamed", correlation_id="", serial_number="")
        _, m = servicenow.reconcile(Doc(d.doc), recs, CM)
        self.assertEqual(m["hw-r760xa-cto-l40s#7XK2Q34"][1]["name"], "renamed")

    def test_cross_system_orphan(self):
        res = sync.run(doc(), CM, cmdb_records(), EM, assets=erp_rows("asset-register-ar01.csv"))
        both = [f for f in res["findings"] if f.get("serial") == "9QW3L11"]
        self.assertEqual({f["kind"] for f in both}, {"orphan-ci", "asset-not-in-bom"})
        self.assertTrue(all(f["severity"] == "high" for f in both))

    def test_view_has_row_per_asset_plus_outside_rows(self):
        res = sync.run(doc(), CM, cmdb_records(), EM, erp_rows("po-lines-me2m.csv"), as_of="2026-09-30")
        rows = res["view"]
        s34 = next(r for r in rows if r["serial"] == "7XK2Q34")
        self.assertEqual((s34["cmdb_install_status"], s34["book_value"]), ("Installed", 65785.0))
        self.assertTrue(any(r["class"] == "cmdb only" for r in rows))
        self.assertTrue(any(r["class"] == "erp only" for r in rows))


class Cli(unittest.TestCase):
    def test_run_manifest_and_fail_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = json.loads((SAMPLES / "bridge.json").read_text())
            m["bom"] = str(ROOT / "examples" / "pod-b02.ibom.json")
            m["outputDir"] = tmp
            for k, v in m["reconcile"].items():
                if isinstance(v, list):
                    m["reconcile"][k] = [str(SAMPLES / p) for p in v]
            m["reconcile"]["writeBack"] = str(Path(tmp) / "synced.json")
            (Path(tmp) / "bridge.json").write_text(json.dumps(m))
            r = subprocess.run([sys.executable, "-m", "ibom_bridge", "run", str(Path(tmp) / "bridge.json")],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            for f in ("servicenow-ire.json", "erp-sap-fixed-assets.csv", "erp-oracle-po-lines.csv",
                      "reconciliation.md", "unified-view.csv", "findings.json", "synced.json"):
                self.assertTrue((Path(tmp) / f).exists(), f)
            with open(Path(tmp) / "unified-view.csv") as fh:
                self.assertGreater(len(list(csv.DictReader(fh))), 20)
            r = subprocess.run([sys.executable, "-m", "ibom_bridge", "reconcile", str(ROOT / "examples" /
                                "pod-b02.ibom.json"), "--cmdb", str(SAMPLES / "cmdb" / "servicenow-cmdb-export.json"),
                                "--report", str(Path(tmp) / "r.md"), "--fail-on", "high"],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)

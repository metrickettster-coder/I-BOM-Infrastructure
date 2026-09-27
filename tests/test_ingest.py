"""Tests for the ingestion engine and lifecycle tracker. Run: python3 tests/test_ingest.py

Needs jsonschema; the PDF and XLSX sample steps also need pypdf and openpyxl.
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

from ibom_ingest import ansible, cloud, lifecycle, purchasing, terraform  # noqa: E402
from ibom_ingest.core import Builder, validate_doc  # noqa: E402

SAMPLES = ROOT / "samples" / "pod-b02"
AT = "2026-09-27T12:00:00Z"


def new():
    return Builder.new("T-1", "test project", timestamp=AT)


def comp(b, ref):
    return b.component(ref)


def write(tmp, name, text):
    p = Path(tmp) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if isinstance(text, str) else json.dumps(text))
    return p


class SamplePipeline(unittest.TestCase):
    """The full pod B02 sample: purchasing -> IaC -> config mgmt -> telemetry -> discovery -> EOL."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = Path(cls.tmp) / "pod.ibom.json"
        r = subprocess.run([sys.executable, "-m", "ibom_ingest", "build", str(SAMPLES / "manifest.json"),
                            "-o", str(cls.out)], cwd=ROOT, capture_output=True, text=True)
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
        committed = json.loads((ROOT / "examples" / "pod-b02.ibom.json").read_text())
        self.assertEqual(committed, self.doc, "rerun: python3 -m ibom_ingest build samples/pod-b02/manifest.json")

    def test_quote_po_invoice_receipt_merge_into_one_line(self):
        srv = self.c["hw-r760xa-cto-l40s"]
        self.assertEqual([s["kind"] for s in srv["extensions"]["ibom.dev/ingest"]["sources"]][:4],
                         ["quote", "po", "invoice", "receipt"])
        self.assertEqual(srv["quantity"], 2)
        self.assertEqual(srv["financial"]["unitCost"]["amount"], 66900)  # PO/invoice beat the quote
        self.assertEqual(srv["financial"]["costType"], "actual")
        self.assertEqual(srv["identifiers"]["erp"]["invoice"], "INV-88213")
        self.assertEqual({u["serialNumber"] for u in srv["units"]}, {"7XK2Q34", "7XK2Q35"})

    def test_status_history(self):
        events = [e["to"] for e in self.c["hw-r760xa-cto-l40s"]["lifecycle"]["events"] if e["type"] == "status-change"]
        self.assertEqual(events, ["quoted", "ordered", "in-transit", "received", "deployed", "in-service"])

    def test_po_quantity_wins_and_backorder(self):
        optic = self.c["hw-qsfp-100g-sr4"]
        self.assertEqual(optic["quantity"], 10)  # quote said 8, PO 10, invoice 8
        self.assertEqual(optic["lifecycle"]["status"], "backordered")

    def test_invoice_sku_matches_line_without_mpn(self):
        frt = self.c["soft-nw-frt"]
        self.assertNotIn("mpn", frt)
        self.assertEqual(frt["financial"]["unitCost"]["amount"], 912.4)

    def test_firmware_and_health(self):
        srv = self.c["hw-r760xa-cto-l40s"]
        fw = [e for e in srv["lifecycle"]["events"] if e["type"] == "firmware-update"]
        self.assertEqual({(e["from"], e["to"]) for e in fw},
                         {("BIOS 1.8.2", "BIOS 1.9.1"), ("BMC 7.10.50.00", "BMC 7.10.70.00")})
        self.assertEqual(srv["lifecycle"]["health"]["status"], "warning")

    def test_shadow_and_missing(self):
        shadow = {c["bom-ref"] for c in self.doc["components"] if c["lifecycle"].get("status") == "discovered-unmanaged"}
        self.assertEqual(shadow, {"shadow-i-0fee1dead0c0ffee1", "shadow-mgmt-b02-01"})
        obs = {o["id"]: o for o in self.doc["observations"]}
        self.assertEqual(obs["OBS-MISSING-tf-module.inference.aws_instance.router"]["status"], "open")
        # the EKS node-group member is not shadow
        self.assertFalse(any("0b2c3d4e5f6a70011" in r for r in self.c))

    def test_plan(self):
        self.assertEqual(self.c["tf-module.batch.aws_instance.embed-0"]["lifecycle"]["status"], "planned")
        eks = self.c["tf-module.inference.aws_eks_cluster.this"]
        self.assertTrue(any(e["type"] == "config-change" for e in eks["lifecycle"]["events"]))

    def test_eol(self):
        self.assertEqual(self.c["sw-ubuntu-22.04"]["lifecycle"]["vendorLifecycle"]["endOfSupport"], "2027-06-01")
        r = lifecycle.report(self.doc, "2026-09-27")
        self.assertEqual({x["ref"] for x in r["eol"]},
                         {"hw-r760xa-cto-l40s", "sw-ubuntu-22.04", "hw-dcs-7050cx3-32s"})
        self.assertEqual([x["ref"] for x in r["late"]], ["hw-qsfp-100g-sr4"])

    def test_rerun_is_idempotent(self):
        b = Builder.load(self.out)
        for f in ("quote-Q-24117.csv", "po-4500018823.csv"):
            purchasing.parse(b, SAMPLES / "purchasing" / f)
        terraform.parse(b, SAMPLES / "iac" / "terraform-state.json", observed_at="2026-08-20T15:00:00Z",
                        source_url="git::https://example.com/infra/pod-b02.git//terraform")
        self.assertEqual(b.finalize(AT)["components"], self.doc["components"])


class Purchasing(unittest.TestCase):
    def test_custom_mapping_without_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            csvp = write(tmp, "order.csv", "Beleg,Pos,Herstellernummer,Bezeichnung,Menge,Preis\n"
                                           "PO-9,1,ABC-123,Rack server,3,1000.00\n")
            m = purchasing.load_mapping()
            m["columns"]["docNumber"].append("beleg")
            m["columns"]["line"].append("pos")
            m["columns"]["mpn"].append("herstellernummer")
            m["columns"]["description"].append("bezeichnung")
            m["columns"]["quantity"].append("menge")
            m["columns"]["unitPrice"].append("preis")
            b = new()
            purchasing.parse(b, csvp, m, doc_type="po", vendor="Acme", doc_date="2026-01-05")
            c = b.doc["components"][0]
            self.assertEqual((c["mpn"], c["quantity"], c["category"]), ("ABC-123", 3, "server"))
            self.assertEqual(c["financial"]["unitCost"]["amount"], 1000)
            self.assertEqual(c["lifecycle"]["status"], "ordered")
            self.assertEqual(validate_doc(b.finalize()), [])

    def test_unknown_format_asks_for_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "stuff.csv", "Part Number,Description,Qty\nX1,thing,1\n")
            with self.assertRaises(ValueError):
                purchasing.parse(new(), p)

    def test_serial_on_single_unit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "receipt.csv", "Receipt No,Receipt Date,Mfr Part Number,Description,Qty Received,Serial Number\n"
                                          "GR1,2026-02-01,SW-1,Core switch,1,SN001\n")
            b = new()
            purchasing.parse(b, p)
            c = b.doc["components"][0]
            self.assertEqual(c["serialNumber"], "SN001")
            self.assertEqual(c["lifecycle"]["status"], "received")

    def test_labor_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "quote.csv", "Quote,Description,Qty,UOM,Unit Price\nQ1,Cabling labor,10,Hours,100\n")
            b = new()
            purchasing.parse(b, p, vendor="Installer Co", doc_date="2026-01-01")
            c = b.doc["components"][0]
            self.assertEqual((c["class"], c["unitOfMeasure"], c["labor"]["hours"], c["labor"]["activity"]),
                             ("labor", "HR", 10, "cabling"))
            self.assertEqual(validate_doc(b.finalize()), [])


class Terraform(unittest.TestCase):
    def test_plan_then_state_is_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = write(tmp, "plan.json", {"resource_changes": [{"address": "aws_instance.a", "type": "aws_instance",
                         "change": {"actions": ["create"], "after": {"instance_type": "p5.48xlarge"}}}]})
            state = write(tmp, "state.json", {"values": {"root_module": {"resources": [{"address": "aws_instance.a",
                          "mode": "managed", "type": "aws_instance", "values": {"id": "i-1", "instance_type": "p5.48xlarge",
                          "arn": "arn:aws:ec2:us-west-2:1:instance/i-1", "availability_zone": "us-west-2b"}}]}}})
            b = new()
            terraform.parse(b, plan, observed_at="2026-01-01T00:00:00Z")
            terraform.parse(b, state, observed_at="2026-01-02T00:00:00Z")
            self.assertEqual(len(b.doc["components"]), 1)
            c = b.doc["components"][0]
            self.assertEqual(c["lifecycle"]["status"], "deployed")
            self.assertEqual(c["compute"]["acceleratorCount"], 8)
            self.assertEqual(c["virtual"]["region"], "us-west-2")
            self.assertEqual(validate_doc(b.finalize()), [])

    def test_shadow_is_adopted_when_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = new()
            cloud.reconcile(b, cloud.read_aws_ec2({"Reservations": [{"OwnerId": "1", "Instances": [
                {"InstanceId": "i-9", "InstanceType": "g5.xlarge", "State": {"Name": "running"},
                 "Placement": {"AvailabilityZone": "us-east-1a"}}]}]}), "2026-01-01T00:00:00Z")
            ref = b.doc["components"][0]["bom-ref"]
            self.assertEqual(comp(b, ref)["lifecycle"]["status"], "discovered-unmanaged")
            state = write(tmp, "state.json", {"values": {"root_module": {"resources": [{"address": "aws_instance.x",
                          "mode": "managed", "type": "aws_instance", "values": {"id": "i-9", "instance_type": "g5.xlarge",
                          "arn": "arn:aws:ec2:us-east-1:1:instance/i-9"}}]}}})
            terraform.parse(b, state, observed_at="2026-01-02T00:00:00Z")
            self.assertEqual(len(b.doc["components"]), 1)
            self.assertEqual(comp(b, ref)["lifecycle"]["status"], "deployed")
            self.assertEqual(b.doc["observations"][0]["status"], "accepted")


class Discovery(unittest.TestCase):
    def test_azure_and_gcp_readers(self):
        az = cloud.read_azure_graph({"data": [{"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
              "name": "vm1", "type": "microsoft.compute/virtualmachines", "location": "eastus",
              "properties": {"hardwareProfile": {"vmSize": "Standard_NC24ads_A100_v4"}}}]})
        gcp = cloud.read_gcp_asset([{"name": "//compute.googleapis.com/projects/p/zones/us-central1-a/instances/g1",
               "assetType": "compute.googleapis.com/Instance", "resource": {"data": {"id": "42", "name": "g1",
               "zone": "projects/p/zones/us-central1-a", "machineType": "zones/us-central1-a/machineTypes/a2-highgpu-1g",
               "status": "RUNNING", "selfLink": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/instances/g1"}}}])
        b = new()
        r = cloud.reconcile(b, az + gcp, AT)
        self.assertEqual(len(r["shadow"]), 2)
        accel = {c["virtual"]["provider"]: c["compute"]["acceleratorModel"] for c in b.doc["components"]}
        self.assertEqual(accel, {"azure": "NVIDIA A100", "gcp": "NVIDIA A100"})
        self.assertEqual(validate_doc(b.finalize()), [])

    def test_terminated_is_decommissioned(self):
        b = new()
        b.upsert({"component": {"class": "cloud-resource", "name": "x", "virtual": {"provider": "aws", "resourceId": "i-5"}},
                  "keys": ["cloud:i-5"], "status": "deployed", "at": "2026-01-01T00:00:00Z"})
        cloud.reconcile(b, [{"provider": "aws", "resourceType": "aws_instance", "category": "vm",
                             "resourceId": "i-5", "state": "terminated"}], AT)
        self.assertEqual(b.doc["components"][0]["lifecycle"]["status"], "decommissioned")


class Ansible(unittest.TestCase):
    def test_yaml_inventory_vm_and_open_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = write(tmp, "hosts.yml", "all:\n  children:\n    gpu:\n      hosts:\n        n1:\n        vm1:\n")
            write(tmp, "facts/n1", {"ansible_facts": {"ansible_product_serial": "NEW-SN", "ansible_system_vendor": "Dell Inc.",
                                                      "ansible_product_name": "PowerEdge R760xa", "ansible_bios_version": "2.0"}})
            write(tmp, "facts/vm1", {"ansible_virtualization_role": "guest", "ansible_virtualization_type": "VMware",
                                     "ansible_product_uuid": "4211-abcd", "ansible_processor_vcpus": 8,
                                     "ansible_memtotal_mb": 32768, "ansible_distribution": "Ubuntu",
                                     "ansible_distribution_version": "24.04"})
            b = new()
            b.upsert({"component": {"class": "hardware", "name": "GPU server", "model": "PowerEdge R760xa", "quantity": 2},
                      "keys": ["part:R760XA"], "status": "received", "at": "2026-01-01T00:00:00Z"})
            s = ansible.parse(b, inv, Path(tmp) / "facts", "2026-02-01T00:00:00Z")
            self.assertEqual(len(s["attached"]), 1)  # serial filled an open unit slot, not shadow
            self.assertEqual(s["shadow"], [])
            srv = b.find(["part:R760XA"])
            self.assertEqual(srv["units"][0]["serialNumber"], "NEW-SN")
            self.assertEqual(srv["lifecycle"]["status"], "deployed")
            vm = b.find(["cloud:4211-abcd"])
            self.assertEqual((vm["class"], vm["virtual"]["provider"], vm["virtual"]["memoryGiB"]), ("virtual", "vmware", 32.0))
            self.assertEqual(validate_doc(b.finalize()), [])


class Lifecycle(unittest.TestCase):
    def _server(self, b):
        return comp(b, b.upsert({"component": {"class": "hardware", "name": "srv", "serialNumber": "S1"},
                                  "keys": ["serial:S1"], "status": "deployed", "at": "2026-01-01T00:00:00Z"}))

    def test_critical_then_recovery(self):
        b = new()
        c = self._server(b)
        lifecycle.observe_health(b, c, "S1", {"status": "critical", "source": "redfish"}, "2026-02-01T00:00:00Z")
        self.assertEqual(c["lifecycle"]["status"], "degraded")
        lifecycle.observe_health(b, c, "S1", {"status": "healthy", "source": "redfish"}, "2026-02-02T00:00:00Z")
        self.assertEqual(c["lifecycle"]["status"], "in-service")
        self.assertIn("alert", [e["type"] for e in c["lifecycle"]["events"]])

    def test_old_firmware_snapshot_ignored(self):
        b = new()
        c = self._server(b)
        lifecycle.observe_firmware(b, c, "S1", {"bios": "2.0"}, "2026-03-01T00:00:00Z", "redfish")
        lifecycle.observe_firmware(b, c, "S1", {"bios": "1.0"}, "2026-02-01T00:00:00Z", "redfish")
        self.assertEqual(c["extensions"]["ibom.dev/lifecycle"]["observedFirmware"]["S1"]["bios"], "2.0")
        self.assertFalse(any(e["type"] == "firmware-update" for e in c["lifecycle"]["events"]))

    def test_stage_moves_with_time(self):
        b = new()
        c = comp(b, b.upsert({"component": {"class": "hardware", "name": "Old box", "model": "BX-1"}, "keys": ["part:BX-1"]}))
        entries = [{"match": "BX-1", "vendorLifecycle": {"stage": "active", "endOfSale": "2026-06-30",
                                                          "endOfSupport": "2028-06-30"}}]
        lifecycle.apply_eol(b, entries, "2026-01-01", "2026-01-01T00:00:00Z")
        self.assertEqual(c["lifecycle"]["vendorLifecycle"]["stage"], "active")
        lifecycle.apply_eol(b, entries, "2026-07-01", "2026-07-01T00:00:00Z")
        self.assertEqual(c["lifecycle"]["vendorLifecycle"]["stage"], "end-of-sale")
        notice = [e for e in c["lifecycle"]["events"] if e["type"] == "eol-notice"]
        self.assertEqual((notice[0]["from"], notice[0]["to"]), ("active", "end-of-sale"))

    def test_endoflife_date_format(self):
        cycles = [{"cycle": "1.30", "releaseDate": "2024-04-17", "eol": "2025-06-28"},
                  {"cycle": "1.3", "releaseDate": "2016-07-01", "eol": True}]
        entries = lifecycle.endoflife_entries("kubernetes", cycles, "(?:kubernetes|eks)")
        b = new()
        c = comp(b, b.upsert({"component": {"class": "software", "name": "Kubernetes", "version": "1.30"},
                              "keys": ["sw:k8s"]}))
        lifecycle.apply_eol(b, entries, "2026-01-01", "2026-01-01T00:00:00Z")
        self.assertEqual(c["lifecycle"]["vendorLifecycle"]["stage"], "end-of-life")
        self.assertEqual(c["lifecycle"]["vendorLifecycle"]["endOfSupport"], "2025-06-28")
        self.assertEqual(validate_doc(b.finalize()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Sample: inference pod B02 (synthetic data)

**Everything in this folder is sample data made up for testing.** Vendor names such as Dell, Arista and NVIDIA appear so the documents look realistic, but the part numbers, prices, serial numbers, dates, IP addresses, account ids and lifecycle dates are illustrative. They are not quotes, invoices or vendor notices. `Northwind IT Supply (sample)` is fictional, and `111122223333` is AWS's documentation example account.

The story, in the order `manifest.json` replays it:

| Date | Source | File | What happens |
|---|---|---|---|
| 2026-06-02 | Reseller quote | `purchasing/quote-Q-24117.csv` | 10 lines: 2 GPU servers, a switch, optics, cables, PDUs, support, AI Enterprise licenses, labor, freight, mounting kit |
| 2026-06-10 | SAP-style PO export (ME2N columns) | `purchasing/po-4500018823.csv` | Negotiated prices, 10 optics instead of 8, promised dates, cost center and GL |
| 2026-08-07 | Invoice PDF | `purchasing/invoice-INV-88213.pdf` | Actual prices (freight 912.40, not 850), service tags for the servers and the switch serial |
| 2026-08-12 | Receiving spreadsheet | `purchasing/receipt-GR-5000044120.xlsx` | 8 of 10 optics received, 2 on backorder, PDU serials |
| 2026-08-20 | Terraform state | `iac/terraform-state.json` | 3 EC2 instances, an EKS cluster and node group, an S3 bucket (security group skipped) |
| 2026-09-01 | Ansible | `ansible/inventory.ini`, `ansible/facts/` | Both GPU servers found by serial. `mgmt-b02-01` (an R650 nobody bought on this project) is shadow hardware. |
| 2026-09-20 and 09-27 | Redfish | `redfish/<date>/` | Both servers in service. One then updates its BIOS and BMC, and the other reports a memory warning. |
| 2026-09-27 | EC2 inventory | `discovery/aws-ec2-describe-instances.json` | One gateway is stopped, the router is missing, an untagged `g6.4xlarge` notebook is shadow, and one node-group member is recognized as managed |
| 2026-09-27 | Terraform plan | `iac/terraform-plan.json` | A new batch instance is planned and the EKS upgrade is logged |
| 2026-09-27 | EOL catalog | built-in sample catalog | Server, switch and Ubuntu 22.04 milestones within a year |

Result: `examples/pod-b02.ibom.json`. To rebuild it:

```bash
python3 -m ibom_ingest build samples/pod-b02/manifest.json
python3 -m ibom_ingest report examples/pod-b02.ibom.json --today 2026-09-27
```

`purchasing/make_binary_samples.py` regenerates the PDF and XLSX files.

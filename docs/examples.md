# Examples

Every example here validates against the schema and is rebuilt by the tests. All vendor names, part numbers, prices, serials and dates are **illustrative sample data**, not quotes or vendor notices.

## Hand-written I-BOMs

| File | What it shows |
|---|---|
| [minimal.ibom.json](examples/minimal.ibom.json) | The smallest useful I-BOM: a switch, its firmware and the install labor. Walked through in [Get started](getting-started.html). |
| [ai-gpu-rack.ibom.json](examples/ai-gpu-rack.ibom.json) | One liquid-assisted AI rack, 25 lines: 2 eight-GPU servers, an 800G switch, optics, firmware, OS, a license, a cloud bucket, labor and soft costs. Shows every block of the schema: rack placement and power budget, supply risk, sub-tier dependencies, embodied carbon, SBOM links and a known CVE, ERP and CMDB keys, a baseline, a drift finding and a shadow EC2 instance. |
| [ai-gpu-rack.cdx.json](examples/ai-gpu-rack.cdx.json) | The same rack exported to CycloneDX 1.6. It passes the official CycloneDX strict schema. |

The validator's rollup for the rack:

```text
PASS: examples/ai-gpu-rack.ibom.json is a valid I-BOM 0.1.0 (25 components, 28 relationships)
Total cost: 1,044,700 USD            (4.5% over the 1.0M budget)
Embodied carbon (Scope 3, where known): 8,710 kgCO2e
Supply risks:
  [critical 78] optic-800g: Approve ECO-0142 to qualify a third-party MSA-compliant DR8 optic; hold 10% spares.
  [high 71] srv-gpu-01: For pod A02, pre-qualify the B300 successor now; H200 allocation is shrinking.
  [high 62] cdu-a01: Qualify a second CDU vendor before pod A02 order.
Open drift: 1   Open shadow/unmanaged: 1
```

## Pod B02: one pod's life, end to end

A synthetic inference pod followed from quote to two weeks after go-live. Each stage's output is committed, so you can read it without running anything.

| Stage | Input | Output | Command |
|---|---|---|---|
| 1. Build the I-BOM from what the team already has | [samples/pod-b02](samples/pod-b02/): quote, PO, invoice PDF, receiving XLSX, Terraform, Ansible, Redfish, EC2 inventory | [pod-b02.ibom.json](examples/pod-b02.ibom.json): 20 lines with history, health, EOL dates and 2 shadow findings | `python3 -m ibom_ingest build samples/pod-b02/manifest.json` |
| 2. Operate it: drift, supply risk, scorecard | [samples/pod-b02-ops](samples/pod-b02-ops/): golden config, live exports from two dates, market signals | [pod-b02-assessed.ibom.json](examples/pod-b02-assessed.ibom.json) and the [scorecard](examples/pod-b02-scorecard.html) (overall 51/100: 4 drift findings, 4 of 6 purchased lines at high or critical supply risk) | `python3 -m ibom_risk build samples/pod-b02-ops/manifest.json` |
| 3. Sync it with ServiceNow and the ERP | [CMDB export](samples/pod-b02/cmdb/servicenow-cmdb-export.json) and [ERP exports](samples/pod-b02/erp/po-lines-me2m.csv) with planted discrepancies | [pod-b02-bridge/](examples/pod-b02-bridge/reconciliation.html): ServiceNow IRE payload and CSVs, SAP/Oracle/generic ERP CSVs, the [reconciliation report](examples/pod-b02-bridge/reconciliation.html) (32 findings) and the I-BOM with keys written back | `python3 -m ibom_bridge run samples/pod-b02/bridge.json` |

What the pod's story includes, so you know what to look for:

- **Procurement drift.** The PO orders 10 optics against a quote of 8, the invoice charges 912.40 for freight quoted at 850, and 2 optics are backordered.
- **Shadow infrastructure.** An R650 nobody bought on this project answers Ansible, and an untagged `g6.4xlarge` notebook runs in the AWS account.
- **Configuration drift.** A server's BMC lags its approved firmware, the switch runs an older EOS, S3 versioning is switched off, and someone lowers the switch MTU in the BOM with no change record.
- **Supply risk.** The backordered optics are predicted to miss the need-by date, the server and switch reach end of sale within a year, and both depend on single-source silicon.
- **ERP and CMDB disagreement.** A duplicate server CI, a retired status on a running VM, an invoice for goods not yet received, and an in-service server with no fixed asset.

## Using the files from other tools

All of these files are served from this site at stable paths, for example:

```text
https://metrickettster-coder.github.io/I-BOM-Infrastructure/ibom.schema.json
https://metrickettster-coder.github.io/I-BOM-Infrastructure/examples/ai-gpu-rack.ibom.json
```

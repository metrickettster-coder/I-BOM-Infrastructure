# ibom-ingest: ingestion engine and lifecycle tracker

`ibom_ingest` turns the documents and exports an infrastructure team already has into one I-BOM, then keeps each line's lifecycle current as new snapshots arrive. Its output always validates against `ibom.schema.json`.

| Input | Formats | What it writes |
|---|---|---|
| Vendor quotes, purchase orders, invoices, receiving and inventory lists | CSV, XLSX, PDF, plain text | Lines with price, quantity, supplier, ERP keys (PO/line, invoice, goods receipt, material, vendor id), serials, and status quoted → ordered → in-transit → received |
| Terraform / OpenTofu | `terraform show -json` (state) and `terraform show -json plan.tfplan` | Cloud and virtual lines keyed by IaC address: `virtual.iac`, resource id, region, instance specs, desired `configuration`. State means deployed. A plan's creates are planned, and its updates and destroys are logged as events. |
| Ansible | INI or YAML inventory plus gathered facts (`--tree` output or the jsonfile fact cache) | Serials matched to purchased units, hostnames, CPU/memory, an OS line per distribution with `runs-on` links, and BIOS versions for firmware history |
| Redfish | `ComputerSystem`, `Manager`, `Thermal`, `Power`, `EnvironmentMetrics` resources | Line and per-unit health with metrics, BIOS and BMC firmware history, and status moves (in-service, degraded, recovered) |
| Cloud inventory | `aws ec2 describe-instances`, Azure Resource Graph, `gcloud asset list`, or a generic JSON list | Running/stopped/terminated state on matched lines, shadow resources pulled in as `discovered-unmanaged` (costed), and `missing` findings for declared resources that weren't found |
| Vendor lifecycle | `catalogs/vendor_lifecycle.json`, or endoflife.date data | `lifecycle.vendorLifecycle`, with the stage recomputed from milestone dates on every run and `eol-notice` events when it moves |

## Quick start

```bash
pip install jsonschema pypdf openpyxl pyyaml     # pypdf, openpyxl and pyyaml only for PDF, XLSX and YAML inputs

# the whole sample pipeline from one manifest
python3 -m ibom_ingest build samples/pod-b02/manifest.json
python3 -m ibom_ingest report examples/pod-b02.ibom.json --today 2026-09-27

# or step by step, updating one file over time
python3 -m ibom_ingest init bom.json --project-id PRJ-1 --name "My pod"
python3 -m ibom_ingest purchasing bom.json quotes/ pos/ invoices/ receipts/
python3 -m ibom_ingest terraform  bom.json state.json
python3 -m ibom_ingest ansible    bom.json --inventory hosts.ini --facts facts/
python3 -m ibom_ingest redfish    bom.json redfish-snapshots/
python3 -m ibom_ingest discover   bom.json aws-ec2.json
python3 -m ibom_ingest eol        bom.json --endoflife ubuntu=Ubuntu
python3 -m ibom_ingest report     bom.json [--json]

python3 tests/test_ingest.py
```

Each command updates the file in place (or writes `-o`), raises `version` only when something changed, and refuses to write an I-BOM that fails validation. `--at` sets the timestamp used for events, which is useful for backfilling history.

## How lines are matched

Every parser emits records with match keys, and the builder merges records that share a key into one line:

| Key | Comes from |
|---|---|
| `part:` | Manufacturer part number or supplier SKU. They share one key space because invoices often print the SKU in the part column. |
| `serial:` | Receiving lists, invoices, Ansible facts, Redfish |
| `cloud:` | ARN, Azure resource id, GCP self-link, instance id |
| `iac:` | Terraform address or Ansible inventory host |

The keys are stored under `extensions["ibom.dev/ingest"]`, together with every source document and line that contributed, so re-ingestion finds the same line and each fact can be traced to its file.

Merge rules:

- **Price:** invoice beats PO, and PO beats quote (`costType` actual, then quoted). A lower-ranked document never overwrites a higher-ranked price.
- **Quantity:** the PO sets it, over the quote. Invoices and receipts can be partial, so they never change quantity.
- **Name and class:** the first document to mention a line names and classifies it. Later documents add facts but don't rename it.
- **Status:** a line only moves forward along planned → quoted → ordered → backordered → in-transit → received → staged → burn-in → deployed → in-service. Operational states (degraded, decommissioned, ...) are set explicitly. A document dated before the line's latest status change is treated as a replay and doesn't move status, so feeding a folder twice, or out of order, gives the same result.
- **Folder order:** a folder of purchasing documents is replayed by document date, then quote < PO < invoice < receipt.

## Shadow infrastructure

A serial, cloud resource or host found by discovery that matches no line is added with status `discovered-unmanaged`, a note, an estimated monthly cost where the instance type is known, and an open `unmanaged` observation. Discovery alone never makes it managed. When a declaring source (a PO, Terraform state or a receipt) later names the same serial or resource id, the line is adopted: its status moves on and the observation becomes `accepted`.

A few cases avoid false alarms:

- Members of a declared EKS node group (tag `eks:nodegroup-name`) count as managed.
- A physical host with an unknown serial whose model matches a purchased line with unserialized units fills one of those units, since it was received without a serial on file.
- A declared resource that discovery didn't find gets a `missing` observation, but only within the provider, region and category the export covered. The observation is closed when the resource reappears.

## Lifecycle tracking

- **Health:** each unit keeps its own `health`. The line shows the worst unit plus counts. Critical moves the line to `degraded` and logs an alert, and healthy again moves it back to `in-service`.
- **Firmware:** the last observed BIOS/BMC version per unit is stored under `extensions["ibom.dev/lifecycle"].observedFirmware`, and a `firmware-update` event is logged when it changes. Older snapshots are ignored. The desired version stays in `configuration`, so drift detection (thread 3) compares the two.
- **Vendor lifecycle:** catalog entries match on manufacturer, model, MPN, name, version and purl. The stage comes from the dates (end-of-life > end-of-support > end-of-sale > last-time-buy), so an unchanged catalog still moves a part along as time passes. `--endoflife PRODUCT[=REGEX]` pulls real release and EOL dates from endoflife.date into a local cache, and re-reads them from the cache unless you pass `--refresh`.
- **Report:** late deliveries (physical lines past their promised date), parts reaching EOL milestones within the horizon, unhealthy lines, firmware changes in the last 30 days, shadow items with cost, declared-but-missing resources, and expiring warranties, support and licenses. `--json` gives the same report for dashboards.

## Using real files instead of the samples

No code changes are needed:

- **New column names** (another reseller or ERP): add aliases in `mappings/purchasing.json`, or pass your own copy with `--mapping`. Header matching ignores case, punctuation and the words no/number.
- **PDF or text layouts:** edit the `pdf.header`, `pdf.line` and `pdf.serials` regexes in the same file.
- **Line classification** (labor, service, license, consumable, soft-cost, hardware category): edit the `classify` keyword rules.
- **Instance sizes and prices:** `catalogs/instance_types.json`
- **Vendor EOL notices:** `catalogs/vendor_lifecycle.json`. The shipped entries are marked `sample: true` and are illustrative; replace them with vendor notices.
- **A whole pipeline:** copy `samples/pod-b02/manifest.json` and point its paths at your exports.

## Known limits (v0.1)

- One line per part number within a document set. The same MPN bought on two POs becomes one line, with the quantity from the higher-ranked document, not the sum.
- PDF parsing is regex based. It handles text PDFs in a line-per-item layout; scanned PDFs need OCR first. An LLM-based extractor for free-form documents is a natural next step and would plug in as another reader.
- Network switches, PDUs and other non-Redfish gear need an SNMP or gNMI reader (not included yet).
- endoflife.date covers software, OS and some firmware. Hardware EOL dates still come from vendor notices.

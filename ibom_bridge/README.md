# ibom_bridge: ERP and CMDB interoperability

Procurement lives in the ERP, operations lives in the CMDB, and neither agrees with the other or with what is actually racked. `ibom_bridge` makes the I-BOM the shared key between them:

1. **Export** the I-BOM into the formats each system already imports.
2. **Reconcile** each system's own export against the I-BOM, line by line, and report every mismatch with the action that fixes it.
3. **Write back** the keys it matched (ServiceNow `sys_id`, fixed-asset number) so the next run matches exactly, even after a CI is renamed.

It needs only Python 3 and the repo's `jsonschema` dependency (plus `openpyxl` for XLSX exports). No ERP or CMDB access is needed to try it: `samples/pod-b02/cmdb/` and `samples/pod-b02/erp/` hold synthetic exports with planted discrepancies.

```bash
python3 -m ibom_bridge run samples/pod-b02/bridge.json      # writes examples/pod-b02-bridge/
python3 tests/test_bridge.py
```

## Commands

| Command | What it does |
|---|---|
| `cmdb-export bom.json -o out/` | ServiceNow IRE payload (`servicenow-ire.json`, for `POST /api/now/identifyreconcile`) and one Import Set CSV per CI class |
| `erp-export bom.json -o out/ --profile generic\|sap\|oracle --as-of DATE` | PO lines, fixed assets (with accumulated depreciation and net book value), a yearly depreciation schedule, and monthly recurring cloud spend |
| `reconcile bom.json --cmdb ... --erp-po ... --erp-gr ... --erp-invoices ... --erp-assets ...` | Any subset of sources. Writes `--report` (Markdown), `--view` (unified CSV), `--json` (findings), and with `--write-back out.json` or `--in-place` a validated I-BOM carrying the matched keys. `--fail-on high` exits 1 so it can gate a pipeline. |
| `cmdb-pull --instance dev12345 --classes cmdb_ci_server ... -o cmdb.json` | Export CIs from a live instance through the Table API (`SN_USER`, `SN_PASSWORD`) |
| `cmdb-push bom.json --instance dev12345 [--send]` | Send the IRE payload; prints it as a dry run unless `--send` |
| `run bridge.json` | All exports and the reconciliation from one manifest |

`cmdb-pull` and `cmdb-push` use only documented ServiceNow REST endpoints but have not been run against a live instance yet. A free ServiceNow Personal Developer Instance is enough to try them.

## ServiceNow CMDB

`mappings/servicenow.json` decides which lines become CIs and how (edit it rather than the code):

- **CI classes** by `class:category`: servers → `cmdb_ci_server`, switches → `cmdb_ci_ip_switch`, PDUs → `cmdb_ci_pdu`, cloud VMs → `cmdb_ci_vm_instance`, clusters → `cmdb_ci_kubernetes_cluster`, buckets → `cmdb_ci_cloud_object_storage`, and so on. Optics, cables, licenses, labor, services and soft costs are not CIs and stay in the I-BOM and ERP.
- **One CI per serialized unit.** A 2-server line becomes two CIs. Each CI's `correlation_id` is the I-BOM asset id (`bom-ref#serial`), which is what makes later matching exact.
- **Status**: I-BOM lifecycle status → `install_status` and `operational_status` (received → In Stock, deployed → Installed, rma → Pending Repair, disposed → Retired, ...).
- **Relationships**: I-BOM `runs-on`, `contains`, `connects-to`, `powered-by`, `depends-on` → ServiceNow relationship types.

Reconciliation matches each I-BOM asset to a CI by, in order: stored `sys_id`, `correlation_id`, cloud object id (an ARN in the I-BOM matches a bare `i-...` id in the CMDB), serial number, then name. It reads Table API JSON (plain or `sysparm_display_value=all`), CSV or XLSX list exports, and accepts child classes (a `cmdb_ci_linux_server` satisfies `cmdb_ci_server`).

| Finding | Meaning |
|---|---|
| `missing-in-cmdb` | Ordered or live in the I-BOM, no CI. High when the item is live. |
| `orphan-ci` | Active CI the I-BOM does not have. Raised to high when the ERP also carries the same serial as an asset. |
| `duplicate-ci` | Several CIs share one serial number or cloud id |
| `status-mismatch` | `install_status` disagrees with the lifecycle; high for a retired CI that is still running |
| `shadow-known-to-cmdb` | Shadow item (discovered-unmanaged) the CMDB already tracks, so it has an owner to adopt it |
| `field-mismatch` | Serial, model, manufacturer, cost center, asset tag or location differ |
| `weak-match`, `class-mismatch` | Matched by name only, or filed under an unexpected class |

## ERP

`mappings/erp.json` holds the column layouts. The `generic` profile uses plain names; `sap` uses the technical field names found in SAP migration templates (EBELN/EBELP, ANLN1/ANLN2, AKTIV, AFASL...); `oracle` follows the FA_MASS_ADDITIONS interface and PO line exports. Check either against your own system's templates before loading anything. Reading is forgiving: any header listed under `aliases` works, including SAP list-view labels such as `Purchasing Doc.`, `Item` and `Net Price`, so ME2M, MB51 and AR01 downloads read without editing.

**Fixed assets and depreciation.** Capex hardware that has been received, with a unit cost at or above `capitalizationThreshold` (1,000 by default), becomes one fixed asset per serialized unit. The capitalization date is the line's in-service date (or the date it was deployed). Depreciation uses `financial.depreciation` when present, otherwise a useful life by category (servers 60 months, PDUs 120, ...). Straight-line, declining-balance and double-declining-balance are supported, with a full-month convention from the capitalization month; declining balance switches to straight-line when that is larger and never goes below salvage value. Each asset row also carries embodied kgCO2e and its GHG Scope 3 category (2, capital goods) when the I-BOM has carbon data, so the asset register can report capital-goods emissions.

| Finding | Meaning |
|---|---|
| `quantity-mismatch`, `price-mismatch` | PO line differs from the I-BOM line (price tolerance 0.5%) |
| `procured-not-in-bom` | PO line on a project PO that the I-BOM does not have (bought, never engineered) |
| `bom-line-not-on-po` | I-BOM references a PO line the ERP does not have (deleted lines are ignored) |
| `receipt-not-posted` | I-BOM says received, the ERP has fewer goods receipted (reversals net out) |
| `bom-behind-erp` | The ERP received everything, the I-BOM has not caught up |
| `invoiced-not-received`, `invoice-price-variance` | Three-way match failures: block or query the payment |
| `not-capitalized` | Live capex hardware with no fixed asset, so depreciation never started |
| `asset-not-in-bom` | Fixed asset with no I-BOM line: possibly a ghost asset |
| `asset-retired-but-in-service`, `retire-asset` | Asset register and lifecycle disagree about retirement |
| `shadow-known-to-erp` | Shadow hardware that the ERP carries as an asset, with its cost center |
| `unallocated-spend` | Recurring cloud cost with no cost center; high when the resource is shadow infrastructure |
| `field-mismatch` | Vendor, material, cost center, G/L account, serial or acquisition value differs |

## Sample run (synthetic data)

`run samples/pod-b02/bridge.json` exports pod B02 to all formats, reconciles it against the sample CMDB (11 CIs) and ERP exports (12 PO lines, 8 goods receipts, 8 invoice lines, 7 fixed assets), and writes the results to `examples/pod-b02-bridge/`. It finds 32 issues (11 high), including every planted one: a duplicate server CI, a cloud VM retired in the CMDB while still running, 12 optics ordered against 10 needed with 10 invoiced and 6 received, a PDU receipt reversed after invoicing, a second GPU server never capitalized, a spare-PSU PO line nobody engineered, an unmanaged notebook VM costing 965.79 USD a month with no cost center, and a legacy server that both the CMDB and the asset register know but the I-BOM does not.

`examples/pod-b02-bridge/unified-view.csv` is the one-row-per-asset picture: I-BOM status, PO quantity and price, fixed-asset number and book value, CMDB `sys_id`, class and status, and the findings for that asset, followed by rows that exist only in the CMDB or the ERP.

## Not done yet

- Capitalizing freight and installation labor into asset values (the report flags the difference instead).
- Other CMDBs (BMC Helix, Device42, NetBox) and ERPs' native APIs: add a mapping file or a reader beside `servicenow.py` / `erp.py`.
- Drift and supply-chain risk live in the drift/risk engine, not here.

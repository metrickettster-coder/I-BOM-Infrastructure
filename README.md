# Open I-BOM: an open Infrastructure Bill of Materials format

Version 0.1.0 (draft), 2026-09-27

I-BOM is one JSON format that covers everything an infrastructure project uses: hardware, firmware, software, virtual and cloud resources, licenses, consumables, labor and soft costs. Each line also carries the data that keeps it current after go-live: lifecycle and health, drift baselines, multi-tier supply chain, SBOM links, Scope 3 carbon, and ERP/CMDB keys.

It is a **JSON Schema (draft 2020-12)** built to **align with CycloneDX 1.6**. It reuses CycloneDX's `bom-ref`, `serialNumber`, purl, cpe, hashes and dependency conventions, and it ships an exporter whose output passes the official CycloneDX 1.6 schema. Security tooling that already reads CycloneDX can read an I-BOM export with no changes.

## Files

| Path | What it is |
|---|---|
| `ibom.schema.json` | The schema |
| `examples/ai-gpu-rack.ibom.json` | Worked example: one liquid-assisted rack with 2 × 8-GPU servers, an 800G switch, optics, firmware, OS, a license, a cloud bucket, labor and soft costs (25 lines). All vendor data is illustrative. |
| `examples/ai-gpu-rack.cdx.json` | The same example exported to CycloneDX 1.6 |
| `tools/ibom_validate.py` | Validator: schema check plus rules JSON Schema can't express (below), then a cost, carbon, risk and drift rollup |
| `tools/ibom_to_cyclonedx.py` | Exporter to CycloneDX 1.6 JSON |
| `tests/test_ibom.py` | 12 positive and negative checks |
| `ibom_ingest/` | Ingestion engine and lifecycle tracker: quotes, POs, invoices, receipts (CSV/XLSX/PDF), Terraform state and plans, Ansible facts, Redfish, cloud inventories and EOL data → a validated I-BOM that stays current. See [ibom_ingest/README.md](ibom_ingest/README.md). |
| `samples/pod-b02/` | Synthetic, clearly labeled input files for a full ingestion run |
| `examples/pod-b02.ibom.json` | I-BOM built from those samples by `python3 -m ibom_ingest build samples/pod-b02/manifest.json` (20 lines, with history, health, EOL and shadow findings) |
| `tests/test_ingest.py` | 24 ingestion and lifecycle tests |
| `ibom_risk/` | Drift detection (golden config, approved baselines, live Redfish/cloud/Terraform-refresh/gNMI exports → drift and match observations), predictive supply chain risk (lead-time prediction, delay probability, sub-tier and single-source exposure, alternates) and the health and drift risk scorecard. See [ibom_risk/README.md](ibom_risk/README.md). |
| `samples/pod-b02-ops/` | Synthetic, clearly labeled golden config, live exports and market signals for two weeks of operations on pod B02 |
| `examples/pod-b02-assessed.ibom.json` | Pod B02 after `python3 -m ibom_risk build samples/pod-b02-ops/manifest.json`: baseline, drift history, supply risk on every purchased line |
| `examples/pod-b02-scorecard.md` | The scorecard for that I-BOM |
| `tests/test_risk.py` | 26 drift, supply risk and scorecard tests |

```bash
pip install jsonschema                       # cyclonedx-python-lib too, to check exports
python3 tools/ibom_validate.py examples/ai-gpu-rack.ibom.json
python3 tools/ibom_to_cyclonedx.py examples/ai-gpu-rack.ibom.json -o out.cdx.json
python3 tests/test_ibom.py

# ingestion + lifecycle (pip install pypdf openpyxl pyyaml for PDF/XLSX/YAML inputs)
python3 -m ibom_ingest build samples/pod-b02/manifest.json
python3 -m ibom_ingest report examples/pod-b02.ibom.json --today 2026-09-27
python3 tests/test_ingest.py

# drift detection + supply chain risk + scorecard
python3 -m ibom_risk build samples/pod-b02-ops/manifest.json
python3 -m ibom_risk scorecard examples/pod-b02-assessed.ibom.json --today 2026-10-12
python3 tests/test_risk.py
```

## How the schema addresses the three lists

| Item | Where it lives in the schema |
|---|---|
| **Problem: no standard format** | One document shape with a required core (`bom-ref`, `class`, `name`, `quantity`). Everything else is typed optional blocks, and `extensions`/`properties` hold vendor-specific data without forking the schema. |
| **Problem: rapid AI obsolescence** | `lifecycle.vendorLifecycle` (stage from `active` to `obsolete`, plus `nrnd`, last-time-buy, end-of-support and successor), `variants.alternates` with a `qualified` flag, and `effectivity` and `changes` for ECO/ECN tracking |
| **Problem: supply chain fragility** | `supply.sources[]` (tier, quoted/observed/predicted lead time, MOQ, price breaks, stock), `supply.subTierDependencies[]` (tier 2+ items such as HBM, CoWoS and optics DSPs), `singleSource`, and `supply.risk` (score, drivers, delay probability, need-by date, recommended action). Organizations carry their own risk profile. |
| **Problem: shadow infrastructure** | `observations[]` with `kind: "unmanaged"` records live resources that aren't in the BOM, and lifecycle status `discovered-unmanaged` marks them once pulled in. Cloud and virtual lines carry `virtual.iac` (Terraform/Ansible address) so declared state and discovered state can be reconciled. |
| **Opportunity: predictive logistics** | `leadTimeDays.predicted`, `risk.delayProbability`, `risk.recommendedAction` and pre-qualified `alternates` are the fields a model writes and a buyer acts on |
| **Opportunity: drift detection** | `baselines[]` (approved snapshot with expected values per JSON Pointer, plus an optional digest), per-component `drift` policy (tracked attributes, discovery sources, tolerance, `onDrift` action), `configuration` (desired state), and `observations[]` (match, drift, missing, unmanaged) |
| **Opportunity: carbon tracking** | `sustainability.embodiedCarbon` (kgCO2e, boundary, stage breakdown, GHG Protocol Scope 3 categories, method, uncertainty), `annualEnergyKwh`, and `location.facility` (PUE, grid intensity) for operating emissions |
| **Opportunity: hardware + software security** | Firmware and software are first-class lines tied to hardware by `runs-on`. `security.sbomRefs[]` links CycloneDX or SPDX SBOMs (including BOM-Link), and the section also holds `knownVulnerabilities[]` (with CISA KEV flag), root of trust, secure boot, attestation and counterfeit check. A CVE in BMC firmware therefore traces to every server and rack it runs on. |
| **Missing: open I-BOM standard** | This schema, openly licensed, with a CycloneDX export path so it extends the existing standard |
| **Missing: dynamic lifecycle tracking** | `lifecycle.status` (18 states from planned to disposed), key dates, `health` (status, metrics, remaining useful life, 90-day failure probability), `maintenance`, an append-only `events[]` history, and `disposal` (NIST 800-88 sanitization, recycler) |
| **Missing: ERP/CMDB interoperability** | `identifiers` holds ERP keys (material, asset, PR, PO and line, goods receipt, invoice, vendor), CMDB keys (CI id, class, sys_id), DCIM, cloud and IaC. `financial` holds GL account, cost center, WBS, capex/opex, direct/indirect and depreciation, so procurement and engineering reference the same line. |

## Document structure

```
I-BOM
├─ metadata        project, budget, contract type, BOM views served, ILM phase, revision, approval
├─ organizations[] manufacturers, suppliers, integrators, cloud providers, recyclers (+ risk, sustainability)
├─ locations[]     site > room > row > rack, or cloud account > region (+ rack power/cooling budget, PUE)
├─ components[]    every line item, classed as:
│                  hardware | firmware | software | virtual | cloud-resource |
│                  license | consumable | labor | service | soft-cost
├─ relationships[] typed graph: contains, runs-on, powered-by, cooled-by, connects-to, licensed-by, ...
├─ dependencies[]  optional CycloneDX-style list
├─ baselines[]     approved snapshots for drift detection
├─ observations[]  live findings: match / drift / missing / unmanaged
└─ changes[]       ECO / ECN / RFC records
```

One component line can be one serialized asset (`serialNumber`, `quantity: 1`) or a quantity with optional per-unit serials (`units[]`). The parent/child tree (`parent`) gives the multi-level BOM, and `relationships` gives the full topology graph for a graph database.

## Mapping from the BOM Best Practices Field Guide

Each field in `BOM/Bill of Materials (BOM) - Best Practices Field Guide.docx` has a home:

| Field Guide | I-BOM |
|---|---|
| Author name, time stamp | `metadata.authors`, `metadata.timestamp` |
| Part number, part name, description | `partNumber`, `name`, `description` |
| Quantity, unit of measure | `quantity`, `unitOfMeasure` |
| Cost, unit cost, extended cost | `financial.unitCost`, `financial.extendedCost`, `financial.costType` |
| Reference designator | `referenceDesignator` |
| Material specs/properties, dims | `physical.materials`, `physical.dimensionsMm`, `weightKg`, `lengthM` |
| Custom properties | `extensions`, `properties` |
| Alternate / substitute part number | `variants.alternates`, `variants.substitutes` |
| Configuration code | `variants.configurationCode` |
| Manufacturer, MPN | `manufacturer` (org ref), `mpn` |
| Supplier/vendor name, part number | `supply.sources[].supplier`, `supplierPartNumber` |
| Procurement type, make vs buy | `procurementType` |
| Lead time | `supply.sources[].leadTimeDays` |
| License information | `licensing` |
| Consumable | `consumable`, or `class: consumable` |
| Inherited | `variants.inherited` |
| BOM structure level, BOM type | `metadata.bomViews` covers all 27 BOM types on the BOM Types sheet plus infrastructure views; the sheet's single-level, multi-level and flat types go in `metadata.structure` |
| CAD / drawing number, attachments | `externalReferences[]` (`drawing`, `cad`, `datasheet`, ...) |
| Notes | `notes` |
| Site location | `placement.location` → `locations[]` |
| Lifecycle state, effectivity, revision, approval | `lifecycle.status`, `effectivity`, `revision`, `metadata.approval` |
| Compliance, certifications | `quality.compliance`, `quality.certifications` |
| Criticality/safety, priority | `criticality`, `priority` |
| Work instructions | `labor.workInstructions`, `externalReferences[type=work-instruction]` |

The manufacturing-only Field Guide fields (scrap rate, yield, work center, routing, cycle time) aren't core to infrastructure BOMs. They go under `extensions` for anyone who needs them.

## Validator rules beyond the schema

- Every `bom-ref` is unique across organizations, locations and components, and every reference resolves
- `extendedCost` = `unitCost` × `quantity`, in the same currency
- `serialNumber` only on quantity-1 lines, and `units[]` count ≤ quantity
- No two items occupy the same rack U on the same face, and nothing extends past the rack height
- Warnings for a rack's max power over its budget and for project cost over `metadata.project.budget`
- Observations reference known baselines, and non-shadow observations name a component

On the example, the rollup reports total cost of 1,044,700 USD (4.5% over the 1.0M budget), embodied carbon of 8,710 kgCO2e, three high or critical supply risks (800G optics, GPU servers, CDU), three end-of-life flags, one open firmware drift and one shadow EC2 GPU instance.

## CycloneDX export

| I-BOM | CycloneDX 1.6 |
|---|---|
| hardware | component `device` |
| firmware | component `firmware` |
| software (os / driver / orchestrator / other) | `operating-system` / `device-driver` / `platform` / `application` |
| cloud-resource | `services[]` |
| `parent` | nested `components` |
| relationships (runs-on, powered-by, depends-on, ...) | `dependencies` |
| `security.knownVulnerabilities` | `vulnerabilities` with VEX analysis state |
| `metadata.lifecyclePhase` | `metadata.lifecycles` |
| lifecycle, supply, sustainability, financial, identifiers, drift, placement | `ibom:data` property (compact JSON) |
| license, labor, consumable, service, soft-cost lines | `ibom:line` properties on the root component |

Nothing is lost on export, so a later importer can round-trip.

## Open questions for v0.2

- Canonicalization rule for `baselines[].digest` (propose RFC 8785 JCS over the tracked attributes)
- Whether to register `ibom:` as a CycloneDX property taxonomy namespace
- A matching SPDX 3.0 export

## License

Apache License 2.0. See [LICENSE](LICENSE).

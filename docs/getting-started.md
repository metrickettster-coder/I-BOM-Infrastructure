# Get started

This page takes you from nothing to a validated I-BOM, a CycloneDX export, and a full pipeline run on the sample pod. Everything runs locally with Python 3; no vendor, ERP or CMDB access is needed.

## 1. Get the code

```bash
git clone {{repo}}.git
cd I-BOM-Infrastructure
pip install jsonschema                      # required
pip install pypdf openpyxl pyyaml           # only for PDF, XLSX and YAML inputs
```

## 2. Write your first I-BOM

An I-BOM needs a format marker, a spec version, a serial number (`urn:uuid:`), a document version, `metadata` with a timestamp and project, and at least one component. This one describes a switch, the firmware it runs and the labor to install it. It is [examples/minimal.ibom.json](examples/minimal.ibom.json).

```json
{
  "$schema": "https://ibom.dev/schema/{{version}}/ibom.schema.json",
  "bomFormat": "I-BOM",
  "specVersion": "0.1.0",
  "serialNumber": "urn:uuid:3b0c7f7e-5d0a-4c1e-9d8a-2f6b1e4c9a10",
  "version": 1,
  "metadata": {
    "timestamp": "2026-09-27T12:00:00Z",
    "project": { "id": "PRJ-EDGE-01", "name": "Edge closet refresh (example)", "type": "edge" }
  },
  "organizations": [
    { "bom-ref": "org-acme", "name": "Acme Networks (example)", "roles": ["manufacturer"] }
  ],
  "components": [
    {
      "bom-ref": "sw-01", "class": "hardware", "category": "switch",
      "name": "48-port 10G access switch", "manufacturer": "org-acme", "mpn": "ACME-4810",
      "quantity": 1, "serialNumber": "ACM2026X0001",
      "lifecycle": { "status": "ordered" },
      "financial": {
        "unitCost": { "amount": 4200, "currency": "USD" },
        "extendedCost": { "amount": 4200, "currency": "USD" },
        "expenseType": "capex"
      }
    },
    { "bom-ref": "sw-01-nos", "class": "firmware", "category": "nos",
      "name": "Acme NOS", "version": "7.2.1", "quantity": 1 },
    {
      "bom-ref": "install", "class": "labor", "name": "Rack, cable and configure",
      "quantity": 6, "unitOfMeasure": "HR",
      "labor": { "role": "network engineer", "activity": "installation", "hours": 6,
                 "rate": { "amount": 120, "currency": "USD" } },
      "financial": {
        "unitCost": { "amount": 120, "currency": "USD" },
        "extendedCost": { "amount": 720, "currency": "USD" },
        "expenseType": "opex"
      }
    }
  ],
  "relationships": [
    { "from": "sw-01-nos", "type": "runs-on", "to": "sw-01" },
    { "from": "sw-01", "type": "installed-by", "to": "install" }
  ]
}
```

Things to notice:

- `class` is one of `hardware`, `firmware`, `software`, `virtual`, `cloud-resource`, `license`, `consumable`, `labor`, `service` or `soft-cost`. Labor lines need a `labor` block, licenses a `licensing` block, and cloud resources a `virtual` block.
- `bom-ref` values are the document's internal keys. `manufacturer`, `parent`, `placement.location` and relationships all point at them.
- Objects are closed, so a typo in a key fails validation instead of being silently kept. Put your own data in `extensions` or `properties`.

## 3. Validate it

```bash
python3 tools/ibom_validate.py examples/minimal.ibom.json
```

```text
PASS: examples/minimal.ibom.json is a valid I-BOM 0.1.0 (3 components, 2 relationships)

Total cost: 4,920 USD
  hardware                4,200
  labor                     720
Embodied carbon (Scope 3, where known): 0 kgCO2e
Open drift: 0   Open shadow/unmanaged: 0
```

Besides the schema, the validator checks that every `bom-ref` is unique and every reference resolves, that extended cost equals unit cost times quantity, that nothing overlaps in a rack, and it warns when a rack's power or the project's cost is over budget. The full list is in the [specification](spec.html#validator-rules-beyond-the-schema). Any other JSON Schema 2020-12 validator can check the schema part: point it at [ibom.schema.json](ibom.schema.json).

## 4. Export to CycloneDX

```bash
python3 tools/ibom_to_cyclonedx.py examples/minimal.ibom.json -o minimal.cdx.json
```

Hardware becomes CycloneDX `device` components, firmware becomes `firmware`, relationships become `dependencies`, and everything CycloneDX has no field for is kept in an `ibom:data` property, so nothing is lost. The [mapping table](spec.html#cyclonedx-export) lists every rule.

## 5. Run the whole pipeline on the sample pod

The repository ships a synthetic inference pod, B02, with a quote, PO, invoice PDF, receiving spreadsheet, Terraform, Ansible, Redfish snapshots, cloud inventories, a golden config, live exports, market signals, and ServiceNow and ERP exports. Every file is labeled as sample data.

```bash
# build the I-BOM from purchasing, IaC, discovery and telemetry
python3 -m ibom_ingest build samples/pod-b02/manifest.json
python3 -m ibom_ingest report examples/pod-b02.ibom.json --today 2026-09-27

# two weeks of operations: baseline, drift, supply risk, scorecard
python3 -m ibom_risk build samples/pod-b02-ops/manifest.json
python3 -m ibom_risk scorecard examples/pod-b02-assessed.ibom.json --today 2026-10-12 --markdown

# export to ServiceNow and ERP formats and reconcile their exports
python3 -m ibom_bridge run samples/pod-b02/bridge.json
```

The outputs of each step are already committed, so you can look at them first: see [Examples](examples.html).

## 6. Run the tests

```bash
python3 tests/test_ibom.py && python3 tests/test_ingest.py && python3 tests/test_risk.py && python3 tests/test_bridge.py
```

## Next

- [Specification](spec.html): document structure, design choices and mappings
- [Schema reference](schema.html): every object and property
- [Tools](tools/): the validator, exporter and three engines in detail

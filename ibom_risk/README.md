# ibom_risk: drift detection, supply chain risk and the scorecard

`ibom_risk` works on an I-BOM built by `ibom_ingest` (or any valid I-BOM). It answers two questions and rolls them into one page:

1. **Does the infrastructure still match its blueprint?** Desired configuration and approved baselines are compared with what Redfish, cloud APIs, Terraform refresh plans and network exports report. Every difference becomes a `drift` observation, and every fully matching line gets a `match` observation.
2. **What will stall the next build or the next spare?** Every purchased line gets a supply risk score, a predicted lead time, a probability of missing its need-by date, its hidden sub-tier dependencies, suggested alternates and a recommended action.

Everything is written into standard schema fields (`baselines`, `observations`, `changes`, `drift`, `supply.risk`, `supply.subTierDependencies`, `variants.alternates`, `organizations[].risk`). Nothing lives only in a report.

```bash
pip install jsonschema
python3 -m ibom_risk build samples/pod-b02-ops/manifest.json        # the sample, end to end
python3 -m ibom_risk scorecard examples/pod-b02-assessed.ibom.json --today 2026-10-12 [--markdown | --json]
python3 tests/test_risk.py
```

## Commands

| Command | What it does |
|---|---|
| `desired bom.json golden.json` | Merges a golden-configuration file into matching lines: `configuration` (desired state), `drift` policy and `criticality`. Rules match by `ref`, `class`, `category`, `mpn`/`name` regex, `resourceType` or `provider`. |
| `baseline bom.json --name N [--approved-by P] [--scope REF ...]` | Freezes every tracked value into `baselines[]` with a SHA-256 digest. Running it again with nothing changed adds nothing. |
| `drift bom.json FILES...` | Reads live exports, compares them with the approved values and writes observations. Firmware that `ibom_ingest` already recorded on the lines is included unless you pass `--no-bom-firmware`. |
| `supply bom.json [--signals S] [--need-by D] [--today D]` | Scores supply risk on every hardware and consumable line and rates suppliers. |
| `scorecard bom.json [--markdown/--json]` | Read-only health and drift risk scorecard. |
| `build manifest.json` | Runs a list of the steps above on an existing I-BOM. Steps named `ingest.<command>` run `ibom_ingest` commands (such as `ingest.redfish` or `ingest.discover`) in the same pipeline. |

## Drift detection

**What is compared.** A line's tracked attributes are `drift.attributes` (JSON Pointers) or, by default, every leaf under `configuration`. The expected value is the latest baseline's value. If `configuration` changed after the baseline, the new value is used only when an approved or implemented change record in `changes[]` covers the line. Otherwise the baseline wins and the edit itself is flagged as drift with source `manual-audit` (an unapproved blueprint change).

**Live sources** (auto-detected by `live.py`):

| Input | Produced by | Compared keys |
|---|---|---|
| `aws-ec2` | `aws ec2 describe-instances --output json` | `instanceType`, `ami`, `tags` |
| `eks-cluster` | `aws eks describe-cluster` | `version`, `tags` |
| `eks-nodegroup` | `aws eks describe-nodegroup` | `scalingConfig`, `instanceTypes`, `releaseVersion` |
| `terraform-drift` | `terraform plan -refresh-only` then `terraform show -json` (`resource_drift`) | every attribute that changed outside Terraform, even untracked ones |
| `redfish` | Redfish snapshot (same format `ibom_ingest redfish` reads) | `firmware.bios`, `firmware.bmc`, `powerCapW`, per serialized unit |
| `facts` | Any gNMI, NETCONF, eAPI, SNMP or agent export mapped to `{"source", "collectedAt", "devices": [{"keys", "state"}]}` | whatever `state` holds |

Lines are found with the same match keys `ibom_ingest` writes (`serial:`, `cloud:`, `iac:`, `nodegroup:`). Live items that match no line are counted but left to `ibom_ingest discover`, which owns shadow infrastructure. Values are normalized before comparison (camelCase keys, Terraform's single-item block lists unwrapped, case-insensitive strings) so IaC, cloud API and hand-written values line up. A source only speaks for the keys it reports.

**Observations.** One `drift` observation per line, attribute and unit (`OBS-DRIFT-<ref>-<attr>[-<unit>]`), with `expected`, `actual`, `baseline`, `source`, `observedAt` and the serial in `discovered.unit`. When the live value matches again the finding becomes `remediated`, and it reopens if the drift comes back. A finding someone marked `accepted` or `false-positive` stays that way while the live value is unchanged. One `match` observation per line and unit records when everything tracked matched.

**Severity** comes from the attribute (firmware, OS and NOS versions, AMIs, encryption and versioning are high; tags are low; everything else is medium), raised one step for `criticality: critical` and lowered one step for `low`.

**Policy** (`drift.onDrift`): `alert` and `ticket` open a finding (the ticket number is left for the ERP/CMDB bridge); `auto-remediate` opens a finding with a concrete remediation step in the scorecard; `accept-and-update-bom` writes the live value into `configuration`, logs an implemented `deviation` change record and a `config-change` event, and marks the finding `accepted`; `ignore` skips the line. `drift.tolerance` sets numeric tolerance per pointer (the sample lets the autoscaler move node count by 2).

## Supply chain risk

For each hardware and consumable line that is not shadow or retired:

- **Predicted lead time** = quoted lead time × the supplier's median slip in this BOM (actual ordered-to-received ÷ quoted, clamped 1-3×) × market-signal multipliers. Written to `supply.sources[].leadTimeDays.predicted`, with `observed` when the line has arrived.
- **Delay probability** (lines not yet received): expected arrival is the order date plus the predicted lead time, or today plus half a lead time if the line is already overdue. The slack against need-by goes through a logistic curve and is nudged up by the supplier's late-delivery rate. Need-by is `supply.risk.needBy`, else `--need-by`, else the promised date.
- **Score** = 100 × (1 − e^(−points/60)), so drivers add up without pinning at 100. Points come from single sourcing (one supplier and no qualified alternate; skipped for commodity consumables), single-source sub-tier parts, long or growing lead times, lateness and the supplier's on-time rate, vendor lifecycle (end of sale or last-time-buy within a year), market signals, export control and the delay probability. Line criticality scales the points. Levels: low < 25 ≤ medium < 50 ≤ high < 75 ≤ critical. The CLI prints every point and its reason.
- **Enrichment**: `catalogs/subtier.json` adds tier-2 and tier-3 dependencies (GPU die, BMC SoC, switch ASIC, laser die, breakers). `catalogs/alternates.json` plus the vendor's named successor add `variants.alternates`, always with `qualified: false` until engineering signs off. An alternate with a known stock lead time is recommended when it would arrive before need-by and the order will not.
- **Suppliers**: `organizations[].risk` gets the on-time delivery rate from this BOM's own history, financial health from signals, and a score.

Market signals (`--signals`) are a small JSON feed: each signal matches by `category`, `mpn` regex, `country` (sub-tier or source country of origin) or `supplier` name, and carries a `leadTimeFactor`, risk `drivers` and optional `points` or `financialHealth`. The built-in catalogs and the sample signals are illustrative. Replace them with distributor lead-time data, allocation notices and your own qualified-alternate list.

## Scorecard

Four areas scored 0-100, each with a letter grade, plus an overall score and a ranked action list:

| Area | Loses points for |
|---|---|
| Configuration drift | open drift findings, weighted by severity |
| Shadow infrastructure | open unmanaged items and declared items not found |
| Supply chain | the average of the three highest supply risk scores |
| Lifecycle and health | health warnings and failures, EOL milestones within the horizon, expiring contracts, late deliveries (from the `ibom_ingest` lifecycle report) |

It reads only the I-BOM, so it works as the read-only audit someone can run on an I-BOM they were handed.

## Limits

- The delay model is a transparent heuristic with made-up constants, not a trained model. The fields it writes (`predicted`, `delayProbability`) are where a trained model's output would go once real delivery history exists.
- Drift compares values. Graph-level drift (a cable moved, a VM on a different host) needs topology sources and is not covered yet.
- Opening tickets and writing back to a CMDB belong to the ERP/CMDB bridge.

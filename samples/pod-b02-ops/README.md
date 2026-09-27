# Sample: pod B02 operations, two weeks after go-live (synthetic data)

**Everything in this folder is sample data made up for testing.** Firmware versions, EOS releases, instance ids, lead-time multipliers and supplier notes are illustrative. They are not vendor recommendations, advisories or market data. It starts from `examples/pod-b02.ibom.json`, the I-BOM `ibom_ingest` built from `samples/pod-b02/`.

The story, in the order `manifest.json` replays it:

| Date | Step | File | What happens |
|---|---|---|---|
| 2026-09-28 | Golden config | `desired/golden-config.json` | Server BIOS/BMC versions, switch EOS/MTU/LLDP, a required `project` tag on the gateways, S3 versioning on, a tolerance of 2 on node-group size, and "accept managed upgrades" for the EKS cluster |
| 2026-09-28 | Baseline | | 18 tracked values frozen as BL-001, approved by the infrastructure lead |
| 2026-10-05 | Drift run | `live/2026-10-05/` | Server 7XK2Q35 still on the old BIOS and BMC. The switch runs an older EOS. gateway-0 was resized to g5.2xlarge in the console (EC2 and the Terraform refresh plan both see it). gateway-1 lost its `project` tag. S3 versioning was switched off. EKS was upgraded to 1.31 and accepted by policy. The autoscaler moved the node group from 2 to 3, which is within tolerance. |
| 2026-10-09 | Unreviewed edit | `desired/unreviewed-edit.json` | Someone lowers the switch MTU in the BOM with no change record |
| 2026-10-12 | Redfish + drift run | `live/2026-10-12/` | 7XK2Q35 got the new BIOS but not the new BMC. gateway-0 is back to g5.xlarge and gateway-1 has its tag again. S3 versioning is still off. The MTU edit is flagged as an unapproved BOM change. |
| 2026-10-12 | Supply risk | `supply/market-signals.json` | Need-by 2026-10-31. The two backordered optics are predicted to arrive 2026-11-17 (77% chance of missing need-by), so a compatible optic is suggested. The server and switch hit end of sale within a year and depend on single-source silicon. The reseller delivered 0 of 7 lines on time and is on credit watch. |

Result: `examples/pod-b02-assessed.ibom.json` and `examples/pod-b02-scorecard.md`. To rebuild them:

```bash
python3 -m ibom_risk build samples/pod-b02-ops/manifest.json
python3 -m ibom_risk scorecard examples/pod-b02-assessed.ibom.json --today 2026-10-12 --markdown > examples/pod-b02-scorecard.md
```

# Infrastructure health and drift risk scorecard
Inference pod B02 (sample data) (I-BOM version 2), as of 2026-10-12

Overall: 51/100, grade F

| Area | Score | Grade | Summary |
|---|---|---|---|
| Configuration drift | 55 | F | 4 open drift findings; 7 of 9 lines with declared configuration have a live source |
| Shadow infrastructure | 60 | D | 2 unmanaged items (about 966 USD/month), 1 declared items not found |
| Supply chain | 21 | F | 4 of 6 purchased lines at high or critical risk |
| Lifecycle and health | 68 | D | 2 lines with health issues, 3 with EOL milestones in 365 days, 1 late deliveries |

## Top actions (12)
1. [critical] hw-dcs-7050cx3-32s: /configuration/eosVersion is '4.31.1F', expected '4.32.2F'. Do: move eosVersion to 4.32.2F through the vendor's upgrade process, or raise a change to accept 4.31.1F.
2. [critical] hw-qsfp-100g-sr4: supply risk 82 (single-source, long-lead-time, lead-time-increasing, logistics, demand-spike, supplier-financial), 77% chance of missing 2026-10-31. Do: Expected 2026-11-17, 17 days after need-by 2026-10-31: expedite with Northwind IT Supply (sample); or buy alternate QSFP28-100G-SR4 (switch-vendor-coded compatible) (about 10 days, arrives before need-by; qualify first).
3. [critical] hw-r760xa-cto-l40s: supply risk 81 (single-source, long-lead-time, lead-time-increasing, logistics, eol-approaching, allocation, geopolitical, supplier-financial). Do: End of sale 2027-03-31: size a final buy for spares and expansion, or qualify PowerEdge R770xa (assumed); Single-source sub-tier (NVIDIA data-center GPU, ASPEED AST2600 BMC SoC, Server CPU): keep critical spares on hand.
4. [critical] hw-dcs-7050cx3-32s: supply risk 75 (single-source, long-lead-time, lead-time-increasing, logistics, eol-approaching, geopolitical, supplier-financial). Do: End of sale 2027-06-30: size a final buy for spares and expansion, or qualify DCS-7060X6 series (assumed); Single-source sub-tier (Broadcom Trident3 switch ASIC): keep critical spares on hand.
5. [high] hw-r760xa-cto-l40s 7XK2Q35: /configuration/firmware/bmc is '7.10.50.00', expected '7.10.70.00'. Do: update BMC to 7.10.70.00 on 7XK2Q35 through the BMC or the vendor's update tooling.
6. [high] tf-aws_s3_bucket.models: /configuration/versioning/enabled is False, expected True. Do: run terraform apply for aws_s3_bucket.models to restore the declared value, or update the code if the change was intended.
7. [high] hw-pdu-sw-0u-17k: supply risk 50 (single-source, long-lead-time, lead-time-increasing, logistics, supplier-financial). Do: Qualify alternate 0U switched PDU, 17.3 kW, 3-phase, C13/C19 mix (any listed vendor).
8. [high] llm-eval-notebook is running but in no BOM (about 966 USD/month). Do: find the owner, then adopt it into IaC/purchasing or shut it down.
9. [high] tf-module.inference.aws_instance.router is declared but was not found by cloud-api. Do: confirm whether it was deleted outside IaC; restore or remove it from the code.
10. [medium] Dell Inc. PowerEdge R650 is running but in no BOM. Do: find the owner, then adopt it into IaC/purchasing or shut it down.

## Drift findings (4)
- hw-r760xa-cto-l40s 7XK2Q35 /configuration/firmware/bmc: approved '7.10.70.00', live '7.10.50.00' (high, redfish 2026-10-12T06:00:00Z)
- hw-dcs-7050cx3-32s /configuration/eosVersion: approved '4.32.2F', live '4.31.1F' (critical, gnmi 2026-10-05T06:40:00Z)
- tf-aws_s3_bucket.models /configuration/versioning/enabled: approved True, live False (high, terraform-state 2026-10-12T07:00:00Z)
- hw-dcs-7050cx3-32s /configuration/mtu: approved 9214, BOM now says 9000 (medium, manual-audit 2026-10-12T08:00:00Z)

## Supply risk by line
- hw-qsfp-100g-sr4: 82 critical [single-source, long-lead-time, lead-time-increasing, logistics, demand-spike, supplier-financial], 77% chance of missing 2026-10-31
- hw-r760xa-cto-l40s: 81 critical [single-source, long-lead-time, lead-time-increasing, logistics, eol-approaching, allocation, geopolitical, supplier-financial]
- hw-dcs-7050cx3-32s: 75 critical [single-source, long-lead-time, lead-time-increasing, logistics, eol-approaching, geopolitical, supplier-financial]
- hw-pdu-sw-0u-17k: 50 high [single-source, long-lead-time, lead-time-increasing, logistics, supplier-financial]
- hw-cab-q28-dac-3m: 43 medium [single-source, lead-time-increasing, logistics, supplier-financial]
- con-nw-con-cage: 23 low [logistics, supplier-financial]

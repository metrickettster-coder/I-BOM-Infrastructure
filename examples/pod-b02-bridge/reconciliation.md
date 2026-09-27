# I-BOM / ERP / CMDB reconciliation

Sources: CMDB: 11 CIs; ERP: 12 PO lines; ERP: 8 goods receipts; ERP: 8 invoice lines; ERP: 7 fixed assets

**32 findings**: 11 high, 13 medium, 5 low, 3 info

Wrote CMDB and ERP keys back onto 13 I-BOM assets.

| Severity | System | Finding | I-BOM line | Record | Detail | Action |
|---|---|---|---|---|---|---|
| high | CMDB | duplicate-ci | hw-r760xa-cto-l40s #7XK2Q35 | 5f1d0c2a1b7e4a10a0000000000b0202, 9a3e77c01b7e4a10a0000000000b0299 | 2 CIs match serial number 7XK2Q35: 5f1d0c2a1b7e4a10a0000000000b0202 (cmdb_ci_linux_server), 9a3e77c01b7e4a10a0000000000b0299 (cmdb_ci_server) | Merge the duplicates in the CMDB and keep the one with the I-BOM correlation_id |
| high | CMDB | missing-in-cmdb | shadow-i-0fee1dead0c0ffee1 |  | cmdb_ci_vm_instance for llm-eval-notebook (unmanaged) (discovered-unmanaged) has no CMDB record | Create the CI from the export (servicenow-ire.json) |
| high | CMDB | missing-in-cmdb | tf-module.inference.aws_instance.router |  | cmdb_ci_vm_instance for inference-router (deployed) has no CMDB record | Create the CI from the export (servicenow-ire.json) |
| high | CMDB | orphan-ci | (not in I-BOM) | 3e8f11aa1b7e4a10a0000000000a0007 | cmdb_ci_linux_server dal1-legacy-07 (serial 9QW3L11) is in the CMDB but not in the I-BOM; the ERP also carries it as asset 100012 | Both systems know this unit: add it to the I-BOM if it serves this project, otherwise record where it belongs |
| high | CMDB | status-mismatch | tf-module.inference.aws_instance.gateway-1 | 7c2b9e101b7e4a10a0000000000c0002 | I-BOM says deployed (Installed), CMDB says Retired (I-BOM `Installed`, CMDB `Retired`) | Update the CMDB install status |
| high | ERP | asset-not-in-bom | (not in I-BOM) | asset 100012 | Fixed asset 100012 (PowerEdge R740xd, serial 9QW3L11) has no matching I-BOM line; the CMDB also has it as CI 3e8f11aa1b7e4a10a0000000000a0007 | Both systems know this unit: add it to the I-BOM if it serves this project, otherwise record where it belongs |
| high | ERP | invoiced-not-received | hw-pdu-sw-0u-17k | PO 4500018823 line 50 | PO 4500018823 line 50: invoiced 2, received 1 | Block payment until the goods arrive (three-way match fails) |
| high | ERP | invoiced-not-received | hw-qsfp-100g-sr4 | PO 4500018823 line 30 | PO 4500018823 line 30: invoiced 10, received 6 | Block payment until the goods arrive (three-way match fails) |
| high | ERP | not-capitalized | hw-r760xa-cto-l40s #7XK2Q35 |  | R760XA-CTO-L40S is in-service but has no fixed asset | Create the asset (row in erp-*-fixed-assets.csv) so depreciation starts |
| high | ERP | quantity-mismatch | hw-qsfp-100g-sr4 | PO 4500018823 line 30 | PO 4500018823 line 30: I-BOM needs 10, PO orders 12 (I-BOM `10`, ERP `12.0`) | Change the PO quantity or the BOM line |
| high | ERP | unallocated-spend | shadow-i-0fee1dead0c0ffee1 |  | llm-eval-notebook (unmanaged) costs 965.79 USD monthly with no cost center and is unmanaged (shadow) | Assign an owner and cost center, or shut it down |
| medium | CMDB | field-mismatch | hw-dcs-7050cx3-32s #JPE26210F1A | 5f1d0c2a1b7e4a10a0000000000b0210 | Cost center differs (I-BOM `CC-AI-INF`, CMDB `CC-NET-CORE`) | Align the CI cost center with the ERP/I-BOM value |
| medium | CMDB | missing-in-cmdb | hw-pdu-sw-0u-17k #PDU24A0193 |  | cmdb_ci_pdu for Switched rack PDU, zero-U, 17.3kW, 3-phase (received) has no CMDB record | Create the CI from the export (servicenow-ire.json) |
| medium | CMDB | missing-in-cmdb | hw-pdu-sw-0u-17k #PDU24A0194 |  | cmdb_ci_pdu for Switched rack PDU, zero-U, 17.3kW, 3-phase (received) has no CMDB record | Create the CI from the export (servicenow-ire.json) |
| medium | CMDB | shadow-known-to-cmdb | shadow-mgmt-b02-01 #4HJ8K21 | 5f1d0c2a1b7e4a10a0000000000b0230 | Dell Inc. PowerEdge R650 (mgmt-b02-01, unmanaged) is unmanaged in the I-BOM but the CMDB already tracks it as cmdb_ci_linux_server mgmt-b02-01 | Adopt it into the I-BOM (owner, cost center, PO) or retire the CI |
| medium | CMDB | status-mismatch | hw-dcs-7050cx3-32s #JPE26210F1A | 5f1d0c2a1b7e4a10a0000000000b0210 | I-BOM says received (In Stock), CMDB says Installed (I-BOM `In Stock`, CMDB `Installed`) | Update the CMDB install status |
| medium | ERP | field-mismatch | hw-dcs-7050cx3-32s #JPE26210F1A | asset 100047 | Acquisition value 21,900.00 vs I-BOM cost 21,400.00 (I-BOM `21400.0`, ERP `21900.0`) | Post the value adjustment (or capitalize freight/labor deliberately) |
| medium | ERP | field-mismatch | hw-pdu-sw-0u-17k | PO 4500018823 line 50 | PO 4500018823 line 50: Cost center differs (I-BOM `CC-AI-INF`, ERP `CC-DC-FAC`) |  |
| medium | ERP | invoice-price-variance | hw-dcs-7050cx3-32s | PO 4500018823 line 20 | PO 4500018823 line 20: invoiced at 21,900.00 per unit vs PO 21,400.00 | Resolve with the supplier before payment |
| medium | ERP | invoice-price-variance | soft-nw-frt | PO 4500018823 line 90 | PO 4500018823 line 90: invoiced at 912.40 per unit vs PO 850.00 | Resolve with the supplier before payment |
| medium | ERP | price-mismatch | soft-nw-frt | PO 4500018823 line 90 | PO 4500018823 line 90: I-BOM actual unit cost 912.40 vs PO price 850.00 (+7.3%) (I-BOM `912.4`, ERP `850.0`) | Approve the variance or correct the PO/invoice |
| medium | ERP | procured-not-in-bom | (not in I-BOM) | PO 4500018823 line 110 | PO 4500018823 line 110 (Spare PSU 2400W titanium, 2,440.00) is not in the I-BOM | Add it to the BOM or cancel the PO line |
| medium | ERP | receipt-not-posted | hw-pdu-sw-0u-17k | PO 4500018823 line 50 | PO 4500018823 line 50: I-BOM shows received, ERP has goods receipt for 1 of 2 (I-BOM `2`, ERP `1.0`) | Post the goods receipt so the invoice can clear |
| medium | ERP | shadow-known-to-erp | shadow-mgmt-b02-01 #4HJ8K21 | asset 100019 | Dell Inc. PowerEdge R650 (mgmt-b02-01, unmanaged) is unmanaged in the I-BOM but the ERP carries it as asset 100019 (cost center CC-IT-OPS) | Adopt it into the I-BOM with that owner and cost center |
| low | CMDB | weak-match | tf-module.inference.aws_eks_cluster.this | 7c2b9e101b7e4a10a0000000000c0010 | Matched cmdb_ci_kubernetes_cluster inf-b02 by name only | Confirm, then set its serial number or correlation_id so the next run matches exactly |
| low | ERP | unallocated-spend | tf-module.inference.aws_eks_node_group.gpu |  | module.inference.aws_eks_node_group.gpu costs 587.65 USD monthly with no cost center | Assign an owner and cost center, or shut it down |
| low | ERP | unallocated-spend | tf-module.inference.aws_instance.gateway-0 |  | inference-gateway-0 costs 734.38 USD monthly with no cost center | Assign an owner and cost center, or shut it down |
| low | ERP | unallocated-spend | tf-module.inference.aws_instance.gateway-1 |  | inference-gateway-1 costs 734.38 USD monthly with no cost center | Assign an owner and cost center, or shut it down |
| low | ERP | unallocated-spend | tf-module.inference.aws_instance.router |  | inference-router costs 147.17 USD monthly with no cost center | Assign an owner and cost center, or shut it down |
| info | CMDB | field-mismatch | hw-dcs-7050cx3-32s #JPE26210F1A | 5f1d0c2a1b7e4a10a0000000000b0210 | Location differs (I-BOM `DAL1 receiving dock (sample)`, CMDB `DAL1 Hall 2 / Row C / Rack C14`) | Update whichever is stale; the I-BOM placement may still be the receiving location |
| info | CMDB | field-mismatch | hw-r760xa-cto-l40s #7XK2Q34 | 5f1d0c2a1b7e4a10a0000000000b0201 | Location differs (I-BOM `DAL1 receiving dock (sample)`, CMDB `DAL1 Hall 2 / Row C / Rack C14`) | Update whichever is stale; the I-BOM placement may still be the receiving location |
| info | CMDB | field-mismatch | hw-r760xa-cto-l40s #7XK2Q35 | 5f1d0c2a1b7e4a10a0000000000b0202 | Location differs (I-BOM `DAL1 receiving dock (sample)`, CMDB `DAL1 Hall 2 / Row C / Rack C14`) | Update whichever is stale; the I-BOM placement may still be the receiving location |

Finding counts by kind: erp:unallocated-spend 5, cmdb:missing-in-cmdb 4, cmdb:field-mismatch 4, cmdb:status-mismatch 2, erp:invoiced-not-received 2, erp:field-mismatch 2, erp:invoice-price-variance 2, cmdb:duplicate-ci 1, cmdb:orphan-ci 1, erp:asset-not-in-bom 1, erp:not-capitalized 1, erp:quantity-mismatch 1, cmdb:shadow-known-to-cmdb 1, erp:price-mismatch 1, erp:procured-not-in-bom 1, erp:receipt-not-posted 1, erp:shadow-known-to-erp 1, cmdb:weak-match 1

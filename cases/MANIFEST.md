# cases/ manifest

- run_id: `final`
- model: `claude-sonnet-5` (effort high)
- engine: `engine`
- git: `813a5f1df4b473da8aef495893efe3941375a82e`
- prompt sha256: `assess.md`=b25e760df35e, `explain.md`=d03890f8f0cc, `investigate.md`=968f775f5dc5, `nba.md`=a58ac2deb54e, `sar.md`=07469f5fe613, `system.md`=b00cac74ffbe

| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes |
|---|---|---|---|---|---|---|---|---|
| HHG-001 | legitimate | 0.1 | none | False | 15 | 241400 | 137.3 |  |
| HHG-002 | fraud | 0.92 | card_not_present_fraud | False | 17 | 212881 | 163.3 |  |
| HHG-003 | uncertain | 0.62 | out_of_region_use | False | 17 | 416827 | 237.6 |  |
| HHG-004 | uncertain | 0.58 | card_not_present_new_device | False | 21 | 276983 | 112.6 |  |
| HHG-005 | legitimate | 0.04 | none | False | 21 | 244121 | 139.1 |  |
| HHG-006 | fraud | 0.9 | undocumented | True | 23 | 301462 | 167.1 |  |
| HHG-007 | fraud | 0.77 | account_takeover | False | 16 | 518676 | 200.0 |  |
| HHG-008 | fraud | 0.9 | card_not_present_new_device | False | 18 | 341530 | 128.7 |  |
| HHG-009 | fraud | 0.9 | card_not_present_fraud | False | 16 | 180872 | 127.8 |  |
| HHG-010 | legitimate | 0.03 | none | False | 20 | 284221 | 163.7 |  |
| HHG-011 | uncertain | 0.6 | card_not_present_new_device | False | 21 | 566083 | 209.3 |  |
| HHG-012 | legitimate | 0.05 | none | False | 15 | 306806 | 128.5 |  |
| HHG-013 | legitimate | 0.08 | none | False | 19 | 348062 | 113.2 |  |
| HHG-014 | fraud | 0.9 | undocumented | True | 22 | 420041 | 179.9 |  |
| HHG-015 | uncertain | 0.35 | account_takeover | False | 22 | 289079 | 177.1 |  |
| HHG-016 | fraud | 0.77 | card_not_present_new_device | False | 18 | 221021 | 141.5 |  |
| HHG-017 | legitimate | 0.08 | none | False | 21 | 246200 | 129.7 |  |
| HHG-018 | uncertain | 0.6 | out_of_region_use | False | 17 | 452572 | 199.6 |  |
| HHG-019 | fraud | 0.77 | card_not_present_new_device | False | 20 | 305996 | 186.6 |  |
| HHG-020 | legitimate | 0.03 | none | False | 21 | 215949 | 99.2 |  |

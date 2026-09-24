# cases/ manifest

- cases: 20 (HHG-001, HHG-002, HHG-003, HHG-004, HHG-005, HHG-006, HHG-007, HHG-008, HHG-009, HHG-010, HHG-011, HHG-012, HHG-013, HHG-014, HHG-015, HHG-016, HHG-017, HHG-018, HHG-019, HHG-020)
- runs: `live-final`; last promoted: `live-final`
- model: `claude-sonnet-5` (effort high), llm backend `cli`
- engine: `engine`
- git (per case below): `49768d051b49c7d33cedbd0e50a27b5e676596cf`
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=a929aacba6f0, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=be81bd123234

| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes | run_id | git |
|---|---|---|---|---|---|---|---|---|---|---|
| HHG-001 | legitimate | 0.1 | none | False | 15 | 239999 | 135.7 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-002 | fraud | 0.92 | card_not_present_fraud | False | 17 | 226345 | 154.3 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-003 | uncertain | 0.6 | out_of_region_use | False | 17 | 318761 | 190.4 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-004 | uncertain | 0.55 | card_not_present_new_device | False | 21 | 263586 | 142.1 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-005 | legitimate | 0.08 | none | False | 21 | 238243 | 122.1 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-006 | fraud | 0.9 | undocumented | True | 24 | 328619 | 163.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-007 | fraud | 0.77 | account_takeover | False | 16 | 493407 | 207.6 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-008 | fraud | 0.94 | card_not_present_new_device | False | 18 | 305119 | 134.6 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-009 | fraud | 0.91 | card_not_present_fraud | False | 18 | 176528 | 120.6 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-010 | legitimate | 0.07 | none | False | 21 | 239360 | 130.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-011 | uncertain | 0.6 | card_not_present_new_device | False | 19 | 499394 | 191.4 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-012 | legitimate | 0.04 | none | False | 15 | 307988 | 138.5 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-013 | legitimate | 0.09 | none | False | 19 | 359515 | 152.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-014 | fraud | 0.9 | undocumented | True | 22 | 286435 | 148.5 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-015 | uncertain | 0.3 | account_takeover | False | 20 | 305918 | 263.9 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-016 | fraud | 0.77 | card_not_present_new_device | False | 18 | 254271 | 125.7 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-017 | legitimate | 0.08 | none | False | 20 | 244874 | 156.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-018 | uncertain | 0.55 | out_of_region_use | False | 17 | 399494 | 207.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-019 | fraud | 0.77 | card_not_present_new_device | False | 21 | 286436 | 186.8 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |
| HHG-020 | legitimate | 0.05 | none | False | 21 | 222569 | 125.6 |  | live-final | 49768d051b49c7d33cedbd0e50a27b5e676596cf |

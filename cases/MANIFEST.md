# cases/ manifest

`cases/` holds two rounds of answer files: 18 Final Round files (HHF-001 … HHF-018) and 20 Round 1 files
(HHG-001 … HHG-020). `make validate` checks all 38: 38 clean, 0 problems.

## Final Round (HHF-001 … HHF-018)

- run: `r2-final`. Each `cases/HHF-*.json` is byte-identical to `runs/r2-final/HHF-*/answer.json`.
- ran: 2026-10-31 07:24:12 to 08:08:07 (`invocation_started` and last `finished` in `runs/r2-final/run_manifest.json`)
- model: `gemini-2.5-flash` for all 145 model calls (`kind=llm` rows of `calls.jsonl`), llm backend `gemini`; `sar_model` `gemini-2.5-flash`
- git: `7de7ddc018bea7091ffe4aac010d72916a448912`, `git_dirty: true` (the same for all 18 cases)
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=87b9249c670e, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=940a201b9574
- detectors: D1-D4 ran on the local DuckDB snapshot. The run has no `typology_scan` call, because it predates the in-graph port.

| case | verdict | p | pattern | final actions (route) | sar | tool_calls | tokens | latency_s |
|---|---|---|---|---|---|---|---|---|
| HHF-001 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 21 | 229193 | 95.4 |
| HHF-002 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 283051 | 107.3 |
| HHF-003 | fraud | 0.94 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L2), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 282956 | 118.8 |
| HHF-004 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 228101 | 148.0 |
| HHF-005 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 222975 | 93.9 |
| HHF-006 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 20 | 193114 | 85.1 |
| HHF-007 | fraud | 0.92 | card_testing | CREATE_CASE (auto), STEP_UP_AUTH (auto), DECLINE_TRANSACTION (L1), BLOCK_CARD (L1) | False | 19 | 155902 | 62.9 |
| HHF-008 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 18 | 174437 | 306.0 |
| HHF-009 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 18 | 138493 | 68.5 |
| HHF-010 | uncertain | 0.4 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 16 | 228439 | 118.2 |
| HHF-011 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 16 | 272135 | 111.2 |
| HHF-012 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 15 | 126658 | 91.4 |
| HHF-013 | uncertain | 0.35 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 16 | 143403 | 78.5 |
| HHF-014 | uncertain | 0.3 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 16 | 201999 | 87.0 |
| HHF-015 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 16 | 145933 | 121.2 |
| HHF-016 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 16 | 324743 | 108.2 |
| HHF-017 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 22 | 249230 | 102.3 |
| HHF-018 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 22 | 258408 | 124.9 |

Totals: 7 fraud / 3 uncertain / 8 legitimate; 6 SARs; final actions 53 auto, 10 L1, 7 L2; 339 tool calls, 3,859,170
tokens, 2,028.8 s. Per case: tool calls median 18.5 (p95 22), tokens median 225,538 (p95 324,743), latency median
104.8 s (p95 306.0 s). p95 is nearest-rank, which with 18 values is the maximum.

Sources: `cases/HHF-*.json` (`case.verdict`, `case.fraud_probability`, `case.pattern`, `next_best_actions.final`, `sar.file`, `tool_calls`, `tokens`, `latency_s`); `runs/r2-final/run_manifest.json`; `runs/r2-final/HHF-*/calls.jsonl`.

## Round 1 (HHG-001 … HHG-020)

- cases: 20 (HHG-001, HHG-002, HHG-003, HHG-004, HHG-005, HHG-006, HHG-007, HHG-008, HHG-009, HHG-010, HHG-011, HHG-012, HHG-013, HHG-014, HHG-015, HHG-016, HHG-017, HHG-018, HHG-019, HHG-020)
- runs: `live-final-2`; last promoted: `live-final-2`
- model: `claude-sonnet-5` (effort high), llm backend `cli`
- engine: `engine`
- git (per case below): `be035338a08c5fd960be2311786659bfb67ade55`
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=a929aacba6f0, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=be81bd123234

| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes | run_id | git |
|---|---|---|---|---|---|---|---|---|---|---|
| HHG-001 | legitimate | 0.1 | none | False | 16 | 232932 | 131.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-002 | fraud | 0.92 | card_not_present_fraud | False | 17 | 238885 | 203.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-003 | uncertain | 0.6 | out_of_region_use | False | 17 | 308253 | 184.0 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-004 | uncertain | 0.6 | card_not_present_new_device | False | 20 | 305455 | 146.4 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-005 | legitimate | 0.03 | none | False | 20 | 244752 | 158.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-006 | fraud | 0.9 | undocumented | True | 24 | 294362 | 182.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-007 | fraud | 0.77 | account_takeover | False | 16 | 483760 | 228.5 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-008 | fraud | 0.92 | card_not_present_new_device | False | 18 | 310639 | 158.2 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-009 | fraud | 0.9 | card_not_present_fraud | False | 17 | 242452 | 166.7 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-010 | legitimate | 0.05 | none | False | 20 | 311261 | 252.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-011 | uncertain | 0.6 | card_not_present_new_device | False | 21 | 616283 | 203.9 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-012 | legitimate | 0.03 | none | False | 15 | 294239 | 112.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-013 | legitimate | 0.09 | none | False | 19 | 359953 | 157.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-014 | fraud | 0.9 | undocumented | True | 22 | 284222 | 128.0 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-015 | uncertain | 0.35 | account_takeover | False | 19 | 314210 | 192.9 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-016 | fraud | 0.77 | card_not_present_new_device | False | 21 | 264901 | 148.1 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-017 | legitimate | 0.1 | none | False | 20 | 239841 | 143.8 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-018 | uncertain | 0.55 | out_of_region_use | False | 17 | 418595 | 182.2 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-019 | fraud | 0.77 | card_not_present_new_device | False | 22 | 291206 | 180.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |
| HHG-020 | legitimate | 0.05 | none | False | 21 | 236845 | 142.6 |  | live-final-2 | be035338a08c5fd960be2311786659bfb67ade55 |

Sources: `cases/HHG-*.json` (byte-identical to `runs/live-final-2/HHG-*/answer.json`); `runs/live-final-2/run_manifest.json`.

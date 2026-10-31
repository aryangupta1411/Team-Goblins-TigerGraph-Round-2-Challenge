# cases/ manifest

`cases/` holds two rounds of answer files: 18 Final Round files (HHF-001 … HHF-018) and 20 Round 1 files
(HHG-001 … HHG-020). `make validate` checks all 38: 38 clean, 0 problems.

## Final Round (HHF-001 … HHF-018)

- run: `hhf-final`. Each `cases/HHF-*.json` is byte-identical to `runs/hhf-final/HHF-*/answer.json`.
- invocations (`runs/hhf-final/run_manifest.json`):
  - main run, started 2026-10-31 11:02:41. HHF-005 failed closed in P5: Gemini's structured output failed validation three times, so no answer was written. The run went on to finish HHF-003 and the other 16 cases.
  - `--resume`, started 11:35:37, after HHF-005's write-back was removed: HHF-005, then HHF-003, in `opened_at` order. The last case finished at 11:38:45.
  - the failure, the superseded first HHF-003 attempt and the tracked-file guard are in `runs/hhf-final/provenance.json` and `runs/hhf-final/_failed_attempt/`.
- model: `gemini-2.5-flash` for all 141 model calls (`kind=llm` rows of `calls.jsonl`, 0 failed); llm backend `gemini`; `sar_model` `gemini-2.5-flash`.
- git: `b397c2c97b5bf1b4cded77e951cfd58e5be8c70b`, `git_dirty: false`, the same for both invocations and all 18 cases.
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=87b9249c670e, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=940a201b9574
- detectors: D1-D4 ran in the graph through the installed `typology_scan`: 18 harness calls through MCP, all `ok`.
- superseded, not promoted: `r2-final`, `r2-live-gemini`, `r2-graph`, and `r2-submit`, which was discarded because tracked files changed while it was starting.

| case | verdict | p | pattern | final actions (route) | sar | tool_calls | tokens | latency_s |
|---|---|---|---|---|---|---|---|---|
| HHF-001 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 331456 | 186.4 |
| HHF-002 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 246248 | 134.8 |
| HHF-003 (resumed run) | fraud | 0.94 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L2), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 23 | 242287 | 84.7 |
| HHF-004 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 22 | 217775 | 93.2 |
| HHF-005 (resumed run) | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 21 | 191664 | 101.4 |
| HHF-006 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 20 | 180782 | 90.9 |
| HHF-007 | fraud | 0.92 | card_testing | CREATE_CASE (auto), STEP_UP_AUTH (auto), DECLINE_TRANSACTION (L1), BLOCK_CARD (L1) | False | 19 | 206345 | 97.4 |
| HHF-008 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 19 | 180370 | 69.8 |
| HHF-009 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 22 | 216185 | 114.9 |
| HHF-010 | uncertain | 0.35 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 17 | 193244 | 76.0 |
| HHF-011 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 269213 | 96.1 |
| HHF-012 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 16 | 133625 | 62.4 |
| HHF-013 | uncertain | 0.3 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), MONITOR_CONNECTED_CARDS (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 15 | 115149 | 77.4 |
| HHF-014 | uncertain | 0.4 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 17 | 188194 | 74.0 |
| HHF-015 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 16 | 128606 | 63.7 |
| HHF-016 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 323889 | 96.6 |
| HHF-017 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 24 | 282878 | 131.4 |
| HHF-018 | legitimate | 0.05 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 23 | 252686 | 88.4 |

Totals: 7 fraud / 3 uncertain / 8 legitimate; 6 SARs; final actions 54 auto, 10 L1, 7 L2; 352 tool calls, 3,900,596
tokens, 1,739.5 s. Per case: tool calls median 19.5 (p95 24), tokens median 211,265 (p95 331,456), latency median
92.1 s (p95 186.4 s). p95 is nearest-rank, which with 18 values is the maximum.

Sources: `cases/HHF-*.json` (`case.verdict`, `case.fraud_probability`, `case.pattern`, `next_best_actions.final`, `sar.file`, `tool_calls`, `tokens`, `latency_s`); `runs/hhf-final/run_manifest.json`; `runs/hhf-final/HHF-*/calls.jsonl`. Failure detail: `runs/hhf-final/provenance.json` (`failed_attempt`) and `runs/hhf-final/_failed_attempt/HHF-005/`.

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

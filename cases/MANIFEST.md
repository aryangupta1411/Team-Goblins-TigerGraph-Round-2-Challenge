# cases/ manifest

`cases/` holds two rounds of answer files: 18 Final Round files (HHF-001 … HHF-018) and 20 Round 1 files
(HHG-001 … HHG-020). `make validate` checks all 38: 38 clean, 0 problems.

## Final Round (HHF-001 … HHF-018)

- run: `hhf-final-2`. Each `cases/HHF-*.json` is byte-identical to `runs/hhf-final-2/HHF-*/answer.json`.
- one invocation (`runs/hhf-final-2/run_manifest.json`), 2026-11-01 22:41:46 to 23:03:26: 18 / 18 completed, 0 failed,
  nothing resumed.
- model: `gemini-3.6-flash` for all 143 model calls (`kind=llm` rows of `calls.jsonl`, 0 failed); llm backend `gemini`;
  `sar_model` `gemini-3.6-flash`.
- git: `8dc9e0723f42bd735e1c4d5b36e8ad09c7e988d0` (tag `hhf-final-2-run`), `git_dirty: false`. A tracked-file guard
  hashed 642 files before and after the run; the two lists are identical (`runs/hhf-final-2/provenance.json`).
- prompt sha256: `assess.md`=ff8dd8b37336, `explain.md`=9580d7143059, `investigate.md`=87b9249c670e, `nba.md`=67043e265092, `sar.md`=b68538088664, `system.md`=940a201b9574
- detectors: D1-D4 ran in the graph through the installed `typology_scan`: 18 harness calls through MCP, all `ok`.
- official grader (`grader/grade.py`, sha256 b27e39ce756b…): 18 / 18 clean, 0 hard, 0 policy, 1 warning (HHF-013
  `flagged_not_affected`: the flagged in-person purchase is genuine; the ring payments are the affected transactions).
- supersedes `hhf-final` (commit `b397c2c`, `gemini-2.5-flash`), kept in `runs/hhf-final/`. Four verdicts changed:
  HHF-008 legitimate → uncertain, HHF-010 and HHF-014 uncertain → legitimate, HHF-013 uncertain → fraud. The
  `hhf-final` write-backs were deleted from the graph before this run (`provenance.json`, `graph_before_run`).

| case | verdict | p | pattern | final actions (route) | sar | tool_calls | tokens | latency_s |
|---|---|---|---|---|---|---|---|---|
| HHF-001 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 26 | 271576 | 105.1 |
| HHF-002 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 26 | 350878 | 82.8 |
| HHF-003 | fraud | 0.94 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L2), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 25 | 222567 | 101.7 |
| HHF-004 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 23 | 229349 | 63.1 |
| HHF-005 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 23 | 227510 | 78.4 |
| HHF-006 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 23 | 254832 | 93.2 |
| HHF-007 | fraud | 0.92 | card_testing | CREATE_CASE (auto), STEP_UP_AUTH (auto), DECLINE_TRANSACTION (L1), BLOCK_CARD (L1) | False | 18 | 158123 | 52.2 |
| HHF-008 | uncertain | 0.3 | out_of_region_use | CREATE_CASE (auto), MONITOR_CARD (auto), DECLINE_TRANSACTION (L1), ESCALATE_TO_ANALYST (auto) | False | 19 | 193441 | 72.5 |
| HHF-009 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 22 | 158933 | 56.3 |
| HHF-010 | legitimate | 0.01 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 191960 | 57.3 |
| HHF-011 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 267005 | 82.0 |
| HHF-012 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 159324 | 55.1 |
| HHF-013 | fraud | 0.92 | undocumented | CREATE_CASE (auto), MONITOR_CONNECTED_CARDS (auto), BLOCK_CARD (L1), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | True | 20 | 205155 | 95.7 |
| HHF-014 | legitimate | 0.1 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 185542 | 53.7 |
| HHF-015 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 148577 | 52.5 |
| HHF-016 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 17 | 322107 | 57.8 |
| HHF-017 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 23 | 174657 | 75.6 |
| HHF-018 | legitimate | 0.0 | none | CREATE_CASE (auto), ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | False | 23 | 250837 | 57.9 |

Totals: 8 fraud / 1 uncertain / 9 legitimate; 7 SARs; final actions 53 auto, 9 L1, 8 L2; 373 tool calls, 3,972,373
tokens, 1,292.9 s. Per case: tool calls median 21 (p95 26), tokens median 213,861 (p95 350,878), latency median
67.8 s (p95 105.1 s). p95 is nearest-rank, which with 18 values is the maximum.

Sources: `cases/HHF-*.json` (`case.verdict`, `case.fraud_probability`, `case.pattern`, `next_best_actions.final`, `sar.file`, `tool_calls`, `tokens`, `latency_s`); `runs/hhf-final-2/run_manifest.json`; `runs/hhf-final-2/HHF-*/calls.jsonl`; `runs/hhf-final-2/provenance.json`.

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

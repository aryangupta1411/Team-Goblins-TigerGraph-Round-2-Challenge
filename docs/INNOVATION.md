# Innovation claim: Final Round (HHGOA 2026)

## The one idea we want judged

**Act only where the graph shows coordination; everywhere else, verify instead of block.** Cross-card detectors D1-D4 find January's undocumented waves that share no device. Only a detector match or a customer denial lets the agent block. A single-card telemetry oddity counts as one weak signal under R1, so the agent verifies first. We backtest every detector over all 5,585 closed cases and name the legitimate customers it would act against.

## Where it changes the outcome

The "Before" column describes earlier code that is no longer on disk as output, so it carries no numbers. Every "After" number is re-measured from the file or command named in its row.

| Change | Before | After | Source |
|---|---|---|---|
| **Single-card shapes no longer push p into the block band.** D5 (in-person clone) and D6 (new-mobile burst) were removed from `scan_card`. | HHF-008 and HHF-009 were fraud with `BLOCK_CARD` | Both legitimate (p 0.01 and 0.01): `CREATE_CASE, ALLOW_TRANSACTION, CLOSE_NO_FRAUD`, all auto. HHF-008's evidence is the cardholder's own known IE 11 desktop (6 prior uses), a card seen in 40 regions, and a cleared travel alert of the same shape (CC-0270). | `cases/HHF-008.json`, `cases/HHF-009.json` |
| **Investigate the card the transaction is on** (`engine/case_card.py`) | HHF-011 judged on the pack's card C09241-K1, the thin one, because an empty `card6` shifts the `-K` number | Judged on C09241-K2 (6,864 transactions): legitimate (p 0.1), every evidence item on -K2 | `cases/HHF-011.json` |
| **D4 rarity needs history** (data must start ≥ 60 days before `as_of`; ≤ 40 cards all-time) | D4 matched cleared closed cases from the data's first weeks | D4: 1 cleared closed case (CC-3407, customer C03470). It still catches HHF-006 and 5 confirmed-fraud closed cases. | `engine/out/detector_backtest.json` |
| **Injection defence generalised** (NFKC + zero-width folding, sentence-level removal, chat-template and role markup, reworded suppression, amount spoofing; stored notes and tool rows scrubbed too) | Exact-phrase patterns that matched the six planted phrasings | 23 / 23 attacks blocked (6 planted in the pack, 17 synthetic). 0 false alarms on the 12 HHF triggers that carry no injection. | `agent.security.run_attack_ablation()`; `sanitize_trigger_text` over the 18 `cp_round2` triggers in `data/hhgoa_engine.duckdb` |
| **Approvals need an authenticated session** | The approver was a dropdown | scrypt passphrase directory. Only a server-minted session can decide, and SQLite triggers refuse edits to decided approvals. 4 of the 23 attacks are approval drills, all refused by the server: L2 by a team lead, a role claimed by the client, a session re-labelled outside `authenticate()`, and an overwrite of a decided row. | `run_attack_ablation()` (ATK-08, ATK-22, ATK-23, ATK-09) |
| **The policy gate admits impact actions only when a rule requires them** (`engine/policy.py`, `IMPACT_ACTIONS` = `BLOCK_CARD`, `BLOCK_ALL_CARDS`, `DECLINE_TRANSACTION`, `FILE_REPORT`) | The model could add any action that was not forbidden | The model can add only low-impact optional actions. In `runs/hhf-final` it added two, both auto: `MONITOR_CONNECTED_CARDS` on HHF-013 (initial and final sets) and `ALLOW_TRANSACTION` on HHF-015 (initial set). No block, decline or report appears beyond what the engine requires. | `engine/policy.py`; `runs/hhf-final/HHF-*/answer.json` vs `engine.run_cases` |

## Detector backtest (all 5,585 closed cases, July 2016 to January 2017)

| Detector | Cards | of which January | Confirmed-fraud closed cases matched | Cleared (legitimate) closed cases matched | Exam cases caught |
|---|---:|---:|---:|---:|---|
| D1 Spoofed-mobile anonymous-proxy wave (no shared device) | 14 | 14 | 0 | 0 | HHF-001, HHF-002 |
| D2 Dormant-wake escalating drain | 6 | 6 | 0 | 0 | HHF-003 |
| D3 Sub-threshold micro-charge skimming | 31 | 24 | 0 | 0 | HHF-004, HHF-005 |
| D4 One recipient email domain paid by many cards | 23 | 17 | 5 | 1 | HHF-006 |
| R0 Anonymous-proxy shared device ring (Round 1) | 52 | 8 | 5 | 0 | HHG-014 |
| R0 Under-$500 40-minute burst (Round 1) | 29 | 0 | 6 | 0 | HHG-006 |

Source: `engine/out/detector_backtest.json` (`n_closed_cases` 5,585 = 4,675 confirmed fraud + 910 cleared; per detector `cards_matched`, `cards_matched_january`, `closed_cases_caught`, `cleared_cases_matched`, `exam_cases_caught`). Rebuild: `uv run python -m engine.backtest_detectors`.

D1-D3 match no closed case at all. The shapes are new in January, so the closed cases cannot confirm them. What the closed cases can show is that the detectors do not fire on any of the bank's 910 cleared cases, and the backtest shows exactly that.

## Containment (targeted block set vs a broad rule)

| Wave | Cards in the plan | Wave exposure | Settled before the case opened | Legitimate customers the plan touches | Extra customers a broad rule would act on |
|---|---:|---:|---:|---:|---:|
| D1 (HHF-001) | 14 | $18,205.28 | $4,835.96 | 0 | 55 |
| D2 (HHF-003) | 6 | $25,602.59 | $25,602.59 | 0 | 3 |
| D3 (HHF-004) | 17 | $278.70 | $243.10 | 0 | 91 |
| D4 (HHF-006) | 17 | $7,848.29 | $7,612.44 | 0 | 26 |
| Device ring (HHG-014, Round 1) | 52 | $18,192.55 | $11,465.15 | 0 | 953 |

Source: `engine/out/containment_summary.json` (`cards`, `exposure`, `pre_as_of`, `legit_touched_targeted`, `broad_rule_extra_customers`).

## The January typology scan in the graph (D1-D4 port)

The waves share no device, so no vertex exists to traverse from. `graph/queries/typology_scan.gsql` runs D1-D4 as filtered scans over `Transaction`, with per-card accumulators, in the same shapes as `engine.detectors.WAVE_SQL`. It is harness-only (the model never sees it). `agent/phase_machine.py` (`_ensure_facts`) calls it for every case opened on or after 2017-01-01, and turns its rows into the detector hit with `engine.detectors.hit_from_scan`. That is the same function the DuckDB twin (`DuckFacts.typology_scan`) feeds, so the graph and DuckDB versions share one output shape. If the scan cannot run, the case raises `CaseDataMissing` and no answer is written. `graph/check_typology_parity.py` compares the graph against the twin case by case, sending the query as an interpreted, read-only query.

Where it stands:

- **Installed on the workspace, and used by the submitted run.** In `runs/hhf-final`, the harness called `typology_scan` through MCP once per case: 18 calls, all `ok` (`runs/hhf-final/HHF-*/calls.jsonl`). The six wave cases took their D1-D4 evidence from the graph.
- **Parity with the DuckDB twin.** `graph/check_typology_parity.py --installed` covers 30 cases (the 18 HHF cases plus 12 other January cards): 0 differences and 6 hits (D1 HHF-001 and 002, D2 HHF-003, D3 HHF-004 and 005, D4 HHF-006). Source: `engine/out/typology_parity.json`, written by `uv run python graph/check_typology_parity.py --installed --json engine/out/typology_parity.json`.
- **The backtest above runs on DuckDB** (`engine.backtest_detectors` over the twin), and so do mock mode and `engine.run_cases`.

## The 18 answers

7 fraud: the four waves (HHF-001 … HHF-006, `undocumented`) plus HHF-007's denied card-testing run. 3 uncertain with escalation (HHF-010, HHF-013, HHF-014). 8 legitimate after customer validation. SARs are filed on the six multi-card undocumented waves, as §3a requires. This includes the $23.99 (HHF-004) and $23.17 (HHF-005) micro-skimming cases.

They come from one live run against FraudGraph, `runs/hhf-final/`, at frozen commit `b397c2c` with no local changes. All 141 model calls went to `gemini-2.5-flash`, and every case was written back to the graph. `make validate` reports 0 problems on all 38 answer files.

The run took two invocations. HHF-005 failed closed in the main run, after Gemini returned degenerate P5 output three times, and no answer was written for it. Its write-back was removed, and a `--resume` re-ran HHF-005 and then HHF-003 in `opened_at` order. The first HHF-003 attempt is superseded and kept. Sources: `runs/hhf-final/provenance.json` (`failed_attempt`), `runs/hhf-final/_failed_attempt/`, `run_manifest.json` (both invocations). Per case:

| Per case | Median | p95 |
|---|---:|---:|
| Tool calls | 19.5 | 24 |
| Tokens | 211,265 | 331,456 |
| Latency | 92.1 s | 186.4 s |

Sources: `runs/hhf-final/HHF-*/answer.json` (`tool_calls`, `tokens`, `latency_s`; p95 is nearest-rank, which is the maximum of 18 values); `runs/hhf-final/HHF-*/calls.jsonl` (`kind=llm`, `model`); `runs/hhf-final/run_manifest.json`.

The deterministic engine (`engine.run_cases`, re-run offline on the frozen code) gives the same verdict, pattern and SAR decision on all 18 cases, with probability within 0.10. The final action set with routes matches on 17. HHF-013 has the extra auto `MONITOR_CONNECTED_CARDS`, and HHF-015's initial set has the extra auto `ALLOW_TRANSACTION`.

## Reproduce

```bash
uv run python -m engine.backtest_detectors
```

```bash
uv run python -c "from agent.security import run_attack_ablation as r; x=r(); print(x['attacks_blocked'], '/', x['total_attacks'])"
```

```bash
uv run python -m engine.run_cases --cases HHF-001 HHF-002 HHF-003 HHF-004 HHF-005 HHF-006 HHF-007 HHF-008 HHF-009 HHF-010 HHF-011 HHF-012 HHF-013 HHF-014 HHF-015 HHF-016 HHF-017 HHF-018 --out runs/engine-check --sheet runs/engine-check/sheet.md
```

## What is not claimed

- **Challenge 2 is partial.** The three candidate detectors in `self_growing_proposals` (AUTO-DET-02, -03, -04 in `engine/out/detector_backtest.json`) are scans we wrote. What is real is the measured backtest and the approval gate (`approve_detector`, fraud manager only). The agent did not invent the shapes.
- **No accuracy against the answer key.** The organisers hold it.
- **The Round 1 scorer was not retrained.** It scores each of the 18 flagged January transactions at 0.093 or below (max `cms_p` over `cp_round2` joined to `txc` in `data/hhgoa_engine.duckdb`), so the engine treats it as one weak family.

# Agentic Fraud Investigation on TigerGraph — Hacker House Goa 2026

An agent that takes an alert from the HHGOA case pack, investigates it through **TigerGraph** (GSQL query library + graph algorithms, exposed as **TigerGraph MCP** tools), grounds itself with **GraphRAG** over closed cases and FinCEN/FFIEC/FATF guidance (TigerVector), decides under the written **Fraud Policy** with a deterministic policy gate, asks for evidence when the policy says so, explains itself, and **writes its case back into the graph** so later investigations retrieve it. Streamlit UI with an approval inbox.

- **Submission index** (frozen commit, answers, clean-clone command, resilience, innovation): [`SUBMISSION.md`](SUBMISSION.md)
- **Live demo**, connected to the live FraudGraph workspace: https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app

## Final Round (HHF-001 … HHF-018)

| Deliverable | Where |
|---|---|
| Everything the brief asks for, in one place | [`SUBMISSION.md`](SUBMISSION.md) |
| 18 answer files | [`cases/HHF-001.json`](cases/) … `HHF-018.json`, per-case table in [`cases/MANIFEST.md`](cases/MANIFEST.md) |
| The run they come from | `runs/hhf-final-3/` (`answer.json`, `calls.jsonl`, `phases.jsonl`, `sar.md` per case; `run_manifest.json`, `provenance.json`) |
| Innovation claim, with measurements | [`docs/INNOVATION.md`](docs/INNOVATION.md) |
| One-page resilience statement | [`docs/RESILIENCE.md`](docs/RESILIENCE.md) |
| 5-minute judged demo | [`docs/demo_script.md`](docs/demo_script.md) |
| January delta: build, gates, load, undo | [`docs/DELTA.md`](docs/DELTA.md) (`make delta`, `make load-delta`) |

### Clean-clone drill: one command, one case

No keys, no graph, no dataset download. The 18 cases and the base + January facts are in the tracked
`data/hhgoa_engine.duckdb`, and the ids the validator checks are in `data/ids.duckdb`:

```bash
git clone https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge.git Team-Goblins && cd Team-Goblins && make install && RUN_MODE=mock uv run python -m agent.bench --cases HHF-004 --run-id drill-1 --mock && uv run python -m agent.validator --db data/ids.duckdb runs/drill-1/HHF-004/answer.json
```

Run on 2026-11-01 from a fresh clone of `main` (`40ad6ae`): 31 s including `make install` with a warm uv cache, 0
validator problems. The answer lands in `runs/drill-1/HHF-004/`. In mock mode the model replies are recorded fixtures and the facts come from
DuckDB (including the DuckDB twin of the January typology scan). CI runs the same drill on Ubuntu (step "Final Round
clean-clone drill" in `.github/workflows/ci.yml`).

All 18 against the live graph (`.env`: `TG_HOST`, `TG_SECRET`; set `LLM_BACKEND` explicitly; `gemini` needs `GEMINI_API_KEY`; `MODEL=gemini-3.6-flash` is the model of the submitted run, the code's default is `gemini-2.5-flash`):

```bash
RUN_MODE=live LLM_BACKEND=gemini MODEL=gemini-3.6-flash uv run python -m agent.bench --cases round2 --run-id hhf-rerun
```

On the current code, every January case calls the installed `typology_scan` query on the workspace. If that call fails,
the case stops with `CaseDataMissing` and writes no answer (see [the typology scan](#the-january-typology-scan-runs-in-the-graph) below).

### Results of the promoted run `runs/hhf-final-3`

The 18 files in `cases/HHF-*.json` are byte-identical to `runs/hhf-final-3/HHF-*/answer.json`. They come from one live run
against FraudGraph through MCP on `gemini-3.6-flash`, from commit `c2bcb29` (tag `hhf-final-3-run`) in a clean detached worktree
(`git_dirty: false`): one invocation, 2026-11-01 23:42:21 to 2026-11-02 00:01:37, 18 / 18 completed, 0 failed, nothing
resumed. Every tracked file (747) was hashed before and after the run, and the two lists are identical
(`runs/hhf-final-3/provenance.json`, `code.list_sha256`; `runs/hhf-final-3/_guard/`).

It supersedes `runs/hhf-final-2` (commit `8dc9e07`), which superseded `runs/hhf-final` (commit `b397c2c`,
`gemini-2.5-flash`); both are kept for the record. The engine change from `hhf-final` to `hhf-final-2` follows the raw
January data on four cases: HHF-008 legitimate → uncertain, HHF-010 and HHF-014 uncertain → legitimate, HHF-013
uncertain → fraud. From `hhf-final-2` to `hhf-final-3` the verdicts are unchanged; HHF-008's final set drops
`DECLINE_TRANSACTION` (the R4 decline waits on a real reply), and `sar.md` is now the cited filing. Before the run, the
156 + 3 `hhf-final-2` write-backs were deleted from the graph (`provenance.json`, `graph_before_run`), so the graph holds
only this run's HHF write-backs.

| Measure | Value |
|---|---|
| Completed | 18 / 18, every case `written_to_graph: true` |
| Verdicts | 8 fraud, 1 uncertain, 9 legitimate |
| Fraud | HHF-001 … HHF-006 and HHF-013 (pattern `undocumented`: the January waves that share no device; HHF-013 is the `sc.rr.com` recipient-email mule ring, payments 3660311 and 3660312, $321.75, D4) and HHF-007 (`card_testing`, the customer denied it; no SAR) |
| Uncertain | HHF-008 (`out_of_region_use`, p 0.30, status `escalated`; no reply to the customer-validation request) |
| Legitimate | HHF-009, 010, 011, 012, 014, 015, 016, 017, 018, all `closed_legitimate` |
| Status | 8 `escalated`, 1 `closed_fraud`, 9 `closed_legitimate` |
| SAR filed | 7: HHF-001 … HHF-006 and HHF-013, all `undocumented`. This includes the $23.99 (HHF-004) and $23.17 (HHF-005) micro-skimming cases. |
| Evidence requested | 10 of 18 cases, all `customer_validation` (replies simulated and labelled `ASSUMED`) |
| Final actions by route | 54 auto, 8 L1, 8 L2 |
| Exposure (sum of `exposure_usd`) | $15,057.50 |
| Blocks or reports on a non-fraud case | 0. No `BLOCK_*`, `DECLINE_TRANSACTION` or `FILE_REPORT` on the 9 legitimate cases or on the uncertain HHF-008, whose final set is `CREATE_CASE`, `VERIFY_WITH_CUSTOMER`, `MONITOR_CARD`, `ESCALATE_TO_ANALYST`. |
| Validator | 0 problems |
| Official grader (`grader/grade.py`, sha256 b27e39ce…44b3) | 18 / 18 clean: 0 hard, 0 policy, 1 warning (HHF-013 `flagged_not_affected`: the flagged in-person purchase is genuine; the ring payments are the affected transactions) |
| Cost per case | p50 $0.10, p95 $0.18, $1.96 for the 18 cases (Gemini paid-tier prices read 2026-11-01; `engine/out/cost_latency.json`, `ops/cost_report.py`) |

Sources: `runs/hhf-final-3/HHF-*/answer.json` (verdict, status, pattern, `sar.file`, `evidence_requests`, `next_best_actions`, `exposure_usd`, `written_to_graph`); byte comparison with `cases/HHF-*.json`; `runs/hhf-final-3/provenance.json` (`stats`, `grader`); `python3 grade.py --teams-dir <dir> --case-pack case_pack_final.csv --transactions transactions_2017q1.csv` (report in `runs/hhf-final-3/_guard/grade_report.json`).

| Per case | Median | p95 | Min | Max | Total |
|---|---:|---:|---:|---:|---:|
| Tool calls | 21 | 25 | 17 | 25 | 369 |
| Tokens | 202,270 | 318,654 | 150,773 | 318,654 | 3,907,730 |
| Latency (s) | 62.7 | 84.2 | 47.9 | 84.2 | 1,151.7 |

Sources: the `tool_calls`, `tokens` and `latency_s` fields of the 18 `runs/hhf-final-3/HHF-*/answer.json`. p95 is nearest-rank: with 18 values it is the 18th, which is also the maximum.

| Model and graph activity | Value |
|---|---|
| Model calls (`kind=llm`) | 142, all `gemini-3.6-flash`, 0 failed (`llm_backend` `gemini`; `run_manifest.json` names `gemini-3.6-flash` as `model` and `sar_model`) |
| Tool rows (`kind=tool`) | 641, 0 failed: 303 harness queries, 66 queries the model chose, 272 write-back calls |
| `typology_scan` | 18 calls, one per case, made by the harness through MCP, all `ok` |
| Write-back calls (`caller=persist`, 272, all `ok`) | 18 `AgentCase` upserts, 18 `open_case`, 124 `append_case_event`, 16 `record_approval` (one per L1/L2 final action), 96 `add_edges` (`CASE_*` / `HAS_*`) |
| In the graph afterwards | 18 `AgentCase` with `run_id` `hhf-final-3`, 121 HHF `CaseEvent`s, 16 HHF `Approval`s |

Sources: `runs/hhf-final-3/HHF-*/calls.jsonl` (fields `kind`, `model`, `caller`, `name`, `ok`); `runs/hhf-final-3/run_manifest.json`. The graph counts are `graph_at_end` in `runs/hhf-final-3/provenance.json` (read-only listing). An answer's `tool_calls` counts the harness and model rows (303 + 66 = 369), not the write-backs.

**Agreement with the deterministic engine.** `engine.run_cases`, re-run offline at `c2bcb29`, gives the same verdict,
pattern, SAR decision and final action set with routes as `runs/hhf-final-3` on all 18 cases, and a probability within
0.10 on all 18. This shows self-consistency, not accuracy: the organisers hold the answer key.

Source: `uv run python -m engine.run_cases --cases HHF-001 … HHF-018 --out <dir> --sheet <dir>/sheet.md` in a checkout of `c2bcb29`, with each `<dir>/HHF-*.draft.json` compared to `runs/hhf-final-3/HHF-*/answer.json`.

**Live workspace.** FraudGraph holds July 2016 to January 2017. `uv run python -m ops.check_live_counts` compares the
live vertex and edge counts with `expected_after` in `data/out_delta/delta_manifest.json`, which covers 18 types
(Transaction 650,938; ClosedCase 5,585). `make snapshot` (`ops/graph_snapshot.py`) records the live vertex and edge
counts with a timestamp in `logs/graph_snapshots.jsonl`, and diffs them against the last snapshot. Both are read-only.

### The January typology scan runs in the graph

The January waves share no device, so a device traversal cannot find them. Detectors D1-D4 are filtered scans over
`Transaction` with per-card accumulators. They live in `graph/queries/typology_scan.gsql`, a harness-only query (the
model never sees it), installed on the workspace. The harness calls it from `agent/phase_machine.py` (`_ensure_facts`)
for every case opened in 2017 or later. In `runs/hhf-final-3` it ran once per case, 18 times, all `ok`. If the scan cannot
run, the case raises `CaseDataMissing` and no answer is written.

The DuckDB twin (`DuckFacts.typology_scan` / `engine.detectors.scan_rows`) serves mock mode, the offline engine and the
backtest. `graph/check_typology_parity.py --installed` compares the installed query with the twin case by case: 30 cases
(the 18 HHF cases plus 12 other January cards), 0 differences, 7 hits (D1 HHF-001 and 002, D2 HHF-003, D3 HHF-004 and 005,
D4 HHF-006 and HHF-013).

Source: `engine/out/typology_parity.json` (30 rows; every `ok` true, every `diffs` empty), written by `uv run python graph/check_typology_parity.py --installed --json engine/out/typology_parity.json`.

### The analyst dashboard

Run `make ui` to open http://localhost:8501. With no workspace, use `RUN_MODE=mock make ui`. By default the dashboard
reads the run that `cases/` was promoted from. Every page has a context bar showing:
- the source and whether it is LIVE or MOCK;
- the models seen in that run's `calls.jsonl`;
- the git sha, and whether there are uncommitted changes;
- how many of the 18 cases are decided;
- who is signed in.

The navigation is `PAGES` in `ui/common.py`, rendered with `st.navigation`. A page whose file is not on disk is left
out (`nav_pages()`).

| Where | Page | What it shows |
|---|---|---|
| Top bar | **Overview** (default, `/`) | Source provenance and measured tiles: decided n / 18; the verdict mix next to the brief's 9 legitimate; hard actions on non-fraud verdicts; agreement with the submitted files. Then proof cards and the 18-case decision board, where one click opens a case. |
| Top bar | **Case file** (`/case?case=HHF-0xx`) | The decision card comes first: verdict, how p moved, actions with routes and their real approval state, the simulated evidence reply, the stop rule and the SAR decision. Below it: the untrusted trigger verbatim with the defences that fired, then "TigerGraph in this case" (reads, write-backs, vector-search precedents with distances). Then five tabs: Decision path P0–P10, Evidence ledger (what / evidence / how / when per claim), SAR & citations, Approvals & handover, Raw files. |
| Top bar | **Graph** | The case subgraph. In mock mode it is rebuilt from the answer and labelled that way. |
| Top bar | **Approvals** | The inbox, with sign-in through a server-side approver directory (`ui/principals.json`, or `APPROVER_DIRECTORY_JSON` on a host). Sign-in fails closed when there is no directory. Also who may decide what, the wrong-role drill in a sandbox, the immutable audit log and, for an admin, the list of principals (names and roles, never hashes). |
| Proof | **Detectors & containment** | The backtest over 5,585 closed cases, with the cleared customers each detector would act against. Self-growing detector proposals awaiting approval, containment per wave, and a ring replay slider. |
| Proof | **Security & drills** | The 23-attack ablation (`agent.security.run_attack_ablation()`), a hostile-input sandbox and the drills ledger |
| Proof | **Data quality & schema** | Data quality (C11), schema drift (C12) and mapping another bank's schema (C13; mapping only, no investigation run on a variant), from `engine/out/proof_snapshot.json` and `data/schema_approvals.json` |
| Proof | **Runs & consistency** | p50 / p95 per case, numbers to quote, same-case agreement across runs |
| System | **Run console** | Starts agent runs after a confirm dialog, with a live S0–P10 stage strip and replay. Per session: Mock or Live, and the `.env` workspace or another one checked with a read-only probe (its secret stays in session memory). Cases come from the case pack, an uploaded alerts CSV or a form. "Bring your own data" builds a month with `etl.delta` and loads it only into another workspace, never FraudGraph. With `DEPLOY_READONLY=1` it replays recorded cases and starts nothing. |
| System | **How it works** | The TigerGraph-use checklist, known limits and the system map |
| System | **Live architecture** | One alert's path through the system, played back step by step from a real case's trace |

### Who may do what

Sign-in is a scrypt passphrase checked against the server-side directory; the role comes from the directory, never from
the request. `ui/approvals_db.py` (`ROLE_PERMISSIONS`) is the single source:

| Role | May |
|---|---|
| analyst | record feedback on a card |
| team lead | feedback, approve L1 actions, approve the schema migration, start live runs |
| fraud manager | everything a team lead may, plus approve L2 actions (`BLOCK_ALL_CARDS`, `FILE_REPORT`, large blocks) and admit detectors |
| admin | every permission, plus connecting another workspace, loading own data and seeing the principals |

On a deployment with `RUNS_NEED_APPROVER=1` (the Docker image's default), starting a live run needs `run_live`, and
another workspace or own data needs an admin.

Add one approver (it prompts for a passphrase of at least 12 characters):

```bash
uv run python -m ui.approvals_db add-principal you@team --role fraud_manager
```

Or create one demo account per role, with random passphrases written to the git-ignored `demo_accounts.local.txt`
(mode 0600, never printed):

```bash
uv run python -m ui.approvals_db seed-demo
```

## Round 1 (HHG-001 … HHG-020)

| Deliverable | Where |
|---|---|
| 20 answer files | [`cases/`](cases/) (`HHG-001.json` … `HHG-020.json`, [`cases/MANIFEST.md`](cases/MANIFEST.md)) |
| Demo video (3–5 min) | `<DEMO_URL>` (its script is in git history; [`docs/demo_script.md`](docs/demo_script.md) now holds the Final Round demo) |
| Technical blog | [`docs/blog.md`](docs/blog.md) (published copy: `<BLOG_URL>`) |
| Social post (tags @TigerGraphDB and @247pmstudio) | [`docs/social.md`](docs/social.md) (posted copy: `<SOCIAL_URL>`) |
| Architecture diagram | [`docs/architecture.svg`](docs/architecture.svg) |

## Judge quick-start, Round 1 (mock mode: no TigerGraph workspace, no API keys)

Everything below runs offline on a fresh clone. The dataset CSVs under `data/raw/` are not in the
repository (they are gitignored), so the checked-in `data/ids.duckdb` (id tables for the validator) and
`data/hhgoa_engine.duckdb` (the engine's facts) stand in for the graph. You need
[uv](https://docs.astral.sh/uv/) and Python 3.12. Tested on macOS; CI runs the same steps on Ubuntu.

Clone the repository:

```bash
git clone https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge.git Team-Goblins && cd Team-Goblins
```

Install the dependencies and create `.env` from `.env.example` (`RUN_MODE=mock`, no keys needed):

```bash
make install
```

Check every answer file in `cases/` (20 Round 1 + 18 Final Round = 38; all 38 clean, 0 problems) against the README
answer schema and the dataset ids:

```bash
make validate
```

Run one case end to end in mock mode (recorded model replies, DuckDB facts instead of the graph). The
one-row case pack fixture replaces `data/raw/case_pack.csv`; the answer lands in `runs/judge/HHG-014/answer.json`:

```bash
CASE_PACK_CSV=tests/fixtures/case_pack_HHG-014.csv uv run python -m agent.bench --cases HHG-014 --run-id judge --mock
```

Build a mock run from the 20 answer files so the dashboard has something to show:

```bash
make mock-run
```

Open the analyst dashboard at http://localhost:8501. The sidebar's run picker opens on the canonical Final Round run
(`runs/hhf-final-3`) when it is present; pick `mock` there to see the run you just built:

```bash
RUN_MODE=mock make ui
```

The unit tests and the linter (data-backed tests skip without the full DuckDB):

```bash
make test lint
```

## Round 1 results (HHG-001 … HHG-020)

These are the Round 1 numbers, not the Final Round ones. The 20 files `cases/HHG-*.json` are byte-identical to
`runs/live-final-2/HHG-*/answer.json`. That run is one live pass against TigerGraph through MCP, from commit
`be03533` with no local changes (`git_dirty: false`), with all 100 model calls on `claude-sonnet-5` (subscription
backend `cli`). [`cases/MANIFEST.md`](cases/MANIFEST.md) has the per-case rows. The ordered rerun commands are in
[`docs/RERUN.md`](docs/RERUN.md).

| Measure (Round 1) | Value |
|---|---|
| Verdicts | 8 fraud, 7 legitimate, 5 uncertain |
| SAR required | 2: HHG-006 (under-$500 burst) and HHG-014 (anonymous-proxy device ring), both `undocumented` |
| Evidence requested | 14 of 20 cases (9 customer validation, 5 step-up; replies simulated and labelled `ASSUMED`) |
| Final actions by route | 46 auto, 18 L1, 2 L2 |
| Tool calls per case | median 19.5 (15-24) |
| Tokens per case | median 294,301 (232,932-616,283) |
| Time per case | median 162.6 s (112.8-252.6 s) |
| Written back to the graph | 20 / 20 `written_to_graph`; 20 `AgentCase` upserts, 130 `append_case_event`, 20 `record_approval` calls, all `ok` |

Sources: `cases/HHG-*.json`; `runs/live-final-2/run_manifest.json` (git SHA, dirty flag, model); `runs/live-final-2/HHG-*/calls.jsonl` (`kind=llm` model names, `caller=persist` rows).

All 20 answers agree with the engine's own offline prediction (`engine/out/<case>.draft.json`) on verdict, pattern,
episode transactions, exposure, SAR decision, status and both action sets with their routes, and on probability
within 0.10. That is a self-consistency check, not accuracy. Against real outcomes, a replay of 310 labelled closed
cases scores, on the confirmed-fraud ones, pattern accuracy 0.878, SAR decision accuracy 0.939 and episode Jaccard
0.792. All three are below the targets of 0.95, 1.00 and 0.85. [`docs/blog.md`](docs/blog.md) walks through HHG-014 and the lessons.

Sources: `engine/out/HHG-*.draft.json` compared field by field with `cases/HHG-*.json`; `engine/out/replay_metrics.json` (`n`, `pattern_accuracy_fraud_all`, `sar_accuracy_fraud_all`, `episode_jaccard_fraud_all`).

## The two documents

| Document | What it is | Read it when |
|---|---|---|
| [PLAN.md](PLAN.md) | The build plan: what the task asks for, what the data really contains, architecture and stack decisions with their reasons, the decision logic, the 14-day schedule, risks, per-case expectations. | Before anything: it explains *why*. |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | The implementation specification: every schema definition, loading job, GSQL query, engine module, agent phase, retrieval script, UI page and ops script in full, with the output of running them against the real data, plus a 44-step build order with acceptance checks. | While building: it is the *how*. |

Both are also published as pages: [plan](https://claude.ai/artifact/WobuxhoP2dZd1bZYn9vG4C) · [implementation spec](https://claude.ai/artifact/D5phbGun9znULNQuR51AAb) (private to the repository owner until shared).

`IMPLEMENTATION.md` embeds every source file verbatim, so the document and this tree are the same thing:

List every file the document carries:

```bash
python extract_code.py --dry-run
```

Write them (existing files kept unless `--force`). `IMPLEMENTATION.md` is the Round 1 snapshot: it has none of the Final Round code, so `--force` would overwrite it with Round 1 versions. Do not use `--force` on this branch.

```bash
python extract_code.py
```

Write edited files back into the document:

```bash
python sync_spec.py
```

## Architecture

The diagram shows the Final Round pipeline: Gemini (`LLM_BACKEND=gemini`) in phases P0–P10, TigerGraph MCP, and the
harness-only `typology_scan`. It is also at [`architecture/architecture.svg`](architecture/architecture.svg), with a
walk-through in [`architecture/README.md`](architecture/README.md) and an interactive page,
[`architecture/index.html`](architecture/index.html), that opens in any browser. The text sketch under the diagram is the
Round 1 build (`LLM_BACKEND=cli`).

![architecture](docs/architecture.svg)

```
case_pack.csv ─► agent/bench.py ─► agent/phase_machine.py (P0–P10; LLM = claude-agent-sdk, LLM_BACKEND=cli)
                                      │  16 typed read-only tools (14 GSQL query wrappers + 2 retrieval; 12 LLM calls/case) via
                                      │  mcp/tg_mcp_launcher.py → tigergraph-mcp 1.0.3 (stdio)  ──HTTPS──► Savanna TG-00 (4.2.x)
                                      ▼                                                                    FraudGraph + TigerVector
                        engine/ (scorecard · pattern · episode · SAR rule · policy gate · VoI · simulator)
                                      ▼
                        agent/persist (open_case / append_case_event / record_approval + add_edges via MCP; AgentCase + note_emb via pyTigerGraph)
                                      ▼
                  cases/HHG-0xx.json · runs/<run_id>/<case>/{calls,phases}.jsonl · ui/ (Streamlit)
offline once:  etl/ (DuckDB, card_id rule, baselines, LightGBM scorer) → engine/facts_from_etl.py → graph/load_all.py → graph/run_algos.py → rag/ (local embeddings) → graph/install_all.py
```

## Start here

Two commands answer "what can I run?" and "what is already built?". Both are instant, offline, and print
nothing but plain text when piped.

```bash
make help
```

Every target, grouped by build stage, with its description wrapped to your terminal width:

```
== make ============================================================================================
     agentic fraud investigation on TigerGraph - 39 targets, in build order

Start here
  help              this list, grouped by build stage (the default target)
  status            what exists and what does not: data, models, chunks, vectors, cases, backends.
                    Offline; never prints a secret

ETL, scorer, facts (day 1)
  db                data/raw/*.csv -> data/hhgoa.duckdb (tx 590,742 / idn 144,432 / cc 5,565 / cp
                    20; card rule 14,975/14,975)
  ...
```

```bash
make status
```

A dashboard of this checkout: which backends are selected, and which artefact each stage has
produced. It reads only the filesystem — no network, no TigerGraph, and secrets are reported as
`set` / `not set`, never printed.

```
artefact                    state    path                                detail
--------------------------  -------  ----------------------------------  ------
etl duckdb                  ready    data/hhgoa.duckdb                   356M
engine facts db             ready    data/hhgoa_engine.duckdb            50M
graph chunks                ready    data/out/txn_*.csv                  8 files, 324M
embeddings                  ready    data/out/vec_*.psv                  2 files, 53M
answer files                missing  cases/HHG-*.json                    -> make bench promote RUN_ID=v1

WARN 1 artefact(s) missing
     next: make bench promote RUN_ID=v1
```

Every command in this repository prints through one terminal layer (`ops/console.py`): a run header
with the config that decides the outcome, aligned tables instead of Python list reprs, and a summary
block with the totals. On a terminal it is coloured; piped, redirected or in CI it degrades to plain
ASCII with **zero escape bytes**, so `make sheet | cat` stays greppable. `NO_COLOR=1` and
`HHG_PLAIN=1` force plain (useful for a clean demo capture), `FORCE_COLOR=1` forces colour.

Mistyped or pasted a trailing `# comment` after a make command? Make no longer dies with
`No rule to make target '#'` — it names the closest real target, or tells you it was a pasted comment
and carries on.

## Setup (macOS Apple Silicon, Python 3.12 via uv)

```bash
git clone https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge.git Team-Goblins && cd Team-Goblins
```

```bash
make install                 # uv sync --extra dev; creates .env from .env.example
```

```bash
# fill .env: TG_HOST, TG_SECRET (Savanna Database Secret)
# the model needs NO key: LLM_BACKEND=cli (the default) runs the agent on claude-agent-sdk, which drives
#   the Claude Code CLI it bundles and authenticates with your Claude subscription. It sees ONLY our typed
#   read queries (in-process SDK MCP server), no built-in tools, setting_sources=[] so your own settings
#   and CLAUDE.md never leak in. LLM_BACKEND=api switches back to the anthropic SDK and is the only case
#   that needs ANTHROPIC_API_KEY.
# embeddings need NO key: EMBED_BACKEND=local (the default) runs BAAI/bge-large-en-v1.5 (1024-d, MIT)
#   through sentence-transformers on this machine — one 1.34 GB download to ~/.cache/huggingface, then
#   no network. Lighter: EMBED_MODEL=BAAI/bge-small-en-v1.5 EMBED_DIM=384 (133 MB).
#   EMBED_BACKEND=voyage switches back to the paid Voyage API and is the only case that needs VOYAGE_API_KEY.
# put the four dataset CSVs and the dataset README under data/raw/  (transactions.csv identity.csv closed_cases_history.csv case_pack.csv README.md)
```

Day 0 (accounts, Savanna workspace, smoke tests): follow [`docs/day0_checklist.md`](docs/day0_checklist.md).
Full build order with acceptance checks: [`docs/BUILD_ORDER.md`](docs/BUILD_ORDER.md).

## Build the graph

Run `make status` between any two of these to see what the previous one produced.

DuckDB → features → scorer → engine facts DB → TSV chunks → `ids.duckdb` (day 1):

```bash
make db features cms facts chunks ids
```

Schema + loading jobs + 590,742 transactions; WCC / degree once (day 2, 4):

```bash
make load algos
```

The 25 installed queries (14 LLM-exposed reads + 4 writers + 7 harness-only: `grounding_chunks`, 4 RAG, `post_open_activity` and the UI's `case_subgraph`; the model sees 16 typed tools, the 14 plus `find_similar_cases` and `grounding_chunks`) and their MCP descriptions (day 3–4):

```bash
make install-queries describe
```

Deterministic engine: the expectation sheet (`qa/engine_expectation_sheet.md`) + 20 LLM-free drafts, printed as one aligned table of 20 cases with a summary of SARs filed and total exposure (day 5–6):

```bash
make sheet engine-drafts
```

GraphRAG corpus, local embeddings (no API key), TigerVector loads (day 7):

```bash
make corpus embed vectors
```

## Run the 20 cases

Wake the workspace (auto-resume drops the first request) — prints the TigerGraph version:

```bash
make awake
```

Optional: ping every 3 min while the benchmark runs, one `OK` line per ping:

```bash
make keepalive &
```

All 20 in `opened_at` order → `runs/v1/<case>/{answer.json,calls.jsonl,phases.jsonl,sar.md}`:

```bash
make bench RUN_ID=v1
```

Validator (ids resolved against `data/ids.duckdb`) → `cases/`:

```bash
make promote RUN_ID=v1
```

The analyst dashboard at http://localhost:8501 (pages: [The analyst dashboard](#the-analyst-dashboard)). Its source
defaults to the run `cases/` was promoted from, and every page reads that one source:

```bash
make ui
```

Without a workspace or API keys, render the UI from `cases/` (or the sample HHG-014 answer) and replay recorded tool traces. `make mock-run` prints a table of every case it replayed, with the phase and call counts it synthesised:

```bash
RUN_MODE=mock make mock-run ui
```

Without a workspace but **with** the real model (subscription, still no keys) — the facts come from DuckDB, the decisions from Claude:

```bash
uv run python -m agent.bench --run-id cli1 --cases HHG-014,HHG-010 --mock --llm-backend cli
```

```bash
uv run python -m agent.validator --db data/ids.duckdb runs/cli1/*/answer.json
```

`--mock` picks the fact source (DuckDB instead of TigerGraph); `--llm-backend` picks the model
(`cli` = claude-agent-sdk, `api` = anthropic SDK, `mock` = recorded fixtures). They are independent.

## Checks that need nothing but this checkout

The unit suite (data-backed tests skip when the DuckDB files are absent):

```bash
make test
```

`ruff check .`:

```bash
make lint
```

Static GSQL lint — every `alias.attribute` in `graph/queries/*.gsql` resolves against `graph/schema.gsql`,
brackets balance, and `mcp/query_descriptions.yaml` has one entry per query. Prints a row per query file
and exits 1 on the first problem:

```bash
make lint-gsql
```

Contract lint — GSQL parameter lists vs `mcp/query_descriptions.yaml` vs `engine.facts_duckdb.DuckFacts`
vs `agent.mcp_client.BUILTIN_QUERIES`, as one coverage table (`ok` / `X` / `-` per module):

```bash
make lint-contracts
```

Rebuild the id-only validator DuckDB (`data/ids.duckdb`, checked in) and verify every row count:

```bash
make ids
```

## Deploy on Railway

Live at https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app, deployed from this repository's
`main`. The whole app runs on [Railway](https://railway.com) as one container: the analyst dashboard and everything a live
agent run needs. The root `Dockerfile` installs torch from the PyTorch CPU index, then `requirements-live.txt` (the
`uv.lock` pins without the CUDA wheels, generated by the command in its header). It bakes the `BAAI/bge-large-en-v1.5`
embedding model into the image and ships:
- `agent/`, `engine/`, `etl/`, `graph/`, `mcp/`, `ops/`, `policy/`, `rag/`, `schema_engine/`, `contracts/` and `ui/`;
- the checked-in part of `data/`, including the facts snapshot `data/hhgoa_engine.duckdb` and the validator's
  `data/ids.duckdb`, but no raw CSVs, work DBs, `.env` or approver directory (`.dockerignore`);
- `cases/`, the Final Round run named by `ARG CANONICAL_RUN` (`hhf-final-3`), the earliest superseded run (`runs/hhf-final/`) and `runs/live-final-2/` (Round 1).

`tests/unit/test_ui_railway_smoke.py` renders every page from exactly the Dockerfile's file set. Without variables
the image starts in mock mode and read-only: it replays the recorded runs, needs no keys and touches no graph.

1. Sign in at railway.com with GitHub, then choose **New Project → Deploy from GitHub repo**, pick this repository
   and the branch `main`. If Railway asks, grant its GitHub app access to the repository.
2. Under **Settings → Build**, set the builder to **Dockerfile**. Under **Settings → Deploy**, set the healthcheck path
   to `/_stcore/health` and leave **Custom Start Command** empty. `railway.json` has the same settings, but Railway
   no longer reads config files for new services.
3. Under **Settings → Networking**, choose **Generate Domain** with Railway's `PORT` (8080) as the target port.
4. If **Auto deploy** says it cannot load branches, pushes do not redeploy: press Cmd+K and run **Deploy Latest Commit**.
5. Do not accept the variables Railway suggests from `.env.example`.

### Live runs from the deployment

The Run console (Investigate on a case, or the queue's Run console) starts `agent.bench` in the container: the agent
reads the graph through the MCP launcher, calls Gemini and writes its AgentCase, CaseEvents and Approvals back. On a
public URL that spends tokens and writes to the shared graph, so the image sets `RUNS_NEED_APPROVER=1`: only a
signed-in team lead, fraud manager or admin can start a live run ([who may do what](#who-may-do-what)).

1. Attach a volume at `/data` (**right-click the service → Attach volume**), so runs and decisions survive redeploys.
2. Create the approver directory locally, then copy its content into a sealed variable:
   `uv run python -m ui.approvals_db add-principal you@bank --role fraud_manager --directory /tmp/principals.json`
   (it asks for the passphrase), then `cat /tmp/principals.json` into `APPROVER_DIRECTORY_JSON`. It holds scrypt hashes,
   not passphrases.
3. Add the variables below, the secrets as sealed variables, and redeploy.

| Variable | Value for live runs | Image default | Effect |
|---|---|---|---|
| `RUN_MODE` | `live` | `mock` | Pages and runs use the live graph (needs `TG_HOST`). |
| `DEPLOY_READONLY` | `0` | `1` | `1` replays recorded traces and never runs the agent or writes to the graph, even in live mode. |
| `LLM_BACKEND` | `gemini` | unset | The model backend for runs. |
| `MODEL` | `gemini-3.6-flash` | unset (`gemini-2.5-flash`) | The model, as in the submitted run. |
| `AGENT_CASE_NAMESPACE` | `web` | unset | The deployment writes `AC-web-<case>` (and `AP-AC-web-…` approvals), never the submitted run's `AC-<case>`. Case memory excludes both forms of the case's own id. |
| `TG_HOST`, `TG_GRAPHNAME`, `TG_SECRET` | the workspace URL, `FraudGraph`, the secret | unset | Sealed. Any visitor who opens a live page wakes, and bills, the workspace. |
| `GEMINI_API_KEY` | the key | unset | Sealed. |
| `APPROVER_DIRECTORY_JSON` | the directory file's content | unset | Who can sign in, approve and start runs. Without it nobody can (fail closed). |
| `RUNS_DIR` | `/data/runs` | `/app/runs` | On the volume; the image's canonical runs are copied there at start, without overwriting. |
| `APPROVALS_DB` | `/data/approvals.sqlite` | `/tmp/approvals.sqlite` | The approval inbox every visitor shares. It must be absolute and writable. |
| `RAILWAY_RUN_UID` | `0` | unset | Railway volumes are root-owned and the image runs as a non-root user. |
| `RUNS_NEED_APPROVER` | leave unset | `1` | `0` would let any visitor start live runs. |
| `PORT` | set by Railway | `8501` | The port Streamlit listens on. |

If the page stays on "Please wait...", set `STREAMLIT_BROWSER_SERVER_ADDRESS` to the service's `*.up.railway.app`
domain, so Streamlit accepts the browser's websocket origin. Keep CORS and XSRF protection on.

To try the image locally:

```bash
docker build -t hhgoa-app . && docker run --rm -p 8501:8501 hhgoa-app
```

## Guarantees about the answer files

- **Every id exists in the dataset.** The validator resolves every `TransactionID`, card id, customer id, `CC-` id and device-profile string against DuckDB before a file is promoted; an agent-written `AC-` id never appears in `entity_ids` (it is only in `graph_case_id`).
- `card_id` is derived by the rule that reproduces 100 % of the 14,975 labelled (transaction → card) pairs (`customer_id-K<dense_rank(card6)>`), tested in CI.
- Evidence is time-boxed to `ts <= opened_at`; post-open activity is a monitoring note, never in `affected_txn_ids` / `exposure_usd`.
- Routes and rules come from `policy/fraud_policy.yaml`; `auto` actions execute, `L1`/`L2` land in the approval inbox; `sar.file ⇔ FILE_REPORT ∈ final`; `verdict == fraud` ⇒ a block / decline / escalation in `final` and never `CLOSE_NO_FRAUD`; SAR narratives pass the single validator `rag/validate_sar.py`.
- Each case is written to the graph as `AgentCase AC-HHG-0nn` with `CaseEvent`s, `Approval`s and `CASE_SIMILAR_TO` edges; cases run in `opened_at` order so a later case can retrieve an earlier one (strictly `opened_at < as_of`, so never itself); in the final run none did, and every precedent was a closed case.
- `tool_calls`, `tokens`, `latency_s` come from the run log (`runs/<run_id>/<case>/calls.jsonl`); prompts are frozen and hashed into `run_manifest.json`.

## Repository map

`etl/` DuckDB + features + scorer (owns `data/out/*.csv` incl. `fraud_pattern.csv`) · `graph/` schema, loading jobs, 25 queries, algorithms, loaders · `mcp/` launcher, tool lists, query descriptions · `rag/` corpus, chunks, embeddings, vector loads, SAR generator + validator · `engine/` deterministic decision engine, facts DB builder, replay, drafts · `policy/` fraud_policy.yaml · `agent/` phase machine, prompts, schemas, MCP client, persist, validator, bench · `qa/` GSQL lint + cross-module contract lint · `ui/` Streamlit · `ops/` `console.py` (the one terminal-output layer every CLI prints through), ensure_awake, keep_alive, id export, mock runs · `docs/` checklists, demo script, blog · `tests/` unit + live.

## Data and licensing

IEEE-CIS Fraud Detection (Vesta) via the HHGOA re-cut. Only the HHGOA re-cut is used, and the answer validator (`engine/validator.py`) rejects any answer whose free text names an outside copy of the data or the outcome label (`isFraud`). Regulatory chunks carry url + sha256 + page provenance; full FATF/FFIEC documents are not vendored.

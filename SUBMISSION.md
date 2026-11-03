# Submission: Team Goblins, HHGOA 2026 Final Round

An agent that investigates fraud alerts on TigerGraph: it reads the case's graph neighbourhood through TigerGraph MCP,
runs the January typology scan in the graph, retrieves similar closed cases with TigerVector, decides under the written
Fraud Policy with a deterministic engine and policy gate, and writes its case back into the graph. Everything the brief
asks for is listed here, each with where to find it and how it was checked.

| Brief asks for | Where | Checked |
|---|---|---|
| Frozen commit | The final pushed commit of `main`, tagged [`round2-freeze`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/tree/round2-freeze); its hash is the one we hand over at the freeze. The answers were produced at [`05bc21d`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/commit/05bc21de1e5ad0f3f81e75d515131f036377efff), tagged [`hhf-final-3-run`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/tree/hhf-final-3-run), and the decision code is unchanged between the two ([below](#what-changed-between-the-run-and-the-freeze)) | `runs/hhf-final-3/run_manifest.json` records `c2bcb29` (`05bc21d` since the history rewrite, see [Commit hashes](#commit-hashes)) with `git_dirty: false` for the run's one invocation and all 18 cases, run from a clean detached worktree; `engine.run_cases` at the freeze reproduces all 18 answers |
| 18 answers produced by the frozen code | [`cases/HHF-001.json`](cases/HHF-001.json) … `HHF-018.json`, per-case table in [`cases/MANIFEST.md`](cases/MANIFEST.md) | byte-identical to `runs/hhf-final-3/HHF-*/answer.json`; validator: 0 problems; the organisers' `grade.py` (sha256 b27e39ce…44b3): 18 / 18 clean, 0 hard, 0 policy, 1 warning |
| The run behind them | [`runs/hhf-final-3/`](runs/hhf-final-3/): `answer.json`, `calls.jsonl`, `phases.jsonl` and `sar.md` per case, `run_manifest.json`, `provenance.json`, `_guard/` | one live run on `gemini-3.6-flash` against FraudGraph through MCP, 18 / 18 with no failure; tracked-file fingerprints (747 files) identical before and after (`provenance.json`, `code.list_sha256`); identical to the offline engine at the same commit |
| One clean-clone, single-case command | [below](#clean-clone-one-command-one-case) | run on 2026-11-01 from a fresh clone of this repository (`main` at `ac248db`): 31 s, 0 validator problems; CI runs it on Ubuntu |
| One-page resilience statement | [`docs/RESILIENCE.md`](docs/RESILIENCE.md) | |
| Innovation claim | [`docs/INNOVATION.md`](docs/INNOVATION.md) | every number names the file or command it comes from |
| Live demo | https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app | [below](#live-deployment) |
| 5-minute demo script | [`docs/demo_script.md`](docs/demo_script.md) | |
| Architecture | [`architecture/README.md`](architecture/README.md), [`architecture/architecture.svg`](architecture/architecture.svg) | |


## Commit hashes

On 2026-11-02 this repository's history was rewritten to remove a co-author line from commit messages. Files, authors
and dates are unchanged; every commit hash changed (tags moved with them). Run records keep the hashes they were written
with: `c2bcb29` in `runs/hhf-final-3/run_manifest.json` is `05bc21d` here (tag `hhf-final-3-run`), and hashing every
tracked file at `05bc21d` reproduces the run's recorded fingerprint `77ecb484…`. Every old → new hash:
[`docs/COMMIT_MAP.md`](docs/COMMIT_MAP.md).

## Clean clone: one command, one case

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12. No keys, no graph and no dataset download: the 18 cases and the
base + January facts are in the tracked `data/hhgoa_engine.duckdb`, and the validator's ids in `data/ids.duckdb`.

```bash
git clone https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge.git Team-Goblins && cd Team-Goblins && make install && RUN_MODE=mock uv run python -m agent.bench --cases HHF-004 --run-id drill-1 --mock && uv run python -m agent.validator --db data/ids.duckdb runs/drill-1/HHF-004/answer.json
```

The answer lands in `runs/drill-1/HHF-004/` (`answer.json`, `calls.jsonl`, `phases.jsonl`, `sar.md`). In mock mode the
model replies are recorded fixtures and the facts come from DuckDB, including the DuckDB twin of the January typology
scan. The same case against the live graph and Gemini needs `TG_HOST`, `TG_SECRET` and `GEMINI_API_KEY` in `.env`:

```bash
RUN_MODE=live LLM_BACKEND=gemini MODEL=gemini-3.6-flash uv run python -m agent.bench --cases HHF-004 --run-id live-drill-1
```

## Results of the submitted run

| Measure | Value |
|---|---|
| Completed | 18 / 18 in one invocation (2026-11-01 23:42:21 to 2026-11-02 00:01:37), 0 failed, nothing resumed; every case `written_to_graph: true` |
| Verdicts | 8 fraud (HHF-001 … 007, HHF-013), 1 uncertain (HHF-008, p 0.30), 9 legitimate |
| SARs filed | 7, on the January rings that share no device (HHF-001 … 006, HHF-013); HHF-007 (`card_testing`) has none. HHF-013 is the `sc.rr.com` recipient-email mule ring (payments 3660311 and 3660312, $321.75) |
| Block, decline or report on a non-fraud case | 0. The uncertain HHF-008's final set is `CREATE_CASE`, `VERIFY_WITH_CUSTOMER`, `MONITOR_CARD`, `ESCALATE_TO_ANALYST`: the R4 decline waits on a real reply |
| Final actions by route | 54 auto, 8 L1, 8 L2 |
| `typology_scan` in the graph | 18 calls through MCP, one per case, all `ok`; 0 differences from the DuckDB twin over 30 cases (7 hits) |
| Graph write-backs | 272 calls, all `ok`: 18 `AgentCase`, 121 `CaseEvent`s, 16 `Approval`s with `run_id` `hhf-final-3` |
| Model | 142 calls, all `gemini-3.6-flash`, 0 failed |
| Per case | latency median 62.7 s (p95 84.2 s), tokens median 202,270 (p95 318,654; 3,907,730 in all), tool calls median 21 (p95 25) |
| Cost per case | p50 $0.10, p95 $0.18, $1.96 for 18 cases (Gemini paid-tier prices read 2026-11-01; `engine/out/cost_latency.json`) |

This run supersedes `runs/hhf-final-2` (commit `0010621`): the verdicts are the same, HHF-008 no longer carries
`DECLINE_TRANSACTION`, and `sar.md` is the cited filing. `hhf-final-2` superseded the earliest run, `runs/hhf-final`
(commit `6945275`, `gemini-2.5-flash`), kept in the repository; the engine change between those two moved four verdicts:
HHF-008 legitimate → uncertain, HHF-010 and HHF-014 uncertain → legitimate, HHF-013 uncertain → fraud. Before this run
the `hhf-final-2` write-backs were deleted from the graph. Every figure here is sourced in
[`README.md`](README.md#results-of-the-promoted-run-runshhf-final-3).

## Live deployment

https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app runs this repository's `main` on Railway,
connected to the live FraudGraph workspace.

- **Anyone can browse, without signing in.** The 18-case board, each case file with its decision path, evidence
  ledger and SAR from the submitted run, the detector backtest, the security ablation and the drills. The Graph page
  draws each case's subgraph from the live workspace (`case_subgraph`, read-only).
- **Starting a run or deciding an approval needs a sign-in.** Live runs spend model tokens and write to the shared
  graph, so the Run console starts one only for a signed-in team lead, fraud manager or admin (`RUNS_NEED_APPROVER=1`).
  Accounts are scrypt hashes in a sealed Railway variable (`APPROVER_DIRECTORY_JSON`); passphrases are never
  committed and are available from the team on request.
- **Runs from the deployment never touch the submitted results.** It writes `AC-web-<case>` (`AGENT_CASE_NAMESPACE=web`),
  never the submitted run's `AC-<case>`, and its runs and approvals live on a volume.

Deploy instructions and every variable: [`README.md`](README.md#deploy-on-railway).

## What the frozen commit contains

The answers were produced at `05bc21d`. Since the previous run (`hhf-final-2`, `0010621`) the answer path gained:

- `agent/sar_doc.py` and `agent/phase_machine.py` (`_write_sar_md`): `sar.md` is a fileable filing with every
  sentence cited (59 / 59), a timeline and a Mermaid evidence graph (Challenge 4). The answer's `sar` object is unchanged.
- `engine/policy_compiler.py`: R5 / R7 policy prose compiled to detectors, with a backtest and rule-vs-agent comparison
  (Challenge 3, `engine/out/policy_detector_R5.json`).
- `engine/discovery.py`: a discovery miner that files a pending detector proposal (Challenge 2). Not called by a case
  investigation.
- `engine/scorecard.py`, `engine/feedback.py`, `policy/fraud_policy.yaml`: an uncertain case's L1 decline waits on a
  real reply (R4), and analyst feedback is read by the engine, with before/after in
  `engine/out/feedback_before_after.json` (Challenge 8).
- `engine/containment.py`: the smallest block set for a wave (Challenge 7).
- `ops/cost_report.py` and `ops/alert_watcher.py`: cost per case and an alert watcher (Challenge 9).
- `agent/lifecycle.py`: `close_case` on a final approval or customer reply, and a hand-over page (Challenge 14).
- `etl/delta*.py`: the loader refuses January until the schema migration is signed; the signature is still pending
  (Challenge 12).
- `schema_engine/mapper.py`, `profiler.py` and `ops/variant_rehearsal.py`: three variants mapped at 100% precision and
  recall, with 18 / 18 decisions matching (Challenge 13).

## What changed between the run and the freeze

The freeze (`round2-freeze`) is the final pushed commit, so all the work above and the commits after the run count.
Between `05bc21d` and the freeze:

- `09426c4`: the graph page redesign (icon nodes in semantic colours, the case layer in live mode, a full-screen view)
  and a light theme on every page;
- `f4fa85b`: the proof board reads the measured evidence (Challenges 2, 3, 8, 9, 12, 13), and cost per case on Gemini 3.6
  Flash prices (`agent/pricing.py`, `ops/cost_report.py`, `engine/out/cost_latency.json`);
- `10dcac5`: Challenge 13 in the dashboard (upload or generate a variant, the attested mapping, run on variant, accept or
  reject the mapping);
- `cc33926` and later: the promotion of `hhf-final-3` and documentation.

On the answer path (`agent/`, `engine/`, `graph/`, `mcp/`, `rag/`, `policy/`, `etl/`, `schema_engine/`) the only code
change is `agent/pricing.py`, which only the cost report and the Runs page import; `agent.bench` never reads it. The
rest is regenerated outputs under `engine/out/`. Checked at the freeze: `engine.run_cases` gives the same verdict,
pattern, SAR decision and final actions with routes as `cases/HHF-*.json` on all 18 cases, probability within 0.10, as
it does at `05bc21d`. `git diff hhf-final-3-run round2-freeze -- agent/ engine/ graph/ mcp/ rag/ policy/ etl/ schema_engine/`
shows every change.

## Changes after the freeze (the post-freeze merge)

Merged on top of `round2-freeze`. None of it changes an answer: `cases/HHF-*.json` and `runs/hhf-final-3` are untouched.

- **Dashboard.** The Challenge 2 case-catch demo is built from the detector backtest, not typed ids; the Challenge 8
  feedback loop is in the UI; the Challenge 9 alert-watcher panel is on the Runs page; the Challenge 14 hand-over page can
  approve pending actions and close the case; Challenge 13 infers a mapping for any unseen `.gsql` schema.
- **Resilience statement.** It now also declares Google Fonts (the viewer's browser), the corpus download and the Hugging
  Face model fetch (build time only), Gemini's data terms, and the optional notification webhook below.
- **Approvals.** The January schema migration and four detectors are recorded as approved by the `manager@demo` demo
  account. All five decisions carry one timestamp, so they were recorded in a single scripted run; they are a demo
  account's signatures, not a named person's. This is what makes Challenges 2 and 12 read "built" on the proof board.
- **Optional notifications** (`ops/notify.py`, the Notifications page). Off and unconfigured by default: nothing is sent
  unless an operator sets `NOTIFY_ENABLED`, an https `NOTIFY_SERVICE_URL` and a `NOTIFY_API_KEY`, and registers a
  recipient. There is no built-in endpoint, key or number. `agent.bench` and the investigation path never call it (the
  clean-clone drill and the submitted run make no outbound call for it). Messages carry the case id, the verdict word and
  the probability, never a report flag, action, pattern, customer or card id, or error text. Changing recipients needs the
  `manage_notifications` permission (fraud manager, admin). See `docs/RESILIENCE.md`.

Files under the answer-path folders (`agent/`, `engine/`, `graph/`, `mcp/`, `rag/`, `policy/`, `etl/`, `schema_engine/`)
that differ from `round2-freeze`, apart from regenerated outputs in `engine/out/`:

| File | What changed | On the decision path? |
|---|---|---|
| `engine/discovery.py` | builds the "which cases this detector catches" demo from the backtest's own rows | No: only the dashboard and `engine/out/discovery_jan2017.json` use it |
| `engine/feedback.py` | the `--demo` command restores `APPROVALS_DB` and `ENGINE_FEEDBACK` after it runs | No: inside `demo()`; the scorecard's read of feedback is unchanged |
| `schema_engine/ai_analyst.py` | infers a mapping for an unseen schema | No: not imported by the runner, the scorecard or the policy gate |

Checked on the merged tree: `agent.bench`, `engine.scorecard`, `engine.run_cases`, `engine.policy` and
`agent.phase_machine` import none of `engine.discovery`, `schema_engine.ai_analyst` or `ops.notify`; `agent/bench.py` is
byte-identical to `round2-freeze`; and `engine.run_cases` gives the same verdict, pattern, SAR decision and final actions
with routes as `cases/HHF-*.json` on all 18 cases, probability within 0.10.

# Submission: Team Goblins, HHGOA 2026 Final Round

An agent that investigates fraud alerts on TigerGraph: it reads the case's graph neighbourhood through TigerGraph MCP,
runs the January typology scan in the graph, retrieves similar closed cases with TigerVector, decides under the written
Fraud Policy with a deterministic engine and policy gate, and writes its case back into the graph. Everything the brief
asks for is listed here, each with where to find it and how it was checked.

| Brief asks for | Where | Checked |
|---|---|---|
| Frozen commit | [`8dc9e0723f42bd735e1c4d5b36e8ad09c7e988d0`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/commit/8dc9e0723f42bd735e1c4d5b36e8ad09c7e988d0), tag [`hhf-final-2-run`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/tree/hhf-final-2-run) | `runs/hhf-final-2/run_manifest.json` records this sha with `git_dirty: false` for the run's one invocation and all 18 cases |
| 18 answers produced by the frozen code | [`cases/HHF-001.json`](cases/HHF-001.json) … `HHF-018.json`, per-case table in [`cases/MANIFEST.md`](cases/MANIFEST.md) | byte-identical to `runs/hhf-final-2/HHF-*/answer.json`; `make validate`: 38 files (20 Round 1 + 18 Final Round), 0 problems; the organisers' `grade.py`: 18 / 18 clean, 0 hard, 0 policy |
| The run behind them | [`runs/hhf-final-2/`](runs/hhf-final-2/): `answer.json`, `calls.jsonl`, `phases.jsonl` and `sar.md` per case, `run_manifest.json`, `provenance.json`, `_guard/` | one live run on `gemini-3.6-flash` against FraudGraph through MCP, 18 / 18 with no failure; tracked-file fingerprints (642 files) identical before and after (`provenance.json`, `code.list_sha256`); identical to the offline engine at the same commit |
| One clean-clone, single-case command | [below](#clean-clone-one-command-one-case) | run on 2026-11-01 from a fresh clone of this repository (`main` at `40ad6ae`): 31 s, 0 validator problems; CI runs it on Ubuntu |
| One-page resilience statement | [`docs/RESILIENCE.md`](docs/RESILIENCE.md) | |
| Innovation claim | [`docs/INNOVATION.md`](docs/INNOVATION.md) | every number names the file or command it comes from |
| Live demo | https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app | [below](#live-deployment) |
| 5-minute demo script | [`docs/demo_script.md`](docs/demo_script.md) | |
| Architecture | [`architecture/README.md`](architecture/README.md), [`architecture/architecture.svg`](architecture/architecture.svg) | |

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
| Completed | 18 / 18 in one invocation, 0 failed, nothing resumed; every case `written_to_graph: true` |
| Verdicts | 8 fraud (HHF-001 … 007, HHF-013), 1 uncertain (HHF-008), 9 legitimate |
| SARs filed | 7, on the January rings that share no device (HHF-001 … 006, HHF-013) |
| Block, decline or report on a non-fraud case | 0 blocks or reports; the uncertain HHF-008 carries `DECLINE_TRANSACTION` (L1), the 9 legitimate cases none |
| Final actions by route | 53 auto, 9 L1, 8 L2 |
| `typology_scan` in the graph | 18 calls through MCP, one per case, all `ok`; 0 differences from the DuckDB twin over 30 cases (7 hits) |
| Graph write-backs | 274 calls, all `ok`: 18 `AgentCase`, 121 `CaseEvent`s, 17 `Approval`s with `run_id` `hhf-final-2` |
| Model | 143 calls, all `gemini-3.6-flash`, 0 failed |
| Per case | latency median 67.8 s (p95 105.1 s), tokens median 213,861 (p95 350,878), tool calls median 21 |

This run supersedes `runs/hhf-final` (commit `b397c2c`, `gemini-2.5-flash`), kept in the repository. The engine change
between them moves four verdicts: HHF-008 legitimate → uncertain, HHF-010 and HHF-014 uncertain → legitimate, HHF-013
uncertain → fraud. Every figure here is sourced in [`README.md`](README.md#results-of-the-promoted-run-runshhf-final-2).

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

## What changed after the frozen commit

The answers were produced at `8dc9e07` (tag `hhf-final-2-run`). The commits after it add challenge work, the dashboard,
the deployment and documentation. On the answer path (`agent/`, `engine/`, `graph/`, `mcp/`, `rag/`, `policy/`, `etl/`,
`schema_engine/`) they add or change only:

- `agent/sar_doc.py` and `agent/phase_machine.py` (`_write_sar_md`): `sar.md` is written as a fileable document (cited
  narrative, timeline, evidence graph, attestation). The answer's `sar` object is unchanged.
- `engine/containment.py`: the containment planner ranks wave cards by exposure stopped (Challenge 7).
- `engine/policy_compiler.py`, `engine/discovery.py` and two entry points in `engine/detectors.py`: policy prose to
  detector (Challenge 3) and a mined detector proposal (Challenge 2). Neither is called by a case investigation.
- `engine/scorecard.py`, `engine/feedback.py`, `policy/fraud_policy.yaml` (`3c56196`): an uncertain case's L1 decline
  waits on verification (R4), graph-neighbourhood evidence, and analyst feedback (Challenge 8).
- `agent/` lifecycle (`427bc8f`): a final approval or customer reply closes the case (`close_case` and a `CaseEvent`);
  an alert watcher and cost per case (Challenge 9 and 14).
- `etl/delta*.py`: the January build refuses to run before the schema migration is signed (Challenge 12).
- `schema_engine/mapper.py` and `profiler.py`: the variant mapper (Challenge 13).
- regenerated outputs under `engine/out/`.

Checked on the pushed head (`engine.run_cases`, offline): the same verdict, pattern and SAR decision as `cases/HHF-*.json`
on all 18 cases, probability within 0.10, and the same final actions with routes on 17. The exception is HHF-008
(uncertain): from `3c56196` its final set has `VERIFY_WITH_CUSTOMER` instead of the L1 `DECLINE_TRANSACTION` the
submitted answer carries. At the frozen commit `8dc9e07` the engine matches the submitted answers on all 18. `git diff
8dc9e07 HEAD -- agent/ engine/ graph/ mcp/ rag/ policy/ etl/ schema_engine/` shows every change.

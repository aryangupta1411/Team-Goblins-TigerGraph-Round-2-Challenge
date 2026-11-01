# Submission: Team Goblins, HHGOA 2026 Final Round

An agent that investigates fraud alerts on TigerGraph: it reads the case's graph neighbourhood through TigerGraph MCP,
runs the January typology scan in the graph, retrieves similar closed cases with TigerVector, decides under the written
Fraud Policy with a deterministic engine and policy gate, and writes its case back into the graph. Everything the brief
asks for is listed here, each with where to find it and how it was checked.

| Brief asks for | Where | Checked |
|---|---|---|
| Frozen commit | [`b397c2c97b5bf1b4cded77e951cfd58e5be8c70b`](https://github.com/aryangupta1411/Team-Goblins-TigerGraph-Round-2-Challenge/commit/b397c2c97b5bf1b4cded77e951cfd58e5be8c70b) | `runs/hhf-final/run_manifest.json` records this sha with `git_dirty: false` for both invocations and all 18 cases |
| 18 answers produced by the frozen code | [`cases/HHF-001.json`](cases/HHF-001.json) … `HHF-018.json`, per-case table in [`cases/MANIFEST.md`](cases/MANIFEST.md) | byte-identical to `runs/hhf-final/HHF-*/answer.json`; `make validate`: 38 files (20 Round 1 + 18 Final Round), 0 problems |
| The run behind them | [`runs/hhf-final/`](runs/hhf-final/): `answer.json`, `calls.jsonl`, `phases.jsonl` and `sar.md` per case, `run_manifest.json`, `provenance.json` | one live run on `gemini-2.5-flash` against FraudGraph through MCP; tracked-file fingerprints identical before and after (`provenance.json`, `code.list_sha256`) |
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
RUN_MODE=live LLM_BACKEND=gemini uv run python -m agent.bench --cases HHF-004 --run-id live-drill-1
```

## Results of the submitted run

| Measure | Value |
|---|---|
| Completed | 18 / 18, every case `written_to_graph: true` |
| Verdicts | 7 fraud (HHF-001 … 007), 3 uncertain (HHF-010, 013, 014), 8 legitimate |
| SARs filed | 6, on the four January waves that share no device (HHF-001 … 006) |
| Block, decline or report on a non-fraud case | 0 blocks or reports; the 3 uncertain cases carry `DECLINE_TRANSACTION` (L1), the 8 legitimate cases none |
| Final actions by route | 54 auto, 10 L1, 7 L2 |
| `typology_scan` in the graph | 18 calls through MCP, one per case, all `ok`; 0 differences from the DuckDB twin over 30 cases |
| Graph write-backs | 276 calls, all `ok`: 18 `AgentCase`, 122 `CaseEvent`s, 17 `Approval`s with `run_id` `hhf-final` |
| Model | 141 calls, all `gemini-2.5-flash`, 0 failed |

HHF-005 failed closed once in P5 (Gemini's structured output was cut off three times) and was re-run with `--resume`;
the story is in [`docs/RESILIENCE.md`](docs/RESILIENCE.md#what-happened-in-the-submitted-run). Every figure here is
sourced in [`README.md`](README.md#results-of-the-promoted-run-runshhf-final).

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

The answers were produced at `b397c2c`; the commits after it add the dashboard, the deployment and documentation. On
the answer path (`agent/`, `engine/`, `graph/`, `mcp/`, `rag/`, `policy/`, prompts) they change only:

- `agent/case_ids.py` and two lines of `agent/phase_machine.py`: the write-back namespace. With `AGENT_CASE_NAMESPACE`
  unset, as in the submitted run, the ids are `AC-<case>` exactly as before.
- `agent/bench.py`: a Round 1 case pack on disk no longer hides the Final Round cases.
- `engine/detectors.py`: who may admit a new detector is read from the role permissions. The scan and its decisions
  are unchanged.

`git diff b397c2c HEAD -- agent/ engine/ graph/ mcp/ rag/ policy/` shows exactly these, plus the regenerated
`engine/out/proof_snapshot.json` and `engine/out/typology_parity.json`.

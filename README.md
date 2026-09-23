# Agentic Fraud Investigation on TigerGraph — Hacker House Goa 2026

An agent that takes an alert from the HHGOA case pack, investigates it through **TigerGraph** (GSQL query library + graph algorithms, exposed as **TigerGraph MCP** tools), grounds itself with **GraphRAG** over closed cases and FinCEN/FFIEC/FATF guidance (TigerVector), decides under the written **Fraud Policy** with a deterministic policy gate, asks for evidence when the policy says so, explains itself, and **writes its case back into the graph** so later investigations retrieve it. Streamlit UI with an approval inbox.

| Deliverable | Where |
|---|---|
| 20 answer files | [`cases/`](cases/) (`HHG-001.json` … `HHG-020.json`, [`cases/MANIFEST.md`](cases/MANIFEST.md)) |
| Demo video (3–5 min) | **TODO link** |
| Technical blog | **TODO link** (outline: [`docs/blog_outline.md`](docs/blog_outline.md)) |
| Social post (tags @TigerGraphDB) | **TODO link** ([`docs/social.md`](docs/social.md)) |
| Optional monitoring cases | [`cases_monitoring/`](cases_monitoring/) |

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

Write them (existing files kept unless `--force`):

```bash
python extract_code.py
```

Write edited files back into the document:

```bash
python sync_spec.py
```

## Architecture

![architecture](docs/architecture.svg)

```
case_pack.csv ─► agent/bench.py ─► agent/phase_machine.py (Anthropic SDK, P0–P10)
                                      │  12 typed read-only tools = installed GSQL queries via
                                      │  mcp/tg_mcp_launcher.py → tigergraph-mcp 1.0.3 (stdio)  ──HTTPS──► Savanna TG-00 (4.2.x)
                                      ▼                                                                    FraudGraph + TigerVector
                        engine/ (scorecard · pattern · episode · SAR rule · policy gate · VoI · simulator)
                                      ▼
                        agent/persist (open_case / append_case_event / close_case via MCP writers; AgentCase.note_emb)
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
git clone <repo> && cd <repo>
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

The 25 installed queries (16 LLM-exposed reads + 4 writers + 5 harness-only: `grounding_chunks` + 4 RAG) and their MCP descriptions (day 3–4):

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

The analyst dashboard at http://localhost:8501 — queue · case · approvals · graph · runs:

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

## Guarantees about the answer files

- **Every id exists in the dataset.** The validator resolves every `TransactionID`, card id, customer id, `CC-` id and device-profile string against DuckDB before a file is promoted; an agent-written `AC-` id never appears in `entity_ids` (it is only in `graph_case_id`).
- `card_id` is derived by the rule that reproduces 100 % of the 14,975 labelled (transaction → card) pairs (`customer_id-K<dense_rank(card6)>`), tested in CI.
- Evidence is time-boxed to `ts <= opened_at`; post-open activity is a monitoring note, never in `affected_txn_ids` / `exposure_usd`.
- Routes and rules come from `policy/fraud_policy.yaml`; `auto` actions execute, `L1`/`L2` land in the approval inbox; `sar.file ⇔ FILE_REPORT ∈ final`; `verdict == fraud` ⇒ a block / decline / escalation in `final` and never `CLOSE_NO_FRAUD`; SAR narratives pass the single validator `rag/validate_sar.py`.
- Each case is written to the graph as `AgentCase AC-HHG-0nn` with `CaseEvent`s, `Approval`s and `CASE_SIMILAR_TO` edges; cases run in `opened_at` order so later cases retrieve earlier ones.
- `tool_calls`, `tokens`, `latency_s` come from the run log (`runs/<run_id>/<case>/calls.jsonl`); prompts are frozen and hashed into `run_manifest.json`.

## Repository map

`etl/` DuckDB + features + scorer (owns `data/out/*.csv` incl. `fraud_pattern.csv`) · `graph/` schema, loading jobs, 25 queries, algorithms, loaders · `mcp/` launcher, tool lists, query descriptions · `rag/` corpus, chunks, embeddings, vector loads, SAR generator + validator · `engine/` deterministic decision engine, facts DB builder, replay, drafts · `policy/` fraud_policy.yaml · `agent/` phase machine, prompts, schemas, MCP client, persist, validator, bench · `qa/` GSQL lint + cross-module contract lint · `ui/` Streamlit · `ops/` `console.py` (the one terminal-output layer every CLI prints through), ensure_awake, keep_alive, id export, mock runs · `docs/` checklists, demo script, blog · `tests/` unit + live.

## Data and licensing

IEEE-CIS Fraud Detection (Vesta) via the HHGOA re-cut. The public Kaggle files are **never** used (a repo lint fails on Kaggle/IEEE-outcome references). Regulatory chunks carry url + sha256 + page provenance; full FATF/FFIEC documents are not vendored.

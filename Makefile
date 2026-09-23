# HHGOA fraud agent — build pipeline (PLAN §3.2, §6; commands corrected per impl/00-integration.md §5 and
# decisions.md). Every target is idempotent and prints what it did. `make help` lists them. Module entry
# points are variables so a renamed script is a one-line change. Everything runs as `python -m <module>`
# from the repo root except the graph/ scripts, which are plain files.

SHELL := /bin/bash
.DEFAULT_GOAL := help
PY := uv run python
RUN_ID ?= $(shell date +%Y%m%d-%H%M%S)
# CASES: "all" or a comma list: HHG-014,HHG-006
CASES ?= all
# MODEL: empty = agent.bench reads MODEL from .env; override: make bench MODEL=claude-opus-5
MODEL ?=
MODEL_FLAG := $(if $(strip $(MODEL)),--model $(strip $(MODEL)),)
HHGOA_DB ?= data/hhgoa.duckdb
VALIDATOR_DB ?= data/ids.duckdb
# engine facts DB built from the ETL tables (engine/facts_from_etl.py; .env ENGINE_FACTS_DB)
ENGINE_FACTS_DB ?= data/hhgoa_engine.duckdb
# dataset README (fraud policy + patterns) that rag.chunk reads; copy it to data/raw/ on day 1 (.env HHGOA_README)
HHGOA_README ?= data/raw/README.md

# ---- module entry points (etl/ graph/ rag/ engine/ agent/ qa/ ui/ ops/) -------------
# etl/ (module A, impl/10-etl-scorer.md): python -m etl.<name> <db> [flags]
# ETL_BUILD_DB: raw CSVs -> data/hhgoa.duckdb (tx, tx_raw, idn, cc, cp, pairs, cardmap; asserts 14,975/14,975)
ETL_BUILD_DB := etl.build_db
# ETL_FEATURES: txc, txn_feat, burst_member, device_profile, card_feat
ETL_FEATURES := etl.features
# ETL_CLOSED: closed_case_parsed (templates + masked embed_text), closed_case_txn, closed_case_conn
ETL_CLOSED := etl.parse_closed_cases
# ETL_CMS: LightGBM scorer -> cms_p into txn_feat; data/models/{lgb_fraud.txt,isotonic.json,cms_metrics.json,cms_features.json}
ETL_CMS := etl.cms_train
# ETL_EXPORT: data/out/*.csv TSV chunks (headerless, per contracts/csv_columns.yaml) + manifest.json; owns fraud_pattern.csv
ETL_EXPORT := etl.export_graph_csvs
# engine/ (module D): facts DB from the ETL tables, deterministic drafts + expectation sheet, 310-case replay
# ENGINE_FACTS: data/hhgoa.duckdb -> $(ENGINE_FACTS_DB) (the only facts-DB builder, decisions D7)
ENGINE_FACTS := engine.facts_from_etl
# ENGINE_RUN: 20 engine-drafted answer files (no LLM) + qa/engine_expectation_sheet.md
ENGINE_RUN := engine.run_cases
# ENGINE_REPLAY: 310 closed-case replay -> engine/out/replay_metrics.json
ENGINE_REPLAY := engine.replay
# graph/ (modules B/C): plain scripts
# GRAPH_LOAD: DDL + loading jobs + every chunk (timeout=600000, count assertions); --schema-only / --jobs-only / --only <stems>
GRAPH_LOAD := graph/load_all.py
# GRAPH_ALGOS: project_shares_device -> tg_wcc -> wcc_summary -> degree
GRAPH_ALGOS := graph/run_algos.py
# GRAPH_INSTALL: CREATE/INSTALL all queries in graph/queries/*.gsql (one batch)
GRAPH_INSTALL := graph/install_all.py
# GRAPH_DESCRIBE: updateQueryDescription from mcp/query_descriptions.yaml
GRAPH_DESCRIBE := graph/describe_queries.py
# GRAPH_REBUILD: --from <stage>: schema|load|algos|vectors|install
GRAPH_REBUILD := graph/rebuild.py
# rag/ (module F): corpus -> chunks -> embeddings (local sentence-transformers by default) -> TigerVector
# RAG_FETCH: --verify: sha256 + text extraction of the downloaded PDFs; --ofac: sdn/alt csv
RAG_FETCH := rag.fetch_corpus
# RAG_CHUNK: README policy/pattern chunks + regulatory chunks -> data/out/policy_chunk*.{csv,jsonl}, document/chunk_of/about
RAG_CHUNK := rag.chunk
# RAG_CLOSED: closed_case_parsed.embed_text (ETL) -> data/out/closed_case_embed.jsonl
RAG_CLOSED := rag.closedcase_embed_text
# RAG_EMBED: --input <jsonl> --out <psv>: $EMBED_BACKEND (local bge-large-en-v1.5 1024-d by default; no key)
RAG_EMBED := rag.embed
# RAG_VECTORS: --ensure-schema (graph/schema_change_vectors.gsql) / --all / --status
RAG_VECTORS := rag.load_vectors
# agent/ (module E)
# AGENT_BENCH: --cases ... --run-id ... [--model ...] [--mock] [--llm-backend cli|api|mock]
AGENT_BENCH := agent.bench
# AGENT_PROMOTE: --run-id ... -> cases/
AGENT_PROMOTE := agent.promote
# AGENT_VALIDATE: --db $(VALIDATOR_DB) <files> (engine.validator over data/ids.duckdb)
AGENT_VALIDATE := agent.validator

.PHONY: help install env db features cms facts chunks ids load algos install-queries describe \
        corpus embed vectors replay sheet engine-drafts bench bench-one bench-free promote validate ui mock-run \
        test test-data test-live lint fmt keepalive awake rebuild demo-warm clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- day 0 -----------------------------------------------------------------------------
install: ## uv sync (+dev extras); copies .env.example -> .env if missing
	uv sync --extra dev
	@test -f .env || (cp .env.example .env && echo "created .env — fill TG_HOST / TG_SECRET / API keys")

env: ## print the effective config (secrets masked)
	@$(PY) -c "import os; from dotenv import load_dotenv; load_dotenv(); \
	  [print(f'{k}={(v[:6]+\"…\") if k.endswith((\"KEY\",\"SECRET\")) and v else v}') for k,v in sorted(os.environ.items()) if k.startswith(('TG_','RUN_MODE','MODEL','SAR_MODEL','LLM_BACKEND','EMBED','VOYAGE','ANTHROPIC','ENGINE_','HHGOA_'))]"

awake: ## wake the Savanna workspace (retries 502/503 up to 4 min) and print the version
	$(PY) -m ops.keep_alive --once

keepalive: ## ping an installed query every 3 min (benchmark / demo recording); Ctrl-C to stop
	$(PY) -m ops.keep_alive

# ---- day 1: ETL + scorer + facts DB ----------------------------------------------------
db: ## data/raw/*.csv -> data/hhgoa.duckdb (tx 590,742 / idn 144,432 / cc 5,565 / cp 20; card rule 14,975/14,975)
	$(PY) -m $(ETL_BUILD_DB) --raw data/raw --db $(HHGOA_DB)

features: ## txn_feat, device_profile (9,705), card_feat (14,317), burst_member; then the closed-case parser (embed_text)
	$(PY) -m $(ETL_FEATURES) $(HHGOA_DB)
	$(PY) -m $(ETL_CLOSED) $(HHGOA_DB)

cms: ## train the case-memory scorer (Oct holdout AUC >= 0.95) -> txn_feat.cms_p + data/models/ (isotonic.json = the calibration of record)
	$(PY) -m $(ETL_CMS) $(HHGOA_DB) --out data/models

facts: ## engine facts DuckDB from the ETL tables -> $(ENGINE_FACTS_DB) (590,742 / 14,317 / 9,705 / 13,553 / 5,565 / 14,955 / 20)
	$(PY) -m $(ENGINE_FACTS) --etl $(HHGOA_DB) --out $(ENGINE_FACTS_DB)

chunks: ## headerless TSV chunks in data/out/ (txn_000..007 <= 45 MB each), verified against contracts/csv_columns.yaml
	$(PY) -m $(ETL_EXPORT) $(HHGOA_DB) --out data/out --yaml contracts/csv_columns.yaml --verify

ids: ## id-only DuckDB for the validator in CI (data/ids.duckdb, ~20 MB, checked in) + views txc/card_feat/customer_feat/cc
	$(PY) -m ops.export_id_tables --src $(HHGOA_DB) --out $(VALIDATOR_DB)

# ---- day 2-4: graph ----------------------------------------------------------------------
load: ## schema + loading jobs + all chunks in one run (counts asserted: 590,742 / 576,425 / 140,784 / 14,955); `graph/load_all.py --schema-only` for the DDL alone
	$(PY) $(GRAPH_LOAD)

algos: ## project_shares_device -> tg_wcc -> wcc_summary -> degree (timeout 600 s each), results into Card / DeviceProfile attrs
	$(PY) $(GRAPH_ALGOS)

install-queries: ## CREATE + INSTALL every graph/queries/*.gsql in one batch (16 reads + 4 writers + 4 RAG = 24; 1-3 min compile on TG-00)
	$(PY) $(GRAPH_INSTALL)

describe: ## register query descriptions + JSON parameter examples (pyTigerGraph updateQueryDescription)
	$(PY) $(GRAPH_DESCRIBE)

# ---- day 7: GraphRAG ---------------------------------------------------------------------
corpus: ## verify the downloaded corpus, chunk README + regulations, emit the ClosedCase embed text (ETL embed_text)
	$(PY) -m $(RAG_FETCH) --verify
	HHGOA_README=$(HHGOA_README) $(PY) -m $(RAG_CHUNK)
	$(PY) -m $(RAG_CLOSED) --db $(HHGOA_DB) --out data/out

embed: ## embeddings ($EMBED_BACKEND, default local/free) -> data/out/vec_PolicyChunk.psv and vec_ClosedCase.psv (id|f1,f2,...)
	$(PY) -m $(RAG_EMBED) --input data/out/policy_chunk_embed.jsonl --out data/out/vec_PolicyChunk.psv
	$(PY) -m $(RAG_EMBED) --input data/out/closed_case_embed.jsonl --out data/out/vec_ClosedCase.psv

vectors: ## add the VECTOR attributes (graph/schema_change_vectors.gsql, GLOBAL job) if missing, load vec_*.psv, poll /vector/status
	$(PY) -m $(RAG_VECTORS) --ensure-schema
	$(PY) -m $(RAG_VECTORS) --all

# ---- day 5-6: engine QA ------------------------------------------------------------------
replay: ## 310-case deterministic replay -> engine/out/replay_metrics.json (targets: pattern >= 95 %, SAR 100 %, Jaccard >= 0.85)
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_REPLAY)

sheet: ## regenerate qa/engine_expectation_sheet.md (+ engine/out/*.draft.json) from the deterministic engine
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_RUN)

engine-drafts: ## 20 engine-drafted answer files with no LLM (day-6 fallback) -> runs/engine-drafts/<case>.draft.json
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_RUN) --out runs/engine-drafts

# ---- day 8-12: agent ---------------------------------------------------------------------
bench: ## run the agent on CASES (default all 20, opened_at order) -> runs/$(RUN_ID)/
	$(PY) -m $(AGENT_BENCH) --cases $(strip $(CASES)) --run-id $(RUN_ID) $(MODEL_FLAG)

bench-one: ## one case: make bench-one CASE=HHG-014
	$(PY) -m $(AGENT_BENCH) --cases $(CASE) --run-id $(RUN_ID) $(MODEL_FLAG)

bench-free: ## real model (claude-agent-sdk, no API key) over DuckDB facts: make bench-free CASES=HHG-014,HHG-010
	$(PY) -m $(AGENT_BENCH) --cases $(strip $(CASES)) --run-id $(RUN_ID) $(MODEL_FLAG) --mock --llm-backend cli
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/*/answer.json

promote: ## copy runs/$(RUN_ID)/*/answer.json -> cases/ after validation, write cases/MANIFEST.md
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/*/answer.json
	$(PY) -m $(AGENT_PROMOTE) --run-id $(RUN_ID)

validate: ## validate cases/*.json against the README schema + id existence (VALIDATOR_DB)
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) cases/*.json

# ---- UI ----------------------------------------------------------------------------------
ui: ## Streamlit analyst dashboard (queue / case / approvals / graph / runs)
	uv run streamlit run ui/app.py --server.headless true --server.port 8501

mock-run: ## runs/mock/ from cases/*.json (or the sample HHG-014) so the UI works without a graph or keys
	$(PY) -m ops.make_mock_run --answers cases --run-id mock

demo-warm: awake keepalive ## wake the workspace and keep it awake for the recording

# ---- quality -----------------------------------------------------------------------------
test: ## unit tests (no network; data-backed tests skip when data/hhgoa.duckdb / the facts DB are absent)
	uv run pytest tests/unit

test-data: ## the data-backed unit tests, mandatory locally after `make db features cms facts` (fails if the DBs are missing)
	@test -f $(HHGOA_DB) || (echo "missing $(HHGOA_DB): run make db features cms" && exit 1)
	@test -f $(ENGINE_FACTS_DB) || (echo "missing $(ENGINE_FACTS_DB): run make facts" && exit 1)
	HHGOA_DB=$(HHGOA_DB) ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) uv run pytest tests/unit/test_card_rule.py tests/unit/test_features.py tests/unit/test_engine.py -rs

test-live: ## live contract tests against the workspace (needs .env)
	uv run pytest tests/live -m live

lint: ## ruff check
	uv run ruff check .

fmt: ## ruff format + fix
	uv run ruff format . && uv run ruff check --fix .

# ---- disaster recovery ---------------------------------------------------------------------
rebuild: ## rebuild the workspace from checked-in chunks: make rebuild FROM=schema|load|algos|vectors|install
	$(PY) $(GRAPH_REBUILD) --from $(FROM)

clean: ## remove caches (never data/ or runs/)
	rm -rf .pytest_cache .ruff_cache; find . -name __pycache__ -type d -prune -exec rm -rf {} +

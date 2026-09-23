# BUILD ORDER — day by day, with the command, the files and the acceptance check

Derived from PLAN §6 (14 working days, solo, ~6 focused hours/day) and corrected to the commands that exist (`impl/00-integration.md` §5, `decisions.md`). Each step: **command → files → check (expected output)**. A step is done only when its check passes; checks are numbers verified on the real files in DuckDB, so a different number means a bug, not a rounding difference. Module contracts: `contracts/schema.md` (names, derivation rules) and `contracts/interfaces.md` (query outputs, Python signatures, file layout). Every command is `uv run python -m <module>` from the repo root unless it names a `graph/*.py` script; the `make` targets wrap exactly these commands.

Legend: ☐ to do · **NEVER CUT** = items the day-6 fallback still needs (PLAN §6.1).

---

## Day 0 — accounts, workspace, smoke tests, scaffolding
Follow `docs/day0_checklist.md`. Summary of the gates:

| ☐ | Step | Command | Check |
|---|---|---|---|
| ☐ | Deadline + Discord questions (PLAN §8: opened_at bounding, wave-scoped connected cards, NULL rendering of profiles, `graph_case_id` existence) | post on Discord | answers recorded in the checklist |
| ☐ | LLM backend — **no key needed** (`LLM_BACKEND=cli`, the default); `MODEL=claude-sonnet-5` | checklist §1a (`claude_agent_sdk.query` with `output_format`) | `{'ok': 'OK'}` + a usage dict on the `ResultMessage`. Only `LLM_BACKEND=api` needs `ANTHROPIC_API_KEY` (prepaid credit, tier ≥ 1) — checklist §1b. |
| ☐ | Embedding backend — **no key needed** (`EMBED_BACKEND=local`, the default) | checklist §2 | `SentenceTransformer("BAAI/bge-large-en-v1.5").encode_document(["x"], normalize_embeddings=True)` → 1024 floats, unit norm. Only `EMBED_BACKEND=voyage` needs a Voyage key (payment method for Tier 1). |
| ☐ | Savanna TG-00 on newest 4.2.x + Database Secret; `.env` from `.env.example` incl. `ENGINE_DATA_DIR=data`, `ENGINE_FACTS_DB=data/hhgoa_engine.duckdb`, `HHGOA_README=data/raw/README.md`, `TG_LOAD_TIMEOUT_MS=600000` | checklist §3, `make awake` | prints `4.2.x` after the 502 backoff; resume time recorded → `TG_RESUME_MAX_WAIT_S` |
| ☐ | Credit balance, decision rule; auto-stop/auto-start on, maintenance window moved | checklist §4–5 | recorded |
| ☐ | `CREATE VERTEX` smoke for all 13 vertices + 24 edges **with `proxy_type`** (PROXY is reserved) | checklist §8 | no syntax error; whether `proxy` compiles as a tuple field noted |
| ☐ | TigerVector smoke incl. two-type mixed `candidate_set` | checklist §9 | 2 vertices, one per type; else record the `similar_prior_cases_closed` + `_agent` fallback |
| ☐ | 150 MB POST test; TAB separator through `/ddl` | checklist §11 | chunk size (45 MB) and separator decided; fallback `\|#\|` |
| ☐ | `IMPORT PACKAGE GDBMS_ALGO` | checklist §10 | recorded (baseline is plain `tg_*.gsql` anyway) |
| ☐ | Scaffolding: copy `impl/code/{etl,graph,mcp,engine,policy,agent,rag,ops,ui,qa,tests,contracts}` + `impl/code/root/.` into the repo, first CI run | `make install && make test && make lint && uv run python -m qa.check_integration` | 44+ unit tests pass (data-backed tests **skipped** without `data/hhgoa.duckdb` / the facts DB); ruff clean; `check_integration` → `OK`; CI green (validator / e2e steps skipped) |

---

## Day 1 — ETL + scorer + facts DB (`etl/`, `engine/facts_from_etl.py`)  **NEVER CUT: card_rule test**

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Put the four CSVs **and the dataset README** in place | `ls -la data/raw/` | `data/raw/{transactions,identity,closed_cases_history,case_pack}.csv`, `data/raw/README.md` (`HHGOA_README`, read by `rag.chunk`) | sizes ≈ 708 MB / 27 MB / 2.7 MB / 4 KB; README present |
| ☐ | Build DuckDB | `make db` = `python -m etl.build_db --raw data/raw --db data/hhgoa.duckdb` (≈ 20 s) | `etl/build_db.py` → `data/hhgoa.duckdb` (`tx, tx_raw, idn, cc, cp, pairs, cardmap`) | prints `card rule: 14975/14975`; `tx 590,742 · idn 144,432 · cc 5,565 (4,665 confirmed_fraud / 900 cleared) · cp 20` |
| ☐ | Card rule + regression test | `uv run pytest tests/unit/test_card_rule.py` | `etl/card_rule.py` | 3 passed: `14,975/14,975` labelled pairs reproduced; `14,317` cards; 12,793 / 756 / 4 customers with 1 / 2 / 3 cards |
| ☐ | Features + closed-case parser | `make features` = `python -m etl.features data/hhgoa.duckdb` then `python -m etl.parse_closed_cases data/hhgoa.duckdb` | `etl/features.py` → `txc, txn_feat, burst_member, device_profile, card_feat`; `etl/parse_closed_cases.py` → `closed_case_parsed` (with `embed_text`, the one text later embedded into `note_emb`), `closed_case_txn`, `closed_case_conn` | `ring_profile: [(True, 52, 28, 114, 114, 4)]`; `ring_id_exact_profile_cards: 52` (the exact ring profile) while `Card.ring_id` is set on **1,157** cards under the schema rule (`n_fraud_cases ≥ 2 OR proxy share ≥ 0.9`) — the engine's strict ring test, not `ring_id` alone, identifies the ring; `novdec_cards: 12` burst cards (`C00466-K1 C01890-K1 C02265-K1 C03633-K1 C05423-K1 C05766-K1 C05851-K1 C06881-K1 C07297-K1 C10751-K1 C10990-K1 C12641-K2`); HHG-006 ids `3476602/3476633/3476665/3476682` under `C07297-K1#3476602`; `modal_region` for `C13487-K1` = **`272.0`** (264.0 / 299.0 / 325.0 for the three largest cards); `next_ties: 157`; parser templates `4656/716/158/26/5/4`, `involves: 14955`, `connected_to: 92`, no `$n.nn` / `CC-` inside `embed_text`. `uv run pytest tests/unit/test_features.py` → 8 passed |
| ☐ | Scorer (the scorer of record, D7) | `make cms` = `python -m etl.cms_train data/hhgoa.duckdb --out data/models` (< 5 min) | `etl/cms_train.py` → `data/models/{lgb_fraud.txt, isotonic.json, cms_metrics.json, cms_features.json}`, `cms_p` non-null on all 590,742 `txn_feat` rows | October holdout **AUC ≥ 0.95** (reference 0.956; risk score alone 0.866); every month OOF ≥ 0.94; calibration bins printed; HHG-006 flagged `cms_p ≈ 0.02`, HHG-014 `≈ 0.08`, HHG-008 `≈ 0.96` |
| ☐ | Engine facts DB | `make facts` = `python -m engine.facts_from_etl --etl data/hhgoa.duckdb --out data/hhgoa_engine.duckdb` | `engine/facts_from_etl.py` (the only facts-DB builder) → `data/hhgoa_engine.duckdb` | counts `590,742 / 14,317 / 9,705 / 13,553 / 5,565 / 14,955 / 20`; `ENGINE_FACTS_DB=data/hhgoa_engine.duckdb uv run python -m engine.run_cases` → `20 cases, 0 with validator errors` and the sheet equals `qa/engine_expectation_sheet.md` (`engine.calibrate` reads `data/models/isotonic.json`, the ETL calibrator of record) |
| ☐ | Chunks | `make chunks` = `python -m etl.export_graph_csvs data/hhgoa.duckdb --out data/out --yaml contracts/csv_columns.yaml --verify` | `etl/export_graph_csvs.py` → `data/out/*.csv` (headerless, **TAB-separated, unquoted**; every file in `contracts/csv_columns.yaml` incl. `fraud_pattern.csv`, which only the ETL writes), `manifest.json` | row counts: `customer 13,553 · card 14,317 · transaction 590,742 (txn_000..007, ≤ 45 MB each) · device_profile 9,705 · email_domain 60 · billing_region 332 · fraud_pattern 7 · closed_case 5,565 · owns 14,317 · next 576,425 · from_device 140,784 · billed_in 525,003 · involves 14,955 · on_card 5,565 · connected_to 92 · matches 5,565`; every file re-parses with the yaml column count; `NEXT` ties broken by TransactionID (157 same-second pairs) |
| ☐ | Id-only DB for CI | `make ids` = `python -m ops.export_id_tables --src data/hhgoa.duckdb --out data/ids.duckdb` | `ops/export_id_tables.py` → `data/ids.duckdb` (checked in, ≈ 20 MB) | `txn 590,742 · card 14,317 · customer 13,553 · device_profile 9,705 · closed_case 5,565 · case_pack 20` + views `txc / card_feat / customer_feat / cc`; `uv run python -m agent.validator --db data/ids.duckdb runs/engine-drafts/*.draft.json` → `[]` ×20 |
| ☐ | Data-backed tests are green locally | `make test-data` | `tests/unit/test_card_rule.py`, `test_features.py`, `test_engine.py` | all pass with the DBs present (they skip in CI) |

---

## Day 2 — schema and load (`graph/`)  **NEVER CUT: time-boxing, rebuild path**

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Contract lint, then schema DDL (no reserved words: `proxy_type`, `ring_id STRING`, `is_strong BOOL`; vector attrs added later by the GLOBAL job) | `uv run python graph/check_contracts.py` → `uv run python graph/load_all.py --schema-only` | `graph/schema.gsql`, `graph/loading_jobs.gsql`, `graph/load_all.py` | `check_contracts` → `OK`; `conn.getVertexTypes()` lists all 13; `getEdgeTypes()` lists 24 (+ `REV_*`) |
| ☐ | Load everything but vectors | `make load` = `uv run python graph/load_all.py` (schema + jobs + every chunk, `timeout=600000`, `ensure_awake`; the four GraphRAG files are re-encoded from quoted CSV to TSV under `data/out/_enc/`) | `graph/load_all.py` | `getVertexCount`: `Transaction 590,742 · Card 14,317 · Customer 13,553 · DeviceProfile 9,705 · EmailDomain 60 · BillingRegion 332 · ClosedCase 5,565 · FraudPattern 7`; `getEdgeCount`: `MADE 590,742 · FROM_DEVICE 140,784 · PURCHASER_EMAIL 496,262 · RECIPIENT_EMAIL 137,453 · BILLED_IN 525,003 · NEXT 576,425 · OWNS 14,317 · INVOLVES 14,955 · ON_CARD 5,565 · CONNECTED_TO 92 · MATCHES 5,565`; `DeviceProfile("SM-G935F Build/NRD90M \| Android 7.0 \| chrome 62.0 for android \| 1920x1080").is_strong == true`; `getVerticesById("Transaction","3478561")` shows `proxy_type == "IP_PROXY:ANONYMOUS"`, `ring_hit == true` |
| ☐ | Memory chart | Savanna workspace → Monitoring | — | memory **< ~60 %** of 16 GiB (else drop `c_counts/d_deltas/m_flags` from `load_txn`) |
| ☐ | Time the rebuild | `time make rebuild FROM=schema` | `graph/rebuild.py` | minutes recorded in README (disaster-recovery budget) |
| ☐ | Manual backup | Savanna → Backups (1 allowed on Free) | — | backup listed |

---

## Day 3 — query library, first half + hand-written HHG-014  **NEVER CUT: HHG-014 file, MCP timeout launcher**

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Queries `case_context, card_profile, card_window, region_history, device_history, device_neighbors, prior_cases_for_customer` + writers `open_case, append_case_event, close_case, record_approval` | `make install-queries` (one batch, 1–3 min compile) | `graph/queries/*.gsql`, `graph/install_all.py` | 25 × `OK  create …`, `install all` without `Semantic Check Fails` (30-queries §4 fallbacks if a file fails; re-run `install_all.py <name>`) |
| ☐ | Query descriptions | `make describe` | `graph/describe_queries.py`, `mcp/query_descriptions.yaml` (25 entries; `post_open_activity`, `case_subgraph`, `grounding_chunks` and the four RAG queries `harness_only: true` → 14 typed tools reach the model) | 25 × `OK`; `conn.getQueryMetadata("card_profile")` shows the description with the JSON parameter example |
| ☐ | Contract tests + latency | `uv run pytest tests/live -q -k contracts` (expectation files regenerated with `python graph/queries/expected/run_expected.py --rebuild`) | `tests/live/test_query_contracts.py`, `graph/queries/expected/*.json` | 25 passed, every query < 5 s; `card_profile` / `card_window` / `episode_candidates` / `card_testing_check` / `recurring_charge_check` **< 5 s** on `C13440-K2` (14,891 txns), `C04125-K2` (14,112), `C11923-K2` (10,332) |
| ☐ | MCP launcher | `TG_LOG_TOOL_CALLS=1 uv run python mcp/tg_mcp_launcher.py` (stdio) + `uv run pytest tests/unit/test_launcher_header.py` | `mcp/tg_mcp_launcher.py`, `mcp/tools_allowlist.txt` | `run_installed_query case_context {"t":{"id":"3478561"},"as_of":"2016-11-22 20:11:00"}` succeeds; `GSQL-TIMEOUT == "120000"`; `discover_tools` lists `run_installed_query` and **not** `gsql`/`run_query` |
| ☐ | Writer round-trip | smoke case through `open_case … record_approval` | `graph/queries/{open_case,append_case_event,close_case,record_approval}.gsql` | `card_profile.prior_agent_cases` shows the smoke case for a later `as_of` only |
| ☐ | Hand-investigate HHG-014 and write `cases/HHG-014.json` | `uv run python -m agent.validator --db data/ids.duckdb cases/HHG-014.json` | `cases/HHG-014.json` (start from `tests/fixtures/answers/HHG-014.sample.json`) | validator `[]`; `affected_txn_ids == ["3460634","3478561"]`, `exposure_usd == 187.33`, 27 `connected_card_ids` (the wave), profile string exact, `similar_prior_cases ⊇ {CC-2649, CC-2971, CC-2985, CC-3035}`, `sar.file == true`, `what_changed == "nothing"` |

---

## Day 4 — query library, second half + algorithms

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Queries `card_testing_check, under_threshold_burst, recurring_charge_check, episode_candidates, shared_origin_scan, ring_profile, case_subgraph, post_open_activity` (+ the four RAG queries on day 7) | `make install-queries` | `graph/queries/*.gsql` | 25 installed queries = 14 typed model tools + 4 writers + 7 harness-only (`post_open_activity`, `case_subgraph`, `grounding_chunks` + the four GraphRAG queries) |
| ☐ | Ring contract (D6 wave semantics) | `uv run pytest tests/live -m live -k ring` | `tests/live/test_query_contracts.py` | `device_neighbors(profile, 2016-11-01→opened_at)` → 20 cards (19 others); `→ 2016-12-31` → 28 (27 others); `device.n_cards_alltime == 52`; `ring_profile("C13487-K1")` → `wave_cards` 27, `pre_open_cards` 19, `n_cards_alltime` 52, `closed_cases` ids `CC-2649/2971/2985/3035` with `template_id`; generic `NULL \| NULL \| chrome 66.0 \| NULL` → empty lists |
| ☐ | Burst / testing / recurring contracts | `uv run pytest tests/live -m live -k "burst or testing or recurring"` | — | `under_threshold_burst("C07297-K1","2016-11-22 02:30:00")` → one burst (ids `3476602, 3476633, 3476665, 3476682`, amounts `478.95, 456.96, 488.04, 482.12`), 11 look-alikes; `card_testing_check("C11923-K2")` → `cleared_over_big true`, `larger_purchase.id "3528455"` (the harness unwraps `run`); `recurring_charge_check(3491361)` → `total_n 143`, group 126.0 n 17 |
| ☐ | Post-open guard | live test | — | `post_open_activity("C13487-K1", opened_at, 7)` includes `3489320` ($252.28, Nov 26) and `card_window(... to_ts=opened_at)` does **not** |
| ☐ | Algorithms once | `make algos` = `uv run python graph/run_algos.py` (`timeout=600000`) | `graph/algos/{project_shares_device,tg_wcc,wcc_summary,tg_degree_cent}.gsql`, `graph/run_algos.py` | `SHARES_DEVICE` edges over strong profiles; largest `wcc_id` component ≈ **5,080** cards (the blog's honest finding); `Card.device_degree` set; every expectation JSON < ~6k tokens |
| ☐ | Live contract tests green | `make test-live` | `tests/live/` | all pass |

---

## Day 5 — deterministic engine (`engine/`)  **NEVER CUT: policy gate + invariants, validator**

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Engine unit tests on the ETL facts (scorecard + `data/models/isotonic.json`, pattern rule, episode, SAR rule, policy gate, simulator, VoI, status, stop, answer writer, validator incl. the D10 invariants) | `ENGINE_FACTS_DB=data/hhgoa_engine.duckdb uv run pytest tests/unit/test_engine.py` (part of `make test-data`) | `engine/*.py`, `policy/fraud_policy.yaml`, `tests/unit/test_engine.py` | all pass: ring 52/27/19, 12 burst cards, pattern replay on true members ≥ 0.95, episode / policy / simulator / status tables; verdict bands D1 (`fraud ≥ 0.70`, `legitimate ≤ 0.15`); R8 gated (D3); `verdict == fraud` ⇒ a block / decline / escalation in `final` and no `CLOSE_NO_FRAUD`; a confirmation never clears a ring / burst hit |
| ☐ | 310-case replay | `make replay` = `ENGINE_FACTS_DB=… python -m engine.replay` (~25 s) | `engine/replay.py` → `engine/out/replay_metrics.json` | targets pattern ≥ 0.95, SAR decision 1.00, episode Jaccard ≥ 0.85, synthetic disputes blocked 0 (initial and final); the honest leak-free numbers after the fix pass and any shortfall are recorded in `impl/40-engine.md` (D12) with the day-6 tuning plan — do not silently accept |
| ☐ | Schemas have no numeric bounds | `uv run pytest tests/unit/test_schemas.py` | `agent/schemas.py` | no `minimum/maximum/minLength/maxLength` in any `model_json_schema()` |

---

## Day 6 — expectation sheet, engine-drafted fallback  **NEVER CUT: the 20 files**

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Expectation sheet from the engine | `make sheet` = `ENGINE_FACTS_DB=… python -m engine.run_cases` | `engine/run_cases.py` → `engine/out/*.draft.json` + `qa/engine_expectation_sheet.md` | the sheet of record (`qa/engine_expectation_sheet.md`, md5 `c6399e5823a1ad6944df474736f17e3f`) reproduced; every diff vs PLAN §2.3 reviewed (D1–D5: HHG-019 fraud, HHG-015 account_takeover uncertain 0.30 with `STEP_UP_AUTH`, HHG-016 fraud pre-evidence, HHG-001 / HHG-004 without `ESCALATE_TO_ANALYST`); SARs 006 and 014 only |
| ☐ | 20 engine-drafted files (no LLM) | `make engine-drafts` = `… python -m engine.run_cases --out runs/engine-drafts` then `uv run python -m agent.validator --db data/ids.duckdb runs/engine-drafts/*.draft.json` | `runs/engine-drafts/HHG-0nn.draft.json` | 20 files validate (`[]` ×20); `RUN_MODE=mock make mock-run ui` renders them |
| ☐ | Tag the fallback | `git tag day6-fallback` | — | tag pushed |

---

## Day 7 — GraphRAG (`rag/`, vectors)

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Corpus download (browser for the FATF/FFIEC PDFs that 403 to curl, into `data/corpus/raw/`), then verify + chunk + closed-case text | `make corpus` = `python -m rag.fetch_corpus --verify` · `python -m rag.chunk` (reads `HHGOA_README`) · `python -m rag.closedcase_embed_text` (reads `closed_case_parsed.embed_text` from `HHGOA_DB`) | `rag/fetch_corpus.py`, `rag/chunk.py`, `rag/closedcase_embed_text.py` → `data/out/policy_chunks.jsonl`, `policy_chunk_embed.jsonl`, `policy_chunk.csv`, `document.csv`, `chunk_of.csv`, `about.csv`, `closed_case_embed.jsonl` | ~130–150 chunks incl. `policy#R1..R10, 3a, 3b, 4..7, sar-format`, `pattern#*`, ≥ 73 regulation chunks with pages; `fraud_pattern.csv` **untouched** by rag (still the ETL's TSV, 1,295 B); `closed_case_embed.jsonl` 5,565 rows, `unmasked rows: 0` |
| ☐ | Embeddings | `make embed` = `python -m rag.embed --input data/out/policy_chunk_embed.jsonl --out data/out/vec_PolicyChunk.psv` and `… --input data/out/closed_case_embed.jsonl --out data/out/vec_ClosedCase.psv` | `rag/embed.py` → `data/out/vec_*.psv` (`id\|f1,f2,…`, split into ≤ 40 MB parts), `vec_*.manifest.json` | 5,565 + ~100 rows × `EMBED_DIM` floats. Default `EMBED_BACKEND=local` (bge-large-en-v1.5, 1024-d, mps): ~29 s for 100 policy chunks and ~33 s for 500 cases including a ~13 s model load ⇒ ~5 min for all 5,565 cases, $0 and no key. `manifest.json` records `backend`/`device`. `EMBED_BACKEND=voyage` instead: tokens < 0.5 M. |
| ☐ | Vector schema + loads + index | `make vectors` = `python -m rag.load_vectors --ensure-schema` (sends `graph/schema_change_vectors.gsql`, a GLOBAL job) then `--all` | `rag/load_vectors.py`, `graph/schema_change_vectors.gsql` | `valid=5565` / `valid=n_chunks`, `Ready_for_query`; `python -m rag.load_vectors --status` → `ready: true` for `ClosedCase.note_emb`, `PolicyChunk.emb`, `AgentCase.note_emb` |
| ☐ | Retrievers | `make install-queries` again (the four RAG queries: `similar_prior_cases`, `similar_prior_cases_closed`, `similar_prior_cases_agent`, `grounding_chunks` — merged in the harness if the day-0 two-type test failed) | `graph/queries/similar_prior_cases*.gsql`, `grounding_chunks.gsql`, `rag/retrieval.py` | `find_similar_cases` for HHG-014 returns `CC-2649, CC-2971, CC-2985, CC-3035` with `same_device`, ≥ 1 cleared case; HHG-011 (`pattern_sig="card_testing"`) returns `CC-0290/2247/2673/4501` among the top 8; `grounding_chunks("who what when where why how", doc_filter="sar_guidance_narrative")` returns the Who/What/When chunks |
| ☐ | RAG unit tests + OFAC | `uv run pytest tests/unit/test_rag.py`; `python -m rag.fetch_corpus --ofac`; `python -m rag.ofac "AEROCARIBBEAN AIRLINES"` | `rag/ofac.py`, `rag/sar.py`, `rag/validate_sar.py` (the single SAR validator), `data/ofac/{sdn,alt}.csv` | 10 passed (1 skipped without `HHGOA_README`); exact OFAC match found; the anonymised customers screen clear |

---

## Day 8 — agent core (P0–P5)

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Mock end-to-end = the engine sheet | `RUN_MODE=mock uv run python -m agent.bench --run-id ci --cases all --mock` | `agent/phase_machine.py` (control flow = `engine.run_cases`), `agent/bench.py`, `agent/runlog.py` | 20 files, `problems=0`; verdict / pattern / final-action columns equal the engine sheet for all 20 |
| ☐ | Schemas, launcher, tool lists | `uv run pytest tests/unit/test_schemas.py tests/unit/test_launcher_header.py` | `agent/schemas.py`, `agent/mcp_client.py`, `mcp/query_descriptions.yaml` | 21 passed; read-only session lists 13 tools without `tigergraph__add_*`, harness session 17 |
| ☐ | Phases P0–P5 on the model | `make bench-one CASE=HHG-014 RUN_ID=d8` and `HHG-010` on `claude-sonnet-5` — no key needed (`LLM_BACKEND=cli`, the default); over DuckDB facts instead of the graph: `uv run python -m agent.bench --run-id d8 --cases HHG-014,HHG-010 --mock --llm-backend cli` | `agent/prompts/*.md` (hashed into `run_manifest.json`), `agent/llm.py` (the backend seam) | both validate; HHG-014 initial = `CREATE_CASE, MONITOR_CONNECTED_CARDS, BLOCK_CARD(L1), FILE_REPORT(L2), ESCALATE_TO_ANALYST`, `evidence_requests == []`; HHG-010 initial = `CREATE_CASE + VERIFY_WITH_CUSTOMER`; ≤ 12 model tool calls; `phases.jsonl` shows `cache_read > 0` from the second parse call (both backends cache; on `cli` the CLI session does it) |

---

## Day 9 — agent rest (P6–P10)

| ☐ | Step | Command | Files | Check |
|---|---|---|---|---|
| ☐ | Evidence request, simulator (D5), reassess, final, SAR (`rag.sar` generator + `rag.validate_sar` in P8), explain, persist (`AgentCase.note_emb` from `rag.closedcase_embed_text.agent_case_embed_text`) | `make bench RUN_ID=dev9 CASES=HHG-014,HHG-006,HHG-007,HHG-010,HHG-018` | `agent/phase_machine.py`, `agent/persist.py`, `rag/sar.py`, `rag/validate_sar.py` | five validated files matching the sheet on verdict / pattern / actions; `AgentCase AC-HHG-014` / `AC-HHG-006` in GraphStudio with `HAS_EVENT`, `HAS_APPROVAL` (`AP-AC-HHG-014-FILE_REPORT` pending), `CASE_SIMILAR_TO` edges; `/vector/status/FraudGraph/AgentCase/note_emb` → `NeedRebuildServers == []`; one live `tigergraph__add_edges` call verified |
| ☐ | Approvals wired | `make ui` → Approvals | `engine/policy.authorize()` → `ui.approvals_db.enqueue` | HHG-014 shows `BLOCK_CARD (L1)` and `FILE_REPORT (L2)` pending; approving writes `Approval` + `CaseEvent kind=approval` (with `event_at`) |

---

## Day 10 — all 20 on Sonnet 5, promote v1

| ☐ | Step | Command | Check |
|---|---|---|---|
| ☐ | Warm + keep-alive | `make awake && make keepalive &` | workspace stays up for the whole run |
| ☐ | Benchmark | `make bench RUN_ID=v1 MODEL=claude-sonnet-5` (opened_at order: HHG-017 first … HHG-004 last); `LLM_BACKEND=cli` (default, subscription, no key) or `LLM_BACKEND=api` for the metered path | 20 × `answer.json`; each ≤ 12 LLM-initiated tool calls, ≤ 60 s + wake-up. Token budget: ≤ 40k on `api`; on `cli` the per-phase figures come from the CLI's `ResultMessage.usage` and are dominated by cache reads (one resumed session per case), so compare `output_tokens`, not the total |
| ☐ | Review against the sheet | `make sheet` + UI Runs page | ≈ 7 legitimate / 8 fraud / 5 uncertain; no block in an `uncertain` initial; SAR only 006 and 014; every fraud verdict carries a block / decline / escalation |
| ☐ | Promote | `make promote RUN_ID=v1` (= `agent.validator --db data/ids.duckdb runs/v1/*/answer.json` then `agent.promote`) | `cases/HHG-001..020.json` + `cases/MANIFEST.md`; `make validate` → `[]` for all 20; CI green including the validator step |

---

## Day 11 — Streamlit + CI hardening

| ☐ | Step | Command | Check |
|---|---|---|---|
| ☐ | Pages against the real run | `make ui` (run `v1`) | queue shows 20 rows with verdict / p_pre / p_post / SAR; case page shows evidence badges, families, initial → final with route badges and rule chips, counterfactual, SAR badges (one per `rag.validate_sar` code) green for 006/014; graph page draws `case_subgraph` (`vtype`/`etype`) for HHG-014 (≤ 300 nodes); approving `FILE_REPORT` for HHG-014 → `Approval AP-AC-HHG-014-FILE_REPORT` + `CaseEvent kind=approval` in GraphStudio |
| ☐ | Mock fixtures + CI | `git push` | CI: ruff · unit tests (data-backed ones skipped) · `tests/unit/test_ensure_awake.py`, `test_approvals_db.py`, `test_ui_common.py` → 23 passed · validator over `cases/*.json` · mock e2e HHG-014 · UI AppTest — all green, no secrets |

---

## Day 12 — final frozen run on Opus 5, agreement, freeze

| ☐ | Step | Command | Check |
|---|---|---|---|
| ☐ | Final run | `make bench RUN_ID=final MODEL=claude-opus-5` (`LLM_BACKEND=cli` needs no key; `MODEL` is passed to the CLI as `--model`, so the id is honoured either way) | 20 validated files; `run_manifest.json` records `llm_backend` |
| ☐ | Second run for agreement | `make bench RUN_ID=final-2 MODEL=claude-opus-5` → UI Runs page "Agreement" | verdict / pattern / final actions agreement **≥ 90 %**. Keep both runs on the same backend: `cli` pins no model snapshot, so agreement across backends measures two things at once |
| ☐ | Hand review every file | read all 20 in the case page | invariants green (`sar.file ⇔ FILE_REPORT`, `legitimate ⇒ exposure 0`, `fraud ⇒ block/decline/escalate and no CLOSE_NO_FRAUD`, `CLOSE_NO_FRAUD ⇒ legitimate`, `uncertain ⇒ escalated/open`, `what_changed == "nothing" ⇔ initial == final`, every id resolves) |
| ☐ | Freeze | `make promote RUN_ID=final && git tag cases-frozen && git push --tags` | `cases/` frozen; `runs/final/run_manifest.json` carries model + prompt hashes |

---

## Day 13 — demo recording
`docs/demo_script.md`. `make demo-warm` (awake + keep-alive), two takes, upload, link in README. Fallback: `RUN_MODE=mock` replay from `runs/final/`.

## Day 14 — blog, social, README, lints
`docs/blog_outline.md` → blog (≥ 900 words + diagram); `docs/social.md` → post tagging @TigerGraphDB; README links filled; repo lint for Kaggle / IEEE-outcome references and the `:00 seconds` tell (`qa/lint_repo.py`, to be written, ~30 lines → 0 hits); corpus licensing check; Discord answers folded in; submission form complete.

---

## Stretch (only when ahead)
Monitoring mode (an `agent.monitor` that does not exist yet → `cases_monitoring/`, capped at 15 alerts; burst alerts without a denial get 0.55 + R1 verify) · "why" panel and live tool trace · `tg_pagerank_pers` on the fraud-restricted projection · third run · `GDBMS_ALGO` screenshot · the 1,800-word blog.

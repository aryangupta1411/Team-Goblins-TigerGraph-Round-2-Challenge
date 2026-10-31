# Resilience and data-egress statement: Final Round (HHGOA 2026)

## One command: one case from a clean clone

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12. It uses no keys, no graph and no dataset download. The 18 Final Round cases and the facts they rest on (base + January) are in the tracked `data/hhgoa_engine.duckdb`, and the validator's ids are in `data/ids.duckdb`.

```bash
git clone <REPO_URL> Tiger-graph && cd Tiger-graph && make install && RUN_MODE=mock uv run python -m agent.bench --cases HHF-004 --run-id drill-1 --mock && uv run python -m agent.validator --db data/ids.duckdb runs/drill-1/HHF-004/answer.json
```

That writes `runs/drill-1/HHF-004/{answer.json,calls.jsonl,phases.jsonl,sar.md}` and validates it. CI runs the same drill (`.github/workflows/ci.yml`, step "Final Round clean-clone drill"). The same case against the live graph and a real model (needs `TG_HOST`, `TG_SECRET` and, for Gemini, `GEMINI_API_KEY` in `.env`):

```bash
RUN_MODE=live LLM_BACKEND=gemini uv run python -m agent.bench --cases HHF-004 --run-id live-drill-1
```

The live command calls `typology_scan` on the workspace for the January detectors. It is installed, and on 30 cases its results match the local detectors exactly (`graph/check_typology_parity.py --installed`: 6 hits, 0 differences). If it fails or is missing, a January case stops with `CaseDataMissing` and writes no answer (see the table below).

## Dependencies and what leaves the bank's boundary

| Dependency | Role | What leaves the boundary |
|---|---|---|
| TigerGraph Savanna (`FraudGraph`, 4.2) | Source of truth in live mode: base + January loaded and count-checked (`make load-delta`; `ops.check_live_counts` against the 18 `expected_after` types in `data/out_delta/delta_manifest.json`, Transaction 650,938, ClosedCase 5,585). The installed GSQL query library (`graph/queries/`; including `typology_scan`, installed 2026-10-31), TigerVector indexes on `ClosedCase`, `AgentCase` and `PolicyChunk`, and write-back of `AgentCase` / `CaseEvent` / `Approval`. | Pseudonymous dataset ids, amounts, timestamps and 1024-d vectors over TLS. No card numbers or names exist in the data. |
| Local facts snapshot (`data/hhgoa_engine.duckdb`) | The validator, the SAR id resolver, the offline engine and mock mode read it. On the current code, the January detectors D1-D4 run in the graph (`typology_scan`) in live mode. The snapshot holds their DuckDB twin (`DuckFacts.typology_scan`), and `graph/check_typology_parity.py` compares the two. | Nothing: local file. |
| LLM: `LLM_BACKEND=gemini` (Google Gemini API, `gemini-2.5-flash` by default), `cli` (Claude Code via claude-agent-sdk) or `api` (Anthropic API). Set `LLM_BACKEND` explicitly: when it is empty and `GEMINI_API_KEY` is set, Gemini is used. | P2 tool choice (read-only typed queries), P4 evidence wording and a probability move of at most ±0.10, P5/P7 action reasons inside the policy's admissible set, P8 SAR draft, P9 summary | The sanitised trigger text (instruction sentences removed), time-boxed query results (`ts <= opened_at`, closed cases only once `closed_at <= opened_at`), and masked closed-case summaries. Never `.env`, file paths or post-opening activity. |
| Embeddings (`BAAI/bge-large-en-v1.5`, local) | Vectors for closed cases, agent cases and policy chunks | Nothing: runs on this machine. Needs `RUN_MODE=live`; under `RUN_MODE=mock` `rag.embed` writes hashed stand-in vectors by design. |
| OFAC screening (`rapidfuzz` over a checked-in SDN snapshot) | Subject screening in P8 | Nothing. |
| Approval inbox (`ui/approvals.sqlite`, approver directory `ui/principals.json`) | Server-side authentication and RBAC, immutable decisions, append-only audit log, mirrored to `Approval` vertices | Nothing. |

## Fail-closed behaviour under the drills

| Fault | What the system does | What is logged |
|---|---|---|
| Graph or MCP unreachable (502/503/504, timeout, dropped connection) | Retries with backoff up to `TG_RESUME_MAX_WAIT_S` (240 s), then raises `GraphUnavailable`. No `answer.json` is written, the run stops, and `--resume` picks up the unfinished case. A missing reply is never treated as evidence. | `phases.jsonl` note, a `FAIL` line on stderr, the case marked failed in the run table |
| The flagged transaction is not in the graph, or (on a January case) `typology_scan` fails or is missing | `CaseDataMissing`: the case stops and no answer is written. Scoring a January case without the scan would silently drop the evidence that decides the verdict. | `phases.jsonl` (`case failed`) |
| Model timeout, refusal or unparseable output | Gemini HTTP errors (429/5xx) are retried up to 5 attempts. Structured phases validate against strict schemas in up to 3 attempts, then the case fails (`agent/llm.py`). A probability more than ±0.10 from the engine's is clamped (`agent.security.clamp_to_engine`). An action outside `engine.policy.admissible()` is replaced by the policy's own set. A SAR failing V00-V19 falls back to the deterministic attested narrative. | `calls.jsonl` (`kind=llm`, latency, tokens); `phases.jsonl` (`clamped`, `policy_override`) |
| Hostile or invalid input (trigger text, stored notes, tool rows) | Text is NFKC-normalised and zero-width characters are removed, and every sentence carrying an instruction is dropped before the model sees it. Channel and amount claims are checked against the transaction record. Customer-visible `evidence_requests` are checked for tip-off terms (report, SAR, regulator, AML, law enforcement). The engine's decision never reads free text. `run_attack_ablation()` blocks 23 / 23 attacks (6 planted in the pack, 17 synthetic), and `sanitize_trigger_text` raises 0 false alarms on the 12 HHF triggers that carry no injection. | `CaseEvent(kind=security_alert)`; `phases.jsonl` codes `INJ_*`, `SPOOF_*` |
| Approval by the wrong role, or editing a decided approval | `decide()` accepts only a session minted by `authenticate()` (scrypt passphrase, server-side directory). The role comes from the directory, never from the request. A team lead decides L1 only; L2 needs a fraud manager. SQLite triggers refuse any update or delete of a decided approval or of the audit log, and there is no reopen. With no directory file, nobody can approve. | `approval_audit_log`: `ok` / `denied_auth` / `denied_rbac` / `denied_immutable`, actor, role, time |
| The same case run twice | Engine output is deterministic: two consecutive `engine.run_cases` runs over HHF-001 … HHF-018 give identical drafts apart from `latency_s`. Graph writes are keyed upserts (`AC-<case>`, `AC-<case>-<seq>`, `AP-AC-<case>-<ACTION>`). Case memory excludes the case's own agent case and every case opened at or after it. `written_to_graph` is true only when the `AgentCase` upsert succeeded. | `run_manifest.json` (git SHA, dirty flag, prompt hashes, per-case invocations) |

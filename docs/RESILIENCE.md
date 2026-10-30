# One-Page Resilience & Data-Egress Statement — Final Round (HHGOA 2026)

## 1. One-Command Setup & Single-Case Execution (Clean Clone)
```bash
make install && uv run python -m schema_engine.cli ingest
# Run a single Final Round case (offline DuckDB facts + deterministic/CLI model):
uv run python -m agent.bench --cases HHF-001 --run-id drill-1 --mock
# Or against live TigerGraph Savanna Cloud + Claude Code CLI:
RUN_MODE=live LLM_BACKEND=cli uv run python -m agent.bench --cases HHF-001 --run-id live-drill-1
```

---

## 2. Dependency Map & Data Boundary (What Leaves the Bank's Boundary)

| Dependency | Role | What Leaves the Bank's Boundary? |
|---|---|---|
| **TigerGraph Savanna Cloud (`FraudGraph` v4.2)** | Primary graph store, 25 installed GSQL queries, TigerVector HNSW (`ClosedCase.note_emb`, `AgentCase.note_emb`, `PolicyChunk.emb`), and write-back target (`AgentCase`, `CaseEvent`, `Approval`). | Anonymised dataset IDs (`C01234`, `C01234-K1`, `3660108`), transaction amounts/timestamps, and masked 1024-d vectors over TLS 1.3 (`HTTPS:443`). No raw card numbers (PANs) or customer PII exist in the graph. |
| **LLM Provider (`claude-agent-sdk` CLI / Anthropic API / Gemini API)** | P2 tool selection (read-only), P4 evidence prose & bounded `±0.10` probability adjustment, P5/P7 action reason prose, P8 SAR narrative draft, P9 executive summary. | Receives **only** sanitized `trigger_text` (prompt injections stripped by `agent/security.py`), time-boxed (`ts <= opened_at`) read-query outputs, and masked closed-case summaries. Never sees `.env`, filesystem paths, or post-opening transactions. |
| **Embeddings (`BAAI/bge-large-en-v1.5`)** | 1024-d embeddings for `ClosedCase`, `AgentCase`, and `PolicyChunk`. | **Nothing leaves the boundary.** Runs 100% locally via `sentence-transformers` on CPU/MPS (`EMBED_BACKEND=local`). |
| **OFAC Screening (`data/ofac/sdn.csv`)** | SDN/ALT sanctions screening for SAR filings (`P8`). | **Nothing leaves the boundary.** Runs 100% locally via `rapidfuzz` over the checked-in SDN snapshot. |
| **Approval Inbox (`ui/approvals.sqlite`)** | Server-side RBAC enforcement (`team_lead` L1 / `fraud_manager` L2) and immutable approval ledger. | **Nothing leaves the boundary.** Stored in local SQLite and mirrored to TigerGraph `Approval` vertices. |

---

## 3. Fail-Closed Behaviour Under Fault Drills

| Fault / Drill Category | How the System Responds (Fail-Closed Guarantee) | What Is Logged |
|---|---|---|
| **1. Graph / MCP Unreachable Mid-Run (`502/503/504`, timeout, dropped connection)** | `ops/ensure_awake.py` and `agent/mcp_client.py` retry transient errors with exponential backoff up to `TG_RESUME_MAX_WAIT_S` (`240s`). If the workspace remains down, `mark_graph_unavailable()` refunds the tool budget and raises `GraphUnavailable` after `P2`. **No `answer.json` is written**, and absence of a graph reply is never treated as evidence of innocence. | `phases.jsonl` (`note: "graph transient, retrying"`), stderr `FAIL` banner; `--resume` re-runs only the unfinished case once awake. |
| **2. LLM Timeout, Refusal, or Garbage / Unparseable Output** | Structured phases (`P4, P5, P7, P8, P9`) validate against strict Pydantic JSON schemas (`agent/schemas.py`) with up to 3 re-prompt retries. Any probability move beyond `±0.10` of `p_engine` is hard-clamped; any action outside `engine.policy.admissible()` is rejected; any SAR failing `V00`–`V19` (`rag/validate_sar.py`) falls back to the deterministic attested narrative (`rag.sar.fallback_narrative`). | `calls.jsonl` (`kind="llm"`, latency, token counts) and `phases.jsonl` (`note: "retrying structured output"` / `"clamped"`). |
| **3. Hostile Prompt Injection or Spoofed Trigger Claims (`HHF-004, 005, 008, 010, 014, 016`)** | `agent/security.py` (`sanitize_trigger_text`) strips `SYSTEM NOTE:`, `[[assistant:...]]`, `<!-- agent:... -->`, and fake compliance/customer close directives before LLM intake, and cross-checks `trigger_text` channel claims against the DB transaction record (`HHF-008` claiming `online` when `3660353` is `in_person`). `check_tipoff()` blocks any SAR/FinCEN/regulator mention in `evidence_requests`. | `CaseEvent(kind="security_alert")` in TigerGraph and `phases.jsonl` (`codes=["INJ_...", "SPOOF_..."]`). |
| **4. Approval Attempted by Wrong Role or Editing a Decided Approval** | `ui/approvals_db.py` (`decide`) resolves the caller's role from the server-side `SERVER_PRINCIPAL_ROLES` directory (ignoring spoofed client role parameters). `team_lead` is restricted to `L1`; `fraud_manager` is required for `L2` (`BLOCK_CARD > $2,500`, `BLOCK_ALL_CARDS`, `FILE_REPORT`); `analyst`/`intern` have `0` approval privileges. Once a row is `approved` or `rejected`, it is **immutable** (`PermissionError` on any overwrite or `reopen`). | `approval_audit_log` SQLite table (`outcome="denied_rbac"` or `"denied_immutable"`, actor, role, timestamp). |
| **5. Running the Same Case Twice & Partial Graph Persistence** | Every graph write is a keyed idempotent upsert (`AC-<case_id>`, `AC-<case_id>-<seq>`, `AP-AC-<case_id>-<ACTION>`), and `PhaseMachine._memory_exclusions()` excludes `AC-<case_id>` and any case with `opened_at >= current` so a re-run never retrieves itself. `written_to_graph` is set to `true` only if `upsertVertex("AgentCase")` succeeds. | `runs/<run_id>/run_manifest.json` (`invocations[]`, `case_runs{}` with git SHA and prompt SHA-256s). |

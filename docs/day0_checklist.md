# Day 0 checklist (PLAN §6 day 0) — accounts, Savanna workspace, smoke tests

Record every answer in this file (edit the **Record** lines). Nothing on day 1+ starts until items 1–12 are ticked.
Sources: research/gap2 (Savanna facts), gap3 (MCP timeouts), VectorSearch.md (TigerVector syntax).

- [ ] **0. Deadline.** Confirm the submission deadline and time zone on the HHGOA Discord (the brief does not state it). Also ask the five questions in PLAN §8.
  **Record:** deadline = ____ ; Discord thread link = ____

## 1. The model — **no API key required** (`LLM_BACKEND=cli` is the default)

`LLM_BACKEND=cli` runs the agent on `claude-agent-sdk`, which drives the Claude Code CLI it bundles
(2.1.x, installed with the package — the `claude` on your PATH is not used) and authenticates with
your **Claude subscription**: no `ANTHROPIC_API_KEY`, no prepaid credit, no rate-limit tier to buy.
You need to have signed in once (`claude` → `/login`). The paid path is still there behind
`LLM_BACKEND=api`, and that is the **only** case that needs `ANTHROPIC_API_KEY` — skip step 1b unless
you deliberately want it.

### 1a. CLI backend (do this one)
```bash
uv run python - <<'EOF'
import anyio
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
opts = ClaudeAgentOptions(system_prompt={"type": "custom", "prompt": "Reply exactly."}, tools=[],
                          setting_sources=[], model="claude-sonnet-5",
                          output_format={"type": "json_schema",
                                         "schema": {"type": "object", "properties": {"ok": {"type": "string"}},
                                                    "required": ["ok"], "additionalProperties": False}})
async def main():
    async for m in query(prompt="Reply with ok='OK'.", options=opts):
        if isinstance(m, ResultMessage):
            print(m.structured_output, m.usage, m.total_cost_usd)
anyio.run(main)
EOF
```
Expected: `{'ok': 'OK'}` plus a usage dict (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, …)
and a `total_cost_usd` figure (what the same tokens *would* cost on the API — subscription runs do not
bill it). If it prints `Invalid API key` or a login prompt, run `claude` once and `/login`.
**Record:** CLI version (`uv run python -c "import claude_agent_sdk._cli_version as v; print(v.__cli_version__)"`) = ____ ;
subscription plan = ____ ; models available (`claude-sonnet-5`, `claude-opus-5`) = ____

### 1b. Anthropic API key (optional, paid — prepaid credit and a rate-limit tier)
```bash
# platform.claude.com → Settings → Billing: buy prepaid credit; Settings → Limits: read the tier (a new key is Tier 1)
uv run python - <<'EOF'
import anthropic, os
from dotenv import load_dotenv; load_dotenv()
c = anthropic.Anthropic()
r = c.messages.create(model=os.getenv("MODEL","claude-sonnet-5"), max_tokens=20, messages=[{"role":"user","content":"Reply with the single word OK."}])
print(r.content[0].text, r.usage)
EOF
```
Expected: `OK` and a usage object. **Record:** tier = ____ ; credit = $____ ; models available (`claude-sonnet-5`, `claude-opus-5`) = ____

## 2. Embeddings — **no API key required** (EMBED_BACKEND=local is the default)

`EMBED_BACKEND=local` runs `BAAI/bge-large-en-v1.5` (1024-d, MIT) through sentence-transformers on this
machine: one 1.34 GB download into `~/.cache/huggingface`, then no network and no key at run time. The
paid Voyage path is still there behind `EMBED_BACKEND=voyage`, and that is the **only** case that needs
`VOYAGE_API_KEY` — skip step 2b unless you deliberately want it.

### 2a. Local backend (do this one)
```bash
uv run python - <<'EOF'
from rag.embed import Embedder
e = Embedder()                       # EMBED_BACKEND=local, BAAI/bge-large-en-v1.5, EMBED_DIM=1024
v = e.embed_documents(["confirmed travel to the billing region"])[0]
print(e.describe(), len(v), round(sum(x*x for x in v) ** 0.5, 6))
EOF
```
Expected: `{'backend': 'local', 'model': 'BAAI/bge-large-en-v1.5', 'dim': 1024, 'device': 'mps:0', ...} 1024 1.0`.
First run downloads the weights (~35 s on a warm connection); later runs load from cache in ~13 s.
Lighter alternative if disk or RAM is tight: `EMBED_MODEL=BAAI/bge-small-en-v1.5 EMBED_DIM=384` (133 MB) —
`EMBED_DIM` is rendered into `graph/schema_change_vectors.gsql`, so nothing else changes.
**Record:** device = ____ ; model = ____ ; dim = ____ ; download seconds = ____

### 2b. Voyage key (optional, paid — Tier 1 needs a payment method)
```bash
EMBED_BACKEND=voyage uv run python - <<'EOF'
import voyageai, os
from dotenv import load_dotenv; load_dotenv()
vo = voyageai.Client()
r = vo.embed(["confirmed travel to the billing region"], model=os.getenv("EMBED_MODEL","voyage-4-lite"), input_type="document")
print(len(r.embeddings[0]), r.total_tokens)
EOF
```
Expected: `1024 <n>` (EMBED_DIM=1024). **Record:** used? = ____ ; free-token balance = ____

> The two backends are different vector spaces. Pick one before `make embed` and do not mix them in one
> graph; switching means re-running `make embed` and reloading `vec_*.psv`.

## 3. Savanna sign-up → workspace → Database Secret
1. https://savanna.tgcloud.io → sign up (no card) → Workgroups → **Create Workspace**: read-write, size **TG-00** (2 vCPU / 16 GiB), runtime = **newest 4.2.x** in the dropdown.
2. Database Secrets → Create Secret → select the workspace → copy once → `.env` `TG_SECRET=…`; `TG_HOST=https://<workspace-id>.i.tgcloud.io`.
3. `make awake` → prints the product version.
**Record:** workspace id = ____ ; runtime dropdown entries (all) = ____ ; chosen = ____ ; `getVer()` = ____

## 4. Credit balance, expiry and the credit decision rule
Billing page: screenshot the balance and expiry; complete every onboarding task and note the per-task credit. TG-00 costs $1/h ⇒ hours = USD ÷ 1.
Decision rule (PLAN day 0): **< 35 h** → develop engine + UI offline in `RUN_MODE=mock`, open the workspace only for load / install / benchmark / recording; **< 20 h** → add a payment method or fall back to Community Edition on Rosetta (`tigergraph/community:4.2.5`, 10–12 GB Docker memory).
**Record:** balance = $____ (≈ ____ h) ; expiry = ____ ; decision = ____

## 5. Auto-stop / auto-start, idle interval, maintenance window
Workspace → Settings: confirm **auto-stop and auto-start are enabled** (brief requirement; auto-stop is locked on for Free tier); list the idle-interval choices offered; move the automatic maintenance window (default every 4 weeks, Sunday 20:00–22:00) away from the demo/deadline days.
**Record:** idle intervals offered = ____ ; chosen = ____ ; maintenance window set to = ____

## 6. 502-resume timing
Let the workspace auto-stop, then:
```bash
time uv run python -m ops.keep_alive --once      # ensure_awake backoff 1,2,4,… up to 240 s
```
Expected: first requests fail with 502/connection error, then `workspace awake (4.2.x)`. **Record:** resume time = ____ s (sets `TG_RESUME_MAX_WAIT_S`)

## 7. Graph Admin timeout field
Workspace → Graph Admin (or Admin Portal) → RESTPP **Default query timeout**: note the field and whether it is editable on Free tier. The MCP launcher injects `GSQL-TIMEOUT: 120000` regardless (gap3), so this is a fallback only.
**Record:** editable = ____ ; value = ____

## 8. `CREATE VERTEX` smoke test for every planned name (reserved-word check, PLAN §3.4)
Run in GSQL Shell (GraphStudio → GSQL Shell) or via `conn.gsql(...)` on a throw-away global schema; then drop everything.
```gsql
CREATE VERTEX Customer (PRIMARY_ID id STRING, n_cards INT) WITH primary_id_as_attribute="true"
CREATE VERTEX Card (PRIMARY_ID id STRING, customer_id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX Transaction (PRIMARY_ID id STRING, ts DATETIME, amt FLOAT) WITH primary_id_as_attribute="true"
CREATE VERTEX DeviceProfile (PRIMARY_ID id STRING, is_strong BOOL) WITH primary_id_as_attribute="true"
CREATE VERTEX EmailDomain (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX BillingRegion (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX ClosedCase (PRIMARY_ID id STRING, outcome STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX AgentCase (PRIMARY_ID id STRING, verdict STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX CaseEvent (PRIMARY_ID id STRING, seq INT, kind STRING, payload STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX Approval (PRIMARY_ID id STRING, status STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX Document (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX PolicyChunk (PRIMARY_ID id STRING, text STRING) WITH primary_id_as_attribute="true"
CREATE VERTEX FraudPattern (PRIMARY_ID id STRING) WITH primary_id_as_attribute="true"
CREATE DIRECTED EDGE OWNS (FROM Customer, TO Card) WITH REVERSE_EDGE="REV_OWNS"
CREATE DIRECTED EDGE MADE (FROM Card, TO Transaction) WITH REVERSE_EDGE="REV_MADE"
CREATE DIRECTED EDGE NEXT (FROM Transaction, TO Transaction, gap_seconds INT) WITH REVERSE_EDGE="REV_NEXT"
CREATE DIRECTED EDGE FROM_DEVICE (FROM Transaction, TO DeviceProfile, device_new STRING, proxy_type STRING) WITH REVERSE_EDGE="REV_FROM_DEVICE"
CREATE DIRECTED EDGE PURCHASER_EMAIL (FROM Transaction, TO EmailDomain) WITH REVERSE_EDGE="REV_PURCHASER_EMAIL"
CREATE DIRECTED EDGE RECIPIENT_EMAIL (FROM Transaction, TO EmailDomain) WITH REVERSE_EDGE="REV_RECIPIENT_EMAIL"
CREATE DIRECTED EDGE BILLED_IN (FROM Transaction, TO BillingRegion) WITH REVERSE_EDGE="REV_BILLED_IN"
CREATE DIRECTED EDGE INVOLVES (FROM ClosedCase, TO Transaction) WITH REVERSE_EDGE="REV_INVOLVES"
CREATE DIRECTED EDGE ON_CARD (FROM ClosedCase, TO Card) WITH REVERSE_EDGE="REV_ON_CARD"
CREATE DIRECTED EDGE CONNECTED_TO (FROM ClosedCase, TO Card) WITH REVERSE_EDGE="REV_CONNECTED_TO"
CREATE DIRECTED EDGE MATCHES (FROM ClosedCase, TO FraudPattern) WITH REVERSE_EDGE="REV_MATCHES"
CREATE DIRECTED EDGE CASE_INVOLVES (FROM AgentCase, TO Transaction) WITH REVERSE_EDGE="REV_CASE_INVOLVES"
CREATE DIRECTED EDGE CASE_ON_CARD (FROM AgentCase, TO Card) WITH REVERSE_EDGE="REV_CASE_ON_CARD"
CREATE DIRECTED EDGE CASE_CONNECTED_TO (FROM AgentCase, TO Card) WITH REVERSE_EDGE="REV_CASE_CONNECTED_TO"
CREATE DIRECTED EDGE CASE_DEVICE (FROM AgentCase, TO DeviceProfile) WITH REVERSE_EDGE="REV_CASE_DEVICE"
CREATE DIRECTED EDGE CASE_SIMILAR_TO (FROM AgentCase, TO ClosedCase, score FLOAT) WITH REVERSE_EDGE="REV_CASE_SIMILAR_TO"
CREATE DIRECTED EDGE CASE_SIMILAR_TO_AGENT (FROM AgentCase, TO AgentCase, score FLOAT) WITH REVERSE_EDGE="REV_CASE_SIMILAR_TO_AGENT"
CREATE DIRECTED EDGE CASE_CITES (FROM AgentCase, TO PolicyChunk) WITH REVERSE_EDGE="REV_CASE_CITES"
CREATE DIRECTED EDGE CASE_MATCHES (FROM AgentCase, TO FraudPattern) WITH REVERSE_EDGE="REV_CASE_MATCHES"
CREATE DIRECTED EDGE HAS_EVENT (FROM AgentCase, TO CaseEvent) WITH REVERSE_EDGE="REV_HAS_EVENT"
CREATE DIRECTED EDGE HAS_APPROVAL (FROM AgentCase, TO Approval) WITH REVERSE_EDGE="REV_HAS_APPROVAL"
CREATE DIRECTED EDGE ABOUT (FROM PolicyChunk, TO FraudPattern) WITH REVERSE_EDGE="REV_ABOUT"
CREATE DIRECTED EDGE CHUNK_OF (FROM PolicyChunk, TO Document) WITH REVERSE_EDGE="REV_CHUNK_OF"
CREATE UNDIRECTED EDGE SHARES_DEVICE (FROM Card, TO Card, n_shared INT, weight FLOAT, via STRING)
CREATE GRAPH SmokeGraph (*)
```
Expected: every statement returns `Successfully created …`; none says `syntax error` (that is how `Case` was caught). Then `DROP ALL` (global) on the throw-away workspace state before day 2's real DDL.
**Record:** all 13 vertices + 24 edges accepted = ____

## 9. Vector attribute + `vectorSearch` smoke, including a two-vertex-type search with a mixed `candidate_set` (PLAN §3.7)
```gsql
USE GLOBAL
CREATE GLOBAL SCHEMA_CHANGE JOB smoke_add_vec {
  ALTER VERTEX ClosedCase ADD VECTOR ATTRIBUTE note_emb(DIMENSION=4, METRIC="COSINE");
  ALTER VERTEX AgentCase  ADD VECTOR ATTRIBUTE note_emb(DIMENSION=4, METRIC="COSINE");
}
RUN GLOBAL SCHEMA_CHANGE JOB smoke_add_vec
USE GRAPH SmokeGraph
CREATE OR REPLACE QUERY smoke_two_type (LIST<FLOAT> q, INT k) SYNTAX v3 {
  MapAccum<VERTEX, FLOAT> @@d;
  cand = {ClosedCase.*, AgentCase.*};
  v = vectorSearch({ClosedCase.note_emb, AgentCase.note_emb}, q, k, {candidate_set: cand, distance_map: @@d});
  PRINT v; PRINT @@d;
}
INSTALL QUERY smoke_two_type
```
Then load three toy vectors the way `persist` and tigergraph-mcp's `upsert_vectors` do it (REST upsert with the vector attribute in the attribute map) and run the query:
```bash
uv run python - <<'EOF'
from ops.ensure_awake import tg_connection, warm_up
c = tg_connection("SmokeGraph"); warm_up(c)
c.upsertVertex("ClosedCase", "CC-0001", {"outcome": "confirmed_fraud", "note_emb": [0.1, 0.2, 0.3, 0.4]})
c.upsertVertex("ClosedCase", "CC-0002", {"outcome": "cleared",         "note_emb": [0.9, 0.1, 0.1, 0.1]})
c.upsertVertex("AgentCase",  "AC-HHG-014", {"verdict": "fraud",       "note_emb": [0.1, 0.2, 0.3, 0.5]})
print(c.getVectorIndexStatus())                       # GET /vector/status — wait until every index reports ready
print(c.runInstalledQuery("smoke_two_type", {"q": [0.1, 0.2, 0.3, 0.45], "k": 2}, timeout=30000, usePost=True))
EOF
```
Expected: two vertices back (`CC-0001` and `AC-HHG-014`) with distances, one from each vertex type.
If the mixed `candidate_set` is rejected, `similar_prior_cases` becomes two installed queries merged in the harness (PLAN §3.7) — note it in contracts.
**Record:** two-type search with mixed candidate set works = ____ ; error text if not = ____ ; `/restpp/vector/status` payload = ____

## 10. `IMPORT PACKAGE GDBMS_ALGO` smoke (for the blog; plain `tg_*.gsql` is the baseline regardless)
```gsql
USE GRAPH SmokeGraph
IMPORT PACKAGE GDBMS_ALGO
SHOW PACKAGE GDBMS_ALGO
CALL GDBMS_ALGO.community.wcc(["Card"], ["SHARES_DEVICE"], "", "", 10, false, false, "", false)
```
Expected: package lists `community.wcc`; the call runs (empty result on the toy graph). **Record:** works on Free tier = ____

## 11. 150 MB POST test (upload limit; chunk size for `export_graph_csvs.py`)
```bash
uv run python - <<'EOF'
import os, time
from ops.ensure_awake import tg_connection, warm_up
conn = tg_connection(); warm_up(conn)
conn.gsql('USE GRAPH SmokeGraph\nCREATE LOADING JOB smoke_load FOR GRAPH SmokeGraph { DEFINE FILENAME f; LOAD f TO VERTEX Customer VALUES ($0, $1) USING separator=",", header="false"; }')
os.makedirs("data/out", exist_ok=True)
with open("data/out/smoke_150mb.csv", "w") as f:
    for i in range(0, 6_000_000): f.write(f"X{i:09d},{i%7}\n")     # ~ 150 MB
print(os.path.getsize("data/out/smoke_150mb.csv")/1e6, "MB")
t0 = time.time()
print(conn.runLoadingJobWithFile("data/out/smoke_150mb.csv", "f", "smoke_load", sep=",", eol="\n", timeout=600000, sizeLimit=200000000))
print("seconds", time.time()-t0, "Customer count", conn.getVertexCount("Customer"))
EOF
```
Expected: accepted (Nginx `ClientMaxBodySize` default 200 MB) in a few minutes; if rejected, use ≤ 50 MB chunks (the ETL default). **Record:** max accepted size = ____ ; seconds per 150 MB = ____

## 12. `uv init`, pins, `.env.example`, first CI run
```bash
make install && make env && make test && make lint      # 23 unit tests, ruff clean
git add -A && git commit -m "day 0: scaffolding" && git push   # CI must be green (mock e2e case skipped until agent/ exists)
```
**Record:** CI run link = ____

## Also on day 0
- Read the workspace **memory chart** once with an empty graph (baseline for the day-2 < 60 % milestone). **Record:** ____ %
- Take note of the Free plan quota: 1 read-write workspace, 1 manual backup, suspend grace 0 days, terminate grace 7 days.
- Drop the smoke schema (`USE GLOBAL` → `DROP ALL`) so day 2 starts from a clean workspace.

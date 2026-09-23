"""Load vector files into TigerGraph and wait for the HNSW index.

    uv run python -m rag.load_vectors --all                           # ClosedCase + PolicyChunk from data/out
    uv run python -m rag.load_vectors --vertex ClosedCase --attr note_emb --psv data/out/vec_ClosedCase.psv
    uv run python -m rag.load_vectors --status                        # print /vector/status for all three attrs
    uv run python -m rag.load_vectors --ensure-schema                 # runs graph/schema_change_vectors.gsql (GLOBAL job) if attrs missing
    uv run python -m rag.load_vectors --dry-run --all                 # print the GSQL, touch nothing

Mechanics (verified against pyTigerGraph 2.0.4 source in mcpvenv and ecosys VectorSearch.md, 2026-09-19):
  * Loading job:  CREATE LOADING JOB load_vec_<vertex>_<attr> FOR GRAPH FraudGraph {
                    DEFINE FILENAME f;
                    LOAD f TO VECTOR ATTRIBUTE <attr> ON VERTEX <vertex> VALUES ($0, SPLIT($1, ",")) USING SEPARATOR="|", HEADER="false";
                  }
    (identical to what tigergraph-mcp's load_vectors_from_csv generates).
  * Upload+run:   conn.runLoadingJobWithFile(filePath, fileTag="f", jobName, sep="|", eol="\\n", timeout=600000, sizeLimit=128000000)
                  -> POST /restpp/ddl/FraudGraph?tag=<job>&filename=f&sep=|&eol=\\n with GSQL-TIMEOUT / RESPONSE-LIMIT headers;
                  the whole file is read into memory, so files are split into <= 40 MB parts (Savanna's Nginx body cap
                  is 200 MB by default; the day-0 150 MB POST test decides whether parts can grow).
  * Vectors load onto EXISTING vertices: the ClosedCase / PolicyChunk vertex loads must run first (graph/load_all.py).
  * Index status:  conn.getVectorIndexStatus(vertexType=, vectorName=) -> GET /restpp/vector/status/FraudGraph/<vertex>/<attr>
                  -> {"NeedRebuildServers": [...]} ; empty list == Ready_for_query (this is what tigergraph-mcp's
                  get_vector_index_status reports). conn.getVectorStatus(vertex, attr) returns the same as a bool.
  * Single-case upsert (P10 persist): conn.upsertVertex("AgentCase", id, {..attrs.., "note_emb": [floats]}) - pyTigerGraph
                  wraps every attribute as {"value": v}, which is the documented REST JSON for vector attributes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config

PART_BYTES = 40 * 1024 * 1024

# The vector attributes are added by the GLOBAL schema-change job in graph/schema_change_vectors.gsql (the vertex
# types are created at USE GLOBAL, and "local schema change jobs cannot alter global vertex types" - gsql-ref 4.2).
# `--ensure-schema` sends that file verbatim (00-integration.md M13); there is no second copy of the DDL here.
SCHEMA_CHANGE_FILE = Path(os.environ.get("HHGOA_SCHEMA_CHANGE_VECTORS", config.REPO_ROOT / "graph" / "schema_change_vectors.gsql"))


DIMENSION_RE = re.compile(r"DIMENSION\s*=\s*\d+")


def render_dimension(text: str, dim: int = 0) -> str:
    """Rewrite every `DIMENSION=<n>` in the schema-change DDL to the configured EMBED_DIM, so switching
    embedding model/backend (e.g. BAAI/bge-small-en-v1.5 at 384-d) needs no edit to the .gsql file.
    The file keeps the default (1024) as its literal, which is what a human reads."""
    return DIMENSION_RE.sub(f"DIMENSION={dim or config.EMBED_DIM}", text)


def schema_change_gsql(path: Path = SCHEMA_CHANGE_FILE, dim: int = 0) -> str:
    """Text of graph/schema_change_vectors.gsql without its `//` comment lines, with DIMENSION rendered
    from config.EMBED_DIM (what conn.gsql() receives)."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - the graph module owns the vector DDL (graph/schema_change_vectors.gsql)")
    lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("//")]
    text = "\n".join(lines).strip() + "\n"
    assert "GLOBAL SCHEMA_CHANGE JOB" in text and "ADD VECTOR ATTRIBUTE" in text, f"{path} is not the vector schema-change job"
    return render_dimension(text, dim)


def loading_job_gsql(vertex: str, attr: str, graph: str = "FraudGraph") -> tuple[str, str]:
    job = f"load_vec_{vertex}_{attr}"
    gsql = (f"USE GRAPH {graph}\n"
            f"CREATE LOADING JOB {job} FOR GRAPH {graph} {{\n"
            f"  DEFINE FILENAME f;\n"
            f"  LOAD f TO VECTOR ATTRIBUTE {attr} ON VERTEX {vertex} VALUES ($0, SPLIT($1, \",\")) "
            f"USING SEPARATOR=\"|\", HEADER=\"false\";\n"
            f"}}\n")
    return job, gsql


def connect():
    """Sync pyTigerGraph connection for offline scripts (Savanna: host https://<id>.i.tgcloud.io + Database Secret)."""
    from pyTigerGraph import TigerGraphConnection

    if not config.TG_HOST or not config.TG_SECRET:
        raise RuntimeError("TG_HOST / TG_SECRET not set")
    conn = TigerGraphConnection(host=config.TG_HOST, graphname=config.TG_GRAPHNAME, gsqlSecret=config.TG_SECRET)
    conn.getToken(config.TG_SECRET)
    conn.customizeHeader(timeout=config.TG_LOAD_TIMEOUT_MS, responseSize=64_000_000)
    return conn


def ensure_awake(fn, *args, attempts: int = 8, **kwargs):
    """Retry through Savanna auto-resume (502/503/connection errors) with 1,2,4..60 s backoff."""
    delay = 1
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if i == attempts - 1 or not any(t in msg for t in ("502", "503", "504", "Connection", "timed out", "Bad Gateway")):
                raise
            print(f"[tg] {type(e).__name__}: {msg[:120]} - retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def split_psv(path: Path, part_bytes: int = PART_BYTES) -> list[Path]:
    if path.stat().st_size <= part_bytes:
        return [path]
    parts: list[Path] = []
    buf, size, k = [], 0, 0
    with path.open() as f:
        for line in f:
            if size + len(line) > part_bytes and buf:
                p = path.with_suffix(f".part{k:02d}.psv")
                p.write_text("".join(buf))
                parts.append(p)
                buf, size, k = [], 0, k + 1
            buf.append(line)
            size += len(line)
    if buf:
        p = path.with_suffix(f".part{k:02d}.psv")
        p.write_text("".join(buf))
        parts.append(p)
    return parts


def vector_attrs_present(conn, vertex: str) -> list[str]:
    """Names of vector attributes on `vertex` (pyTigerGraph getVertexVectors; falls back to LS text)."""
    try:
        vecs = conn.getVertexVectors(vertex)
        return [v.get("Name") or v.get("name") for v in vecs] if isinstance(vecs, list) else []
    except Exception:  # noqa: BLE001
        txt = str(conn.gsql(f"USE GRAPH {config.TG_GRAPHNAME}\nLS"))
        return [a for a in config.VECTOR_ATTRS.values() if f"VECTOR ATTRIBUTE {a}" in txt or f"{a}(" in txt]


def ensure_schema(conn, dry_run: bool = False) -> None:
    missing = [(v, a) for v, a in config.VECTOR_ATTRS.items() if a not in vector_attrs_present(conn, v)]
    if not missing:
        print("vector attributes already present")
        return
    print("adding vector attributes:", missing, "via", SCHEMA_CHANGE_FILE)
    gsql = schema_change_gsql()
    if dry_run:
        print(gsql)
        return
    print(conn.gsql(gsql))


def index_status(conn, vertex: str, attr: str) -> dict:
    """{"ready": bool, "need_rebuild": [...]} from GET /restpp/vector/status/<graph>/<vertex>/<attr>."""
    res = conn.getVectorIndexStatus(vertexType=vertex, vectorName=attr)
    need = res.get("NeedRebuildServers", []) if isinstance(res, dict) else []
    return {"ready": len(need) == 0, "need_rebuild": need, "raw": res}


def wait_index_ready(conn, vertex: str, attr: str, timeout_s: int = 600, poll_s: float = 3.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = ensure_awake(index_status, conn, vertex, attr)
        if st["ready"]:
            print(f"[index] {vertex}.{attr} Ready_for_query after {time.time() - t0:.0f}s")
            return True
        print(f"[index] {vertex}.{attr} Rebuild_processing on {st['need_rebuild']} ...")
        time.sleep(poll_s)
    print(f"[index] {vertex}.{attr} still rebuilding after {timeout_s}s", file=sys.stderr)
    return False


def load_psv(conn, psv: Path, vertex: str, attr: str, dry_run: bool = False) -> dict:
    """Create (or replace) the loading job, upload every part, return the summed loading statistics."""
    job, gsql = loading_job_gsql(vertex, attr, config.TG_GRAPHNAME)
    if dry_run:
        print(gsql)
        print(f"runLoadingJobWithFile({psv}, 'f', '{job}', sep='|', eol='\\n', timeout={config.TG_LOAD_TIMEOUT_MS}, sizeLimit=128000000)")
        return {"dry_run": True}
    try:
        conn.dropLoadingJob(job)
    except Exception:  # noqa: BLE001 - first run
        pass
    r = conn.gsql(gsql)
    if "error" in str(r).lower() and "Successfully created" not in str(r):
        raise RuntimeError(f"loading job create failed: {r}")
    total = {"parts": 0, "valid": 0, "invalid": 0}
    for part in split_psv(psv):
        t0 = time.time()
        res = ensure_awake(conn.runLoadingJobWithFile, str(part), "f", job, sep="|", eol="\n",
                           timeout=config.TG_LOAD_TIMEOUT_MS, sizeLimit=128_000_000)
        stats = (res[0] if isinstance(res, list) and res else res) or {}
        vstats = stats.get("statistics", {}).get("vertex", []) if isinstance(stats, dict) else []
        valid = sum(int(v.get("validObject", 0)) for v in vstats)
        invalid = sum(int(v.get("invalidAttribute", 0)) + int(v.get("noIdFound", 0)) for v in vstats)
        total["parts"] += 1
        total["valid"] += valid
        total["invalid"] += invalid
        print(f"[load] {part.name}: valid={valid} invalid={invalid} in {time.time() - t0:.1f}s; raw={json.dumps(stats)[:300]}")
    return total


def upsert_case_vector(conn, case_id: str, vector: list[float], attributes: dict) -> int:
    """P10 persist: AgentCase attributes AND note_emb in one upsertVertex call (REST JSON {"note_emb": {"value": [...]}})."""
    if len(vector) != config.EMBED_DIM:
        raise ValueError(f"vector has {len(vector)} dims, expected {config.EMBED_DIM}")
    attrs = dict(attributes)
    attrs["note_emb"] = vector
    return ensure_awake(conn.upsertVertex, "AgentCase", case_id, attrs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vertex")
    ap.add_argument("--attr")
    ap.add_argument("--psv", type=Path)
    ap.add_argument("--all", action="store_true", help="ClosedCase.note_emb + PolicyChunk.emb from data/out")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ensure-schema", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args(argv)

    jobs: list[tuple[str, str, Path]] = []
    if a.all:
        jobs = [("ClosedCase", "note_emb", config.OUT_DIR / "vec_ClosedCase.psv"),
                ("PolicyChunk", "emb", config.OUT_DIR / "vec_PolicyChunk.psv")]
    elif a.vertex and a.attr and a.psv:
        jobs = [(a.vertex, a.attr, a.psv)]

    if a.dry_run and not a.status and not a.ensure_schema:
        for v, at, p in jobs:
            load_psv(None, p, v, at, dry_run=True)
        return 0

    conn = ensure_awake(connect)
    if a.ensure_schema:
        ensure_schema(conn, a.dry_run)
    if a.status:
        for v, at in config.VECTOR_ATTRS.items():
            print(v, at, json.dumps(ensure_awake(index_status, conn, v, at)))
        return 0
    for v, at, p in jobs:
        if not p.exists():
            print(f"missing {p}; run rag.embed first", file=sys.stderr)
            return 1
        total = load_psv(conn, p, v, at)
        print(f"[load] {v}.{at}: {total}")
        if not a.no_wait:
            wait_index_ready(conn, v, at)
    return 0


if __name__ == "__main__":
    sys.exit(main())

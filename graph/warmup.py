"""graph/warmup.py — wake the workspace and report its state before a load, a benchmark or the demo.

    uv run python graph/warmup.py                # version, vertex/edge counts, installed queries, vector index status,
                                                 # then the mandatory queries once on HHG-014 (first call after a resume is slow)
    uv run python graph/warmup.py --no-queries   # state only
    uv run python graph/warmup.py --json         # machine-readable (ui/ and tests/live read it)

Exit 0 when the workspace answers; 1 when it is still unreachable after TG_RESUME_MAX_WAIT_S. Nothing is written.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for ops.console
from tg import connect, ensure_awake, run_query, version  # noqa: E402

from ops.console import Col, Table, detail, fail, header, joinlist, ok, summary, warn  # noqa: E402

log = logging.getLogger("graph.warmup")

# HHG-014 (PLAN §2.3): flagged txn 3478561 on card C13487-K1 of customer C13487, opened 2016-11-22 20:11:00.
WARM_QUERIES = [
    ("case_context", {"t": {"id": "3478561"}, "as_of": "2016-11-22 20:11:00"}),
    ("card_profile", {"c": {"id": "C13487-K1"}, "as_of": "2016-11-22 20:11:00"}),
    ("card_window", {"c": {"id": "C13487-K1"}, "from_ts": "2016-11-19 20:11:00", "to_ts": "2016-11-22 20:11:00", "max_rows": 60}),
    ("prior_cases_for_customer", {"cu": {"id": "C13487"}, "as_of": "2016-11-22 20:11:00"}),
    ("ring_profile", {"c": {"id": "C13487-K1"}, "as_of": "2016-11-22 20:11:00"}),
]
VECTOR_ATTRS = {"ClosedCase": "note_emb", "AgentCase": "note_emb", "PolicyChunk": "emb"}


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _n(value) -> str:
    """A count as a right-alignable, thousands-separated cell; non-numbers pass through."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def state(conn) -> dict:
    @ensure_awake()
    def _counts():
        return conn.getVertexCount("*"), conn.getEdgeCount("*")

    @ensure_awake()
    def _queries():
        return sorted(q.split("/")[-1] for q in (conn.getInstalledQueries() or {}))

    out: dict = {"version": version(conn)}
    try:
        v, e = _counts()
        out["vertices"], out["edges"] = v, e
    except Exception as exc:  # noqa: BLE001 - no graph yet (day 0/2)
        out["vertices"], out["edges"], out["graph_error"] = {}, {}, str(exc)[:200]
    try:
        out["installed_queries"] = _queries()
    except Exception as exc:  # noqa: BLE001
        out["installed_queries"], out["queries_error"] = [], str(exc)[:200]
    vec = {}
    for vt, attr in VECTOR_ATTRS.items():
        try:
            res = conn.getVectorIndexStatus(vertexType=vt, vectorName=attr)
            need = res.get("NeedRebuildServers", []) if isinstance(res, dict) else []
            vec[f"{vt}.{attr}"] = "ready" if not need else f"rebuilding on {need}"
        except Exception as exc:  # noqa: BLE001 - attribute not added yet
            vec[f"{vt}.{attr}"] = f"absent ({str(exc)[:80]})"
    out["vector_index"] = vec
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-queries", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if not args.json else logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    if not args.json:
        header(
            "graph.warmup",
            "wake the workspace and report its state before a load, a benchmark or the demo",
            {"queries": "state only" if args.no_queries else f"{len(WARM_QUERIES)} mandatory reads on HHG-014",
             "timeout": "120s", "resume budget": f"{os.getenv('TG_RESUME_MAX_WAIT_S', '240')}s", "writes": "nothing"},
        )
    t0 = time.time()
    try:
        conn = connect(timeout_ms=120_000)
        st = state(conn)
    except Exception as exc:  # noqa: BLE001
        if args.json:
            log.error("workspace unreachable: %s", str(exc)[:300])
        else:
            fail("workspace unreachable")
            detail(str(exc)[:300])
            summary("warmup failed", {"seconds": f"{time.time() - t0:.0f}"}, status="fail")
        return 1
    st["wake_seconds"] = round(time.time() - t0, 1)
    if not args.no_queries:
        timings = {}
        installed = set(st["installed_queries"])
        for name, params in WARM_QUERIES:
            if name not in installed:
                timings[name] = "not installed"
                continue
            t1 = time.time()
            try:
                run_query(conn, name, params, timeout_ms=120_000)
                timings[name] = round(time.time() - t1, 2)
            except Exception as exc:  # noqa: BLE001
                timings[name] = f"error: {str(exc)[:120]}"
        st["warm_queries_s"] = timings
    if args.json:
        # --json is a MACHINE contract (ui/ and tests/live parse stdout): JSON and nothing else.
        print(json.dumps(st, indent=1, default=str))
        return 0

    ok(f"TigerGraph {st['version']} awake in {st['wake_seconds']}s")
    if st.get("graph_error"):
        warn("no graph yet (day 0/2)")
        detail(str(st["graph_error"])[:200])
    counts = Table(Col("type", max_width=22), Col("kind", width=6), Col("count", align="right", width=12),
                   title="vertices and edges in the graph")
    for kind, key in (("vertex", "vertices"), ("edge", "edges")):
        for name, n in sorted((st.get(key) or {}).items(), key=lambda kv: -_int(kv[1])):
            counts.add_row(name, kind, _n(n))
    counts.print()

    idx = Table(Col("vector attribute", max_width=26), Col("status", max_width=60), title="vector index")
    for name, status in st["vector_index"].items():
        idx.add_row(name, status, style=None if status == "ready" else "yellow")
    idx.print()

    if "warm_queries_s" in st:
        warm = Table(Col("query", max_width=28), Col("seconds", align="right", width=9), Col("note", max_width=60),
                     title="mandatory queries, first call after the resume")
        for name, v in st["warm_queries_s"].items():
            if isinstance(v, (int, float)):
                warm.add_row(name, f"{v:.2f}", "" if v < 5 else "slower than the 5 s contract",
                             style=None if v < 5 else "yellow")
            else:
                warm.add_row(name, "-", str(v), style="red" if str(v).startswith("error") else "yellow")
        warm.print()

    installed = st["installed_queries"]
    if st.get("queries_error"):
        warn(f"installed queries unavailable: {str(st['queries_error'])[:160]}")
    summary(
        "workspace warm",
        {"version": st["version"], "wake seconds": st["wake_seconds"],
         "vertex types": len(st.get("vertices") or {}), "edge types": len(st.get("edges") or {}),
         "installed queries": f"{len(installed)}: {joinlist(installed, max_items=6)}",
         "vector index": joinlist([f"{k}={v}" for k, v in st["vector_index"].items()], sep="  ")},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

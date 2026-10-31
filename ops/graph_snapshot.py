"""Record what is in the live TigerGraph graph, with a timestamp.

TigerGraph keeps no load history or snapshots, so this is the only record of what the graph held
and when. Each run prints per-type vertex and edge counts and appends one JSON line to
``logs/graph_snapshots.jsonl``; the diff against the previous snapshot shows what changed.

    uv run python -m ops.graph_snapshot            # snapshot + diff vs the last one
    uv run python -m ops.graph_snapshot --expect Transaction=650938   # exit 1 if it does not match
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from ops import console
from ops.ensure_awake import tg_connection, warm_up

LOG = Path("logs/graph_snapshots.jsonl")


def take(conn) -> dict:
    vertices = {k: v for k, v in conn.getVertexCount("*").items()}
    edges = {k: v for k, v in conn.getEdgeCount("*").items() if not k.startswith("REV_")}
    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "graph": conn.graphname,
        "vertices": vertices,
        "edges": edges,
    }


def last_snapshot() -> dict | None:
    if not LOG.exists():
        return None
    lines = [ln for ln in LOG.read_text().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect", action="append", default=[], metavar="TYPE=N",
                    help="vertex count that must match (repeatable); exit 1 if not")
    ap.add_argument("--no-write", action="store_true", help="print only, do not append to the log")
    args = ap.parse_args(argv)

    conn = tg_connection()
    warm_up(conn)
    snap = take(conn)
    prev = last_snapshot()

    console.ok(f"{snap['graph']} at {snap['at']}")
    for kind in ("vertices", "edges"):
        for name, n in sorted(snap[kind].items()):
            was = (prev or {}).get(kind, {}).get(name)
            delta = "" if was is None or was == n else f"   ({n - was:+,} since {prev['at']})"
            console.step(f"{kind[0].upper()} {name:<28} {n:>10,}{delta}")
    if prev is None:
        console.step("no earlier snapshot to compare against")

    bad = False
    for item in args.expect:
        name, _, want = item.partition("=")
        got = snap["vertices"].get(name, 0)
        if got != int(want):
            console.fail(f"expected {name}={int(want):,}, graph has {got:,}")
            bad = True
    if not args.no_write:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(snap) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

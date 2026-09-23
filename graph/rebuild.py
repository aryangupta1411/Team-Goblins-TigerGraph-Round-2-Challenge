"""graph/rebuild.py — disaster recovery / clean rebuild of FraudGraph from the checked-in chunk and vector files.

    uv run python graph/rebuild.py --from schema                    # schema -> load -> vectors -> algos -> install -> describe
    uv run python graph/rebuild.py --from load                      # the workspace still has the schema
    uv run python graph/rebuild.py --only vectors                   # one stage
    uv run python graph/rebuild.py --only vectors --no-vector-data  # just the GLOBAL schema-change job (vector attributes)
    uv run python graph/rebuild.py --from schema --drop-all --yes   # stale workspace: USE GLOBAL DROP ALL first
    uv run python graph/rebuild.py --list

Stages (each timed; summary on stdout and, with --report, as JSON - the day-2 milestone records the total):
  schema    graph/load_all.py --schema-only [--drop-all --yes]
  load      graph/load_all.py                       jobs re-created, every chunk uploaded, exact counts asserted
  vectors   graph/schema_change_vectors.gsql for the VECTOR attributes that are missing (GLOBAL job; checked with
            conn.getVertexVectors), then `python -m rag.load_vectors --all` (module F) when data/out/vec_*.psv exist
  algos     graph/run_algos.py                      SHARES_DEVICE projection, tg_wcc, degree, wcc_summary
  install   graph/install_all.py                    CREATE OR REPLACE every graph/queries/*.gsql + INSTALL QUERY ALL
  describe  graph/describe_queries.py               updateQueryDescription from mcp/query_descriptions.yaml
Order: `vectors` after `load` because the ALTER VERTEX in the schema-change job disables the loading jobs of the
altered types (load_all re-creates them on its next run anyway); `install` after `algos` so INSTALL QUERY ALL does
not compile the algorithm queries a second time. There is no server-side export on Savanna (no shell), so this
script plus the Free plan's one manual backup is the whole recovery story (PLAN §3.6).
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg import DATA_OUT, GRAPH_DIR, REPO, connect, gsql, is_transient  # noqa: E402

log = logging.getLogger("graph.rebuild")

STAGES = ["schema", "load", "vectors", "algos", "install", "describe"]
VECTOR_ATTRS = {"ClosedCase": "note_emb", "AgentCase": "note_emb", "PolicyChunk": "emb"}   # contracts/schema.md
SCHEMA_CHANGE_GSQL = GRAPH_DIR / "schema_change_vectors.gsql"
VEC_FILES = [DATA_OUT / "vec_ClosedCase.psv", DATA_OUT / "vec_PolicyChunk.psv"]


def run(cmd: list[str]) -> None:
    log.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, cwd=str(REPO), check=True)


def script(name: str, *args: str) -> list[str]:
    return [sys.executable, str(GRAPH_DIR / name), *args]


def vector_attrs(conn, vertex: str) -> list[str]:
    try:
        return [name for name, _ in conn.getVertexVectors(vertex)]
    except Exception as exc:  # noqa: BLE001 - a vertex type without EmbeddingAttributes raises KeyError
        if is_transient(exc):
            raise
        return []


def schema_change_text(missing: dict[str, str]) -> str:
    """graph/schema_change_vectors.gsql with only the ALTER lines for the attributes that are still missing,
    and DIMENSION rendered from EMBED_DIM (rag.load_vectors.render_dimension) so a 384-d local model works
    without editing the .gsql."""
    from rag.load_vectors import render_dimension

    lines = []
    for line in SCHEMA_CHANGE_GSQL.read_text().splitlines():
        s = line.strip()
        if s.startswith("//"):
            continue
        if s.startswith("ALTER VERTEX"):
            vertex = s.split()[2]
            if vertex not in missing:
                continue
        lines.append(line)
    return render_dimension("\n".join(lines) + "\n")


def stage_vectors(args) -> dict:
    conn = connect(timeout_ms=600_000)
    present = {v: vector_attrs(conn, v) for v in VECTOR_ATTRS}
    missing = {v: a for v, a in VECTOR_ATTRS.items() if a not in present[v]}
    out = {"present_before": present, "added": list(missing)}
    if missing:
        log.info("adding vector attributes: %s", missing)
        gsql(conn, schema_change_text(missing), ok_if=("completes in",))
        still = {v: a for v, a in missing.items() if a not in vector_attrs(conn, v)}
        if still:
            raise RuntimeError(f"vector attributes still missing after the schema change: {still}")
    else:
        log.info("vector attributes already present: %s", present)
    if args.no_vector_data:
        return out
    if all(p.exists() for p in VEC_FILES):
        run([sys.executable, "-m", "rag.load_vectors", "--all"])
        out["vectors_loaded"] = True
    else:
        log.warning("no %s - vectors not loaded (rag.embed, day 7)", [p.name for p in VEC_FILES if not p.exists()])
        out["vectors_loaded"] = False
    return out


def run_stage(stage: str, args) -> dict:
    if stage == "schema":
        extra = ["--drop-all", "--yes"] if args.drop_all else []
        run(script("load_all.py", "--schema-only", *extra))
    elif stage == "load":
        run(script("load_all.py"))
    elif stage == "vectors":
        return stage_vectors(args)
    elif stage == "algos":
        run(script("run_algos.py"))
    elif stage == "install":
        run(script("install_all.py"))
    elif stage == "describe":
        run(script("describe_queries.py"))
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--from", dest="start", choices=STAGES, help="run this stage and every later one")
    g.add_argument("--only", choices=STAGES, help="run one stage")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--drop-all", action="store_true", help="schema stage: USE GLOBAL DROP ALL first")
    ap.add_argument("--yes", action="store_true", help="required with --drop-all")
    ap.add_argument("--no-vector-data", action="store_true", help="vectors stage: schema change only")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.list or not (args.start or args.only):
        print("stages, in order:", " -> ".join(STAGES))
        return 0
    if args.drop_all and not args.yes:
        ap.error("--drop-all needs --yes (it wipes the workspace)")
    stages = [args.only] if args.only else STAGES[STAGES.index(args.start):]

    timings: dict[str, dict] = {}
    t_all = time.time()
    for st in stages:
        t0 = time.time()
        log.info("==== stage %s", st)
        try:
            info = run_stage(st, args)
        except Exception as exc:  # noqa: BLE001
            timings[st] = {"seconds": round(time.time() - t0, 1), "ok": False, "error": str(exc)[:500]}
            log.error("stage %s failed after %.0fs: %s", st, time.time() - t0, exc)
            break
        timings[st] = {"seconds": round(time.time() - t0, 1), "ok": True, **info}
    total = round(time.time() - t_all, 1)
    summary = {"stages": timings, "total_seconds": total, "ok": all(v["ok"] for v in timings.values()) and len(timings) == len(stages)}
    for st, v in timings.items():
        print(f"  {st:9s} {'ok ' if v['ok'] else 'ERR'} {v['seconds']:8.1f}s")
    print(f"  total     {'ok ' if summary['ok'] else 'ERR'} {total:8.1f}s")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=1))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

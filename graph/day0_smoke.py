"""graph/day0_smoke.py — runs graph/day0_smoke.gsql block by block on the fresh workspace and fills in the day-0 records.

    uv run python graph/day0_smoke.py                       # every block + toy vectors + two-type search + 3-row tab load
    uv run python graph/day0_smoke.py --post-test-mb 150    # also POST a 150 MB CSV (Nginx body cap; seconds per 150 MB)
    uv run python graph/day0_smoke.py --drop-all            # ... and USE GLOBAL DROP ALL at the end
    uv run python graph/day0_smoke.py --only ddl_all_names two_type_vector_query
    uv run python graph/day0_smoke.py --report docs/day0_records.json

Prints one line per record of docs/day0_checklist.md items 8-11:
  ddl_all_names      every CREATE VERTEX / EDGE of schema.gsql accepted (reserved-word check)
  proxy_reserved     whether `proxy` as an attribute name is rejected (schema.gsql uses proxy_type)
  vectors            GLOBAL schema-change job adding VECTOR attributes accepted
  two_type_search    vectorSearch over {ClosedCase.note_emb, AgentCase.note_emb} with a mixed candidate_set returns
                     one vertex of EACH type (else: similar_prior_cases becomes two queries, PLAN §3.7)
  tab_load           sep="\\t" through POST /ddl and `"` `,` `'` `|#|` inside a field round-trip (module A open issue 1)
  gdbms_algo         IMPORT PACKAGE GDBMS_ALGO / CALL works on the Free tier (blog only)
  post_150mb         accepted size and seconds (chunk-size decision; the ETL default is 45 MiB)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg import GRAPH_DIR, connect, ensure_awake, env, gsql, merged, run_query  # noqa: E402

log = logging.getLogger("graph.day0_smoke")
SMOKE_GSQL = GRAPH_DIR / "day0_smoke.gsql"
GRAPH = "SmokeGraph"
INFORMATIONAL = {"probe_reserved_proxy", "gdbms_algo_import", "gdbms_algo_call"}   # failure is a record, not an error
TOY_VECTORS = [("ClosedCase", "CC-0001", {"outcome": "confirmed_fraud", "note_emb": [0.1, 0.2, 0.3, 0.4]}),
               ("ClosedCase", "CC-0002", {"outcome": "cleared", "note_emb": [0.9, 0.1, 0.1, 0.1]}),
               ("AgentCase", "AC-HHG-014", {"verdict": "fraud", "note_emb": [0.1, 0.2, 0.3, 0.5]})]
TAB_ROWS = [["SMOKE-1", "AC-SMOKE", "1", "2016-11-22 20:11:00", "evidence", '{"a":"b,c","d":"it\'s","e":"x|#|y","f":"F80\'S+ | NULL"}'],
            ["SMOKE-2", "AC-SMOKE", "2", "2016-11-22 20:12:00", "assessment", ""],
            ["SMOKE-3", "AC-SMOKE", "3", "2016-11-22 20:13:00", "sar", "plain, text; with 'quotes' and \"double\""]]


def blocks(text: str) -> list[tuple[str, str]]:
    """[(step name, gsql text)] from the `// ==== STEP <name>` markers."""
    out, name, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"// ==== STEP (\w+)", line)
        if m:
            if name:
                out.append((name, "\n".join(buf)))
            name, buf = m.group(1), []
        elif name and not line.strip().startswith("//"):
            buf.append(line)
    if name:
        out.append((name, "\n".join(buf)))
    return out


def send(conn, name: str, text: str, records: dict) -> bool:
    t0 = time.time()
    try:
        reply = gsql(conn, text, quiet=True)
        ok = True
    except Exception as exc:  # noqa: BLE001
        reply, ok = str(exc), False
    records[name] = {"ok": ok, "seconds": round(time.time() - t0, 1), "reply_tail": reply[-600:]}
    log.info("%-24s %s %.1fs", name, "OK " if ok else ("info" if name in INFORMATIONAL else "ERR"), time.time() - t0)
    if not ok:
        log.info("    %s", reply[-400:].replace("\n", " | "))
    return ok


def wait_index(conn, timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = conn.getVectorIndexStatus(graphName=GRAPH)
        need = st.get("NeedRebuildServers", []) if isinstance(st, dict) else []
        if not need:
            return True
        time.sleep(3)
    return False


def vector_steps(conn, records: dict) -> None:
    for vt, vid, attrs in TOY_VECTORS:
        conn.upsertVertex(vt, vid, attrs)
    records["vector_upsert"] = {"ok": True, "n": len(TOY_VECTORS)}
    records["index_ready"] = {"ok": wait_index(conn)}
    for q in ("smoke_two_type", "smoke_one_type"):
        try:
            res = run_query(conn, q, {"q": [0.1, 0.2, 0.3, 0.45], "k": 2}, timeout_ms=30_000)
            vs = merged(res).get("v", [])
            types = sorted({v.get("v_type") for v in vs})
            ids = sorted(v.get("v_id") for v in vs)
            records[q] = {"ok": True, "types": types, "ids": ids, "raw": json.dumps(res)[:800]}
        except Exception as exc:  # noqa: BLE001
            records[q] = {"ok": False, "error": str(exc)[:600]}
    two = records["smoke_two_type"]
    records["two_type_search"] = {"ok": two.get("ok") and len(two.get("types", [])) == 2, "detail": two}


def tab_load_step(conn, records: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8", newline="") as fh:
        for r in TAB_ROWS:
            fh.write("\t".join(r) + "\n")
        path = fh.name
    try:
        res = conn.runLoadingJobWithFile(path, "f", "smoke_load_tab", sep="\t", eol="\n", timeout=60_000, sizeLimit=128_000_000)
        got = conn.getVerticesById("CaseEvent", [r[0] for r in TAB_ROWS], select="payload,kind,seq")
        by_id = {v["v_id"]: v["attributes"] for v in got}
        ok = all(by_id.get(r[0], {}).get("payload") == r[5] and by_id.get(r[0], {}).get("kind") == r[4] for r in TAB_ROWS)
        records["tab_load"] = {"ok": ok, "stats": json.dumps(res)[:600], "payload_roundtrip": {k: v.get("payload") for k, v in by_id.items()}}
    except Exception as exc:  # noqa: BLE001
        records["tab_load"] = {"ok": False, "error": str(exc)[:600]}
    finally:
        os.unlink(path)


def post_test(conn, mb: int, records: dict) -> None:
    path = Path(tempfile.gettempdir()) / f"smoke_{mb}mb.csv"
    with path.open("w") as fh:
        i = 0
        while path.stat().st_size < mb * 1_000_000 if i % 100_000 == 0 else True:
            fh.write(f"X{i:09d},{i % 7},{i % 11},0\n")
            i += 1
            if i % 100_000 == 0 and fh.flush() is None and path.stat().st_size >= mb * 1_000_000:
                break
    size = path.stat().st_size
    t0 = time.time()
    try:
        res = conn.runLoadingJobWithFile(str(path), "f", "smoke_load_csv", sep=",", eol="\n", timeout=600_000, sizeLimit=200_000_000)
        records["post_150mb"] = {"ok": True, "bytes": size, "seconds": round(time.time() - t0, 1),
                                 "customer_count": conn.getVertexCount("Customer"), "stats": json.dumps(res)[:400]}
    except Exception as exc:  # noqa: BLE001
        records["post_150mb"] = {"ok": False, "bytes": size, "seconds": round(time.time() - t0, 1), "error": str(exc)[:600]}
    finally:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--drop-all", action="store_true")
    ap.add_argument("--post-test-mb", type=int, default=0)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    records: dict = {}
    conn = connect(graphname=GRAPH, timeout_ms=600_000, token=False)   # DDL first: Basic secret auth, no graph yet
    steps = blocks(SMOKE_GSQL.read_text())
    for name, text in steps:
        if name == "drop_all" or (args.only and name not in args.only):
            continue
        send(conn, name, text, records)
        if name == "ddl_all_names" and records[name]["ok"]:
            ensure_awake()(conn.getToken)(env("TG_SECRET"))            # REST++ (upserts, loads, counts) needs a token
        if name == "two_type_vector_query" and records[name]["ok"]:
            vector_steps(conn, records)
        if name == "loading_jobs" and records[name]["ok"]:
            tab_load_step(conn, records)
            if args.post_test_mb:
                post_test(conn, args.post_test_mb, records)
    if args.drop_all:
        send(conn, "drop_all", dict(steps)["drop_all"], records)

    summary = {
        "ddl_all_names": records.get("ddl_all_names", {}).get("ok"),
        "proxy_reserved": (not records["probe_reserved_proxy"]["ok"]) if "probe_reserved_proxy" in records else None,
        "vectors": records.get("vectors_global_job", {}).get("ok"),
        "two_type_search": records.get("two_type_search", {}).get("ok"),
        "one_type_search": records.get("smoke_one_type", {}).get("ok"),
        "tab_load": records.get("tab_load", {}).get("ok"),
        "gdbms_algo": records.get("gdbms_algo_import", {}).get("ok") and records.get("gdbms_algo_call", {}).get("ok"),
        "post_150mb": records.get("post_150mb"),
    }
    print("\nday-0 records:")
    for k, v in summary.items():
        print(f"  {k:18s} {v}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"summary": summary, "records": records}, indent=1, default=str))
    hard = ["ddl_all_names", "vectors", "tab_load"]
    return 0 if all(summary.get(k) for k in hard if k in records or k == "tab_load") else 1


if __name__ == "__main__":
    sys.exit(main())

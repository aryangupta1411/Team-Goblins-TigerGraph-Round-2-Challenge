"""graph/run_algos.py — the once-only graph-algorithm pass (PLAN §3.5 "Graph algorithms", day 4).

    uv run python graph/run_algos.py                        # install the six algo queries, project, wcc, degree, summary
    uv run python graph/run_algos.py --no-install           # queries already installed (re-run after a data reload)
    uv run python graph/run_algos.py --no-project           # keep the existing SHARES_DEVICE edges
    uv run python graph/run_algos.py --report runs/algos.json

Every call is graph/tg.run_query -> runInstalledQuery(name, params, timeout=600000, usePost=True), never the MCP.
  1. clear_shares_device()                                     drops every SHARES_DEVICE edge (idempotent re-run)
  2. project_shares_device(60, true)                           SHARES_DEVICE over strong profiles (n_shared, weight, via)
  3. tg_wcc(["Card"], ["SHARES_DEVICE"], 0, false, "wcc_id")   Card.wcc_id (library query, SYNTAX V1, file verbatim)
  4. tg_degree_cent(["Card"], ["SHARES_DEVICE"], ["SHARES_DEVICE"], true, false, 20, true, "device_degree", "", false)
                                                               Card.device_degree + top-20 hubs; the library stores a
                                                               DOUBLE via setAttr - if the server rejects DOUBLE->INT the
                                                               step is reported and step 5 is the result
  5. set_device_degree(true)                                   exact INT degree; wcc_id = -1 for isolated cards
  6. wcc_summary()                                             component sizes + the ring cards' component (blog numbers)
The library files are sent with `CREATE QUERY` rewritten to `CREATE OR REPLACE QUERY` at send time so the checked-in
text stays verbatim (algos/README.md) and re-runs need no DROP QUERY. Card.fraud_ppr stays 0 (tg_pagerank_pers is a
PLAN stretch item and is not fetched).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg import GRAPH_DIR, connect, ensure_awake, gsql, merged, run_query  # noqa: E402

log = logging.getLogger("graph.run_algos")

GRAPH = "FraudGraph"
ALGOS = GRAPH_DIR / "algos"
OURS = ["clear_shares_device", "project_shares_device", "set_device_degree", "wcc_summary"]   # USE GRAPH + CREATE OR REPLACE
LIBRARY = ["tg_wcc", "tg_degree_cent"]                                                        # verbatim, no USE GRAPH
RING_PROFILE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
RING_CARD = "C13487-K1"                                                                       # HHG-014


def install(conn) -> None:
    for name in OURS:
        gsql(conn, (ALGOS / f"{name}.gsql").read_text(), ok_if=("successfully created",))
    for name in LIBRARY:
        text = (ALGOS / f"{name}.gsql").read_text().replace("CREATE QUERY", "CREATE OR REPLACE QUERY", 1)
        gsql(conn, f"USE GRAPH {GRAPH}\n{text}", ok_if=("successfully created",))
    t0 = time.time()
    gsql(conn, f"USE GRAPH {GRAPH}\nINSTALL QUERY {', '.join(OURS + LIBRARY)}", ok_if=("query installation finished",))
    log.info("installed %s in %.0fs", OURS + LIBRARY, time.time() - t0)


def step(conn, report: dict, name: str, params: dict) -> dict:
    t0 = time.time()
    res = merged(run_query(conn, name, params, timeout_ms=600_000))
    dt = round(time.time() - t0, 1)
    report[name] = {"seconds": dt, "params": params, "result": res}
    log.info("%s %s -> %s (%.1fs)", name, json.dumps(params)[:120], json.dumps(res)[:400], dt)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-install", action="store_true")
    ap.add_argument("--no-project", action="store_true")
    ap.add_argument("--max-cards-per-profile", type=int, default=60)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect(timeout_ms=600_000)
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not args.no_install:
        install(conn)

    if not args.no_project:
        step(conn, report, "clear_shares_device", {})
        proj = step(conn, report, "project_shares_device", {"max_cards_per_profile": args.max_cards_per_profile, "do_insert": True})
        if not proj.get("n_inserts"):
            log.error("project_shares_device inserted nothing (%s) - is DeviceProfile.is_strong loaded?", proj)
            return 1

    step(conn, report, "tg_wcc", {"v_type_set": ["Card"], "e_type_set": ["SHARES_DEVICE"], "print_limit": 0,
                                  "print_results": False, "result_attribute": "wcc_id", "file_path": ""})

    try:
        step(conn, report, "tg_degree_cent", {"v_type_set": ["Card"], "e_type_set": ["SHARES_DEVICE"], "reverse_e_type_set": ["SHARES_DEVICE"],
                                              "in_degree": True, "out_degree": False, "top_k": 20, "print_results": True,
                                              "result_attribute": "device_degree", "file_path": "", "normalize": False})
    except Exception as exc:  # noqa: BLE001 - DOUBLE -> INT setAttr rejected: set_device_degree below is the result
        report["tg_degree_cent"] = {"error": str(exc)[:500]}
        log.warning("tg_degree_cent failed (%s); set_device_degree provides Card.device_degree", str(exc)[:160])

    step(conn, report, "set_device_degree", {"reset_isolated_wcc": True})
    summary = step(conn, report, "wcc_summary", {"ring_profile_id": RING_PROFILE, "top_n": 10})

    @ensure_awake()
    def _facts():
        return {"SHARES_DEVICE": conn.getEdgeCount("SHARES_DEVICE"),
                "ring_card": conn.getVerticesById("Card", RING_CARD, select="wcc_id,device_degree,ring_id")}

    facts = _facts()
    report["facts"] = facts
    top = summary.get("top_components", [])
    print(json.dumps({"largest_components": top[:3], "n_components": summary.get("n_components"),
                      "n_components_ge2": summary.get("n_components_ge2"), "n_isolated_cards": summary.get("n_isolated_cards"),
                      "ring_card_components": summary.get("ring_card_components"), "shares_device_edges": facts["SHARES_DEVICE"],
                      "ring_card": facts["ring_card"]}, indent=1, default=str))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

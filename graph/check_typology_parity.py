"""graph/check_typology_parity.py -- the graph's typology_scan against its DuckDB twin, case by case.

Both sides feed engine.detectors.hit_from_scan, so a difference is a difference in the waves each one finds. READ-ONLY:
by default the query is sent as an INTERPRETED query (nothing is installed on the workspace); `--installed` calls the
installed query instead (after `make install-queries`).

    uv run python graph/check_typology_parity.py                 # the 18 HHF cases + 12 other January cards
    uv run python graph/check_typology_parity.py --extra 60      # more January cards (false-positive hunt)
    uv run python graph/check_typology_parity.py --installed     # against the installed query

Exit 1 on any difference. Checked per case: the wave rows of D1-D4 (ids), the D4 domain, the card's own rows, and the hit
(detector, episode, exposure, connected cards, probability floor).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import duckdb  # noqa: E402

from engine import config  # noqa: E402
from engine.detectors import TYPOLOGY_DATA_START, episode_rows, hit_from_scan, scan_rows  # noqa: E402
from graph import tg  # noqa: E402
from ops.console import detail, fail, header, ok, summary  # noqa: E402

QUERY = REPO / "graph" / "queries" / "typology_scan.gsql"


def interpreted_text() -> str:
    text = "\n".join(line for line in QUERY.read_text().splitlines() if not line.strip().startswith("USE GRAPH"))
    return re.sub(r"CREATE OR REPLACE QUERY\s+\w+\(", "INTERPRET QUERY (", text, count=1)


def graph_scan(conn, params: dict, installed: bool) -> dict:
    if installed:
        return tg.merged(tg.run_query(conn, "typology_scan", {**params, "c": {"id": params["c"]}, "t": {"id": params["t"]}}, timeout_ms=120_000))
    return tg.merged(conn.runInterpretedQuery(interpreted_text(), params))


def wave_ids(scan: dict) -> dict:
    return {
        **{k: sorted(str(r["id"]) for r in scan.get(k) or []) for k in ("d1_rows", "d2_rows", "d3_rows", "d4_rows")},
        "d4_domain": scan.get("d4_domain") or "",
        "card_rows": sorted(str(r["id"]) for r in scan.get("card_rows") or []),
    }


def hit_key(hit) -> dict | None:
    if hit is None:
        return None
    return {"detector": hit.detector_id, "episode": sorted(hit.episode_txn_ids), "exposure": hit.exposure_usd,
            "connected": sorted(hit.connected_card_ids), "p_floor": hit.p_floor}


def cases(con, extra: int) -> list[tuple[str, str, str, str]]:
    rows = con.execute("SELECT case_id, card_id, CAST(flagged_txn_id AS VARCHAR), strftime(opened_at, '%Y-%m-%d %H:%M:%S') FROM cp_all WHERE case_id LIKE 'HHF-%' ORDER BY case_id").fetchall()
    if extra:
        # January transactions of cards that are in no HHF case, spread over the month: the false-positive side of the check
        seen = {r[1] for r in rows}
        for card, tid, ts in con.execute(
            "SELECT card_id, id, strftime(ts, '%Y-%m-%d %H:%M:%S') FROM txc WHERE ts >= '2017-01-05' AND product_cd = 'C' AND channel = 'online' ORDER BY hash(id)"
        ).fetchall():
            if card not in seen and len(rows) < 18 + extra:
                seen.add(card)
                rows.append((f"JAN-{tid}", card, tid, ts))
    return [tuple(map(str, r)) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--extra", type=int, default=12, help="other January cards to compare (default 12)")
    ap.add_argument("--installed", action="store_true", help="call the installed query instead of interpreting the file")
    ap.add_argument("--json", help="write the per-case comparison to this file")
    args = ap.parse_args()

    header("graph.check_typology_parity", "typology_scan on TigerGraph vs the DuckDB twin",
           {"mode": "installed query" if args.installed else "interpreted (read-only, nothing installed)", "facts db": config.FACTS_DB})
    con = duckdb.connect(str(config.FACTS_DB), read_only=True)
    conn = tg.connect(timeout_ms=330_000)
    todo = cases(con, args.extra)
    bad = hits = 0
    report = []
    for case_id, card, tid, as_of in todo:
        t0 = time.time()
        params = {"c": card, "t": tid, "as_of": as_of, "data_start": TYPOLOGY_DATA_START}
        g = graph_scan(conn, params, args.installed)
        d = scan_rows(con, card, tid, as_of, TYPOLOGY_DATA_START)
        gh, dh = hit_from_scan(g, card, tid, as_of), hit_from_scan(d, card, tid, as_of)
        diffs = [f"{k}: graph {len(gv) if isinstance(gv, list) else gv} vs duckdb {len(dv) if isinstance(dv, list) else dv}"
                 for k, gv, dv in ((k, wave_ids(g)[k], wave_ids(d)[k]) for k in wave_ids(g)) if gv != dv]
        if hit_key(gh) != hit_key(dh):
            diffs.append(f"hit: graph {hit_key(gh)} vs duckdb {hit_key(dh)}")
        elif gh is not None and sorted(r["id"] for r in episode_rows(g, gh)) != sorted(r["id"] for r in episode_rows(d, dh)):
            diffs.append("episode rows differ")
        hits += gh is not None
        report.append({"case": case_id, "card": card, "txn": tid, "hit": hit_key(gh), "ok": not diffs, "diffs": diffs, "seconds": round(time.time() - t0, 1)})
        if diffs:
            bad += 1
            fail(f"{case_id} {card}: {len(diffs)} difference(s)")
            for x in diffs:
                detail(x)
        else:
            ok(f"{case_id:<14} {card:<11} {(gh.detector_id if gh else 'no hit'):<28} {time.time() - t0:4.1f}s")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1))
    summary("typology parity", {"cases": len(todo), "graph hits": hits, "differences": bad}, status="fail" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Upload the Round 2 (January 2017) delta files to the live TigerGraph workspace (`FraudGraph`).

    uv run python graph/load_round2_delta.py             # uploads data/out/round2_delta/*.csv if TG_HOST/TG_SECRET set
    uv run python graph/load_round2_delta.py --dry-run   # offline verification of the delta files and job mappings
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "graph"))

from ops.console import Col, Table, header, ok, summary, warn  # noqa: E402

DELTA_JOBS = [
    ("txn_2017q1.csv", "load_txn"),
    ("closed_case_delta.csv", "load_closed_case"),
    ("involves_delta.csv", "load_involves"),
    ("on_card_delta.csv", "load_on_card"),
    ("connected_to_delta.csv", "load_connected_to"),
    ("matches_delta.csv", "load_matches"),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upload Round 2 delta TSVs to TigerGraph FraudGraph")
    ap.add_argument("--delta-dir", default=str(ROOT / "data" / "out" / "round2_delta"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ddir = Path(args.delta_dir)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    host = os.getenv("TG_HOST", "")
    secret = os.getenv("TG_SECRET", "")
    live = bool(host and secret and "<workspace" not in host and not args.dry_run)

    header(
        "graph.load_round2_delta",
        "Incremental Round 2 (Jan 2017) delta uploader for TigerGraph FraudGraph",
        {"delta_dir": str(ddir), "host": host or "(unset)", "mode": "live" if live else "dry-run"},
    )

    t = Table(Col("file", max_width=26), Col("job", max_width=22), Col("rows", align="right", width=10), Col("status", max_width=18), title="Round 2 TigerGraph delta")
    conn = None
    if live:
        from ops.ensure_awake import tg_connection, warm_up

        conn = tg_connection()
        warm_up(conn)

    t0 = time.time()
    for fname, job in DELTA_JOBS:
        p = ddir / fname
        if not p.exists():
            t.add_row(fname, job, "0", "MISSING", style="red")
            continue
        with p.open("rb") as fh:
            n_rows = sum(1 for _ in fh)
        if live and conn is not None and n_rows > 0:
            conn.runLoadingJobWithFile(str(p), "f", job, sep="\t", eol="\n", timeout=600_000, sizeLimit=128_000_000)
            t.add_row(fname, job, f"{n_rows:,}", "UPLOADED", style="green")
        else:
            t.add_row(fname, job, f"{n_rows:,}", "READY (dry-run)")
    t.print()
    if live:
        ok(f"Uploaded Round 2 delta to {host} in {time.time() - t0:.1f}s")
    else:
        warn("Dry-run complete (set TG_HOST and TG_SECRET in .env to upload live)")
    summary("load_round2_delta", {"mode": "live" if live else "dry-run", "elapsed": f"{time.time() - t0:.1f}s"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

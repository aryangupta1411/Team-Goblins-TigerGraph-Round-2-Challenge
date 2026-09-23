"""graph/keep_alive.py — keep the Savanna workspace awake during loads, benchmarks and the demo recording.

    uv run python graph/keep_alive.py                 # every 180 s until Ctrl-C
    uv run python graph/keep_alive.py --once          # single ping (= "wake the workspace", prints the version)
    uv run python graph/keep_alive.py --interval 120 --for 5400

Savanna auto-stop counts running queries as activity, not controller calls, so the ping is a real installed query
(default prior_cases_for_customer on the HHG-014 customer, contract 2A; KEEP_ALIVE_QUERY / KEEP_ALIVE_PARAMS override).
Same behaviour as ops/keep_alive.py (module G); this copy depends only on graph/tg.py so a checkout with graph/ and
data/ alone can run day 2. Falls back to getVer() when the query is not installed yet (day 2, before install_all).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for ops.console
from tg import connect, run_query, version  # noqa: E402

from ops.console import fail, header, ok, step, summary  # noqa: E402

DEFAULT_QUERY = "prior_cases_for_customer"
DEFAULT_PARAMS = {"cu": {"id": "C13487"}, "as_of": "2016-12-31 23:59:59"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=float(os.getenv("KEEP_ALIVE_INTERVAL_S", "180")))
    ap.add_argument("--for", dest="duration", type=float, default=0.0, help="seconds to run (0 = until Ctrl-C)")
    ap.add_argument("--query", default=os.getenv("KEEP_ALIVE_QUERY", DEFAULT_QUERY))
    ap.add_argument("--params", default=os.getenv("KEEP_ALIVE_PARAMS", json.dumps(DEFAULT_PARAMS)))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    # graph/tg.py echoes GSQL replies at INFO; WARNING keeps the resume retries and hard errors on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    params = json.loads(args.params)
    header(
        "graph.keep_alive",
        "keeps the Savanna workspace awake (a running query counts as activity, a controller call does not)",
        {"mode": "single ping" if args.once else "loop until Ctrl-C", "query": args.query,
         "interval": "n/a" if args.once else f"{args.interval:.0f}s",
         "duration": "one ping" if args.once else (f"{args.duration:.0f}s" if args.duration else "until Ctrl-C")},
    )
    step("waking the workspace (ensure_awake blocks through the 502 resume window)")
    conn = connect(timeout_ms=30_000)
    ver = version(conn)                                   # ensure_awake: blocks through the 502 resume window
    installed = {q.split("/")[-1] for q in (conn.getInstalledQueries() or {})} if not args.once else set()
    use_query = args.once or args.query in installed
    ok(f"workspace awake ({ver}); pinging {args.query if use_query else 'getVer()'} every {args.interval:.0f}s")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    started, n, failures = time.time(), 0, 0
    while not stop["flag"]:
        t0 = time.time()
        try:
            res = run_query(conn, args.query, params, timeout_ms=30_000) if use_query else version(conn)
            n += 1
            what = f"{len(res)} result objects" if isinstance(res, list) else str(res)
            ok(f"ping {n} in {time.time() - t0:.2f}s ({what}); awake {time.time() - started:.0f}s")
        except Exception as exc:  # noqa: BLE001 - keep looping, report loudly
            failures += 1
            fail(f"ping failed after {time.time() - t0:.2f}s: {str(exc)[:200]}")
        if args.once or (args.duration and time.time() - started >= args.duration):
            break
        for _ in range(int(args.interval)):
            if stop["flag"]:
                break
            time.sleep(1)
    summary(
        "keep-alive stopped",
        {"pings": n, "failures": failures, "kept awake": f"{time.time() - started:.0f}s",
         "query": args.query if use_query else "getVer()"},
        status="ok" if not failures else "warn",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

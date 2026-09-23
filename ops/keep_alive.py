"""Keep a Savanna workspace awake during a benchmark run or a demo recording.

Savanna's auto-suspend counts *running queries* as activity (release note
2024-08-27), so a trivial installed query every 3 minutes prevents the
auto-stop that would otherwise cost a 502 + resume in the middle of a case.
Controller-API calls do not count, so we run a real query.

Usage::

    uv run python -m ops.keep_alive                     # every 180 s until Ctrl-C
    uv run python -m ops.keep_alive --interval 120 --for 5400   # 90 minutes then exit
    make keepalive

The query defaults to ``prior_cases_for_customer`` (contract 2A, always
installed) on the HHG-014 customer; override with KEEP_ALIVE_QUERY and
KEEP_ALIVE_PARAMS (JSON) if module D ships a dedicated ``ping`` query.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

from ops.ensure_awake import run_query, tg_connection, warm_up

log = logging.getLogger("ops.keep_alive")

DEFAULT_QUERY = "prior_cases_for_customer"
DEFAULT_PARAMS = {"cu": "C13487", "as_of": "2016-12-31 23:59:59"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=float(os.getenv("KEEP_ALIVE_INTERVAL_S", "180")))
    ap.add_argument("--for", dest="duration", type=float, default=0.0, help="seconds to run (0 = until Ctrl-C)")
    ap.add_argument("--query", default=os.getenv("KEEP_ALIVE_QUERY", DEFAULT_QUERY))
    ap.add_argument("--params", default=os.getenv("KEEP_ALIVE_PARAMS", json.dumps(DEFAULT_PARAMS)))
    ap.add_argument("--once", action="store_true", help="single ping (used by `make awake`)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    params = json.loads(args.params)
    conn = tg_connection()
    ver = warm_up(conn)
    log.info("workspace awake (%s); pinging %s every %.0fs", ver, args.query, args.interval)

    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    started = time.time()
    n = 0
    while not stop["flag"]:
        t0 = time.time()
        try:
            res = run_query(conn, args.query, params, timeout_ms=30_000)
            n += 1
            log.info("ping %d ok in %.2fs (%d result objects)", n, time.time() - t0, len(res))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, report loudly
            log.error("ping failed: %s", exc)
        if args.once or (args.duration and time.time() - started >= args.duration):
            break
        # sleep in 1 s slices so Ctrl-C is prompt
        for _ in range(int(args.interval)):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("keep-alive stopped after %d pings", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

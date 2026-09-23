"""graph/install_all.py — (re)creates and installs the whole GSQL query library in ONE compile.

    uv run python graph/install_all.py                # all files in graph/queries/*.gsql
    uv run python graph/install_all.py card_window ring_profile   # only these (still one INSTALL QUERY ALL)

Each .gsql file is a self-contained `USE GRAPH FraudGraph / CREATE OR REPLACE QUERY ...` batch; they are sent
one by one through pyTigerGraph conn.gsql() (GSQL server, not RESTPP: the 16 s timer does not apply, but the
launcher-style customizeHeader timeout does bound the HTTP call, so we set 600 s here), then
`INSTALL QUERY ALL` compiles everything that is created-but-not-installed (1–3 min on TG-00).
Exit code 1 if any CREATE reports an error, so the day-3/day-4 Makefile targets fail loudly.
"""

from __future__ import annotations

import glob
import os
import sys
import time

from dotenv import load_dotenv
from pyTigerGraph import TigerGraphConnection

HERE = os.path.dirname(os.path.abspath(__file__))
QDIR = os.path.join(HERE, "queries")
SKIP = {"install_all.gsql"}


def connect() -> TigerGraphConnection:
    load_dotenv()
    conn = TigerGraphConnection(
        host=os.environ["TG_HOST"],
        graphname=os.environ.get("TG_GRAPHNAME", "FraudGraph"),
        gsqlSecret=os.environ.get("TG_SECRET", ""),
        username=os.environ.get("TG_USERNAME", ""),
        password=os.environ.get("TG_PASSWORD", ""),
        restppPort=os.environ.get("TG_RESTPP_PORT", "14240"),
        gsPort=os.environ.get("TG_GS_PORT", "14240"),
        tgCloud=os.environ.get("TG_TGCLOUD", "true").lower() == "true",
    )
    if os.environ.get("TG_SECRET"):
        conn.getToken(os.environ["TG_SECRET"])
    conn.customizeHeader(timeout=600_000, responseSize=64_000_000)
    return conn


def gsql_ok(text: str) -> bool:
    bad = ("Syntax error", "Semantic Check Fails", "Encountered", "failed", "Error:", "error:")
    return not any(b in text for b in bad)


def main() -> int:
    conn = connect()
    names = [a for a in sys.argv[1:] if a != "--only"]  # `--only a b` (module F's runbook) == `a b`
    files = sorted(glob.glob(os.path.join(QDIR, "*.gsql")))
    files = [f for f in files if os.path.basename(f) not in SKIP and (not names or os.path.basename(f)[:-5] in names)]
    failed = []
    for f in files:
        t = time.time()
        res = conn.gsql(open(f).read())
        ok = gsql_ok(res)
        print(f"{'OK ' if ok else 'ERR'} create {os.path.basename(f):32s} {time.time() - t:5.1f}s")
        if not ok:
            failed.append(os.path.basename(f))
            print(res)
    t = time.time()
    res = conn.gsql("USE GRAPH FraudGraph\nINSTALL QUERY ALL")
    print(f"install all: {time.time() - t:.0f}s\n{res[-2000:]}")
    if failed:
        print("FAILED:", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

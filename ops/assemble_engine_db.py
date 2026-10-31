"""Assemble the engine facts DB of record for base + January (`data/hhgoa_engine.duckdb`).

`make delta` re-derives every table over July 2016 to January 2017 (`data/hhgoa_engine_r2.duckdb`, the same build the live
graph was loaded from). This copies those tables and adds the case-pack and data-quality tables the schema engine keeps
(`cp` = the 20 Round 1 cases, `cp_round1`, `cp_round2`, `cp_all`, `dq_issues`, `schema_attestations`), so the engine, the
detectors and the validator read exactly what the graph holds.

    uv run python -m ops.assemble_engine_db --facts data/hhgoa_engine_r2.duckdb --extra-from data/hhgoa_engine.duckdb --out data/hhgoa_engine.duckdb
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import duckdb

from ops.console import header, ok, summary

FACT_TABLES = ("txc", "card_feat", "customer_feat", "device_profile", "cc", "cc_txn")
EXTRA_TABLES = ("cp", "cp_round1", "cp_round2", "cp_all", "dq_issues", "schema_attestations")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--facts", type=Path, required=True, help="re-derived facts DB (make delta)")
    ap.add_argument("--extra-from", type=Path, required=True, help="DB holding the case-pack / DQ tables")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    header("ops.assemble_engine_db", "engine facts DB for base + month", {"facts": args.facts, "extra from": args.extra_from, "out": args.out})
    with tempfile.TemporaryDirectory() as td:
        extra = Path(td) / "extra.duckdb"
        shutil.copy(args.extra_from, extra)            # --out may be the same file as --extra-from
        tmp = Path(td) / "out.duckdb"
        con = duckdb.connect(str(tmp))
        con.execute(f"ATTACH '{args.facts}' AS f (READ_ONLY)")
        con.execute(f"ATTACH '{extra}' AS x (READ_ONLY)")
        counts = {}
        for t in FACT_TABLES:
            con.execute(f"CREATE TABLE {t} AS SELECT * FROM f.{t}")
            counts[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        have = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'x'").fetchall()}
        for t in EXTRA_TABLES:
            if t in have:
                con.execute(f"CREATE TABLE {t} AS SELECT * FROM x.{t}")
                counts[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        n_card = con.execute("SELECT sum(n_txns) FROM card_feat").fetchone()[0]
        if n_card != counts["txc"]:
            raise SystemExit(f"card_feat covers {n_card} transactions but txc has {counts['txc']}: the facts DB is not consistent")
        con.execute("DETACH f")
        con.execute("DETACH x")
        con.execute("CHECKPOINT")
        con.close()
        shutil.move(str(tmp), args.out)
    ok(f"wrote {args.out}")
    summary("engine facts DB assembled", {k: f"{v:,}" for k, v in counts.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

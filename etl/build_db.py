"""etl/build_db.py — raw CSVs -> DuckDB (contracts §D: tables tx, tx_raw, idn, cc, cp, pairs, cardmap).

    python -m etl.build_db --raw data/raw --db data/hhgoa.duckdb

tx_raw keeps all 397 columns as VARCHAR (fidelity; the scorer reads V/C/D from it),
tx is the typed slim view used everywhere else, `pairs` is the labelled (case, transaction)
set used by the card-rule regression test, and `cardmap` is the card_id rule (etl/card_rule.py).
Runtime on an M-series Mac: ~20 s.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from etl.card_rule import build_cardmap, check_rule

TX_COLS = """
  TransactionID::BIGINT AS TransactionID,
  TransactionDT::BIGINT AS TransactionDT,
  TransactionAmt::DOUBLE AS TransactionAmt,
  ProductCD, card1, card2, card3, card4, card5, card6, addr1, addr2, dist1, dist2, P_emaildomain, R_emaildomain,
  C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14,
  D1,D2,D3,D4,D5,D6,D7,D8,D9,D10,D11,D12,D13,D14,D15,
  M1,M2,M3,M4,M5,M6,M7,M8,M9,
  customer_id, ts::TIMESTAMP AS ts, ts AS ts_str, channel, risk_score::DOUBLE AS risk_score,
  row_number() OVER () AS file_row
"""


def build(raw: str, db: str) -> None:
    t0 = time.time()
    con = duckdb.connect(db)
    con.execute("PRAGMA threads=8; SET memory_limit='8GB';")
    f = lambda name: os.path.join(raw, name)  # noqa: E731
    con.execute(f"""
        CREATE OR REPLACE TABLE tx_raw AS
        SELECT * FROM read_csv('{f("transactions.csv")}', header=true, all_varchar=true, sample_size=-1, quote='"', escape='"')
    """)
    print("tx_raw rows:", con.execute("SELECT count(*) FROM tx_raw").fetchone()[0], f"{time.time()-t0:.0f}s", flush=True)
    con.execute(f"CREATE OR REPLACE TABLE tx AS SELECT {TX_COLS} FROM tx_raw")
    con.execute(f"""
        CREATE OR REPLACE TABLE idn AS
        SELECT * FROM read_csv('{f("identity.csv")}', header=true, all_varchar=true, sample_size=-1)
    """)
    con.execute("ALTER TABLE idn ALTER TransactionID TYPE BIGINT")
    con.execute(f"CREATE OR REPLACE TABLE cc AS SELECT * FROM read_csv('{f('closed_cases_history.csv')}', header=true, all_varchar=true, sample_size=-1)")
    con.execute(f"CREATE OR REPLACE TABLE cp AS SELECT * FROM read_csv('{f('case_pack.csv')}', header=true, all_varchar=true, sample_size=-1)")
    con.execute("""
        CREATE OR REPLACE TABLE pairs AS
        SELECT case_id, card_id, customer_id, unnest(string_split(txn_ids, '|'))::BIGINT AS TransactionID, 'closed' AS src FROM cc
        UNION ALL
        SELECT case_id, card_id, customer_id, flagged_txn_id::BIGINT, 'casepack' FROM cp
    """)
    n_cards = build_cardmap(con)
    n, ok = check_rule(con)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tx", "idn", "cc", "cp", "pairs", "cardmap")}
    print("counts:", counts, "| card rule:", f"{ok}/{n}", f"| {time.time()-t0:.0f}s")
    assert counts["tx"] == 590_742 and counts["idn"] == 144_432 and counts["cc"] == 5_565 and counts["cp"] == 20
    assert n == ok == 14_975 and n_cards == 14_317
    con.close()


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--db", default="data/hhgoa.duckdb")
    a = ap.parse_args()
    build(a.raw, a.db)

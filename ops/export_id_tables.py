"""Export an id-only DuckDB (``data/ids.duckdb``, ~30 MB) so the answer-file
validator (agent/validator.py -> engine.validator.validate(answer, db, meta)) can
resolve every id in CI and on a laptop without the 708 MB raw CSVs or the full
hhgoa.duckdb.

Tables (all ids as STRING, exactly the strings that appear in answer files):
  txn(id, card_id, customer_id, ts TIMESTAMP, amt DOUBLE, p_email, r_email, addr1)   590,742 rows
  card(id, customer_id)                                      14,317
  customer(id)                                               13,553
  device_profile(id)                                          9,705   "DeviceInfo | id_30 | id_31 | id_33"
  closed_case(id, outcome, pattern)                           5,565
  case_pack(*)                                                   20
Views with the column names engine.validator queries (00-integration.md M4):
  txc(TransactionID BIGINT, id, card_id, customer_id, ts, amt, p_email, r_email, addr1)  = txn
  card_feat(id) = card · customer_feat(id) = customer · cc(case_id, outcome, pattern) = closed_case
  (device_profile already carries the validator's column name `id`)

Derivations follow contracts/schema.md verbatim (card_id dense_rank on card6,
device profile with literal NULL parts, all-NULL identity rows excluded);
p_email / r_email / addr1 are the raw strings ('' for NULL) that the answer's
`entity_ids` may name (email domains, billing regions).

Usage:  uv run python -m ops.export_id_tables [--src data/hhgoa.duckdb] [--out data/ids.duckdb]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

CARDMAP = """
SELECT customer_id, coalesce(card6,'') AS c6,
       dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6,'')) AS k
FROM (SELECT DISTINCT customer_id, card6 FROM src.tx)
"""

SQL = [
    f"""CREATE TABLE txn AS
        SELECT t.TransactionID::VARCHAR AS id,
               t.customer_id || '-K' || cm.k AS card_id,
               t.customer_id,
               t.ts::TIMESTAMP AS ts,
               t.TransactionAmt::DOUBLE AS amt,
               coalesce(t.P_emaildomain, '') AS p_email,
               coalesce(t.R_emaildomain, '') AS r_email,
               coalesce(t.addr1::VARCHAR, '') AS addr1
        FROM src.tx t
        JOIN ({CARDMAP}) cm ON cm.customer_id = t.customer_id AND cm.c6 = coalesce(t.card6,'')""",
    "CREATE TABLE card AS SELECT DISTINCT card_id AS id, customer_id FROM txn ORDER BY 1",
    "CREATE TABLE customer AS SELECT DISTINCT customer_id AS id FROM txn ORDER BY 1",
    """CREATE TABLE device_profile AS
        SELECT DISTINCT coalesce(DeviceInfo,'NULL') || ' | ' || coalesce(id_30,'NULL') || ' | ' ||
                        coalesce(id_31,'NULL') || ' | ' || coalesce(id_33,'NULL') AS id
        FROM src.idn
        WHERE NOT (DeviceInfo IS NULL AND id_30 IS NULL AND id_31 IS NULL AND id_33 IS NULL)""",
    "CREATE TABLE closed_case AS SELECT case_id AS id, outcome, pattern FROM src.cc",
    "CREATE TABLE case_pack AS SELECT * FROM src.cp",
    # views with the column names engine.validator.validate() queries (txc.TransactionID / p_email / r_email / addr1,
    # card_feat.id, customer_feat.id, cc.case_id, device_profile.id)
    """CREATE VIEW txc AS SELECT id::BIGINT AS TransactionID, id, card_id, customer_id, ts, amt, p_email, r_email, addr1 FROM txn""",
    "CREATE VIEW card_feat AS SELECT id, customer_id FROM card",
    "CREATE VIEW customer_feat AS SELECT id FROM customer",
    "CREATE VIEW cc AS SELECT id AS case_id, outcome, pattern FROM closed_case",
]
VIEWS = {"txc": 590_742, "card_feat": 14_317, "customer_feat": 13_553, "cc": 5_565}

EXPECTED = {"txn": 590_742, "card": 14_317, "customer": 13_553, "device_profile": 9_705, "closed_case": 5_565, "case_pack": 20}


def export(src: str, out: str) -> dict[str, int]:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    for stmt in SQL:
        con.execute(stmt)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in EXPECTED}
    for v, n in VIEWS.items():
        got = con.execute(f"SELECT count(*) FROM {v}").fetchone()[0]
        assert got == n, f"view {v}: {got} rows, expected {n}"
    con.execute("DETACH src")
    con.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=os.getenv("HHGOA_DB", "data/hhgoa.duckdb"))
    ap.add_argument("--out", default=os.getenv("VALIDATOR_DB", "data/ids.duckdb"))
    args = ap.parse_args(argv)
    t0 = time.time()
    counts = export(args.src, args.out)
    ok = True
    for t, n in counts.items():
        flag = "ok" if n == EXPECTED[t] else f"EXPECTED {EXPECTED[t]:,}"
        ok &= n == EXPECTED[t]
        print(f"{t:15s} {n:>9,}  {flag}")
    print(f"views: {', '.join(VIEWS)} (txc/card_feat/customer_feat/cc for engine.validator)")
    print(f"wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB) in {time.time()-t0:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

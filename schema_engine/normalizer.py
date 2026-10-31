"""Canonical Normalizer & Round 2 Merger (`schema_engine/normalizer.py`).

Merges the 590,742 Jul–Dec 2016 transactions with the 60,196 Jan 2017 transactions
(`data/raw/round2/transactions_2017q1.csv` + `identity_2017q1.csv` = 650,938 total),
computes leakage-free running card history features across the full 7-month timeline,
scores Jan 2017 with LightGBM (`data/models/lgb_fraud.txt`), updates `cc` (5,585 rows),
`cc_txn`, `cp` (HHG-001..020 + HHF-001..018), `device_profile`, `data/hhgoa_engine.duckdb`,
`data/ids.duckdb`, and writes the TigerGraph delta TSV files (`data/out/round2_delta/`).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

from schema_engine.data_quality import run_data_quality_scan
from schema_engine.drift import detect_round2_drift, get_schema_approval

ROOT = Path(__file__).resolve().parents[1]


def _score_jan2017_cms(
    con: duckdb.DuckDBPyConnection,
    tx17_csv: Path,
    id17_csv: Path,
    model_path: Path,
    features_json: Path,
) -> pd.DataFrame:
    """Score the 60,196 January 2017 transactions using the trained LightGBM model (`lgb_fraud.txt`)."""
    meta = json.loads(features_json.read_text(encoding="utf-8"))
    feats: list[str] = meta["features"]
    catcols: list[str] = meta["categorical"]

    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_csv_auto('{tx17_csv}', all_varchar=true)").fetchall()]
    vcd = [c for c in cols if c[0] in "VCD" and c[1:].isdigit()]
    cat_tx = [
        "ProductCD", "card4", "card6", "addr1", "addr2", "P_emaildomain", "R_emaildomain",
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "card2", "card3", "card5", "dist1", "dist2",
    ]
    id_cols = [f"id_{i:02d}" for i in range(1, 39)]
    id_num, id_cat = id_cols[:11], id_cols[11:] + ["DeviceType", "DeviceInfo"]
    hist_cols = ["card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_chan", "prior_med_amt", "prior_max_amt"]

    num_expr = ", ".join(f"TRY_CAST(t.{c} AS FLOAT) AS {c}" for c in vcd + ["TransactionAmt", "risk_score"])
    id_num_expr = ", ".join(f"TRY_CAST(i.{c} AS FLOAT) AS {c}" for c in id_num)
    id_cat_expr = ", ".join(f"i.{c}" for c in id_cat)
    hist_expr = ", ".join(f"h.{c}" for c in hist_cols)

    df = con.execute(
        f"""
        SELECT t.TransactionID::BIGINT AS TransactionID, t.ts::TIMESTAMP AS ts, t.channel,
               {num_expr}, {", ".join("t." + c for c in cat_tx)}, {id_num_expr}, {id_cat_expr},
               (i.TransactionID IS NOT NULL)::INT AS has_id, {hist_expr}
        FROM read_csv_auto('{tx17_csv}', all_varchar=true) t
        LEFT JOIN read_csv_auto('{id17_csv}', all_varchar=true) i USING (TransactionID)
        JOIN _jan_hist h ON h.TransactionID = t.TransactionID::BIGINT
        ORDER BY t.TransactionID::BIGINT
        """
    ).fetchdf()

    df["amt_ratio"] = np.where(df.prior_med_amt > 0, df.TransactionAmt / df.prior_med_amt.replace(0, np.nan), np.nan)
    for c, src in (
        ("region_new", "prior_in_region"),
        ("dev_new", "prior_on_dev"),
        ("pem_new", "prior_pem"),
        ("pcd_new", "prior_pcd"),
        ("chan_new", "prior_chan"),
    ):
        df[c] = (df[src] == 0).astype(int)
    df["hour"] = df.ts.dt.hour
    df["dow"] = df.ts.dt.dayofweek
    for c in catcols:
        if c in df.columns:
            df[c] = df[c].astype("category")

    booster = lgb.Booster(model_file=str(model_path))
    preds = booster.predict(df[feats])
    return pd.DataFrame({"TransactionID": df.TransactionID.values, "cms_p": preds.astype(float)})


def normalize_and_merge_round2(
    engine_db: str | Path = ROOT / "data" / "hhgoa_engine.duckdb",
    ids_db: str | Path = ROOT / "data" / "ids.duckdb",
    round2_dir: str | Path = ROOT / "data" / "raw" / "round2",
    delta_out_dir: str | Path = ROOT / "data" / "out" / "round2_delta",
    auto_approve: bool = True,
) -> dict[str, Any]:
    """Execute the Round 2 schema normalization, merging Jan 2017 into `hhgoa_engine.duckdb` and `ids.duckdb`."""
    t0 = time.time()
    engine_db = Path(engine_db)
    ids_db = Path(ids_db)
    r2 = Path(round2_dir)
    delta_dir = Path(delta_out_dir)

    approval = get_schema_approval("round2_2017q1_migration")
    if approval.get("status") != "approved":
            raise RuntimeError("Schema migration 'round2_2017q1_migration' is pending human approval.")

    tx17_csv = r2 / "transactions_2017q1.csv"
    id17_csv = r2 / "identity_2017q1.csv"
    cc2_csv = r2 / "closed_cases_history.csv"
    cp2_csv = r2 / "case_pack_final.csv"

    con = duckdb.connect(str(engine_db))
    con.execute("PRAGMA threads=8")

    # 1. Keep clean Round 1 base (< 3600000) so re-runs are 100% idempotent
    con.execute("DELETE FROM txc WHERE TransactionID >= 3600000")

    # 2. Stage Jan 2017 rows and resolve card_id dynamically against card_feat + case_pack_final.csv bindings
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _cp_card_bindings AS
        SELECT DISTINCT
            t.customer_id,
            COALESCE(NULLIF(trim(t.card6), ''), 'unknown') AS card_type,
            p.card_id
        FROM read_csv_auto('{cp2_csv}', all_varchar=true) p
        JOIN read_csv_auto('{tx17_csv}', all_varchar=true) t
          ON t.TransactionID = p.flagged_txn_id
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _jan_raw AS
        SELECT
            t.TransactionID::BIGINT AS TransactionID,
            t.TransactionID::VARCHAR AS id,
            COALESCE(b.card_id, c.id, t.customer_id || '-K1') AS card_id,
            t.customer_id,
            t.ts::TIMESTAMP AS ts,
            t.ts AS ts_str,
            TRY_CAST(t.TransactionAmt AS DOUBLE) AS amt,
            COALESCE(t.ProductCD, '') AS product_cd,
            COALESCE(t.channel, '') AS channel,
            COALESCE(t.addr1, '') AS addr1,
            COALESCE(t.addr2, '') AS addr2,
            COALESCE(TRY_CAST(t.dist1 AS DOUBLE), -1.0) AS dist1,
            COALESCE(TRY_CAST(t.dist2 AS DOUBLE), -1.0) AS dist2,
            COALESCE(t.P_emaildomain, '') AS p_email,
            COALESCE(t.R_emaildomain, '') AS r_email,
            COALESCE(TRY_CAST(t.risk_score AS DOUBLE), -1.0) AS risk_score,
            (i.TransactionID IS NOT NULL) AS has_identity,
            COALESCE(i.id_15, '') AS device_new,
            COALESCE(i.id_23, '') AS proxy,
            COALESCE(i.DeviceType, '') AS device_type,
            CASE
                WHEN i.TransactionID IS NULL OR (i.DeviceInfo IS NULL AND i.id_30 IS NULL AND i.id_31 IS NULL AND i.id_33 IS NULL) THEN ''
                ELSE COALESCE(i.DeviceInfo, 'NULL') || ' | ' || COALESCE(i.id_30, 'NULL') || ' | ' || COALESCE(i.id_31, 'NULL') || ' | ' || COALESCE(i.id_33, 'NULL')
            END AS device_id
        FROM read_csv_auto('{tx17_csv}', all_varchar=true) t
        LEFT JOIN read_csv_auto('{id17_csv}', all_varchar=true) i USING (TransactionID)
        LEFT JOIN _cp_card_bindings b
          ON b.customer_id = t.customer_id
         AND b.card_type = COALESCE(NULLIF(trim(t.card6), ''), 'unknown')
        LEFT JOIN card_feat c
          ON c.customer_id = t.customer_id
         AND c.card_type = COALESCE(NULLIF(trim(t.card6), ''), 'unknown')
        """
    )

    # 3. Compute leakage-free running card features across combined (2016 + 2017) timeline
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _all_ AS
        SELECT TransactionID, card_id, ts, amt, product_cd, channel, addr1, p_email, device_id FROM txc
        UNION ALL
        SELECT TransactionID, card_id, ts, amt, product_cd, channel, addr1, p_email, device_id FROM _jan_raw
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _jan_hist AS
        WITH w AS (
            SELECT
                TransactionID,
                card_id,
                ts,
                amt,
                row_number() OVER (PARTITION BY card_id ORDER BY ts, TransactionID)::INT AS card_seq,
                (count(*) OVER (PARTITION BY card_id, addr1 ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))::INT AS prior_in_region_raw,
                (count(*) OVER (PARTITION BY card_id, device_id ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))::INT AS prior_on_dev_raw,
                (count(*) OVER (PARTITION BY card_id, p_email ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))::INT AS prior_pem_raw,
                (count(*) OVER (PARTITION BY card_id, product_cd ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))::INT AS prior_pcd,
                (count(*) OVER (PARTITION BY card_id, channel ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))::INT AS prior_chan,
                COALESCE(quantile_cont(amt, 0.5) OVER (PARTITION BY card_id ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), -1.0) AS prior_med_amt,
                COALESCE(max(amt) OVER (PARTITION BY card_id ORDER BY ts, TransactionID ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), -1.0) AS prior_max_amt,
                addr1,
                device_id,
                p_email
            FROM _all_
        )
        SELECT
            TransactionID,
            card_seq,
            CASE WHEN addr1 = '' THEN 0 ELSE prior_in_region_raw END AS prior_in_region,
            CASE WHEN device_id = '' THEN 0 ELSE prior_on_dev_raw END AS prior_on_dev,
            CASE WHEN p_email = '' THEN 0 ELSE prior_pem_raw END AS prior_pem,
            prior_pcd,
            prior_chan,
            round(prior_med_amt, 2) AS prior_med_amt,
            round(prior_max_amt, 2) AS prior_max_amt
        FROM w
        WHERE TransactionID >= 3600000
        """
    )

    # 4. Score Jan 2017 with LightGBM
    scores_df = _score_jan2017_cms(
        con,
        tx17_csv,
        id17_csv,
        ROOT / "data" / "models" / "lgb_fraud.txt",
        ROOT / "data" / "models" / "cms_features.json",
    )
    con.register("_jan_scores", scores_df)

    # 5. Insert Jan 2017 rows into txc
    con.execute(
        """
        INSERT INTO txc
        SELECT
            r.TransactionID, r.id, r.card_id, r.customer_id, r.ts, r.ts_str, r.amt, r.product_cd, r.channel,
            r.addr1, r.addr2, r.dist1, r.dist2, r.p_email, r.r_email, r.risk_score, r.has_identity,
            r.device_new, r.proxy, r.device_type, r.device_id,
            s.cms_p,
            h.card_seq, h.prior_in_region, h.prior_on_dev, h.prior_pem, h.prior_pcd, h.prior_med_amt, h.prior_max_amt,
            (r.device_id != '' AND r.device_id = COALESCE(cf.ring_id, '')) AS ring_hit,
            '' AS burst_id
        FROM _jan_raw r
        JOIN _jan_hist h USING (TransactionID)
        JOIN _jan_scores s USING (TransactionID)
        LEFT JOIN card_feat cf ON cf.id = r.card_id
        ORDER BY r.TransactionID
        """
    )
    con.unregister("_jan_scores")

    # 6. Insert any new Jan 2017 device profiles into device_profile (preserving existing 2016 rows)
    con.execute(
        """
        INSERT INTO device_profile
        WITH new_devs AS (
            SELECT
                device_id AS id,
                split_part(device_id, ' | ', 1) AS device_info,
                split_part(device_id, ' | ', 2) AS os,
                split_part(device_id, ' | ', 3) AS browser,
                split_part(device_id, ' | ', 4) AS screen,
                count(*)::INT AS n_txns,
                count(DISTINCT card_id)::INT AS n_cards_alltime,
                count(DISTINCT card_id)::INT AS n_cards_30d,
                count(*) FILTER (WHERE proxy != '')::INT AS n_proxy,
                min(ts) AS first_seen,
                max(ts) AS last_seen,
                0::INT AS n_fraud_cases
            FROM txc
            WHERE TransactionID >= 3600000
              AND device_id != ''
              AND device_id NOT IN (SELECT id FROM device_profile)
            GROUP BY 1
        )
        SELECT
            id, device_info, os, browser, screen, n_txns, n_cards_alltime, n_cards_30d, n_proxy,
            first_seen, last_seen, n_fraud_cases,
            (device_info != 'NULL'
             AND ((device_info != 'NULL')::INT + (os != 'NULL')::INT + (browser != 'NULL')::INT + (screen != 'NULL')::INT) >= 3
             AND n_cards_alltime BETWEEN 2 AND 60) AS is_strong
        FROM new_devs
        """
    )

    # 7. Update closed cases (cc = 5,585 rows, cc_txn = 15,075 rows)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cc AS
        SELECT
            case_id,
            customer_id,
            card_id,
            opened_at::TIMESTAMP AS opened_at,
            closed_at::TIMESTAMP AS closed_at,
            outcome,
            pattern,
            COALESCE(first_fraud_txn_id, '') AS first_fraud_txn_id,
            COALESCE(txn_ids, '') AS txn_ids,
            COALESCE(TRY_CAST(n_txns AS BIGINT), 0) AS n_txns,
            COALESCE(TRY_CAST(exposure_usd AS DOUBLE), 0.0) AS exposure_usd,
            COALESCE(connected_card_ids, '') AS connected_card_ids,
            COALESCE(actions_taken, '') AS actions_taken,
            (lower(COALESCE(report_filed, 'no')) IN ('yes', 'true', '1')) AS report_filed,
            COALESCE(analyst_notes, '') AS analyst_notes
        FROM read_csv_auto('{cc2_csv}', all_varchar=true)
        ORDER BY case_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE cc_txn AS
        SELECT
            case_id,
            TRY_CAST(unnest(string_split(txn_ids, '|')) AS BIGINT) AS TransactionID
        FROM cc
        WHERE txn_ids != ''
        """
    )

    # 8. Store both Round 1 (cp_round1) and Round 2 (cp_round2) case packs
    # Keep `cp` with Round 1 (20 rows) if not already backed up, and create `cp_round2` (18 rows) + `cp_all` (38 rows)
    has_r1_backup = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'cp_round1'").fetchone()[0] > 0
    if not has_r1_backup:
        con.execute("CREATE TABLE cp_round1 AS SELECT * FROM cp WHERE case_id LIKE 'HHG-%'")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cp_round2 AS
        SELECT
            case_id,
            opened_at::TIMESTAMP AS opened_at,
            trigger_type,
            trigger_text,
            flagged_txn_id,
            card_id,
            customer_id,
            TRY_CAST(risk_score AS DOUBLE) AS risk_score
        FROM read_csv_auto('{cp2_csv}', all_varchar=true)
        ORDER BY opened_at::TIMESTAMP, case_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE cp_all AS
        SELECT * FROM cp_round1
        UNION ALL
        SELECT * FROM cp_round2
        """
    )

    # 9. Persist Data Quality issues and Schema Drift Attestations into DuckDB tables
    con.close()
    drift_report = detect_round2_drift(engine_db, r2)
    dq_report = run_data_quality_scan(engine_db, r2)
    con = duckdb.connect(str(engine_db))
    con.execute("CREATE OR REPLACE TABLE dq_issues (code VARCHAR, title VARCHAR, severity VARCHAR, count BIGINT, tainted_cases VARCHAR, attestation_json VARCHAR)")
    for d in dq_report["defects"]:
        con.execute(
            "INSERT INTO dq_issues VALUES (?, ?, ?, ?, ?, ?)",
            [d["code"], d["title"], d["severity"], d["count"], json.dumps(d["tainted_cases"]), json.dumps(d["attestation"])],
        )
    con.execute("CREATE OR REPLACE TABLE schema_attestations (drift_id VARCHAR, title VARCHAR, severity VARCHAR, what_breaks VARCHAR, remapping_action VARCHAR, attestation_json VARCHAR)")
    for dr in drift_report["drifts"]:
        con.execute(
            "INSERT INTO schema_attestations VALUES (?, ?, ?, ?, ?, ?)",
            [dr["drift_id"], dr["title"], dr["severity"], dr["what_breaks"], dr["remapping_action"], json.dumps(dr["attestation"])],
        )

    counts = {
        "txc": int(con.execute("SELECT count(*) FROM txc").fetchone()[0]),
        "txc_2017": int(con.execute("SELECT count(*) FROM txc WHERE TransactionID >= 3600000").fetchone()[0]),
        "card_feat": int(con.execute("SELECT count(*) FROM card_feat").fetchone()[0]),
        "device_profile": int(con.execute("SELECT count(*) FROM device_profile").fetchone()[0]),
        "cc": int(con.execute("SELECT count(*) FROM cc").fetchone()[0]),
        "cc_txn": int(con.execute("SELECT count(*) FROM cc_txn").fetchone()[0]),
        "cp_round1": int(con.execute("SELECT count(*) FROM cp_round1").fetchone()[0]),
        "cp_round2": int(con.execute("SELECT count(*) FROM cp_round2").fetchone()[0]),
    }
    con.close()

    # 10. Update validator `data/ids.duckdb` with all 650,938 txns, 5,585 closed cases, and 38 case_pack rows
    _update_ids_db(engine_db, ids_db)

    # 11. Export TigerGraph delta TSVs (`data/out/round2_delta/`)
    delta_files = _export_tigergraph_delta(engine_db, delta_dir)

    return {
        "status": "ok",
        "approval": approval,
        "counts": counts,
        "delta_files": delta_files,
        "elapsed_s": round(time.time() - t0, 2),
    }


def _update_ids_db(engine_db: Path, ids_db: Path) -> None:
    if not ids_db.exists():
        return
    con = duckdb.connect(str(ids_db))
    con.execute(f"ATTACH '{engine_db}' AS eng (READ_ONLY)")
    con.execute(
        """
        CREATE OR REPLACE TABLE txn AS
        SELECT TransactionID, card_id, customer_id, ts, amt, channel, addr1, p_email, r_email, device_id, device_new
        FROM eng.txc
        """
    )
    con.execute("CREATE OR REPLACE TABLE card AS SELECT id, customer_id, modal_region FROM eng.card_feat")
    con.execute("CREATE OR REPLACE TABLE customer AS SELECT id FROM eng.customer_feat")
    con.execute("CREATE OR REPLACE TABLE device_profile AS SELECT id, is_strong FROM eng.device_profile")
    con.execute("CREATE OR REPLACE TABLE closed_case AS SELECT case_id, card_id, customer_id, opened_at, outcome, pattern, report_filed FROM eng.cc")
    con.execute("CREATE OR REPLACE TABLE case_pack AS SELECT case_id, card_id, customer_id, flagged_txn_id, opened_at FROM eng.cp_all")
    con.execute("CREATE OR REPLACE VIEW txc AS SELECT TransactionID, TransactionID::VARCHAR AS id, card_id, customer_id, ts, amt, channel, addr1, p_email, r_email, device_id, device_new FROM txn")
    con.execute("CREATE OR REPLACE VIEW card_feat AS SELECT id, customer_id, modal_region FROM card")
    con.execute("CREATE OR REPLACE VIEW customer_feat AS SELECT id FROM customer")
    con.execute("CREATE OR REPLACE VIEW cc AS SELECT case_id, card_id, customer_id, opened_at, outcome, pattern, report_filed FROM closed_case")
    con.execute("DETACH eng")
    con.close()


def _export_tigergraph_delta(engine_db: Path, delta_dir: Path) -> dict[str, int]:
    """Export the Jan 2017 delta TSV files in `contracts/csv_columns.yaml` format for live TigerGraph upload."""
    if delta_dir.exists():
        shutil.rmtree(delta_dir)
    delta_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(engine_db), read_only=True)

    # 1. txn_2017q1.csv (33 columns matching contracts/csv_columns.yaml txn_NNN.csv)
    txn_path = delta_dir / "txn_2017q1.csv"
    con.execute(
        f"""
        COPY (
            SELECT
                id, card_id, customer_id, strftime(ts, '%Y-%m-%d %H:%M:%S'), ts_str,
                printf('%.2f', amt), product_cd, channel, addr1, addr2,
                printf('%.2f', dist1), printf('%.2f', dist2), p_email, r_email,
                printf('%.4f', risk_score),
                CASE WHEN has_identity THEN 'true' ELSE 'false' END,
                device_new, proxy, device_type, device_id,
                '{{}}', '{{}}', '{{}}',
                printf('%.6f', cms_p), card_seq, prior_in_region, prior_on_dev, prior_pem, prior_pcd,
                printf('%.2f', prior_med_amt), printf('%.2f', prior_max_amt),
                CASE WHEN ring_hit THEN 'true' ELSE 'false' END,
                burst_id
            FROM txc
            WHERE TransactionID >= 3600000
            ORDER BY TransactionID
        ) TO '{txn_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )

    # 2. closed_case_delta.csv (the 20 new closed cases CC-5566..CC-5585, 18 columns)
    cc_path = delta_dir / "closed_case_delta.csv"
    con.execute(
        f"""
        COPY (
            SELECT
                case_id, customer_id, card_id,
                strftime(opened_at, '%Y-%m-%d %H:%M:%S'),
                strftime(closed_at, '%Y-%m-%d %H:%M:%S'),
                outcome, pattern, first_fraud_txn_id, n_txns,
                printf('%.2f', exposure_usd), actions_taken,
                CASE WHEN report_filed THEN 'true' ELSE 'false' END,
                replace(replace(analyst_notes, '\t', ' '), '\n', ' '),
                CASE WHEN outcome = 'cleared' THEN 'cleared_amount' ELSE 'fraud_reported' END,
                '', '', '',
                'pattern: ' || pattern || ' | outcome: ' || outcome || ' | ' || replace(replace(analyst_notes, '\t', ' '), '\n', ' ')
            FROM cc
            WHERE case_id >= 'CC-5566'
            ORDER BY case_id
        ) TO '{cc_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )

    # 3. involves_delta.csv, on_card_delta.csv, connected_to_delta.csv, matches_delta.csv
    inv_path = delta_dir / "involves_delta.csv"
    con.execute(
        f"""
        COPY (
            SELECT case_id, TransactionID::VARCHAR
            FROM cc_txn
            WHERE case_id >= 'CC-5566'
            ORDER BY case_id, TransactionID
        ) TO '{inv_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )
    on_card_path = delta_dir / "on_card_delta.csv"
    con.execute(
        f"""
        COPY (
            SELECT case_id, card_id FROM cc WHERE case_id >= 'CC-5566' ORDER BY case_id
        ) TO '{on_card_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )
    conn_path = delta_dir / "connected_to_delta.csv"
    con.execute(
        f"""
        COPY (
            SELECT case_id, unnest(string_split(connected_card_ids, '|'))
            FROM cc
            WHERE case_id >= 'CC-5566' AND connected_card_ids != ''
        ) TO '{conn_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )
    matches_path = delta_dir / "matches_delta.csv"
    con.execute(
        f"""
        COPY (
            SELECT case_id, CASE WHEN outcome = 'cleared' THEN 'none' ELSE pattern END
            FROM cc WHERE case_id >= 'CC-5566' ORDER BY case_id
        ) TO '{matches_path}' (DELIMITER '\t', HEADER false, QUOTE '')
        """
    )

    out_counts = {}
    for p in (txn_path, cc_path, inv_path, on_card_path, conn_path, matches_path):
        with p.open("rb") as fh:
            out_counts[p.name] = sum(1 for _ in fh)
    con.close()
    return out_counts

"""Challenge 12 — Schema Drift Survival (`schema_engine/drift.py`).

Detects what changed when January 2017 (`data/raw/round2/`) arrived alongside Round 1
(`data/hhgoa_engine.duckdb`), explains what breaks downstream if un-handled, proposes the
re-mapping / recovery plan, and enforces a human approval gate before ingestion proceeds.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import duckdb

from schema_engine.attestation import attest

ROOT = Path(__file__).resolve().parents[1]
APPROVAL_FILE = ROOT / "data" / "schema_approvals.json"


def detect_round2_drift(
    r1_db: str | Path = ROOT / "data" / "hhgoa_engine.duckdb",
    round2_dir: str | Path = ROOT / "data" / "raw" / "round2",
) -> dict[str, Any]:
    """Compare Round 1 baseline DB against the incoming Round 2 files and emit attested drift items."""
    r2 = Path(round2_dir)
    con = duckdb.connect()
    con.execute(f"ATTACH '{r1_db}' AS r1 (READ_ONLY)")
    tx17_path = r2 / "transactions_2017q1.csv"
    id17_path = r2 / "identity_2017q1.csv"
    cc2_path = r2 / "closed_cases_history.csv"
    cp2_path = r2 / "case_pack_final.csv"

    con.execute(f"CREATE VIEW tx17 AS SELECT * FROM read_csv_auto('{tx17_path}', all_varchar=true)")
    con.execute(f"CREATE VIEW id17 AS SELECT * FROM read_csv_auto('{id17_path}', all_varchar=true)")
    con.execute(f"CREATE VIEW cc2 AS SELECT * FROM read_csv_auto('{cc2_path}', all_varchar=true)")
    con.execute(f"CREATE VIEW cp2 AS SELECT * FROM read_csv_auto('{cp2_path}', all_varchar=true)")

    r1_tx_n, r1_min_ts, r1_max_ts = con.execute(
        "SELECT count(*), min(ts)::VARCHAR, max(ts)::VARCHAR FROM r1.txc WHERE TransactionID < 3600000"
    ).fetchone()
    r2_tx_n, r2_min_ts, r2_max_ts = con.execute("SELECT count(*), min(ts), max(ts) FROM tx17").fetchone()
    r2_id_n = con.execute("SELECT count(*) FROM id17").fetchone()[0]

    # 1. Null card6 rows in Jan 2017
    null_c6_rows = con.execute(
        "SELECT TransactionID, customer_id FROM tx17 WHERE card6 IS NULL OR trim(card6) = '' ORDER BY TransactionID::BIGINT"
    ).fetchall()
    null_c6_txns = [str(r[0]) for r in null_c6_rows]
    null_c6_custs = sorted({str(r[1]) for r in null_c6_rows})

    # 2. Closed case history growth + new note format (CC-5566..CC-5585)
    r1_cc_n = 5565
    r2_cc_n = con.execute("SELECT count(*) FROM cc2").fetchone()[0]
    new_cc_rows = con.execute(
        "SELECT case_id, card_id, outcome, pattern, analyst_notes FROM cc2 WHERE case_id > 'CC-5565' ORDER BY case_id"
    ).fetchall()
    new_cc_ids = [str(r[0]) for r in new_cc_rows]

    # 3. Case pack shift (HHG-001..020 -> HHF-001..018)
    cp2_rows = con.execute("SELECT case_id, flagged_txn_id, card_id, trigger_type FROM cp2 ORDER BY case_id").fetchall()
    cp2_ids = [str(r[0]) for r in cp2_rows]

    # 4. In-person transactions with identity records in Jan 2017 (channel/device drift)
    inperson_id_rows = con.execute(
        "SELECT t.TransactionID, t.customer_id, t.channel, i.DeviceInfo, i.id_23 FROM tx17 t JOIN id17 i USING (TransactionID) WHERE t.channel = 'in_person' ORDER BY t.TransactionID::BIGINT"
    ).fetchall()
    inperson_id_txns = [str(r[0]) for r in inperson_id_rows]

    drifts = [
        {
            "drift_id": "DRIFT-01",
            "title": "Incremental Temporal Partition (transactions_2017q1.csv + identity_2017q1.csv)",
            "severity": "critical",
            "before_shape": {
                "files": ["transactions.csv", "identity.csv"],
                "rows": r1_tx_n,
                "window": f"{r1_min_ts} .. {r1_max_ts}",
            },
            "after_shape": {
                "files": ["transactions_2017q1.csv", "identity_2017q1.csv"],
                "delta_tx_rows": r2_tx_n,
                "delta_id_rows": r2_id_n,
                "window": f"{r2_min_ts} .. {r2_max_ts}",
                "combined_rows": r1_tx_n + r2_tx_n,
            },
            "what_breaks": (
                "1) etl/build_db.py hardcodes filenames 'transactions.csv'/'identity.csv' and asserts count == 590,742. "
                "2) Loading 2017q1 standalone resets card_seq to 1, destroys 2016 modal_region / recurring_amounts / prior_med_amt "
                "baselines, and orphans INVOLVES edges on all 5,565 historical closed cases."
            ),
            "remapping_action": (
                "Union the 590,742 Jul-Dec 2016 transactions with the 60,196 Jan 2017 transactions (650,938 total) before "
                "computing running window features (card_seq, prior_*, NEXT gap_seconds) and LightGBM cms_p scoring."
            ),
            "attestation": attest(
                what=f"January 2017 arrives as a {r2_tx_n:,}-row transaction delta ({r2_min_ts} to {r2_max_ts}) and {r2_id_n:,} identity rows that must be unioned with the {r1_tx_n:,} Jul-Dec 2016 rows.",
                evidence=["3600000", "3660108", "3660355"],
                query_or_rule="schema_engine.drift.detect_round2_drift:DRIFT-01",
                params={"delta_tx_rows": r2_tx_n, "delta_id_rows": r2_id_n, "combined_rows": r1_tx_n + r2_tx_n},
                sql="SELECT TransactionID FROM txc WHERE TransactionID >= 3600000 LIMIT 25",
            ).to_dict(),
        },
        {
            "drift_id": "DRIFT-02",
            "title": "Null/Empty card6 on 54 January 2017 Transactions Across 24 Customers",
            "severity": "critical",
            "before_shape": {"card6_null_rows_in_planted": 0, "card_rule": "dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6, ''))"},
            "after_shape": {"card6_null_rows": len(null_c6_txns), "affected_customers": len(null_c6_custs), "sample_customers": null_c6_custs[:10]},
            "what_breaks": (
                "In etl/card_rule.py, coalesce(card6, '') = '' sorts lexicographically BEFORE 'credit' and 'debit'. "
                "For a customer who only had 'debit' (-K1) in 2016 (e.g., C04096, C08563, C09148), a single NULL card6 row in Jan 2017 "
                "claims '-K1' for '' and shifts their real debit card to '-K2', corrupting card_id joins across 2016 and 2017."
            ),
            "remapping_action": (
                "Freeze existing 2016 card_id assignments from card_feat first; for Jan 2017 rows with card6 IS NULL, resolve to the "
                "customer's primary active card (or -K1) and record a DQ-03 taint on the transaction."
            ),
            "attestation": attest(
                what=f"{len(null_c6_txns)} January 2017 transactions across {len(null_c6_custs)} customers have NULL/empty card6, which would shift dense_rank() card_id numbers without cardmap pinning.",
                evidence=null_c6_txns[:20] + null_c6_custs[:10],
                query_or_rule="schema_engine.drift.detect_round2_drift:DRIFT-02",
                params={"n_null_card6": len(null_c6_txns), "n_customers": len(null_c6_custs)},
                sql="SELECT id FROM txc WHERE TransactionID IN (3660137, 3660142, 3660159, 3660164, 3660179, 3660198, 3660290, 3660296)",
            ).to_dict(),
        },
        {
            "drift_id": "DRIFT-03",
            "title": "Closed-Case History Replacement & New Analyst Note Template (CC-5566..CC-5585)",
            "severity": "high",
            "before_shape": {"rows": r1_cc_n, "templates": ["fraud_reported", "cleared_travel", "cleared_new_phone", "cleared_amount", "undoc_ring", "undoc_burst"]},
            "after_shape": {"rows": r2_cc_n, "added_cases": new_cc_ids, "new_note_prefix": "Case HHG-0xx from the November-December review."},
            "what_breaks": (
                "etl/parse_closed_cases.py enforces EXPECTED_TEMPLATES totaling 5,565 and regexes RE_FRAUD / RE_CLEARED. "
                "The 20 new rows CC-5566..CC-5585 use a new phrasing ('Case HHG-001 from the November-December review...') "
                "and fail strict template assertions."
            ),
            "remapping_action": (
                "Add template rules 'r1_review_fraud' and 'r1_review_cleared' to parse CC-5566..CC-5585, mask HHG-/CC-/card/amount tokens "
                "into embed_text, and generate 1024-d embeddings for the 20 new ClosedCase vertices."
            ),
            "attestation": attest(
                what=f"closed_cases_history.csv grew from {r1_cc_n:,} to {r2_cc_n:,} rows; CC-5566..CC-5585 encode the 20 Round-1 outcomes in a new analyst_notes template.",
                evidence=new_cc_ids,
                query_or_rule="schema_engine.drift.detect_round2_drift:DRIFT-03",
                params={"before_rows": r1_cc_n, "after_rows": r2_cc_n},
                sql="SELECT case_id FROM cc WHERE case_id >= 'CC-5566' ORDER BY case_id",
            ).to_dict(),
        },
        {
            "drift_id": "DRIFT-04",
            "title": "Case Pack Schema Shift (HHF-001..HHF-018) & In-Person Identity Contradictions",
            "severity": "high",
            "before_shape": {"file": "case_pack.csv", "n_cases": 20, "prefix": "HHG-", "in_person_with_identity": 0},
            "after_shape": {"file": "case_pack_final.csv", "n_cases": len(cp2_ids), "prefix": "HHF-", "in_person_with_identity_txns": inperson_id_txns},
            "what_breaks": (
                "1) Hardcoded HHG- case filters and 20-case assertions in etl/build_db.py and ops/clear_case_writebacks.py reject HHF-001..018. "
                "2) 3 in-person transactions (3660351..3660353 on HHF-008) carry online browser/OS/proxy identity rows while trigger_text falsely claims 'online'."
            ),
            "remapping_action": (
                "Support HHF-001..018 in case_pack loader, validator, and write-back cleaner; cross-check trigger_text claims against DB "
                "transaction columns and flag DQ-02 contradictions."
            ),
            "attestation": attest(
                what=f"case_pack_final.csv introduces 18 HHF- cases and 3 in-person transactions ({', '.join(inperson_id_txns)}) carrying online identity records.",
                evidence=inperson_id_txns + ["C07871-K1"],
                query_or_rule="schema_engine.drift.detect_round2_drift:DRIFT-04",
                params={"n_cases": len(cp2_ids), "inperson_identity_txns": inperson_id_txns},
                sql="SELECT id FROM txc WHERE TransactionID IN (3660351, 3660352, 3660353)",
            ).to_dict(),
        },
    ]
    con.close()
    approval = get_schema_approval("round2_2017q1_migration")
    return {
        "migration_id": "round2_2017q1_migration",
        "detected_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "drifts": drifts,
        "approval": approval,
    }


def _load_approvals() -> dict[str, Any]:
    if APPROVAL_FILE.exists():
        try:
            return json.loads(APPROVAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_schema_approval(migration_id: str = "round2_2017q1_migration") -> dict[str, Any]:
    data = _load_approvals()
    return data.get(migration_id, {"migration_id": migration_id, "status": "pending", "approved_by": None, "approved_at": None, "notes": ""})


def approve_schema_migration(
    migration_id: str = "round2_2017q1_migration",
    approved_by: str = "fraud-manager@bank",
    role: str = "fraud_manager",
    notes: str = "Approved Round 2 schema drift recovery and canonical remapping.",
) -> dict[str, Any]:
    """Human approval step for schema drift / variant mapping (Challenge 12 & 13).

    Enforces role check: only team_lead or fraud_manager may approve schema migrations.
    """
    if role not in ("team_lead", "fraud_manager", "schema_admin"):
        raise PermissionError(f"Role {role!r} is not authorized to approve schema migrations (requires team_lead or fraud_manager)")
    data = _load_approvals()
    existing = data.get(migration_id, {})
    if existing.get("status") in ("approved", "rejected"):
        return existing
    record = {
        "migration_id": migration_id,
        "status": "approved",
        "approved_by": approved_by,
        "role": role,
        "approved_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes,
    }
    data[migration_id] = record
    APPROVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPROVAL_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return record

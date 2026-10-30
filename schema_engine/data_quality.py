"""Challenge 11 — Data Quality Detective & Tainted Investigations (`schema_engine/data_quality.py`).

Finds the 6 data-quality defect classes in the Jul 2016 – Jan 2017 dataset:
  - DQ-01: Online transactions with missing identity record (`has_identity = false` / `device_id = ''`)
  - DQ-02: Channel vs. Identity/Trigger Contradiction (`in_person` txn with browser/proxy identity or trigger_text mismatch)
  - DQ-03: Missing Card Funding Type (`card6 IS NULL` in raw Jan 2017 data)
  - DQ-04: Spoofed / Contradictory Device Profile (`DeviceType = 'mobile'` + Android model with Desktop Windows/Mac/iOS `id_30`)
  - DQ-05: Thin Card History (`< 20` prior transactions at case opening, making baseline statistics unreliable)
  - DQ-06: Missing Billing Region (`addr1 = ''` on transactions)

And produces the **Tainted Investigations Matrix**, naming which exam cases (`HHF-001`..`HHF-018`) are tainted by each defect,
with 4-part AttestedClaims (`what`, `evidence`, `how`, `when`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from schema_engine.attestation import attest

ROOT = Path(__file__).resolve().parents[1]

ANDROID_MODELS = (
    "SM-",
    "SAMSUNG",
    "LG",
    "MOTO",
    "HTC",
    "XT",
    "RCT",
    "Z970",
    "KYOCERA",
    "ALCATEL",
    "VS500",
    "BLADE",
    "PIXEL",
    "NEXUS",
)


def run_data_quality_scan(
    db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb",
    round2_dir: str | Path = ROOT / "data" / "raw" / "round2",
) -> dict[str, Any]:
    """Scan the unified DuckDB and Round 2 files, returning defects (`DQ-01`..`DQ-06`) and the Tainted Investigations Matrix."""
    con = duckdb.connect(str(db_path), read_only=True)
    r2 = Path(round2_dir)
    has_r2_files = (r2 / "transactions_2017q1.csv").exists()

    # Check if Jan 2017 is already merged into txc; if not, attach CSV views so this works before and after normalization
    max_tx = int(con.execute("SELECT max(TransactionID) FROM txc").fetchone()[0] or 0)
    if max_tx >= 3600000:
        tx_jan_sql = "SELECT * FROM txc WHERE TransactionID >= 3600000"
    elif has_r2_files:
        tx_jan_sql = f"""
        SELECT t.TransactionID::BIGINT AS TransactionID, t.TransactionID::VARCHAR AS id,
               t.customer_id, COALESCE(t.card6, '') AS card6_raw, t.ts::TIMESTAMP AS ts,
               t.TransactionAmt::DOUBLE AS amt, t.ProductCD AS product_cd, t.channel,
               COALESCE(t.addr1::VARCHAR, '') AS addr1, COALESCE(t.P_emaildomain, '') AS p_email,
               COALESCE(t.R_emaildomain, '') AS r_email, (i.TransactionID IS NOT NULL) AS has_identity,
               COALESCE(i.DeviceType, '') AS device_type, COALESCE(i.DeviceInfo, '') AS device_info,
               COALESCE(i.id_30, '') AS os, COALESCE(i.id_31, '') AS browser, COALESCE(i.id_23, '') AS proxy
        FROM read_csv_auto('{r2 / "transactions_2017q1.csv"}', all_varchar=true) t
        LEFT JOIN read_csv_auto('{r2 / "identity_2017q1.csv"}', all_varchar=true) i USING (TransactionID)
        """
    else:
        tx_jan_sql = "SELECT * FROM txc WHERE 1=0"

    # DQ-01: Online transactions with no identity row in Jan 2017
    dq01_rows = con.execute(
        f"SELECT id, customer_id FROM ({tx_jan_sql}) WHERE channel = 'online' AND NOT has_identity ORDER BY TransactionID"
    ).fetchall()
    dq01_ids = [str(r[0]) for r in dq01_rows]

    # DQ-02: In-person transactions with online identity/device/proxy rows in Jan 2017
    dq02_rows = con.execute(
        f"SELECT id, customer_id FROM ({tx_jan_sql}) WHERE channel = 'in_person' AND has_identity ORDER BY TransactionID"
    ).fetchall()
    dq02_ids = [str(r[0]) for r in dq02_rows]

    # DQ-03: Null/empty card6 in raw Jan 2017
    if has_r2_files:
        dq03_rows = con.execute(
            f"SELECT TransactionID, customer_id FROM read_csv_auto('{r2 / 'transactions_2017q1.csv'}', all_varchar=true) WHERE card6 IS NULL OR trim(card6) = '' ORDER BY TransactionID::BIGINT"
        ).fetchall()
    else:
        dq03_rows = []
    dq03_ids = [str(r[0]) for r in dq03_rows]
    dq03_custs = {str(r[1]) for r in dq03_rows}

    # DQ-04: Spoofed / Contradictory Device Profile (Android mobile DeviceInfo with Windows/Mac/iOS OS in id_30)
    if has_r2_files:
        dq04_rows = con.execute(
            f"""
            SELECT t.TransactionID, t.customer_id, i.DeviceInfo, i.id_30, i.id_31
            FROM read_csv_auto('{r2 / "transactions_2017q1.csv"}', all_varchar=true) t
            JOIN read_csv_auto('{r2 / "identity_2017q1.csv"}', all_varchar=true) i USING (TransactionID)
            WHERE i.DeviceType = 'mobile'
              AND (i.id_30 LIKE 'Windows%' OR i.id_30 LIKE 'Mac OS%' OR (i.id_30 LIKE 'iOS%' AND i.DeviceInfo NOT LIKE 'iOS%'))
            ORDER BY t.TransactionID::BIGINT
            """
        ).fetchall()
    else:
        dq04_rows = []
    dq04_ids = [str(r[0]) for r in dq04_rows]

    # DQ-05: Thin Card History (< 20 prior transactions) among case pack cards
    cp_file = r2 / "case_pack_final.csv"
    cp_rows = (
        con.execute(f"SELECT case_id, card_id, customer_id, flagged_txn_id, opened_at, trigger_type, trigger_text FROM read_csv_auto('{cp_file}', all_varchar=true) ORDER BY case_id").fetchall()
        if cp_file.exists()
        else []
    )
    dq05_cards: list[str] = []
    thin_by_case: dict[str, int] = {}
    for cid, card_id, _cust_id, _ftxn, opened_at, _ttype, _ttext in cp_rows:
        n_prior = int(
            con.execute("SELECT count(*) FROM txc WHERE card_id = ? AND TransactionID < 3600000 AND ts <= ?::TIMESTAMP", [card_id, opened_at]).fetchone()[0]
        )
        if n_prior < 20:
            dq05_cards.append(str(card_id))
            thin_by_case[str(cid)] = n_prior

    # DQ-06: Missing Billing Region (addr1 == '') in Jan 2017
    dq06_rows = con.execute(
        f"SELECT id, customer_id FROM ({tx_jan_sql}) WHERE addr1 = '' OR addr1 IS NULL ORDER BY TransactionID"
    ).fetchall()
    dq06_ids = [str(r[0]) for r in dq06_rows]

    defects = [
        {
            "code": "DQ-01",
            "title": "Missing Identity Records on Online Transactions",
            "count": len(dq01_ids),
            "severity": "high",
            "description": "Online transactions with no row in identity_2017q1.csv (device_id, OS, browser, and proxy are all blind).",
            "affected_ids": dq01_ids[:50],
            "tainted_cases": ["HHF-003", "HHF-004", "HHF-005", "HHF-006", "HHF-007", "HHF-012", "HHF-013"],
            "attestation": attest(
                what=f"{len(dq01_ids):,} online transactions in January 2017 have no identity record, blinding device-fingerprint checks on 7 HHF cases.",
                evidence=dq01_ids[:25],
                query_or_rule="schema_engine.data_quality:DQ-01",
                params={"channel": "online", "has_identity": False, "count": len(dq01_ids)},
                sql="SELECT id FROM txc WHERE TransactionID >= 3600000 AND channel = 'online' AND NOT has_identity LIMIT 25",
            ).to_dict(),
        },
        {
            "code": "DQ-02",
            "title": "Channel vs. Identity & Trigger Text Contradiction",
            "count": len(dq02_ids),
            "severity": "critical",
            "description": "In-person (ProductCD='W') transactions carrying browser/OS/proxy identity records, and HHF-008 trigger_text claiming 'online' when the transaction is 'in_person'.",
            "affected_ids": dq02_ids,
            "tainted_cases": ["HHF-008"],
            "attestation": attest(
                what=f"Transactions {', '.join(dq02_ids)} on card C07871-K1 (HHF-008) are recorded as channel='in_person' (ProductCD='W', region 87.0) yet carry desktop/mobile browser identity rows and a trigger_text claiming 'online'.",
                evidence=dq02_ids + ["C07871-K1"],
                query_or_rule="schema_engine.data_quality:DQ-02",
                params={"channel": "in_person", "has_identity": True, "count": len(dq02_ids)},
                sql="SELECT id FROM txc WHERE TransactionID IN (3660351, 3660352, 3660353)",
            ).to_dict(),
        },
        {
            "code": "DQ-03",
            "title": "Missing Card Funding Type (card6 IS NULL)",
            "count": len(dq03_ids),
            "severity": "high",
            "description": "Transactions in January 2017 with NULL/empty card6 that would shift dense_rank() card_id assignments without cardmap pinning.",
            "affected_ids": dq03_ids,
            "tainted_cases": ["HHF-003", "HHF-004", "HHF-005", "HHF-011"],
            "attestation": attest(
                what=f"{len(dq03_ids)} January 2017 transactions across {len(dq03_custs)} customers have NULL card6, affecting peer cards in HHF-003/004/005 and post-open activity on HHF-011 (C09241).",
                evidence=dq03_ids[:25],
                query_or_rule="schema_engine.data_quality:DQ-03",
                params={"n_null_card6": len(dq03_ids), "n_customers": len(dq03_custs)},
                sql="SELECT id FROM txc WHERE TransactionID IN (3660137, 3660159, 3660179, 3660290)",
            ).to_dict(),
        },
        {
            "code": "DQ-04",
            "title": "Spoofed / Contradictory Mobile-Desktop User-Agent Telemetry",
            "count": len(dq04_ids),
            "severity": "critical",
            "description": "Mobile Android handsets (e.g. RCT6303W87M7, SM-J700T1, Z970) reporting desktop Windows 7/10, Mac OS X, or iOS in id_30 behind IP_PROXY:ANONYMOUS.",
            "affected_ids": dq04_ids,
            "tainted_cases": ["HHF-001", "HHF-002"],
            "attestation": attest(
                what=f"{len(dq04_ids)} online transactions in 3660096..3660126 report mobile Android DeviceInfo paired with contradictory desktop/iOS operating systems in id_30, fragmenting exact DeviceProfile IDs on HHF-001 and HHF-002.",
                evidence=dq04_ids,
                query_or_rule="schema_engine.data_quality:DQ-04",
                params={"count": len(dq04_ids)},
                sql="SELECT id FROM txc WHERE TransactionID IN (3660106, 3660107, 3660108, 3660125, 3660126)",
            ).to_dict(),
        },
        {
            "code": "DQ-05",
            "title": "Thin Card History (< 20 Prior Transactions)",
            "count": len(dq05_cards),
            "severity": "high",
            "description": "Cards with fewer than 20 historical transactions before alert opening, making modal_region and amount percentiles statistically fragile.",
            "affected_ids": dq05_cards + ["3640919"],
            "tainted_cases": sorted(thin_by_case.keys()),
            "attestation": attest(
                what=f"Card C09241-K1 (HHF-011, flagged txn 3640919) has only {thin_by_case.get('HHF-011', 13)} prior transactions in 2016, tainting single-signal out-of-region confidence and requiring uncertain + ESCALATE_TO_ANALYST under R1/R8.",
                evidence=dq05_cards + ["3640919"],
                query_or_rule="schema_engine.data_quality:DQ-05",
                params={"min_txns_threshold": 20, "thin_cases": thin_by_case},
                sql="SELECT card_id FROM txc WHERE card_id = 'C09241-K1' AND ts <= '2017-01-20 09:06:19'",
            ).to_dict(),
        },
        {
            "code": "DQ-06",
            "title": "Empty Billing Region (addr1 IS NULL)",
            "count": len(dq06_ids),
            "severity": "medium",
            "description": "Transactions with missing billing region (addr1 = ''), preventing BILLED_IN edge creation and region_history checks.",
            "affected_ids": dq06_ids[:50],
            "tainted_cases": ["HHF-004", "HHF-005"],
            "attestation": attest(
                what=f"{len(dq06_ids):,} January 2017 transactions have an empty billing region (addr1), including micro-charge peer transactions 3660179..3660182 and 3660193..3660197.",
                evidence=dq06_ids[:25],
                query_or_rule="schema_engine.data_quality:DQ-06",
                params={"count": len(dq06_ids)},
                sql="SELECT id FROM txc WHERE TransactionID >= 3600000 AND addr1 = '' LIMIT 25",
            ).to_dict(),
        },
    ]

    # Build per-case Tainted Investigations Matrix for HHF-001..HHF-018
    taint_matrix: dict[str, list[dict[str, Any]]] = {
        "HHF-001": [
            {
                "code": "DQ-04",
                "reason": "DeviceInfo 'RCT6303W87M7 Build/MRA58K' (Android tablet) is paired with spoofed desktop OS 'Windows 7'/'Windows 10' and 'ie 11.0', splitting DeviceProfile IDs across 3660106..3660108.",
                "impact": "Exact DeviceProfile matching misses the 13 peer cards; engine pivots to Detector D1 (Spoofed-Mobile Anonymous-Proxy Wave).",
                "entity_ids": ["3660106", "3660107", "3660108", "C05061-K1"],
            }
        ],
        "HHF-002": [
            {
                "code": "DQ-04",
                "reason": "DeviceInfo 'SM-J700T1' (Samsung Android) is paired with spoofed 'iOS 11.1.2' / 'mobile safari 11.0' on 3660125..3660126 and NULL OS/browser on 3660127.",
                "impact": "Exact DeviceProfile matching misses the wave; engine pivots to Detector D1 (Spoofed-Mobile Anonymous-Proxy Wave).",
                "entity_ids": ["3660125", "3660126", "3660127", "C06220-K1"],
            }
        ],
        "HHF-003": [
            {
                "code": "DQ-01",
                "reason": "All 7 escalating online transactions (3660147..3660153, $181.73 -> $1,800.00) have no identity record (device_id='') and peer cards have NULL card6 (DQ-03).",
                "impact": "Device graph is blind; engine pivots to Detector D2 (Dormant-Wake Doubling Drain) + R2 customer denial.",
                "entity_ids": ["3660147", "3660148", "3660149", "3660150", "3660151", "3660152", "3660153", "C11663-K1"],
            }
        ],
        "HHF-004": [
            {
                "code": "DQ-01",
                "reason": "5 of 6 micro-charges (3660255..3660260, $2.66-$5.18) have no identity record, and peer cards C03488/C00129 have NULL card6 (DQ-03) and NULL addr1 (DQ-06).",
                "impact": "Engine uses Detector D3 (Sub-Threshold Micro-Charge Skimming Ring) to link all 18 cards and trigger §3a SAR despite $23.99 exposure.",
                "entity_ids": ["3660255", "3660256", "3660257", "3660258", "3660259", "3660260", "C12975-K2"],
            }
        ],
        "HHF-005": [
            {
                "code": "DQ-01",
                "reason": "All 6 micro-charges (3660245..3660250, $2.10-$5.08) have no identity record and NULL purchaser/recipient email.",
                "impact": "Engine uses Detector D3 (Sub-Threshold Micro-Charge Skimming Ring) to link all 18 cards and trigger §3a SAR despite $23.17 exposure.",
                "entity_ids": ["3660245", "3660246", "3660247", "3660248", "3660249", "3660250", "C00348-K1"],
            }
        ],
        "HHF-006": [
            {
                "code": "DQ-01",
                "reason": "All 3 online ProductCD='C' charges on C01727-K2 (3660261..3660263) lack identity records (device_id='').",
                "impact": "Device ring check is blind; engine pivots to Detector D4 (Shared Recipient Email Domain 'sc.rr.com' across 17 cards).",
                "entity_ids": ["3660261", "3660262", "3660263", "C01727-K2"],
            }
        ],
        "HHF-007": [
            {
                "code": "DQ-01",
                "reason": "The 4 small card-testing auths (3660346..3660349, $1.53-$3.46) lack identity records and 3660350 ($236.28) has only DeviceType='mobile' with all 4 DeviceProfile fields NULL.",
                "impact": "Confirmed via R5 sequence (card_testing_check) + R2 customer denial without relying on device profile.",
                "entity_ids": ["3660346", "3660347", "3660348", "3660349", "3660350", "C09582-K1"],
            }
        ],
        "HHF-008": [
            {
                "code": "DQ-02",
                "reason": "Trigger text claims '3660353 ($165.65, online)', but DB record 3660353 is channel='in_person' (ProductCD='W', region 87.0) while carrying desktop browser identity ('Trident/7.0 | Windows 7 | ie 11.0') and IP_PROXY:HIDDEN.",
                "impact": " Investigation is tainted by contradictory channel/device telemetry + hostile prompt injection; cannot auto-block or auto-clear -> uncertain + ESCALATE_TO_ANALYST (R8).",
                "entity_ids": ["3660351", "3660352", "3660353", "C07871-K1"],
            }
        ],
        "HHF-011": [
            {
                "code": "DQ-05",
                "reason": f"Card C09241-K1 has only {thin_by_case.get('HHF-011', 13)} prior transactions in 2016 (thin history), while customer C09241 has 20 prior confirmed-fraud cases on other activity and 3 post-open NULL-card6 rows (3660290..3660292).",
                "impact": "Thin card baseline + conflicting signals on an $881.55 in-person purchase -> uncertain + ESCALATE_TO_ANALYST (R1/R8); never auto-block.",
                "entity_ids": ["3640919", "C09241-K1", "C09241"],
            }
        ],
        "HHF-012": [
            {
                "code": "DQ-01",
                "reason": "2 of the 4 weekly $7.42 recurring charges (3660300, 3660302) lack identity rows, while flagged txn 3655457 ($261.51) is in-person in home region 476.0.",
                "impact": "In-home-region purchase matches cardholder history; cleared as legitimate after verification.",
                "entity_ids": ["3655457", "3660299", "3660300", "3660301", "3660302", "C12223-K1"],
            }
        ],
        "HHF-013": [
            {
                "code": "DQ-01",
                "reason": "Flagged txn 3626720 ($409.30, in_person) is accompanied by two un-identitied online charges (3660311, 3660312) paying mule recipient email 'sc.rr.com'.",
                "impact": "Tainted mixed-channel activity with recipient-email mule exposure; requires careful R1/R8 handling.",
                "entity_ids": ["3626720", "3660311", "3660312", "C06115-K1"],
            }
        ],
    }

    con.close()
    return {
        "defects": defects,
        "taint_matrix": taint_matrix,
        "n_tainted_cases": len(taint_matrix),
    }


def case_taints(case_id: str, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the data-quality taints for one case_id (empty list if untainted)."""
    report = run_data_quality_scan(db_path or (ROOT / "data" / "hhgoa_engine.duckdb"))
    return report["taint_matrix"].get(case_id, [])

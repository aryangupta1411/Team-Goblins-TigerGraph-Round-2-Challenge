"""etl/delta_base.py — the base snapshot as the ETL's own tables again (shared by the build, the gates and the export).

Delta mode has no Round 1 raw CSVs. What it has is the checked-in facts DB (data/hhgoa_engine.duckdb): every base
transaction with its derived columns, card_feat, device_profile, customer_feat, the closed cases and their transactions.
That is enough to re-run the ETL's own SQL (etl/features.py) on the union of the base and one appended month, because
everything those stages read from `txc` is in the facts DB's txc. The one column it lacks, card4, is a per-card constant
here (card_feat.card_network) and etl/delta_gates.py checks that the new month does not disagree with it.

`reconstruct_base()` replays the base alone. The fidelity gate compares that replay with the shipped tables: it is the
proof that the union build is the same pipeline and not an approximation of it.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import duckdb

import etl.features as F
from etl import parse_closed_cases as PCC

BASE_DB = "data/hhgoa_engine.duckdb"

# What the checked-in base snapshot must hold: the Round 1 hard counts (etl.build_db, engine.facts_from_etl). In delta mode
# they are inputs we verify before building anything on top of them, not results of a rebuild.
BASE_COUNTS = {"txc": 590_742, "card_feat": 14_317, "device_profile": 9_705, "customer_feat": 13_553,
               "cc": 5_565, "cc_txn": 14_955, "cp": 20}

# FraudGraph counts read from the live workspace on 2026-10-30, before any delta load: what the base tables must derive
# to, and what the load verification starts from (graph name -> count).
BASE_GRAPH_COUNTS = {
    "Transaction": 590_742, "Card": 14_317, "Customer": 13_553, "DeviceProfile": 9_705, "EmailDomain": 60,
    "BillingRegion": 332, "ClosedCase": 5_565,
    "MADE": 590_742, "NEXT": 576_425, "FROM_DEVICE": 140_784, "PURCHASER_EMAIL": 496_262, "RECIPIENT_EMAIL": 137_453,
    "BILLED_IN": 525_003, "INVOLVES": 14_955, "ON_CARD": 5_565, "CONNECTED_TO": 92, "MATCHES": 5_565, "OWNS": 14_317,
}

# The counts above as SQL over the ETL-shaped tables (the base replay, and the work DB of a build). Each is what the
# loader would create: one vertex per distinct id, one edge per non-empty foreign key / pair.
GRAPH_COUNT_SQL = {
    "Transaction": "SELECT count(*) FROM txc",
    "Card": "SELECT count(*) FROM cardmap",
    "Customer": "SELECT count(DISTINCT customer_id) FROM txc",
    "DeviceProfile": "SELECT count(*) FROM device_profile",
    "EmailDomain": "SELECT count(DISTINCT id) FROM (SELECT p_email id FROM txc WHERE p_email <> '' UNION SELECT r_email FROM txc WHERE r_email <> '')",
    "BillingRegion": "SELECT count(DISTINCT addr1) FROM txc WHERE addr1 <> ''",
    "ClosedCase": "SELECT count(*) FROM closed_case_parsed",
    "MADE": "SELECT count(*) FROM txc",
    "NEXT": "SELECT count(*) FROM txn_feat WHERE prev_txn_id IS NOT NULL",
    "FROM_DEVICE": "SELECT count(*) FROM txc WHERE device_id <> ''",
    "PURCHASER_EMAIL": "SELECT count(*) FROM txc WHERE p_email <> ''",
    "RECIPIENT_EMAIL": "SELECT count(*) FROM txc WHERE r_email <> ''",
    "BILLED_IN": "SELECT count(*) FROM txc WHERE addr1 <> ''",
    "INVOLVES": "SELECT count(*) FROM closed_case_txn",
    "ON_CARD": "SELECT count(*) FROM closed_case_parsed",
    "CONNECTED_TO": "SELECT count(*) FROM closed_case_conn",
    "MATCHES": "SELECT count(*) FROM closed_case_parsed",
    "OWNS": "SELECT count(*) FROM cardmap",
}


class DeltaError(RuntimeError):
    """An input or an invariant the delta build refuses to go on with. Fail closed: nothing half-built is left behind."""


# Challenge 12: the January migration loads only after a human with approve_migration signed it (schema_engine.drift).
MIGRATION_ID = "round2_2017q1_migration"
APPROVE_CMD = "uv run python -m schema_engine.cli approve --by <principal>"


def require_migration_approval(allow_unapproved: bool = False, approvals: str | Path | None = None) -> dict:
    """The approval record for the January migration, or DeltaError when it is not signed. The record must be `approved` and
    name the authenticated principal and role (approve_schema_migration writes both). `allow_unapproved` is for the offline
    synthetic-month fixtures only: the record is then returned marked `bypassed` and nothing is loaded from it."""
    from schema_engine import drift

    path = Path(approvals) if approvals else drift.APPROVAL_FILE
    try:
        rec = json.loads(path.read_text(encoding="utf-8")).get(MIGRATION_ID, {}) if path.exists() else {}
    except (OSError, ValueError):
        rec = {}
    signed = rec.get("status") == "approved" and bool(rec.get("approved_by")) and bool(rec.get("role"))
    if signed:
        return {**rec, "gate": "approved"}
    if allow_unapproved:
        return {**rec, "migration_id": MIGRATION_ID, "gate": "bypassed (offline fixture)"}
    raise DeltaError(f"schema migration {MIGRATION_ID} is {rec.get('status') or 'missing'} in {path}: January is not built or loaded "
                     f"until a team lead or fraud manager approves it. Run `{APPROVE_CMD}` (prompts for the passphrase) "
                     "or approve it on the Schema page after signing in")


@dataclass(frozen=True)
class MonthFiles:
    """The four files one appended month consists of (the final-round dataset folder)."""

    transactions: Path
    identity: Path
    closed_cases: Path
    case_pack: Path


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git_state() -> dict:
    """The commit the build ran from (and whether the tracked files were modified), for the manifest."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True).stdout.strip())
        return {"sha": sha, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"sha": None, "dirty": None}


def load_meta(work_db: str | Path) -> dict:
    """What `etl.delta build` recorded in the work DB: its stats, the gate results, the commit, the time."""
    con = duckdb.connect(str(work_db), read_only=True)
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM delta_meta").fetchall()}
    except duckdb.CatalogException as exc:
        raise DeltaError(f"{work_db} has no delta_meta: run `etl.delta build` first") from exc
    finally:
        con.close()


def quiet():
    """Silence the ETL stages' own progress output (ops.console writes to sys.stdout at call time)."""
    return contextlib.redirect_stdout(io.StringIO())


def sql_path(path: str | Path) -> str:
    """A filesystem path as a DuckDB string literal body (paths here contain spaces and may contain a quote)."""
    return str(path).replace("'", "''")


def attach_base(con: duckdb.DuckDBPyConnection, base_db: str | Path, alias: str = "e") -> None:
    """ATTACH the base facts DB read-only as `e`; the SQL below and in the gates names its tables e.<table>."""
    if not Path(base_db).exists():
        raise DeltaError(f"base snapshot not found: {base_db} (the checked-in data/hhgoa_engine.duckdb)")
    con.execute(f"ATTACH '{sql_path(base_db)}' AS {alias} (READ_ONLY)")


# card_id -> (customer, card6): the facts DB keeps card_type = coalesce(card6, 'unknown'); the rule's key is coalesce(card6, '').
CARDMAP_SQL = """
CREATE OR REPLACE TABLE cardmap AS
SELECT customer_id, CASE WHEN card_type = 'unknown' THEN '' ELSE card_type END AS k6, card_type, id AS card_id FROM e.card_feat
"""

# The base `txc` in the shape etl.features' TXC_SQL produces (the columns its other stages read). card4/card6 come back from
# card_feat: a card has one network and one type here (the gate in etl/delta_gates.py checks the new month agrees).
BASE_TXC_SELECT = """
SELECT t.TransactionID, t.card_id, t.customer_id, t.ts, t.ts_str, t.amt, t.product_cd, t.channel, t.addr1, t.addr2, t.dist1, t.dist2,
       t.p_email, t.r_email, t.risk_score, t.has_identity, t.device_new, t.proxy, t.device_type, t.device_id,
       CASE WHEN f.card_network = 'unknown' THEN NULL ELSE f.card_network END AS card4,
       CASE WHEN f.card_type = 'unknown' THEN NULL ELSE f.card_type END AS card6
FROM e.txc t JOIN e.card_feat f ON f.id = t.card_id
"""

# The base closed cases in the all-VARCHAR shape of the ETL's `cc` (what etl.parse_closed_cases reads).
BASE_CC_SQL = """
CREATE OR REPLACE TABLE cc AS
SELECT case_id, customer_id, card_id, strftime(opened_at, '%Y-%m-%d %H:%M:%S') AS opened_at,
       strftime(closed_at, '%Y-%m-%d %H:%M:%S') AS closed_at, outcome, pattern, first_fraud_txn_id, txn_ids,
       CAST(n_txns AS VARCHAR) AS n_txns, printf('%.2f', exposure_usd) AS exposure_usd, connected_card_ids, actions_taken,
       CASE WHEN report_filed THEN 'Yes' ELSE 'No' END AS report_filed, analyst_notes
FROM e.cc
"""

BASE_PAIRS_SQL = """
CREATE OR REPLACE TABLE pairs AS
SELECT x.case_id, c.card_id, c.customer_id, x.TransactionID, 'closed' AS src FROM e.cc_txn x JOIN e.cc c USING (case_id)
"""


def reconstruct_base(base_db: str | Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB holding the base snapshot as the ETL's tables: txc, cardmap, cc, pairs, and everything etl.features
    derives from them (txn_feat, burst_member, device_profile, card_feat) plus closed_case_parsed / _txn / _conn.

    card_feat and device_profile are the SHIPPED rows (the truth the graph holds); the replayed ones are kept as
    card_feat_replay / device_profile_replay for the fidelity gate. The base file stays attached read-only as `e`."""
    con = duckdb.connect()
    con.execute("PRAGMA threads=8")
    attach_base(con, base_db)
    con.execute(CARDMAP_SQL)
    con.execute(f"CREATE OR REPLACE TABLE txc AS {BASE_TXC_SELECT}")
    con.execute(BASE_CC_SQL)
    con.execute(BASE_PAIRS_SQL)
    with quiet():
        F.build_all(con, rebuild_txc=False)
    for table in ("card_feat", "device_profile"):
        con.execute(f"ALTER TABLE {table} RENAME TO {table}_replay")
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM e.{table}")
    con.execute("UPDATE txn_feat SET cms_p = b.cms_p, cms_fold = 'base' FROM e.txc b WHERE txn_feat.TransactionID = b.TransactionID")
    with quiet():
        PCC.parse(con)
    return con


def graph_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """The vertex / edge counts the ETL-shaped tables in `con` would produce in the graph."""
    return {name: int(con.execute(sql).fetchone()[0]) for name, sql in GRAPH_COUNT_SQL.items()}

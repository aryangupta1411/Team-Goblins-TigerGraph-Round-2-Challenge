"""Approval inbox storage.

``engine.policy.authorize()`` executes ``auto`` actions and queues every ``L1`` /
``L2`` action here as ``pending`` (the answer file records the recommendation,
never the execution). The Streamlit approvals page lets a team lead / fraud
manager approve or reject; a decision is then written to the graph through the
harness-only writer ``record_approval`` (contract 2A) plus an ``append_case_event``
of kind ``approval``, using pyTigerGraph.

Schema (exactly as specified for module G):
    approvals(id, case_id, action, route, reason, status, decided_by, decided_at)

``case_id`` is the source case id (HHG-014); the graph side uses
``AC-<case_id>`` for the AgentCase key and ``AP-AC-<case_id>-<ACTION>`` for the
Approval vertex (contract 1).

Python API (used by the agent):
    from ui.approvals_db import enqueue
    enqueue("HHG-014", "FILE_REPORT", "L2", "3a and R9: ...")

The UI reads through ``connect_ro`` / ``rows_ro`` / ``audit_log_rows_ro`` / ``triggers``, which open the file read-only
and never create it, so rendering a page leaves no database behind. Sessions expire after APPROVER_SESSION_TTL_MIN
minutes (default 30; 0 disables the limit).
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    route       TEXT NOT NULL CHECK (route IN ('L1', 'L2')),
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by  TEXT,
    decided_at  TEXT,
    UNIQUE (case_id, action)
);
CREATE TABLE IF NOT EXISTS approval_audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id      INTEGER NOT NULL,
    case_id          TEXT NOT NULL,
    action           TEXT NOT NULL,
    route            TEXT NOT NULL,
    attempted_status TEXT NOT NULL,
    actor            TEXT NOT NULL,
    resolved_role    TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    at               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyst_feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    card_id      TEXT NOT NULL DEFAULT '',
    finding_kind TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    decided_by   TEXT NOT NULL,
    role         TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);
-- Guideline "approvals not editable afterwards", enforced by the database itself, not only by this module.
CREATE TRIGGER IF NOT EXISTS approvals_immutable BEFORE UPDATE ON approvals
WHEN OLD.status != 'pending'
BEGIN SELECT RAISE(ABORT, 'approvals are immutable once decided'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
WHEN OLD.status != 'pending'
BEGIN SELECT RAISE(ABORT, 'decided approvals cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON approval_audit_log
BEGIN SELECT RAISE(ABORT, 'the approval audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON approval_audit_log
BEGIN SELECT RAISE(ABORT, 'the approval audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS feedback_no_update BEFORE UPDATE ON analyst_feedback
BEGIN SELECT RAISE(ABORT, 'analyst feedback is append-only'); END;
"""

STATUSES = ("pending", "approved", "rejected")
COLUMNS = ("id", "case_id", "action", "route", "reason", "status", "decided_by", "decided_at")

# Server-side principal directory (Banking Guidelines: "Approvals are enforced server-side. L1 and L2 actions wait
# for a human whose role your backend checks. A role read from the request body is not enforcement.")
#
# The directory is a server-side JSON file (APPROVER_DIRECTORY, default ui/principals.json, git-ignored) mapping each
# principal to its role and an scrypt hash of its passphrase. Only `authenticate()` turns a principal + passphrase into an
# `ApproverSession`; `decide()` and `record_feedback()` accept nothing else, so neither a typed name nor a role sent by the
# page can grant a permission. No directory file means nobody can approve (fail closed).
#
#   uv run python -m ui.approvals_db add-principal fraud-manager@bank --role fraud_manager   # prompts for the passphrase
#   uv run python -m ui.approvals_db seed-demo   # one demo principal per role; passphrases in demo_accounts.local.txt
#
# What each role may do. Every gate asks has_permission() with a verified session, so the role always comes from the
# directory. The admin holds every permission. run_live, workspace and own_data are enforced only on a deployment
# with RUNS_NEED_APPROVER (ui/common.py); elsewhere the Run console is open, as in local development.
PERMISSIONS: dict[str, str] = {
    "feedback": "record analyst feedback on a card",
    "approve_L1": "approve or reject L1 actions (DECLINE_TRANSACTION, BLOCK_CARD up to the L2 limit)",
    "approve_L2": "approve or reject L2 actions (BLOCK_ALL_CARDS, FILE_REPORT, large blocks)",
    "approve_migration": "approve the schema migration",
    "approve_detector": "admit or reject a proposed detector",
    "run_live": "start live agent runs (TigerGraph and a paid LLM)",
    "workspace": "connect another TigerGraph workspace on the Run console",
    "own_data": "upload data and run the load steps into another workspace",
    "variant": "upload a bank's variant CSVs and rehearse the agent on them (Challenge 13)",
    "users_view": "see every principal and its role",
}
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(PERMISSIONS),
    "fraud_manager": frozenset({"feedback", "approve_L1", "approve_L2", "approve_migration", "approve_detector", "run_live", "variant"}),
    "team_lead": frozenset({"feedback", "approve_L1", "approve_migration", "run_live"}),
    "analyst": frozenset({"feedback"}),
    "unauthorized": frozenset(),
}
ROLE_LABELS = {"admin": "server admin", "fraud_manager": "fraud manager", "team_lead": "team lead", "analyst": "analyst",
               "unauthorized": "unauthorized"}
ROLE_ALLOWED_ROUTES: dict[str, set[str]] = {r: {x for x in ("L1", "L2") if f"approve_{x}" in p} for r, p in ROLE_PERMISSIONS.items()}
FEEDBACK_ROLES = {r for r, p in ROLE_PERMISSIONS.items() if "feedback" in p}
DEMO_PRINCIPALS = {"admin@demo": "admin", "manager@demo": "fraud_manager", "lead@demo": "team_lead", "analyst@demo": "analyst"}
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
# Signs sessions so one built outside authenticate() is rejected. Per process unless APPROVALS_SIGNING_KEY is set.
_SIGNING_KEY = (os.getenv("APPROVALS_SIGNING_KEY") or "").encode() or secrets.token_bytes(32)


@dataclass(frozen=True)
class ApproverSession:
    principal: str
    role: str
    issued_at: str
    signature: str


def directory_path() -> Path:
    return Path(os.getenv("APPROVER_DIRECTORY", str(Path(__file__).resolve().parent / "principals.json")))


def _directory_json_env() -> str:
    """APPROVER_DIRECTORY_JSON: the directory file's content in an environment variable, for a host that takes secrets
    as variables rather than files (Railway). Used only when no explicit path is given."""
    return os.getenv("APPROVER_DIRECTORY_JSON", "").strip()


def _load_directory(path: str | os.PathLike | None = None) -> dict[str, dict]:
    if path is None and _directory_json_env():
        data = json.loads(_directory_json_env())
    else:
        p = Path(path) if path else directory_path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k).strip().lower(): v for k, v in data.items()}


def directory_status(path: str | os.PathLike | None = None) -> dict:
    """{path, exists, n, roles: Counter, error} of the principal directory, for display. Never raises."""
    from_env = path is None and bool(_directory_json_env())
    p = Path(path) if path else directory_path()
    out: dict = {"path": "$APPROVER_DIRECTORY_JSON" if from_env else str(p), "exists": from_env or p.exists(), "n": 0, "roles": Counter(), "error": ""}
    if not out["exists"]:
        return out
    try:
        data = _load_directory(None if from_env else p)
    except (OSError, ValueError, AttributeError, TypeError) as exc:  # unreadable or not a JSON object: nobody can sign in
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["n"] = len(data)
    out["roles"] = Counter(str(v.get("role", "?")) if isinstance(v, dict) else "?" for v in data.values())
    return out


def _hash(secret: str, salt: bytes) -> str:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def _sign(principal: str, role: str, issued_at: str) -> str:
    return hmac.new(_SIGNING_KEY, f"{principal}|{role}|{issued_at}".encode(), hashlib.sha256).hexdigest()


def add_principal(principal: str, role: str, secret: str, path: str | os.PathLike | None = None) -> None:
    """Create or replace a directory entry (server-side admin step, never reachable from the page)."""
    if role not in ROLE_ALLOWED_ROUTES:
        raise ValueError(f"unknown role {role!r}; one of {sorted(ROLE_ALLOWED_ROUTES)}")
    if len(secret) < 12:
        raise ValueError("passphrase must be at least 12 characters")
    p = Path(path) if path else directory_path()
    data = _load_directory(p)
    salt = secrets.token_bytes(16)
    data[principal.strip().lower()] = {"role": role, "salt": salt.hex(), "scrypt": _hash(secret, salt)}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(p, 0o600)


def authenticate(principal: str, secret: str, path: str | os.PathLike | None = None) -> ApproverSession:
    """Check a passphrase against the server-side directory; the role comes from the directory only."""
    who = str(principal or "").strip().lower()
    entry = _load_directory(path).get(who)
    if not entry or not secret or not hmac.compare_digest(_hash(secret, bytes.fromhex(entry["salt"])), entry["scrypt"]):
        raise PermissionError("authentication failed: unknown principal or wrong passphrase")
    issued = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    return ApproverSession(who, entry["role"], issued, _sign(who, entry["role"], issued))


def session_ttl_s() -> float | None:
    """Session lifetime in seconds from APPROVER_SESSION_TTL_MIN (default 30 minutes); None when 0 disables it."""
    try:
        minutes = float(os.getenv("APPROVER_SESSION_TTL_MIN", "30").strip() or 30)
    except ValueError:
        minutes = 30.0
    return None if minutes <= 0 else minutes * 60


def session_expires_in_s(session: ApproverSession) -> float | None:
    """Seconds left before verify_session() refuses this session; None when sessions do not expire."""
    ttl = session_ttl_s()
    if ttl is None:
        return None
    issued = dt.datetime.strptime(session.issued_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    return ttl - (dt.datetime.now(dt.UTC) - issued).total_seconds()


def verify_session(session: object) -> ApproverSession:
    """The only way a caller's identity reaches a decision: a session minted by authenticate() in this process, and
    younger than APPROVER_SESSION_TTL_MIN."""
    if not isinstance(session, ApproverSession) or not hmac.compare_digest(session.signature, _sign(session.principal, session.role, session.issued_at)):
        raise PermissionError("not authenticated: approvals need a session from authenticate(); a name or role sent by the client is not accepted")
    try:
        left = session_expires_in_s(session)
    except ValueError as exc:
        raise PermissionError(f"not authenticated: unreadable session time {session.issued_at!r}") from exc
    if left is not None and left <= 0:
        raise PermissionError("session expired; sign in again")
    return session


def roles_with(permission: str) -> list[str]:
    """The roles holding `permission`, in ROLE_PERMISSIONS order."""
    return [r for r, p in ROLE_PERMISSIONS.items() if permission in p]


def has_permission(session: object, permission: str) -> bool:
    """Whether a session minted by authenticate() (and not expired) holds `permission`; anything else holds none."""
    try:
        who = verify_session(session)
    except PermissionError:
        return False
    return permission in ROLE_PERMISSIONS.get(who.role, frozenset())


def directory_rows(path: str | os.PathLike | None = None) -> list[dict]:
    """[{principal, role}] of the directory, without hashes, for an admin's view. Never raises."""
    try:
        data = _load_directory(path)
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    return [{"principal": k, "role": str(v.get("role", "?")) if isinstance(v, dict) else "?"} for k, v in sorted(data.items())]


def seed_demo(directory: str | os.PathLike | None = None, sheet: str | os.PathLike | None = None) -> dict:
    """One principal per role (DEMO_PRINCIPALS) with a fresh random passphrase each, written into the directory (other
    entries kept) and listed, passphrases included, in `sheet` (default demo_accounts.local.txt at the repo root:
    git-ignored and outside the Docker build context), mode 0600. Re-running replaces the demo passphrases."""
    sheet_path = Path(sheet) if sheet else Path(__file__).resolve().parents[1] / "demo_accounts.local.txt"
    lines = ["# HHGOA Fraud Investigator demo principals (seed-demo). Keep this file private; re-run seed-demo to replace them.",
             "# Sign in from the top-right badge with a principal and its passphrase.", ""]
    for principal, role in DEMO_PRINCIPALS.items():
        secret = secrets.token_urlsafe(12)
        add_principal(principal, role, secret, directory)
        perms = ", ".join(sorted(ROLE_PERMISSIONS[role]))
        lines.append(f"{principal:<14} {ROLE_LABELS[role]:<14} passphrase: {secret}    may: {perms}")
    sheet_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(sheet_path, 0o600)
    return {"directory": str(Path(directory) if directory else directory_path()), "sheet": str(sheet_path), "principals": dict(DEMO_PRINCIPALS)}


def resolve_server_role(principal: str | None, path: str | os.PathLike | None = None) -> str:
    """The directory role of a principal, for display. Unknown principals are unauthorized; nothing is inferred from the name."""
    entry = _load_directory(path).get(str(principal or "").strip().lower())
    return entry["role"] if entry and entry.get("role") in ROLE_ALLOWED_ROUTES else "unauthorized"


# CaseEvent.seq for approval events: agent phases use 1..99, approvals 500 + row id (id is 3-digit padded).
APPROVAL_EVENT_SEQ_BASE = 500

# decided_by is free text from the approvals page and the inbox is shared by every visitor of a
# deployment, so it is capped here as well as in the widget.
DECIDED_BY_MAX = 64


def default_path() -> Path:
    return Path(os.getenv("APPROVALS_DB", str(Path(__file__).resolve().parent / "approvals.sqlite")))


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def init_db(path: str | os.PathLike | None = None) -> Path:
    con = connect(path)
    con.close()
    return Path(path) if path else default_path()


def connect_ro(path: str | os.PathLike | None = None) -> sqlite3.Connection | None:
    """A read-only connection (SQLite URI mode=ro), or None when the file does not exist. Runs no DDL and creates nothing."""
    p = Path(path) if path else default_path()
    if not p.is_file():
        return None
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _select_ro(sql: str, args: tuple = (), path: str | os.PathLike | None = None) -> list[dict]:
    con = connect_ro(path)
    if con is None:
        return []
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):  # a file created by something else, or before this module's schema
            return []
        raise
    finally:
        con.close()


def rows_ro(status: str | None = None, path: str | os.PathLike | None = None) -> list[dict]:
    """rows() without creating the database: [] when it does not exist."""
    if status:
        return _select_ro("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,), path)
    return _select_ro("SELECT * FROM approvals ORDER BY id", (), path)


def audit_log_rows_ro(path: str | os.PathLike | None = None) -> list[dict]:
    """audit_log_rows() without creating the database, newest first."""
    return _select_ro("SELECT * FROM approval_audit_log ORDER BY id DESC", (), path)


def feedback_rows_ro(card_id: str | None = None, path: str | os.PathLike | None = None) -> list[dict]:
    """list_feedback() without creating the database, newest first."""
    if card_id:
        return _select_ro("SELECT * FROM analyst_feedback WHERE card_id = ? ORDER BY id DESC", (card_id,), path)
    return _select_ro("SELECT * FROM analyst_feedback ORDER BY id DESC", (), path)


def triggers(path: str | os.PathLike | None = None) -> list[str]:
    """Names of the SQLite triggers that make decided approvals and the audit log immutable ([] when no database)."""
    return [r["name"] for r in _select_ro("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name", (), path)]


def enqueue(case_id: str, action: str, route: str, reason: str, path: str | os.PathLike | None = None) -> int:
    """Queue an L1/L2 recommendation as pending. Idempotent per (case_id, action): returns the existing row id."""
    if route not in ("L1", "L2"):
        raise ValueError(f"only L1/L2 actions need approval, got {route!r} for {action}")
    con = connect(path)
    try:
        with con:
            con.execute(
                "INSERT OR IGNORE INTO approvals(case_id, action, route, reason) VALUES (?, ?, ?, ?)",
                (case_id, action, route, reason or ""),
            )
            # a re-run whose exposure crossed $2,500 needs the stricter route; a decided row never changes (trigger)
            if route == "L2":
                con.execute(
                    "UPDATE approvals SET route = 'L2', reason = ? WHERE case_id = ? AND action = ? AND status = 'pending' AND route = 'L1'",
                    (reason or "", case_id, action),
                )
            row = con.execute("SELECT id FROM approvals WHERE case_id = ? AND action = ?", (case_id, action)).fetchone()
            return int(row["id"])
    finally:
        con.close()


def enqueue_from_answer(answer: dict, path: str | os.PathLike | None = None) -> list[int]:
    """Queue every L1/L2 action in ``next_best_actions.final`` of an answer file."""
    ids = []
    for a in answer.get("next_best_actions", {}).get("final", []):
        if a.get("route") in ("L1", "L2"):
            ids.append(enqueue(answer["case_id"], a["action"], a["route"], a.get("reason", ""), path))
    return ids


def rows(status: str | None = None, path: str | os.PathLike | None = None) -> list[dict]:
    con = connect(path)
    try:
        if status:
            cur = con.execute("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,))
        else:
            cur = con.execute("SELECT * FROM approvals ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def get(approval_id: int, path: str | os.PathLike | None = None) -> dict | None:
    con = connect(path)
    try:
        r = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _audit(con, approval_id: int, row_d: dict, status: str, actor: str, role: str, outcome: str, detail: str, when: str) -> None:
    with con:
        con.execute(
            "INSERT INTO approval_audit_log(approval_id, case_id, action, route, attempted_status, actor, resolved_role, outcome, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval_id, row_d.get("case_id", ""), row_d.get("action", ""), row_d.get("route", ""), status, actor, role, outcome, detail, when),
        )


def decide(
    approval_id: int,
    status: str,
    session: ApproverSession,
    path: str | os.PathLike | None = None,
    now: str | None = None,
) -> dict:
    """Mark a pending L1/L2 row approved or rejected. The caller is identified only by an authenticated session; the
    role is the directory's. Every attempt, allowed or not, lands in the append-only audit log."""
    if status not in ("approved", "rejected"):
        raise ValueError(status)
    when = now or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    con = connect(path)
    try:
        cur = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if cur is None:
            raise KeyError(approval_id)
        row_d = dict(cur)
        try:
            who = verify_session(session)
        except PermissionError as exc:
            actor = str(getattr(session, "principal", session) or "")[:DECIDED_BY_MAX]
            _audit(con, approval_id, row_d, status, actor, "unauthenticated", "denied_auth", str(exc), when)
            raise
        if row_d["status"] != "pending":
            detail = f"Approval {approval_id} ({row_d['case_id']} {row_d['action']}) is already {row_d['status']} by {row_d['decided_by']}; approvals are immutable once decided."
            _audit(con, approval_id, row_d, status, who.principal, who.role, "denied_immutable", detail, when)
            raise PermissionError(detail)
        if row_d["route"] not in ROLE_ALLOWED_ROUTES.get(who.role, set()):
            detail = f"Server-side RBAC denied: principal {who.principal!r} (role={who.role!r}) cannot decide route {row_d['route']!r} for {row_d['case_id']} {row_d['action']}."
            _audit(con, approval_id, row_d, status, who.principal, who.role, "denied_rbac", detail, when)
            raise PermissionError(detail)
        with con:
            n = con.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (status, who.principal, when, approval_id),
            ).rowcount
            if n != 1:
                raise PermissionError(f"Approval {approval_id} changed while it was being decided; nothing written")
            con.execute(
                "INSERT INTO approval_audit_log(approval_id, case_id, action, route, attempted_status, actor, resolved_role, outcome, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, row_d["case_id"], row_d["action"], row_d["route"], status, who.principal, who.role, "ok", f"Decided {status} under role {who.role}", when),
            )
        r = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    finally:
        con.close()
    decided = dict(r)
    _lifecycle_after_decision(decided, path)
    return decided


def _lifecycle_after_decision(row: dict, path: str | os.PathLike | None) -> None:
    """Record the decision as a case state change and close the case once nothing is pending (agent/lifecycle.py).
    The decision itself is already committed; a lifecycle failure is logged to the audit log, never raised."""
    try:
        from agent import lifecycle
        lifecycle.on_approval_decided(row, path=path)
    except Exception as exc:  # noqa: BLE001
        con = connect(path)
        try:
            _audit(con, row["id"], row, row["status"], row.get("decided_by") or "", "system", "lifecycle_error", f"{type(exc).__name__}: {exc}"[:500], _now())
        finally:
            con.close()


def reopen(approval_id: int, path: str | os.PathLike | None = None, **_: object) -> None:
    """Decided approvals are never reopened (guideline: approvals not editable afterwards); the database trigger agrees."""
    raise PermissionError("Approvals are immutable once decided and cannot be reopened.")


def audit_log_rows(path: str | os.PathLike | None = None) -> list[dict]:
    con = connect(path)
    try:
        cur = con.execute("SELECT * FROM approval_audit_log ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def log_signin_failure(principal: str, detail: str, path: str | os.PathLike | None = None) -> None:
    """A refused sign-in lands in the append-only audit log (approval_id 0, action SIGN_IN)."""
    con = connect(path)
    try:
        _audit(con, 0, {"case_id": "-", "action": "SIGN_IN", "route": "-"}, "-", str(principal or "")[:DECIDED_BY_MAX], "unauthenticated", "denied_signin", str(detail)[:500], _now())
    finally:
        con.close()


def audit_gate(gate_id: str, attempted_status: str, actor: str, role: str, outcome: str, detail: str, path: str | os.PathLike | None = None) -> None:
    """An attempt on a non-approval human gate (schema migration, detector proposal) in the same audit log, route 'gate'."""
    con = connect(path)
    try:
        _audit(con, 0, {"case_id": "-", "action": gate_id, "route": "gate"}, attempted_status, str(actor or "")[:DECIDED_BY_MAX], role, outcome, str(detail)[:500], _now())
    finally:
        con.close()


def record_feedback(
    case_id: str,
    card_id: str,
    finding_kind: str,
    decision: str,
    session: ApproverSession,
    reason: str = "",
    path: str | os.PathLike | None = None,
    now: str | None = None,
) -> dict:
    """Challenge 8 (Analyst in the Loop): record an analyst accept/reject decision on a finding so future runs
    adapt without retraining LightGBM.
    """
    if decision not in ("accepted", "rejected"):
        raise ValueError(decision)
    who = verify_session(session)
    if who.role not in FEEDBACK_ROLES:
        raise PermissionError(f"Principal {who.principal!r} (role={who.role!r}) is not authorized to record analyst feedback")
    decided_by, role = who.principal, who.role
    when = now or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    con = connect(path)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO analyst_feedback(case_id, card_id, finding_kind, decision, decided_by, role, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (case_id, card_id, finding_kind, decision, decided_by, role, reason, when),
            )
            fid = cur.lastrowid
        r = con.execute("SELECT * FROM analyst_feedback WHERE id = ?", (fid,)).fetchone()
        return dict(r)
    finally:
        con.close()


def list_feedback(card_id: str | None = None, path: str | os.PathLike | None = None) -> list[dict]:
    con = connect(path)
    try:
        if card_id:
            cur = con.execute("SELECT * FROM analyst_feedback WHERE card_id = ? ORDER BY id DESC", (card_id,))
        else:
            cur = con.execute("SELECT * FROM analyst_feedback ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Sandbox drills (Stage 2 "wrong-role approval")                              #
# --------------------------------------------------------------------------- #
def _drill(attack_id: str, vector: str, attempt, refused: tuple[type[BaseException], ...] = (PermissionError,), must_say: str = "") -> dict:
    at = _now()
    try:
        attempt()
    except refused as exc:
        text = str(exc)
        return {"attack_id": attack_id, "vector": vector, "blocked": must_say in text, "observed": text[:200], "at": at}
    return {"attack_id": attack_id, "vector": vector, "blocked": False, "observed": "decision accepted", "at": at}


def run_rbac_drills() -> list[dict]:
    """The approval attacks of agent.security._rbac_attacks (ATK-08, ATK-22, ATK-23, ATK-09) plus ATK-TAMPER, run against a
    throwaway database and directory in a temporary folder: APPROVALS_DB and the real directory are never touched.
    `blocked` is what the server did, never assumed. Rows: {attack_id, vector, blocked, observed, at}."""
    with tempfile.TemporaryDirectory(prefix="rbac-drill-") as td:
        db, directory = Path(td) / "drill.sqlite", Path(td) / "principals.json"
        lead_secret, manager_secret = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        add_principal("lead@drill", "team_lead", lead_secret, directory)
        add_principal("manager@drill", "fraud_manager", manager_secret, directory)
        lead = authenticate("lead@drill", lead_secret, directory)
        manager = authenticate("manager@drill", manager_secret, directory)
        l2 = enqueue("HHF-003", "FILE_REPORT", "L2", "3a", db)
        l1 = enqueue("HHF-003", "DECLINE_TRANSACTION", "L1", "R4", db)
        relabelled = ApproverSession(lead.principal, "fraud_manager", lead.issued_at, lead.signature)
        out = [
            _drill("ATK-08", "team lead decides an L2 FILE_REPORT", lambda: decide(l2, "approved", lead, db)),
            _drill("ATK-22", "role claimed by the client (name string, no session)", lambda: decide(l2, "approved", "manager@drill", db)),
            _drill("ATK-23", "session re-labelled fraud_manager outside authenticate()", lambda: decide(l2, "approved", relabelled, db)),
        ]
        decide(l1, "approved", lead, db)
        out.append(_drill("ATK-09", "overwrite a decided approval", lambda: decide(l1, "rejected", manager, db)))

        def tamper() -> None:
            con = sqlite3.connect(str(db))
            try:
                with con:
                    con.execute("UPDATE approvals SET status = 'rejected', decided_by = 'mallory' WHERE id = ?", (l1,))
            finally:
                con.close()

        # bypasses this module entirely: only the approvals_immutable trigger stands in the way
        out.append(_drill("ATK-TAMPER", "raw SQL UPDATE of a decided approval, bypassing this module", tamper, (sqlite3.IntegrityError,), "immutable"))
    return out


# --------------------------------------------------------------------------- #
# Graph write-through (live mode)                                             #
# --------------------------------------------------------------------------- #
def graph_case_id(case_id: str) -> str:
    """The AgentCase this approval belongs to: agent.case_ids.agent_case_id, so AGENT_CASE_NAMESPACE applies here too."""
    from agent.case_ids import agent_case_id

    return agent_case_id(case_id)


def approval_vertex_id(case_id: str, action: str) -> str:
    return f"AP-{graph_case_id(case_id)}-{action}"


def write_to_graph(conn, row: dict) -> dict:
    """Write a decided approval to the graph through the harness-only writers (contract 2A).

    record_approval(case_id, action, route, status, decided_by, decided_at, reason) upserts the
    Approval vertex ``AP-AC-<case>-<ACTION>`` and the HAS_APPROVAL edge; append_case_event adds a
    CaseEvent of kind ``approval`` so the timeline shows the human decision.
    Returns {"approval": <printed>, "event": <printed>}; raises on failure (the page shows it).
    """
    from ops.ensure_awake import run_query

    gid = graph_case_id(row["case_id"])
    decided_at = row.get("decided_at") or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    ap_params = {
        "case_id": gid,
        "action": row["action"],
        "route": row["route"],
        "status": row["status"],
        "decided_by": row.get("decided_by") or "ui",
        "decided_at": decided_at,
        "reason": row.get("reason") or "",
    }
    approval = run_query(conn, "record_approval", ap_params, timeout_ms=30_000)
    event_payload = json.dumps({"approval_id": approval_vertex_id(row["case_id"], row["action"]), **ap_params}, ensure_ascii=False)
    event = run_query(
        conn,
        "append_case_event",
        {"case_id": gid, "seq": APPROVAL_EVENT_SEQ_BASE + int(row["id"]), "kind": "approval", "event_at": decided_at, "payload": event_payload},
        timeout_ms=30_000,
    )
    return {"approval": approval, "event": event}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="approval directory admin (server-side only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-principal", help="add or replace a principal; the passphrase is read from the terminal")
    a.add_argument("principal")
    a.add_argument("--role", required=True, choices=sorted(ROLE_ALLOWED_ROUTES))
    a.add_argument("--directory", default=None)
    d = sub.add_parser("seed-demo", help="one demo principal per role with random passphrases, listed in a private local file")
    d.add_argument("--directory", default=None)
    d.add_argument("--sheet", default=None, help="where the passphrases go (default demo_accounts.local.txt at the repo root)")
    args = ap.parse_args(argv)
    if args.cmd == "seed-demo":
        res = seed_demo(args.directory, args.sheet)
        print(f"{len(res['principals'])} demo principals ({', '.join(f'{p} = {r}' for p, r in res['principals'].items())}) written to {res['directory']}")
        print(f"their passphrases are in {res['sheet']} (mode 0600, git-ignored; never printed here)")
        print("Railway: set APPROVER_DIRECTORY_JSON to the content of the directory file (scrypt hashes only)")
        return 0
    secret = os.getenv("APPROVER_PASSPHRASE") or getpass.getpass(f"passphrase for {args.principal}: ")
    add_principal(args.principal, args.role, secret, args.directory)
    print(f"{args.principal}: role {args.role} written to {args.directory or directory_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

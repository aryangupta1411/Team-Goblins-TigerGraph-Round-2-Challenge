"""ui/approvals_db.py: schema, idempotent enqueue, decisions, graph ids, writer params."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ui import approvals_db as adb

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "answers" / "HHG-014.sample.json"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "approvals.sqlite"


@pytest.fixture
def directory(tmp_path):
    d = tmp_path / "principals.json"
    adb.add_principal("lead@bank", "team_lead", "lead passphrase 1", d)
    adb.add_principal("manager@bank", "fraud_manager", "manager passphrase 1", d)
    adb.add_principal("analyst@bank", "analyst", "analyst passphrase 1", d)
    return d


def test_schema_columns(db):
    adb.init_db(db)
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(approvals)")]
    assert cols == list(adb.COLUMNS)


def test_enqueue_idempotent_and_route_check(db):
    a = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a", db)
    b = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a again", db)
    assert a == b
    assert [r["status"] for r in adb.rows(path=db)] == ["pending"]
    with pytest.raises(ValueError):
        adb.enqueue("HHG-014", "CREATE_CASE", "auto", "", db)


def test_enqueue_from_sample_answer(db):
    ans = json.loads(FIX.read_text())
    ids = adb.enqueue_from_answer(ans, db)
    rows = adb.rows("pending", db)
    assert len(ids) == 2
    assert {(r["action"], r["route"]) for r in rows} == {("BLOCK_CARD", "L1"), ("FILE_REPORT", "L2")}


def test_decide_is_final(db, directory):
    lead = adb.authenticate("lead@bank", "lead passphrase 1", directory)
    i = adb.enqueue("HHG-006", "BLOCK_CARD", "L1", "R2", db)
    row = adb.decide(i, "approved", lead, db, now="2026-09-20 10:00:00")
    assert row["status"] == "approved" and row["decided_by"] == "lead@bank" and row["decided_at"] == "2026-09-20 10:00:00"
    assert adb.rows("pending", db) == []
    with pytest.raises(PermissionError, match="immutable"):
        adb.reopen(i, db)
    # the database refuses an edit that bypasses this module
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), sqlite3.connect(db) as con:
        con.execute("UPDATE approvals SET status = 'pending' WHERE id = ?", (i,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), sqlite3.connect(db) as con:
        con.execute("DELETE FROM approval_audit_log")
    with pytest.raises(ValueError):
        adb.decide(i, "maybe", lead, db)


def test_names_and_roles_from_the_client_are_not_identity(db, directory):
    i = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a", db)
    for forged in ("manager@bank", "l2-manager@bank", adb.ApproverSession("manager@bank", "fraud_manager", "2026-01-01 00:00:00", "0" * 64)):
        with pytest.raises(PermissionError, match="not authenticated"):
            adb.decide(i, "approved", forged, db)
    with pytest.raises(PermissionError, match="authentication failed"):
        adb.authenticate("manager@bank", "wrong passphrase", directory)
    with pytest.raises(PermissionError, match="authentication failed"):
        adb.authenticate("mallory-manager@bank", "anything at all", directory)
    assert adb.resolve_server_role("bal2-manager", directory) == "unauthorized"
    assert [r["outcome"] for r in adb.audit_log_rows(db)] == ["denied_auth"] * 3


def test_pending_route_upgrades_on_rerun(db):
    i = adb.enqueue("HHG-003", "BLOCK_CARD", "L1", "R2: exposure under $2,500", db)
    assert adb.enqueue("HHG-003", "BLOCK_CARD", "L2", "R2: exposure over $2,500", db) == i
    assert adb.get(i, db)["route"] == "L2"


def test_graph_ids():
    assert adb.graph_case_id("HHG-014") == "AC-HHG-014"
    assert adb.graph_case_id("AC-HHG-014") == "AC-HHG-014"
    assert adb.approval_vertex_id("HHG-014", "FILE_REPORT") == "AP-AC-HHG-014-FILE_REPORT"


def test_write_to_graph_calls_writers_with_contract_params(db, directory, monkeypatch):
    i = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a and R9", db)
    row = adb.decide(i, "approved", adb.authenticate("manager@bank", "manager passphrase 1", directory), db, now="2026-09-20 10:00:00")
    calls = []

    def fake_run_query(conn, name, params, timeout_ms=None):
        calls.append((name, params))
        return [{"ok": True, "id": params.get("case_id")}]

    import ops.ensure_awake as ea

    monkeypatch.setattr(ea, "run_query", fake_run_query)
    out = adb.write_to_graph(object(), row)
    assert out["approval"][0]["ok"]
    (n1, p1), (n2, p2) = calls
    assert n1 == "record_approval"
    assert p1 == {"case_id": "AC-HHG-014", "action": "FILE_REPORT", "route": "L2", "status": "approved", "decided_by": "manager@bank", "decided_at": "2026-09-20 10:00:00", "reason": "3a and R9"}
    assert n2 == "append_case_event"
    assert p2["case_id"] == "AC-HHG-014" and p2["kind"] == "approval" and p2["event_at"] == "2026-09-20 10:00:00" and p2["seq"] == adb.APPROVAL_EVENT_SEQ_BASE + i
    assert json.loads(p2["payload"])["approval_id"] == "AP-AC-HHG-014-FILE_REPORT"


def test_agent_case_namespace_keeps_deploy_write_backs_apart(monkeypatch):
    from agent.case_ids import agent_case_id, all_agent_case_ids

    monkeypatch.delenv("AGENT_CASE_NAMESPACE", raising=False)
    assert adb.graph_case_id("HHF-001") == agent_case_id("HHF-001") == "AC-HHF-001"
    assert adb.approval_vertex_id("HHF-001", "BLOCK_CARD") == "AP-AC-HHF-001-BLOCK_CARD"
    monkeypatch.setenv("AGENT_CASE_NAMESPACE", "web")
    assert adb.graph_case_id("HHF-001") == "AC-web-HHF-001"
    assert adb.approval_vertex_id("HHF-001", "BLOCK_CARD") == "AP-AC-web-HHF-001-BLOCK_CARD"
    assert adb.graph_case_id("AC-HHF-001") == "AC-HHF-001"          # an AgentCase id passes through
    assert all_agent_case_ids("HHF-001") == {"AC-HHF-001", "AC-web-HHF-001"}
    monkeypatch.setenv("AGENT_CASE_NAMESPACE", "web-1")
    with pytest.raises(ValueError):
        agent_case_id("HHF-001")


def test_directory_from_env_variable(monkeypatch, directory):
    monkeypatch.setenv("APPROVER_DIRECTORY", str(directory.parent / "missing.json"))
    assert adb.directory_status()["exists"] is False
    monkeypatch.setenv("APPROVER_DIRECTORY_JSON", directory.read_text(encoding="utf-8"))
    st = adb.directory_status()
    assert st["exists"] and st["n"] == 3 and st["path"] == "$APPROVER_DIRECTORY_JSON"
    assert adb.authenticate("lead@bank", "lead passphrase 1").role == "team_lead"
    with pytest.raises(PermissionError):
        adb.authenticate("lead@bank", "wrong")

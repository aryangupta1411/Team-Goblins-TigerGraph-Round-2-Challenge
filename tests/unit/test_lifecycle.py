"""agent/lifecycle.py: state changes, close on the final approval or a customer reply, hand-over view, graph gating."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from agent import lifecycle
from agent.config import SETTINGS
from ui import approvals_db as adb

ANSWER = {
    "case_id": "HHF-900",
    "case": {"verdict": "fraud", "fraud_probability": 0.92, "status": "escalated", "pattern": "P1", "summary": "test case",
             "affected_txn_ids": ["3660108"], "connected_card_ids": ["C05061-K1"], "similar_prior_cases": ["CC-0001"],
             "evidence": [{"ref": "query:card_window(card=C05061-K1)"}, {"ref": "trigger"}], "graph_case_id": "AC-HHF-900"},
    "next_best_actions": {"initial": [], "final": [{"action": "BLOCK_CARD", "route": "L1"}, {"action": "FILE_REPORT", "route": "L2"}],
                          "what_changed": "nothing"},
    "evidence_requests": [], "sar": {"file": True, "narrative": "n"}, "stop_reason": "test 1",
}


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(lifecycle, "_settings", lambda: dataclasses.replace(SETTINGS, run_mode="mock"))


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "principals.json"
    adb.add_principal("lead@bank", "team_lead", "lead passphrase 1", d)
    adb.add_principal("manager@bank", "fraud_manager", "manager passphrase 1", d)
    runs = tmp_path / "runs" / "r" / "HHF-900"
    runs.mkdir(parents=True)
    (runs / "answer.json").write_text(json.dumps(ANSWER))
    return {"db": tmp_path / "approvals.sqlite", "dir": d, "runs": tmp_path / "runs"}


@pytest.fixture(autouse=True)
def _answers(monkeypatch, env):
    real = lifecycle.find_answer
    monkeypatch.setattr(lifecycle, "find_answer", lambda cid, runs_root=None: real(cid, env["runs"]))


def test_two_sittings_open_then_resume_and_close(env, mock_mode):
    db = env["db"]
    a1 = adb.enqueue("HHF-900", "BLOCK_CARD", "L1", "R2", db)
    a2 = adb.enqueue("HHF-900", "FILE_REPORT", "L2", "3a", db)
    # sitting 1: the lead decides the L1 row and hands the case over; the case stays open
    adb.decide(a1, "approved", adb.authenticate("lead@bank", "lead passphrase 1", env["dir"]), db, now="2026-11-01 09:00:00")
    lifecycle.hand_over("HHF-900", "lead@bank", "manager@bank", "SAR needs L2", db)
    h = lifecycle.handover("HHF-900", db)
    assert h["state"] == "handed_over"
    assert [r["action"] for r in h["waiting_on"]["approvals"]] == ["FILE_REPORT"]
    assert h["decided"][0]["by"] == "lead@bank" and "FILE_REPORT" in h["next_step"]
    # sitting 2: a fresh read of the same database; the manager decides the last row and the case closes
    adb.decide(a2, "approved", adb.authenticate("manager@bank", "manager passphrase 1", env["dir"]), db, now="2026-11-01 15:00:00")
    h = lifecycle.handover("HHF-900", db)
    assert h["state"] == "closed" and h["waiting_on"]["approvals"] == []
    kinds = [e["kind"] for e in h["events"]]
    assert kinds == ["opened", "escalated", "approval_decided", "handed_over", "approval_decided", "closed"]
    closed = h["events"][-1]
    assert closed["actor"] == "manager@bank" and closed["at"] == "2026-11-01 15:00:00"
    assert "closed_fraud" in closed["detail"] and f"approval:{a2}" in closed["evidence_ids"] and "trigger" in closed["evidence_ids"]
    assert closed["graph_written"] == 1 and "close_case" in closed["graph_note"]
    with pytest.raises(lifecycle.CaseClosed):
        lifecycle.record_event("HHF-900", "evidence_added", "x", path=db)


def test_rejection_closes_as_overridden(env, mock_mode):
    db = env["db"]
    a1 = adb.enqueue("HHF-900", "BLOCK_CARD", "L1", "R2", db)
    adb.decide(a1, "rejected", adb.authenticate("lead@bank", "lead passphrase 1", env["dir"]), db)
    ev = lifecycle.events("HHF-900", db)
    assert ev[-1]["kind"] == "closed" and "closed_fraud_overridden" in ev[-1]["detail"]


def test_customer_reply_closes_when_nothing_pending(env, mock_mode):
    out = lifecycle.on_customer_reply("HHF-900", "I did not make these purchases", "ana@bank", env["db"])
    assert out["closed"]["outcome"] == "closed_fraud" and out["closed"]["graph"]["ok"]
    names = out["closed"]["graph"]["tool_calls"]
    assert names == ["tigergraph__run_installed_query", "tigergraph__run_installed_query"]


def test_customer_reply_with_pending_approval_only_adds(env, mock_mode):
    adb.enqueue("HHF-900", "BLOCK_CARD", "L1", "R2", env["db"])
    out = lifecycle.on_customer_reply("HHF-900", "yes that was me", "ana@bank", env["db"])
    assert "closed" not in out and lifecycle.state("HHF-900", env["db"]) == "added_to"


def test_close_params_shape(env):
    p = lifecycle.close_params("HHF-900", ANSWER, "closed_fraud", "m@bank", "2026-11-01 10:00:00", "ok")
    gsql = (Path(__file__).resolve().parents[2] / "graph" / "queries" / "close_case.gsql").read_text()
    for k in p:
        assert f" {k}" in gsql or f"({k}" in gsql or f"<{k}" in gsql, k
    assert p["ac"] == {"id": "AC-HHF-900"} and p["similar_closed_cases"] == {"CC-0001": 1.0}


def test_live_without_switch_writes_nothing(env, monkeypatch):
    monkeypatch.setattr(lifecycle, "_settings", lambda: dataclasses.replace(SETTINGS, run_mode="live"))
    monkeypatch.delenv("LIFECYCLE_GRAPH_WRITES", raising=False)
    out = lifecycle.on_customer_reply("HHF-900", "hi", "ana@bank", env["db"])
    assert out["closed"]["graph"]["mode"] == "off" and not out["closed"]["graph"]["ok"]


def test_lifecycle_rows_are_append_only(env, mock_mode):
    lifecycle.hand_over("HHF-900", "a", "b", path=env["db"])
    con = sqlite3.connect(env["db"])
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM case_lifecycle")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE case_lifecycle SET actor = 'x'")

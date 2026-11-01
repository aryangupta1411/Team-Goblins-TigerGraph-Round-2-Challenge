"""ops/alert_watcher.py (inbox -> one bench run per new alert) and agent/pricing.py + ops/cost_report.py."""
from __future__ import annotations

import json

import pytest

from agent import pricing
from ops import alert_watcher, cost_report

HEADER = "case_id,opened_at,trigger_type,flagged_txn_id,card_id,customer_id,risk_score,trigger_text\n"
ROW = "{cid},2017-01-15 01:25:19,analyst_request,3660108,C05061-K1,C05061,,Review it.\n"


def test_read_alerts_skips_bad_rows(tmp_path):
    (tmp_path / "a.csv").write_text(HEADER + ROW.format(cid="W-1") + ROW.format(cid="../etc") + "W-2,,x,,,,,\n")
    assert [r["case_id"] for _, r in alert_watcher.read_alerts(tmp_path)] == ["W-1"]


def test_sweep_starts_each_alert_once(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(alert_watcher, "start_case", lambda row, run_id, mock, work: started.append(row["case_id"]) or
                        {"case_id": row["case_id"], "answer_ready": False, "exit_code": 0})
    inbox, log = tmp_path / "in", tmp_path / "run" / "watcher.jsonl"
    inbox.mkdir(); log.parent.mkdir()
    (inbox / "a.csv").write_text(HEADER + ROW.format(cid="W-1"))
    alert_watcher.sweep(inbox, "r", True, log, {})
    (inbox / "b.csv").write_text(HEADER + ROW.format(cid="W-2") + ROW.format(cid="W-1"))
    alert_watcher.sweep(inbox, "r", True, log, {})
    alert_watcher.sweep(inbox, "r", True, log, {})
    assert started == ["W-1", "W-2"]
    assert [json.loads(x)["case_id"] for x in log.read_text().splitlines()] == ["W-1", "W-2"]


def test_call_cost_prices_each_token_kind():
    row = {"kind": "llm", "model": "gemini-2.5-flash", "input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    assert pricing.call_cost_usd(row) == pytest.approx(0.30 + 2.50 + 0.03)
    assert pricing.call_cost_usd({"kind": "llm", "model": "fake"}) is None
    c = pricing.case_cost([row, {"kind": "tool"}, {"kind": "llm", "model": "fake"}])
    assert c["llm_calls"] == 2 and c["unpriced_calls"] == 1 and c["usd"] == pytest.approx(2.83)


def test_cost_report(tmp_path):
    for cid, lat, out in (("A", 10.0, 1000), ("B", 30.0, 3000)):
        d = tmp_path / cid
        d.mkdir()
        (d / "answer.json").write_text(json.dumps({"case_id": cid, "latency_s": lat, "tokens": out}))
        (d / "calls.jsonl").write_text(json.dumps({"kind": "llm", "model": "gemini-2.5-flash", "output_tokens": out}) + "\n")
    rep = cost_report.report(tmp_path)
    assert rep["n_cases"] == 2 and rep["latency_s"]["p50"] == 20.0 and rep["latency_s"]["p95"] == 30.0
    assert rep["cost_usd_per_case"]["p95"] == pytest.approx(0.0075) and rep["cost_usd_total"] == pytest.approx(0.01)

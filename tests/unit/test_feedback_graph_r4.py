"""engine/feedback.py (Challenge 8), the SHARES_DEVICE neighbourhood item and the R4 / uncertain_final action sets."""

from engine import feedback
from engine.policy import params
from engine.scorecard import _graph_neighbourhood
from engine.types import CaseContext

CTX = CaseContext("HHX-001", "risk_score", "", "1", "C1-K1", "C1", "2017-01-10 00:00:00", 0.5)
P = params()["engine"]


def _row(i, card, kind, decision):
    return {"id": i, "case_id": "HHX-000", "card_id": card, "finding_kind": kind, "decision": decision,
            "decided_by": "lead@bank", "role": "team_lead", "reason": "r", "at": "2026-11-01 10:00:00"}


def test_no_rows_adds_nothing():
    out = feedback.apply(CTX, {"r2_detector_hit": True}, [], P, fb_rows=[], detector_id="D4")
    assert out == {"evidence": [], "adjustments": [], "lifted": set(), "rows": []}


def test_rejected_card_finding_is_attested_and_lifts_its_floor():
    flags = {"r2_detector_hit": True, "r2_detector_pattern": "undocumented"}
    out = feedback.apply(CTX, flags, [], P, fb_rows=[_row(7, "C1-K1", "D4", "rejected")], detector_id="D4")
    (ev,) = out["evidence"]
    assert ev.direction == "legit" and ev.family == "memory" and ev.ref == "feedback:analyst_feedback#7"
    assert "lead@bank (team_lead)" in ev.claim
    assert out["adjustments"][0][1] == P["feedback"]["rejected_bump"]
    assert out["lifted"] == {"D4"}


def test_customer_scope_is_weaker_and_never_lifts_a_floor():
    flags = {"r2_detector_hit": True, "r2_detector_pattern": "undocumented"}
    out = feedback.apply(CTX, flags, ["C1-K2"], P, fb_rows=[_row(1, "C1-K2", "D4", "rejected"), _row(2, "C9-K1", "D4", "rejected")], detector_id="D4")
    assert len(out["evidence"]) == 1 and out["rows"][0]["scope"] == "customer"
    assert out["adjustments"][0][1] == P["feedback"]["rejected_bump"] * P["feedback"]["customer_scope_factor"]
    assert out["lifted"] == set()


def test_feedback_on_a_finding_the_case_does_not_carry_is_ignored():
    out = feedback.apply(CTX, {}, [], P, fb_rows=[_row(1, "C1-K1", "ring", "accepted")])
    assert out["evidence"] == []


def test_graph_neighbourhood_prefers_the_algorithm_attributes_and_falls_back_to_the_twin():
    sos = {"devices": [{"device_id": "d1", "is_strong": True, "n_cards_30d": 4, "n_fraud_cases": 1}]}
    live = _graph_neighbourhood({"wcc_id": 12, "device_degree": 3}, sos)
    assert live["source"] == "tg_wcc/degree" and live["degree"] == 3 and live["fires"]
    twin = _graph_neighbourhood({"wcc_id": -1, "device_degree": 0}, sos)
    assert twin["source"].startswith("offline twin") and twin["degree"] == 3 and twin["fires"]
    assert not _graph_neighbourhood({"wcc_id": -1, "device_degree": 0}, {"devices": [dict(sos["devices"][0], n_fraud_cases=0)]})["fires"]
    assert P["graph_neighbourhood_bump"] < P["family_strength"]


def test_uncertain_cases_hold_the_decline_until_a_real_reply_lapses():
    rules = params()["rules"]
    assert "DECLINE_TRANSACTION" not in rules["uncertain_final"]["require"]
    assert "DECLINE_TRANSACTION" not in rules["R4"]["require"]
    assert {"when": ["not_verdict_uncertain"], "actions": ["DECLINE_TRANSACTION"]} in rules["R4"]["add_if"]
    assert rules["R8"]["require"] == ["ESCALATE_TO_ANALYST"]

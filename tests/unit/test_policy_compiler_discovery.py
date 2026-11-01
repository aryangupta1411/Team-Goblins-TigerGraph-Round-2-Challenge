"""Challenge 3 (policy prose -> detector) and Challenge 2 (mined, unlabelled pattern -> pending proposal)."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import config
from engine import policy_compiler as pc

needs_db = pytest.mark.skipif(not Path(config.FACTS_DB).exists(), reason=f"needs the facts DB ({config.FACTS_DB})")


def test_r5_thresholds_come_from_the_policy_text():
    det = pc.compile_rule("R5")
    by = {p["param"]: p for p in det["parse"]}
    assert det["params"]["min_small"] == 3 and det["params"]["window_min"] == 60 and det["params"]["block_over"] == 100.0
    assert by["min_small"]["matched_text"].lower() == "three or more" and by["window_min"]["matched_text"] == "within an hour"
    assert by["small_max"]["source"] == "operational_default"           # the text says "small" with no number
    assert det["rule_text"] in Path(pc.POLICY_MD).read_text(encoding="utf-8").replace("**", "").replace("R5. ", "")


def test_a_changed_threshold_in_the_text_changes_the_detector(tmp_path):
    md = Path(pc.POLICY_MD).read_text(encoding="utf-8").replace("Three or more small", "Five or more small").replace("over $100", "over $250")
    (tmp_path / "p.md").write_text(md, encoding="utf-8")
    params = {p["param"]: p["value"] for p in pc.parse_r5(pc.rule_text("R5", tmp_path / "p.md"))}
    assert params["min_small"] == 5 and params["block_over"] == 250.0


def test_missing_rule_language_fails_closed():
    with pytest.raises(ValueError):
        pc.parse_r5("Card testing. Some small purchases then a big one.")
    with pytest.raises(KeyError):
        pc.rule_text("R99")


def test_r7_parse():
    p = pc.compile_rule("R7")["params"]
    assert p["gap_band_days"] == [25, 35] and p["forbid_block"] and p["trigger"] == "customer_report"


@needs_db
def test_r5_backtest_and_rule_versus_agent():
    from engine.detectors import compile_policy_rule_detector

    r = compile_policy_rule_detector("R5", write=False)
    bt, cmp = r["backtest"], r["comparison"]
    assert bt["closed_cases_total"] == 5585 and bt["card_testing_cases_total"] == 18
    assert len(bt["caught_card_testing"]) + len(bt["missed_card_testing"]) == 18
    assert cmp["n"] == 38 and cmp["agree"] + cmp["disagree"] == 38
    hhf7 = next(c for c in cmp["cases"] if c["case_id"] == "HHF-007")
    assert hhf7["rule_fires"] and hhf7["query"] and hhf7["as_of"] and hhf7["params"]["card_id"]


@needs_db
def test_discovery_files_a_pending_proposal_outside_the_existing_toolkit():
    import duckdb

    from engine import discovery

    con = duckdb.connect(str(discovery.DB), read_only=True)
    out = discovery.discover(con, write=False)
    p = out["proposal"]
    assert p and p["proposal_id"].startswith("AUTO-DISC-") and p["approval_status"] in ("pending", "approved", "rejected")
    covered = set().union(*discovery.covered_ids(con).values())
    assert not set(p["backtest"]["january_txn_ids"]) & covered

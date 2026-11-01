"""tests/unit/test_containment.py — Challenge 7: the block-k curve and the smallest block set (engine/containment.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import containment as C


def _card(cid: str, post: float, legit: float = 0.0, cleared: bool = False) -> dict:
    return {"card_id": cid, "customer_id": "C" + cid, "exposure_prevented_post_asof_usd": post, "legit_spend_after_asof_usd": legit,
            "legit_txns_after_asof": int(legit > 0), "is_legitimate_customer_harmed": cleared or legit > 0}


def test_cards_are_ranked_by_exposure_prevented_and_the_curve_accumulates():
    curve = C.block_curve([_card("a", 10), _card("b", 500, legit=40), _card("c", 0), _card("d", 500)], penalty_usd=25)
    assert [p["card_added"] for p in curve] == [None, "d", "b", "a", "c"]          # tie at 500: less legit spend first
    assert [p["exposure_stopped_usd"] for p in curve] == [0, 500, 1000, 1010, 1010]
    assert [p["legit_customers_touched"] for p in curve] == [0, 0, 1, 1, 1]
    assert curve[2]["legit_customers"] == [{"customer_id": "Cb", "card_id": "b", "legit_spend_after_asof_usd": 40, "legit_txns_after_asof": 1}]


def test_the_smallest_set_maximising_stopped_minus_penalty_is_chosen():
    best = C.choose_block_set(C.block_curve([_card("a", 10, legit=5), _card("b", 500), _card("c", 0)], penalty_usd=25))
    assert best["cards"] == ["b"] and best["k"] == 1 and best["objective_usd"] == 500     # a stops $10 but costs $25; c stops nothing
    assert C.choose_block_set(C.block_curve([_card("a", 0)], 25))["k"] == 0              # nothing left to prevent: block nothing


@pytest.mark.skipif(not C.SUMMARY.exists(), reason="no containment summary")
def test_the_summary_keeps_the_keys_the_ui_reads_and_adds_the_curve():
    data = json.loads(Path(C.SUMMARY).read_text())
    for row in data.values():
        assert {"wave", "cards", "exposure", "pre_as_of", "post_as_of", "legit_touched_targeted", "broad_rule_extra_customers"} <= set(row)
        assert len(row["curve"]) == row["cards"] + 1 and row["block_k"] == len(row["block_set"]) <= row["cards"]
        assert row["block_exposure_stopped"] <= row["post_as_of"] + 0.01

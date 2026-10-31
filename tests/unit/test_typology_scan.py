"""The January typology detectors behind the `typology_scan` query: the graph's rows and the DuckDB twin give one DetectorHit."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import config  # noqa: E402
from engine.detectors import episode_rows, hit_from_scan, scan_card, scan_rows  # noqa: E402

needs_db = pytest.mark.skipif(not config.FACTS_DB.exists(), reason="engine facts DuckDB not present")

def wave(card_ids, per_card, ts="2017-01-10 10:00:00"):
    return [{"id": str(3660000 + 10 * i + j), "card_id": c, "ts": ts, "amt": 3.0, "device_id": ""} for i, c in enumerate(card_ids) for j in range(per_card)]


def test_hit_from_scan_builds_the_hit_from_graph_rows():
    cards = [f"C0000{i}-K1" for i in range(5)]
    scan = {"d1_rows": [], "d2_rows": [], "d3_rows": wave(cards, 4), "d4_rows": [], "d4_domain": "",
            "card_rows": [{"id": "3660000", "ts": "2017-01-10 10:00:00", "amt": 3.0, "product_cd": "C", "channel": "online", "addr1": "", "p_email": "", "r_email": "",
                           "device_id": "", "device_new": "", "proxy_type": "", "cms_p": 0.1}]}
    hit = hit_from_scan(scan, cards[0], "3660000", "2017-01-31 00:00:00")
    assert hit and hit.detector_id == "D3_MICRO_CHARGE_SKIMMING" and hit.pattern == "undocumented"
    assert hit.episode_txn_ids == ["3660000", "3660001", "3660002", "3660003"] and hit.connected_card_ids == sorted(cards[1:])
    assert hit.exposure_usd == 12.0 and hit.attestation["what"]
    rows = episode_rows(scan, hit)
    assert [r["id"] for r in rows] == ["3660000"] and rows[0]["sig_match"] and rows[0]["proxy"] == "" and "proxy_type" not in rows[0]


def test_hit_from_scan_needs_the_flagged_transaction_in_the_episode():
    cards = [f"C0000{i}-K1" for i in range(3)]
    scan = {"d3_rows": wave(cards, 4)}
    assert hit_from_scan(scan, cards[0], "3669999", "2017-01-31 00:00:00") is None       # flagged txn is not part of the wave
    assert hit_from_scan(scan, "C99999-K1", "3660000", "2017-01-31 00:00:00") is None    # card is not in the wave


def test_hit_from_scan_does_not_run_before_january():
    cards = [f"C0000{i}-K1" for i in range(3)]
    assert hit_from_scan({"d3_rows": wave(cards, 4)}, cards[0], "3660000", "2016-12-31 23:59:59") is None


def test_d4_rows_win_the_precedence():
    cards = [f"C0000{i}-K1" for i in range(12)]
    scan = {"d4_rows": wave(cards, 1), "d4_domain": "rare.example", "d3_rows": wave(cards[:3], 4)}
    hit = hit_from_scan(scan, cards[0], "3660000", "2017-01-31 00:00:00")
    assert hit and hit.detector_id == "D4_RECIPIENT_EMAIL_MULE" and "rare.example" in hit.name


@needs_db
@pytest.mark.parametrize("case_id", ["HHF-001", "HHF-002", "HHF-003", "HHF-004", "HHF-005", "HHF-006", "HHF-007", "HHF-010"])
def test_duckdb_twin_matches_scan_card(case_id):
    import duckdb

    con = duckdb.connect(str(config.FACTS_DB), read_only=True)
    card, tid, opened = con.execute("SELECT card_id, CAST(flagged_txn_id AS VARCHAR), strftime(opened_at, '%Y-%m-%d %H:%M:%S') FROM cp_all WHERE case_id = ?", [case_id]).fetchone()
    direct = scan_card(con, card, tid, opened)
    via_scan = hit_from_scan(scan_rows(con, card, tid, opened), card, tid, opened)
    assert (direct is None) == (via_scan is None)
    if direct is not None:
        assert (direct.detector_id, direct.episode_txn_ids, direct.exposure_usd, direct.connected_card_ids, direct.p_floor) == \
               (via_scan.detector_id, via_scan.episode_txn_ids, via_scan.exposure_usd, via_scan.connected_card_ids, via_scan.p_floor)


@needs_db
def test_the_waves_are_the_ones_the_innovation_doc_names():
    import duckdb

    con = duckdb.connect(str(config.FACTS_DB), read_only=True)
    hits = {}
    for cid in ("HHF-001", "HHF-002", "HHF-003", "HHF-004", "HHF-005", "HHF-006"):
        card, tid, opened = con.execute("SELECT card_id, CAST(flagged_txn_id AS VARCHAR), strftime(opened_at, '%Y-%m-%d %H:%M:%S') FROM cp_all WHERE case_id = ?", [cid]).fetchone()
        h = hit_from_scan(scan_rows(con, card, tid, opened), card, tid, opened)
        hits[cid] = h.detector_id if h else None
    assert hits == {"HHF-001": "D1_MOBILE_ANON_PROXY_WAVE", "HHF-002": "D1_MOBILE_ANON_PROXY_WAVE", "HHF-003": "D2_ESCALATING_C_DRAIN",
                    "HHF-004": "D3_MICRO_CHARGE_SKIMMING", "HHF-005": "D3_MICRO_CHARGE_SKIMMING", "HHF-006": "D4_RECIPIENT_EMAIL_MULE"}

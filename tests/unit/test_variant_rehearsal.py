"""tests/unit/test_variant_rehearsal.py — Challenge 13: map a reshaped variant, translate it, decide its cases (ops/variant_rehearsal.py)."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ops import variant_rehearsal as V
from schema_engine.mapper import infer_variant_mapping

ROOT = Path(__file__).resolve().parents[2]
HAVE_FACTS = V.FACTS.exists()
HAVE_RAW = (V.ROUND2 / "transactions_2017q1.csv").exists()


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def test_a_split_timestamp_and_separate_device_columns_are_mapped_from_values(tmp_path):
    _write(tmp_path / "p.csv", ["clock", "ref", "who", "usd_value", "line", "presence", "day"],
           [[f"0{h}:1{h}:00", f"36000{h:02d}", f"C0000{h}", f"{50 + h}.25", "WCHSR"[h % 5], ["card_present", "card_not_present"][h % 2], "2017-01-0" + str(h + 1)]
            for h in range(8)])
    _write(tmp_path / "s.csv", ["ref", "agent", "platform"],
           [[f"36000{h:02d}", ["chrome 62.0", "android browser 4.0", "safari generic"][h % 3], ["Windows 10", "Android 7.0", "iOS 11.1"][h % 3]] for h in range(8)])
    _write(tmp_path / "t.csv", ["ticket", "words"],
           [[f"HHF-00{h}", "the customer called to say this charge on the card was not made by them at all"] for h in range(1, 6)])
    m = {(r["source_file"], r["source_column"]): (r["canonical_column"], r["transform"]) for r in infer_variant_mapping(tmp_path)["mappings"]}
    assert m[("p.csv", "day")] == ("ts", "date_part") and m[("p.csv", "clock")] == ("ts", "time_part")
    assert m[("p.csv", "presence")] == ("channel", "channel_synonym")
    assert m[("s.csv", "agent")][0] == "id_31" and m[("s.csv", "platform")][0] == "id_30"   # 'android browser' is a browser
    assert m[("t.csv", "words")][0] == "trigger_text"


def test_a_flat_file_maps_identity_columns_and_lists_what_it_cannot_map(tmp_path):
    _write(tmp_path / "flat.csv", ["ref", "who", "usd_value", "form", "seen", "agent", "junk"],
           [[f"36000{h:02d}", f"C0000{h}", f"{50 + h}.25", ["mobile", "desktop"][h % 2], ["New", "Found"][h % 2],
             ["chrome 62.0", "safari generic"][h % 2], f"zz{h}"] for h in range(8)])
    rep = infer_variant_mapping(tmp_path)
    m = {r["source_column"]: r["canonical_column"] for r in rep["mappings"]}
    assert rep["table_roles"] == {"flat.csv": "transactions"}
    assert m["form"] == "DeviceType" and m["seen"] == "id_15" and m["agent"] == "id_31" and m["ref"] == "TransactionID"
    assert [u["source_column"] for u in rep["unmapped_columns"]] == ["junk"]
    score = V.score_mapping(rep, {"flat.csv": {"ref": "TransactionID", "form": "DeviceType", "junk": None, "who": "customer_id"}})
    assert score["precision"] == 1.0 and score["recall"] == 1.0


@pytest.mark.skipif(not (HAVE_FACTS and HAVE_RAW), reason="needs the facts DB and the raw January files (git-ignored)")
def test_a_fresh_variant_translates_to_the_canonical_rows_and_the_same_decisions(tmp_path):
    vdir = V.make_fresh_variant(tmp_path / "src", max_tx_rows=300)
    rep = V.rehearse(vdir, tmp_path / "out", ["HHF-001", "HHF-008"])
    assert rep["row_comparison"]["all_equal"], {c: v["examples"] for c, v in rep["row_comparison"]["columns"].items() if v["differ"]}
    assert rep["n_same_decision"] == rep["n_cases"] == 2
    assert (tmp_path / "out" / "mapping.json").exists()
    assert rep["accuracy"]["precision"] == rep["accuracy"]["recall"] == 1.0


@pytest.mark.skipif(not HAVE_FACTS, reason="needs the facts DB")
def test_bank_b_decides_every_case_as_the_canonical_schema_does(tmp_path):
    rep = V.rehearse(ROOT / "data" / "variant_bank_b", tmp_path, ["HHF-001", "HHF-004"])
    assert rep["n_same_decision"] == 2
    assert rep["accuracy"]["recall"] == 1.0, rep["accuracy"]
    # Bank B's export drops the 'charge card' funding type: the one row it cannot place on its card is reported, not guessed right
    differ = {c: v["differ"] for c, v in rep["row_comparison"]["columns"].items() if v["differ"]}
    assert differ in ({}, {"card_id": 1})

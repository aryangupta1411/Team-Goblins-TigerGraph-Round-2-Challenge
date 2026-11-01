"""agent.sar_doc: the fileable sar.md (cited sentences, timeline, evidence graph, attestation)."""
import json
from pathlib import Path

import pytest

from agent import sar_doc
from engine import config

ROOT = Path(__file__).resolve().parents[2]
ANS = ROOT / "runs" / "r2-submit" / "HHF-001" / "answer.json"

pytestmark = pytest.mark.skipif(not (config.FACTS_DB.exists() and ANS.exists()), reason="facts DB or HHF-001 run missing")


def _answer():
    return json.loads(ANS.read_text())


def test_every_sentence_cited_and_sections_present():
    md, st = sar_doc.render(_answer(), "HHF-001", config.FACTS_DB)
    assert st["sentences"] and st["cited"] == st["sentences"]
    nar = md.split("## Narrative")[1].split("## Timeline")[0].strip().splitlines()
    assert len(nar) == st["sentences"] and all(l.rstrip().endswith("]") and "[" in l for l in nar)
    for h in ("## Subjects", "## Timeline", "## Evidence graph", "```mermaid", "## Attestation", "Filing reason (3a)"):
        assert h in md
    assert "| 3660106 | C05061-K1 | $850.28 | online |" in md


def test_unattested_sentence_dropped():
    a = _answer()
    a["sar"]["narrative"] += (" On 2017-01-13 at 03:03 a $9,999.99 purchase was made."
                              " Transaction 1234567 was declined. Card C99999-K9 was also used.")
    _, st = sar_doc.render(a, "HHF-001", config.FACTS_DB)
    assert st["sentences"] - st["cited"] == 3 and len(st["dropped"]) == 3


def test_invented_amount_dropped_real_one_kept():
    s = sar_doc.split_sentences("The total was $2,189.02. A refund of $17.17 followed.")
    a = _answer()
    a["sar"]["narrative"] = " ".join(s)
    _, st = sar_doc.render(a, "HHF-001", config.FACTS_DB)
    assert st["cited"] == 1 and "17.17" in st["dropped"][0][1]


def test_no_sar_and_write_does_not_touch_answer(tmp_path):
    a = _answer()
    before = json.dumps(a, sort_keys=True)
    sar_doc.write(tmp_path / "sar.md", a, "HHF-001", config.FACTS_DB)
    assert json.dumps(a, sort_keys=True) == before
    a["sar"] = {"file": False, "reason": "3a not met: x", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []}
    md, _ = sar_doc.render(a, "HHF-001", config.FACTS_DB)
    assert md.startswith("# No SAR filed") and "3a not met" in md


def test_write_falls_back_without_db(tmp_path):
    st = sar_doc.write(tmp_path / "sar.md", _answer(), "HHF-001", tmp_path / "missing.duckdb")
    assert "error" in st and (tmp_path / "sar.md").read_text().startswith("This report")

"""Emit the ClosedCase embedding input from the ETL's `closed_case_parsed.embed_text` (decisions.md D11,
00-integration.md M11): the text that is embedded into `ClosedCase.note_emb` is byte-for-byte the text the
loader puts into `ClosedCase.embed_text`, so retrieval, the graph attribute and the AgentCase vectors all
live in one space.

    uv run python -m rag.closedcase_embed_text                  # reads DuckDB `closed_case_parsed` (built by etl.parse_closed_cases)
    uv run python -m rag.closedcase_embed_text --db data/hhgoa.duckdb --out data/out
    uv run python -m rag.closedcase_embed_text --standalone     # no `closed_case_parsed` yet: parse `cc` here (same masking), warn

Output (data/out/):
  closed_case_embed.jsonl    {id, text} - input for `rag.embed --input ... --out data/out/vec_ClosedCase.psv`

NOT written here any more: `closed_case_parsed.csv` (the ETL exports `closed_case.csv` with template_id, note_region,
note_device, note_evidence_type and embed_text straight from its DuckDB table; etl/export_graph_csvs.py).

Shared with the ETL: `mask(note)` and `embed_text(pattern, outcome, note_device, note_region, note)` are imported from
etl/parse_closed_cases.py (one implementation, contracts/schema.md rule "template-normalised note with amounts, scores,
ids and dates masked, prefixed by pattern: X | outcome: Y | device: Z | region: R |"). rag.retrieval.case_query_text
uses the same `mask` on the query side.

Exported for the agent (agent/persist.py): `agent_case_embed_text(answer, device_profile, region)` builds the AgentCase
text in exactly that style (pattern | outcome | device | region | masked summary) for `AgentCase.note_emb`.

The ETL's six note templates (verified on all 5,565 rows; etl/parse_closed_cases.py):
  fraud_reported 4,656 · cleared_travel 716 · cleared_new_phone 158 · cleared_amount 26 · undoc_ring 4 · undoc_burst 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from etl.parse_closed_cases import embed_text, mask, parse_note

from . import config

__all__ = ["mask", "embed_text", "parse_note", "agent_case_embed_text", "build", "main"]


def agent_case_embed_text(answer: dict, device_profile: str = "", region: str = "") -> str:
    """AgentCase.note_emb text in the ClosedCase.embed_text style (answer = README JSON). `device_profile` /
    `region` are the flagged transaction's DeviceProfile id and addr1 (from case_context), passed by agent/persist."""
    case = answer.get("case", {})
    verdict = case.get("verdict", "uncertain")
    outcome = {"fraud": "confirmed_fraud", "legitimate": "cleared"}.get(verdict, "uncertain")
    pieces = [case.get("summary", "")]
    if case.get("pattern_description"):
        pieces.append(case["pattern_description"])
    reqs = answer.get("evidence_requests", [])
    if reqs:
        pieces.append("Evidence requested: " + "; ".join(r.get("assumed_response", "") for r in reqs))
    finals = answer.get("next_best_actions", {}).get("final", [])
    if finals:
        pieces.append("Actions: " + ", ".join(a.get("action", "") for a in finals))
    note = " ".join(" ".join(pieces).split())
    return embed_text(case.get("pattern") or "none", outcome, device_profile, region, note)


def _standalone_rows(con) -> list[tuple[str, str]]:
    """Fallback when the ETL table is absent: parse `cc` here with the ETL's own parser/mask (same text)."""
    # note_region exactly as etl/parse_closed_cases.build: modal addr1 of the case's in-person transactions
    region = dict(con.execute("""
        SELECT case_id, addr1 FROM (
          SELECT p.case_id, t.addr1, row_number() OVER (PARTITION BY p.case_id ORDER BY count(*) DESC, min(t.ts)) rk
          FROM pairs p JOIN txc t USING (TransactionID)
          WHERE p.src = 'closed' AND t.channel = 'in_person' AND t.addr1 <> '' GROUP BY 1, 2) WHERE rk = 1
    """).fetchall())
    rows = con.execute("SELECT case_id, outcome, pattern, analyst_notes FROM cc ORDER BY case_id").fetchall()
    out = []
    for case_id, outcome, pattern, note in rows:
        p = parse_note(note or "")
        out.append((case_id, embed_text(pattern, outcome, p["note_device"], region.get(case_id, ""), note or "")))
    return out


def build(db_path: Path, out_dir: Path, standalone: bool = False) -> int:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    tables = {r[0] for r in con.execute("show tables").fetchall()}
    if "closed_case_parsed" in tables and not standalone:
        rows = con.execute("SELECT id, embed_text FROM closed_case_parsed ORDER BY id").fetchall()
        source = "closed_case_parsed.embed_text (ETL)"
    else:
        if not standalone:
            print("closed_case_parsed not in DuckDB - run `python -m etl.parse_closed_cases <db>` first "
                  "(falling back to --standalone parsing of `cc`; the text is built by the same ETL functions)", file=sys.stderr)
        rows = _standalone_rows(con)
        source = "standalone parse of cc"
    out_dir.mkdir(parents=True, exist_ok=True)
    # sanity: no case id / card id / cent amount survives the ETL mask ("$500" in the burst template is pattern text)
    unmasked = re.compile(r"\bCC-\d{4}\b|\bC\d{5}(?:-K\d)?\b|\$[\d,]+\.\d{2}")
    bad = 0
    with (out_dir / "closed_case_embed.jsonl").open("w") as fj:
        for case_id, text in rows:
            if unmasked.search(text):
                bad += 1
            fj.write(json.dumps({"id": case_id, "text": text}) + "\n")
    print(f"wrote {len(rows)} rows -> {out_dir}/closed_case_embed.jsonl from {source}; unmasked rows: {bad}")
    return 0 if rows and bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=config.DUCKDB_PATH)
    ap.add_argument("--out", type=Path, default=config.OUT_DIR)
    ap.add_argument("--standalone", action="store_true", help="parse cc here instead of reading closed_case_parsed")
    a = ap.parse_args(argv)
    return build(a.db, a.out, a.standalone)


if __name__ == "__main__":
    sys.exit(main())

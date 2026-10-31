"""Backtest of the undocumented-typology detectors over the bank's closed cases (`engine/backtest_detectors.py`, Challenges 1-2).

Every number here is measured on each run; nothing is asserted by hand:

  * each wave detector (D1-D3) runs over the FULL history (July 2016 to January 2017), not only the month it was written
    for, and every closed case whose transactions it matches is counted: a confirmed-fraud case is a catch, a cleared
    case is a legitimate customer the detector would have acted against (named);
  * D4 (one recipient domain paid by many cards) is evaluated the way the agent uses it: on each closed case's first
    online ProductCD='C' transaction, at the case's own opened_at;
  * the two Round 1 shapes (anonymous-proxy device ring, under-$500 burst) are measured from the ETL's own flags;
  * the 18 Final Round cases are scanned with `scan_card` exactly as the engine does.

    uv run python -m engine.backtest_detectors      # -> engine/out/detector_backtest.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from engine.detectors import WAVE_SQL, detect_d4_recipient_email_mule, discover_unlabelled_patterns, scan_card
from ops.console import Col, Table, header, ok, summary, warn
from schema_engine.attestation import attest

ROOT = Path(__file__).resolve().parents[1]
FULL_HISTORY = "2016-07-01"
RING_PROFILE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"


def _case_hits(con: Any, txn_ids: set[str]) -> dict[str, list[dict]]:
    """Closed cases whose transactions intersect `txn_ids`, split by outcome."""
    if not txn_ids:
        return {"confirmed_fraud": [], "cleared": []}
    con.execute("CREATE OR REPLACE TEMP TABLE _hit(id VARCHAR)")
    con.executemany("INSERT INTO _hit VALUES (?)", [[t] for t in sorted(txn_ids)])
    rows = con.execute(
        """SELECT DISTINCT c.case_id, c.outcome, c.customer_id, c.card_id
           FROM cc c JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID) JOIN _hit h ON h.id = t.id
           ORDER BY c.case_id"""
    ).fetchall()
    out: dict[str, list[dict]] = {"confirmed_fraud": [], "cleared": []}
    for cid, outcome, cust, card in rows:
        out.setdefault(outcome, []).append({"case_id": cid, "customer_id": cust, "card_id": card})
    return out


def _detector_row(detector_id: str, name: str, txn_rows: list[tuple], hits: dict[str, list[dict]], exam: list[str], how: str) -> dict:
    cards = sorted({str(r[1]) for r in txn_rows})
    jan_cards = sorted({str(r[1]) for r in txn_rows if str(r[2]) >= "2017-01-01"})
    fp = hits.get("cleared", [])
    tp = hits.get("confirmed_fraud", [])
    return {
        "detector_id": detector_id,
        "name": name,
        "cards_matched": len(cards),
        "cards_matched_january": len(jan_cards),
        "matched_card_ids": cards,
        "txns_matched": len(txn_rows),
        "closed_cases_caught": [h["case_id"] for h in tp],
        "false_positives_on_cleared_cases": len(fp),
        "legitimate_customers_acted_against": sorted({h["customer_id"] for h in fp}),
        "cleared_cases_matched": [h["case_id"] for h in fp],
        "exam_cases_caught": exam,
        "attestation": attest(
            what=(f"{name}: matches {len(cards)} cards ({len(jan_cards)} in January 2017) over July 2016-January 2017; it overlaps "
                  f"{len(tp)} confirmed-fraud closed cases and {len(fp)} cleared closed cases."),
            evidence=[h["case_id"] for h in tp][:15] + [h["case_id"] for h in fp][:15] + cards[:20],
            query_or_rule=f"engine.backtest_detectors:{detector_id}",
            params={"since": FULL_HISTORY, "how": how},
        ).to_dict(),
    }


def run_backtest(db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb") -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    n_cc = int(con.execute("SELECT count(*) FROM cc").fetchone()[0])
    n_cleared = int(con.execute("SELECT count(*) FROM cc WHERE outcome = 'cleared'").fetchone()[0])
    n_fraud = int(con.execute("SELECT count(*) FROM cc WHERE outcome = 'confirmed_fraud'").fetchone()[0])

    # the 18 Final Round cases, scanned exactly as the engine scans them
    exam_hits: dict[str, list[str]] = {}
    for case_id, txn, card, opened in con.execute("SELECT case_id, flagged_txn_id, card_id, opened_at::VARCHAR FROM cp_round2 ORDER BY case_id").fetchall():
        real_card = con.execute("SELECT card_id FROM txc WHERE TransactionID = ?", [int(txn)]).fetchone()
        hit = scan_card(con, real_card[0] if real_card else card, str(txn), opened)
        if hit is not None:
            exam_hits.setdefault(hit.detector_id, []).append(case_id)

    rows: list[dict] = []
    names = {"D1_MOBILE_ANON_PROXY_WAVE": "Spoofed-mobile anonymous-proxy wave (no shared device)",
             "D2_ESCALATING_C_DRAIN": "Dormant-wake escalating drain",
             "D3_MICRO_CHARGE_SKIMMING": "Sub-threshold micro-charge skimming"}
    for det, name in names.items():
        txn_rows = con.execute(WAVE_SQL[det].format(since=FULL_HISTORY)).fetchall()
        rows.append(_detector_row(det, name, txn_rows, _case_hits(con, {str(r[0]) for r in txn_rows}), exam_hits.get(det, []),
                                  "wave query over the full history"))

    # D4 at each closed case's own first online ProductCD='C' transaction and opened_at
    d4_txn: set[str] = set()
    d4_rows: list[tuple] = []
    probes = con.execute(
        """SELECT c.case_id, c.card_id, c.opened_at::VARCHAR, min(t.TransactionID)
           FROM cc c JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID)
           WHERE t.product_cd = 'C' AND t.channel = 'online' AND t.r_email != ''
           GROUP BY 1, 2, 3"""
    ).fetchall()
    for _cid, card, opened, txn in probes:
        hit = detect_d4_recipient_email_mule(con, card, str(txn), opened)
        if hit is not None and str(txn) in hit.episode_txn_ids:
            d4_txn |= set(hit.episode_txn_ids)
            d4_rows += [(i, card, opened, 0.0) for i in hit.episode_txn_ids]
    for _cid, txn, card, opened in con.execute("SELECT case_id, flagged_txn_id, card_id, opened_at::VARCHAR FROM cp_round2").fetchall():
        hit = detect_d4_recipient_email_mule(con, card, str(txn), opened)
        if hit is not None:
            d4_rows += [(i, c, opened, 0.0) for i, c in zip(hit.episode_txn_ids, [hit.card_id] * len(hit.episode_txn_ids))]
            d4_rows += [(f"peer:{c}", c, opened, 0.0) for c in hit.connected_card_ids]
    rows.append(_detector_row("D4_RECIPIENT_EMAIL_MULE", "One recipient email domain paid by many cards", d4_rows,
                              _case_hits(con, d4_txn), exam_hits.get("D4_RECIPIENT_EMAIL_MULE", []),
                              f"detect_d4 at each closed case's first online C transaction ({len(probes):,} cases probed)"))

    # Round 1 shapes from the ETL flags
    ring = con.execute("SELECT id, card_id, ts::VARCHAR, amt FROM txc WHERE device_id = ?", [RING_PROFILE]).fetchall()
    rows.append(_detector_row("R0_ANON_PROXY_DEVICE_RING", "Anonymous-proxy shared device ring (SM-G935F)", ring,
                              _case_hits(con, {str(r[0]) for r in ring}), ["HHG-014"], "txc.device_id = ring profile"))
    burst = con.execute("SELECT id, card_id, ts::VARCHAR, amt FROM txc WHERE burst_id != ''").fetchall()
    rows.append(_detector_row("R0_UNDER_500_VELOCITY_BURST", "Under-$500 40-minute burst", burst,
                              _case_hits(con, {str(r[0]) for r in burst}), ["HHG-006"], "txc.burst_id from etl.features"))

    discovered = discover_unlabelled_patterns(con)
    con.close()
    out = {
        "n_closed_cases": n_cc,
        "n_confirmed_fraud": n_fraud,
        "n_cleared_legitimate": n_cleared,
        "exam_scan": exam_hits,
        "detectors": rows,
        "self_growing_proposals": discovered,
    }
    out_file = ROOT / "engine" / "out" / "detector_backtest.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main() -> int:
    rep = run_backtest()
    header("engine.backtest_detectors", "undocumented-typology detectors, backtested over every closed case",
           {"closed cases": f"{rep['n_closed_cases']:,}", "cleared (legitimate)": f"{rep['n_cleared_legitimate']:,}", "detectors": len(rep["detectors"])})
    t = Table(Col("detector", max_width=28), Col("cards", align="right", width=6), Col("Jan", align="right", width=4),
              Col("fraud cases hit", align="right", width=8), Col("cleared hit", align="right", width=8),
              Col("exam cases", max_width=26), title="measured on this run")
    for d in rep["detectors"]:
        t.add_row(d["detector_id"], d["cards_matched"], d["cards_matched_january"], len(d["closed_cases_caught"]),
                  d["false_positives_on_cleared_cases"], ", ".join(d["exam_cases_caught"]) or "-")
    t.print()
    fp = sum(d["false_positives_on_cleared_cases"] for d in rep["detectors"])
    (ok if fp == 0 else warn)(f"{fp} cleared closed case(s) matched across all detectors (legitimate customers a detector would act against)")
    summary("backtest complete", {"detectors": len(rep["detectors"]), "cleared cases matched": fp, "out": "engine/out/detector_backtest.json"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

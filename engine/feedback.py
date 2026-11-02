"""Challenge 8 (Analyst in the Loop): analyst accept / reject decisions become attested evidence in the next run.

The UI records a decision on a finding with ui.approvals_db.record_feedback (table `analyst_feedback`, append-only, the
principal and role taken from a signed session, never from the request). This module reads those rows read-only
(ui.approvals_db.feedback_rows_ro: no database is created when none exists) and turns the ones scoped to the case's card,
or to another card of the same customer, into ledger evidence the scorecard weighs like any other item:

  rejected finding on this card     memory / legit, log-odds feedback.rejected_bump; when the finding is the source of the
                                    probability floor (a Round-2 detector id, the ring, the burst, the R5 run) the floor is lifted
  accepted finding on this card     memory / fraud, log-odds feedback.accepted_bump
  either, on another card of the customer   the same direction at feedback.customer_scope_factor of the weight; never lifts a floor

Every item names the row id, the deciding principal and role, and when — the attestation the brief asks for. With zero
rows nothing is added, so the engine output is byte-identical to a run without this module. ENGINE_FEEDBACK=off disables
the read (frozen-answer runs).

    uv run python -m engine.feedback --demo    # before/after on the same cases, temp sqlite, writes engine/out/feedback_before_after.json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from engine.types import CaseContext, Evidence

FLOOR_KINDS = ("ring", "burst", "r5")


def rows(path: str | os.PathLike | None = None) -> list[dict]:
    """Every analyst_feedback row, oldest first; [] when disabled or when the database does not exist."""
    if os.getenv("ENGINE_FEEDBACK", "on").lower() in ("off", "0", "false"):
        return []
    from ui.approvals_db import feedback_rows_ro

    return sorted(feedback_rows_ro(None, path), key=lambda r: int(r["id"]))


def case_findings(flags: dict, detector_id: str = "") -> set[str]:
    """The finding kinds this case carries, in the vocabulary record_feedback stores (detector ids, pattern names)."""
    k = {"fraud_verdict"}
    if flags.get("r2_detector_hit"):
        k |= {detector_id, str(flags.get("r2_detector_pattern") or "")}
    if flags.get("ring_hit"):
        k.add("ring")
    if flags.get("burst_hit"):
        k.add("burst")
    if flags.get("r5_run"):
        k.add("r5")
    if (flags.get("graph_neighbourhood") or {}).get("fires"):
        k.add("graph_neighbourhood")
    return k - {""}


def floor_kind(flags: dict, detector_id: str = "") -> str:
    """The finding that sets the scorecard's probability floor, in the order compute() applies them."""
    if flags.get("ring_hit"):
        return "ring"
    if flags.get("r2_detector_hit"):
        return detector_id or str(flags.get("r2_detector_pattern") or "")
    if flags.get("burst_hit"):
        return "burst"
    if flags.get("r5_run"):
        return "r5"
    return ""


def apply(ctx: CaseContext, flags: dict, customer_cards: list[str], P: dict, fb_rows: list[dict] | None = None, detector_id: str = "") -> dict:
    """Evidence, log-odds adjustments and lifted floors from the feedback rows that touch this case's card or customer."""
    out: dict = {"evidence": [], "adjustments": [], "lifted": set(), "rows": []}
    fb_rows = rows() if fb_rows is None else fb_rows
    if not fb_rows:
        return out
    F = P["feedback"]
    findings = case_findings(flags, detector_id)
    for r in fb_rows:
        card = str(r.get("card_id") or "")
        scope = "card" if card == ctx.card_id else ("customer" if card and card in customer_cards else "")
        kind = str(r.get("finding_kind") or "")
        if not scope or kind not in findings:
            continue
        rejected = r["decision"] == "rejected"
        w = F["rejected_bump"] if rejected else F["accepted_bump"]
        if scope == "customer":
            w *= F["customer_scope_factor"]
        who = f"{r['decided_by']} ({r['role']}) at {r['at']}"
        claim = (
            f"Analyst feedback #{r['id']}: finding `{kind}` {r['decision']} on case {r['case_id']} by {who}"
            + (f" — {r['reason']}" if r.get("reason") else "")
            + ("" if scope == "card" else f"; scoped to customer {ctx.customer_id} through card {card}")
        )
        out["evidence"].append(
            Evidence(claim, "external", f"feedback:analyst_feedback#{r['id']}", [card, str(r["case_id"])], "memory", "legit" if rejected else "fraud", abs(w))
        )
        out["adjustments"].append((f"analyst feedback #{r['id']} ({kind} {r['decision']}, {scope} scope, {r['decided_by']})", w))
        if rejected and scope == "card" and kind == floor_kind(flags, detector_id):
            out["lifted"].add(kind)
        out["rows"].append({"id": r["id"], "case_id": r["case_id"], "card_id": card, "finding_kind": kind, "decision": r["decision"],
                            "decided_by": r["decided_by"], "role": r["role"], "at": r["at"], "scope": scope, "weight": w})
    return out


# ---------------------------------------------------------------------------------------------------- demo

IMPACT = ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION", "FILE_REPORT")
DEMO_CASES = ["HHF-001", "HHF-013", "HHG-007", "HHG-019"]
# (case, card the analyst decided on — None = the case's own card, finding kind, decision, reason). The HHG-007 row is recorded
# on the customer's OTHER card (C09933-K1), so it reaches HHG-007 through customer scope at reduced weight.
DEMO_FEEDBACK = [
    ("HHF-013", None, "D4_RECIPIENT_EMAIL_MULE", "rejected", "sc.rr.com payments are the cardholder's own utility bills (demo label)"),
    ("HHG-019", None, "graph_neighbourhood", "rejected", "shared Windows profile is an office machine, not a fraud neighbourhood (demo label)"),
    ("HHG-019", None, "fraud_verdict", "rejected", "cardholder reached by phone, recognises the purchase (demo label)"),
    ("HHG-007", "C09933-K1", "fraud_verdict", "rejected", "customer cleared the account-takeover alert on their other card (demo label)"),
    ("HHF-001", None, "D1_MOBILE_ANON_PROXY_WAVE", "accepted", "wave pattern confirmed (demo label)"),
]


def _run(case_ids: list[str]) -> dict:
    from engine.facts_duckdb import DuckFacts
    from engine.run_cases import contexts, run_one

    fx = DuckFacts()
    res = {}
    for ctx in contexts(fx, case_ids):
        r = run_one(ctx, fx)
        sc = r["sc_post"]
        fin = [a["action"] for a in r["answer"]["next_best_actions"]["final"]]
        res[ctx.case_id] = {"card_id": ctx.card_id, "verdict": sc.verdict, "p": sc.p_engine, "actions": fin,
                            "acted_against": sorted(set(fin) & set(IMPACT)), "feedback": sc.flags.get("analyst_feedback", [])}
    return res


def demo(out_path: Path) -> dict:
    from ui import approvals_db as adb

    case_ids = DEMO_CASES
    with tempfile.TemporaryDirectory() as td:
        db, directory = Path(td) / "approvals.sqlite", Path(td) / "principals.json"
        saved = {k: os.environ.get(k) for k in ("APPROVALS_DB", "ENGINE_FEEDBACK")}
        os.environ["APPROVALS_DB"] = str(db)
        os.environ.pop("ENGINE_FEEDBACK", None)
        before = _run(case_ids)
        adb.add_principal("lead@demo", "team_lead", "demo lead passphrase", directory)
        lead = adb.authenticate("lead@demo", "demo lead passphrase", directory)
        for case_id, card, kind, decision, reason in DEMO_FEEDBACK:
            adb.record_feedback(case_id, card or before[case_id]["card_id"], kind, decision, lead, reason, db)
        after = _run(case_ids)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rejected = {c for c, _, _, d, _ in DEMO_FEEDBACK if d == "rejected"}
    wrong_before = sorted(c for c, v in before.items() if c in rejected and v["acted_against"])
    wrong_after = sorted(c for c, v in after.items() if c in rejected and v["acted_against"])
    report = {
        "what": "Same cases scored by the deterministic engine before and after analyst feedback rows were recorded (temp sqlite).",
        "wrongly_acted_against": "a case whose final actions include BLOCK_CARD / BLOCK_ALL_CARDS / DECLINE_TRANSACTION / FILE_REPORT on a card "
                                 "or customer where an analyst rejected the finding (the analyst label is the demo's ground truth)",
        "feedback_rows": [{"case_id": c, "card_id": card or before[c]["card_id"], "finding_kind": k, "decision": d, "reason": r,
                           "decided_by": "lead@demo", "role": "team_lead"} for c, card, k, d, r in DEMO_FEEDBACK],
        "wrongly_acted_against_before": wrong_before,
        "wrongly_acted_against_after": wrong_after,
        "cases": {c: {"before": {k: before[c][k] for k in ("verdict", "p", "actions")},
                      "after": {k: after[c][k] for k in ("verdict", "p", "actions")},
                      "caused_by": after[c]["feedback"]} for c in case_ids},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true", help="before/after on the demo cases with a temp sqlite")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "out" / "feedback_before_after.json"))
    a = ap.parse_args()
    if not a.demo:
        print(json.dumps(rows(), indent=2))
        return
    rep = demo(Path(a.out))
    print(f"wrongly acted against: before {rep['wrongly_acted_against_before']} -> after {rep['wrongly_acted_against_after']}")
    for c, v in rep["cases"].items():
        print(f"{c}: {v['before']['verdict']} {v['before']['p']} {v['before']['actions']} -> {v['after']['verdict']} {v['after']['p']} {v['after']['actions']}"
              + "".join(f"\n    caused by #{f['id']} {f['finding_kind']} {f['decision']} by {f['decided_by']} ({f['scope']})" for f in v["caused_by"]))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()

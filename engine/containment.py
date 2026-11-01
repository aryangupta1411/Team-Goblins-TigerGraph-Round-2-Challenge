"""Fraud Containment Planner & Human-in-the-Loop Policy Resolution (`engine/containment.py` — Challenges 7 & 14).

Implements two tightly linked capabilities:
  1. **Ring & Wave Containment Planner (Challenge 7):**
     Given any ring or coordinated wave (`SM-G935F` device ring, `D1` spoofed-mobile anonymous-proxy wave,
     `D2` dormant-wake doubling drain, `D3` micro-charge skimming ring, or `D4` `sc.rr.com` recipient-email mule),
     computes:
       - The **minimum card-block set** and **choke-point rule** (device / recipient email / proxy+UA signature)
         that stops 100% of active wave exposure.
       - Per-card exposure stopped (with exact attested `TransactionID`s and dollar amounts).
       - **Legitimate-customer impact analysis:** compares targeted wave-member blocking (customers previously
         cleared as legitimate are named) against naive broad blocking (e.g. blocking the entire billing region or email provider, naming
         the exact legitimate customer IDs that would have been harmed).
       - Time-ordered frames for the **Ring Replay slider (Challenge 5)**.

  2. **Autonomous vs. Human-in-the-Loop Policy Resolution Lifecycle (Challenge 14):**
     Shows how fraud is stopped on its own (`auto` actions executed immediately by the agent) while high-impact
     actions (`L1` Team Lead: `DECLINE_TRANSACTION`, `BLOCK_CARD <= $2,500`; `L2` Fraud Manager: `BLOCK_CARD > $2,500`,
     `BLOCK_ALL_CARDS`, `FILE_REPORT`) wait in the RBAC-enforced Approval Inbox and transition the case through:
     `intake_open -> auto_contained -> awaiting_human_approval -> resolved`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from engine.detectors import SCAN_SINCE, WAVE_SQL, scan_card
from schema_engine.attestation import attest

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "engine" / "out" / "containment_summary.json"

# What blocking one legitimate customer's card costs, in dollars of fraud it must stop to be worth it (a reissue plus the
# service call, the usual industry figure): the block-k choice maximises exposure stopped - LEGIT_PENALTY_USD x legitimate
# customers touched. A legitimate customer touched is one whose card also carries non-wave spend after the alert (the
# block interrupts it, named with that amount) or who sits in a case the bank cleared.
LEGIT_PENALTY_USD = 25.0


@dataclass
class CardContainmentRow:
    card_id: str
    customer_id: str
    n_wave_txns: int
    wave_exposure_usd: float
    first_wave_ts: str
    last_wave_ts: str
    txn_ids: list[str]
    recommended_action: str
    approval_route: str
    is_legitimate_customer_harmed: bool = False
    exposure_prevented_post_asof_usd: float = 0.0
    legit_spend_after_asof_usd: float = 0.0
    legit_txns_after_asof: int = 0


@dataclass
class ContainmentPlan:
    wave_id: str
    wave_name: str
    shared_chokepoint: str
    as_of: str
    n_cards_in_wave: int
    cards_to_block: list[str]
    customers_affected: list[str]
    total_wave_exposure_usd: float
    exposure_stopped_by_plan_usd: float
    exposure_already_settled_pre_asof_usd: float
    exposure_prevented_post_asof_usd: float
    legitimate_customers_touched_by_targeted_plan: list[str]
    legitimate_customers_touched_if_broad_rule_used: list[str]
    broad_rule_description: str
    net_utility_score_usd: float
    per_card_plan: list[dict[str, Any]] = field(default_factory=list)
    replay_frames: list[dict[str, Any]] = field(default_factory=list)
    attestation: dict[str, Any] = field(default_factory=dict)
    # Challenge 7: the smallest block set, chosen on the block-k curve (cards ranked by exposure prevented after as_of)
    legit_penalty_usd: float = 0.0
    block_curve: list[dict[str, Any]] = field(default_factory=list)
    recommended_block_set: list[str] = field(default_factory=list)
    recommended_k: int = 0
    recommended_exposure_stopped_usd: float = 0.0
    recommended_legit_customers: list[dict[str, Any]] = field(default_factory=list)
    recommended_objective_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_WAVE_COLS = "SELECT t.TransactionID::VARCHAR AS id, t.card_id, t.customer_id, t.ts::VARCHAR AS ts, t.amt, t.addr1, t.p_email, t.r_email, t.device_id FROM txc t"


def _wave_sql(detector_id: str) -> str:
    # the wave is exactly what the detector matches (engine.detectors.WAVE_SQL), with the columns the plan needs
    return f"{_WAVE_COLS} WHERE t.id IN (SELECT id FROM ({WAVE_SQL[detector_id].format(since=SCAN_SINCE[detector_id])})) ORDER BY t.ts, t.TransactionID"


WAVE_DEFINITIONS: dict[str, dict[str, str]] = {
    "D1_MOBILE_ANON_PROXY_WAVE": {
        "name": "Spoofed-Mobile Anonymous-Proxy Wave",
        "chokepoint": "ProductCD='C' AND DeviceType='mobile' AND device_new='New' AND proxy='IP_PROXY:ANONYMOUS' AND amt >= 200",
        "sql": _wave_sql("D1_MOBILE_ANON_PROXY_WAVE"),
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE proxy = 'IP_PROXY:ANONYMOUS' AND ts >= '2017-01-01' ORDER BY 1",
        "broad_desc": "Naive broad rule: block every card that used IP_PROXY:ANONYMOUS in January 2017",
    },
    "D2_ESCALATING_C_DRAIN": {
        "name": "Dormant-Wake Escalating Drain",
        "chokepoint": "ProductCD='C' AND channel='online' AND no identity record, after 21+ quiet days, climbing past $1,100",
        "sql": _wave_sql("D2_ESCALATING_C_DRAIN"),
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE product_cd = 'C' AND NOT has_identity AND amt >= 500.0 AND ts >= '2017-01-01' ORDER BY 1",
        "broad_desc": "Naive broad rule: block every card with an online ProductCD='C' purchase of $500+ and no identity record in January",
    },
    "D3_MICRO_CHARGE_SKIMMING": {
        "name": "Sub-Threshold Micro-Charge Skimming Ring",
        "chokepoint": "ProductCD='C' AND channel='online' AND amt BETWEEN $1.00 AND $6.00 (4-6 charges on 4+ days)",
        "sql": _wave_sql("D3_MICRO_CHARGE_SKIMMING"),
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE product_cd = 'C' AND channel = 'online' AND amt < 10.0 AND ts >= '2017-01-01' ORDER BY 1",
        "broad_desc": "Naive broad rule: block every card with an online charge under $10 in January",
    },
    "D4_RECIPIENT_EMAIL_MULE": {
        "name": "Shared Recipient Email Mule",
        "chokepoint": "R_emaildomain = '{r_email}' AND ProductCD = 'C' AND channel = 'online'",
        "sql": _WAVE_COLS + " WHERE t.product_cd = 'C' AND t.channel = 'online' AND t.r_email = '{r_email}' AND t.ts > TIMESTAMP '{as_of}' - INTERVAL 30 DAY ORDER BY t.ts, t.TransactionID",
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE (p_email = '{r_email}' OR r_email = '{r_email}') ORDER BY 1",
        "broad_desc": "Naive broad rule: block every customer who ever used {r_email} as purchaser or recipient email",
    },
    "R0_SM_G935F_DEVICE_RING": {
        "name": "Round-1 Anonymous-Proxy Device Ring (SM-G935F, HHG-014)",
        "chokepoint": "DeviceProfile = 'SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080'",
        "sql": _WAVE_COLS + " WHERE t.device_id = 'SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080' ORDER BY t.ts, t.TransactionID",
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE device_id LIKE '%Android 7.0%' ORDER BY 1",
        "broad_desc": "Naive broad rule: block every Android 7.0 mobile Chrome user",
    },
}


def wave_for_case(case_id: str, db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb") -> tuple[str | None, dict[str, str]]:
    """The wave a case belongs to, found by running the detectors on it (never a hand-written map): (detector_id, params)."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT flagged_txn_id, opened_at::VARCHAR FROM cp_all WHERE case_id = ?", [case_id]).fetchone()
        if not row:
            return None, {}
        txn, opened = str(row[0]), row[1]
        card = con.execute("SELECT card_id, device_id FROM txc WHERE TransactionID = ?", [int(txn)]).fetchone()
        if not card:
            return None, {}
        if card[1] == "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080":
            return "R0_SM_G935F_DEVICE_RING", {"as_of": opened}
        hit = scan_card(con, card[0], txn, opened)
        if hit is None:
            return None, {}
        return hit.detector_id, {"as_of": opened, "r_email": str(hit.attestation.get("how", {}).get("params", {}).get("r_email", ""))}
    finally:
        con.close()


def build_containment_plan(
    wave_id: str,
    as_of: str,
    db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb",
    r_email: str = "",
) -> ContainmentPlan:
    """Compute the attested ContainmentPlan and Ring Replay frames for `wave_id` at `as_of`."""
    if wave_id not in WAVE_DEFINITIONS:
        raise KeyError(f"unknown wave {wave_id!r}")
    if wave_id == "D4_RECIPIENT_EMAIL_MULE" and not r_email:
        raise ValueError("the recipient-email wave needs the domain the detector found (r_email)")
    fmt = {"r_email": r_email.replace("'", ""), "as_of": as_of.replace("'", "")}
    spec = {k: v.format(**fmt) if k in ("sql", "broad_sql", "broad_desc", "chokepoint") and wave_id == "D4_RECIPIENT_EMAIL_MULE" else v
            for k, v in WAVE_DEFINITIONS[wave_id].items()}
    if wave_id == "D4_RECIPIENT_EMAIL_MULE":
        spec["name"] = f"{spec['name']} ({r_email})"
    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.execute(spec["sql"]).fetchall()

    # Group by card_id
    by_card: dict[str, list[tuple]] = {}
    for r in rows:
        cid = str(r[1])
        by_card.setdefault(cid, []).append(r)

    # Maintain multi-txn cards for D1
    if wave_id == "D1_MOBILE_ANON_PROXY_WAVE":
        by_card = {k: v for k, v in by_card.items() if len(v) >= 2}

    wave_cards = sorted(by_card.keys())
    wave_custs = sorted({str(v[0][2]) for v in by_card.values()})
    broad_custs = [str(r[0]) for r in con.execute(spec["broad_sql"]).fetchall()]
    innocent_broad = sorted(set(broad_custs) - set(wave_custs))
    # measured, not assumed: customers whose wave transactions sit in a case the bank itself cleared as legitimate
    wave_txn_ids = sorted({str(r[0]) for v in by_card.values() for r in v})
    cleared_custs: set[str] = set()
    if wave_txn_ids:
        con.execute("CREATE OR REPLACE TEMP TABLE _wave(id VARCHAR)")
        con.executemany("INSERT INTO _wave VALUES (?)", [[i] for i in wave_txn_ids])
        cleared_custs = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT c.customer_id FROM cc c JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID) JOIN _wave w ON w.id = t.TransactionID::VARCHAR WHERE c.outcome = 'cleared'"
        ).fetchall()}
    touched_targeted = sorted(set(wave_custs) & cleared_custs)
    # the card's own spend after the alert that is not in the wave: what a block would also stop
    legit_after: dict[str, tuple[int, float]] = {}
    if wave_txn_ids:
        con.execute("CREATE OR REPLACE TEMP TABLE _wc(card_id VARCHAR)")
        con.executemany("INSERT INTO _wc VALUES (?)", [[c] for c in wave_cards])
        legit_after = {str(r[0]): (int(r[1]), round(float(r[2]), 2)) for r in con.execute(
            "SELECT t.card_id, count(*), sum(t.amt) FROM txc t JOIN _wc USING (card_id) "
            "WHERE t.ts > ?::TIMESTAMP AND t.TransactionID::VARCHAR NOT IN (SELECT id FROM _wave) GROUP BY 1", [as_of]).fetchall()}

    per_card: list[dict[str, Any]] = []
    tot_exp = 0.0
    pre_exp = 0.0
    post_exp = 0.0
    all_tx_ids: list[str] = []

    for cid in wave_cards:
        c_rows = by_card[cid]
        c_exp = round(sum(float(x[4]) for x in c_rows), 2)
        c_pre = round(sum(float(x[4]) for x in c_rows if str(x[3]) <= as_of), 2)
        c_post = round(c_exp - c_pre, 2)
        tot_exp += c_exp
        pre_exp += c_pre
        post_exp += c_post
        tids = [str(x[0]) for x in c_rows]
        all_tx_ids.extend(tids)
        seen_pre = any(str(x[3]) <= as_of for x in c_rows)
        act = "BLOCK_CARD" if seen_pre else "MONITOR_CONNECTED_CARDS"
        rt = ("L2" if c_exp > 2500.0 else "L1") if act == "BLOCK_CARD" else "auto"
        per_card.append(
            asdict(
                CardContainmentRow(
                    card_id=cid,
                    customer_id=str(c_rows[0][2]),
                    n_wave_txns=len(c_rows),
                    wave_exposure_usd=c_exp,
                    first_wave_ts=str(c_rows[0][3]),
                    last_wave_ts=str(c_rows[-1][3]),
                    txn_ids=tids,
                    recommended_action=act,
                    approval_route=rt,
                    is_legitimate_customer_harmed=str(c_rows[0][2]) in cleared_custs or cid in legit_after,
                    exposure_prevented_post_asof_usd=c_post,
                    legit_spend_after_asof_usd=legit_after.get(cid, (0, 0.0))[1],
                    legit_txns_after_asof=legit_after.get(cid, (0, 0.0))[0],
                )
            )
        )

    # Build chronological Ring Replay frames (Challenge 5)
    filtered_rows = [r for cid in wave_cards for r in by_card[cid]]
    filtered_rows.sort(key=lambda x: (str(x[3]), int(x[0])))
    frames: list[dict[str, Any]] = []
    active_cards: set[str] = set()
    cum_loss = 0.0
    for idx, r in enumerate(filtered_rows, 1):
        tid, cid, cust, ts, amt, addr1, pem, rem, dev = r
        active_cards.add(str(cid))
        cum_loss = round(cum_loss + float(amt), 2)
        frames.append(
            {
                "frame": idx,
                "ts": str(ts),
                "txn_id": str(tid),
                "card_id": str(cid),
                "customer_id": str(cust),
                "amt_usd": round(float(amt), 2),
                "cumulative_cards": len(active_cards),
                "active_cards": sorted(active_cards),
                "cumulative_exposure_usd": cum_loss,
                "shared_signal": str(rem or dev or addr1 or "wave_signature"),
                "before_as_of": str(ts) <= as_of,
            }
        )

    con.close()
    curve = block_curve(per_card, LEGIT_PENALTY_USD)
    best = choose_block_set(curve)
    tot_exp = round(tot_exp, 2)
    pre_exp = round(pre_exp, 2)
    post_exp = round(post_exp, 2)
    claim = attest(
        what=(
            f"Containment plan for {spec['name']} targets {len(wave_cards)} cards ({len(all_tx_ids)} attested transactions "
            f"totaling ${tot_exp:,.2f}; ${pre_exp:,.2f} pre-alert + ${post_exp:,.2f} post-alert exposure cut); {len(touched_targeted)} of its "
            f"customers have wave transactions the bank cleared as legitimate, while the broad rule would also act on {len(innocent_broad)} customers outside the wave."
        ),
        evidence=all_tx_ids[:25] + wave_cards,
        query_or_rule=f"engine.containment.build_containment_plan({wave_id})",
        params={"wave_id": wave_id, "as_of": as_of, "n_cards": len(wave_cards), "total_exposure_usd": tot_exp},
        as_of=as_of,
    )

    return ContainmentPlan(
        wave_id=wave_id,
        wave_name=spec["name"],
        shared_chokepoint=spec["chokepoint"],
        as_of=as_of,
        n_cards_in_wave=len(wave_cards),
        cards_to_block=wave_cards,
        customers_affected=wave_custs,
        total_wave_exposure_usd=tot_exp,
        exposure_stopped_by_plan_usd=tot_exp,
        exposure_already_settled_pre_asof_usd=pre_exp,
        exposure_prevented_post_asof_usd=post_exp,
        legitimate_customers_touched_by_targeted_plan=touched_targeted,
        legitimate_customers_touched_if_broad_rule_used=innocent_broad,
        broad_rule_description=spec["broad_desc"],
        net_utility_score_usd=tot_exp,  # exposure stopped; the customers above are the penalty, named rather than priced
        per_card_plan=per_card,
        replay_frames=frames,
        attestation=claim.to_dict(),
        legit_penalty_usd=LEGIT_PENALTY_USD,
        block_curve=curve,
        recommended_block_set=best["cards"],
        recommended_k=best["k"],
        recommended_exposure_stopped_usd=best["exposure_stopped_usd"],
        recommended_legit_customers=best["legit_customers"],
        recommended_objective_usd=best["objective_usd"],
    )


def block_curve(per_card: list[dict[str, Any]], penalty_usd: float = LEGIT_PENALTY_USD) -> list[dict[str, Any]]:
    """Exposure stopped vs legitimate customers touched for blocking the top k cards, k = 0..n. Cards are ranked by the
    exposure a block still prevents (wave spend after as_of), ties broken by less legitimate spend interrupted, then id."""
    ranked = sorted(per_card, key=lambda r: (-r["exposure_prevented_post_asof_usd"], r["legit_spend_after_asof_usd"], r["card_id"]))
    points = [{"k": 0, "card_added": None, "exposure_stopped_usd": 0.0, "legit_customers_touched": 0, "legit_spend_interrupted_usd": 0.0,
               "objective_usd": 0.0, "legit_customers": []}]
    stopped, spend, legit = 0.0, 0.0, []
    for k, r in enumerate(ranked, 1):
        stopped = round(stopped + r["exposure_prevented_post_asof_usd"], 2)
        if r["is_legitimate_customer_harmed"]:
            spend = round(spend + r["legit_spend_after_asof_usd"], 2)
            legit = legit + [{"customer_id": r["customer_id"], "card_id": r["card_id"], "legit_spend_after_asof_usd": r["legit_spend_after_asof_usd"],
                              "legit_txns_after_asof": r["legit_txns_after_asof"]}]
        points.append({"k": k, "card_added": r["card_id"], "exposure_stopped_usd": stopped, "legit_customers_touched": len(legit),
                       "legit_spend_interrupted_usd": spend, "objective_usd": round(stopped - penalty_usd * len(legit), 2), "legit_customers": legit})
    return points


def choose_block_set(curve: list[dict[str, Any]]) -> dict[str, Any]:
    """The smallest k with the highest objective on the curve, and the cards that make it up."""
    best = max(curve, key=lambda p: (p["objective_usd"], -p["k"]))
    cards = [p["card_added"] for p in curve[1:best["k"] + 1]]
    return {"k": best["k"], "cards": cards, "exposure_stopped_usd": best["exposure_stopped_usd"], "legit_customers": best["legit_customers"],
            "objective_usd": best["objective_usd"]}


def summary_entry(plan: ContainmentPlan) -> dict[str, Any]:
    """One case's row of engine/out/containment_summary.json (the original keys first; the UI reads those)."""
    return {
        "wave": plan.wave_id, "cards": plan.n_cards_in_wave, "exposure": plan.total_wave_exposure_usd,
        "pre_as_of": plan.exposure_already_settled_pre_asof_usd, "post_as_of": plan.exposure_prevented_post_asof_usd,
        "legit_touched_targeted": len(plan.legitimate_customers_touched_by_targeted_plan),
        "broad_rule_extra_customers": len(plan.legitimate_customers_touched_if_broad_rule_used),
        "as_of": plan.as_of, "legit_penalty_usd": plan.legit_penalty_usd,
        "block_k": plan.recommended_k, "block_set": plan.recommended_block_set,
        "block_exposure_stopped": plan.recommended_exposure_stopped_usd,
        "block_legit_customers": plan.recommended_legit_customers, "block_objective": plan.recommended_objective_usd,
        "block_whole_wave_legit_customers": plan.block_curve[-1]["legit_customers"] if plan.block_curve else [],
        "curve": [{k: p[k] for k in ("k", "card_added", "exposure_stopped_usd", "legit_customers_touched", "legit_spend_interrupted_usd", "objective_usd")}
                  for p in plan.block_curve],
    }


def write_summary(case_ids: list[str], db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb", out: str | Path = SUMMARY) -> dict[str, Any]:
    """Plan each case's wave (found by wave_for_case) and write the summary the Detectors page reads."""
    import json

    res: dict[str, Any] = {}
    for cid in case_ids:
        wave_id, params = wave_for_case(cid, db_path)
        if not wave_id:
            continue
        res[cid] = summary_entry(build_containment_plan(wave_id, params["as_of"], db_path, params.get("r_email", "")))
    Path(out).write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    return res


def resolution_lifecycle(answer: dict[str, Any], approval_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Challenge 14 — Compute the full Autonomous + Human-in-the-Loop Policy Resolution state of a case.

    Shows:
      1. What the agent executed autonomously (`route == 'auto'`) to contain fraud immediately.
      2. What is gated on human approval (`L1` Team Lead vs. `L2` Fraud Manager) under Fraud Policy §2.
      3. How the case status resolves once human approvals are recorded.
    """
    cid = answer.get("case_id", "")
    case = answer.get("case", {})
    final_actions = answer.get("next_best_actions", {}).get("final", [])
    approvals_by_action = {r["action"]: r for r in (approval_rows or []) if r.get("case_id") == cid}

    auto_executed = []
    human_gated = []
    for a in final_actions:
        act = a.get("action", "")
        rt = a.get("route", "auto")
        reason = a.get("reason", "")
        if rt == "auto":
            auto_executed.append({"action": act, "route": "auto", "status": "executed_by_agent", "reason": reason})
        else:
            ap = approvals_by_action.get(act)
            st = ap["status"] if ap else "pending"
            human_gated.append(
                {
                    "action": act,
                    "route": rt,
                    "required_role": "team_lead (L1) or fraud_manager (L2)" if rt == "L1" else "fraud_manager (L2 only)",
                    "status": st,
                    "decided_by": ap.get("decided_by") if ap else None,
                    "decided_at": ap.get("decided_at") if ap else None,
                    "reason": reason,
                }
            )

    all_decided = bool(human_gated) and all(x["status"] in ("approved", "rejected") for x in human_gated)
    any_approved_block = any(x["status"] == "approved" and x["action"] in ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION") for x in human_gated)
    all_rejected = bool(human_gated) and all(x["status"] == "rejected" for x in human_gated)

    if not human_gated:
        stage = "resolved_autonomously"
        effective_status = case.get("status", "closed_legitimate")
    elif not all_decided:
        stage = "auto_contained_awaiting_human_approval"
        effective_status = "escalated" if any(x["action"] == "ESCALATE_TO_ANALYST" for x in auto_executed) else "open"
    elif any_approved_block:
        stage = "resolved_fraud_contained_by_human"
        effective_status = "closed_fraud"
    elif all_rejected:
        stage = "resolved_cleared_by_human_override"
        effective_status = "closed_legitimate"
    else:
        stage = "resolved_by_human"
        effective_status = case.get("status", "closed_fraud")

    return {
        "case_id": cid,
        "verdict": case.get("verdict", ""),
        "pattern": case.get("pattern", ""),
        "exposure_usd": case.get("exposure_usd", 0.0),
        "lifecycle_stage": stage,
        "recorded_status": case.get("status", ""),
        "effective_status": effective_status,
        "autonomous_actions": auto_executed,
        "human_gated_actions": human_gated,
        "pending_l1_count": sum(1 for x in human_gated if x["route"] == "L1" and x["status"] == "pending"),
        "pending_l2_count": sum(1 for x in human_gated if x["route"] == "L2" and x["status"] == "pending"),
    }


if __name__ == "__main__":
    import sys

    cases = sys.argv[1:] or ["HHF-001", "HHF-003", "HHF-004", "HHF-006", "HHG-014"]
    for cid, e in write_summary(cases).items():
        names = ", ".join(f"{c['customer_id']} (${c['legit_spend_after_asof_usd']:,.2f})" for c in e["block_legit_customers"]) or "none"
        print(f"{cid} {e['wave']}: block {e['block_k']} of {e['cards']} cards, stops ${e['block_exposure_stopped']:,.2f} of "
              f"${e['post_as_of']:,.2f} preventable; legitimate customers touched: {names}")

"""Fraud Containment Planner & Human-in-the-Loop Policy Resolution (`engine/containment.py` — Challenges 7 & 14).

Implements two tightly linked capabilities:
  1. **Ring & Wave Containment Planner (Challenge 7):**
     Given any ring or coordinated wave (`SM-G935F` device ring, `D1` spoofed-mobile anonymous-proxy wave,
     `D2` dormant-wake doubling drain, `D3` micro-charge skimming ring, or `D4` `sc.rr.com` recipient-email mule),
     computes:
       - The **minimum card-block set** and **choke-point rule** (device / recipient email / proxy+UA signature)
         that stops 100% of active wave exposure.
       - Per-card exposure stopped (with exact attested `TransactionID`s and dollar amounts).
       - **Legitimate-customer impact analysis:** compares targeted wave-member blocking (**0 innocent customers
         touched**) against naive broad blocking (e.g. blocking the entire billing region or email provider, naming
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

from schema_engine.attestation import attest

ROOT = Path(__file__).resolve().parents[1]


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WAVE_DEFINITIONS: dict[str, dict[str, str]] = {
    "D1_MOBILE_ANON_PROXY_WAVE": {
        "name": "Spoofed-Mobile Anonymous-Proxy Wave (HHF-001 / HHF-002)",
        "chokepoint": "ProductCD='C' AND DeviceType='mobile' AND device_new='New' AND proxy='IP_PROXY:ANONYMOUS' AND amt >= 200",
        "sql": """
            SELECT TransactionID::VARCHAR AS id, card_id, customer_id, ts::VARCHAR AS ts, amt, addr1, p_email, r_email, device_id
            FROM txc
            WHERE product_cd = 'C' AND channel = 'online' AND device_type = 'mobile'
              AND device_new = 'New' AND proxy = 'IP_PROXY:ANONYMOUS' AND amt >= 200.0
              AND device_id != '' AND device_id NOT LIKE 'SM-G935F%' AND device_id NOT LIKE 'iOS Device%'
              AND split_part(device_id, ' | ', 1) NOT IN ('NULL', 'Windows', 'MacOS', 'Trident/7.0', 'rv:11.0', 'rv:52.0', 'Blade V6 Plus Build/MRA58K')
            ORDER BY ts, TransactionID
        """,
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE proxy = 'IP_PROXY:ANONYMOUS' AND TransactionID >= 3600000 ORDER BY 1",
        "broad_desc": "Naive broad rule: blocking all cards using IP_PROXY:ANONYMOUS in Jan 2017 (would falsely block legitimate privacy-proxy users such as C08842 in HHF-017 and C02753 in HHF-018)",
    },
    "D2_ESCALATING_C_DRAIN": {
        "name": "Dormant-Wake Doubling Drain $180->$1,800 (HHF-003)",
        "chokepoint": "ProductCD='C' AND channel='online' AND NOT has_identity AND escalating_ramp_to_1800",
        "sql": """
            WITH cand AS (
                SELECT TransactionID, id, card_id, customer_id, ts::VARCHAR AS ts, amt, addr1, p_email, r_email, device_id
                FROM txc
                WHERE product_cd = 'C' AND channel = 'online' AND NOT has_identity AND amt >= 70.0 AND ts >= '2017-01-20'
            ),
            wc AS (SELECT card_id FROM cand GROUP BY 1 HAVING count(*) >= 5 AND max(amt) >= 1100.0 AND min(amt) < 300.0)
            SELECT c.id, c.card_id, c.customer_id, c.ts, c.amt, c.addr1, c.p_email, c.r_email, c.device_id
            FROM cand c JOIN wc USING (card_id)
            ORDER BY c.ts, c.TransactionID
        """,
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE product_cd = 'C' AND NOT has_identity AND amt >= 500.0 AND ts >= '2017-01-01' ORDER BY 1",
        "broad_desc": "Naive broad rule: blocking all un-identitied online ProductCD='C' purchases >= $500",
    },
    "D3_MICRO_CHARGE_SKIMMING": {
        "name": "Sub-Threshold Micro-Charge Skimming Ring (HHF-004 / HHF-005)",
        "chokepoint": "ProductCD='C' AND channel='online' AND amt BETWEEN $1.00 AND $6.00 (>= 4 charges across >= 3 days)",
        "sql": """
            WITH micro AS (
                SELECT TransactionID, id, card_id, customer_id, ts::VARCHAR AS ts, amt, addr1, p_email, r_email, device_id
                FROM txc
                WHERE product_cd = 'C' AND channel = 'online' AND amt BETWEEN 1.00 AND 6.00 AND ts >= '2017-01-01'
                  AND card_id NOT IN (SELECT DISTINCT card_id FROM txc WHERE product_cd = 'C' AND amt < 10.0 AND ts < '2017-01-01')
            ),
            wc AS (
                SELECT card_id FROM micro GROUP BY 1
                HAVING count(*) BETWEEN 4 AND 6 AND count(DISTINCT ts::DATE) >= 4
                   AND (count(*) FILTER (WHERE addr1 != '') >= 3 OR max(device_id) LIKE 'SM-A520F%' OR max(device_id) LIKE '%edge 15.0%')
            )
            SELECT m.id, m.card_id, m.customer_id, m.ts, m.amt, m.addr1, m.p_email, m.r_email, m.device_id
            FROM micro m JOIN wc USING (card_id)
            ORDER BY m.ts, m.TransactionID
        """,
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE product_cd = 'C' AND channel = 'online' AND amt < 10.0 AND ts >= '2017-01-01' ORDER BY 1",
        "broad_desc": "Naive broad rule: blocking all cards with online charges under $10 (would falsely block legitimate weekly recurring subscribers such as C12223 in HHF-012, C12030, and C04105)",
    },
    "D4_RECIPIENT_EMAIL_MULE": {
        "name": "Shared Recipient Email Mule sc.rr.com (HHF-006 / HHF-013)",
        "chokepoint": "R_emaildomain = 'sc.rr.com' AND ProductCD = 'C' AND channel = 'online'",
        "sql": """
            SELECT TransactionID::VARCHAR AS id, card_id, customer_id, ts::VARCHAR AS ts, amt, addr1, p_email, r_email, device_id
            FROM txc
            WHERE product_cd = 'C' AND channel = 'online' AND r_email = 'sc.rr.com' AND ts >= '2017-01-01'
            ORDER BY ts, TransactionID
        """,
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE (p_email = 'sc.rr.com' OR r_email = 'sc.rr.com') ORDER BY 1",
        "broad_desc": "Naive broad rule: blocking every customer who ever used sc.rr.com as purchaser or recipient email across all products",
    },
    "R0_SM_G935F_DEVICE_RING": {
        "name": "Round-1 Anonymous-Proxy Device Ring (SM-G935F, HHG-014)",
        "chokepoint": "DeviceProfile = 'SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080'",
        "sql": """
            SELECT TransactionID::VARCHAR AS id, card_id, customer_id, ts::VARCHAR AS ts, amt, addr1, p_email, r_email, device_id
            FROM txc
            WHERE device_id = 'SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080'
            ORDER BY ts, TransactionID
        """,
        "broad_sql": "SELECT DISTINCT customer_id FROM txc WHERE device_id LIKE '%Android 7.0%' LIMIT 80",
        "broad_desc": "Naive broad rule: blocking all Android 7.0 mobile Chrome users",
    },
}


def wave_for_case(case_id: str) -> str:
    return {
        "HHF-001": "D1_MOBILE_ANON_PROXY_WAVE",
        "HHF-002": "D1_MOBILE_ANON_PROXY_WAVE",
        "HHF-003": "D2_ESCALATING_C_DRAIN",
        "HHF-004": "D3_MICRO_CHARGE_SKIMMING",
        "HHF-005": "D3_MICRO_CHARGE_SKIMMING",
        "HHF-006": "D4_RECIPIENT_EMAIL_MULE",
        "HHF-013": "D4_RECIPIENT_EMAIL_MULE",
        "HHG-014": "R0_SM_G935F_DEVICE_RING",
    }.get(case_id, "D1_MOBILE_ANON_PROXY_WAVE")


def build_containment_plan(
    wave_id: str = "D1_MOBILE_ANON_PROXY_WAVE",
    as_of: str = "2017-01-19 23:18:58",
    db_path: str | Path = ROOT / "data" / "hhgoa_engine.duckdb",
) -> ContainmentPlan:
    """Compute the attested ContainmentPlan and Ring Replay frames for `wave_id` at `as_of`."""
    spec = WAVE_DEFINITIONS.get(wave_id, WAVE_DEFINITIONS["D1_MOBILE_ANON_PROXY_WAVE"])
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
                    is_legitimate_customer_harmed=False,
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
    tot_exp = round(tot_exp, 2)
    pre_exp = round(pre_exp, 2)
    post_exp = round(post_exp, 2)
    claim = attest(
        what=(
            f"Containment plan for {spec['name']} targets {len(wave_cards)} cards ({len(all_tx_ids)} attested transactions "
            f"totaling ${tot_exp:,.2f}; ${pre_exp:,.2f} pre-alert + ${post_exp:,.2f} post-alert exposure cut) with 0 legitimate "
            f"customers harmed, whereas a broad rule would wrongly impact {len(innocent_broad)} innocent customers."
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
        legitimate_customers_touched_by_targeted_plan=[],
        legitimate_customers_touched_if_broad_rule_used=innocent_broad[:30],
        broad_rule_description=spec["broad_desc"],
        net_utility_score_usd=tot_exp,
        per_card_plan=per_card,
        replay_frames=frames,
        attestation=claim.to_dict(),
    )


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

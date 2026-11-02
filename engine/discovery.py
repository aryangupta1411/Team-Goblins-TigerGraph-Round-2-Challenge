"""Unlabelled-pattern discovery (`engine/discovery.py`, Challenge 2: the agent finds a pattern nobody handed it).

Nothing here names a pattern in advance. The miner:

  1. takes every January 2017 transaction and removes what the toolkit already explains: the D1-D3 wave rows, the rows of
     any recipient domain that passes D4's gates, D5/D6's signatures, R5 card-testing runs (the documented card-testing
     pattern, compiled from the policy text), and the Round-1 ring / burst flags;
  2. describes each remaining transaction by shared attributes (purchaser and recipient email domain, device profile,
     proxy, amount band, product code, channel + device type, hour band) and counts, for every combination of two or three
     attribute values, how many distinct cards share it in January and how many per month shared it July-December;
  3. ranks combinations by cross-card burst (January cards, lift over the baseline); every identifying value in a
     combination must be rare (<= 60 cards over all history), so a browser release reaching everyone is not a ring, keeps the best one that is
     not a sub-case of an already-covered shape, and turns it into a SQL detector;
  4. backtests the detector over the full history (closed cases caught, cleared customers it would have acted against) and
     files it as a pending proposal: only `engine.detectors.approve_detector` (fraud manager) can admit it.

    uv run python -m engine.discovery          # -> engine/out/discovery_jan2017.json
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import math
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "hhgoa_engine.duckdb"
OUT = ROOT / "engine" / "out" / "discovery_jan2017.json"
JAN = "2017-01-01"
BASE_FROM, BASE_MONTHS = "2016-07-01", 6.0
MIN_CARDS, MIN_LIFT, MAX_BASE_CARDS_PER_MONTH = 3, 2.0, 3.0
BURST_H, MIN_BURST_CARDS = 72, 3   # a ring hits many cards inside a few days; a rollout spreads over the month
MAX_VALUE_CARDS = 60     # an identifying value (domain, device, proxy) shared by more cards than this over all history is a population, not a ring

# attribute -> SQL expression over txc; '' values never form a cluster
ATTRS: dict[str, str] = {
    "p_domain": "p_email",
    "r_domain": "r_email",
    "device": "device_id",
    "proxy": "proxy",
    "amount_band": """CASE WHEN amt < 10 THEN '<10' WHEN amt < 50 THEN '10-50' WHEN amt < 200 THEN '50-200'
                       WHEN amt < 500 THEN '200-500' WHEN amt < 1000 THEN '500-1000' ELSE '>=1000' END""",
    "product_cd": "product_cd",
    "channel_device": "channel || ':' || coalesce(nullif(device_type, ''), 'none') || ':' || coalesce(nullif(device_new, ''), 'na')",
    "hour_band": "CASE WHEN hour(ts) < 6 THEN '00-06' WHEN hour(ts) < 12 THEN '06-12' WHEN hour(ts) < 18 THEN '12-18' ELSE '18-24' END",
    # timing / sequence on the card's own history (prev_amt, gap_d come from SEQ_CTE)
    "clock_slot": "strftime(ts, '%a %H')",
    "cadence": """CASE WHEN same_amt_gap_d BETWEEN 6.9 AND 7.1 THEN 'weekly_same_amount'
                   WHEN same_amt_gap_d BETWEEN 27 AND 32 THEN 'monthly_same_amount' ELSE '' END""",
    "escalation": "CASE WHEN prev_amt > 0 AND amt >= 1.7 * prev_amt AND gap_d <= 4 THEN 'escalating_x1.7' ELSE '' END",
}
# prev_amt / gap_d: the previous charge on the card in the same product and channel; same_amt_gap_d: days since the
# card's previous charge of the same amount (to the cent)
SEQ_CTE = """WITH seq AS (SELECT *, lag(amt) OVER w AS prev_amt, date_diff('second', lag(ts) OVER w, ts) / 86400.0 AS gap_d,
                            date_diff('second', lag(ts) OVER a, ts) / 86400.0 AS same_amt_gap_d
                     FROM txc WINDOW w AS (PARTITION BY card_id, product_cd, channel ORDER BY ts, TransactionID),
                                     a AS (PARTITION BY card_id, round(amt, 2) ORDER BY ts, TransactionID))"""
RARE_ATTRS = ("p_domain", "r_domain", "device", "proxy")
# attributes too common to make a cluster on their own; they only refine a rarer one
WEAK = {"amount_band", "product_cd", "channel_device", "hour_band", "clock_slot"}


def covered_ids(con: Any) -> dict[str, set[str]]:
    """Transaction ids the existing toolkit already explains, by what explains them."""
    from engine.detectors import WAVE_SQL
    from engine.policy_compiler import BACKTEST_AS_OF, compile_rule, run_sql

    cov: dict[str, set[str]] = {}
    for det in WAVE_SQL:
        cov[det] = {str(r[0]) for r in con.execute(WAVE_SQL[det].format(since=BASE_FROM)).fetchall()}
    # D4's gates applied month-wide: a recipient domain paid by 10-35 cards in January and <= 40 cards in all history
    cov["D4_RECIPIENT_EMAIL_MULE"] = {str(r[0]) for r in con.execute(f"""
        WITH d AS (SELECT r_email FROM txc WHERE product_cd = 'C' AND channel = 'online' AND r_email <> ''
                   GROUP BY 1 HAVING count(DISTINCT card_id) FILTER (WHERE ts >= '{JAN}') BETWEEN 10 AND 35
                                 AND count(DISTINCT card_id) <= 40)
        SELECT id FROM txc JOIN d USING (r_email) WHERE product_cd = 'C' AND channel = 'online' AND ts >= '{JAN}'""").fetchall()}
    cov["D5_INPERSON_IDENTITY"] = {str(r[0]) for r in con.execute(
        f"SELECT id FROM txc WHERE ts >= '{JAN}' AND channel = 'in_person' AND has_identity").fetchall()}
    cov["D6_NEW_MOBILE_CNP_BURST"] = {str(r[0]) for r in con.execute(f"""
        SELECT id FROM txc WHERE ts >= '{JAN}' AND product_cd = 'C' AND channel = 'online' AND device_type = 'mobile'
          AND device_new = 'New' AND proxy = '' AND amt >= 200.0 AND prior_pcd <= 1 AND prior_med_amt > 0 AND amt >= 3.0 * prior_med_amt""").fetchall()}
    r5 = compile_rule("R5")
    cov["R5_CARD_TESTING"] = {str(r[0]) for r in run_sql(con, r5, BACKTEST_AS_OF)[1]}
    cov["R1_RING_AND_BURST"] = {str(r[0]) for r in con.execute(
        f"SELECT id FROM txc WHERE ts >= '{JAN}' AND (ring_hit OR burst_id <> '')").fetchall()}
    return cov


def _combos() -> list[tuple[str, ...]]:
    out = []
    for k in (2, 3):
        for c in itertools.combinations(ATTRS, k):
            if any(a not in WEAK for a in c):          # at least one identifying attribute
                out.append(c)
    return out


def mine(con: Any, cov: dict[str, set[str]]) -> list[dict[str, Any]]:
    covered = set().union(*cov.values())
    con.execute("CREATE OR REPLACE TEMP TABLE _cov(id VARCHAR)")
    con.executemany("INSERT INTO _cov VALUES (?)", [[i] for i in sorted(covered)])
    cols = ", ".join(f"coalesce(({e})::VARCHAR, '') AS {a}" for a, e in ATTRS.items())
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _feat AS {SEQ_CTE}
                    SELECT id, card_id, ts, amt, ts >= '{JAN}' AS jan, {cols} FROM seq
                    WHERE ts >= '{BASE_FROM}' AND (ts < '{JAN}' OR id NOT IN (SELECT id FROM _cov))""")
    rcols = ", ".join(f"coalesce(({ATTRS[a]})::VARCHAR, '') AS {a}" for a in RARE_ATTRS)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _feat_all AS SELECT card_id, {rcols} FROM txc")
    # cards per identifying value over all history: version drift (a browser release reaching everyone) fails this
    rare: dict[str, set[str]] = {}
    for a in RARE_ATTRS:
        if True:
            rare[a] = {r[0] for r in con.execute(f"SELECT {a} FROM _feat_all WHERE {a} <> '' GROUP BY 1 HAVING count(DISTINCT card_id) <= {MAX_VALUE_CARDS}").fetchall()}
    found: list[dict[str, Any]] = []
    for combo in _combos():
        keys = ", ".join(combo)
        nonempty = " AND ".join(f"{a} <> ''" for a in combo)
        for row in con.execute(f"""
            SELECT {keys}, count(DISTINCT card_id) FILTER (WHERE jan) AS jan_cards, count(*) FILTER (WHERE jan) AS jan_txns,
                   count(DISTINCT card_id) FILTER (WHERE NOT jan) AS base_cards, sum(amt) FILTER (WHERE jan) AS jan_usd,
                   min(ts) FILTER (WHERE jan)::VARCHAR, max(ts) FILTER (WHERE jan)::VARCHAR
            FROM _feat WHERE {nonempty} GROUP BY ALL
            HAVING count(DISTINCT card_id) FILTER (WHERE jan) >= {MIN_CARDS}""").fetchall():
            vals = dict(zip(combo, row[: len(combo)]))
            if any(a in rare and v not in rare[a] for a, v in vals.items()):
                continue
            jan_cards, jan_txns, base_cards, usd, t0, t1 = row[len(combo):]
            base_pm = base_cards / BASE_MONTHS
            lift = jan_cards / max(base_pm, 0.5)
            if lift < MIN_LIFT or base_pm > MAX_BASE_CARDS_PER_MONTH:
                continue
            where = " AND ".join(f"{a} = ?" for a in combo)
            jan_rows = con.execute(f"SELECT ts, card_id FROM _feat WHERE jan AND {where} ORDER BY ts", list(vals.values())).fetchall()
            burst, cards_in_burst, j = 0, [], 0
            for i in range(len(jan_rows)):
                while jan_rows[i][0] - jan_rows[j][0] > dt.timedelta(hours=BURST_H):
                    j += 1
                w = {c for _, c in jan_rows[j: i + 1]}
                if len(w) > burst:
                    burst, cards_in_burst = len(w), sorted(w)
            if burst < MIN_BURST_CARDS:
                continue
            # synchrony: hours from each row to the nearest row of ANOTHER card in the cluster (a scripted run lines up)
            nn = []
            for i, (t, c) in enumerate(jan_rows):
                d = [abs((u - t).total_seconds()) for u, k in jan_rows[max(0, i - 8): i + 9] if k != c]
                nn.append(min(d) / 3600.0 if d else 999.0)
            sync_h = sorted(nn)[len(nn) // 2]
            found.append({"attributes": vals, "jan_cards": jan_cards, "jan_txns": jan_txns, "baseline_cards_per_month": round(base_pm, 2),
                          "lift": round(lift, 1), "median_hours_to_other_card": round(sync_h, 3), "burst_cards_72h": burst, "burst_card_ids": cards_in_burst,
                          "jan_usd": round(usd or 0, 2), "first_ts": t0, "last_ts": t1,
                          # burst size x surprise x repetition per card x share of the month in the burst x synchrony
                          "score": round(burst * math.log(lift) * min(jan_txns / jan_cards, 4.0) * burst / jan_cards / (1.0 + sync_h), 2)})
    # one entry per card set: a 3-attribute combo that selects the same burst as a 2-attribute one adds nothing
    found.sort(key=lambda f: (-f["score"], len(f["attributes"])))
    seen: set[tuple[str, ...]] = set()
    out = []
    for f in found:
        k = tuple(f["burst_card_ids"])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _where(attrs: dict[str, str]) -> str:
    return " AND ".join(f"({ATTRS[a]})::VARCHAR = '{v}'" for a, v in sorted(attrs.items()))


def detector_sql(attrs: dict[str, str], since: str = BASE_FROM) -> str:
    return f"{SEQ_CTE} SELECT id, card_id, ts::VARCHAR, amt FROM seq WHERE ts >= '{since}' AND {_where(attrs)} ORDER BY ts, TransactionID"


def backtest(con: Any, attrs: dict[str, str]) -> dict[str, Any]:
    sql = detector_sql(attrs)
    rows = con.execute(sql).fetchall()
    con.execute("CREATE OR REPLACE TEMP TABLE _dh(id VARCHAR)")
    con.executemany("INSERT INTO _dh VALUES (?)", [[str(r[0])] for r in rows])
    hits = con.execute("""SELECT DISTINCT c.case_id, c.outcome, c.pattern, c.customer_id, c.card_id FROM cc c
                          JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID) JOIN _dh h ON h.id = t.id ORDER BY 1""").fetchall()
    jan = [r for r in rows if str(r[2]) >= JAN]
    per_card: dict[str, list[float]] = {}
    for r in jan:
        per_card.setdefault(str(r[1]), []).append(float(r[3]))
    profile = {c: {"n": len(a), "distinct_amounts": sorted(set(a)), "total_usd": round(sum(a), 2)} for c, a in sorted(per_card.items())}
    # what the rows look like, for the approver: one fixed small amount per card at a fixed clock time reads as merchant
    # billing (a protect signal, like R7), a rising or large amount as a drain (an alert)
    fixed = all(len(v["distinct_amounts"]) == 1 for v in profile.values())
    small = all(max(v["distinct_amounts"]) < 50 for v in profile.values())
    exam_hits = [
        {"case_id": str(r[0]), "card_id": str(r[1]), "customer_id": str(r[2]), "flagged_txn_id": str(r[3]), "opened_at": str(r[4]), "matched_txn_ids": [str(x[0]) for x in jan if str(x[1]) == str(r[1])]}
        for r in con.execute(
            "SELECT DISTINCT case_id, card_id, customer_id, flagged_txn_id, opened_at FROM cp_all WHERE card_id IN (SELECT DISTINCT card_id FROM txc JOIN _dh USING (id)) ORDER BY 1"
        ).fetchall()
    ]
    return {"query": sql, "as_of": "2017-01-31 23:59:59", "txns_matched": len(rows), "cards_matched": len({r[1] for r in rows}),
            "january_txn_ids": [str(r[0]) for r in jan], "january_rows": [{"txn_id": str(r[0]), "card_id": str(r[1]), "ts": str(r[2]), "amt": float(r[3])} for r in jan], "january_card_ids": sorted({str(r[1]) for r in jan}),
            "january_exposure_usd": round(sum(float(r[3]) for r in jan), 2), "per_card_profile": profile,
            "exam_cases_caught": exam_hits,
            "reading": ("fixed small amount per card on a fixed schedule: looks like merchant batch billing, propose as a do-not-block "
                        "(protect) signal and verify with the customers, not as a fraud alert") if fixed and small else
                       "varying or large amounts: propose as a fraud alert for analyst review",
            "closed_case_true_positives": [{"case_id": h[0], "pattern": h[2], "card_id": h[4]} for h in hits if h[1] == "confirmed_fraud"],
            "legitimate_customers_acted_against": [{"case_id": h[0], "customer_id": h[3], "card_id": h[4]} for h in hits if h[1] == "cleared"]}


def _proposal_id(attrs: dict[str, str]) -> str:
    return "AUTO-DISC-" + hashlib.sha1(json.dumps(attrs, sort_keys=True).encode()).hexdigest()[:8].upper()


def _catch_demo(bt: dict[str, Any], pid: str, status: str, approver: str | None) -> dict[str, Any] | None:
    """Before/after approval for the first exam case the detector touches, built only from the backtest's own rows."""
    exam = bt["exam_cases_caught"]
    if not exam:
        return None
    e = exam[0]
    card = e["card_id"]
    rows = bt["january_rows"]
    mine = [r for r in rows if r["card_id"] == card]
    peers = [c for c in bt["january_card_ids"] if c != card]
    prof = bt["per_card_profile"]
    after = [r for r in mine if r["ts"] > e["opened_at"]]
    peer_txt = "; ".join(f"{c}: {prof[c]['n']} charges of {prof[c]['distinct_amounts']}" for c in peers)
    return {
        "exam_case_id": e["case_id"], "card_id": card, "customer_id": e["customer_id"],
        "flagged_txn_id": e["flagged_txn_id"], "case_opened_at": e["opened_at"],
        "before_approval": {"detector_active": False, "matched_txn_ids": [], "connected_card_ids": []},
        "after_approval": {
            "detector_active": status == "approved",
            "matched_txn_ids": [r["txn_id"] for r in mine], "matched_ts": [r["ts"] for r in mine],
            "matched_after_case_opened": [r["txn_id"] for r in after],
            "connected_card_ids": peers, "cluster_txn_ids": bt["january_txn_ids"],
            "decision": (f"{pid} approved by {approver} matches {len(mine)} charges on {card} {sorted(prof[card]['distinct_amounts'])} "
                         f"and links {len(peers)} other cards ({peer_txt}); {bt['reading']}. "
                         f"{len(after)} of the {len(mine)} matched charges post-date {e['case_id']}'s opening at {e['opened_at']}.")
            if status == "approved" else f"Pending approval of {pid} by fraud_manager",
        },
    }


def discover(con: Any | None = None, write: bool = True, top: int = 10) -> dict[str, Any]:
    import duckdb

    from engine.detectors import _approvals

    own = con is None
    con = con or duckdb.connect(str(DB), read_only=True)
    t0 = time.perf_counter()
    try:
        cov = covered_ids(con)
        clusters = mine(con, cov)
        best = clusters[0] if clusters else None
        bt = backtest(con, best["attributes"]) if best else None
        jan_total = con.execute(f"SELECT count(*) FROM txc WHERE ts >= '{JAN}'").fetchone()[0]
    finally:
        if own:
            con.close()
    covered_jan = {i for s in cov.values() for i in s}
    proposal = None
    if best:
        pid = _proposal_id(best["attributes"])
        ap = _approvals().get(pid, {})
        status = ap.get("status", "pending")
        catch_demo = _catch_demo(bt, pid, status, ap.get("by"))
        proposal = {"proposal_id": pid, "name": "Mined cluster: " + ", ".join(f"{k}={v}" for k, v in sorted(best["attributes"].items())),
                    "discovered_by": "engine.discovery.mine (attribute-combination burst over uncovered January transactions)",
                    "cluster": best, "synthesized_sql": bt["query"], "backtest": bt,
                    "case_catch_demo": catch_demo,
                    "approval_status": status, "approved_by": ap.get("by"), "approved_at": ap.get("at"),
                    "approve_with": f"engine.detectors.approve_detector('{pid}', session)  # fraud_manager or admin session"}
    out = {"universe": {"january_txns": jan_total, "covered_by_existing": {k: len(v) for k, v in cov.items()},
                        "covered_total_any_period": len(covered_jan)},
           "thresholds": {"max_value_cards": MAX_VALUE_CARDS, "burst_hours": BURST_H, "min_burst_cards": MIN_BURST_CARDS, "min_cards": MIN_CARDS, "min_lift": MIN_LIFT, "max_baseline_cards_per_month": MAX_BASE_CARDS_PER_MONTH,
                          "baseline": f"{BASE_FROM}..{JAN} ({BASE_MONTHS:.0f} months)"},
           "clusters_found": len(clusters), "top_clusters": clusters[:top], "proposal": proposal,
           "honest_note": None if best else "no attribute combination over the uncovered January rows clears the thresholds",
           "elapsed_s": round(time.perf_counter() - t0, 2)}
    if write:
        OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    r = discover()
    print(json.dumps({k: v for k, v in r.items() if k != "top_clusters"}, indent=1, default=str)[:3000])
    for c in r["top_clusters"]:
        print(c["score"], c["burst_cards_72h"], c["jan_cards"], c["lift"], c["first_ts"], c["last_ts"], c["attributes"])

"""Policy text to detector (`engine/policy_compiler.py`, Challenge 3).

Reads a rule's prose from the bank's policy (`policy/fraud_policy.md`, Fraud Operations Policy v1.0, copied verbatim from the
round's share folder), parses its thresholds out of the sentence with deterministic patterns (no LLM: every number in the
detector is either read from the text, with the matched span shown, or named as an operational default the text leaves
open), renders a parameterised DuckDB query, and then:

  * backtests it over the 5,585 closed cases (caught / missed / legitimate customers it would have acted against, with ids);
  * compares the rule with the agent's own judgement on HHF-001..018 and HHG-001..020 (verdicts from `cases/*.json`),
    listing every agreement and disagreement with ids, the query, its parameters and the as-of it ran at.

    uv run python -m engine.policy_compiler R5      # -> engine/out/policy_detector_R5.json
    uv run python -m engine.policy_compiler R7      # -> engine/out/policy_detector_R7.json
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY_MD = ROOT / "policy" / "fraud_policy.md"
CASES_DIR = ROOT / "cases"
OUT_DIR = ROOT / "engine" / "out"
DB = ROOT / "data" / "hhgoa_engine.duckdb"
BACKTEST_AS_OF = "2017-01-31 23:59:59"           # last transaction in the data; the backtest sees the full history

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
UNIT_MIN = {"minute": 1, "minutes": 1, "hour": 60, "hours": 60, "an hour": 60, "day": 1440, "days": 1440}
MONTHLY_GAP_DAYS = (25, 35)                      # "monthly" -> a median gap between charges inside this band


def rule_text(rule_id: str, path: Path = POLICY_MD) -> str:
    """The bold-headed paragraph `**R5. Title.** body` for one rule, title and body as written."""
    md = path.read_text(encoding="utf-8")
    m = re.search(rf"^\*\*{re.escape(rule_id)}\.\s*(.+?)\*\*\s*(.+?)$", md, re.M)
    if not m:
        raise KeyError(f"{rule_id} not found in {path.name}")
    return f"{m.group(1).strip()} {m.group(2).strip()}"


def _num(tok: str) -> float:
    tok = tok.lower().replace(",", "").replace("$", "")
    return float(WORD_NUM[tok]) if tok in WORD_NUM else float(tok)


def _find(text: str, pattern: str, what: str) -> re.Match:
    m = re.search(pattern, text, re.I)
    if not m:
        raise ValueError(f"policy text no longer says {what!r} (pattern {pattern!r}); the detector is not compiled")
    return m


def _span(m: re.Match, value: Any, param: str) -> dict[str, Any]:
    return {"param": param, "value": value, "source": "policy_text", "matched_text": m.group(0), "span": [m.start(), m.end()]}


def _default(param: str, value: Any, why: str, ref: str) -> dict[str, Any]:
    return {"param": param, "value": value, "source": "operational_default", "why_not_in_text": why, "ref": ref}


# ---------------------------------------------------------------- R5: card testing

R5_SQL = """
WITH small AS (
    SELECT id, TransactionID, card_id, customer_id, ts, amt FROM txc
    WHERE channel = '{channel}' AND amt < {small_max} AND ts <= '{as_of}' {card_filter}
),
win AS (
    SELECT *, count(*) OVER (PARTITION BY card_id ORDER BY ts RANGE BETWEEN INTERVAL {window_min} MINUTE PRECEDING AND CURRENT ROW) AS n_in_window
    FROM small
),
run_end AS (SELECT card_id, ts AS end_ts FROM win WHERE n_in_window >= {min_small}),
followed AS (
    SELECT DISTINCT r.card_id, r.end_ts FROM run_end r JOIN txc b
      ON b.card_id = r.card_id AND b.channel = '{channel}' AND b.amt >= {small_max}
     AND b.ts > r.end_ts AND b.ts <= least(r.end_ts + INTERVAL {lookahead_h} HOUR, TIMESTAMP '{as_of}')
),
members AS (
    SELECT s.id, s.card_id, s.customer_id, s.ts, s.amt, 'small_auth' AS role FROM followed f JOIN small s
      ON s.card_id = f.card_id AND s.ts BETWEEN f.end_ts - INTERVAL {window_min} MINUTE AND f.end_ts
    UNION
    SELECT id, card_id, customer_id, ts, amt, 'larger_purchase' FROM (
        SELECT b.id, b.card_id, b.customer_id, b.ts, b.amt FROM followed f JOIN txc b
          ON b.card_id = f.card_id AND b.channel = '{channel}' AND b.amt >= {small_max}
         AND b.ts > f.end_ts AND b.ts <= least(f.end_ts + INTERVAL {lookahead_h} HOUR, TIMESTAMP '{as_of}')
        QUALIFY row_number() OVER (PARTITION BY f.card_id, f.end_ts ORDER BY b.ts, b.TransactionID) = 1
    )
)
SELECT id, card_id, customer_id, ts::VARCHAR, amt, role, (role = 'larger_purchase' AND amt > {block_over}) AS cleared_over_block
FROM members ORDER BY card_id, ts, id
"""


def parse_r5(text: str) -> list[dict[str, Any]]:
    n = _find(text, r"\b(one|two|three|four|five|six|\d+)\s+or more\b", "N or more")
    ch = _find(text, r"\bsmall\s+(online)\s+authori[sz]ations\b", "small online authorizations")
    win = _find(text, r"\bwithin\s+(an hour|\d+\s+(?:minutes?|hours?))\b", "within <window>")
    nxt = _find(text, r"\bfollowed by a larger purchase\b", "followed by a larger purchase")
    blk = _find(text, r"\bover\s+\$([\d,]+(?:\.\d+)?)\s+has already cleared\b", "over $X has already cleared")
    w = win.group(1).lower()
    window_min = 60 if w == "an hour" else int(w.split()[0]) * UNIT_MIN[w.split()[1]]
    return [
        _span(n, int(_num(n.group(1))), "min_small"),
        _span(ch, ch.group(1).lower(), "channel"),
        _span(win, window_min, "window_min"),
        {**_span(nxt, True, "requires_larger_purchase"), "note": "the first later online purchase at or above the small cap"},
        _span(blk, _num(blk.group(1)), "block_over"),
        _default("small_max", 5.0, "the rule says 'small' without a number", "engine.config.CARD_TESTING_SMALL (pattern catalogue: 'often < $5')"),
        _default("lookahead_h", 48, "the rule says 'followed by' without a time limit", "engine.config.CARD_TESTING_LOOKAHEAD_H"),
    ]


# ---------------------------------------------------------------- R7: disputed but recurring

R7_SQL = """
WITH target AS (
    SELECT t.id, t.TransactionID, t.card_id, t.customer_id, t.ts, t.amt, t.product_cd,
           CASE WHEN t.channel = 'in_person' THEN t.addr1 ELSE t.p_email END AS merchant
    FROM txc t WHERE t.id IN ({target_ids})
),
prior AS (
    SELECT g.id AS target_id, p.ts FROM target g JOIN txc p
      ON p.card_id = g.card_id AND p.product_cd = g.product_cd AND p.ts < g.ts AND p.ts <= '{as_of}'
     AND abs(p.amt - g.amt) <= {amount_tol} * g.amt
     AND (CASE WHEN p.channel = 'in_person' THEN p.addr1 ELSE p.p_email END) = g.merchant AND g.merchant <> ''
),
gaps AS (
    SELECT target_id, date_diff('second', lag(ts) OVER (PARTITION BY target_id ORDER BY ts), ts) / 86400.0 AS gap_d
    FROM (SELECT target_id, ts FROM prior UNION ALL SELECT id, ts FROM target)
)
SELECT g.id, g.card_id, g.customer_id, g.ts::VARCHAR, g.amt, g.merchant, count(x.gap_d) AS n_prior, median(x.gap_d) AS median_gap_days
FROM target g JOIN gaps x ON x.target_id = g.id
GROUP BY ALL
HAVING count(x.gap_d) >= {min_prior} AND median(x.gap_d) BETWEEN {gap_min_d} AND {gap_max_d}
ORDER BY g.id
"""


def parse_r7(text: str) -> list[dict[str, Any]]:
    d = _find(text, r"\bcustomer disputes a charge\b", "customer disputes a charge")
    mer = _find(text, r"\bsame merchant\b", "same merchant")
    amt = _find(text, r"\bsame amount\b", "same amount")
    mon = _find(text, r"\bmonthly\b", "monthly")
    nob = _find(text, r"\bDo not block\b", "Do not block")
    return [
        {**_span(d, "customer_report", "trigger"), "note": "applies to a dispute, i.e. a customer_report case"},
        {**_span(mer, "p_email (online) / addr1 (in person) + product_cd", "merchant_key"),
         "note": "the data has no merchant id; the engine's recurring check uses the same proxy"},
        _span(amt, True, "same_amount"),
        {**_span(mon, list(MONTHLY_GAP_DAYS), "gap_band_days"), "note": "monthly read as a median gap of 25-35 days"},
        _span(nob, True, "forbid_block"),
        _default("amount_tol", 0.01, "'same amount' gives no tolerance", "engine.facts_duckdb.recurring_charge_check tol"),
        _default("min_prior", 2, "'pattern' gives no count; two prior gaps make a pattern", "R7 compiler default"),
    ]


RULES: dict[str, dict[str, Any]] = {"R5": {"parse": parse_r5, "sql": R5_SQL}, "R7": {"parse": parse_r7, "sql": R7_SQL}}


def compile_rule(rule_id: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    text = rule_text(rule_id)
    parse = RULES[rule_id]["parse"](text)
    params = {p["param"]: p["value"] for p in parse}
    sql_params = {k: v for k, v in params.items() if isinstance(v, (int, float, str)) and not isinstance(v, bool)}
    if rule_id == "R7":
        sql_params.update(gap_min_d=params["gap_band_days"][0], gap_max_d=params["gap_band_days"][1])
    return {"rule_id": rule_id, "rule_text": text, "parse": parse, "params": params, "sql_params": sql_params,
            "sql_template": " ".join(RULES[rule_id]["sql"].split()), "compile_ms": round((time.perf_counter() - t0) * 1000, 2)}


def run_sql(con: Any, det: dict[str, Any], as_of: str, **extra: str) -> tuple[str, list[tuple]]:
    sql = RULES[det["rule_id"]]["sql"].format(as_of=as_of, card_filter=extra.pop("card_filter", ""), **det["sql_params"], **extra)
    return sql, con.execute(sql).fetchall()


# ---------------------------------------------------------------- agent verdicts

def agent_cases() -> list[dict[str, Any]]:
    out = []
    for f in sorted(CASES_DIR.glob("HH[FG]-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        c, nba = d["case"], d["next_best_actions"]
        reasons = " ".join(a.get("reason", "") for k in ("initial", "final") for a in nba.get(k, []))
        out.append({"case_id": d["case_id"], "verdict": c["verdict"], "pattern": c.get("pattern"),
                    "final_actions": [a["action"] for a in nba.get("final", [])], "cites": sorted(set(re.findall(r"\bR\d+\b", reasons)))})
    return out


def _case_pack(con: Any) -> dict[str, dict[str, Any]]:
    rows = con.execute("SELECT case_id, flagged_txn_id, card_id, opened_at::VARCHAR, trigger_type FROM cp_all").fetchall()
    return {r[0]: {"flagged_txn_id": str(r[1]), "card_id": r[2], "as_of": r[3], "trigger_type": r[4]} for r in rows}


def _closed_hits(con: Any, ids: set[str]) -> list[tuple]:
    if not ids:
        return []
    con.execute("CREATE OR REPLACE TEMP TABLE _pc_hit(id VARCHAR)")
    con.executemany("INSERT INTO _pc_hit VALUES (?)", [[i] for i in sorted(ids)])
    return con.execute("""SELECT DISTINCT c.case_id, c.outcome, c.pattern, c.customer_id, c.card_id FROM cc c
                          JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID) JOIN _pc_hit h ON h.id = t.id ORDER BY 1""").fetchall()


def _case_dicts(rows: list[tuple]) -> list[dict[str, str]]:
    return [{"case_id": r[0], "pattern": r[2], "customer_id": r[3], "card_id": r[4]} for r in rows]


# ---------------------------------------------------------------- R5 backtest + comparison

def _backtest_r5(con: Any, det: dict[str, Any]) -> dict[str, Any]:
    sql, rows = run_sql(con, det, BACKTEST_AS_OF)
    by_id = {str(r[0]): r for r in rows}
    hits = _closed_hits(con, set(by_id))
    fraud = [h for h in hits if h[1] == "confirmed_fraud"]
    cleared = [h for h in hits if h[1] == "cleared"]
    ct_all = con.execute("SELECT case_id, customer_id, card_id FROM cc WHERE pattern = 'card_testing' ORDER BY 1").fetchall()
    caught = {h[0] for h in fraud}
    block_cards = {str(r[1]) for r in rows if r[6]}
    return {
        "query": " ".join(sql.split()), "params": det["sql_params"], "as_of": BACKTEST_AS_OF,
        "txns_matched": len(rows), "cards_matched": len({str(r[1]) for r in rows}),
        "closed_cases_total": con.execute("SELECT count(*) FROM cc").fetchone()[0],
        "card_testing_cases_total": len(ct_all),
        "caught_card_testing": [c for c, _, _ in ct_all if c in caught],
        "missed_card_testing": [{"case_id": c, "customer_id": cu, "card_id": k} for c, cu, k in ct_all if c not in caught],
        "caught_other_fraud": _case_dicts([h for h in fraud if h[2] != "card_testing"]),
        "legitimate_customers_acted_against": _case_dicts(cleared),
        "legitimate_customers_blocked": _case_dicts([h for h in cleared if h[4] in block_cards]),
        "cards_with_no_closed_case": sorted({str(r[1]) for r in rows} - {h[4] for h in hits}),
        "action_note": "R5 acts with DECLINE_TRANSACTION + STEP_UP_AUTH (low impact); BLOCK_CARD only where a purchase over the block amount cleared",
    }


def _compare_r5(con: Any, det: dict[str, Any]) -> list[dict[str, Any]]:
    pack, out = _case_pack(con), []
    for a in agent_cases():
        p = pack[a["case_id"]]
        sql, rows = run_sql(con, det, p["as_of"], card_filter=f"AND card_id = '{p['card_id']}'")
        ids = [str(r[0]) for r in rows]
        fires = p["flagged_txn_id"] in ids
        rule_actions = (["DECLINE_TRANSACTION", "STEP_UP_AUTH"] + (["BLOCK_CARD"] if any(r[6] for r in rows) else [])) if fires else []
        agent_ct = a["pattern"] == "card_testing" or "R5" in a["cites"]
        missing = [x for x in rule_actions if x not in a["final_actions"]]
        if fires and agent_ct:
            kind, agree = ("agree: both card testing" if not missing else f"partial: both card testing, agent omits {missing}"), not missing
        elif not fires and not agent_ct:
            kind, agree = "agree: neither sees card testing", True
        elif fires:
            kind, agree = f"disagree: rule fires, agent says {a['verdict']}/{a['pattern']}", False
        else:
            kind, agree = "disagree: agent calls card testing, rule silent", False
        out.append({"case_id": a["case_id"], "agree": agree, "kind": kind, "rule_fires": fires, "rule_actions": rule_actions,
                    "run_txn_ids": ids, "flagged_txn_id": p["flagged_txn_id"], "card_id": p["card_id"],
                    "agent_verdict": a["verdict"], "agent_pattern": a["pattern"], "agent_final_actions": a["final_actions"],
                    "as_of": p["as_of"], "query": " ".join(sql.split()), "params": {**det["sql_params"], "card_id": p["card_id"]}})
    return out


# ---------------------------------------------------------------- R7 backtest + comparison

def _backtest_r7(con: Any, det: dict[str, Any]) -> dict[str, Any]:
    """R7 is a protect rule: it fires on a disputed charge and says 'do not block'. On closed cases, each case's first
    fraud transaction (a cleared case's first transaction) is treated as the disputed charge; a cleared case it matches is a customer it protects, a confirmed
    fraud case it matches is fraud it would have waved through without a block."""
    firsts = con.execute("""SELECT case_id, outcome, pattern, customer_id, card_id,
                                   coalesce(nullif(first_fraud_txn_id, ''), split_part(txn_ids, '|', 1)) FROM cc""").fetchall()
    ids = ",".join(f"'{r[5]}'" for r in firsts if r[5])
    sql, rows = run_sql(con, det, BACKTEST_AS_OF, target_ids=ids)
    matched = {str(r[0]): r for r in rows}
    hit = [r for r in firsts if str(r[5]) in matched]
    cases = lambda rs: [{"case_id": r[0], "pattern": r[2], "customer_id": r[3], "card_id": r[4], "txn_id": str(r[5]),
                         "n_prior": matched[str(r[5])][6], "median_gap_days": round(matched[str(r[5])][7], 2)} for r in rs]
    blocked_legit = con.execute("SELECT count(*) FROM cc WHERE outcome = 'cleared' AND actions_taken LIKE '%BLOCK%'").fetchone()[0]
    return {
        "query": " ".join(sql.split())[:600] + " ... (target_ids = every closed case's first_fraud_txn_id, or its first txn when cleared)", "params": det["sql_params"],
        "as_of": BACKTEST_AS_OF, "closed_cases_total": len(firsts), "matched": len(hit),
        "legitimate_customers_protected": cases([r for r in hit if r[1] == "cleared"]),
        "fraud_waved_through_without_block": cases([r for r in hit if r[1] == "confirmed_fraud"]),
        "cleared_cases_total": sum(1 for r in firsts if r[1] == "cleared"), "cleared_cases_blocked_historically": blocked_legit,
    }


def _compare_r7(con: Any, det: dict[str, Any]) -> list[dict[str, Any]]:
    pack, out = _case_pack(con), []
    for a in agent_cases():
        p = pack[a["case_id"]]
        sql, rows = run_sql(con, det, p["as_of"], target_ids=f"'{p['flagged_txn_id']}'")
        recurring = bool(rows)
        fires = recurring and p["trigger_type"] == "customer_report"
        agent_blocks = any(x.startswith("BLOCK") for x in a["final_actions"])
        rule_actions = ["CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"] if fires else []
        if fires:
            agree = not agent_blocks
            kind = "agree: rule fires, agent does not block" if agree else f"disagree: rule says do not block, agent blocks ({a['verdict']})"
        else:
            agree, kind = True, "agree: rule does not apply" + (" (recurring match but not a dispute)" if recurring else "")
        out.append({"case_id": a["case_id"], "agree": agree, "kind": kind, "rule_fires": fires, "recurring_match": recurring,
                    "match": ({"n_prior": rows[0][6], "median_gap_days": round(rows[0][7], 2), "merchant": rows[0][5]} if rows else None),
                    "rule_actions": rule_actions, "trigger_type": p["trigger_type"], "flagged_txn_id": p["flagged_txn_id"],
                    "agent_verdict": a["verdict"], "agent_final_actions": a["final_actions"], "agent_cites_R7": "R7" in a["cites"],
                    "as_of": p["as_of"], "query": " ".join(sql.split()), "params": {**det["sql_params"], "txn_id": p["flagged_txn_id"]}})
    return out


def compile_and_backtest(rule_id: str, con: Any | None = None, write: bool = True) -> dict[str, Any]:
    import duckdb

    if rule_id not in RULES:
        raise NotImplementedError(f"{rule_id}: the compiler knows {sorted(RULES)}; other rules are action rules with no data shape")
    own = con is None
    con = con or duckdb.connect(str(DB), read_only=True)
    try:
        det = compile_rule(rule_id)
        t1 = time.perf_counter()
        bt = (_backtest_r5 if rule_id == "R5" else _backtest_r7)(con, det)
        t2 = time.perf_counter()
        cmp = (_compare_r5 if rule_id == "R5" else _compare_r7)(con, det)
        t3 = time.perf_counter()
    finally:
        if own:
            con.close()
    out = {
        "detector_id": f"POLICY-{rule_id}", "policy": {"file": "policy/fraud_policy.md", "version": "1.0",
                                                     "sha256": hashlib.sha256(POLICY_MD.read_bytes()).hexdigest()},
        "compiler": "deterministic regex parse (engine/policy_compiler.py); no LLM call", **det,
        "timing_ms": {"prose_to_detector": det["compile_ms"], "backtest": round((t2 - t1) * 1000, 1), "comparison": round((t3 - t2) * 1000, 1)},
        "backtest": bt,
        "comparison": {"n": len(cmp), "agree": sum(c["agree"] for c in cmp), "disagree": sum(not c["agree"] for c in cmp),
                       "agree_ids": [c["case_id"] for c in cmp if c["agree"]],
                       "disagree_ids": [c["case_id"] for c in cmp if not c["agree"]], "rule_fires_ids": [c["case_id"] for c in cmp if c["rule_fires"]],
                       "cases": cmp},
    }
    if write:
        (OUT_DIR / f"policy_detector_{rule_id}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys

    for rid in sys.argv[1:] or ["R5", "R7"]:
        r = compile_and_backtest(rid)
        b, c = r["backtest"], r["comparison"]
        print(f"{rid}: compiled in {r['timing_ms']['prose_to_detector']} ms; params {r['params']}")
        print(f"  backtest: { {k: (len(v) if isinstance(v, list) else v) for k, v in b.items() if k not in ('query', 'params')} }")
        print(f"  vs agent: {c['agree']}/{c['n']} agree; disagree {c['disagree_ids']}; rule fires on {c['rule_fires_ids']}")

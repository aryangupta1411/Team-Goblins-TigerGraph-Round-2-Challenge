"""etl/delta_gates.py — the checks a delta build must pass before anything is exported (and the findings it must report).

A gate is PASS, FAIL (stop: nothing is exported), WARN (a finding a human reads before the graph is touched) or INFO (a
measurement). FAIL means an invariant of delta mode is broken: the base is not the snapshot we think it is, the replay of the
base through the ETL SQL is not the shipped tables, the month does not append cleanly, or old history would change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import duckdb

from etl import parse_closed_cases as PCC
from etl.delta_base import BASE_COUNTS, BASE_GRAPH_COUNTS, graph_counts
from ops.console import Col, Table, detail

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


@dataclass
class Gate:
    name: str
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "summary": self.summary, "detail": self.detail}


@dataclass
class BuildContext:
    con: duckdb.DuckDBPyConnection       # the work DB, base attached as `e`
    base: duckdb.DuckDBPyConnection      # reconstruct_base(): the replayed base, shipped card_feat / device_profile
    score: str                           # 'frozen' | 'none'
    stats: dict[str, Any]                # what the build measured (month rows, scorer, normalisations)
    models_dir: str = "data/models"


def failed(gates: list[Gate]) -> list[Gate]:
    return [g for g in gates if g.status == FAIL]


def render(gates: list[Gate], title: str = "delta gates") -> None:
    t = Table(Col("gate", max_width=26), Col("result", width=6, align="center"), Col("what it found", max_width=96), title=title)
    for g in gates:
        t.add_row(g.name, g.status, g.summary, style="red" if g.status == FAIL else "yellow" if g.status == WARN else None)
    t.print()
    for g in gates:
        if g.status in (FAIL, WARN):
            for line in g.detail.get("lines", [])[:12]:
                detail(f"{g.name}: {line}")


# ------------------------------------------------------------------------------------------------ helpers
def _types(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute(f"DESCRIBE {table}").fetchall()}


def diff_cells(con, left: str, right: str, key: str, columns: list[str] | None = None, skip: tuple[str, ...] = (),
               tol: float = 1e-9) -> dict[str, Any]:
    """Row- and cell-level difference between two tables joined on `key`. DOUBLE columns compare with a relative tolerance
    (a different DuckDB build may differ in the last digit of a quantile); everything else is exact."""
    types = _types(con, right)
    cols = [c for c in (columns or list(types)) if c != key and c not in skip]
    out: dict[str, Any] = {
        "only_left": con.execute(f"SELECT count(*) FROM {left} x WHERE NOT EXISTS (SELECT 1 FROM {right} y WHERE y.{key} = x.{key})").fetchone()[0],
        "only_right": con.execute(f"SELECT count(*) FROM {right} y WHERE NOT EXISTS (SELECT 1 FROM {left} x WHERE x.{key} = y.{key})").fetchone()[0],
        "cells": {},
    }
    for c in cols:
        if types.get(c, "") in ("DOUBLE", "FLOAT"):
            cond = f"(x.{c} IS NULL) <> (y.{c} IS NULL) OR abs(x.{c} - y.{c}) > {tol} * (1 + abs(y.{c}))"
        else:
            cond = f"x.{c} IS DISTINCT FROM y.{c}"
        n = con.execute(f"SELECT count(*) FROM {left} x JOIN {right} y ON x.{key} = y.{key} WHERE {cond}").fetchone()[0]
        if n:
            out["cells"][c] = n
    return out


def canon(blob: str) -> list[str]:
    return sorted(json.dumps(x, sort_keys=True) for x in json.loads(blob or "[]"))


def recurring_differences(con, left: str, right: str) -> tuple[int, int]:
    """(cards whose recurring_amounts lists hold different entries, cards that differ only in the order of equal-ranked entries)."""
    rows = con.execute(f"SELECT x.id, x.recurring_amounts, y.recurring_amounts FROM {left} x JOIN {right} y USING (id) "
                       "WHERE x.recurring_amounts <> y.recurring_amounts").fetchall()
    semantic = sum(1 for _, a, b in rows if canon(a) != canon(b))
    return semantic, len(rows) - semantic


# ------------------------------------------------------------------------------------------------ gates
def gate_base_snapshot(ctx: BuildContext) -> Gate:
    con = ctx.con
    got = {t: con.execute(f"SELECT count(*) FROM e.{t}").fetchone()[0] for t in BASE_COUNTS}
    lines = [f"e.{t}: {got[t]:,} rows, expected {BASE_COUNTS[t]:,}" for t in BASE_COUNTS if got[t] != BASE_COUNTS[t]]
    null_cms = con.execute("SELECT count(*) FROM e.txc WHERE cms_p IS NULL").fetchone()[0]
    if null_cms:
        lines.append(f"e.txc: {null_cms:,} rows without cms_p")
    derived = graph_counts(ctx.base)
    lines += [f"{k}: the base derives {derived[k]:,}, the workspace held {v:,}" for k, v in BASE_GRAPH_COUNTS.items() if derived[k] != v]
    last = con.execute("SELECT max(ts) FROM e.txc").fetchone()[0]
    return Gate("base_snapshot", FAIL if lines else PASS,
                "; ".join(lines[:3]) if lines else f"{got['txc']:,} transactions to {last:%Y-%m-%d}; {len(BASE_GRAPH_COUNTS)} graph counts re-derive exactly",
                {"counts": got, "lines": lines, "last_ts": str(last)})


def gate_replay_fidelity(ctx: BuildContext) -> Gate:
    """The base replayed through etl.features == the shipped tables. This is what makes the union build the same pipeline."""
    b = ctx.base
    lines: list[str] = []
    report: dict[str, Any] = {}

    dp = diff_cells(b, "device_profile_replay", "device_profile", "id")
    report["device_profile"] = dp
    cf = diff_cells(b, "card_feat_replay", "card_feat", "id", skip=("wcc_id", "device_degree", "fraud_ppr", "recurring_amounts"))
    semantic, order_only = recurring_differences(b, "card_feat_replay", "card_feat")
    if semantic:
        cf["cells"]["recurring_amounts"] = semantic
    report["card_feat"] = {**cf, "recurring_order_only": order_only}
    derived_cols = ["card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_med_amt", "prior_max_amt", "ring_hit", "burst_id", "cms_p"]
    tx = diff_cells(b, "txn_feat", "e.txc", "TransactionID", columns=derived_cols)
    report["txn_feat"] = tx
    for name, d in (("device_profile", dp), ("card_feat", cf), ("txn_feat", tx)):
        if d["only_left"] or d["only_right"] or d["cells"]:
            lines.append(f"{name}: {d['only_left']} rows only in the replay, {d['only_right']} only shipped, differing cells {d['cells']}")
    nxt = b.execute("SELECT count(*) FROM txn_feat WHERE prev_txn_id IS NOT NULL").fetchone()[0]
    want = b.execute("SELECT count(*) FROM txc").fetchone()[0] - b.execute("SELECT count(*) FROM cardmap").fetchone()[0]
    if nxt != want:
        lines.append(f"NEXT chain: {nxt:,} edges, expected transactions - cards = {want:,}")
    n_dev, n_card = b.execute("SELECT count(*) FROM device_profile").fetchone()[0], b.execute("SELECT count(*) FROM card_feat").fetchone()[0]
    return Gate("replay_fidelity", FAIL if lines else PASS,
                "; ".join(lines[:2]) if lines else f"the base replayed through etl.features equals the shipped tables ({n_card:,} cards, {n_dev:,} devices, "
                f"{b.execute('SELECT count(*) FROM txn_feat').fetchone()[0]:,} transactions; {order_only} cards differ only in the order of equal-ranked recurring entries)",
                {"report": report, "lines": lines})


def gate_month_inputs(ctx: BuildContext) -> Gate:
    con, s = ctx.con, ctx.stats
    problems: list[str] = []
    warnings: list[str] = []
    n_raw, n_join = s["month_rows_raw"], s["month_rows_joined"]
    if not n_raw:
        problems.append("the month's transaction file has no rows")
    if n_join != n_raw:
        problems.append(f"{n_raw - n_join:,} transactions match no card in the base cardmap (a new customer or card type: delta mode cannot add cards)")
    dup = n_raw - con.execute("SELECT count(DISTINCT TransactionID) FROM tx_raw").fetchone()[0]
    if dup:
        problems.append(f"{dup:,} duplicate TransactionIDs in the month file")
    overlap = con.execute("SELECT count(*) FROM tx_raw r JOIN e.txc b ON b.TransactionID = TRY_CAST(r.TransactionID AS BIGINT)").fetchone()[0]
    if overlap:
        problems.append(f"{overlap:,} month TransactionIDs already exist in the base")
    bad = con.execute("""SELECT count(*) FROM tx_raw WHERE TRY_CAST(TransactionID AS BIGINT) IS NULL OR TRY_CAST(TransactionAmt AS DOUBLE) IS NULL
                         OR TRY_CAST(ts AS TIMESTAMP) IS NULL OR customer_id IS NULL OR customer_id = ''""").fetchone()[0]
    if bad:
        problems.append(f"{bad:,} month rows with an unreadable id, amount, timestamp or customer")
    lo, hi = con.execute("SELECT min(ts::TIMESTAMP), max(ts::TIMESTAMP) FROM tx_raw").fetchone()
    base_hi = con.execute("SELECT max(ts) FROM e.txc").fetchone()[0]
    if lo is not None and lo <= base_hi:
        problems.append(f"the month starts {lo} but the base ends {base_hi}: delta mode only appends")
    card4 = con.execute("""SELECT count(*) FROM txc m JOIN e.card_feat f ON f.id = m.card_id
                           WHERE NOT EXISTS (SELECT 1 FROM e.txc b WHERE b.TransactionID = m.TransactionID)
                             AND coalesce(m.card4, 'unknown') <> f.card_network""").fetchone()[0]
    if card4:
        problems.append(f"{card4:,} month rows carry a card network different from their card's: the modal network cannot be recomputed without Round 1's card4 counts")
    n_idn = con.execute("SELECT count(*) FROM idn").fetchone()[0]
    orphan = con.execute("SELECT count(*) FROM idn i WHERE NOT EXISTS (SELECT 1 FROM tx_raw r WHERE TRY_CAST(r.TransactionID AS BIGINT) = i.TransactionID)").fetchone()[0]
    dup_idn = n_idn - con.execute("SELECT count(DISTINCT TransactionID) FROM idn").fetchone()[0]
    if orphan or dup_idn:
        problems.append(f"identity file: {orphan:,} rows without a month transaction, {dup_idn:,} duplicate ids")
    if s.get("missing_raw_columns"):
        (problems if ctx.score == "frozen" else warnings).append(
            f"the month file lacks {len(s['missing_raw_columns'])} columns the frozen scorer reads: {', '.join(s['missing_raw_columns'][:6])}")
    if s.get("unmodelled_columns"):
        warnings.append(f"{len(s['unmodelled_columns'])} month columns are neither scorer features nor ETL inputs (schema drift?): {', '.join(s['unmodelled_columns'][:6])}")
    cov_m = n_idn / n_raw if n_raw else 0
    cov_b = con.execute("SELECT avg(has_identity::INT) FROM e.txc").fetchone()[0]
    info = {"rows": n_raw, "identity_rows": n_idn, "first_ts": str(lo), "last_ts": str(hi),
            "identity_coverage_month": round(cov_m, 4), "identity_coverage_base": round(cov_b, 4)}
    status = FAIL if problems else WARN if warnings else PASS
    msg = ("; ".join((problems or warnings)[:2]) if problems or warnings else
           f"{n_raw:,} transactions {lo:%Y-%m-%d} to {hi:%Y-%m-%d}, all on existing cards, no id collision; "
           f"identity on {cov_m:.1%} (base {cov_b:.1%})")
    return Gate("month_inputs", status, msg, {**info, "lines": problems + warnings})


def gate_closed_case_history(ctx: BuildContext) -> Gate:
    con = ctx.con
    problems: list[str] = []
    warnings: list[str] = []
    total = con.execute("SELECT count(*) FROM cc").fetchone()[0]
    base_n = con.execute("SELECT count(*) FROM e.cc").fetchone()[0]
    if total != con.execute("SELECT count(DISTINCT case_id) FROM cc").fetchone()[0]:
        problems.append("duplicate case ids in the closed-case history")
    missing = con.execute("SELECT count(*) FROM e.cc b WHERE NOT EXISTS (SELECT 1 FROM cc n WHERE n.case_id = b.case_id)").fetchone()[0]
    changed = con.execute("""SELECT count(*) FROM e.cc r JOIN cc n USING (case_id) WHERE
        r.customer_id <> n.customer_id OR r.card_id <> n.card_id OR r.outcome <> n.outcome OR r.pattern <> n.pattern
        OR coalesce(r.first_fraud_txn_id, '') <> coalesce(n.first_fraud_txn_id, '') OR r.txn_ids <> n.txn_ids
        OR abs(r.exposure_usd - n.exposure_usd::DOUBLE) > 0.005 OR coalesce(r.connected_card_ids, '') <> coalesce(n.connected_card_ids, '')
        OR r.actions_taken <> n.actions_taken OR r.analyst_notes <> n.analyst_notes OR r.n_txns <> n.n_txns::INT
        OR (CASE WHEN r.report_filed THEN 'Yes' ELSE 'No' END) <> n.report_filed
        OR strftime(r.opened_at, '%Y-%m-%d %H:%M:%S') <> n.opened_at OR strftime(r.closed_at, '%Y-%m-%d %H:%M:%S') <> n.closed_at""").fetchone()[0]
    if missing or changed:
        problems.append(f"the replaced history drops {missing} and rewrites {changed} of the base's {base_n:,} closed cases")
    new_ids = [r[0] for r in con.execute("SELECT case_id FROM cc WHERE case_id NOT IN (SELECT case_id FROM e.cc) ORDER BY 1").fetchall()]
    top = con.execute("SELECT max(case_id) FROM e.cc").fetchone()[0]
    want = [f"CC-{int(top[3:]) + 1 + i:04d}" for i in range(len(new_ids))]
    if new_ids != want:
        problems.append(f"appended case ids are not contiguous after {top}: {new_ids[:3]}...")
    tmpl = dict(con.execute("""SELECT template_id, count(*) FROM closed_case_parsed WHERE id IN (SELECT case_id FROM e.cc) GROUP BY 1""").fetchall())
    if tmpl != dict(PCC.EXPECTED_TEMPLATES):
        problems.append(f"the base cases parse to {tmpl}, the documented counts are {PCC.EXPECTED_TEMPLATES}")
    new_tmpl = dict(con.execute("""SELECT template_id, count(*) FROM closed_case_parsed WHERE id NOT IN (SELECT case_id FROM e.cc) GROUP BY 1 ORDER BY 1""").fetchall())
    month_lo = con.execute("SELECT min(ts::TIMESTAMP) FROM tx_raw").fetchone()[0]
    late = con.execute("SELECT id, opened_at, closed_at FROM closed_case_parsed WHERE id NOT IN (SELECT case_id FROM e.cc) AND closed_at >= ? ORDER BY id", [month_lo]).fetchall()
    if late:
        warnings.append(f"{len(late)} appended cases close inside the new month (a month case opened before their close date must not see them): "
                        + ", ".join(f"{i} opened {o:%Y-%m-%d} closed {c:%Y-%m-%d}" for i, o, c in late))
    status = FAIL if problems else WARN if warnings else PASS
    msg = ("; ".join((problems or warnings)[:2]) if problems or warnings else
           f"the {base_n:,} base cases are byte-identical; {len(new_ids)} appended ({new_ids[0]}..{new_ids[-1]}) parse to {new_tmpl}")
    return Gate("closed_case_history", status, msg, {"base_cases": base_n, "total": total, "appended": new_ids, "new_templates": new_tmpl,
                                                      "base_templates": tmpl, "lines": problems + warnings})


def gate_history_invariance(ctx: BuildContext) -> Gate:
    """Appending a month must not rewrite the base's history: every derived column of every base transaction is unchanged,
    except ring_hit, and each ring_hit flip is explained by a card whose ring_id changed."""
    con = ctx.con
    problems: list[str] = []
    cols = ["card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_med_amt", "prior_max_amt", "burst_id", "cms_p"]
    d = diff_cells(con, "txn_feat", "e.txc", "TransactionID", columns=cols)
    if d["only_right"] or d["cells"]:
        problems.append(f"base transactions changed: {d['only_right']} missing, differing cells {d['cells']}")
    base_arrow = ctx.base.execute("SELECT TransactionID, prev_txn_id, gap_seconds FROM txn_feat").arrow()
    con.register("_base_next", base_arrow)
    n_next = con.execute("""SELECT count(*) FROM txn_feat t JOIN _base_next b USING (TransactionID)
                            WHERE t.prev_txn_id IS DISTINCT FROM b.prev_txn_id OR t.gap_seconds IS DISTINCT FROM b.gap_seconds""").fetchone()[0]
    con.unregister("_base_next")
    if n_next:
        problems.append(f"{n_next:,} base NEXT edges changed (previous transaction or gap)")
    flips = con.execute("""SELECT t.TransactionID, t.card_id, b.ring_hit, t.ring_hit FROM txn_feat t JOIN e.txc b USING (TransactionID)
                           WHERE t.ring_hit <> b.ring_hit ORDER BY 1""").fetchall()
    ring_changed = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT a.id, b.ring_id, a.ring_id FROM card_feat a JOIN e.card_feat b USING (id) WHERE a.ring_id <> b.ring_id").fetchall()}
    unexplained = [f for f in flips if f[1] not in ring_changed]
    if unexplained:
        problems.append(f"{len(unexplained)} base ring_hit flips on cards whose ring_id did not change, e.g. {unexplained[0][0]}")
    card = con.execute("""SELECT count(*) FROM card_feat a JOIN e.card_feat b USING (id) WHERE a.customer_id <> b.customer_id OR a.card_type <> b.card_type
                          OR a.card_network <> b.card_network OR a.first_ts <> b.first_ts OR a.n_txns < b.n_txns OR a.last_ts < b.last_ts""").fetchone()[0]
    gone_c = con.execute("SELECT count(*) FROM e.card_feat b WHERE NOT EXISTS (SELECT 1 FROM card_feat a WHERE a.id = b.id)").fetchone()[0]
    dev = con.execute("""SELECT count(*) FROM device_profile a JOIN e.device_profile b USING (id) WHERE a.first_seen <> b.first_seen OR a.n_txns < b.n_txns
                         OR a.n_cards_alltime < b.n_cards_alltime OR a.last_seen < b.last_seen""").fetchone()[0]
    gone_d = con.execute("SELECT count(*) FROM e.device_profile b WHERE NOT EXISTS (SELECT 1 FROM device_profile a WHERE a.id = b.id)").fetchone()[0]
    if card or gone_c or dev or gone_d:
        problems.append(f"cards/devices not append-only: {card} cards and {dev} devices regress an immutable or monotone field, {gone_c} cards and {gone_d} devices vanish")
    status = FAIL if problems else WARN if flips else PASS
    msg = ("; ".join(problems[:2]) if problems else
           (f"{len(flips)} base transactions flip ring_hit (patched, not rewritten): {len(ring_changed)} cards' ring device changes with the 20 new closed cases"
            if flips else "no base row changes"))
    if not problems and not flips:
        msg = "no base row changes"
    elif not problems:
        msg += "; every other derived column of all base rows is identical"
    return Gate("history_invariance", status, msg, {"ring_hit_flips": [list(f) for f in flips], "ring_id_changes": {k: list(v) for k, v in ring_changed.items()},
                                                       "lines": problems + [f"ring_hit {f[2]} -> {f[3]} on {f[0]} ({f[1]})" for f in flips]})


def gate_case_pack(ctx: BuildContext) -> Gate:
    con = ctx.con
    problems: list[str] = []
    findings: list[str] = []
    n = con.execute("SELECT count(*) FROM cp").fetchone()[0]
    if not n or n != con.execute("SELECT count(DISTINCT case_id) FROM cp").fetchone()[0]:
        problems.append("the case pack is empty or has duplicate case ids")
    absent = con.execute("SELECT case_id, flagged_txn_id FROM cp WHERE TRY_CAST(flagged_txn_id AS BIGINT) NOT IN (SELECT TransactionID FROM txc) OR TRY_CAST(flagged_txn_id AS BIGINT) IS NULL").fetchall()
    if absent:
        problems.append(f"flagged transactions that do not exist: {absent[:3]}")
    rows = con.execute("""SELECT p.case_id, p.card_id, t.card_id, p.customer_id, t.customer_id, p.opened_at::TIMESTAMP, t.ts, TRY_CAST(p.risk_score AS DOUBLE),
                                 t.risk_score, p.trigger_type
                          FROM cp p JOIN txc t ON t.TransactionID = TRY_CAST(p.flagged_txn_id AS BIGINT) ORDER BY p.case_id""").fetchall()
    cust = [r[0] for r in rows if r[3] != r[4]]
    if cust:
        problems.append(f"pack customer differs from the flagged transaction's customer: {cust}")
    card = [(r[0], r[1], r[2]) for r in rows if r[1] != r[2]]
    for case, pack_card, txn_card in card:
        findings.append(f"{case}: the pack says card {pack_card}, the flagged transaction is on {txn_card}")
    unknown_card = con.execute("SELECT count(*) FROM cp WHERE card_id NOT IN (SELECT card_id FROM cardmap)").fetchone()[0]
    if unknown_card:
        problems.append(f"{unknown_card} pack cards are not in the cardmap")
    early = [r[0] for r in rows if r[5] < r[6]]
    if early:
        findings.append(f"opened before the flagged transaction: {early}")
    scored = [(r[0], r[7], r[8]) for r in rows if r[9] == "risk_score" and r[7] is not None]
    off = [(c, p, t) for c, p, t in scored if abs(p - t) > 0.02]
    if off:
        findings.append(f"{len(off)} of {len(scored)} risk_score cases carry a pack score that differs from the transaction's risk_score by more than 0.02 "
                        f"(both are kept; e.g. {off[0][0]}: pack {off[0][1]:.2f}, transaction {off[0][2]:.2f})")
    by_type = dict(con.execute("SELECT trigger_type, count(*) FROM cp GROUP BY 1 ORDER BY 1").fetchall())
    status = FAIL if problems else WARN if findings else PASS
    msg = ("; ".join(problems[:2]) if problems else f"{n} cases {by_type}: every flagged transaction and card exists" + (f"; {len(findings)} findings to review" if findings else ""))
    return Gate("case_pack", status, msg, {"cases": n, "by_trigger": by_type, "card_mismatch": [list(c) for c in card],
                                          "score_mismatch": [list(o) for o in off], "lines": problems + findings})


def gate_scorer(ctx: BuildContext) -> Gate:
    s = ctx.stats.get("scorer")
    if ctx.score == "none" or not s:
        return Gate("scorer", WARN, "month left unscored (cms_p = -1): the engine must not read cms_p for these transactions", {"lines": ["--score none"]})
    lines = []
    if s["flagged_above_0.3"] == 0 and s["flagged_n"]:
        lines.append(f"the frozen scorer puts none of the {s['flagged_n']} case-pack flagged transactions above 0.3 (max {s['flagged_max']:.3f})")
    if s["corr_risk_score"] < 0.3:
        lines.append(f"cms_p tracks the bank's risk_score weakly this month (corr {s['corr_risk_score']:.2f}, base {s['corr_risk_score_base']:.2f})")
    return Gate("scorer", WARN if lines else INFO,
                f"frozen model scored {s['n']:,} rows: mean {s['mean']:.4f} (base months {s['mean_base']:.4f}), share > 0.5 {s['gt_0.5']:.2%} (base {s['gt_0.5_base']:.2%})"
                + ("; treat cms_p as out of distribution" if lines else ""), {**s, "lines": lines})


def run_build_gates(ctx: BuildContext) -> list[Gate]:
    return [gate_base_snapshot(ctx), gate_replay_fidelity(ctx), gate_month_inputs(ctx), gate_closed_case_history(ctx),
            gate_history_invariance(ctx), gate_case_pack(ctx), gate_scorer(ctx)]

"""LLM-free engine run over a case pack — the day-6 fallback and the expectation-sheet generator.

For every case (in opened_at order):
  facts     = DuckFacts.collect(ctx)                       (mock backend; live: the same query names over MCP)
  sc_pre    = scorecard.compute(ctx, facts, [])
  initial   = policy.admissible(initial) → reasons
  request   = none if §6 test 1 already holds, else voi.should_ask → simulator.reply → scorecard.post_evidence
  final     = policy.admissible(final) → reasons
  status / stop_reason / sar / summary → answer_writer.build → validator.validate → out/<case_id>.draft.json

Run:  python -m engine.run_cases                 # 20 exam cases → engine/out/*.draft.json + qa/engine_expectation_sheet.md
      python -m engine.run_cases --cases HHG-014 HHG-006
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from engine import answer_writer, config, policy, scorecard, simulator, stop, validator, voi
from engine import status as status_rule
from engine.facts_duckdb import DuckFacts
from engine.types import CaseContext


def contexts(fx: DuckFacts, only: list[str] | None = None) -> list[CaseContext]:
    cp = fx.q("SELECT * FROM cp ORDER BY opened_at, case_id")
    out = []
    for r in cp.itertuples():
        if only and r.case_id not in only:
            continue
        rs = None if r.risk_score != r.risk_score else float(r.risk_score)
        out.append(CaseContext(r.case_id, r.trigger_type, r.trigger_text, str(r.flagged_txn_id), r.card_id, r.customer_id,
                               r.opened_at.strftime("%Y-%m-%d %H:%M:%S"), rs))
    return out


def run_one(ctx: CaseContext, fx: DuckFacts, facts: dict | None = None) -> dict:
    """Deterministic decision for one case. Returns the answer dict plus engine internals for the sheet."""
    t0 = time.time()
    facts = facts or fx.collect(ctx)
    n_calls = len(facts)
    sc_pre = scorecard.compute(ctx, facts, [])
    settled = stop.settled_pre(sc_pre)
    request = None
    sc_post = sc_pre
    branches: dict = {}
    asked = False
    voi_zero = False
    if not settled:
        rt = voi.request_type_for(ctx, sc_pre)
        ask, branches = voi.should_ask(ctx, sc_pre, rt)
        # D1: a fraud-band case that is not settled (§6 test 1) still makes its one §3b verification request even when the
        # value-of-information test finds no branch that changes the action set; a settled case never asks (handled above).
        if not ask and sc_pre.verdict == "fraud" and ctx.trigger_type != "analyst_request":
            ask, voi_zero = True, True
        if ask:
            # the request is part of the initial recommendation: 3a opens the case, the verification action matches the request type
            sc_pre.flags["asked"] = True
            sc_pre.flags["request_type"] = rt
            rep = simulator.reply(rt, ctx, sc_pre)
            sc_post = scorecard.post_evidence(sc_pre, rep["outcome"])
            sc_post.flags["asked"] = True
            sc_post.flags["request_seq"] = 1
            request = {"type": rt, "asked_after_step": n_calls, "assumed_response": rep["assumed_response"],
                       "outcome": rep["outcome"], "counterfactual": rep["counterfactual"]}
            asked = True
        else:
            voi_zero = True
    adm_i = policy.admissible(ctx, sc_pre, "initial")
    initial = answer_writer.reasons_for(adm_i["required"], adm_i, ctx, sc_pre, "initial", request["type"] if request else "")
    if asked:
        adm_f = policy.admissible(ctx, sc_post, "final")
        final = answer_writer.reasons_for(adm_f["required"], adm_f, ctx, sc_post, "final", request["type"])
    else:
        adm_f, final = adm_i, list(initial)
    viol_i = policy.check(initial, ctx, sc_pre, "initial")
    viol_f = policy.check(final, ctx, sc_post, "final")
    pending = bool(sc_post.flags.get("pending"))
    st = status_rule.derive(sc_post.verdict, final, pending)
    sc_post.flags["escalated"] = st == "escalated"
    reason = stop.stop_reason(sc_pre, sc_post if asked else None, asked, voi_zero)
    graph_case_id = f"AC-{ctx.case_id}"
    sar = answer_writer.draft_sar(ctx, sc_post, facts, graph_case_id)
    requests = [request] if request else []
    summary = answer_writer.draft_summary(ctx, sc_pre, sc_post, initial, final, request, st)
    # evidence for the file: the engine ledger minus neutral notes beyond the first few, plus the assumed reply
    evid = [e for e in sc_post.evidence if e.direction != "neutral"] + [e for e in sc_post.evidence if e.direction == "neutral"][:3]
    closing = {"summary": summary, "stop_reason": reason, "status": st, "similar_prior_cases_used": sc_post.similar_prior_cases,
               "written_to_graph": False,
               "what_changed": answer_writer.what_changed_text(initial, final, request, sc_pre, sc_post)}
    runlog = {"tool_calls": n_calls, "tokens": 0, "latency_s": time.time() - t0}
    ans = answer_writer.build(ctx, sc_pre, sc_post, initial, final, evid, requests, sar, closing, runlog, graph_case_id)
    meta = {"p_pre": sc_pre.p_engine, "families_fraud_pre": len(sc_pre.families_fraud), "opened_at": ctx.opened_at, "card_id": ctx.card_id,
            "customer_id": ctx.customer_id}
    errors = validator.validate(ans, fx, meta)
    return {"answer": ans, "sc_pre": sc_pre, "sc_post": sc_post, "request": request, "branches": branches, "initial": initial, "final": final,
            "violations": viol_i + viol_f, "errors": errors, "settled": settled, "voi_zero": voi_zero, "adm_initial": adm_i, "adm_final": adm_f}


def sheet_row(ctx: CaseContext, r: dict) -> dict:
    a, b = r["sc_pre"], r["sc_post"]
    adj = "; ".join(f"{k} {v:+.2f}" if v else k for k, v in a.adjustments)
    return {"case": ctx.case_id, "trigger": ctx.trigger_type, "cms": a.cms_p, "cal_p": a.cal_p, "adjustments": adj, "p_pre": a.p_engine,
            "F_pre": "/".join(sorted(a.families_fraud)) or "-", "L_pre": "/".join(sorted(a.families_legit)) or "-", "verdict_pre": a.verdict,
            "pattern": b.pattern, "episode": ",".join(b.episode_ids), "exposure": b.exposure_usd, "connected": len(b.connected_card_ids),
            "initial": ", ".join(f"{x['action']}({x['route']})" for x in r["initial"]),
            "request": f"{r['request']['type']} → {r['request']['outcome']}" if r["request"] else ("none (settled)" if r["settled"] else "none (VOI 0)"),
            "p_post": b.p_engine, "verdict_post": b.verdict, "final": ", ".join(f"{x['action']}({x['route']})" for x in r["final"]),
            "status": r["answer"]["case"]["status"], "sar": r["answer"]["sar"]["file"]}


def write_sheet(rows: list[dict], path: Path) -> None:
    cols = ["case", "trigger", "cms", "cal_p", "adjustments", "p_pre", "F_pre", "L_pre", "verdict_pre", "pattern", "episode", "exposure", "connected",
            "initial", "request", "p_post", "verdict_post", "final", "status", "sar"]
    lines = ["# Engine expectation sheet (regenerated from the deterministic engine, `python -m engine.run_cases`, on the ETL facts DB "
             "`engine.facts_from_etl` — the sheet of record, decision D7)", "",
             "Cases in `opened_at` order. `F_pre` / `L_pre` = evidence families for fraud / legitimacy before any request; "
             "`p_pre` = calibrated engine probability after adjustments and floors; `request` = the simulated evidence request and its assumed outcome.", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]).replace("|", "\\|") for c in cols) + " |")
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--out", default=str(config.OUT_DIR))
    ap.add_argument("--sheet", default=str(Path(config.HERE).parent / "qa" / "engine_expectation_sheet.md"),
                    help="where the expectation sheet is written (default: qa/engine_expectation_sheet.md)")
    args = ap.parse_args()
    config.ensure_dirs()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fx = DuckFacts()
    rows = []
    n_err = 0
    for ctx in contexts(fx, args.cases):
        r = run_one(ctx, fx)
        (out / f"{ctx.case_id}.draft.json").write_text(json.dumps(r["answer"], indent=2))
        (out / f"{ctx.case_id}.engine.json").write_text(json.dumps({"scorecard_pre": r["sc_pre"].to_dict(), "scorecard_post": r["sc_post"].to_dict(),
                                                                    "branches": r["branches"], "adm_initial": {k: v for k, v in r["adm_initial"].items() if k != "conditions"},
                                                                    "violations": r["violations"], "errors": r["errors"]}, indent=1, default=str))
        rows.append(sheet_row(ctx, r))
        flag = "OK " if not r["errors"] and not r["violations"] else "ERR"
        print(f"{flag} {ctx.case_id} {ctx.trigger_type:15s} cms={r['sc_pre'].cms_p:.3f} p_pre={r['sc_pre'].p_engine:.2f} {r['sc_pre'].verdict:10s} "
              f"F={sorted(r['sc_pre'].families_fraud)} L={sorted(r['sc_pre'].families_legit)} → {r['request']['type'] + '/' + r['request']['outcome'] if r['request'] else '-':28s} "
              f"p_post={r['sc_post'].p_engine:.2f} {r['sc_post'].verdict:10s} {r['sc_post'].pattern:28s} exp={r['sc_post'].exposure_usd:8.2f} "
              f"status={r['answer']['case']['status']:17s} sar={r['answer']['sar']['file']}")
        if r["errors"]:
            n_err += 1
            print("   validator:", r["errors"])
        if r["violations"]:
            print("   policy:", r["violations"])
    write_sheet(rows, Path(args.sheet))
    print(f"\n{len(rows)} cases, {n_err} with validator errors; sheet → {args.sheet}")


if __name__ == "__main__":
    main()

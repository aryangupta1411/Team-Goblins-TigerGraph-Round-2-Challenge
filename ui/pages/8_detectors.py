"""Detectors & containment: the measured proof for C1 (new typologies), C2 (self-growing proposals), C3 (policy text to
detector), C5 (ring replay) and C7 (containment).

Everything here is read from files: engine/out/detector_backtest.json (never recomputed on view; only the operator's
'Re-run backtest' button calls run_backtest, which rewrites that file), engine/out/containment_summary.json and the
proof snapshot. A containment plan is computed by engine.containment on the read-only DuckDB when that module is
importable, else read from the snapshot, else reduced to the summary totals. Every containment number is a proposal
awaiting approval; nothing on this page blocks a card. A case outside every detected wave gets no plan.
"""
from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

md = common.md
ctx = common.page_setup("detectors")
bt = common.proof("backtest")
cs = common.proof("containment_summary")
plans_snap = common.proof("containment_plans")
cont, cont_err = common.optional_import("engine.containment")
det_mod, det_err = common.optional_import("engine.detectors")

BT = bt["data"] if isinstance(bt["data"], dict) else {}
DETECTORS = [d for d in BT.get("detectors") or [] if isinstance(d, dict)]
EXAM_SCAN = {k: [str(c) for c in v or []] for k, v in (BT.get("exam_scan") or {}).items()} if isinstance(BT.get("exam_scan"), dict) else {}
PROPOSALS = [p for p in BT.get("self_growing_proposals") or [] if isinstance(p, dict)]
BT_SRC = {"source": bt["source"] or "engine/out/detector_backtest.json", "as_of": bt["as_of"] or "missing"}
APPROVALS_FILE = common.ROOT / "data" / "detector_approvals.json"
DB_PATH = common.ROOT / "data" / "hhgoa_engine.duckdb"
LOSS_SPLIT = "'Lost before the alert' settled at or before the trigger case opened; 'Preventable after the alert' is what the plan would stop if approved"


def _short_id(det_id: str) -> str:
    return str(det_id).split("_")[0]


def _engine_label() -> str:
    if cont is not None:
        return "live engine (read-only DuckDB)"
    if plans_snap["data"] is not None:
        return f"snapshot {plans_snap['generated_at'] or plans_snap['as_of']}"
    if cs["data"] is not None:
        return "containment summary totals"
    return "containment not available in this deployment"


common.page_header(
    "Detectors & containment",
    eyebrow="C1 new typologies · C2 self-growing · C3 policy → detector · C5 ring replay · C7 containment",
    chips=[(f"{BT_SRC['source']} · {BT_SRC['as_of']}", "gray"), (_engine_label(), "gray")],
)
if bt["data"] is None:
    st.warning(md(f"Detector backtest not available here: {bt['error']}"), icon=":material/error:")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _read_approvals() -> tuple[dict, str]:
    """data/detector_approvals.json (written only by engine.detectors.approve_detector): {proposal_id: record}."""
    if not APPROVALS_FILE.is_file():
        return {}, f"{common.rel(APPROVALS_FILE)} absent: no proposal has been decided"
    try:
        data = json.loads(APPROVALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"{common.rel(APPROVALS_FILE)} unreadable: {type(exc).__name__}: {exc}"
    return (data if isinstance(data, dict) else {}), ""


def _hits() -> dict[str, str]:
    """case id -> detector id, from exam_scan and each detector's exam_cases_caught."""
    out: dict[str, str] = {}
    for det_id, ids in EXAM_SCAN.items():
        for cid in ids:
            out.setdefault(cid, det_id)
    for d in DETECTORS:
        for cid in d.get("exam_cases_caught") or []:
            out.setdefault(str(cid), str(d.get("detector_id")))
    return out


def _answers(source_id: str | None) -> dict[str, dict]:
    if not source_id:
        return {}
    return {cid: v.answer for cid, v in common.source_views(source_id).items() if v.ok and isinstance(v.answer, dict)}


def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "x"


def _hard(answer: dict) -> list[str]:
    return [f"{x.get('action')} {x.get('route', 'auto')}" for x in _final(answer) if x.get("action") in common.HARD_ACTIONS]


def _fires_on(det: dict, answers: dict[str, dict]) -> str:
    parts = []
    for cid in det.get("exam_cases_caught") or []:
        a = answers.get(cid)
        if a is None:
            parts.append(f"{cid} (not in this source)")
            continue
        verdict = (a.get("case") or {}).get("verdict") or "no verdict"
        hard = _hard(a)
        parts.append(f"{cid} {verdict}" + (f": {', '.join(hard)}" if hard else ": no hard action"))
    return " · ".join(parts) or "no exam case"


HITS = _hits()
SRC_ANSWERS = _answers(ctx.source_id)
SUB_ANSWERS = _answers("submitted") if ctx.source_id != "submitted" else SRC_ANSWERS
APPROVALS, APPROVALS_NOTE = _read_approvals()


def _status(p: dict) -> str:
    rec = APPROVALS.get(str(p.get("proposal_id")))
    return str(rec.get("status")) if isinstance(rec, dict) and rec.get("status") else str(p.get("approval_status") or "pending")


def _c3() -> tuple[str | None, str, str, str]:
    """(value, detail, source, as_of) for C3 policy text -> detector."""
    if det_mod is not None:
        built = hasattr(det_mod, "compile_policy_rule_detector")
        detail = ("compile_policy_rule_detector is defined in engine/detectors.py" if built else
                  "Not built in this checkout: engine/detectors.py describes compile_policy_rule_detector in its docstring but defines no such function")
        return ("built" if built else "not built"), detail, "engine/detectors.py", common.as_of(common.ROOT / "engine" / "detectors.py")
    c3 = common.proof("c3_built")
    if c3["data"] is None:
        return None, f"not measured here: {det_err}; {c3['error']}", c3["source"] or "engine/out/proof_snapshot.json", c3["as_of"] or "missing"
    built = bool(c3["data"])
    detail = (f"snapshot {c3['generated_at']}: compile_policy_rule_detector " + ("present" if built else
              "absent: engine/detectors.py describes it in its docstring but defines no such function"))
    return ("built" if built else "not built"), detail, c3["source"], c3["as_of"]


# --------------------------------------------------------------------------- #
# Scoreboard                                                                  #
# --------------------------------------------------------------------------- #
final_hits = sorted({c for ids in EXAM_SCAN.values() for c in ids if c.startswith("HHF-")})
acted = [(str(d.get("detector_id")), c, list(d.get("cleared_cases_matched") or [])) for d in DETECTORS for c in d.get("legitimate_customers_acted_against") or []]
n_pending = sum(_status(p) == "pending" for p in PROPOSALS)
n_approved = sum(_status(p) == "approved" for p in PROPOSALS)
c3_value, c3_detail, c3_src, c3_as_of = _c3()

k = st.columns(5)
n_cc = BT.get("n_closed_cases")
common.measured("Closed cases backtested", f"{n_cc:,}" if isinstance(n_cc, int) else None, container=k[0], **BT_SRC,
                what="closed cases in the bank's history every detector was replayed over",
                how="n_closed_cases, n_confirmed_fraud and n_cleared_legitimate counted by engine.backtest_detectors")
if isinstance(BT.get("n_confirmed_fraud"), int) and isinstance(BT.get("n_cleared_legitimate"), int):
    k[0].caption(md(f"{BT['n_confirmed_fraud']:,} confirmed fraud / {BT['n_cleared_legitimate']:,} cleared"))
common.measured("Final Round cases with a detector hit", len(final_hits) if bt["data"] is not None else None, container=k[1], **BT_SRC,
                what="HHF cases on which scan_card returned a detector hit", how="exam_scan: the 18 Final Round cases scanned exactly as the engine scans them")
if final_hits:
    k[1].caption(md(", ".join(final_hits)))
common.measured("Cleared customers a detector would act against", len(acted) if bt["data"] is not None else None, container=k[2], **BT_SRC,
                what="customers whose closed case the bank cleared as legitimate, inside a detector's match set",
                how="legitimate_customers_acted_against per detector, from the cleared closed cases it matches")
for det_id, cust, cleared in acted:
    common.chip(f"{_short_id(det_id)} → {cust}" + (f" ({', '.join(cleared)})" if cleared else ""), "red", icon=":material/block:", where=k[2])
common.measured("Self-growing proposals", f"{n_pending} pending / {n_approved} approved" if PROPOSALS or bt["data"] is not None else None, container=k[3],
                source=f"{BT_SRC['source']} + {common.rel(APPROVALS_FILE)}", as_of=BT_SRC["as_of"],
                what="candidate detectors found by discover_unlabelled_patterns, each waiting for a fraud manager",
                how="approval_status from the backtest, overridden by data/detector_approvals.json when a decision is recorded")
common.measured("C3 policy text → detector", c3_value, container=k[4], source=c3_src, as_of=c3_as_of, what=c3_detail,
                how="hasattr(engine.detectors, 'compile_policy_rule_detector'), else the snapshot's c3_built")


# --------------------------------------------------------------------------- #
# Safety join: detector hits x verdicts                                       #
# --------------------------------------------------------------------------- #
with st.container(border=True, key="det-safety"):
    st.markdown("**Safety join** · detector hits crossed with the verdicts")
    checked: dict[str, list[tuple[str, str, str]]] = {}
    for label, answers in ((ctx.source_id or "(no source)", SRC_ANSWERS), ("submitted", SUB_ANSWERS)):
        if label in checked:
            continue
        checked[label] = [(cid, det, str((answers[cid].get("case") or {}).get("verdict") or "")) for cid, det in sorted(HITS.items()) if cid in answers]
    bad = [(src, cid, det, v) for src, rows in checked.items() for cid, det, v in rows if v in ("legitimate", "uncertain")]
    n_checked = sum(len(r) for r in checked.values())
    if not HITS:
        st.caption("No detector hit recorded in the backtest, so there is nothing to cross.")
    elif n_checked == 0:
        st.caption(md(f"None of the {len(HITS)} detector-hit cases has an answer in {' or '.join(checked)}: not measured here."))
    elif not bad:
        st.markdown(md(f"No detector fires on a case the answers call legitimate or uncertain ({n_checked} detector-hit answers checked in "
                       f"{' and '.join(checked)})."))
    else:
        st.error(md("A detector fires on a case the answers call legitimate or uncertain: "
                    + "; ".join(f"{cid} ({det}) is {v} in {src}" for src, cid, det, v in bad)), icon=":material/error:")
    saf = common.safety(SRC_ANSWERS)
    hard_rows = []
    for cid, a in sorted(SRC_ANSWERS.items()):
        hard = _hard(a)
        if hard:
            det = HITS.get(cid)
            hard_rows.append({"Case": cid, "Verdict": [(a.get("case") or {}).get("verdict") or "no verdict"], "Hard actions": hard,
                              "Basis": det if det else "no detector hit — see the case evidence"})
    if hard_rows:
        hard_opts = sorted({h for r in hard_rows for h in r["Hard actions"]})
        st.dataframe(pd.DataFrame(hard_rows), hide_index=True, key="det_safety", height=35 * len(hard_rows) + 38,
                     column_config={
                         "Case": st.column_config.ButtonColumn("Case", type="tertiary", key="det_safety_open", help="Open the Case file"),
                         "Verdict": st.column_config.MultiselectColumn("Verdict", options=list(common.VERDICT_COLORS), color=list(common.VERDICT_COLORS.values())),
                         "Hard actions": st.column_config.MultiselectColumn("Hard actions", options=hard_opts, color=["red"] * len(hard_opts),
                                                                            help="BLOCK_CARD / BLOCK_ALL_CARDS / FILE_REPORT in next_best_actions.final, with the route"),
                         "Basis": st.column_config.TextColumn("Basis", width="large", help="the detector whose exam scan caught the case, else none"),
                     })
        click = st.session_state.get("det_safety_open")
        if click:
            common.goto("case", case=click["label"])
    answers_path = f"runs/{ctx.source_id}/*/answer.json" if ctx.source and ctx.source.kind == "run" else "cases/*.json"
    st.caption(md(f"Hard-action cases in {ctx.source_id}: {len(hard_rows)} · hard actions on legitimate or uncertain verdicts: "
                  f"{len(saf['hard_on_non_fraud'])} · answers: {answers_path} · hits: {BT_SRC['source']} exam_scan"))


# --------------------------------------------------------------------------- #
# C1: detector backtest table                                                 #
# --------------------------------------------------------------------------- #
st.markdown("#### C1 · Detector backtest")


def _detector_table(rows: list[dict], key: str) -> None:
    if not rows:
        st.caption("No detectors in this backtest.")
        return
    data = [{
        "Detector": _short_id(d.get("detector_id")),
        "Legitimate customers acted against": [str(x) for x in d.get("legitimate_customers_acted_against") or []],
        "Cleared cases matched": [str(x) for x in d.get("cleared_cases_matched") or []],
        "Confirmed-fraud closed cases": len(d.get("closed_cases_caught") or []),
        "Fires on": _fires_on(d, SRC_ANSWERS),
        "Cards (Jan / all)": f"{d.get('cards_matched_january', '—')} / {d.get('cards_matched', '—')}",
        "Txns": d.get("txns_matched"),
        "Name": str(d.get("name") or ""),
        "Open": (d.get("exam_cases_caught") or ["—"])[0],
    } for d in rows]
    legit_opts = sorted({x for r in data for x in r["Legitimate customers acted against"]}) or ["none"]
    cleared_opts = sorted({x for r in data for x in r["Cleared cases matched"]}) or ["none"]
    st.dataframe(
        pd.DataFrame(data), hide_index=True, key=f"{key}_table", height=35 * len(data) + 38,
        column_order=["Detector", "Legitimate customers acted against", "Cleared cases matched", "Confirmed-fraud closed cases", "Fires on",
                      "Cards (Jan / all)", "Txns", "Name", "Open"],
        column_config={
            "Detector": st.column_config.TextColumn("Detector", width=70, pinned=True,
                                                    help="detector id prefix; the full id and attestation are in the popovers below"),
            "Legitimate customers acted against": st.column_config.MultiselectColumn(
                "Legit customers hit", options=legit_opts, color=["red"] * len(legit_opts), width=130,
                help="customers of the cleared closed cases inside the detector's match set: a block would act against them"),
            "Cleared cases matched": st.column_config.MultiselectColumn("Cleared cases", options=cleared_opts, color=["red"] * len(cleared_opts), width=110,
                                                                        help="closed cases the bank cleared as legitimate that the detector matches"),
            "Confirmed-fraud closed cases": st.column_config.NumberColumn("Fraud cases", width=90, help="confirmed-fraud closed cases the detector matches"),
            "Fires on": st.column_config.TextColumn("Fires on (verdict: hard actions)", width="large",
                                                    help=f"exam cases caught, with the verdict and hard actions of {ctx.source_id}"),
            "Cards (Jan / all)": st.column_config.TextColumn("Cards Jan / all", width=100),
            "Txns": st.column_config.NumberColumn("Txns", width=60),
            "Name": st.column_config.TextColumn("Name", width="medium"),
            "Open": st.column_config.ButtonColumn("Trigger case", type="tertiary", key=f"{key}_open", width=100, help="Open the trigger case's Case file"),
        },
    )
    click = st.session_state.get(f"{key}_open")
    if click and str(click["label"]) not in ("", "—"):
        common.goto("case", case=str(click["label"]))
    with st.container(horizontal=True, gap="small", key=f"{key}-attest"):
        for d in rows:
            with st.popover(md(f"{_short_id(d.get('detector_id'))} attestation"), icon=":material/verified:"):
                st.markdown(f"**{md(d.get('detector_id'))}** · {md(d.get('name'))}")
                att = d.get("attestation") or {}
                st.caption(md(f"What: {att.get('what', 'not recorded')}"))
                st.json({"evidence": att.get("evidence"), "how": att.get("how"), "when": att.get("when")}, expanded=True)


new_rows = [d for d in DETECTORS if not str(d.get("detector_id", "")).startswith("R0_")]
r0_rows = [d for d in DETECTORS if str(d.get("detector_id", "")).startswith("R0_")]
_detector_table(new_rows, "det")
st.caption(md("D1–D3 are new in January: 0 of 910 cleared cases hit shows they do not fire on legitimate history; it does not measure recall."
              if BT.get("n_cleared_legitimate") == 910 and all(not d.get("false_positives_on_cleared_cases") for d in new_rows if _short_id(d.get("detector_id")) in ("D1", "D2", "D3"))
              else f"Cleared cases in the backtest: {BT.get('n_cleared_legitimate', 'not measured')}. A detector with 0 cleared hits does not fire on legitimate history; it does not measure recall."))
with st.expander("Round 1 reference", expanded=False):
    _detector_table(r0_rows, "det_r0")


# --------------------------------------------------------------------------- #
# C7: containment planner                                                     #
# --------------------------------------------------------------------------- #
WAVES: dict[str, str] = {det: ids[0] for det, ids in sorted(EXAM_SCAN.items()) if ids and not det.startswith("R0_")}  # wave -> trigger case
case_wave = next((det for det, ids in EXAM_SCAN.items() if ctx.case_id in ids and det in WAVES), None)


def _db_sig() -> tuple:
    try:
        s = DB_PATH.stat()
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return (0, 0)


@st.cache_data(ttl=600, show_spinner="Building the containment proposal on the read-only DuckDB…")
def _engine_plan(wave_id: str, trigger: str, opened: str, _sig: tuple) -> dict:
    """engine.containment on the read-only DuckDB, anchored to the wave's trigger case. Errors come back as text."""
    mod, why = common.optional_import("engine.containment")
    if mod is None:
        return {"error": why}
    try:
        found, params = mod.wave_for_case(trigger)
        if found != wave_id:
            return {"error": f"engine.containment.wave_for_case({trigger}) found {found or 'no wave'}, not {wave_id}"}
        at = params.get("as_of") or opened
        plan = mod.build_containment_plan(wave_id, as_of=at, r_email=params.get("r_email", ""))
        window = ""
        sql = str((getattr(mod, "WAVE_DEFINITIONS", {}).get(wave_id) or {}).get("sql", ""))
        m = re.search(r"INTERVAL\s+(\d+)\s+DAY", sql)
        since = (getattr(det_mod, "SCAN_SINCE", {}) or {}).get(wave_id) if det_mod is not None else None
        if since:
            window = f"from {since}"
        elif m and params.get("r_email"):
            window = f"{m.group(1)} days before the alert, recipient email {params.get('r_email')}"
        return {"plan": plan.to_dict(), "r_email": params.get("r_email", ""), "window": window}
    except Exception as exc:  # noqa: BLE001 - a locked or absent DuckDB falls back to the snapshot
        return {"error": f"engine.containment: {type(exc).__name__}: {exc}"}


def _plan_for(wave_id: str, trigger: str, opened: str) -> dict:
    """{plan | totals, source, as_of, how, window, notes} for one wave, from the engine, else the snapshot, else the summary."""
    notes = []
    if cont is not None:
        res = _engine_plan(wave_id, trigger, opened, _db_sig())
        if res.get("plan"):
            return {"plan": res["plan"], "source": f"{common.rel(DB_PATH)} (read-only) via engine.containment", "as_of": f"computed {_now()}",
                    "how": f"build_containment_plan({wave_id}, as_of={res['plan'].get('as_of')}, r_email={res.get('r_email') or '—'})",
                    "window": res.get("window") or "not recorded", "notes": notes}
        notes.append(res.get("error") or "engine plan empty")
    else:
        notes.append(cont_err or "engine.containment unavailable")
    snap = plans_snap["data"]
    plan = snap.get(trigger) if isinstance(snap, dict) else next((p for p in snap or [] if isinstance(p, dict) and p.get("case_id") == trigger), None)
    if isinstance(plan, dict) and plan.get("wave_id", wave_id) == wave_id:
        return {"plan": plan, "source": plans_snap["source"], "as_of": plans_snap["as_of"],
                "how": f"snapshot {plans_snap['generated_at']}: build_containment_plan({wave_id}) for {trigger}", "window": "recorded in the snapshot's plan", "notes": notes}
    notes.append(plans_snap["error"] or f"no snapshot plan for {trigger}")
    totals = cs["data"].get(trigger) if isinstance(cs["data"], dict) else None
    if isinstance(totals, dict) and totals.get("wave", wave_id) == wave_id:
        return {"totals": totals, "source": cs["source"], "as_of": cs["as_of"], "how": "containment summary totals", "window": "not recorded", "notes": notes}
    notes.append(cs["error"] or f"no containment summary for {trigger}")
    return {"notes": notes}


st.markdown("#### C7 · Containment planner")
st.caption(md(f"Proposal — awaiting approval. Nothing here has been executed or written to FraudGraph. {LOSS_SPLIT}."))
r1_wave = next((str(d.get("detector_id")) for d in DETECTORS if str(d.get("detector_id", "")).startswith("R0_") and ctx.case_id in (d.get("exam_cases_caught") or [])), None)
if ctx.case_id and not case_wave and r1_wave:
    st.markdown(md(f"{ctx.case_id} is caught by the Round 1 detector {r1_wave} (see Round 1 reference above); this planner covers the Final Round waves only, so no plan is shown for it."))
elif ctx.case_id and not case_wave:
    st.markdown(md(f"{ctx.case_id}: Not part of any detected wave — nothing to contain"))
plan_res: dict = {}
wave = None
if not WAVES:
    st.caption(md("No wave has a trigger case in the backtest's exam_scan, so there is nothing to plan."))
else:
    if st.session_state.get("_det_wave_case") != ctx.case_id or st.session_state.get("det_wave") not in [*WAVES, None]:
        st.session_state["det_wave"] = case_wave  # None for a case outside every wave: no plan until a wave is picked
        st.session_state["_det_wave_case"] = ctx.case_id
    wave = st.segmented_control("Wave", list(WAVES), key="det_wave", format_func=lambda w: md(f"{_short_id(w)} · {WAVES[w]}"),
                                help="Each wave is anchored to the exam case that triggered it in exam_scan.")
    if wave is None:
        st.caption("Pick a wave to see its trigger case's containment proposal.")
if wave:
    trigger = WAVES[wave]
    opened = common.case_meta(trigger).get("opened_at", "")
    plan_res = _plan_for(wave, trigger, opened)
    plan, totals = plan_res.get("plan"), plan_res.get("totals")
    as_of_txt = (plan or {}).get("as_of") or opened or "not recorded"
    member = f" · {ctx.case_id} is part of this wave" if ctx.case_id in EXAM_SCAN.get(wave, []) and ctx.case_id != trigger else ""
    st.markdown(md(f"{wave} · trigger {trigger} · as of {as_of_txt} · scan window {plan_res.get('window', 'not recorded')}{member}"))
    src = {"source": plan_res.get("source", ""), "as_of": plan_res.get("as_of", ""), "how": plan_res.get("how", "")}
    with st.container(border=True, key="det-plan"):
        if plan:
            per_card = [r for r in plan.get("per_card_plan") or [] if isinstance(r, dict)]
            block = [r for r in per_card if r.get("recommended_action") == "BLOCK_CARD"]
            l1 = sum(r.get("approval_route") == "L1" for r in block)
            broad = [str(c) for c in plan.get("legitimate_customers_touched_if_broad_rule_used") or []]
            touched = [str(c) for c in plan.get("legitimate_customers_touched_by_targeted_plan") or []]
            k = st.columns(3)
            common.measured(f"Block now (L1 {l1} / L2 {len(block) - l1})", len(block), container=k[0], **src,
                            what="proposal — awaiting approval: wave cards with a wave transaction at or before the alert")
            common.measured("Watched by rule", len(per_card) - len(block), container=k[1], **src,
                            what="proposal — awaiting approval: wave cards whose wave transactions all come after the alert (MONITOR_CONNECTED_CARDS)")
            common.measured("Lost before the alert", common.money(plan.get("exposure_already_settled_pre_asof_usd")), container=k[2], **src,
                            what="wave exposure already settled at or before the trigger case opened: not preventable")
            k = st.columns(3)
            common.measured("Preventable after the alert, if approved", common.money(plan.get("exposure_prevented_post_asof_usd")), container=k[0], **src,
                            what="proposal — awaiting approval: wave exposure after the trigger case opened")
            common.measured("Customers outside the wave a broad rule would also hit", len(broad), container=k[1], **src,
                            what=f"comparison: {plan.get('broad_rule_description') or 'broad rule'}")
            wave_custs = {str(c) for c in plan.get("customers_affected") or []}
            outside = sorted({str(r.get("customer_id")) for r in per_card} - wave_custs) if wave_custs else None
            common.measured("Customers outside the wave the targeted plan touches", None if outside is None else len(outside), container=k[2], **src,
                            what="customers of the per-card plan that are not wave customers (customers_affected); cleared customers inside the wave are named below")
            if broad:
                buf = io.StringIO()
                pd.DataFrame({"customer_id": broad}).to_csv(buf, index=False)
                k[1].download_button("Broad-rule customers (CSV)", buf.getvalue(), file_name=f"{_short_id(wave)}_broad_rule_customers.csv", mime="text/csv",
                                     on_click="ignore", icon=":material/download:", type="tertiary", key="det_broad_csv")
            k[2].caption(md("Cleared customers inside the wave the plan touches: " + (", ".join(touched) if touched else "none (measured)")))
            if touched:
                common.chip(f"touches cleared customer {', '.join(touched)}", "red", icon=":material/block:", where=k[2])
            backtest_legit = [c for d in DETECTORS if d.get("detector_id") == wave for c in d.get("legitimate_customers_acted_against") or []]
            if backtest_legit:
                in_plan = sorted(set(backtest_legit) & {str(c) for c in plan.get("customers_affected") or []})
                st.caption(md(f"The full-history backtest of {_short_id(wave)} matches cleared customer {', '.join(backtest_legit)}; "
                              + (f"this plan includes {', '.join(in_plan)}." if in_plan else "this plan's wave (as of the alert) does not include it.")))
            st.caption(md(f"Shared choke-point: {plan.get('shared_chokepoint', 'not recorded')} · broad rule: {plan.get('broad_rule_description', 'not recorded')}"))
            with st.popover("Attestation", icon=":material/verified:"):
                att = plan.get("attestation") or {}
                st.caption(md(f"What: {att.get('what', 'not recorded')}"))
                st.json({"evidence": att.get("evidence"), "how": att.get("how"), "when": att.get("when")}, expanded=True)
        elif totals:
            k = st.columns(3)
            common.measured("Cards in wave", totals.get("cards"), container=k[0], **src, what="proposal — awaiting approval: every card in the wave")
            common.measured("Lost before the alert", common.money(totals.get("pre_as_of")), container=k[1], **src,
                            what="wave exposure already settled at or before the trigger case opened")
            common.measured("Preventable after the alert, if approved", common.money(totals.get("post_as_of")), container=k[2], **src,
                            what="proposal — awaiting approval: wave exposure after the trigger case opened")
            k = st.columns(3)
            common.measured("Customers outside the wave a broad rule would also hit", totals.get("broad_rule_extra_customers"), container=k[0], **src,
                            what="comparison with the naive broad rule for this wave")
            common.measured("Cleared customers the targeted plan touches", totals.get("legit_touched_targeted"), container=k[1], **src,
                            what="wave customers whose wave transactions the bank cleared as legitimate")
            st.caption("Block now / watched split, replay frames and per-card plan not in this deployment (summary totals only).")
        else:
            st.caption(md("Containment plan not available here: " + "; ".join(n for n in plan_res.get("notes") or [] if n)))
        if plan_res.get("notes") and (plan or totals):
            st.caption(md("Fallback used: " + "; ".join(n for n in plan_res["notes"] if n)))


# --------------------------------------------------------------------------- #
# C5: ring replay                                                             #
# --------------------------------------------------------------------------- #
def _step(delta: int, n: int) -> None:
    st.session_state["det_frame"] = min(max(1, int(st.session_state.get("det_frame", 1)) + delta), n)


plan = plan_res.get("plan") if wave else None
if wave:
    st.markdown("#### C5 · Ring replay")
    frames = [f for f in (plan or {}).get("replay_frames") or [] if isinstance(f, dict)]
    if not frames:
        st.caption("Replay frames not in this deployment.")
    else:
        n = len(frames)
        alert = max((i for i, f in enumerate(frames, 1) if f.get("before_as_of")), default=1)
        if st.session_state.get("_det_frame_wave") != wave or not 1 <= int(st.session_state.get("det_frame") or 0) <= n:
            st.session_state["det_frame"] = alert
            st.session_state["_det_frame_wave"] = wave
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            st.button("Prev", icon=":material/chevron_left:", key="det_prev", on_click=_step, args=(-1, n))
            st.button("Next", icon=":material/chevron_right:", key="det_next", on_click=_step, args=(1, n))
            st.slider("Frame", 1, n, key="det_frame", help=f"Opens on frame {alert}, the last transaction at or before the alert.")
        idx = int(st.session_state["det_frame"])
        f = frames[idx - 1]
        phase = "before the alert" if f.get("before_as_of") else "after the alert (hindsight)"
        st.markdown(md(f"Frame {idx}/{n} · {f.get('ts')} · txn {f.get('txn_id')} on {f.get('card_id')} ({common.money(f.get('amt_usd'))}) · "
                       f"{f.get('cumulative_cards')}/{plan.get('n_cards_in_wave')} cards active · cumulative {common.money(f.get('cumulative_exposure_usd'))} · {phase}"))
        df = pd.DataFrame([{"ts": fr.get("ts"), "frame": fr.get("frame"), "card": fr.get("card_id"), "amount": fr.get("amt_usd"),
                            "cumulative": fr.get("cumulative_exposure_usd"),
                            "phase": "before the alert" if fr.get("before_as_of") else "hindsight (after the alert)"} for fr in frames])
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
        at = pd.to_datetime(plan.get("as_of"), errors="coerce")
        base = alt.Chart(df).encode(x=alt.X("ts:T", title=None), y=alt.Y("cumulative:Q", title="Cumulative wave exposure ($)", axis=alt.Axis(format="$,.0f")))
        layers = [base.mark_line(interpolate="step-after"),
                  base.mark_point(filled=True, size=40).encode(color=alt.Color("phase:N", title=None, legend=alt.Legend(orient="bottom")),
                                                               tooltip=["frame", "ts:T", "card", alt.Tooltip("amount:Q", format="$,.2f"),
                                                                        alt.Tooltip("cumulative:Q", format="$,.2f"), "phase"])]
        if pd.notna(at) and df["ts"].notna().any():
            end = max(df["ts"].max(), at)
            layers.insert(0, alt.Chart(pd.DataFrame([{"start": at, "end": end, "label": "hindsight"}])).mark_rect(opacity=0.10).encode(x="start:T", x2="end:T"))
            layers.append(alt.Chart(pd.DataFrame([{"at": at}])).mark_rule(strokeDash=[4, 4]).encode(x="at:T", tooltip=[alt.Tooltip("at:T", title="as of (alert)")]))
        cur = df.iloc[[idx - 1]]
        layers.append(alt.Chart(cur).mark_rule(strokeWidth=2).encode(x="ts:T"))
        st.altair_chart(alt.layer(*layers).properties(height=260), width="stretch", key="det_replay_chart")
        st.caption(md(f"Dashed rule: the alert ({plan.get('as_of')}). Shaded: hindsight, transactions after the alert. Solid rule: frame {idx}."))
    if plan:
        per_card = [r for r in plan.get("per_card_plan") or [] if isinstance(r, dict)]
        if per_card:
            opts = sorted({f"{r.get('recommended_action')} · {r.get('approval_route')}" for r in per_card})
            st.dataframe(
                pd.DataFrame([{"Card": r.get("card_id"), "Customer": r.get("customer_id"),
                               "Proposed action": [f"{r.get('recommended_action')} · {r.get('approval_route')}"],
                               "Exposure": r.get("wave_exposure_usd"), "Txns": r.get("n_wave_txns"), "First": r.get("first_wave_ts"), "Last": r.get("last_wave_ts"),
                               "Cleared customer": bool(r.get("is_legitimate_customer_harmed"))} for r in per_card]),
                hide_index=True, key="det_per_card", height=35 * min(len(per_card), 12) + 38,
                column_config={
                    "Proposed action": st.column_config.MultiselectColumn(
                        "Proposed action · route", options=opts, color=[common.action_color(*o.split(" · ", 1)) for o in opts],
                        help="proposal — awaiting approval: BLOCK_CARD for a card with a wave transaction at or before the alert, else watched by rule"),
                    "Exposure": st.column_config.NumberColumn("Wave exposure", format="dollar"),
                    "Cleared customer": st.column_config.CheckboxColumn("Cleared customer", help="the customer has a wave transaction the bank cleared as legitimate"),
                })
            with st.popover("Transaction ids per card", icon=":material/receipt_long:"):
                st.dataframe([{"card": r.get("card_id"), "txn_ids": ", ".join(str(t) for t in r.get("txn_ids") or [])} for r in per_card], hide_index=True)


# --------------------------------------------------------------------------- #
# C2: self-growing proposals                                                  #
# --------------------------------------------------------------------------- #
head = st.container(horizontal=True, vertical_alignment="center", gap="small")
head.markdown("#### C2 · Self-growing proposals")
common.chip("partial", "orange", where=head, help="proposals are found and backtested; none is admitted until a fraud manager approves it")
st.caption(md(f"Decisions: {APPROVALS_NOTE or common.rel(APPROVALS_FILE) + ' · ' + common.as_of(APPROVALS_FILE)}"))
session = ctx.session
shapes = {(d.get("cards_matched"), d.get("txns_matched")): _short_id(d.get("detector_id")) for d in DETECTORS}


def _gate_reason() -> str | None:
    """Why Approve / Reject is disabled, or None when this session may decide."""
    if ctx.read_only:
        return "read-only deployment (DEPLOY_READONLY=1)"
    if det_mod is None or not hasattr(det_mod, "approve_detector"):
        return f"engine.detectors.approve_detector unavailable: {det_err or 'not defined'}"
    if session is None:
        return "sign in as a fraud manager (identity menu in the top bar)"
    if session.role != "fraud_manager":
        return f"fraud_manager only; signed in as {session.principal} ({session.role})"
    return None


@st.dialog("Confirm detector decision")
def _decide(pid: str, decision: str) -> None:
    st.markdown(f"**{md(pid)}** → **{md(decision)}** as {md(session.principal if session else 'signed out')}")
    st.info("Final: recorded in data/detector_approvals.json and in the approval audit log (route 'gate').")
    if st.button("Confirm", type="primary", key="det_dlg_confirm"):
        actor, role = (session.principal, session.role) if session else ("", "unauthenticated")
        msgs = st.session_state.setdefault("det_gate_msg", {})
        try:
            rec = det_mod.approve_detector(pid, session, decision)
        except (PermissionError, OSError, ValueError) as exc:
            approvals_db.audit_gate(pid, decision, actor, role, "denied_rbac" if isinstance(exc, PermissionError) else "error", str(exc), common.approvals_db_path())
            msgs[pid] = {"ok": False, "text": f"Refused: {exc}"}
        else:
            approvals_db.audit_gate(pid, decision, actor, role, "ok", f"{decision} via the Detectors page", common.approvals_db_path())
            msgs[pid] = {"ok": True, "text": f"{decision}: {rec}"}
        st.rerun()


reason = _gate_reason()
if not PROPOSALS:
    st.caption(md("No self-growing proposals in the backtest." if bt["data"] is not None else f"Not available here: {bt['error']}"))
cols = st.columns(max(1, min(3, len(PROPOSALS))))
for i, p in enumerate(PROPOSALS):
    pid = str(p.get("proposal_id"))
    status = _status(p)
    with cols[i % len(cols)], st.container(border=True, key=f"det-prop-{_slug(pid)}"):
        st.markdown(f"**{md(pid)}** · {md(p.get('name'))} " + common.badge(status, {"approved": "green", "rejected": "gray"}.get(status, "orange")))
        st.caption(md(f"discovered by {p.get('discovered_by')}"))
        same = shapes.get((p.get("cards_matched"), p.get("txns_matched")))
        if same:
            st.caption(md(f"replays the {same} wave query over the full history: same {p.get('cards_matched')} cards and {p.get('txns_matched')} transactions"))
        legit = [str(c) for c in p.get("legitimate_customers_acted_against") or []]
        st.markdown(md(f"{p.get('cards_matched')} cards · {p.get('txns_matched')} txns · exposure {common.money(p.get('total_exposure_usd'))} · "
                       f"cleared customers hit: {', '.join(legit) if legit else 'none'} ({p.get('closed_case_false_positives', 0)} cleared closed cases)"),
                    help=md(f"Source: {BT_SRC['source']} · as of {BT_SRC['as_of']}"))
        rec = APPROVALS.get(pid)
        if isinstance(rec, dict):
            st.caption(md(f"{rec.get('status')} by {rec.get('by')} ({rec.get('role')}) at {rec.get('at')}"))
        with st.expander("Synthesized SQL"):
            st.code(str(p.get("synthesized_sql") or "not recorded"), language="sql", wrap_lines=True)
        msg = (st.session_state.get("det_gate_msg") or {}).get(pid)
        if msg:
            (st.success if msg["ok"] else st.error)(md(msg["text"]))
        why = reason or (f"already {status}" if status != "pending" else None)
        b1, b2 = st.columns(2)
        if b1.button("Approve", key=f"det_approve_{pid}", icon=":material/check:", disabled=why is not None, help=md(why) if why else None, width="stretch"):
            _decide(pid, "approved")
        if b2.button("Reject", key=f"det_reject_{pid}", icon=":material/close:", disabled=why is not None, help=md(why) if why else None, width="stretch"):
            _decide(pid, "rejected")
        if why:
            st.caption(md(f"Disabled: {why}"))


# --------------------------------------------------------------------------- #
# C3: policy text -> detector                                                 #
# --------------------------------------------------------------------------- #
with st.container(border=True, key="det-c3"):
    head = st.container(horizontal=True, vertical_alignment="center", gap="small")
    head.markdown("**C3 · Policy text → detector**")
    common.chip("not attempted" if c3_value == "not built" else c3_value or "not measured here", "gray" if c3_value != "built" else "green", where=head)
    st.markdown(md(c3_detail), help=md(f"Source: {c3_src} · as of {c3_as_of}"))


# --------------------------------------------------------------------------- #
# Operator: re-run the backtest (never on view)                               #
# --------------------------------------------------------------------------- #
with st.expander("Operator · re-run the backtest", expanded=False):
    st.caption(md(f"Loading this page never recomputes: it reads {BT_SRC['source']} (written {BT_SRC['as_of']}). "
                  "Re-running rewrites that file from the read-only DuckDB."))
    bt_mod, bt_err = (None, "read-only deployment") if ctx.read_only else common.optional_import("engine.backtest_detectors")
    if st.button("Re-run backtest", icon=":material/replay:", key="det_rerun", disabled=bt_mod is None, help=md(bt_err) if bt_err else None):
        with st.status("Running engine.backtest_detectors.run_backtest()…", expanded=True) as status_box:
            try:
                bt_mod.run_backtest()
            except Exception as exc:  # noqa: BLE001 - shown verbatim, the old file stays
                status_box.update(label=md(f"Backtest failed: {type(exc).__name__}: {exc}"), state="error")
            else:
                _engine_plan.clear()
                status_box.update(label="Backtest written", state="complete")
                st.rerun()
    if bt_mod is None:
        st.caption(md(f"Disabled: {bt_err}"))

"""Overview, the default page: which source is on screen and how it was produced, four measured tiles, the proof strip,
the decision board (one click opens a case), a live-run strip while cases are running or broken, the C1-C14 challenge
index, the top known limits and a provenance footer.

Files only: it reads answers, traces, cases/, engine/out/*.json, data/schema_approvals.json and the approvals database
(read-only). This file imports no engine, agent, schema_engine or duckdb code and writes nothing.
"""
from __future__ import annotations

import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

ctx = common.page_setup("overview")
src = ctx.source
sid = ctx.source_id or ""
md = common.md

if src is None:
    common.page_header("Agentic fraud investigator on TigerGraph", eyebrow="Overview")
    st.info("No sources on this deployment: no agent run under runs/ and no answer files under cases/.")
    st.stop()

FINAL = src.round != "round1"
REF_ID = "submitted" if FINAL else "submitted-r1"  # the submitted answers of the same round
REF = common.get_source(REF_ID) if sid != REF_ID else None
ORDER = [r["case_id"] for r in common.pack_for_source(src)]  # the pack is in opened_at order
ORDER += [c for c in src.cases if c not in ORDER]
views = {cid: common.case_view(sid, cid) for cid in ORDER}
refs = {cid: common.case_view(REF_ID, cid) for cid in ORDER} if REF else {}
answers = {cid: v.answer for cid, v in views.items() if v.ok}
inbox, inbox_note = common.inbox_rows()
N = len(ORDER)


def _answer_file(cid: str) -> Path:
    return common.cases_dir() / f"{cid}.json" if src.kind == "submitted" else src.dir / cid / "answer.json"


def _newest(paths) -> str:
    stamps = [s for s in (common.as_of(p) for p in paths) if s != "missing"]
    return max(stamps) if stamps else "missing"


ANSWERS_SRC = f"cases/{common.SUBMITTED[sid][0]}*.json" if src.kind == "submitted" else f"runs/{sid}/<case>/answer.json"
ANSWERS_AS_OF = _newest(_answer_file(c) for c in ORDER)
TRACE_SRC = f"runs/{sid}/<case>/calls.jsonl and phases.jsonl"
TRACE_AS_OF = "missing" if src.kind == "submitted" else _newest(src.dir / c / "calls.jsonl" for c in ORDER)
REF_GLOB = f"{common.SUBMITTED[REF_ID][0]}*.json"
REF_SRC = f"cases/{REF_GLOB}"


def _id_range(ids: list[str]) -> str:
    ids = sorted(ids)
    return "" if not ids else ids[0] if len(ids) == 1 else f"{ids[0]}..{ids[-1].rsplit('-', 1)[-1]}"


def _float(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _nearest_rank(values: list[float], q: float) -> float | None:
    """The value at rank ceil(q*n) of the sorted values (the Runs page rule)."""
    vals = sorted(v for v in values if v is not None)
    return vals[max(1, math.ceil(q * len(vals))) - 1] if vals else None


# --------------------------------------------------------------------------- #
# Agreement with the submitted answers                                        #
# --------------------------------------------------------------------------- #
agree = {cid: common.agreement(views[cid].answer, refs[cid].answer) for cid in ORDER if views[cid].ok and refs.get(cid) and refs[cid].ok}


def _vs_submitted(cid: str) -> str:
    if src.kind == "submitted":
        runs = views[cid].promoted_from
        return f"promoted from {', '.join(runs)}" if runs else "no identical run answer"
    a = agree.get(cid)
    if a is None:
        return "—" if not views[cid].ok else "no submitted answer"
    if a["identical"] or a["same_decision"]:
        return a["cls"]
    fields = list(dict.fromkeys(d.split(":", 1)[0] for d in a["diffs"]))
    return "differs: " + ", ".join(fields)


def _diff_brief(a: dict, b: dict) -> str:
    """'verdict uncertain → legitimate', or the actions only one side recommends ('+X' here, '−Y' only in the submitted answer)."""
    va, vb = (a.get("case") or {}).get("verdict"), (b.get("case") or {}).get("verdict")
    if va != vb:
        return f"verdict {va} → {vb}"
    acts = [{f"{x.get('action')} · {x.get('route', 'auto')}" for x in (ans.get("next_best_actions") or {}).get("final") or [] if isinstance(x, dict)} for ans in (a, b)]
    moved = [f"+{x}" for x in sorted(acts[0] - acts[1])] + [f"−{x}" for x in sorted(acts[1] - acts[0])]
    return ", ".join(moved) or ("SAR" if bool((a.get("sar") or {}).get("file")) != bool((b.get("sar") or {}).get("file")) else "decision")


# --------------------------------------------------------------------------- #
# Header                                                                      #
# --------------------------------------------------------------------------- #
def _subtitle() -> str:
    if src.kind == "submitted":
        equal = Counter(r for rs in src.promoted_from.values() for r in rs)
        tail = " · ".join(f"identical to {r} {n}/{src.n_total}" for r, n in equal.most_common(1)) or "not identical to any run on disk"
        return f"Submitted answers {ANSWERS_SRC} · {src.n_answered}/{src.n_total} readable · {tail}"
    model = ", ".join(src.models_seen) or src.manifest_model or "model unknown"
    bits = [model] + ([f"git {src.git_sha[:7]}"] if src.git_sha else []) + (["uncommitted changes"] if src.git_dirty else [])
    head = f"Live run {sid} (FraudGraph + {', '.join(bits)})" if src.mode == "live" else f"Mock run {sid} ({', '.join(bits)})"
    out = f"{head} · {src.n_answered}/{src.n_total} answered"
    if REF and refs:
        n_ref = sum(1 for v in refs.values() if v.ok)
        n_ident = sum(1 for a in agree.values() if a["identical"])
        out += (f" · the submitted answers in {REF_SRC} are identical copies ({n_ident}/{n_ref})" if n_ref and n_ident == n_ref
                else f" · {n_ident}/{n_ref} identical to the submitted answers in {REF_SRC}")
    return out


if FINAL:
    eyebrow = f"Final Round · HHGOA 2026 · {N} cases ({_id_range(ORDER)})"
else:
    eyebrow = f"Round 1 archive · {N} cases"
_title, _credit = st.columns([6, 1], vertical_alignment="center")
with _title:
    common.page_header("Agentic fraud investigator on TigerGraph", _subtitle(), eyebrow=eyebrow)
with _credit:
    common.tigergraph_credit("overview", width=128, align="right")
if not FINAL:
    final_default = common.default_source()
    final_src = common.get_source(final_default)
    st.info(md(f"Round 1 archive: {sid} answers the {N} Round 1 cases ({_id_range(ORDER)}). The Final Round answers are in "
               + (f"{final_default}." if final_src is not None and final_src.round != "round1" else "no source on this deployment.")),
            icon=":material/history:")
    if final_src is not None and final_src.round != "round1" and st.button("Back to Final Round", icon=":material/arrow_back:", key="ov_back"):
        common.goto("overview", run=final_default)

# --------------------------------------------------------------------------- #
# Tiles                                                                       #
# --------------------------------------------------------------------------- #
states = {cid: v.state for cid, v in views.items()}
n_done = sum(1 for s in states.values() if s == "done")
saf = common.safety(answers)
mix = saf["verdict_mix"]
t1, t2, t3, t4 = st.columns(4)

not_done = {s: [c for c, x in states.items() if x == s] for s in ("running", "incomplete", "failed", "unreadable", "not_run")}
common.measured(
    "Decided", f"{n_done}/{N}", source=ANSWERS_SRC, as_of=ANSWERS_AS_OF, container=t1,
    what="cases whose answer.json parses and carries case.verdict",
    how="common.case_view state per case; " + "; ".join(f"{common.STATE_LABELS[s]}: {', '.join(ids) or 'none'}" for s, ids in not_done.items()),
)
if any(not_done.values()):
    t1.caption(md(" · ".join(f"{len(ids)} {common.STATE_LABELS[s]}" for s, ids in not_done.items() if ids)))

common.measured(
    "Verdicts: fraud · legitimate · uncertain", f"{mix.get('fraud', 0)} · {mix.get('legitimate', 0)} · {mix.get('uncertain', 0)}" if n_done else None,
    source=ANSWERS_SRC, as_of=ANSWERS_AS_OF, container=t2, what="case.verdict over the decided cases", how="common.safety verdict_mix",
)
if n_done:
    parts_v = [(mix.get(k, 0), cls, lbl) for k, cls, lbl in (("fraud", "ov-fraud", "fraud"), ("legitimate", "ov-legit", "legitimate"), ("uncertain", "ov-unc", "uncertain"))]
    bar = "".join(f'<span class="{cls}" style="flex:{n}"></span>' for n, cls, _ in parts_v if n)
    key = "".join(f'<span><i class="{cls}"></i>{n} {lbl}</span>' for n, cls, lbl in parts_v)
    t2.markdown(f'<div class="ov-vbar">{bar}</div><div class="ov-vkey">{key}</div>', unsafe_allow_html=True)
if FINAL:
    t2.caption(md(f"Brief: exactly {common.BRIEF_LEGITIMATE} legitimate — not graded here"))

ref_saf = common.safety({c: v.answer for c, v in refs.items() if v.ok}) if refs else None
non_fraud = {c for c, a in answers.items() if (a.get("case") or {}).get("verdict") in ("legitimate", "uncertain")}
inbox_hard = sorted({r["case_id"] for r in inbox if r.get("case_id") in non_fraud and r.get("action") in common.HARD_ACTIONS and r.get("status") != "rejected"})
hard = saf["hard_on_non_fraud"]
common.measured(
    "Hard actions on non-fraud verdicts", len(hard) if n_done else None, source=ANSWERS_SRC, as_of=ANSWERS_AS_OF, container=t3,
    what="legitimate or uncertain cases whose final actions include BLOCK_CARD, BLOCK_ALL_CARDS or FILE_REPORT, or that file a SAR",
    how="common.safety hard_on_non_fraud; the inbox count reads the approvals database read-only",
)
if hard:
    common.chip("; ".join(f"{c}: {', '.join(acts)}" for c, acts in hard), "red", icon=":material/block:", where=t3)
parts = [f"uncertain escalated {saf['uncertain_escalated']}/{saf['n_uncertain']}"]
if ref_saf is not None:
    parts.append(f"submitted {len(ref_saf['hard_on_non_fraud'])}")
parts.append(f"inbox {len(inbox_hard)}" if not inbox_note else "no inbox on this deployment")
t3.caption(md(" · ".join(parts)))
if inbox_hard:
    common.chip(f"inbox holds hard actions for {', '.join(inbox_hard)}", "red", where=t3)

if src.kind == "submitted":
    equal = Counter(r for rs in src.promoted_from.values() for r in rs)
    top = equal.most_common(1)
    common.measured("Same as a run", f"promoted from {top[0][0]} {top[0][1]}/{src.n_total}" if top else None, source=f"{ANSWERS_SRC} vs runs/*/<case>/answer.json",
                    as_of=ANSWERS_AS_OF, container=t4, what="submitted answers whose parsed JSON equals a run's answer.json", how="common.promoted_from")
    not_identical = sorted(c for c, rs in src.promoted_from.items() if not rs)
    if not_identical:
        common.chip(f"not identical to any run: {', '.join(not_identical)}", "orange", where=t4)
elif REF is None:
    common.measured("Same as submitted", None, source=f"{REF_SRC} (absent)", as_of="missing", container=t4)
else:
    n_cmp = len(agree)
    n_ident = sum(1 for a in agree.values() if a["identical"])
    n_same = sum(1 for a in agree.values() if a["same_decision"])
    differs = [c for c, a in agree.items() if not a["same_decision"]]
    value = None if not n_cmp else f"{n_ident}/{n_cmp} identical" if n_ident == n_cmp else f"{n_same}/{n_cmp} same decision"
    common.measured("Same as submitted", value, source=f"{ANSWERS_SRC} vs {REF_SRC}", as_of=max(ANSWERS_AS_OF, _newest(common.cases_dir().glob(REF_GLOB))),
                    container=t4, what="decided cases compared with the submitted answer of the same case",
                    how="common.agreement: identical = parsed JSON equal; same decision = verdict, final (action, route) set and sar.file")
    if differs:
        hard_diff = any(agree[c]["cls"] == "differs · hard action" for c in differs)
        common.chip("differs: " + ", ".join(f"{c} ({_diff_brief(views[c].answer, refs[c].answer)})" for c in differs), "red" if hard_diff else "orange",
                    help="  \n".join(f"{c}: {'; '.join(agree[c]['diffs'])}" for c in differs), where=t4)
    elif n_cmp and n_ident < n_cmp:
        t4.caption(md(f"{n_ident}/{n_cmp} byte-identical; the rest differ only outside the decision"))

# --------------------------------------------------------------------------- #
# Proof strip                                                                 #
# --------------------------------------------------------------------------- #
bt = common.proof("backtest")
bt_data = bt["data"] if isinstance(bt["data"], dict) else {}
cs = common.proof("containment_summary")
attacks = common.proof("attacks")
dq = common.proof("dq")
prefix = "HHF-" if FINAL else "HHG-"
traced = {c: v for c, v in views.items() if v.ok and v.has_trace}
usage = {c: common.tg_usage(v) for c, v in traced.items()}
stats = {c: common.trace_stats(v) for c, v in views.items() if v.ok}


def _attacks_value():
    d = attacks["data"] if isinstance(attacks["data"], dict) else None
    return None if d is None else f"{d.get('attacks_blocked')}/{d.get('total_attacks')}"


def _detector_value():
    scan = bt_data.get("exam_scan") or {}
    if not scan:
        return None
    dets = sorted(str(k).split("_")[0] for k in scan)
    cases = sorted({c for cs_ in scan.values() for c in cs_})
    hits = [(str(d.get("detector_id", "")).split("_")[0], c) for d in bt_data.get("detectors") or [] for c in d.get("legitimate_customers_acted_against") or []]
    span = f"{dets[0]}–{dets[-1]}" if len(dets) > 1 else dets[0]
    return f"{span} fire on {_id_range(cases)} · {len(hits)} cleared customer hit" + ("s" if len(hits) != 1 else "") + (f" ({', '.join(f'{d} → {c}' for d, c in hits)})" if hits else "")


def _waves() -> dict:
    data = cs["data"] if isinstance(cs["data"], dict) else {}
    return {k: v for k, v in data.items() if str(k).startswith(prefix) and isinstance(v, dict)}


def _containment_value():
    waves = _waves()
    if not waves:
        return None
    outside = sum(int(v.get("legit_touched_targeted") or 0) for v in waves.values())
    return f"{outside} customers outside the wave · {len(waves)} waves"


def _vector_value():
    if not usage:
        return None
    with_hits = sum(1 for u in usage.values() if u["vector_hits"])
    readbacks = sum(len(u["agent_readbacks"]) for u in usage.values())
    return f"{with_hits}/{len(usage)} traces · {readbacks} agent-case read-backs"


def _writeback_value():
    if not traced:
        return None
    ok = sum(1 for c in traced if stats[c]["writes"] and stats[c]["writes_ok"] == stats[c]["writes"])
    return f"ok {ok}/{len(traced)} (persist rows)"


ds = approvals_db.directory_status()
pending = [r for r in inbox if r.get("status") == "pending" and str(r.get("case_id", "")).startswith(prefix)]
pend_txt = f"{len(pending)} pending" if not inbox_note else "no inbox on this deployment"
approvers = f"Approvers 0 → approvals fail closed · {pend_txt}" if not ds["n"] else f"Approvers {ds['n']} ({', '.join(f'{r} {n}' for r, n in ds['roles'].items())}) · {pend_txt}"
readback_case = next((c for c, u in usage.items() if u["agent_readbacks"]), None)
no_trace = "no trace in this source" if src.kind == "submitted" else TRACE_SRC

st.markdown('<div class="ov-section">Proof <b>measured from files, click through to the evidence</b></div>', unsafe_allow_html=True)
with st.container(horizontal=True, key="ov-proofs"):
    common.proof_chip("Attacks blocked", _attacks_value(), source=attacks["source"] or attacks["error"], as_of=attacks["as_of"] or "missing", page_key="security")
    common.proof_chip("Detectors on the exam cases", _detector_value(), source=bt["source"] or bt["error"], as_of=bt["as_of"] or "missing", page_key="detectors")
    common.proof_chip("Targeted containment", _containment_value(), source=cs["source"] or cs["error"], as_of=cs["as_of"] or "missing", page_key="detectors")
    common.proof_chip("Vector search", _vector_value(), source=no_trace if not usage else f"{TRACE_SRC} (P1 similar_prior_cases)", as_of=TRACE_AS_OF,
                      page_key="case", case=readback_case)
    common.proof_chip("Write-back", _writeback_value(), source=no_trace if not traced else f"runs/{sid}/<case>/calls.jsonl (caller persist)", as_of=TRACE_AS_OF,
                      page_key="graph")
    common.proof_chip("Approvals", approvers, source=f"{common.rel(ds['path'])} and {common.rel(common.approvals_db_path())}",
                      as_of=f"{common.as_of(ds['path'])} / {common.as_of(common.approvals_db_path())}", page_key="approvals")

# --------------------------------------------------------------------------- #
# Decision board                                                              #
# --------------------------------------------------------------------------- #
FLAG_TYPES = {"security_alert": "injection stripped", "structured_retry": "retry", "clamp": "clamp", "policy_reprompt": "re-prompt",
              "failed_call": "failed call", "dropped_ids": "dropped ids", "sar_fallback": "SAR fallback"}
FLAG_COLORS = {"injection stripped": "orange", "retry": "orange", "clamp": "orange", "re-prompt": "orange", "failed call": "red",
               "dropped ids": "gray", "SAR fallback": "orange"}
LANES = {"all": "All", "contain": "Contain (fraud)", "analyst": "Handed to analyst (uncertain)", "closed": "Closed by agent (legitimate)", "not_done": "Not done"}
LANE_OF = {"fraud": "contain", "uncertain": "analyst", "legitimate": "closed"}
waves_by_case = {c: str(k).split("_")[0] for k, cs_ in (bt_data.get("exam_scan") or {}).items() for c in cs_}
dq_data = dq["data"] if isinstance(dq["data"], dict) else {}
dq_by_case = {c: sorted({str(t.get("code")) for t in ts if isinstance(t, dict)}) for c, ts in (dq_data.get("taint_matrix") or {}).items()}


def _trigger(meta: dict) -> str:
    kind = str(meta.get("trigger_type") or "")
    score = common.prob(meta.get("risk_score")) if kind == "risk_score" else ""
    return f"{kind} {score}".strip() if score != "—" else kind or "—"


def _final_actions(ans: dict) -> list[str]:
    return [f"{a.get('action')} · {a.get('route', 'auto')}" for a in (ans.get("next_best_actions") or {}).get("final") or []
            if isinstance(a, dict) and a.get("action") != "CREATE_CASE"]


def _evidence_request(ans: dict) -> str:
    out = []
    for r in ans.get("evidence_requests") or []:
        if not isinstance(r, dict):
            continue
        reply = str(r.get("assumed_response") or "")
        simulated = "simulated" in reply.lower()
        reply = re.sub(r"^\s*ASSUMED\s*\(simulated\)\s*:\s*", "", reply, flags=re.I) or "no reply recorded"
        out.append(f"{r.get('type', '?')} → {reply}" + (" (simulated)" if simulated else ""))
    return "; ".join(out) or "—"


def _approvals(cid: str, ans: dict) -> str:
    gated = {str(a.get("action")) for a in (ans.get("next_best_actions") or {}).get("final") or [] if isinstance(a, dict) and a.get("route") in ("L1", "L2")}
    rows = [r for r in inbox if r.get("case_id") == cid]
    if not gated and not rows:
        return "—"
    live = [r for r in rows if r.get("action") in gated]
    parts = []
    pend = [r for r in live if r.get("status") == "pending"]
    if pend:
        parts.append(f"{len(pend)} pending · {', '.join(sorted({str(r.get('route')) for r in pend}))}")
    parts += [f"{r.get('status')} by {r.get('decided_by')}" for r in live if r.get("status") in ("approved", "rejected")]
    unqueued = gated - {str(r.get("action")) for r in live}
    if unqueued:
        parts.append(f"{len(unqueued)} not queued" if parts else "not queued")
    superseded = [r for r in rows if r.get("action") not in gated]
    if superseded:
        parts.append(f"superseded {len(superseded)}")
    return " · ".join(parts)


def _flags(view) -> list[str]:
    seen = [FLAG_TYPES[e["type"]] for e in common.guardrail_events(view) if e["type"] in FLAG_TYPES]
    extra = dq_by_case.get(view.case_id, []) + ([f"{waves_by_case[view.case_id]} wave"] if view.case_id in waves_by_case else [])
    return list(dict.fromkeys(seen)) + extra


def _run_state(view) -> str:
    if view.state == "running":
        t = max((_float(r.get("t")) or 0.0 for r in view.phases), default=0.0)
        return f"running · {view.last_phase or 'P0'} · {t + (view.last_write_age_s or 0):.0f} s"
    if view.state == "incomplete":
        return f"incomplete · last {view.last_phase or 'no phase'}"
    return common.STATE_LABELS.get(view.state, view.state)


rows = []
for cid in ORDER:
    v = views[cid]
    a = v.answer if v.ok else {}
    verdict = str((a.get("case") or {}).get("verdict") or "") if v.ok else ""
    rows.append({
        "Case": cid, "_lane": LANE_OF.get(verdict, "not_done") if v.ok else "not_done", "_trigger": str(v.meta.get("trigger_type") or "unknown"),
        "Trigger": _trigger(v.meta), "Verdict": [verdict] if verdict else [],
        "p(fraud)": _float((a.get("case") or {}).get("fraud_probability")) if v.ok else None,
        "Final actions": _final_actions(a) if v.ok else [], "SAR": ("filed" if (a.get("sar") or {}).get("file") else "—") if v.ok else "—",
        "Evidence request": _evidence_request(a) if v.ok else "—", "Approvals": _approvals(cid, a) if v.ok else "—",
        "Flags": _flags(v), "Run state": _run_state(v), "vs submitted": _vs_submitted(cid),
    })

st.markdown(f'<div class="ov-section">Decision board <b>{N} cases · one click opens the case file</b></div>', unsafe_allow_html=True)
lane_counts = Counter(r["_lane"] for r in rows)
trigger_counts = Counter(r["_trigger"] for r in rows)
with st.container(horizontal=True, vertical_alignment="bottom", key="ov-controls"):
    lane = st.segmented_control(
        "Lane", list(LANES), default="all", key="ov_lane",
        format_func=lambda k: f"{LANES[k]} · {N if k == 'all' else lane_counts.get(k, 0)}",
        help="Contain = fraud · Handed to analyst = uncertain · Closed by agent = legitimate · Not done = state other than done",
    )
    picked = st.pills("Trigger", [t for t, _ in trigger_counts.most_common()], selection_mode="multi", key="ov_trigger",
                      format_func=lambda t: f"{md(t)} · {trigger_counts[t]}")
    queue_order = st.toggle("Queue order (opened_at)", key="ov_order", help="Off: case-id order")
    all_cols = st.toggle("All columns", key="ov_cols", help="Off: the decision at a glance (case, verdict, p(fraud), actions, approvals, flags)")

shown = [r for r in rows if (not lane or lane == "all" or r["_lane"] == lane) and (not picked or r["_trigger"] in picked)]
if not queue_order:
    shown = sorted(shown, key=lambda r: r["Case"])

act_opts = sorted({x for r in rows for x in r["Final actions"]})
flag_opts = sorted({x for r in rows for x in r["Flags"]})


def _action_color(opt: str) -> str:
    action, _, route = opt.partition(" · ")
    return common.action_color(action, route)


board = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in shown],
                     columns=["Case", "Trigger", "Verdict", "p(fraud)", "Final actions", "SAR", "Evidence request", "Approvals", "Flags", "Run state", "vs submitted"])
st.dataframe(
    board, key="board", hide_index=True, height=35 * len(shown) + 38,
    column_order=None if all_cols else ["Case", "Verdict", "p(fraud)", "Trigger", "Final actions", "Approvals", "Flags"],
    column_config={
        "Case": st.column_config.ButtonColumn("Case", type="tertiary", pinned=True, key="board_open", help="Open the Case file"),
        "Verdict": st.column_config.MultiselectColumn("Verdict", options=list(common.VERDICT_COLORS), color=list(common.VERDICT_COLORS.values())),
        "p(fraud)": st.column_config.ProgressColumn("p(fraud)", min_value=0.0, max_value=1.0, format="%.2f"),
        "Final actions": st.column_config.MultiselectColumn(
            "Final actions", options=act_opts or None, color=[_action_color(o) for o in act_opts] or None,
            help="<ACTION> · <route>: red = irreversible for an innocent customer, violet = waits for L1/L2, gray = auto; CREATE_CASE left out"),
        "Flags": st.column_config.MultiselectColumn("Flags", options=flag_opts or None, color=[FLAG_COLORS.get(o, "gray") for o in flag_opts] or None,
                                                    help="guardrail notes from phases.jsonl, failed calls, DQ taints and detector waves"),
        "Evidence request": st.column_config.TextColumn("Evidence request", width="medium"),
        "Trigger": st.column_config.TextColumn("Trigger", width="small"),
        "Approvals": st.column_config.TextColumn("Approvals", width="medium"),
        "vs submitted": st.column_config.TextColumn("vs submitted", help=f"common.agreement against {REF_SRC}" if REF else "no submitted answers to compare"),
    },
)
click = st.session_state.get("board_open")
if click:
    common.goto("case", case=click["label"])
st.caption(md(
    f"Rows: {ANSWERS_SRC}" + (f", {TRACE_SRC}" if src.kind != "submitted" else "")
    + (f" · vs submitted: {REF_SRC}" if REF else "") + f" · approvals: {inbox_note or common.rel(common.approvals_db_path()) + ' (read-only)'}"
    + (f" · DQ: {dq['source']}" if dq_by_case else " · DQ flags: not measured here")
    + f" · {len(shown)} of {N} shown"
))


# --------------------------------------------------------------------------- #
# Live-run strip                                                              #
# --------------------------------------------------------------------------- #
@st.fragment(run_every="5s")
def _live_strip(source_id: str, ids: list[str]) -> None:
    now = {cid: common.case_view(source_id, cid) for cid in ids}
    by_state = {s: [v for v in now.values() if v.state == s] for s in ("running", "incomplete", "failed", "unreadable", "not_run")}
    with st.container(border=True, key="live-strip"):
        st.markdown("**Live run**")
        with st.container(horizontal=True):
            for s, vs in by_state.items():
                if vs:
                    common.chip(f"{len(vs)} {common.STATE_LABELS[s]}", common.STATE_COLORS[s])
        for v in by_state["running"]:
            st.markdown(md(f"{v.case_id}: {_run_state(v)} (phase {common.PHASE_NAMES.get(v.last_phase, v.last_phase or '?')}, "
                           f"last write {common.secs(v.last_write_age_s)} ago)"))
        for v in by_state["incomplete"]:
            st.markdown(md(f"{v.case_id}: incomplete, last phase {v.last_phase or 'none'}, last write {common.secs(v.last_write_age_s)} ago; no answer.json"))
        for v in by_state["failed"]:
            st.markdown(md(f"{v.case_id} failed (phases.jsonl note 'case failed'):"))
            st.code(v.error, language=None, wrap_lines=True)
        for v in by_state["unreadable"]:
            st.markdown(md(f"{v.case_id}: answer.json unreadable, shown without a verdict:"))
            st.code(v.error, language=None, wrap_lines=True)
        if by_state["not_run"]:
            st.caption(md(f"Not run: {', '.join(v.case_id for v in by_state['not_run'])}"))


if any(s in ("running", "incomplete", "failed", "unreadable") for s in states.values()):
    _live_strip(sid, ORDER)

# --------------------------------------------------------------------------- #
# Challenge index                                                             #
# --------------------------------------------------------------------------- #
STATUS_COLORS = {"built": "green", "partial": "orange", "not attempted": "gray"}
done_stats = [stats[c] for c in ORDER if c in stats]
lat = [s["latency_s"] for s in done_stats if s["latency_s"] is not None]
tok = [float(s["tokens_total"]) for s in done_stats]
sar_cases = [c for c, a in answers.items() if (a.get("sar") or {}).get("file")]
cites = [x for c in sar_cases for x in common.sar_citations(answers[c])]
attest = [x for c in traced for x in common.attest_claims(traced[c])]
gated = Counter(str(a.get("route")) for ans in answers.values() for a in (ans.get("next_best_actions") or {}).get("final") or []
                if isinstance(a, dict) and a.get("route") in ("L1", "L2"))
sa = common.proof("schema_approval")
gate = next((str(r.get("status")) for r in (sa["data"] or {}).values() if isinstance(r, dict)), None) if isinstance(sa["data"], dict) else None
drift, gsql, variant = common.proof("drift"), common.proof("gsql"), common.proof("variant")
detectors_py = common.ROOT / "engine" / "detectors.py"
db_path = common.approvals_db_path()
try:
    feedback = approvals_db.feedback_rows_ro(path=db_path) if db_path.exists() else None
except sqlite3.Error:
    feedback = None


def _p(name: str) -> tuple[str, str]:
    p = common.proof(name)
    return (p["source"] or p["error"] or "missing"), (p["as_of"] or "missing")


def _c3() -> str | None:
    try:
        text = detectors_py.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        return "compile_policy_rule_detector is defined in engine/detectors.py" if re.search(r"^def compile_policy_rule_detector\b", text, re.M) \
            else "compile_policy_rule_detector does not exist in engine/detectors.py"
    c3 = common.proof("c3_built")
    return None if c3["data"] is None else ("snapshot: compile_policy_rule_detector present" if c3["data"] else "snapshot: compile_policy_rule_detector does not exist")


def _c5():
    waves = _waves()
    if not waves:
        return None
    pre = sum(float(v.get("pre_as_of") or 0) for v in waves.values())
    post = sum(float(v.get("post_as_of") or 0) for v in waves.values())
    return f"{len(waves)} waves replayed · {common.money(pre)} before the alert, {common.money(post)} after"


def _c7():
    waves = _waves()
    if not waves:
        return None
    outside = sum(int(v.get("legit_touched_targeted") or 0) for v in waves.values())
    broad = sum(int(v.get("broad_rule_extra_customers") or 0) for v in waves.values())
    return f"{len(waves)} waves · {outside} customers outside the wave touched · a broad rule adds {broad} customers"


def _c1():
    dets = [d for d in bt_data.get("detectors") or [] if re.match(r"^D\d", str(d.get("detector_id", "")))]
    if not dets:
        return None
    fp = sum(int(d.get("false_positives_on_cleared_cases") or 0) for d in dets)
    return f"{len(dets)} detectors backtested over {int(bt_data.get('n_closed_cases') or 0):,} closed cases · {fp} cleared case(s) inside a match set"


def _c2():
    props = bt_data.get("self_growing_proposals")
    if props is None:
        return None
    approved = sum(1 for p in props if p.get("approval_status") == "approved")
    return f"{len(props)} proposals · {approved} approved by a fraud manager"


def _c10():
    d = attacks["data"] if isinstance(attacks["data"], dict) else None
    return None if d is None else f"{d.get('attacks_blocked')}/{d.get('total_attacks')} blocked ({d.get('planted_in_pack')} planted, {d.get('synthetic_drills')} synthetic)"


def _c11():
    if not dq_data:
        return None
    return f"{len(dq_data.get('defects') or [])} defect classes · {int(dq_data.get('n_tainted_cases') or len(dq_by_case))}/{N} cases tainted"


def _c12():
    d = drift["data"]
    n = len(d.get("drifts") or []) if isinstance(d, dict) else len(d) if isinstance(d, list) else None
    if n is None and gate is None:
        return None
    return " · ".join(x for x in (f"{n} drifts" if n is not None else "drifts not measured here", f"migration {gate}" if gate else "") if x)


def _c13():
    g = gsql["data"]
    if not isinstance(g, dict):
        return None
    v = variant["data"]
    cols = v.get("column_mappings") or v.get("columns") or v.get("mappings") if isinstance(v, dict) else None
    return f"{len(g)} bank schemas mapped (self-check)" + (f" · {len(cols)} variant columns" if isinstance(cols, list | dict) else " · no variant folder")


def _c9():
    if not done_stats:
        return None
    p50, p95 = _nearest_rank(lat, 0.5), _nearest_rank(lat, 0.95)
    k50, k95 = _nearest_rank(tok, 0.5), _nearest_rank(tok, 0.95)
    return (f"latency p50 {common.secs(p50)} / p95 {common.secs(p95)} · tokens p50 {int(k50 or 0):,} / p95 {int(k95 or 0):,} "
            f"over {len(done_stats)} done cases · cost not computed")


bt_src, bt_at = bt["source"] or bt["error"] or "missing", bt["as_of"] or "missing"
cs_src, cs_at = cs["source"] or cs["error"] or "missing", cs["as_of"] or "missing"
gate_txt = f"gate {gate}" if gate else "gate status not measured here"
challenges = [
    ("C1 new typologies", "built", "", _c1(), bt_src, bt_at, "detectors"),
    ("C2 self-growing detectors", "partial", "candidate shapes written by the team, not discovered by the agent", _c2(), bt_src, bt_at, "detectors"),
    ("C3 policy text → detector", "not attempted", "compile_policy_rule_detector does not exist", _c3(), common.rel(detectors_py), common.as_of(detectors_py), "detectors"),
    ("C4 SAR with citations", "built", "", f"{len(sar_cases)} SARs filed · {sum(1 for x in cites if x['status'] == 'cited')}/{len(cites)} sentences cited" if answers else None,
     ANSWERS_SRC, ANSWERS_AS_OF, "case"),
    ("C5 ring replay", "built", "", _c5(), cs_src, cs_at, "detectors"),
    ("C6 audit replay", "built", "", f"{len(traced)}/{n_done} decided cases traced · {sum(1 for x in attest if x['call'])}/{len(attest)} claims matched to a call" if traced else None,
     TRACE_SRC if src.kind != "submitted" else "no trace in this source", TRACE_AS_OF, "case"),
    ("C7 containment", "built", "", _c7(), cs_src, cs_at, "detectors"),
    ("C8 analyst feedback", "partial", "feedback recorded, never read back", None if feedback is None else f"{len(feedback)} feedback rows recorded",
     common.rel(db_path) + ("" if db_path.exists() else " (absent)"), common.as_of(db_path), "approvals"),
    ("C9 speed, cost, autonomy", "partial", "no alert-triggered pipeline, cost not computed; latency measured", _c9(), ANSWERS_SRC, ANSWERS_AS_OF, "runs"),
    ("C10 break the agent", "built", "", _c10(), *_p("attacks"), "security"),
    ("C11 data quality", "built", "", _c11(), *_p("dq"), "data"),
    ("C12 schema drift", "partial", f"{gate_txt}, not enforced by etl.delta", _c12(), *_p("drift"), "data"),
    ("C13 someone else's schema", "partial", "mapping only, no investigation run on a variant", _c13(), *_p("gsql"), "data"),
    ("C14 human gates", "built", "",
     f"{sum(gated.values())} gated actions ({gated.get('L1', 0)} L1 / {gated.get('L2', 0)} L2) · " + (f"inbox {len(pending)} pending" if not inbox_note else "no inbox on this deployment") if answers else None,
     f"{ANSWERS_SRC} and {common.rel(db_path)}", ANSWERS_AS_OF, "approvals"),
]

st.markdown("#### Challenge index")
index = pd.DataFrame([{
    "Challenge": name, "Status": [status], "Scope": scope or "—", "Measured headline": headline if headline is not None else "not measured here",
    "Source file": source, "As of": when, "Open": common.PAGES[page]["title"],
} for name, status, scope, headline, source, when, page in challenges])
st.dataframe(
    index, key="challenges", hide_index=True, height=35 * len(index) + 38,
    column_config={
        "Status": st.column_config.MultiselectColumn("Status", options=list(STATUS_COLORS), color=list(STATUS_COLORS.values())),
        "Measured headline": st.column_config.TextColumn("Measured headline", width="large", help="computed now from the source file; 'not measured here' when it is missing"),
        "Open": st.column_config.ButtonColumn("Open", type="tertiary", key="ch_open"),
    },
)
ch_click = st.session_state.get("ch_open")
if ch_click and 0 <= int(ch_click["row"]) < len(challenges):
    common.goto(challenges[int(ch_click["row"])][-1], case=ctx.case_id)

# --------------------------------------------------------------------------- #
# Known limits                                                                #
# --------------------------------------------------------------------------- #
limits = common.known_limits()
with st.container(border=True, key="known-limits"):
    st.markdown(f"**Known limits** · {len(limits)} computed from files")
    for item in limits[:5]:
        detail = str(item["detail"])
        short = detail if len(detail) <= 90 else detail[:89].rstrip() + "…"  # one line each; the full text is in the tooltip
        st.markdown(f"**{md(item['title'])}** · {md(short)} · :gray[{md(item['source'])}]",
                    help=f"{md(detail)}  \n**Source:** {md(item['source'])}  \n**As of:** {md(item['as_of'])}")
    common.page_link("system", "All known limits", icon=":material/arrow_forward:")


# --------------------------------------------------------------------------- #
# Provenance footer                                                           #
# --------------------------------------------------------------------------- #
def _hashes_consistent() -> str:
    top = src.manifest.get("prompt_sha256") or {}
    runs = [cr for cr in (src.manifest.get("case_runs") or {}).values() if isinstance(cr, dict)]
    ok = sum(1 for cr in runs if all(str(top.get(k, "")).startswith(str(h)) for k, h in (cr.get("prompt_sha256") or {}).items()))
    return f"prompt hashes consistent {ok}/{len(runs)}" if runs else "prompt hashes not recorded"


if src.kind == "submitted":
    footer = f"Answer files {ANSWERS_SRC}, newest written {ANSWERS_AS_OF} · no trace in this source; the trace is in the run each answer was promoted from"
else:
    models = ", ".join(f"{m} × {n} LLM calls" for m, n in src.models_seen.most_common()) or "no LLM calls recorded"
    git = f"git {src.git_sha[:7]}" + (" + uncommitted changes" if src.git_dirty else "") if src.git_sha else "git sha not recorded"
    footer = " · ".join([
        models, _hashes_consistent(), git, f"{src.first_started or '?'} → {src.finished or '?'}",
        f"max_tool_calls {src.max_tool_calls if src.max_tool_calls is not None else 'not recorded'}",
        f"embed {src.manifest.get('embed_model') or 'not recorded'}", f"source runs/{sid}/run_manifest.json",
    ])
st.caption(md(footer))
common.page_link("runs", "Runs & consistency (raw manifest, graph hygiene log)", icon=":material/monitoring:")

"""Overview, the default page: what the agent decided on the alerts of the selected source, and whether to trust it.

The title and one provenance line (run, model, git, finish time; "decided" and "identical" return as warnings only when
they are not true), a run-health banner while a case is running or broken, the decision board as one block per verdict
(fraud → uncertain → legitimate, then any undecided case), each under a heading that carries its count and outcome, then
one Proof section: the C1-C14 table and the known limits not already on screen.

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

from ui import approvals_db, common, design  # noqa: E402

ctx = common.page_setup("overview")
src = ctx.source
sid = ctx.source_id or ""
md = common.md
esc = design.esc

if src is None:
    common.page_header("Overview")
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
# Header: the title, one provenance line, the run details popover             #
# --------------------------------------------------------------------------- #
BROKEN = ("running", "incomplete", "failed", "unreadable")
states = {cid: v.state for cid, v in views.items()}
n_done = sum(1 for s in states.values() if s == "done")
not_done = {s: [c for c, x in states.items() if x == s] for s in (*BROKEN, "not_run")}
unhealthy = any(not_done[s] for s in BROKEN)
n_cmp = len(agree)
n_ident = sum(1 for a in agree.values() if a["identical"])
differs = [c for c, a in agree.items() if not a["same_decision"]]
equal = Counter(r for rs in src.promoted_from.values() for r in rs) if src.kind == "submitted" else Counter()


def _identity() -> tuple[str | None, str | None, bool]:
    """(the provenance-line phrase when every answer is identical to its reference, the warning when not, whether the
    warning involves a hard action)."""
    if src.kind == "submitted":
        missing = sorted(c for c, rs in src.promoted_from.items() if not rs)
        if missing:
            return None, f"Not identical to any run on disk: {', '.join(missing)}", False
        top = equal.most_common(3)
        return ("identical to " + ", ".join(f"run {r} {n}/{src.n_total}" for r, n in top)) if top else None, None, False
    if REF is None or not n_cmp:
        return f"identity to {REF_SRC} not measured here", None, False
    if n_ident == n_cmp == n_done:
        return "identical to the submitted answers", None, False
    parts, hard_diff = [], False
    if differs:
        hard_diff = any(agree[c]["cls"] == "differs · hard action" for c in differs)
        parts.append("Differs from the submitted answers: " + ", ".join(f"{c} ({_diff_brief(views[c].answer, refs[c].answer)})" for c in differs))
    elif n_ident < n_cmp:
        parts.append(f"{n_ident}/{n_cmp} identical to the submitted answers; the rest differ only outside the decision")
    if n_cmp < n_done:
        parts.append(f"{n_done - n_cmp} decided cases have no submitted answer in {REF_SRC}")
    return None, " · ".join(parts), hard_diff


ident_phrase, ident_warning, ident_hard = _identity()


def _provenance() -> str:
    if src.kind == "submitted":
        bits = [f"Submitted answers {ANSWERS_SRC}"]
    else:
        model = ", ".join(src.models_seen) or src.manifest_model or "model not recorded"
        git = f"git {src.git_sha[:7]}" + (" + uncommitted changes" if src.git_dirty else "") if src.git_sha else "git sha not recorded"
        bits = [f"{'Live' if src.mode == 'live' else 'Mock'} run {sid}", model, git, f"finished {src.finished}" if src.finished else "finish time not recorded"]
    if n_done == N:
        bits.append(f"{n_done}/{N} decided")
    if ident_phrase:
        bits.append(ident_phrase)
    return " · ".join(bits)


def _hashes_consistent() -> str:
    top = src.manifest.get("prompt_sha256") or {}
    runs = [cr for cr in (src.manifest.get("case_runs") or {}).values() if isinstance(cr, dict)]
    ok = sum(1 for cr in runs if all(str(top.get(k, "")).startswith(str(h)) for k, h in (cr.get("prompt_sha256") or {}).items()))
    return f"prompt hashes consistent {ok}/{len(runs)}" if runs else "prompt hashes not recorded"


def _run_details() -> None:
    """The former provenance footer, folded into a popover beside the title."""
    rows = [("Answers", f"{ANSWERS_SRC}, newest written {ANSWERS_AS_OF}")]
    if src.kind == "submitted":
        rows.append(("Trace", "none in this source; the trace is in the run each answer was promoted from"))
    else:
        rows += [
            ("Trace", f"{TRACE_SRC}, newest written {TRACE_AS_OF}"),
            ("LLM calls", ", ".join(f"{m} × {n}" for m, n in src.models_seen.most_common()) or "none recorded"),
            ("Prompts", _hashes_consistent()),
            ("Window", f"{src.first_started or '?'} → {src.finished or '?'}"),
            ("max_tool_calls", str(src.max_tool_calls) if src.max_tool_calls is not None else "not recorded"),
            ("Embedding model", str(src.manifest.get("embed_model") or "not recorded")),
            ("Manifest", f"runs/{sid}/run_manifest.json"),
        ]
    if REF:
        rows.append(("Compared with", f"{REF_SRC} (common.agreement)"))
    rows.append(("Approvals", inbox_note or f"{common.rel(common.approvals_db_path())} (read-only)"))
    with st.popover("Run details", icon=":material/info:", key="ov_details"):
        design.html_block('<table class="tc-table ov-details">'
                          + "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows) + "</table>")
        common.page_link("runs", "Runs & consistency", icon=common.PAGE_ICONS["runs"])


ROUND_NAME = {"final": "Final Round", "round1": "Round 1"}.get(src.round, "")
TITLE = " ".join(x for x in (str(N), ROUND_NAME, "alert" if N == 1 else "alerts") if x)
SCOPE = " · ".join(x for x in ("HHGOA 2026" if FINAL else "Round 1 archive", _id_range(ORDER)) if x)
_title, _side = st.columns([6, 1.3], vertical_alignment="center")
with _title:
    common.page_header(TITLE, _provenance(), eyebrow=SCOPE)
with _side, st.container(horizontal_alignment="right", gap="small", key="ov-side"):
    common.tigergraph_credit("overview", width=128, align="right")
    _run_details()
if not FINAL:
    final_default = common.default_source()
    final_src = common.get_source(final_default)
    st.info(md("The Final Round answers are in " + (f"{final_default}." if final_src is not None and final_src.round != "round1" else "no source on this deployment.")),
            icon=":material/history:")
    if final_src is not None and final_src.round != "round1" and st.button("Back to Final Round", icon=":material/arrow_back:", key="ov_back"):
        common.goto("overview", run=final_default)


# --------------------------------------------------------------------------- #
# Run health: only while a case is running, incomplete, failed or unreadable  #
# --------------------------------------------------------------------------- #
def _run_state(view) -> str:
    if view.state == "running":
        t = max((_float(r.get("t")) or 0.0 for r in view.phases), default=0.0)
        return f"running · {view.last_phase or 'P0'} · {t + (view.last_write_age_s or 0):.0f} s"
    if view.state == "incomplete":
        return f"incomplete · last {view.last_phase or 'no phase'}"
    return common.STATE_LABELS.get(view.state, view.state)


@st.fragment(run_every="5s")
def _run_health(source_id: str, ids: list[str]) -> None:
    now = {cid: common.case_view(source_id, cid) for cid in ids}
    by_state = {s: [v for v in now.values() if v.state == s] for s in (*BROKEN, "not_run")}
    decided = sum(1 for v in now.values() if v.state == "done")
    kind = "red" if by_state["failed"] or by_state["unreadable"] else "ochre"
    counts = " · ".join(f"{len(vs)} {common.STATE_LABELS[s]}" for s, vs in by_state.items() if vs)
    with st.container(border=True, key="live-strip"):
        design.html_block(f'<p class="tc ov-health {kind}"><b>Run health</b> · decided {decided}/{len(ids)}' + (f" · {esc(counts)}" if counts else "") + "</p>")
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


if unhealthy:
    _run_health(sid, ORDER)
elif n_done < N:
    st.warning(md(f"Decided {n_done}/{N} · not run: {', '.join(not_done['not_run'])}"), icon=":material/warning:")
if ident_warning:
    (st.error if ident_hard else st.warning)(md(ident_warning), icon=":material/difference:")

# --------------------------------------------------------------------------- #
# Outcome figures: they head the board's verdict groups and the approvals line #
# --------------------------------------------------------------------------- #
def _prov_help(what: str, source: str, as_of: str, how: str = "") -> str:
    """The tooltip common.measured gives a headline number (what, source, as of, how), for a number set in a heading."""
    lines = [f"**What:** {md(what)}", f"**Source:** {md(source)}", f"**As of:** {md(as_of)}"]
    return "  \n".join(lines + ([f"**How:** {md(how)}"] if how else []))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


saf = common.safety(answers)
mix = saf["verdict_mix"]
hard = saf["hard_on_non_fraud"]
n_hard = sum(len(acts) for _, acts in hard)
sar_cases = sorted(c for c, a in answers.items() if (a.get("sar") or {}).get("file"))
prefix = "HHF-" if FINAL else "HHG-"
ds = approvals_db.directory_status()
db_path = common.approvals_db_path()
pending = [r for r in inbox if r.get("status") == "pending" and str(r.get("case_id", "")).startswith(prefix)]
non_fraud = {c for c, a in answers.items() if (a.get("case") or {}).get("verdict") in ("legitimate", "uncertain")}
inbox_hard = sorted({r["case_id"] for r in inbox if r.get("case_id") in non_fraud and r.get("action") in common.HARD_ACTIONS and r.get("status") != "rejected"})
brief = f" The brief says exactly {common.BRIEF_LEGITIMATE} of the 18 cases are legitimate; not graded here." if FINAL else ""
roles = ", ".join(f"{r} {n}" for r, n in ds["roles"].items())
# (group, verdict word, colour class, count, outcome, tooltip): the board's verdict groups, in board order
GROUP_HEADS = [
    (0, "Fraud", "red", mix.get("fraud", 0), f"contain · {_plural(len(sar_cases), 'SAR draft')}",
     _prov_help("decided cases with case.verdict fraud; SAR drafts are answers with sar.file true (FILE_REPORT goes to L2)",
                ANSWERS_SRC, ANSWERS_AS_OF, "common.safety verdict_mix; sar.file per answer")),
    (1, "Uncertain", "ochre", mix.get("uncertain", 0), f"handed to an analyst · {saf['uncertain_escalated']}/{saf['n_uncertain']} escalated",
     _prov_help("decided cases with case.verdict uncertain; escalated = ESCALATE_TO_ANALYST in the final actions",
                ANSWERS_SRC, ANSWERS_AS_OF, "common.safety verdict_mix and uncertain_escalated")),
    (2, "Legitimate", "green", mix.get("legitimate", 0), f"closed · {_plural(n_hard, 'hard action')}",
     _prov_help("decided cases with case.verdict legitimate; hard actions = BLOCK_CARD, BLOCK_ALL_CARDS, FILE_REPORT or a SAR on a "
                "legitimate or uncertain verdict." + brief, ANSWERS_SRC, ANSWERS_AS_OF, "common.safety verdict_mix and hard_on_non_fraud")),
] if n_done else []
APPROVALS_HELP = _prov_help(
    f"pending {prefix}* rows in the approvals inbox; " + ("no approver directory, so approvals fail closed" if not ds["n"] else f"approver directory: {roles}"),
    f"{common.rel(db_path)} (read-only) and {common.rel(ds['path'])}", f"{common.as_of(db_path)} / {common.as_of(ds['path'])}",
    "approvals_db.directory_status and common.inbox_rows, both read-only")
APPROVALS_LINE = ("Approvals pending: not measured here" if inbox_note else f"**{_plural(len(pending), 'approval')} pending**") + " · " + (
    "nobody can approve here (fail closed)" if not ds["n"] else md(f"{_plural(ds['n'], 'approver')} ({roles})"))
if hard:
    st.error(md("Hard action on a non-fraud verdict: " + "; ".join(f"{c}: {', '.join(acts)}" for c, acts in hard)), icon=":material/block:")
if saf["uncertain_not_escalated"]:
    st.warning(md(f"Uncertain and not escalated to an analyst: {', '.join(saf['uncertain_not_escalated'])}"), icon=":material/warning:")
if inbox_hard:
    st.error(md(f"The approvals inbox holds hard actions on a non-fraud verdict for {', '.join(inbox_hard)}"), icon=":material/block:")

# --------------------------------------------------------------------------- #
# Decision board                                                              #
# --------------------------------------------------------------------------- #
bt = common.proof("backtest")
bt_data = bt["data"] if isinstance(bt["data"], dict) else {}
cs = common.proof("containment_summary")
attacks = common.proof("attacks")
dq = common.proof("dq")
traced = {c: v for c, v in views.items() if v.ok and v.has_trace}
usage = {c: common.tg_usage(v) for c, v in traced.items()}
stats = {c: common.trace_stats(v) for c, v in views.items() if v.ok}

FLAG_TYPES = {"security_alert": "injection stripped", "structured_retry": "retry", "clamp": "clamp", "policy_reprompt": "re-prompt",
              "failed_call": "failed call", "dropped_ids": "dropped ids", "sar_fallback": "SAR fallback"}
GROUP = {"fraud": 0, "uncertain": 1, "legitimate": 2}  # the board's row groups; a case without a verdict comes last
waves_by_case = {c: str(k).split("_")[0] for k, cs_ in (bt_data.get("exam_scan") or {}).items() for c in cs_}
dq_data = dq["data"] if isinstance(dq["data"], dict) else {}
dq_by_case = {c: sorted({str(t.get("code")) for t in ts if isinstance(t, dict)}) for c, ts in (dq_data.get("taint_matrix") or {}).items()}


def _trigger_label(kind: str) -> str:
    return kind.replace("_", " ") if kind else "unknown"


def _trigger(meta: dict) -> str:
    kind = str(meta.get("trigger_type") or "")
    score = common.prob(meta.get("risk_score")) if kind == "risk_score" else "—"
    return _trigger_label(kind) + (f" {score}" if score != "—" else "")


def _pattern(cid: str, ans: dict) -> str:
    wave = waves_by_case.get(cid)
    pat = str((ans.get("case") or {}).get("pattern") or "").strip().replace("_", " ")
    pat = "" if pat.lower() in ("", "none") else pat if len(pat) <= 32 else pat[:31].rstrip() + "…"
    return " · ".join(([f"{wave} wave"] if wave else []) + ([pat] if pat else [])) or "—"


def _gated(ans: dict) -> str:
    """'BLOCK_CARD · L1, FILE_REPORT · L2 +2 auto': the L1/L2-gated final actions, then the count without a gate."""
    final = [a for a in (ans.get("next_best_actions") or {}).get("final") or [] if isinstance(a, dict) and a.get("action") != "CREATE_CASE"]
    gated = [f"{a.get('action')} · {a.get('route')}" for a in final if a.get("route") in ("L1", "L2")]
    n_auto = len(final) - len(gated)
    return " ".join(x for x in (", ".join(gated), f"+{n_auto} auto" if n_auto else "") if x) or "—"


def _sar(cid: str, ans: dict) -> str:
    if not (ans.get("sar") or {}).get("file"):
        return "—"
    rows = [r for r in inbox if r.get("case_id") == cid and r.get("action") == "FILE_REPORT"]
    by = {str(r.get("status")): r for r in rows}
    if "approved" in by:
        return f"approved by {by['approved'].get('decided_by')}"
    if "pending" in by:
        return f"awaits {by['pending'].get('route') or 'L2'}"
    if "rejected" in by:
        return "rejected"
    return "drafted" if inbox_note else "drafted, not queued"


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
    pend = [r for r in live if r.get("status") == "pending"]
    parts = [f"{len(pend)} pending"] if pend else []
    parts += [f"{r.get('status')} by {r.get('decided_by')}" for r in live if r.get("status") in ("approved", "rejected")]
    unqueued = gated - {str(r.get("action")) for r in live}
    if unqueued:
        parts.append(f"{len(unqueued)} not queued" if parts else "not queued")
    superseded = [r for r in rows if r.get("action") not in gated]
    if superseded:
        parts.append(f"superseded {len(superseded)}")
    return " · ".join(parts)


def _attention(view) -> str:
    """'injection stripped · DQ-01': the run state when not done, the guardrail notes, then the DQ taint codes."""
    seen = [FLAG_TYPES[e["type"]] for e in common.guardrail_events(view) if e["type"] in FLAG_TYPES]
    state = [] if view.state == "done" else [common.STATE_LABELS.get(view.state, view.state)]
    return " · ".join(state + list(dict.fromkeys(seen)) + dq_by_case.get(view.case_id, [])) or "—"


rows = []
for cid in ORDER:
    v = views[cid]
    a = v.answer if v.ok else {}
    verdict = str((a.get("case") or {}).get("verdict") or "") if v.ok else ""
    rows.append({
        "Case": cid, "_group": GROUP.get(verdict, 3), "_trigger": str(v.meta.get("trigger_type") or "unknown"),
        "p(fraud)": _float((a.get("case") or {}).get("fraud_probability")) if v.ok else None,
        "Trigger": _trigger(v.meta), "Pattern or wave": _pattern(cid, a) if v.ok else "—", "Gated actions": _gated(a) if v.ok else "—",
        "SAR": _sar(cid, a) if v.ok else "—", "Approvals": _approvals(cid, a) if v.ok else "—", "Attention": _attention(v),
        "Evidence request": _evidence_request(a) if v.ok else "—", "Run state": _run_state(v), "vs submitted": _vs_submitted(cid),
        "Opened at": str(v.meta.get("opened_at") or "—"),
    })

design.html_block(design.section("", "Decision board"))
with st.container(horizontal=True, vertical_alignment="center", gap="small", key="ov-pending"):
    st.markdown(APPROVALS_LINE, help=APPROVALS_HELP, width="content")
    common.page_link("approvals", "Approvals", icon=common.PAGE_ICONS["approvals"])
trigger_counts = Counter(r["_trigger"] for r in rows)
with st.container(horizontal=True, vertical_alignment="center", key="ov-controls"):
    picked = st.pills("Trigger", [t for t, _ in trigger_counts.most_common()], selection_mode="multi", key="ov_trigger", label_visibility="collapsed",
                      format_func=lambda t: f"{_trigger_label(t)} · {trigger_counts[t]}")
    all_cols = st.toggle("All columns", key="ov_cols", help="Adds the evidence request, run state, opened_at and the comparison with the submitted answers")

shown = sorted((r for r in rows if not picked or r["_trigger"] in picked), key=lambda r: (r["_group"], r["Case"]))
MAIN = ["Case", "p(fraud)", "Trigger", "Pattern or wave", "Gated actions", "SAR", "Approvals", "Attention"]
EXTRA = ["Evidence request", "Run state", "Opened at", "vs submitted"]
BOARD_COLS = {
    "p(fraud)": st.column_config.ProgressColumn("p(fraud)", min_value=0.0, max_value=1.0, format="%.2f", width=90, help="case.fraud_probability on a fixed 0–1 scale"),
    "Trigger": st.column_config.TextColumn("Trigger", width=118),
    "Pattern or wave": st.column_config.TextColumn("Pattern or wave", width=common.text_width((r["Pattern or wave"] for r in rows), 150, 300),
                                                   help=f"case.pattern from the answer; the detector wave from {bt['source'] or 'the detector backtest'} exam_scan"),
    "Gated actions": st.column_config.TextColumn("Gated actions", width=common.text_width((r["Gated actions"] for r in rows), 160, 480),
                                                 help="ACTION · route for each L1/L2-gated final action; +N auto: final actions without a gate, CREATE_CASE left out"),
    "SAR": st.column_config.TextColumn("SAR", width=88, help="sar.file true: the SAR draft, and its FILE_REPORT row in the approvals inbox"),
    "Approvals": st.column_config.TextColumn("Approvals", width=88),
    "Attention": st.column_config.TextColumn("Attention", width=common.text_width((r["Attention"] for r in rows), 150, 340),
                                             help="run state when not done, guardrail notes from phases.jsonl (injection stripped, retries) and DQ taints"),
    "Evidence request": st.column_config.TextColumn("Evidence request", width="medium"),
    "vs submitted": st.column_config.TextColumn("vs submitted", help=f"common.agreement against {REF_SRC}" if REF else "no submitted answers to compare"),
}
covered = {g for g, *_ in GROUP_HEADS}
n_undecided = sum(1 for r in rows if r["_group"] not in covered)
undecided_head = (3, "Undecided", "", n_undecided, "no verdict shown", _prov_help(
    "cases without a parsed answer (running, incomplete, failed, unreadable or not run) or without a fraud, uncertain or legitimate "
    "verdict; a case without a parsed answer never shows a verdict", ANSWERS_SRC, ANSWERS_AS_OF, "common.case_view per case"))
board_names = []
for g, word, kind, total, outcome, tip in [*GROUP_HEADS, *([undecided_head] if n_undecided else [])]:
    name = word.lower()
    grp = [r for r in shown if (r["_group"] == g if g in covered else r["_group"] not in covered)]
    note = f" · {len(grp)} shown" if picked and len(grp) != total else ""
    word_html = f'<span class="ov-v {kind}">{esc(word)}</span>' if kind else esc(word)  # colour sits on the verdict word only
    with st.container(key=f"ov-g-{name}"):
        st.markdown(f'<p class="ov-gh">{word_html} · {total} <span class="ov-o">→ {esc(outcome)}{esc(note)}</span></p>',
                    unsafe_allow_html=True, help=tip, width="content")  # content width: the help icon sits after the heading
        if grp:
            board_names.append(name)
            st.dataframe(
                pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in grp], columns=MAIN + EXTRA),
                key=f"board_{name}", hide_index=True, height="content", column_order=None if all_cols else MAIN,
                column_config={"Case": st.column_config.ButtonColumn("Case", type="tertiary", pinned=True, key=f"board_open_{name}", width=84,
                                                                     help="Open the Case file"), **BOARD_COLS},
            )
for name in board_names:
    click = st.session_state.get(f"board_open_{name}")
    if click:
        common.goto("case", case=click["label"])
st.caption(md(
    f"Rows: {ANSWERS_SRC}" + (f", {TRACE_SRC}" if src.kind != "submitted" else "")
    + f" · approvals: {inbox_note or common.rel(db_path) + ' (read-only)'}"
    + (f" · DQ: {dq['source']}" if dq_by_case else " · DQ: not measured here")
    + (f" · waves: {bt['source']}" if waves_by_case else "")
    + (f" · {len(shown)} of {N} shown" if len(shown) != N else "")
))

# --------------------------------------------------------------------------- #
# Proof: the C1-C14 table and the known limits not already on screen          #
# --------------------------------------------------------------------------- #
done_stats = [stats[c] for c in ORDER if c in stats]
lat = [s["latency_s"] for s in done_stats if s["latency_s"] is not None]
tok = [float(s["tokens_total"]) for s in done_stats]
cites = [x for c in sar_cases for x in common.sar_citations(answers[c])]
attest = [x for c in traced for x in common.attest_claims(traced[c])]
gated = Counter(str(a.get("route")) for ans in answers.values() for a in (ans.get("next_best_actions") or {}).get("final") or []
                if isinstance(a, dict) and a.get("route") in ("L1", "L2"))
sa = common.proof("schema_approval")
gate = next((str(r.get("status")) for r in (sa["data"] or {}).values() if isinstance(r, dict)), None) if isinstance(sa["data"], dict) else None
drift, gsql, variant = common.proof("drift"), common.proof("gsql"), common.proof("variant")
detectors_py = common.ROOT / "engine" / "detectors.py"
try:
    feedback = approvals_db.feedback_rows_ro(path=db_path) if db_path.exists() else None
except sqlite3.Error:
    feedback = None
readback_case = next((c for c, u in usage.items() if u["agent_readbacks"]), None)


def _p(name: str) -> tuple[str, str]:
    p = common.proof(name)
    return (p["source"] or p["error"] or "missing"), (p["as_of"] or "missing")


def _waves() -> dict:
    data = cs["data"] if isinstance(cs["data"], dict) else {}
    return {k: v for k, v in data.items() if str(k).startswith(prefix) and isinstance(v, dict)}


def _c1():
    dets = [d for d in bt_data.get("detectors") or [] if re.match(r"^D\d", str(d.get("detector_id", "")))]
    if not dets:
        return None
    fp = sum(int(d.get("false_positives_on_cleared_cases") or 0) for d in dets)
    hits = [(str(d.get("detector_id", "")).split("_")[0], c) for d in bt_data.get("detectors") or [] for c in d.get("legitimate_customers_acted_against") or []]
    scan = bt_data.get("exam_scan") or {}
    names = sorted(str(k).split("_")[0] for k in scan)
    span = (f"{names[0]}–{names[-1]}" if len(names) > 1 else names[0]) if names else ""
    fire = f" · {span} fire on {_id_range(sorted({c for x in scan.values() for c in x}))}" if names else ""
    return (f"{len(dets)} detectors backtested over {int(bt_data.get('n_closed_cases') or 0):,} closed cases{fire} · {fp} cleared case(s) inside a match set"
            + (f" ({', '.join(f'{d} → {c}' for d, c in hits)})" if hits else ""))


def _c2():
    props = bt_data.get("self_growing_proposals")
    if props is None:
        return None
    approved = sum(1 for p in props if p.get("approval_status") == "approved")
    return f"{len(props)} proposals · {approved} approved by a fraud manager"


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


def _c6():
    if not traced:
        return None
    out = f"{len(traced)}/{n_done} decided cases traced · {sum(1 for x in attest if x['call'])}/{len(attest)} claims matched to a call"
    with_hits = sum(1 for u in usage.values() if u["vector_hits"])
    readbacks = sum(len(u["agent_readbacks"]) for u in usage.values())
    ok = sum(1 for c in traced if stats[c]["writes"] and stats[c]["writes_ok"] == stats[c]["writes"])
    return out + f" · vector search hits in {with_hits}/{len(usage)} traces ({readbacks} agent-case read-backs) · write-back ok {ok}/{len(traced)}"


def _c7():
    waves = _waves()
    if not waves:
        return None
    outside = sum(int(v.get("legit_touched_targeted") or 0) for v in waves.values())
    broad = sum(int(v.get("broad_rule_extra_customers") or 0) for v in waves.values())
    return f"{len(waves)} waves · {outside} customers outside the wave touched · a broad rule adds {broad} customers"


def _c9():
    if not done_stats:
        return None
    p50, p95 = _nearest_rank(lat, 0.5), _nearest_rank(lat, 0.95)
    k50, k95 = _nearest_rank(tok, 0.5), _nearest_rank(tok, 0.95)
    return (f"latency p50 {common.secs(p50)} / p95 {common.secs(p95)} · tokens p50 {int(k50 or 0):,} / p95 {int(k95 or 0):,} "
            f"over {len(done_stats)} done cases · cost not computed")


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


bt_src, bt_at = bt["source"] or bt["error"] or "missing", bt["as_of"] or "missing"
cs_src, cs_at = cs["source"] or cs["error"] or "missing", cs["as_of"] or "missing"
gate_txt = f"gate {gate}" if gate else "gate status not measured here"
# (code, name, status, scope, measured headline, source, as of, page, case)
challenges = [
    ("C1", "New typologies", "built", "", _c1(), bt_src, bt_at, "detectors", None),
    ("C2", "Self-growing detectors", "partial", "candidate shapes written by the team, not discovered by the agent", _c2(), bt_src, bt_at, "detectors", None),
    ("C3", "Policy text → detector", "not attempted", "compile_policy_rule_detector does not exist", _c3(), common.rel(detectors_py), common.as_of(detectors_py), "detectors", None),
    ("C4", "SAR with citations", "built", "", f"{len(sar_cases)} SAR drafts · {sum(1 for x in cites if x['status'] == 'cited')}/{len(cites)} sentences cited" if answers else None,
     ANSWERS_SRC, ANSWERS_AS_OF, "case", sar_cases[0] if sar_cases else ctx.case_id),
    ("C5", "Ring replay", "built", "", _c5(), cs_src, cs_at, "detectors", None),
    ("C6", "Audit replay", "built", "", _c6(), TRACE_SRC if src.kind != "submitted" else "no trace in this source", TRACE_AS_OF, "case", readback_case or ctx.case_id),
    ("C7", "Containment", "built", "", _c7(), cs_src, cs_at, "detectors", None),
    ("C8", "Analyst feedback", "partial", "feedback recorded, never read back", None if feedback is None else f"{len(feedback)} feedback rows recorded",
     common.rel(db_path) + ("" if db_path.exists() else " (absent)"), common.as_of(db_path), "approvals", None),
    ("C9", "Speed, cost, autonomy", "partial", "no alert-triggered pipeline, cost not computed; latency measured", _c9(), ANSWERS_SRC, ANSWERS_AS_OF, "runs", None),
    ("C10", "Break the agent", "built", "", _c10(), *_p("attacks"), "security", None),
    ("C11", "Data quality", "built", "", _c11(), *_p("dq"), "data", None),
    ("C12", "Schema drift", "partial", f"{gate_txt}, not enforced by etl.delta", _c12(), *_p("drift"), "data", None),
    ("C13", "Someone else's schema", "partial", "mapping only, no investigation run on a variant", _c13(), *_p("gsql"), "data", None),
    ("C14", "Human gates", "built", "", f"{sum(gated.values())} gated actions ({gated.get('L1', 0)} L1 / {gated.get('L2', 0)} L2)" if answers else None,
     f"{ANSWERS_SRC} and {common.rel(db_path)}", ANSWERS_AS_OF, "approvals", None),
]
STATUS_KIND = {"built": "green", "partial": "ochre", "not attempted": ""}
PROOF_COLS = [1.45, .75, 3.2, 2.1, 1.3]

design.html_block(design.section("", "Proof"))
with st.container(key="ov-proof"):
    with st.container(key="ov-proof-head"):
        for col, text in zip(st.columns(PROOF_COLS, gap="small"), ("Challenge", "Status", "Measured headline", "Source", "Evidence"), strict=True):
            with col:
                design.html_block(f'<div class="ov-th">{esc(text)}</div>')
    for i, (code, name, status, scope, headline, source, when, page, case) in enumerate(challenges):
        with st.container(key=f"ov-c-{i}"):
            cells = st.columns(PROOF_COLS, gap="small")
            with cells[0]:
                design.html_block(f'<div class="ov-td"><span class="ov-code">{esc(code)}</span>{esc(name)}'
                                  + (f'<span class="sub">{esc(scope)}</span>' if scope else "") + "</div>")
            with cells[1]:
                design.html_block(f'<div class="ov-td">{design.tag(status, STATUS_KIND.get(status, ""))}</div>')
            with cells[2]:
                design.html_block(f'<div class="ov-td">{esc(headline)}</div>' if headline is not None else '<div class="ov-td ov-nm">not measured here</div>')
            with cells[3]:
                design.html_block(f'<div class="ov-td"><span class="mono">{esc(source)}</span><span class="sub">as of {esc(when)}</span></div>')
            with cells[4]:
                common.page_link(page, common.PAGES[page]["title"], icon=common.PAGE_ICONS[page], case=case or ctx.case_id)

limits = common.known_limits()
# limits whose fact is already on this screen (a C row, a board group heading or the approvals line) are left to How it works
ON_SCREEN = ("C2:", "C3 ", "Verdict mix", "No approver principals", "Schema migration", "Analyst feedback is not used")
rest = [x for x in limits if not str(x["title"]).startswith(ON_SCREEN) and "acts against cleared customer" not in str(x["title"])]
if rest:
    design.html_block('<div class="tc ov-limits"><h3>Known limits</h3><ul>' + "".join(
        f'<li><b>{esc(x["title"])}</b> <span class="d">{esc(x["detail"])}</span><span class="s">{esc(x["source"])} · as of {esc(x["as_of"])}</span></li>'
        for x in rest[:5]) + "</ul></div>")
common.page_link("system", f"How it works: all {len(limits)} known limits", icon=common.PAGE_ICONS["system"])

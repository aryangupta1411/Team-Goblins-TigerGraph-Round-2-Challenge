"""Case file: one case in one source. The decision card, why the agent decided it, what TigerGraph did, and five lazy
tabs (decision path, evidence ledger, SAR and citations, approvals and handover, raw files).

The Case picker is the router's; this page reads ctx.case_id. Every view comes from common.case_view() over ONE source,
so a run's trace is never shown next to another source's answer. A case without a parsed answer renders its state card
and stops. Nothing here writes: the approvals database is read-only and the live re-run starts only from a confirmed
dialog.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, case_brief, common, design  # noqa: E402

md, badge = common.md, common.badge
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SHORT_NAME = re.compile(r"^(query|mcp|pytg):")
TABS = ["Decision path P0–P10", "Evidence ledger", "SAR & citations", "Approvals & handover", "Raw files"]
ORANGE_NOTES = {"clamp", "policy_reprompt", "structured_retry", "sar_validation_failed", "sar_fallback", "dropped_ids", "security_alert", "card_mismatch"}
RED_NOTES = {"case_failed", "failed_call"}
FAILED_ROW = "background-color: rgba(220, 38, 38, 0.14)"


def _num(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _count(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _simulated(text: object) -> bool:
    """An assumed evidence reply ('ASSUMED (simulated) ...', 'Assumed reply: ...'), which must carry a SIMULATED badge."""
    return bool(re.search(r"\bassumed\b|\(simulated\)", str(text or ""), re.I))


def _height(n: int, cap: int = 420) -> int | str:
    """Every row while they fit in about `cap` px, else that many rows and a scrollbar."""
    return common.table_height(n, cap=(cap - 2) // common.TABLE_ROW_PX - 1)


def _collapse(value):
    """Embedding vectors replaced by '<N-d vector>' so payloads stay readable."""
    if isinstance(value, dict):
        return {k: _collapse(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) >= 32 and all(isinstance(x, int | float) for x in value):
            return f"<{len(value)}-d vector>"
        return [_collapse(v) for v in value]
    return value


def _file(view: common.CaseView, name: str) -> tuple[str, str]:
    """(display path, as-of time) of one file of this case in its source."""
    # named as cases/... and runs/... even when those folders are links or RUNS_DIR / CASES_DIR point elsewhere
    if view.source_id in common.SUBMITTED:
        return f"cases/{view.case_id}.json", common.as_of(common.cases_dir() / f"{view.case_id}.json")
    p = (view.dir or common.runs_root() / view.source_id / view.case_id) / name
    return f"runs/{view.source_id}/{view.case_id}/{name}", common.as_of(p)


def _holders(case_id: str, exclude: str | None) -> list[common.CaseView]:
    """Views of this case in every other source that has it (any state but not_run)."""
    out = []
    for s in common.sources():
        if s.id == exclude:
            continue
        v = common.case_view(s.id, case_id)
        if v.state != "not_run":
            out.append(v)
    return out


def _queue(ctx: common.Ctx) -> list[str]:
    ids = [r["case_id"] for r in common.pack_for_source(ctx.source)]
    return ids + [c for c in (ctx.source.cases if ctx.source else []) if c not in ids]


def _live_blockers(ctx: common.Ctx, backend: str) -> list[str]:
    reasons = []
    if common.read_only():
        reasons.append("read-only deployment (DEPLOY_READONLY=1)")
    if ctx.mode != "live":
        reasons.append(f"mock mode ({ctx.mode_reason})")
    bs = common.backend_status(backend)
    if not bs["ok"]:
        reasons.append(f"backend {backend}: {bs['reason']}")
    held = common.run_lock()
    if held:
        reasons.append(f"run {held.get('run_id')} is still running (pid {held.get('pid')})")
    return reasons


@st.dialog("Run this case live", width="medium")
def _confirm_rerun(case_id: str, default_backend: str, last: common.CaseView | None) -> None:
    backends = [b for b in common.LLM_BACKENDS if b != "mock"]
    backend = st.selectbox("LLM backend", backends, index=backends.index(default_backend) if default_backend in backends else 0)
    bs = common.backend_status(backend)
    st.caption(md(bs["reason"]))
    run_id = st.text_input("Target run id", value=f"live-{case_id}-{datetime.now():%Y%m%d-%H%M}", help="letters, digits, '.', '_' and '-', at most 64")
    problem = "" if RUN_ID_RE.match(run_id or "") else "the run id must match ^[A-Za-z0-9._-]{1,64}$"
    if not problem and (common.runs_root() / run_id).exists():
        problem = f"runs/{run_id} already exists"
    if problem:
        st.error(md(problem))
    st.markdown("**Side effects**")
    st.markdown("\n".join(f"- {md(x)}" for x in (
        f"writes AgentCase {approvals_db.graph_case_id(case_id)}, its CaseEvents and an Approval per L1/L2 action to the shared FraudGraph",
        "adds pending rows to the local approvals inbox for L1/L2 actions",
        f"spends LLM tokens on {backend}" + (f" (last time {last.answer.get('tokens', 0):,} tokens)" if last and last.ok else ""),
        f"writes runs/{run_id}/ (answer.json, phases.jsonl, calls.jsonl, sar.md, bench.log)",
    )))
    if last and last.ok:
        st.caption(md(f"last time {common.secs(last.answer.get('latency_s'))} for this case in {last.source_id}"))
    ack = st.checkbox("I understand this writes to the shared graph and spends LLM tokens")
    if st.button("Start the run", type="primary", disabled=bool(problem) or not ack or not bs["ok"], icon=":material/play_arrow:"):
        res = common.launch_run([case_id], backend, run_id, session=common.approver_session())
        if not res["ok"]:
            st.error(md(res["error"]))
            return
        st.session_state["rerun_job"] = {"run": run_id, "case": case_id, "started": time.time(), "pid": res["pid"], "backend": backend}
        st.rerun()


def _note_lines(view: common.CaseView, limit: int = 1) -> list[str]:
    notes = [r for r in view.phases if r.get("kind") == "note"]
    return [str(r.get("text") or "") for r in notes[-limit:]]


@st.fragment(run_every="3s")
def _rerun_strip(job: dict, recorded: common.CaseView) -> None:
    """Progress of a live re-run started from this page. It reads the new run only; the recorded view never changes."""
    jv = common.case_view(job["run"], job["case"])
    with st.container(border=True, key="rerun-strip"):
        phases = [r.get("phase") for r in jv.phases if r.get("kind", "phase") == "phase" and r.get("phase")]
        bits = [f"re-run {job['run']}", f"elapsed {common.secs(time.time() - float(job.get('started') or time.time()))}",
                f"last phase {phases[-1] if phases else '—'}", f"{len(jv.calls)} calls so far"]
        st.markdown(f"{common.state_badge(jv.state)} " + md(" · ".join(bits)))
        last = _note_lines(jv)
        if last:
            st.caption(md(f"last note: {last[0]}"))
        if jv.error:
            st.code(jv.error, language=None, wrap_lines=True)
        if jv.ok:
            chips = []
            if recorded.ok:
                cls = common.agreement(jv.answer, recorded.answer)["cls"]
                chips.append(badge(f"{cls} vs {recorded.source_id}", common.AGREEMENT_COLORS.get(cls, "gray")))
            sub = common.load_promoted_answer(job["case"])
            if sub is not None:
                cls = common.agreement(jv.answer, sub)["cls"]
                chips.append(badge(f"{cls} vs submitted", common.AGREEMENT_COLORS.get(cls, "gray")))
            st.markdown(" ".join(chips))
            if st.button("Switch to it", key="rerun-switch", icon=":material/arrow_forward:"):
                common.goto("case", case=job["case"], run=job["run"])


def _show_rerun(ctx: common.Ctx, view: common.CaseView) -> None:
    job = st.session_state.get("rerun_job")
    if isinstance(job, dict) and job.get("case") == ctx.case_id and job.get("run") != ctx.source_id:
        _rerun_strip(job, view)


@st.fragment(run_every="3s")
def _running_card(source_id: str, case_id: str) -> None:
    v = common.answer_attempt_view(common.case_view(source_id, case_id))
    if v.state != "running":
        st.rerun()  # the answer (or a failure) landed: redraw the whole page
    phases = [r.get("phase") for r in v.phases if r.get("kind", "phase") == "phase" and r.get("phase")]
    ts = [_num(r.get("t")) for r in [*v.phases, *v.calls]]
    t_max = max((t for t in ts if t is not None), default=None)
    with st.container(border=True, key="state-running"):
        st.markdown(f"{common.state_badge('running')} **{md(case_id)}** · " + md("no verdict yet; the decision appears when answer.json is written"))
        st.markdown(md(f"phases reached: {', '.join(phases) or 'none'} · {len(v.calls)} calls so far · elapsed {common.secs(t_max)} in the trace"
                       + (f" · newest write {common.secs(v.last_write_age_s)} ago" if v.last_write_age_s is not None else "")))
        last = _note_lines(v)
        if last:
            st.caption(md(f"last note: {last[0]}"))


def _state_gate(ctx: common.Ctx, view: common.CaseView) -> None:
    """Exactly one card for a case without a parsed answer, then stop. Never another source's answer."""
    if view.state == "running":
        _running_card(view.source_id, view.case_id)
    elif view.state == "failed":
        with st.container(border=True, key="state-failed"):
            st.error(md(f"Case failed in {view.last_phase or 'an unrecorded phase'}; no answer was written"), icon=":material/error:")
            st.code(view.error, language=None, wrap_lines=True)
            bad = [c for c in view.calls if c.get("ok") is False]
            if bad:
                st.dataframe(pd.DataFrame([{"t": c.get("t"), "phase": c.get("phase"), "caller": c.get("caller"), "name": c.get("name"),
                                            "error": str(c.get("error") or "")} for c in bad]), hide_index=True, height=_height(len(bad)))
            else:
                st.caption("No failed calls in calls.jsonl")
    elif view.state == "incomplete":
        with st.container(border=True, key="state-incomplete"):
            st.warning(md(f"Incomplete: the trace stopped at {view.last_phase or 'no phase'} and no answer was written"), icon=":material/hourglass_disabled:")
            st.caption(md(f"newest write {common.secs(view.last_write_age_s)} ago · {len(view.calls)} calls recorded"))
    elif view.state == "unreadable":
        with st.container(border=True, key="state-unreadable"):
            st.error(md(f"{_file(view, 'answer.json')[0]} is unreadable; no verdict is shown"), icon=":material/error:")
            st.code(view.error, language=None, wrap_lines=True)
    else:
        with st.container(border=True, key="state-not-run"):
            st.info(md(f"{view.case_id} is not in {ctx.source_id}"), icon=":material/search_off:")
            holders = _holders(view.case_id, ctx.source_id)
            if not holders:
                st.caption("No other source holds this case")
            with st.container(horizontal=True):
                for h in holders:
                    if st.button(md(f"Open in {h.source_id}"), key=f"open-in-{h.source_id}", help=md(f"state there: {h.state}")):
                        common.goto("case", case=view.case_id, run=h.source_id)
    _show_rerun(ctx, view)
    st.stop()


def _agreement_line(ctx: common.Ctx, view: common.CaseView) -> str:
    """One sentence for how this case's answer compares with the same case in every other source, e.g. 'identical to the
    submitted answer · same decision in 5 other runs'; differences name their source."""
    holders = _holders(view.case_id, ctx.source_id)
    if not view.ok or not holders:
        return ""
    same, differs, other = 0, [], []
    submitted = ""
    for h in holders:
        if not h.ok:
            other.append(f"{h.source_id} {common.STATE_LABELS.get(h.state, h.state)}")
            continue
        cls = common.agreement(view.answer, h.answer)["cls"]
        if h.source_id in common.SUBMITTED:
            submitted = "identical to the submitted answer" if cls == "identical" else f"{cls} vs the submitted answer"
        elif cls in ("identical", "same decision"):
            same += 1
        else:
            differs.append(f"{h.source_id} ({cls})")
    parts = [submitted] if submitted else []
    if same:
        parts.append(f"same decision in {same} other run{'s' if same != 1 else ''}")
    if differs:
        parts.append("differs in " + ", ".join(differs))
    if other:
        parts.append("; ".join(other))
    return " · ".join(parts)


def _case_bar(ctx: common.Ctx, view: common.CaseView, states: list[dict], lc: dict) -> None:
    case_id = view.case_id
    queue = _queue(ctx)
    i = queue.index(case_id) if case_id in queue else -1
    backend = ctx.source.llm_backend if ctx.source and ctx.source.llm_backend in common.LLM_BACKENDS and ctx.source.llm_backend != "mock" else "gemini"
    blockers = _live_blockers(ctx, backend)
    with st.container(border=True, horizontal=True, vertical_alignment="center", key="casebar"):
        st.markdown(f"# {md(case_id)}")  # the screen's one h1: the case it is about
        if view.ok and view.state == "done":
            design.html_block(case_brief.verdict_html(view, states, lc))
        else:
            st.markdown(common.state_badge(view.state))
        st.space("stretch")
        if st.button("Prev", key="case-prev", icon=":material/chevron_left:", disabled=i <= 0):
            common.goto("case", case=queue[i - 1])
        if st.button("Next", key="case-next", icon=":material/chevron_right:", disabled=i < 0 or i >= len(queue) - 1):
            common.goto("case", case=queue[i + 1])
        common.page_link("graph", "Graph", icon=":material/hub:", case=case_id)
        common.page_link("approvals", "Approvals", icon=":material/approval:", case=case_id)
        clicked = st.button(md(f"Run {case_id} live"), key="run-live", type="primary", icon=":material/play_arrow:", disabled=bool(blockers),
                            help=md("Needs: " + "; ".join(blockers)) if blockers else md(f"Re-run {case_id} with {backend} into a new run folder"))
    # the blockers are the disabled button's tooltip, and the context strip already says this console reads files only
    if clicked and not blockers:
        last = view if view.ok else next((h for h in _holders(case_id, None) if h.ok), None)
        _confirm_rerun(case_id, backend, last)


def _provenance(ctx: common.Ctx, view: common.CaseView, stats: dict) -> None:
    ans = view.answer or {}
    case = ans.get("case") or {}
    src = ctx.source
    if view.source_id in common.SUBMITTED:
        path, when = _file(view, "")
        if view.promoted_from:
            run = view.promoted_from[0]
            with st.container(horizontal=True, vertical_alignment="center"):
                st.caption(md(f"Submitted answer {path} · identical to runs/{run}/{view.case_id}/answer.json"), help=md(f"compared as parsed JSON; file written {when}"))
                if st.button("Open the trace there", key="open-trace", icon=":material/timeline:"):
                    common.goto("case", case=view.case_id, run=run)
        elif stats["tokens_total"] == 0:
            st.caption(md(f"Submitted answer {path} · deterministic engine output · no LLM · no trace"
                          + ("" if case.get("written_to_graph") else " · not written to graph")))
        else:
            st.caption(md(f"Submitted answer {path} · no identical run answer · {stats['reads']} reads and {stats['tokens_total']:,} tokens"
                          " from the answer's own counters · no trace in this source"))
        return
    prov = common.run_provenance(view.source_id)
    calls_path, when = _file(view, "calls.jsonl")
    rbc = stats["reads_by_caller"]
    tg = common.tg_usage(view)
    chosen = ", ".join(tg["reads_by_caller"].get("llm", []))
    cap = prov.get("max_tool_calls")
    reads = f"{stats['reads']} reads ({rbc.get('harness', 0)} harness + {rbc.get('llm', 0)} LLM-chosen" + (f" of max {cap}" if cap else "") + (f": {chosen}" if chosen else "") + ")"
    match = {True: "matches answer.json", False: f"answer.json says {stats['answer_tool_calls']} reads / {stats['answer_tokens']} tokens", None: "no answer.json counters"}[stats["matches_answer"]]
    persist = "persist ok" if tg["persist_ok"] else "persist failed" if tg["persist_ok"] is False else "no persist rows"
    graph = f"written to graph ({persist})" if case.get("written_to_graph") else f"not written to graph ({persist})"
    kind = "Live run" if src and src.mode == "live" else "Mock run" if src and src.mode == "mock" else "Run"
    models = ", ".join(stats["models"]) or "no LLM rows"
    parts = [f"{kind} {view.source_id}", models, f"finished {_file(view, 'answer.json')[1]}", f"{reads} + {stats['writes']} write-backs",
             f"{stats['llm_calls']} LLM calls", f"{stats['tokens_total']:,} tokens incl. cache ({match})", common.secs(stats["latency_s"]), graph]
    help_text = f"Counted from {calls_path} (written {when}): reads are tool rows with caller != persist, tokens are input + output + cache read + cache write."
    if prov.get("model_mismatch"):
        help_text += f" Model note: {prov['model_mismatch']}"
    st.caption(md(" · ".join(parts)), help=md(help_text))
    if view.skipped_lines:
        st.caption(md(f"{view.skipped_lines} unparseable JSONL line(s) skipped in this case's trace"))


def _security_codes(view: common.CaseView, events: list[dict]) -> tuple[list[str] | None, str, str | None]:
    """(codes, where they come from, sanitised text) for the trigger: the run's P0 note, else the labelled snapshot."""
    scans = common.proof("intake_scans")
    data = scans["data"]
    row = None
    if isinstance(data, dict):
        row = data.get(view.case_id)
    elif isinstance(data, list):
        row = next((r for r in data if isinstance(r, dict) and r.get("case_id") == view.case_id), None)
    clean = str(row.get("clean_text")) if isinstance(row, dict) and row.get("clean_text") is not None else None
    alert = next((e for e in events if e["type"] == "security_alert"), None)
    if alert is not None:
        t = alert.get("t")
        return list(alert.get("codes") or []), f"phases.jsonl note at t={t:.2f} s" if t is not None else "phases.jsonl note", clean
    if view.has_trace:
        return [], "phases.jsonl: no security-alert note in this trace", clean
    if isinstance(row, dict):
        return list(row.get("codes") or []), f"snapshot {scans['generated_at'] or scans['as_of']}", clean
    return None, "not scanned in this deployment", None


def _card_mismatches(view: common.CaseView, events: list[dict]) -> tuple[list[dict], str | None]:
    """card_mismatch notes for this case, and the run they came from when this source has no trace of its own."""
    own = [e for e in events if e["type"] == "card_mismatch"]
    if own or view.has_trace or not view.promoted_from:
        return own, None
    run = view.promoted_from[0]
    return [e for e in common.guardrail_events(common.case_view(run, view.case_id)) if e["type"] == "card_mismatch"], run


def _brief(view: common.CaseView, events: list[dict], claims: list[dict], states: list[dict], lc: dict) -> None:
    """The first screen under the toolbar (case_brief): the case as a numbered scheme, the alert text verbatim and its
    defences, the indicators with the query behind each; beside them the decision on the fixed probability ramp, the why,
    the actions with their routes and where the case stands. Keys on the scheme light their indicator row."""
    meta = view.meta or {}
    case = (view.answer or {}).get("case") or {}
    mismatches, mismatch_run = _card_mismatches(view, events)
    investigated = next(((e.get("info") or {}).get("txn_card") for e in mismatches if (e.get("info") or {}).get("txn_card")), None)
    fam_fraud, fam_legit = common.families(view)
    with st.container(key="case-brief"):
        left, right = st.columns([1.72, 1], gap="large")
    with left:
        design.html_block(case_brief.scheme_html(view, claims, states, investigated))
        codes, where, clean = _security_codes(view, events)
        common.untrusted_text("Alert text", str(meta.get("trigger_text") or ""), codes=codes, clean_text=clean, source=where, key="trigger")
        design.html_block(case_brief.indicators_html(claims, fam_fraud, fam_legit))
        st.caption(md(f"customer {meta.get('customer_id') or '?'} · opened {meta.get('opened_at') or '?'} · risk score {meta.get('risk_score') or '—'}"))
        for e in mismatches:
            info = e.get("info") or {}
            origin = f" (from the trace in {mismatch_run})" if mismatch_run else ""
            st.warning(md(f"{e['text']}: case card {info.get('pack_card', '?')}, transaction record card {info.get('txn_card', '?')}{origin}"),
                       icon=":material/compare_arrows:")
        dq = common.proof("dq")
        taints = case_brief.dq_taints(view.case_id)
        if taints:
            label = f"snapshot {dq['generated_at'] or dq['as_of']}" if dq["mode"] == "snapshot" else dq["source"]
            for t in taints:
                st.caption(md(f"{t.get('code')} on {', '.join(map(str, t.get('entity_ids') or []))}: {t.get('reason', '')} → {t.get('impact', '')} ({label})"))
        elif dq["mode"] == "missing":
            st.caption("Data-quality taints: not available in this deployment")
        elif common.dq_covered(view.case_id):
            st.caption(md(f"Data-quality taints: none for this case ({dq['source']}, {dq['as_of']})"))
        else:
            st.caption(md(f"Data-quality taints: this case is not covered by the scan, which builds its taint matrix for the Final Round cases only ({dq['source']})"))
        waves = case_brief.detector_waves(view.case_id)
        if waves:
            bt = common.proof("backtest")
            common.page_link("detectors", f"{waves[0][0].split('_', 1)[0]} wave in Detectors & containment", icon=":material/radar:", case=view.case_id,
                             help=md(f"{bt['source']}, written {bt['as_of']}"))
        if case.get("summary") or case.get("pattern_description"):
            with st.expander("The agent's own summary (model-written)"):
                if case.get("summary"):
                    st.markdown(md(case["summary"]))
                if case.get("pattern_description"):
                    st.caption(md(f"Pattern description: {case['pattern_description']}"))
    with right:
        answer_src, answer_when = _file(view, "answer.json")
        design.html_block(case_brief.decision_html(view, states, lc, events, f"{answer_src} · {answer_when}"))
        p3 = common.phase_payload(view, "P3")
        moves = [{"move": str(a[0]), "Δp": _num(a[1])} for a in p3.get("adjustments") or [] if isinstance(a, list | tuple) and len(a) == 2]
        if moves:
            with st.expander(f"Score moves at P3 ({len(moves)})"):
                st.dataframe(pd.DataFrame(moves), hide_index=True, height=_height(len(moves)),
                             column_config={"move": st.column_config.TextColumn("Adjustment", width="large"), "Δp": st.column_config.NumberColumn(format="%+.2f")})
                st.caption(md(f"p_engine {common.prob(p3.get('p_engine'))} · calibrated {common.prob(p3.get('cal_p'))} · case-memory scorer {common.prob(p3.get('cms_p'))}"))


def _tg_panel(view: common.CaseView, stats: dict) -> None:
    tg = common.tg_usage(view)
    with st.container(key="tg-panel"):
        design.html_block(design.section(5, "What TigerGraph did", "reads, case memory and the write-back for this case"))
        if not tg["has_trace"]:
            st.caption(md("No trace recorded for this source: graph reads, vector hits and write-backs are only in a run's calls.jsonl"
                          + (f" (see {', '.join(view.promoted_from)})" if view.promoted_from else "")))
            return
        src, when = _file(view, "calls.jsonl")
        p_src, p_when = _file(view, "phases.jsonl")
        with st.container(border=True, key="strip-tg"):
            cols = st.columns(4)
        common.measured("Graph reads", stats["reads"], source=src, as_of=when, container=cols[0],
                        how="tool rows with caller != persist", what=f"{len(tg['reads_by_caller'].get('harness', []))} harness + {tg['llm_chosen']} LLM-chosen")
        common.measured("Write-backs", f"{stats['writes_ok']}/{stats['writes']} ok", source=src, as_of=when, container=cols[1], how="rows with caller == persist")
        closed = [h for h in tg["vector_hits"] if h.get("kind", "closed") == "closed"]
        common.measured("Similar closed cases", len(closed), source=p_src, as_of=p_when, container=cols[2], how="P1 similar_prior_cases, kind closed")
        common.measured("Grounding chunks", len(tg["grounding"]), source=p_src, as_of=p_when, container=cols[3], how="P1 grounding")
        for caller, names in tg["reads_by_caller"].items():
            head = f"{'chosen by the model' if caller == 'llm' else 'by the ' + caller} ({len(names)}" + (f" of max {tg['max_tool_calls']}" if caller == "llm" and tg["max_tool_calls"] else "") + ")"
            st.markdown(f"{badge(head, 'blue' if caller == 'llm' else 'gray')} " + md(", ".join(names)))
        if tg["vector_hits"]:
            rows = [{"id": h.get("id"), "kind": h.get("kind"), "outcome": h.get("outcome_or_verdict"), "pattern": h.get("pattern"),
                     "distance": _num(h.get("distance")), "overlap": ", ".join(map(str, h.get("overlap_reasons") or [])),
                     "note": "read back by vector search from an earlier write-back" if h.get("kind") == "agent" else ""} for h in tg["vector_hits"]]
            rows.sort(key=lambda r: (r["distance"] is None, r["distance"] or 0.0))
            st.markdown("**Vector search** " + md("(P1 similar_prior_cases)"))
            st.dataframe(pd.DataFrame(rows), hide_index=True, height=_height(len(rows), 320),
                         column_config={"distance": st.column_config.NumberColumn(format="%.3f"), "note": st.column_config.TextColumn(width="medium")})
        if tg["grounding"]:
            st.markdown("**Grounding chunks** (P1)")
            st.dataframe(pd.DataFrame(tg["grounding"]), hide_index=True, height=_height(len(tg["grounding"]), 240))
        if tg["persist"]:
            with st.expander(f"Write-backs ({len(tg['persist'])})"):
                st.dataframe(pd.DataFrame(tg["persist"]), hide_index=True, height=_height(len(tg["persist"])),
                             column_config={"latency_s": st.column_config.NumberColumn(format="%.3f")})
        vi = tg["vector_index_after_persist"]
        if vi:
            st.caption(md(f"{vi['text']}: ready={((vi.get('info') or {}).get('ready'))} at t={common.secs(vi.get('t'))}"))
        gid = str(((view.answer or {}).get("case") or {}).get("graph_case_id") or approvals_db.graph_case_id(view.case_id))
        if common.run_mode() == "live":
            if st.button(md(f"Read back {gid} now"), key="tg-readback", icon=":material/sync:"):
                conn, err = common.graph_connection()
                if conn is None:
                    st.warning(md(f"read-back unavailable: {err}"))
                else:
                    t0 = time.time()
                    try:
                        res = conn.getVerticesById("AgentCase", gid)
                    except Exception as exc:  # noqa: BLE001 - shown verbatim, the recorded view stays
                        st.warning(md(f"getVerticesById(AgentCase, {gid}) failed: {type(exc).__name__}: {exc}"))
                    else:
                        st.caption(md(f"read at {datetime.now():%Y-%m-%d %H:%M:%S} in {time.time() - t0:.2f} s"))
                        st.json([_collapse(v.get("attributes", v)) for v in res or []] or {"found": False}, expanded=False)
        else:
            st.caption(md(f"Reading {gid} back from the graph needs live mode"))


def _tab_decision_path(view: common.CaseView) -> None:
    if not view.phases:
        ans = view.answer or {}
        nba = ans.get("next_best_actions") or {}
        st.info(md("No trace recorded for this source" + (f"; the trace is in {', '.join(view.promoted_from)}" if view.promoted_from else "")),
                icon=":material/timeline:")
        st.markdown(md("The answer's own fields:"))
        st.markdown(" ".join(common.action_badge(str(a.get("action")), str(a.get("route", "auto"))) for a in nba.get("initial") or [] if isinstance(a, dict)) + " → "
                    + " ".join(common.action_badge(str(a.get("action")), str(a.get("route", "auto"))) for a in nba.get("final") or [] if isinstance(a, dict)))
        for label, value in (("What changed", nba.get("what_changed")), ("Stop reason", ans.get("stop_reason"))):
            if value:
                st.markdown(f"**{label}.** {md(value)}")
        return
    voi = common.voi_branches(view)
    p6 = common.phase_payload(view, "P6")
    taken = str((p6.get("simulated") or {}).get("outcome") or "")
    timeline = common.trace_timeline(view)
    by_code = {e["code"]: e for e in timeline}
    stages = {s["code"]: s for s in common.pipeline_state(view)["stages"]}
    pick = st.pills("Pipeline stage", ["all", *common.PIPELINE_CODES], default="all", required=True, key=STAGE_KEY, width="stretch",
                    format_func=lambda c: _stage_option(c, stages),
                    help="Pick a stage to see what it recorded: its calls with their params, its model calls, its notes and its payload. "
                         "S0 is the offline schema and data-quality gate.")
    st.caption(md("Stage states come from this case's phases.jsonl and calls.jsonl. 'Skipped' means the stage did not run "
                  "because P5 decided it was not needed: P6 and P7 when the case was settled before any evidence or no reply "
                  "could change the actions, P8 when no SAR was owed."))
    n_att = common.trace_attempts(view)
    if n_att > 1:
        shown = ("the attempt that wrote answer.json (it has the P9 row)" if "P9" in by_code
                 else "the newest attempt, which has no P9 row, so any answer.json is from an earlier attempt")
        st.caption(md(f"This folder holds {n_att} attempts (a rerun of the case appends to the same files); the decision path shows {shown}."))
    if pick in (None, "all"):
        _stage_s0(view, detail=False)
        for e in timeline:
            _phase_block(e, voi, p6, taken)
    elif pick == "S0":
        _stage_s0(view, detail=True)
    elif pick in by_code:
        _phase_block(by_code[pick], voi, p6, taken, stage=stages[pick])
    else:
        _stage_missing(view, stages[pick])


STAGE_KEY = "case_stage"  # the stage picked in the Decision path inspector


def _stage_option(code: str, stages: dict[str, dict]) -> str:
    if code == "all":
        return ":material/view_list: All stages"
    s = stages[code]
    color, icon = common.STAGE_STYLE.get(s["state"], ("gray", ":material/remove:"))
    return f":{color}[{icon}] {code} · {s['label']}"


def _phase_block(e: dict, voi: dict, p6: dict, taken: str, *, stage: dict | None = None) -> None:
    """One recorded phase: name, timing, sentence, call and model chips, notes. With `stage` (the inspector) also the code
    that ran it, every call with its params, the model calls and the payload, open."""
    with st.container(border=True, key=f"phase-{e['code']}"):
        timing = " · ".join(x for x in (f"t {e['t']:.2f} s" if e["t"] is not None else "", f"{e['duration']:.2f} s" if e["duration"] is not None else "") if x)
        sim = f"{badge('SIMULATED', 'gray')} " if _simulated(e["sentence"]) else ""
        state = badge(stage.get("state_label") or common.STAGE_LABELS.get(stage["state"], stage["state"]), common.STAGE_STYLE.get(stage["state"], ("gray", ""))[0]) + " " if stage else ""
        st.markdown(f"**{md(e['code'])} · {md(e['name'])}** {state}" + (badge(timing, "gray") if timing else "") + f"  \n{sim}{md(e['sentence'])}")
        if stage is not None:
            st.caption(md(f"Code: {stage['module']}"))
        chips = [badge(f"{SHORT_NAME.sub('', str(c['name']))} · {c['caller'] or '—'} · {common.secs(c['latency_s'])} · {'ok' if c['ok'] else 'failed'}",
                       "gray" if c["ok"] else "red") for c in e["calls"]]
        chips += [badge(f"{x['model'] or 'model not recorded'} · {x['tokens']:,} tok · {common.secs(x['latency_s'])}", "violet") for x in e["llm"]]
        if chips and stage is None:
            st.markdown(" ".join(chips))
        for n in e["notes"]:
            color = "red" if n["type"] in RED_NOTES else "orange" if n["type"] in ORANGE_NOTES else "gray"
            detail = {k: n[k] for k in ("codes", "violations", "dropped", "clamped", "llm", "problems", "attempt", "residual", "ref", "info") if k in n}
            st.markdown(f"{badge(n['type'].replace('_', ' '), color)} {md(n['text'])}" + (f" · {md(json.dumps(detail, default=str))}" if detail else ""))
            if n.get("error"):
                st.code(str(n["error"]), language=None, wrap_lines=True)
        if e["code"] == "P6" and voi:
            rows = [{"outcome": k, "taken": k == taken, "actions": ", ".join(map(str, v or []))} for k, v in voi.items()]
            st.dataframe(pd.DataFrame(rows).style.apply(lambda r: ["font-weight: 700" if r["taken"] else "" for _ in r], axis=1),
                         hide_index=True, height=_height(len(rows)), column_config={"taken": st.column_config.CheckboxColumn("taken")})
            if p6.get("counterfactual"):
                st.caption(md(f"counterfactual: {p6['counterfactual']}"))
        if e["code"] == "P4":
            basis = str((e["payload"].get("assessment") or {}).get("calibration_basis") or e["payload"].get("calibration_basis") or "")
            if basis:
                st.caption(md(f"calibration basis: {basis}"))
        if stage is None:
            with st.expander("Params and payload"):
                st.json({"payload": _collapse(e["payload"]), "calls": [{"name": c["name"], "params": c["params"]} for c in e["calls"]]}, expanded=False)
            return
        if e["calls"]:
            st.markdown("**Calls** " + md(f"({len(e['calls'])} in calls.jsonl)"))
            st.dataframe(pd.DataFrame([{"seq": c["seq"], "name": c["name"], "caller": c["caller"] or "—", "params": common.compact_params(c["params"], 400),
                                        "latency_s": c["latency_s"], "ok": c["ok"], "error": c["error"]} for c in e["calls"]]),
                         hide_index=True, width="stretch", height=_height(len(e["calls"])), column_config={"params": st.column_config.TextColumn(width="large")})
        if e["llm"]:
            st.markdown("**Model calls** " + md(f"({len(e['llm'])}; the model as calls.jsonl records it)"))
            st.dataframe(pd.DataFrame([{"phase": x["phase"], "model": x["model"] or "not recorded", "tokens": x["tokens"], "latency_s": x["latency_s"]} for x in e["llm"]]),
                         hide_index=True, width="stretch", height=_height(len(e["llm"])))
        if not e["calls"] and not e["llm"]:
            st.caption("No call recorded in this stage: it ran in-process (engine or policy code)")
        st.markdown("**Payload** " + md("(phases.jsonl)"))
        if e["payload"]:
            st.json(_collapse(e["payload"]), expanded=2)
        else:
            st.caption(md("No payload: this stage wrote no phase row" + ("; its result is the answer's sar block (SAR & citations tab)" if e["code"] == "P8" else "")))


def _stage_s0(view: common.CaseView, *, detail: bool) -> None:
    """S0 from the proof snapshot: this case's data-quality taint flags and the schema migration, whose gate status comes
    from data/schema_approvals.json (as on the Schema page) or the drift snapshot's copy. It runs offline in schema_engine/
    before the Final Round cases, so nothing here comes from the case's trace, and nothing is recomputed."""
    dq, drift = common.proof("dq"), common.proof("drift")
    dd = dq["data"] if isinstance(dq["data"], dict) else None
    matrix = (dd or {}).get("taint_matrix")
    taints = [t for t in (matrix.get(view.case_id) or []) if isinstance(t, dict)] if isinstance(matrix, dict) else None
    rd = drift["data"] if isinstance(drift["data"], dict) else None
    covered = common.dq_covered(view.case_id)
    parts = []
    if taints:
        parts.append(f"{len(taints)} data-quality taint flag(s) on this case: {', '.join(str(t.get('code')) for t in taints)}")
    elif taints is not None:
        parts.append("no data-quality taint flag on this case" if covered
                     else "not covered by the data-quality scan, which builds its taint matrix for the Final Round cases over January 2017 rows")
    else:
        parts.append(f"data-quality scan not readable here ({dq['error'] or 'missing'})")
    if rd is not None:
        mid = str(rd.get("migration_id") or "")
        live = common.proof("schema_approval")
        rec = live["data"].get(mid) if mid and isinstance(live["data"], dict) else None
        where = live["source"]
        if not isinstance(rec, dict):  # Railway ships no data/: the drift snapshot holds a copy of the record
            rec, where = (rd.get("approval") if isinstance(rd.get("approval"), dict) else None), "the drift snapshot's copy"
        gate = f"gate {(rec or {}).get('status') or 'not recorded'}" + (f" by {rec['approved_by']} at {rec.get('approved_at')}" if rec and rec.get("approved_by") else "")
        label = f"schema migration {mid or '?'}" if covered else f"the Final Round migration {mid or '?'} (not this case's gate)"
        parts.append(f"{label}: {len(rd.get('drifts') or [])} drift(s), {gate} (from {where})")
    with st.container(border=True, key="phase-S0"):
        st.markdown(f"**S0 · Schema & DQ gate** {badge('offline gate', 'violet')}  \n{md(' · '.join(parts))}")
        src = dq if dq["data"] is not None else drift
        st.caption(md(f"Runs offline in schema_engine/ before the Final Round cases; read from {src['source'] or 'the proof snapshot'}"
                      + (f" (generated {src['generated_at']}, git {src['git_sha'] or 'unknown'})" if src["generated_at"] else "") + ", not from this case's trace"))
        if not detail:
            return
        if taints:
            st.dataframe(pd.DataFrame([{"code": t.get("code"), "reason": t.get("reason"), "impact": t.get("impact")} for t in taints]), hide_index=True, width="stretch",
                         column_config={"reason": st.column_config.TextColumn(width="large"), "impact": st.column_config.TextColumn(width="large")})
        if rd is not None and rd.get("drifts"):
            st.dataframe(pd.DataFrame([{"drift": d.get("drift_id"), "title": d.get("title"), "severity": d.get("severity")} for d in rd["drifts"] if isinstance(d, dict)]),
                         hide_index=True, width="stretch")
        common.page_link("data", "Open Data quality & schema", icon=common.PAGE_ICONS["data"])


def _stage_missing(view: common.CaseView, s: dict) -> None:
    """A stage with no row in the case's trace files: say so, with the reason the trace itself gives when it gives one."""
    if s["state"] == "pending":
        st.info(md(f"{s['code']} · {s['label']}: not reached yet in this case's phases.jsonl or calls.jsonl"), icon=":material/info:")
    elif s.get("skip_reason"):
        st.info(md(f"{s['code']} · {s['label']}: skipped. {s['skip_reason'][0].upper()}{s['skip_reason'][1:]}."), icon=":material/info:")
    else:
        st.info(md(f"{s['code']} · {s['label']}: no row in this case's phases.jsonl or calls.jsonl"), icon=":material/info:")
    st.caption(md(f"Code: {s['module']}"))


def _tab_evidence(view: common.CaseView, claims: list[dict]) -> None:
    if not claims:
        st.caption("No evidence items in this answer")
        return
    traced = bool(view.calls)
    rows = []
    for c in claims:
        how_ok = c["call"] is not None or not str(c["ref"]).startswith("query:")
        rows.append({"#": c["n"], "claim": c["claim"], "direction": c["direction"], "family": c["family"], "ref": c["ref"], "entity ids": c["entity_ids"],
                     "matched call": c["how"] if c["call"] else "", "call ts": c["ts"] or c["when"], "what": bool(c["claim"]), "evidence": bool(c["entity_ids"]),
                     "how": how_ok and traced, "when": bool(c["ts"]), "simulated": _simulated(c["claim"]), "flags": "; ".join(c["flags"])})
    full = sum(1 for r in rows if r["what"] and r["evidence"] and r["how"] and r["when"])
    src, when = _file(view, "calls.jsonl" if traced else "answer.json")
    cols = st.columns([1, 3])
    common.measured("Claims fully attested", f"{full}/{len(rows)}", source=src, as_of=when, container=cols[0],
                    how="what = claim text, evidence = entity ids, how = matching calls.jsonl row, when = that row's ts")
    with cols[1]:
        agent = [c["n"] for c in claims if any(f.startswith("cites agent case") for f in c["flags"])]
        later = [c["n"] for c in claims if any(f.startswith("monitoring only") for f in c["flags"])]
        quoted = [c["n"] for c in claims if any(f.startswith("quotes a transaction") for f in c["flags"])]
        st.markdown(" ".join([
            badge("no agent case ids cited", "green") if not agent else badge(f"agent case ids in claims {agent}", "orange"),
            badge("every read is as of opened_at", "green") if not later else badge(f"claims {later} read past opened_at (monitoring only)", "orange"),
            badge("quoted trigger text matches this case", "green") if not quoted else badge(f"claims {quoted} quote another trigger", "red"),
        ]))
        if not traced:
            st.caption("No trace in this source: how and when are not recorded")
    st.dataframe(pd.DataFrame(rows), hide_index=True, height=_height(len(rows), 560), column_config={
        "claim": st.column_config.TextColumn(width="large"), "entity ids": st.column_config.ListColumn(), "matched call": st.column_config.TextColumn(width="medium"),
        "what": st.column_config.CheckboxColumn(), "evidence": st.column_config.CheckboxColumn(), "how": st.column_config.CheckboxColumn(), "when": st.column_config.CheckboxColumn(),
        "simulated": st.column_config.CheckboxColumn("SIMULATED", help="an assumed evidence reply, not a real one")})


def _tab_sar(view: common.CaseView) -> None:
    ans = view.answer or {}
    sar = ans.get("sar") or {}
    filed = bool(sar.get("file"))
    st.markdown(f"{badge('SAR filed', 'red', icon=':material/block:') if filed else badge('SAR not filed', 'gray')} {md(sar.get('reason') or '')}")
    src, when = _file(view, "answer.json")
    rag, err = common.optional_import("rag.validate_sar")
    if rag is None:
        st.caption(md(f"SAR validator: {err}"))
    else:
        resolver, where = common.sar_resolver()
        checks = common.sar_checks(ans, view.meta, resolver=resolver)
        ok = sum(1 for _, passed, _ in checks if passed)
        cols = st.columns([1, 3])
        common.measured("Validator", f"{ok}/{len(checks)}", source=f"rag.validate_sar over {src}", as_of=when, container=cols[0],
                        how=f"V00-V18 over the answer; only V00 applies when no SAR is filed; {where}")
        with cols[1]:
            for label, passed, detail in checks:
                if not passed:
                    st.markdown(f"{badge('fail', 'red')} {md(label)}" + (f" · {md(detail)}" if detail else ""))
            if ok == len(checks):
                st.caption("every check passes")
            st.caption(md(where[:1].upper() + where[1:]))
    for e in common.guardrail_events(view):
        if e["type"] in ("sar_validation_failed", "sar_fallback"):
            problems = "; ".join(map(str, e.get("problems") or []))
            st.markdown(f"{badge('P8 ' + e['type'].replace('_', ' '), 'orange')} {md(e['text'])}"
                        + (f" (attempt {md(e['attempt'])})" if e.get("attempt") is not None else "") + (f" · {md(problems)}" if problems else ""))
    narrative = str(sar.get("narrative") or "")
    if not narrative:
        st.caption("No narrative: nothing was filed")
        return
    st.markdown("**Narrative**")
    st.text(narrative)
    st.markdown("**Citations per sentence**")
    for i, s in enumerate(common.sar_citations(ans), 1):
        mark = badge(f"evidence {', '.join(f'#{n}' for n in s['evidence'])} · {', '.join(s['tokens'])}", "gray") if s["evidence"] else badge("no citation", "red")
        st.markdown(f"{i}\\. {md(s['sentence'])} {mark}")
    subjects = sar.get("subjects") or []
    if subjects:
        st.markdown("**Subjects** " + " ".join(badge(json.dumps(x, default=str) if isinstance(x, dict) else x, "gray") for x in subjects))


def _tab_approvals(view: common.CaseView, inbox: list[dict], notice: str | None) -> None:
    st.markdown("**Inbox rows for this case**")
    if notice:
        st.caption(md(notice))
    elif not inbox:
        st.caption("No inbox rows for this case")
    else:
        recommenders: dict[str, list[str]] = {}
        for v in [view, *_holders(view.case_id, view.source_id)]:
            final = ((v.answer or {}).get("next_best_actions") or {}).get("final") or [] if v.ok else []
            for a in final:
                if isinstance(a, dict) and a.get("route") in ("L1", "L2"):
                    recommenders.setdefault(str(a.get("action")), []).append(v.source_id)
        rows = [{"id": r.get("id"), "action": r.get("action"), "route": r.get("route"), "state": r.get("status"), "decided by": r.get("decided_by") or "",
                 "decided at": r.get("decided_at") or "", "recommended by": recommenders.get(str(r.get("action")), []),
                 "superseded": str(r.get("action")) not in recommenders} for r in inbox]
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=_height(len(rows)), column_config={
            "recommended by": st.column_config.ListColumn(), "superseded": st.column_config.CheckboxColumn(help="no source's current answer recommends this action")})
    st.markdown("**Audit log attempts**")
    try:
        audit = [r for r in approvals_db.audit_log_rows_ro(path=common.approvals_db_path()) if r.get("case_id") == view.case_id]
    except sqlite3.Error as exc:
        audit = []
        st.caption(md(f"audit log unreadable: {exc}"))
    if audit:
        st.dataframe(pd.DataFrame(audit), hide_index=True, height=_height(len(audit)))
    else:
        st.caption("No decision attempts logged for this case")
    lc = common.lifecycle(view, inbox)
    with st.container(border=True, key="lifecycle"):
        st.markdown(f"**Where it stands:** {md(lc['stage'])} · next owner {md(lc['next_owner'])}")
        if lc["open_items"]:
            st.markdown("\n".join(f"- {md(x)}" for x in lc["open_items"]))
        else:
            st.caption("No open items")


def _tab_raw(view: common.CaseView) -> None:
    if view.calls:
        st.markdown("**calls.jsonl**")
        phases = sorted({str(c.get("phase") or "") for c in view.calls}, key=lambda p: (len(p), p))
        callers = sorted({str(c.get("caller") or c.get("kind") or "") for c in view.calls})
        with st.container(horizontal=True):
            pick_p = st.pills("Phase", phases, selection_mode="multi", key="raw-phase", format_func=md)
            pick_c = st.pills("Caller", callers, selection_mode="multi", key="raw-caller", format_func=md)
        rows = []
        for c in view.calls:
            phase, caller = str(c.get("phase") or ""), str(c.get("caller") or c.get("kind") or "")
            if (pick_p and phase not in pick_p) or (pick_c and caller not in pick_c):
                continue
            size = sum(_count(c.get(k)) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")) if c.get("kind") == "llm" else _count(c.get("result_bytes"))
            rows.append({"t": _num(c.get("t")), "phase": phase, "caller": caller, "name": str(c.get("name") or c.get("model") or ""),
                         "params": json.dumps(_collapse(c.get("params")), default=str) if c.get("params") is not None else "",
                         "latency_s": _num(c.get("latency_s")), "bytes / tokens": size, "ok": c.get("ok") is not False, "error": str(c.get("error") or "")})
        rows.sort(key=lambda r: (r["t"] is None, r["t"] or 0.0))
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df.style.apply(lambda r: [FAILED_ROW if not r["ok"] else "" for _ in r], axis=1), hide_index=True, height=_height(len(rows), 480),
                         column_config={"t": st.column_config.NumberColumn(format="%.2f"), "latency_s": st.column_config.NumberColumn(format="%.3f"),
                                        "params": st.column_config.TextColumn(width="large"), "ok": st.column_config.CheckboxColumn()})
        else:
            st.caption("No calls match the filters")
    else:
        st.caption("No calls.jsonl in this source")
    if view.phases:
        st.markdown("**phases.jsonl**")
        rows = [{"kind": r.get("kind", "phase"), "phase": r.get("phase", ""), "name": r.get("name", ""), "t": _num(r.get("t")), "at": r.get("at", ""),
                 "text": r.get("text", ""), "payload keys": ", ".join(r["payload"]) if isinstance(r.get("payload"), dict) else ""} for r in view.phases]
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=_height(len(rows), 420), column_config={"t": st.column_config.NumberColumn(format="%.2f")})
    path, _ = _file(view, "answer.json")
    st.markdown(f"**{md(path)}**")
    st.json(view.answer or {}, expanded=False)
    if view.sar_md:
        st.markdown(f"**{md(_file(view, 'sar.md')[0])}**")
        st.text(view.sar_md)


# --------------------------------------------------------------------------- #
ctx = common.page_setup("case")
if not ctx.source_id or not ctx.case_id:
    st.info(md("No source or case to show on this deployment: pick a source with cases in the top bar"), icon=":material/folder_off:")
    st.stop()

view = common.case_view(ctx.source_id, ctx.case_id)
view = common.answer_attempt_view(view)  # one attempt for every panel: the one that wrote answer.json when a rerun appended more
inbox, inbox_notice = common.inbox_rows(ctx.case_id)

states = common.action_states(view, inbox)
lifecycle = common.lifecycle(view, inbox)
_case_bar(ctx, view, states, lifecycle)
if view.state != "done" or not view.ok:
    _state_gate(ctx, view)

stats = common.trace_stats(view)
events = common.guardrail_events(view)
claims = common.attest_claims(view)
case = view.answer.get("case") or {}

design.html_block(case_brief.checks_html(view, _agreement_line(ctx, view)))
_brief(view, events, claims, states, lifecycle)
_provenance(ctx, view, stats)
_show_rerun(ctx, view)

_tg_panel(view, stats)

design.html_block('<span id="case-tabs" class="tc-anchor"></span>')  # the checks line's jump link lands here
# Every tab is drawn on each run and switched in the browser (no rerun): a rerun per switch cleared the old panel and
# re-focused the clicked tab, and the browser scrolled the page under the reader. The five together take a fraction of
# a second to build.
tabs = st.tabs(TABS, key="case_tab", default=TABS[0] if view.phases else TABS[1])
with tabs[0]:
    _tab_decision_path(view)
with tabs[1]:
    _tab_evidence(view, claims)
with tabs[2]:
    _tab_sar(view)
with tabs[3]:
    _tab_approvals(view, inbox, inbox_notice)
with tabs[4]:
    _tab_raw(view)

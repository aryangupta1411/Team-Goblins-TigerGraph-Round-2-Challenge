"""Replay a recorded case on the Run console: the S0 + P0-P10 flow played back from the case's trace, in step with its
calls, and a stage inspector (PR #1's "live pipeline flow" and "clickable phase inspector", rebuilt on common's readers).

Everything comes from one run folder (phases.jsonl, calls.jsonl, answer.json) through common.case_view, trace_timeline
and pipeline_state, for the same attempt the timeline shows (common.answer_attempt_view). The steps are every call and
every phase row of that attempt in the order they were written (by t), each tagged with the stage that wrote it; a stage
counts as finished when its own row is written, P0 (whose row opens it) once a later stage starts, and P8 (which writes no
row) once a later stage writes one. A stage with no row (P6 and P7 without an evidence request, P8 without a SAR) is shown
as skipped, never filled in. Models are the ones calls.jsonl records. Nothing starts or writes, so it also renders on a
read-only deployment.
"""
from __future__ import annotations

import json
import re

import streamlit as st

from ui import common, design

md, badge = common.md, common.badge
RUN_KEY, CASE_KEY, STEP_KEY, PLAY_KEY, SPEED_KEY, STAGE_KEY = (
    "replay_run", "replay_case", "replay_step", "replay_playing", "replay_speed", "replay_stage")
SPEEDS = {"slow": 1.2, "normal": 0.6, "fast": 0.25}  # seconds per step
CONSOLE_LINES = 22
_PHASE_RE = re.compile(r"^(P\d+)")
def _cut(text: object, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _stage_of(phase: object) -> str:
    m = _PHASE_RE.match(str(phase or ""))
    return m.group(1) if m else ""


def _t(row: dict) -> float | None:
    t = row.get("t")
    return float(t) if isinstance(t, int | float) and not isinstance(t, bool) else None


def steps(view: common.CaseView) -> list[dict]:
    """The replay steps in the order the case wrote them: every call and every phase row of the attempt the timeline shows,
    by t (a phase row after the calls of the same instant), each tagged with its stage and the stages finished before it."""
    v = common.answer_attempt_view(view)
    by_code = {e["code"]: e for e in common.trace_timeline(v)}
    rows = [(_t(c), 0, i, "call", c) for i, c in enumerate(getattr(v, "calls", None) or [])]
    rows += [(_t(r), 1, i, "row", r) for i, r in enumerate(getattr(v, "phases", None) or []) if r.get("kind", "phase") == "phase" and r.get("phase")]
    rows.sort(key=lambda x: (x[0] is None, x[0] or 0.0, x[1], x[2]))
    out: list[dict] = []
    done: list[str] = []
    for t, _, _, kind, r in rows:
        stage = _stage_of(r.get("phase"))
        if stage and stage != "P0" and "P0" not in done:
            done.append("P0")  # P0's row opens it: it is finished once a later stage starts
        if kind == "call":
            out.append({"stage": stage, "done": list(done), "line": common.call_line(r, view.case_id),
                        "what": ("model " + str(r.get("model") or "not recorded")) if r.get("kind") == "llm" else f"tool {r.get('name') or '?'}"})
            continue
        if stage in ("P9", "P10") and "P8" in by_code and "P8" not in done:
            done.append("P8")  # P8 writes no row: a later stage's row closes it
        raw = str(r.get("phase") or "")
        if raw != "P5-voi" and stage != "P0" and stage not in done:
            done.append(stage)
        what = (f"{stage} value-of-information test written" if raw == "P5-voi"
                else f"{stage} {r.get('name') or common.PHASE_NAMES.get(stage, '')} row written") + ("" if t is None else f" at t+{t:.1f} s")
        sentence = (by_code.get(stage) or {}).get("sentence") if raw != "P5-voi" else ""
        out.append({"stage": stage, "done": list(done), "what": what, "line": f"{view.case_id} {what}" + (f": {_cut(sentence, 220)}" if sentence else "")})
    return out


def _flow(stages: list[dict], step: dict | None, finished: bool) -> None:
    """The stages as a row of boxes joined by arrows: done (green), the stage the step belongs to (blue, pulsing), not yet
    reached (grey), and stages the case never wrote (skipped). S0 is the offline schema and data-quality gate."""
    done = set(step["done"]) if step else set()
    active = None if finished or step is None else step["stage"]
    items = []
    for s in stages:
        if s["code"] == "S0":
            state = "offline"
        elif s["state"] == "skipped":
            state = "skipped"
        elif s["code"] == active and s["code"] not in done:
            state = "active"
        elif s["code"] in done or finished:
            state = "done"
        else:
            state = "pending"
        items.append({"code": s["code"], "label": s["label"], "state": state} | ({"state_label": s["state_label"]} if state == "skipped" and s.get("state_label") else {}))
    design.html_block(common.stage_flow_html(items))


def _inspector(view: common.CaseView, stages: list[dict]) -> None:
    """Pick a stage to see what it recorded: the code that ran it, its sentence and timing, every call with its params,
    the model calls as calls.jsonl records them, the notes and the phases.jsonl payload."""
    by_code = {e["code"]: e for e in common.trace_timeline(view)}
    states = {s["code"]: s for s in stages}

    def label(code: str) -> str:
        s = states[code]
        color, icon = common.STAGE_STYLE.get(s["state"], ("gray", ":material/remove:"))
        return f":{color}[{icon}] {code} · {s['label']}"

    pick = st.pills("Inspect a stage", list(common.PIPELINE_CODES), default="P3", key=STAGE_KEY, format_func=label,
                    help="Click a stage to see the result it recorded in this case's trace.")
    if pick is None:
        return
    s = states[pick]
    with st.container(border=True, key="replay-inspector"):
        st.markdown(f"**{md(pick)} · {md(s['label'])}** " + badge(s.get("state_label") or common.STAGE_LABELS.get(s["state"], s["state"]), common.STAGE_STYLE.get(s["state"], ("gray", ""))[0])
                    + (" " + badge("calls the LLM", "violet") if s.get("llm") else ""))
        st.caption(md(f"Code: {s['module']}"))
        if pick == "S0":
            dq = common.proof("dq")
            matrix = dq["data"].get("taint_matrix") if isinstance(dq["data"], dict) else None
            if isinstance(matrix, dict):
                taints = [t for t in matrix.get(view.case_id) or [] if isinstance(t, dict)]
                if taints or common.dq_covered(view.case_id):
                    st.markdown(md(f"{len(taints)} data-quality taint flag(s) on this case" + (": " + ", ".join(str(t.get("code")) for t in taints) if taints else "")))
                else:
                    st.markdown(md("Not covered by the data-quality scan, which builds its taint matrix for the Final Round cases only."))
                for t in taints:
                    st.caption(md(f"{t.get('code')}: {t.get('reason', '')} → {t.get('impact', '')}"))
            else:
                st.caption(md(f"The data-quality scan is not readable here ({dq['error'] or 'missing'})."))
            st.caption("S0 runs offline in schema_engine/ before the Final Round cases, so the case trace has no row for it.")
            common.page_link("data", "Open Data quality & schema", icon=common.PAGE_ICONS["data"])
            return
        e = by_code.get(pick)
        if e is None:
            skipped = s["state"] == "skipped"  # a later stage wrote rows: this one was passed over, not still ahead
            if s.get("rows"):
                st.caption(md(f"This stage has {s['rows']} call(s) in calls.jsonl but no phase row yet: the case is still in it, or stopped in it."))
                return
            st.caption(md("This stage did not run in this case: " + (
                s["skip_reason"] if skipped and s.get("skip_reason") else
                "no evidence request was made, so there was no assumed reply and no final re-decision" if skipped and pick in ("P6", "P7") else
                "no SAR was owed, so none was drafted" if skipped and pick == "P8" else "the case has not reached it")))
            return
        timing = " · ".join(x for x in (f"finished at t+{e['t']:.2f} s" if e["t"] is not None else "", f"took {e['duration']:.2f} s" if e["duration"] is not None else "") if x)
        if timing:
            st.caption(md(timing))
        if e["sentence"]:
            st.markdown(md(e["sentence"]))
        if e["calls"]:
            st.markdown("**Tool calls** " + md(f"({len(e['calls'])} in calls.jsonl)"))
            st.dataframe([{"seq": c["seq"], "name": c["name"], "caller": c["caller"] or "—", "params": common.compact_params(c["params"], 400),
                           "latency_s": c["latency_s"], "ok": c["ok"]} for c in e["calls"]],
                         hide_index=True, width="stretch", column_config={"params": st.column_config.TextColumn(width="large")})
        if e["llm"]:
            st.markdown("**Model calls** " + md(f"({len(e['llm'])}; the model as calls.jsonl records it)"))
            st.dataframe([{"phase": x["phase"], "model": x["model"] or "not recorded", "tokens": x["tokens"], "latency_s": x["latency_s"]} for x in e["llm"]],
                         hide_index=True, width="stretch")
        if not e["calls"] and not e["llm"]:
            st.caption("No call in this stage: it ran in-process (engine or policy code).")
        for n in e["notes"]:
            st.markdown(f"{badge(str(n.get('type', 'note')).replace('_', ' '), 'gray')} {md(n.get('text', ''))}")
        if e["payload"]:
            with st.expander("Payload (phases.jsonl)"):
                st.json(json.loads(json.dumps(e["payload"], default=str)), expanded=1)
        common.page_link("case", "Open this case's full decision path", icon=common.PAGE_ICONS["case"], case=view.case_id)


def _controls(n: int) -> None:
    def go(to: int, play: bool = False) -> None:
        st.session_state[STEP_KEY] = max(0, min(n, to))
        st.session_state[PLAY_KEY] = play

    step = st.session_state.get(STEP_KEY, n)
    playing = bool(st.session_state.get(PLAY_KEY))
    with st.container(horizontal=True, gap="small", vertical_alignment="bottom", key="replay-controls"):
        st.button("Restart", icon=":material/skip_previous:", on_click=go, args=(0,), key="replay-restart")
        if playing:
            # stop only: the playing fragment has moved the step on since these buttons were drawn
            st.button("Pause", icon=":material/pause:", on_click=lambda: st.session_state.update({PLAY_KEY: False}), key="replay-pause")
        else:
            st.button("Play", icon=":material/play_arrow:", type="primary", on_click=go, args=(0 if step >= n else step, True), key="replay-play")
        st.button("Step", icon=":material/arrow_forward:", on_click=go, args=(step + 1,), key="replay-step", disabled=playing or step >= n)
        st.button("Show all", icon=":material/skip_next:", on_click=go, args=(n,), key="replay-end", disabled=step >= n)
        st.segmented_control("Speed", list(SPEEDS), default="normal", key=SPEED_KEY, required=True)


def _frame(view: common.CaseView, stages: list[dict], evs: list[dict]) -> None:
    n = len(evs)
    step = max(0, min(n, int(st.session_state.get(STEP_KEY, n))))
    finished = step >= n
    cur = evs[step - 1] if step else None
    head = f"step {step} of {n}"
    if finished:
        status = badge("replayed", "green") + " " + md(f"{head} · every recorded step of {view.case_id} shown; pick a stage below to inspect it")
    elif cur is None:
        status = badge("ready", "gray") + " " + md(f"{head} · press Play to step through {view.case_id}'s trace")
    else:
        label = next((s["label"] for s in stages if s["code"] == cur["stage"]), "")
        status = badge("replaying", "blue", icon=":material/autorenew:") + " " + md(f"{head} · {cur['stage']} {label} · {cur['what']}")
    st.markdown(status)
    _flow(stages, cur if not finished else (evs[-1] if evs else None), finished)
    lines = [e["line"] for e in evs[:step]][-CONSOLE_LINES:]
    st.code("\n".join(lines) or "(press Play)", language=None)


def render(default_run: str | None = None, default_case: str | None = None) -> None:
    """The 'Replay a recorded case' section: run and case pickers, the flow with its controls and console, the inspector."""
    runs = [s for s in common.sources() if s.kind == "run" and s.cases]
    if not runs:
        st.caption("No recorded run under runs/ to replay.")
        return
    ids = [s.id for s in runs]
    if st.session_state.get(RUN_KEY) not in ids:
        canonical = common.default_source()  # the run the submitted answers were promoted from
        st.session_state[RUN_KEY] = default_run if default_run in ids else (canonical if canonical in ids else ids[0])
    c1, c2 = st.columns([2, 1])
    run_id = c1.selectbox("Recorded run", ids, key=RUN_KEY, format_func=lambda r: f"{r} · {next(s for s in runs if s.id == r).n_answered} answered")
    src = next(s for s in runs if s.id == run_id)
    if st.session_state.get(CASE_KEY) not in src.cases:
        st.session_state[CASE_KEY] = default_case if default_case in src.cases else src.cases[0]
    case_id = c2.selectbox("Case", src.cases, key=CASE_KEY)
    shown = (run_id, case_id)
    if st.session_state.get("replay_shown") != shown:  # a new case starts at its first step, paused
        st.session_state["replay_shown"] = shown
        st.session_state[STEP_KEY], st.session_state[PLAY_KEY] = 0, False
    view = common.case_view(run_id, case_id)
    if not view.has_trace:
        st.caption(md(f"{case_id} has no phases.jsonl or calls.jsonl in {run_id}, so there is nothing to replay."))
        return
    stages = common.pipeline_state(view)["stages"]
    evs = steps(view)
    with st.container(border=True, key="replay-panel"):
        _controls(len(evs))
        if st.session_state.get(PLAY_KEY):
            speed = SPEEDS.get(st.session_state.get(SPEED_KEY) or "normal", 0.6)

            @st.fragment(run_every=speed)
            def _playing() -> None:
                nxt = int(st.session_state.get(STEP_KEY, 0)) + 1
                if nxt > len(evs):
                    st.session_state[PLAY_KEY] = False
                    st.rerun(scope="app")  # the last step: redraw once without the timer, with Play back
                st.session_state[STEP_KEY] = nxt
                _frame(view, stages, evs)

            _playing()
        else:
            _frame(view, stages, evs)
        st.caption(md("Played back from phases.jsonl and calls.jsonl: each call where it finished, then the stage's own row. "
                      "A skipped stage did not run in this case; its inspector says why. The model names are the ones calls.jsonl records."))
    _inspector(view, stages)

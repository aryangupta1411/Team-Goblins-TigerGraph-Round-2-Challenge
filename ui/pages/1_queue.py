"""Queue: the 20 exam cases (case_pack.csv) with the state each one reached in the selected run.

"Investigate" runs the agent for one case or all 20 cases (live or mock via `python -m agent.bench`),
or replays the recorded tool calls from the run log (mock / demo fallback, PLAN §5).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Queue", page_icon=common.PAGE_ICONS["queue"], layout="wide")
common.inject_css()
common.page_header("Case queue", "The 20 exam cases and the state each one reached in the selected run.")

run_id = st.session_state.get("run_id") or (common.list_runs() or [None])[0]
st.session_state["run_id"] = run_id
pack = common.load_case_pack()
pack_ids = [m["case_id"] for m in pack]

f1, f2, f3 = st.columns([1, 1, 2])
trigger = f1.multiselect("Trigger", ["risk_score", "customer_report", "analyst_request"], default=[])
only_unrun = f2.checkbox("Only cases without an answer in this run", value=False)
f3.caption(f"Run **{run_id or '—'}** · cases run in `opened_at` order so later cases can retrieve earlier agent-written ones (PLAN §4.2).")

rows = []
for m in pack:
    cr = common.load_case_run(run_id, m["case_id"]) if run_id else None
    a = cr.answer if cr else None
    if trigger and m["trigger_type"] not in trigger:
        continue
    if only_unrun and a:
        continue
    pre, post = common.probability_pre_post(cr) if cr else (None, None)
    rows.append(
        {
            "case_id": m["case_id"],
            "opened_at": m["opened_at"],
            "trigger": m["trigger_type"],
            "card": m["card_id"],
            "flagged_txn": m["flagged_txn_id"],
            "risk": m.get("risk_score") or "",
            "verdict": a["case"]["verdict"] if a else "",
            "status": a["case"]["status"] if a else ("running" if cr and cr.calls else "not run"),
            "p_pre": common.pct(pre) if a else "",
            "p_post": common.pct(post) if a else "",
            "pattern": a["case"]["pattern"] if a else "",
            "exposure": common.money(a["case"]["exposure_usd"]) if a else "",
            "asked": len(a["evidence_requests"]) if a else 0,
            "changed": (a["next_best_actions"]["what_changed"] != "nothing") if a else False,
            "SAR": a["sar"]["file"] if a else False,
            "final actions": ", ".join(x["action"] for x in a["next_best_actions"]["final"]) if a else "",
        }
    )
st.dataframe(rows, width="stretch", hide_index=True)


def _replay_cases(target_cases: list[str], active_run: str | None, speed: float, box: st.delta_generator.DeltaGenerator) -> None:
    if not active_run:
        box.update(label="No run selected (run `make mock-run` or `agent.bench` first)", state="error")
        return
    total_calls = 0
    replayed_cases = 0
    for idx, cid in enumerate(target_cases, 1):
        cr = common.load_case_run(active_run, cid)
        if not cr or not cr.calls:
            box.write(f"`[{idx}/{len(target_cases)}]` **{cid}** — no recorded trace in run `{active_run}`")
            continue
        replayed_cases += 1
        if len(target_cases) > 1:
            box.write(f"#### [{idx}/{len(target_cases)}] {cid}")
        for c in cr.calls:
            total_calls += 1
            if c.get("kind") == "llm":
                box.write(
                    f"`{cid}` `{c.get('phase','')}` {common.badge('LLM', 'violet')} **{c.get('name')}** "
                    f"({c.get('model','')}) — {c.get('input_tokens',0)}+{c.get('output_tokens',0)} tokens, {c.get('latency_s','?')}s"
                )
            else:
                box.write(
                    f"`{cid}` `{c.get('phase','')}` {common.badge('TOOL', 'blue')} **tigergraph__run_installed_query** "
                    f"`{c.get('name')}` `{c.get('params',{})}` — {c.get('result_bytes','?')} B, {c.get('latency_s','?')}s"
                )
            if speed > 0:
                time.sleep(speed)
        for ph in cr.phases:
            if ph.get("phase") in ("P4", "P5", "P6", "P7"):
                box.write(f"`{cid}` **{ph.get('phase')} {ph.get('name')}** → `{str(ph.get('payload'))[:300]}`")
    if replayed_cases == 0:
        box.update(label=f"No recorded trace found in run `{active_run}` (make mock-run first)", state="error")
    else:
        label = f"Replayed {total_calls} calls across {replayed_cases} case(s)" if len(target_cases) > 1 else f"Replayed {total_calls} calls for {target_cases[0]}"
        box.update(label=label, state="complete")
        common.link_to("pages/2_case.py", label="See the evidence and the recommendation", icon=common.PAGE_ICONS["case"])


def _run_bench(
    target_cases: list[str],
    target_run: str,
    use_mock: bool,
    resume: bool,
    box: st.delta_generator.DeltaGenerator,
) -> None:
    cases_arg = "all" if len(target_cases) == len(pack_ids) else ",".join(target_cases)
    cmd = [sys.executable, "-m", "agent.bench", "--cases", cases_arg, "--run-id", target_run]
    if use_mock:
        cmd.append("--mock")
    if resume:
        cmd.append("--resume")
    box.write(f"`{' '.join(cmd)}`")
    proc = subprocess.Popen(cmd, cwd=str(common.ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen: dict[str, int] = {cid: 0 for cid in target_cases}
    done_ans: set[str] = set()
    while proc.poll() is None:
        for cid in target_cases:
            cr = common.load_case_run(target_run, cid)
            prev = seen.get(cid, 0)
            for c in cr.calls[prev:]:
                params = c.get("params", "") if c.get("kind", "tool") == "tool" else ""
                box.write(f"`{cid}` `{c.get('phase','')}` **{c.get('kind','tool')}** `{c.get('name')}` {params} — {c.get('latency_s','?')}s")
            seen[cid] = len(cr.calls)
            if cr.answer and cid not in done_ans:
                done_ans.add(cid)
                v = cr.answer.get("case", {}).get("verdict", "")
                p = common.pct(cr.answer.get("case", {}).get("fraud_probability"))
                box.write(f"**{cid} complete** ({len(done_ans)}/{len(target_cases)}) → verdict `{v}` ({p})")
        time.sleep(0.5)
    for cid in target_cases:
        cr = common.load_case_run(target_run, cid)
        prev = seen.get(cid, 0)
        for c in cr.calls[prev:]:
            params = c.get("params", "") if c.get("kind", "tool") == "tool" else ""
            box.write(f"`{cid}` `{c.get('phase','')}` **{c.get('kind','tool')}** `{c.get('name')}` {params} — {c.get('latency_s','?')}s")
        seen[cid] = len(cr.calls)
    out = proc.stdout.read() if proc.stdout else ""
    if proc.returncode == 0:
        st.session_state["run_id"] = target_run
        box.update(label=f"Done — {len(target_cases)} case(s) in run {target_run}", state="complete")
        common.link_to("pages/2_case.py", label="See the evidence and the recommendation", icon=common.PAGE_ICONS["case"])
    else:
        box.update(label="agent.bench failed", state="error")
        st.code(out[-4000:])


st.divider()
left, right = st.columns([1, 2])
with left:
    case_id = common.case_select(st, pack_ids, key="queue_case")
    meta = common.case_meta(case_id)
    st.markdown(f"**{meta.get('trigger_type','')}** · opened `{meta.get('opened_at','')}`")
    st.info(meta.get("trigger_text", ""))
    common.link_to("pages/2_case.py", label="Open the case view", icon=common.PAGE_ICONS["case"])

with right:
    default_mode = "mock" if common.read_only() else common.run_mode()  # read-only deployment: replay, never spawn agent.bench
    if common.read_only():
        exec_mode = "replay"
        st.markdown("**Investigate** — replays the recorded tool trace (read-only deployment)")
    else:
        mode_options = ["live", "mock_bench", "replay"] if default_mode == "live" else ["replay", "mock_bench", "live"]
        mode_labels = {
            "replay": "Replay recorded trace (instant)",
            "mock_bench": "Run agent.bench (mock / DuckDB facts)",
            "live": "Run agent.bench (live TigerGraph MCP)",
        }
        exec_mode = st.radio(
            "Execution mode",
            mode_options,
            format_func=lambda k: mode_labels[k],
            horizontal=True,
            key="queue_exec_mode",
        )
    if exec_mode == "replay":
        speed = st.slider("Replay speed (s per call)", 0.0, 2.0, 0.4, 0.1)
        target_run = run_id or ""
        resume_flag = False
    else:
        speed = 0.0
        o1, o2 = st.columns([2, 1])
        default_run = run_id if (run_id and run_id.startswith("ui-")) else f"ui-{time.strftime('%Y%m%d-%H%M%S')}"
        target_run = o1.text_input("Target run ID", value=default_run, help="Cases run into runs/<run_id>/; reuse the same ID to accumulate single cases.")
        resume_flag = o2.checkbox("Skip completed (--resume)", value=True)

    b1, b2 = st.columns(2)
    run_one = b1.button(f"▶ Investigate {case_id}", type="primary", width="stretch")
    run_all = b2.button(f"▶▶ Investigate all {len(pack_ids)} cases", width="stretch")

    if run_one or run_all:
        targets = pack_ids if run_all else [case_id]
        title = f"Investigating all {len(targets)} cases in opened_at order…" if run_all else f"Investigating {case_id}…"
        box = st.status(title, expanded=True)
        if exec_mode == "replay":
            _replay_cases(targets, run_id, speed, box)
        else:
            _run_bench(targets, target_run.strip() or f"ui-{time.strftime('%Y%m%d-%H%M%S')}", use_mock=(exec_mode == "mock_bench"), resume=resume_flag, box=box)

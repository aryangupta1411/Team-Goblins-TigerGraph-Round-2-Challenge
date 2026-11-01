"""Run console panels for where a run goes and what it investigates (ui/pages/1_queue.py renders them).

mode_panel: the session's Mock / Live switch and, in Live, the TigerGraph workspace: the one in .env or another one typed
in here (host, graph, GSQL secret) and checked with a read-only probe (token, vertex counts, missing queries). The
secret stays in this session's server memory and reaches only the processes this page starts for that workspace; it is
never written to disk or shown again.

case_rows: what to investigate besides the source's own case pack: alerts uploaded as a CSV (case-pack columns) or typed
in one at a time, validated by common.validate_case_pack; in Live, their cards can be checked against the workspace.

own_data_panel: a month of transactions in the Final Round's file layout, appended offline to the Jul-Dec 2016 base
snapshot (etl.delta build, its gates), exported as graph files and loaded into another workspace only (never the .env
one), then that workspace's queries and graph algorithms. Each step is a detached process with its own log; the
server-side guards live in common.launch_data_step.
"""
from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import streamlit as st

from ui import common, design

md, esc = common.md, design.esc
SOURCE_KEY = "rc_cases_source"   # 'pack' | 'upload' | 'typed' (a plain key, so it survives page changes)
UPLOAD_ROWS_KEY = "rc_upload_rows"
TYPED_ROWS_KEY = "rc_typed_rows"
CARDS_KEY = "rc_cards_check"
JOB_PICK_KEY = "rc_data_job"
SOURCES = {"pack": "Case pack", "upload": "Upload alerts", "typed": "Type an alert"}
TEMPLATE_ROW = {"case_id": "MY-001", "opened_at": "2017-01-15 01:25:19", "trigger_type": "customer_report", "flagged_txn_id": "3660108",
                "card_id": "C05061-K1", "customer_id": "C05061", "risk_score": "", "trigger_text": "Customer says they did not make this purchase."}


def _mirror(widget_key: str, plain_key: str) -> None:
    """Copy a widget's value into a plain session key: widget state is dropped when another page runs, plain keys stay."""
    st.session_state[plain_key] = st.session_state.get(widget_key)


# --------------------------------------------------------------------------- #
# Mode and workspace                                                          #
# --------------------------------------------------------------------------- #
def _env_mode() -> str:
    return "live" if (common._env("RUN_MODE", "mock").strip().lower() == "live") else "mock"


def _check_other() -> None:
    """The workspace form's submit: keep the typed values in plain keys and probe the workspace (reads only)."""
    other = {"host": str(st.session_state.get("rc_ws_host") or "").strip(), "graph": str(st.session_state.get("rc_ws_graph") or "FraudGraph").strip(),
             "secret": str(st.session_state.get("rc_ws_secret") or "")}
    if not other["secret"] and (st.session_state.get(common.SESSION_OTHER_KEY) or {}).get("host") == other["host"]:
        other["secret"] = st.session_state[common.SESSION_OTHER_KEY].get("secret", "")  # an empty field keeps the secret already given
    st.session_state[common.SESSION_OTHER_KEY] = other
    st.session_state[common.SESSION_CHECK_KEY] = common.check_workspace(other["host"], other["graph"], other["secret"], session=common.approver_session())
    st.session_state.pop("rc_ws_secret", None)  # the field never keeps the secret on screen


def _forget_other() -> None:
    for k in (common.SESSION_OTHER_KEY, common.SESSION_CHECK_KEY, "rc_ws_host", "rc_ws_graph", "rc_ws_secret"):
        st.session_state.pop(k, None)


def _check_html(chk: dict) -> str:
    if not chk:
        return '<p class="tc rc-ws-note">Not checked yet: the check reads only (a token, vertex counts, the installed queries).</p>'
    if not chk.get("ok"):
        return f'<p class="tc rc-ws-note red">Not connected · {esc(chk.get("error"))} · checked {esc(chk.get("at"))}</p>'
    counts = chk.get("counts") or {}
    main = " · ".join(f"{counts[k]:,} {k}" for k in ("Transaction", "Card", "Customer", "AgentCase") if k in counts)
    missing = chk.get("missing") or []
    q = ("installed queries not listed by this server" if chk.get("queries") is None
         else f"all {chk['queries']} of the agent's queries installed" if not missing
         else f"{len(missing)} of the agent's queries missing: {', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")
    empty = " · the graph is empty: load data into it below" if not any(counts.values()) else ""
    live = ' · <b class="red">this is the .env workspace (the live FraudGraph)</b>' if chk.get("live_workspace") else ""
    return (f'<p class="tc rc-ws-note"><span class="green">Connected</span> to {esc(common._hostname(chk.get("host", "")))} · '
            f'{esc(chk.get("graph"))} in {chk.get("latency_s")} s · {esc(main or "no vertices")}{esc(empty)} · {esc(q)}{live} · '
            f'checked {esc(chk.get("at"))}</p>')


def mode_panel() -> dict:
    """The Mock / Live switch and the workspace; returns common.run_target() after this render's choices."""
    picked = st.session_state.get(common.SESSION_MODE_KEY) or _env_mode()
    with st.container(border=True, key="rc-mode"):
        with st.container(horizontal=True, vertical_alignment="center", gap="medium", key="rc-mode-row"):
            st.segmented_control("Mode", ["mock", "live"], default=picked, key="rc_mode_pick", required=True,
                                 format_func=lambda m: "Mock · offline" if m == "mock" else "Live · TigerGraph + LLM",
                                 on_change=_mirror, args=("rc_mode_pick", common.SESSION_MODE_KEY),
                                 help="Mock runs on the local facts DB and the mock model: no graph writes, no tokens. Live runs the agent "
                                      "against a TigerGraph workspace with a real LLM backend. The choice holds for this browser session.")
            live = (st.session_state.get(common.SESSION_MODE_KEY) or picked) == "live"
            if live:
                st.radio("Workspace", ["env", "other"], key="rc_ws_pick", horizontal=True,
                         index=0 if st.session_state.get(common.SESSION_WS_KEY, "env") == "env" else 1,
                         format_func=lambda w: "The .env workspace" if w == "env" else "Another workspace",
                         on_change=_mirror, args=("rc_ws_pick", common.SESSION_WS_KEY),
                         help="The .env workspace holds the live FraudGraph with the Final Round evidence. Another workspace is "
                              "one you connect here for this session; uploaded data can be loaded only there.")
        target = common.run_target()
        if not live:
            design.html_block('<p class="tc rc-ws-note">Mock: agent.bench --mock on the local facts DB and the mock model; nothing reaches '
                              "TigerGraph and no LLM tokens are spent. Switch to Live to use a workspace and a real model.</p>")
        elif st.session_state.get(common.SESSION_WS_KEY, "env") == "env":
            host = common._env("TG_HOST").strip()
            ready = '<span class="green">Ready</span>' if target["live"] else '<span class="red">Not ready</span>'
            why = "live runs write their AgentCase back to this FraudGraph, the shared live workspace" if target["live"] else target["reason"]
            design.html_block(f'<p class="tc rc-ws-note">{ready} · {esc(common._hostname(host) or "TG_HOST unset")} · graph '
                              f'{esc(common._env("TG_GRAPHNAME", "FraudGraph"))} · {esc(why)}</p>')
        elif common.read_only():
            st.info("Another workspace cannot be connected on a read-only deployment.", icon=":material/lock:")
        elif common.approver_gate(common.approver_session(), "workspace"):
            st.info(md(f"Another workspace: {common.approver_gate(common.approver_session(), 'workspace')}."), icon=":material/lock:")
        else:
            other = st.session_state.get(common.SESSION_OTHER_KEY) or {}
            with st.form("rc_ws_form", border=False, enter_to_submit=False):
                c1, c2, c3 = st.columns([3, 1.2, 2], vertical_alignment="bottom")
                c1.text_input("Host", value=other.get("host", ""), key="rc_ws_host", placeholder="https://tg-….i.tgcloud.io")
                c2.text_input("Graph", value=other.get("graph", "FraudGraph"), key="rc_ws_graph")
                c3.text_input("GSQL secret", type="password", key="rc_ws_secret",
                              placeholder="kept for this session" if other.get("secret") else "",
                              help="Kept in this session's server memory only and passed to the processes this page starts for this "
                                   "workspace. Never written to disk or shown again. Leave empty to keep the one already given.")
                b1, b2 = st.columns([1, 4], vertical_alignment="center")
                b1.form_submit_button("Check connection", icon=":material/lan:", on_click=_check_other)
            design.html_block(_check_html(st.session_state.get(common.SESSION_CHECK_KEY) or {}))
            if other:
                st.button("Forget this workspace", key="rc_ws_forget", type="tertiary", icon=":material/close:", on_click=_forget_other)
            target = common.run_target()
    return target


# --------------------------------------------------------------------------- #
# What to investigate                                                         #
# --------------------------------------------------------------------------- #
def _cut(text: object, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _alerts_table(rows: list[dict]) -> None:
    st.dataframe(pd.DataFrame([{**{k: r.get(k, "") for k in ("case_id", "opened_at", "trigger_type", "card_id", "customer_id", "flagged_txn_id", "risk_score")},
                                "trigger_text": _cut(r.get("trigger_text", ""), 90)} for r in rows]),
                 hide_index=True, height=common.table_height(len(rows), 8), key=f"rc-alerts-{len(rows)}",
                 column_config={"trigger_text": st.column_config.TextColumn("trigger_text", help="shown as text; the agent sanitises it at intake")})


def _problems(res: dict) -> None:
    for p in res.get("problems") or []:
        st.error(md(p), icon=":material/block:")
    if res.get("warnings"):
        st.caption(md(" · ".join(res["warnings"][:5]) + (f" · and {len(res['warnings']) - 5} more" if len(res["warnings"]) > 5 else "")))


def _add_typed() -> None:
    row = {k: str(st.session_state.get(f"rc_t_{k}") or "").strip() for k in common.CASE_PACK_COLUMNS}
    have = st.session_state.get(TYPED_ROWS_KEY) or []
    res = common.validate_case_pack(common.case_pack_csv([*have, row]))
    st.session_state["rc_typed_check"] = res
    if not res["problems"]:
        st.session_state[TYPED_ROWS_KEY] = res["rows"]


def _cards_check(rows: list[dict]) -> None:
    found, err = common.cards_in_graph([r["card_id"] for r in rows])
    st.session_state[CARDS_KEY] = {"found": sorted(found) if found is not None else None, "error": err,
                                   "cards": sorted({r["card_id"] for r in rows}), "at": common._now()}


def case_rows(target: dict) -> tuple[str, list[dict] | None]:
    """('pack', None) for the source's own case pack, else ('upload' | 'typed', rows): the alerts this run investigates."""
    src = st.session_state.get(SOURCE_KEY, "pack")
    st.segmented_control("What to investigate", list(SOURCES), default=src, key="rc_src_pick", required=True, format_func=SOURCES.get,
                         on_change=_mirror, args=("rc_src_pick", SOURCE_KEY))
    src = st.session_state.get(SOURCE_KEY, "pack")
    rows: list[dict] | None = None
    if src == "upload":
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small", key="rc-upload"):
            up = st.file_uploader("Alerts CSV", type=["csv"], key="rc_upload_file", max_upload_size=2,
                                  help=f"One alert per row with the case-pack columns: {', '.join(common.CASE_PACK_COLUMNS)} (risk_score may be empty)")
            st.download_button("Template", common.case_pack_csv([TEMPLATE_ROW]), file_name="alerts_template.csv", mime="text/csv",
                               icon=":material/download:", type="tertiary", on_click="ignore")
        if up is not None:
            res = common.validate_case_pack(up.getvalue())
            _problems(res)
            st.session_state[UPLOAD_ROWS_KEY] = res["rows"]
        rows = st.session_state.get(UPLOAD_ROWS_KEY) or []
        if rows:
            _alerts_table(rows)
        elif up is None:
            st.caption("Upload a CSV of alerts, or start from the template.")
    elif src == "typed":
        with st.form("rc_typed_form", border=False, clear_on_submit=True, enter_to_submit=False):
            c1, c2, c3 = st.columns(3)
            c1.text_input("case_id", key="rc_t_case_id", placeholder="MY-001")
            c2.text_input("opened_at", key="rc_t_opened_at", placeholder="2017-01-15 01:25:19")
            c3.selectbox("trigger_type", common.TRIGGER_TYPES, key="rc_t_trigger_type")
            c4, c5, c6, c7 = st.columns(4)
            c4.text_input("card_id", key="rc_t_card_id", placeholder="C05061-K1")
            c5.text_input("customer_id", key="rc_t_customer_id", placeholder="C05061")
            c6.text_input("flagged_txn_id", key="rc_t_flagged_txn_id", placeholder="3660108")
            c7.text_input("risk_score", key="rc_t_risk_score", placeholder="optional, 0-1")
            st.text_area("trigger_text", key="rc_t_trigger_text", height=80, placeholder="What the alert says (treated as untrusted text)")
            st.form_submit_button("Add alert", icon=":material/add:", on_click=_add_typed)
        res = st.session_state.get("rc_typed_check")
        if res and res.get("problems"):
            _problems(res)
        rows = st.session_state.get(TYPED_ROWS_KEY) or []
        if rows:
            _alerts_table(rows)
            st.button("Clear typed alerts", key="rc_typed_clear", type="tertiary", icon=":material/delete:",
                      on_click=lambda: st.session_state.pop(TYPED_ROWS_KEY, None))
    if rows:
        if target.get("live"):
            st.button(f"Check these {len({r['card_id'] for r in rows})} card(s) in the workspace", key="rc_cards_btn", type="tertiary",
                      icon=":material/fact_check:", on_click=_cards_check, args=(rows,), help="getVerticesById(Card): a read; a card the graph lacks fails closed")
            chk = st.session_state.get(CARDS_KEY) or {}
            if chk and chk.get("cards") == sorted({r["card_id"] for r in rows}):
                if chk.get("found") is None:
                    st.caption(md(f"Cards not checked: {chk.get('error')}"))
                else:
                    gone = [c for c in chk["cards"] if c not in chk["found"]]
                    st.caption(md(f"{len(chk['cards']) - len(gone)} of {len(chk['cards'])} card(s) found in the workspace at {chk['at']}"
                                  + (f"; missing: {', '.join(gone[:10])} (those cases fail closed: the agent never guesses)" if gone else "")))
        else:
            st.caption("Mock runs read the local facts DB (data/): an alert whose card is not in it fails closed.")
    return src, (rows if src != "pack" else None)


# --------------------------------------------------------------------------- #
# Bring your own data: a month into another workspace                         #
# --------------------------------------------------------------------------- #
def _save_upload() -> None:
    files = {slot: (st.session_state.get(f"rc_m_{slot}").getvalue() if st.session_state.get(f"rc_m_{slot}") is not None else b"")
             for slot in common.MONTH_SLOTS}
    name = str(st.session_state.get("rc_m_name") or "").strip()
    res = common.save_month_upload(name, files, session=common.approver_session())
    st.session_state["rc_m_result"] = res
    if res["ok"]:
        st.session_state[JOB_PICK_KEY] = name


def _start_step(job: str, step: str) -> None:
    st.session_state["rc_step_result"] = {**common.launch_data_step(job, step, session=common.approver_session()), "step": step, "job": job}


def _use_pack(job: str) -> None:
    path = common.data_job_dir(job) / "month" / common.MONTH_SLOTS["case_pack"]
    res = common.validate_case_pack(path.read_bytes() if path.is_file() else b"")
    st.session_state["rc_m_pack_check"] = res
    if not res["problems"]:
        st.session_state[UPLOAD_ROWS_KEY] = res["rows"]
        st.session_state[SOURCE_KEY] = "upload"


STATE_TAG = {"done": "green", "failed": "red", "running": "ochre", "not run": ""}


def _shown(job: str, step: str) -> str:
    """A step's argv for display: the interpreter dropped, paths inside the repo relative to it."""
    root = f"{common.ROOT}/"
    return " ".join(a.replace(root, "") for a in common.data_step_command(job, step)[1:])


def _steps(job: str, target: dict, scrub: Callable[[str], str]) -> None:
    state = common.data_job_state(job)
    other_ok = target.get("live") and target.get("workspace") == "other" and not common.is_live_workspace(target.get("host", ""))
    held = common.data_job_lock()
    rows = []
    for i, step in enumerate(common.DATA_STEPS, 1):
        s = state["steps"][step]
        rows.append(f'<li><span class="n">{i}</span><span class="l">{esc(common.DATA_STEP_LABELS[step])}</span>'
                    f'{design.tag(s, STATE_TAG[s])}<code>{esc(scrub(_shown(job, step)))}</code></li>')
    design.html_block(f'<ol class="tc rc-steps">{"".join(rows)}</ol>')
    nxt = next((s for s in common.DATA_STEPS if state["steps"][s] != "done"), None)
    if held:
        st.caption(md(f"{common.DATA_STEP_LABELS.get(held.get('step'), held.get('step'))} is running for {held.get('job')} since {held.get('started')} (pid {held.get('pid')})"))
    elif nxt:
        graph = nxt in common.GRAPH_STEPS
        why = "" if not graph or other_ok else f"needs another workspace, switched to Live and checked above ({target.get('reason')})"
        ack = True
        if graph and other_ok:
            ack = st.checkbox(f"I understand this writes to {common._hostname(target['host'])} (graph {target.get('graph')})", key=f"rc_ack_{job}_{nxt}")
        st.button(f"{'Retry' if state['steps'][nxt] == 'failed' else 'Start'}: {common.DATA_STEP_LABELS[nxt]}", key=f"rc_step_{job}_{nxt}",
                  type="primary", icon=":material/play_arrow:", disabled=bool(why) or not ack, on_click=_start_step, args=(job, nxt))
        if why:
            st.caption(md(why))
    else:
        st.success("All steps done: the workspace holds the base snapshot plus this month.", icon=":material/check:")
        st.button("Investigate this upload's case pack", key=f"rc_use_pack_{job}", icon=":material/arrow_upward:", on_click=_use_pack, args=(job,),
                  help="Puts the upload's case_pack_final.csv under 'Upload alerts' in the start form above")
        res = st.session_state.get("rc_m_pack_check")
        if res:
            _problems(res)
    res = st.session_state.get("rc_step_result")
    if res and res.get("job") == job and not res.get("ok"):
        st.error(md(res["error"]), icon=":material/error:")
    last = state["running"] or next((s for s in reversed(common.DATA_STEPS) if state["steps"][s] != "not run"), None)
    if last:
        path = common.data_job_dir(job) / f"{last}.log"
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - 24_000))
                tail = fh.read().decode("utf-8", "replace").splitlines()[-40:]
        except OSError:
            tail = []
        st.caption(md(f"{common.DATA_STEP_LABELS[last]} · {common.rel(path)} (last 40 lines, secrets masked)"))
        st.code(scrub("\n".join(tail)) or "(empty)", language=None)


def own_data_panel(target: dict, scrub: Callable[[str], str]) -> None:
    design.html_block(design.section("", "Bring your own data", "a month of transactions into another workspace, never the live FraudGraph"))
    gate = common.approver_gate(common.approver_session())
    if gate:
        st.info(md(f"Uploads and data steps: {gate}."), icon=":material/lock:")
        return
    missing = common.data_pipeline_missing()
    if missing:
        st.info(md(f"Not on this deployment: {', '.join(missing)}. Loading your own data runs from a full checkout of the repository."),
                icon=":material/info:")
        return
    design.html_block(
        '<p class="tc rc-ws-note">Upload a month in the Final Round\'s file layout (transactions, identity, closed-case history, case pack). '
        "Step 1 appends it offline to the Jul-Dec 2016 base snapshot and runs the delta gates (duplicates, time order, history "
        "invariance, the case pack against the transactions); a failed gate stops there. Steps 3-5 write to another TigerGraph workspace "
        "connected above: the graph files, the agent's queries, the graph algorithms. Vectors are not loaded, so similar-case search "
        "(P1) finds nothing there until rag/embed runs against it. Other banks' column layouts: see Data quality &amp; schema.</p>")
    with st.expander("Upload a month", expanded=not common.data_jobs()):
        with st.form("rc_month_form", border=False, enter_to_submit=False):
            st.text_input("Upload name", key="rc_m_name", placeholder="feb-2017", help="A folder runs/_uploads/<name>/")
            c1, c2 = st.columns(2)
            c1.file_uploader("Transactions (transactions_*.csv)", type=["csv"], key="rc_m_transactions", max_upload_size=200)
            c2.file_uploader("Identity (identity_*.csv)", type=["csv"], key="rc_m_identity", max_upload_size=100)
            c3, c4 = st.columns(2)
            c3.file_uploader("Closed-case history", type=["csv"], key="rc_m_closed_cases", max_upload_size=100)
            c4.file_uploader("Case pack", type=["csv"], key="rc_m_case_pack", max_upload_size=2)
            st.form_submit_button("Save upload", icon=":material/upload:", on_click=_save_upload)
        res = st.session_state.get("rc_m_result")
        if res and not res["ok"]:
            st.error(md(res["error"]), icon=":material/error:")
        elif res:
            st.caption(md(f"Saved under {res['dir']}"))
    jobs = common.data_jobs()
    if not jobs:
        return
    if st.session_state.get(JOB_PICK_KEY) not in jobs:
        st.session_state[JOB_PICK_KEY] = jobs[0]
    job = st.selectbox("Upload", jobs, key=JOB_PICK_KEY, width=320)

    @st.fragment(run_every=3 if common.data_job_lock() else None)
    def _live_steps() -> None:
        _steps(job, target, scrub)

    _live_steps()

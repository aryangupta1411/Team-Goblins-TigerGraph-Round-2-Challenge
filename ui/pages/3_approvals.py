"""Approvals: the human gate. Every L1 / L2 action the policy gate queued waits here for a person whose role the server
checks (approvals_db.decide). The page answers what is waiting and whether anyone here can decide it.

Four lazy views (tab key approvals_tab): Inbox, a queue table with one row per gated action and the selected request
beside it; Enforcement & audit, the wrong-role drill in a sandbox, who decides what and the audit log; Other gates, the
schema migration and the detector proposals; Store, the SQLite file, loading gated actions and the analyst feedback log.

Viewing never writes: the inbox, the audit log and the feedback log are read through approvals_db's read-only helpers
(connect_ro), and every write starts from a confirm dialog. A decision is final; in live mode (not read-only) it is also
written to FraudGraph and read back. RBAC lives in approvals_db; a disabled button here is only a hint.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common, design  # noqa: E402

md, esc, glyph = common.md, design.esc, design.glyph

ctx = common.page_setup("approvals")
session = common.approver_session()
dirst = approvals_db.directory_status()
db_path = common.approvals_db_path()
db_rel, db_as_of = common.rel(db_path), common.as_of(db_path)
dir_rel = common.rel(dirst["path"])
ro = common.read_only()
graph_writes = ctx.mode == "live" and not ro  # DEPLOY_READONLY=1 keeps decisions local even in live mode
allowed_routes = approvals_db.ROLE_ALLOWED_ROUTES.get(session.role, set()) if session else set()
fail_closed = bool(dirst["error"]) or not dirst["n"]
GATE_ROLES = tuple(approvals_db.roles_with("approve_migration"))
SUBMITTED_FOR = {"HHF-": "submitted", "HHG-": "submitted-r1"}
ROUNDS = {"final": "Final Round", "round1": "Round 1", "all": "All rounds"}
STATUSES = ("pending", "approved", "rejected", "all")
STATUS_TAG = {"pending": "ochre", "approved": "green", "rejected": ""}  # design.tag kinds: ochre waits on a human
MIGRATION_ID = "round2_2017q1_migration"
ROLE_WORDS = approvals_db.ROLE_LABELS
ROUTE_NEEDS = {r: " or ".join(f"a {approvals_db.ROLE_LABELS[x]}" for x in approvals_db.roles_with(f"approve_{r}")) for r in ("L1", "L2")}
# audit outcome codes in plain words (the codes stay in the audit table's help)
OUTCOME_WORDS = {"ok": "decided", "denied_rbac": "refused: wrong role", "denied_auth": "refused: no valid sign-in",
                 "denied_immutable": "refused: already decided", "denied_signin": "sign-in refused", "error": "error"}
APPROVAL_DRILLS = ("ATK-08", "ATK-22", "ATK-23", "ATK-09")  # the approval attacks of agent.security; ATK-TAMPER is raw SQL
VERDICT_WORDS = {"fraud": "Fraud", "uncertain": "Uncertain", "legitimate": "Legitimate"}
REASON_CHARS = 300  # model-written reasons are cut near this many characters (whitespace collapsed first)
QUEUE_KEY, ROW_H = "ap-queue", 33  # the queue dataframe's key (its row selection) and row height in px


# --------------------------------------------------------------------------- #
# Reads (all read-only)                                                       #
# --------------------------------------------------------------------------- #
def _audit_rows() -> tuple[list[dict], str | None]:
    try:
        return approvals_db.audit_log_rows_ro(db_path), None
    except sqlite3.Error as exc:
        return [], f"audit log unreadable: {exc}"


def _k(text: object) -> str:
    """A widget-key fragment from a case or proposal id."""
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-") or "x"


def _round_of(case_id: object) -> str:
    cid = str(case_id or "")
    return "final" if cid.startswith("HHF-") else "round1" if cid.startswith("HHG-") else "other"


def _submitted_id(case_id: str) -> str | None:
    return next((sid for prefix, sid in SUBMITTED_FOR.items() if case_id.startswith(prefix)), None)


def _gated(answer: dict | None) -> dict[str, dict]:
    """action -> {route, reason} of the L1/L2 actions in next_best_actions.final."""
    if not isinstance(answer, dict):
        return {}
    final = (answer.get("next_best_actions") or {}).get("final") or []
    return {str(a.get("action")): {"route": str(a.get("route")), "reason": str(a.get("reason") or "")}
            for a in final if isinstance(a, dict) and a.get("route") in ("L1", "L2")}


def _answers(source_id: str | None) -> dict:
    if not source_id or common.get_source(source_id) is None:
        return {}
    return common.source_views(source_id)


inbox, inbox_err = common.inbox_rows()
audit, audit_err = _audit_rows()
src_id = ctx.source_id
src_views = _answers(src_id)
sub_views: dict = {}
for _sid in common.SUBMITTED:
    sub_views.update(src_views if _sid == src_id else _answers(_sid))
src_ans = {cid: v.answer for cid, v in src_views.items() if v.ok}
sub_ans = {cid: v.answer for cid, v in sub_views.items() if v.ok and _submitted_id(cid)}


def _view(case_id: str):
    for views in (src_views, sub_views):
        v = views.get(case_id)
        if v is not None and v.ok:
            return v
    return src_views.get(case_id) or sub_views.get(case_id) or common.case_view(_submitted_id(case_id) or src_id or "submitted", case_id)


def _recommended_by(case_id: str, action: str) -> list[tuple[str, str]]:
    """(source id, route) for every answer (selected source, submitted) whose final actions gate this action."""
    out = []
    sub_id = _submitted_id(case_id)
    for sid, answers in ((src_id, src_ans), (sub_id, sub_ans)):
        g = _gated(answers.get(case_id)).get(action)
        if sid and g and (sid, g["route"]) not in out:
            out.append((sid, g["route"]))
    return out


def _superseded(case_id: str, action: str) -> bool:
    """The submitted answer no longer gates this action: the row is no longer recommended and can only be rejected."""
    return case_id in sub_ans and action not in _gated(sub_ans[case_id])


def _non_fraud(case_id: str) -> list[str]:
    """'<source>: <verdict>' for every answer of this case whose verdict is not fraud."""
    sub_id = _submitted_id(case_id)
    out = []
    for sid, answers in ((src_id, src_ans), (sub_id, sub_ans)):
        a = answers.get(case_id)
        verdict = str(((a or {}).get("case") or {}).get("verdict") or "")
        if a and verdict != "fraud" and f"{sid}: {verdict}" not in out:
            out.append(f"{sid}: {verdict}")
    return out


def _num(x: object) -> float | None:
    try:
        return None if x is None or x == "" else float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _case_of(case_id: str) -> dict:
    """answer.case of the case's parsed answer ({} without one: then no verdict is ever shown)."""
    v = _view(case_id)
    return ((v.answer or {}).get("case") or {}) if v.ok else {}


def _exposure(case_id: str) -> float | None:
    return _num(_case_of(case_id).get("exposure_usd"))


def _audit_for(approval_id: int) -> dict | None:
    rows, _ = _audit_rows()
    return next((a for a in rows if a.get("approval_id") == approval_id), None)


def _squash(text: object) -> str:
    return " ".join(str(text or "").split())


def _clip(text: object, limit: int = REASON_CHARS) -> tuple[str, bool]:
    """Model-written text with its whitespace collapsed and cut at a word near `limit`; True when it was cut."""
    s = _squash(text)
    if len(s) <= limit:
        return s, False
    return s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…", True


def _current_gate(case_id: str, action: str) -> tuple[str | None, dict | None]:
    """(source, {route, reason}) of this action in the current answer: the submitted one, else the selected source's.
    The gate is None when that answer does not hold the action as L1/L2."""
    for sid, answers in ((_submitted_id(case_id), sub_ans), (src_id, src_ans)):
        if sid and case_id in answers:
            return sid, _gated(answers[case_id]).get(action)
    return None, None


def _earlier_attempt(row: dict) -> tuple[str | None, dict | None]:
    """(source, its gate) when the current answer still gates the row's action but with another reason or route: the
    row was queued by an earlier attempt of the case (HHF-003's first attempt, for one). (None, None) otherwise."""
    sid, g = _current_gate(str(row.get("case_id")), str(row.get("action")))
    if g is None or (_squash(g["reason"]) == _squash(row.get("reason")) and g["route"] == row.get("route")):
        return None, None
    return sid, g


def _answer_file(v) -> tuple[str, str]:
    """(display path, as-of time) of the answer file a view was read from."""
    if v.source_id in common.SUBMITTED:
        return f"cases/{v.case_id}.json", common.as_of(common.cases_dir() / f"{v.case_id}.json")
    p = (v.dir or common.runs_root() / v.source_id / v.case_id) / "answer.json"
    return f"runs/{v.source_id}/{v.case_id}/answer.json", common.as_of(p)


def _page_link(page_key: str, label: str, icon: str, case: str, run: str | None) -> None:
    """st.page_link carrying ?run=<the source the decision was read from>&case= (common.page_link always carries the
    session's run). A caption when the page cannot be linked, as when this page runs alone."""
    from streamlit.errors import StreamlitAPIException, StreamlitPageNotFoundError

    qp = {k: v for k, v in {"run": run or st.session_state.get("run_id"), "case": case}.items() if v}
    try:
        st.page_link(common.UI_DIR / common.PAGES[page_key]["file"], label=label, icon=icon, query_params=qp)
    except (StreamlitPageNotFoundError, StreamlitAPIException):
        st.caption(md(f"{label}: open {common.PAGES[page_key]['title']} from the top bar"))


def _role_word(role: str | None) -> str:
    return ROLE_WORDS.get(str(role or ""), str(role or "unknown role"))


def _decide_block(route: str, superseded: bool = False, approve: bool = True) -> str | None:
    """Why this session may not decide a row on `route` (None when it may). Only a hint: approvals_db.decide checks the
    session and the role again on the server and logs every attempt."""
    if dirst["error"]:
        return "Approver directory unreadable: nobody can decide (fail closed)"
    if fail_closed:
        return "No approver directory: nobody can decide (fail closed)"
    if session is None:
        return f"Sign in as {ROUTE_NEEDS.get(route, 'an approver')} to decide"
    if route not in allowed_routes:
        return f"Needs {ROUTE_NEEDS.get(route, route)} ({route}); signed in as {_role_word(session.role)}"
    if approve and superseded:
        return "No longer recommended by the submitted answer: reject only"
    return None


# --------------------------------------------------------------------------- #
# Pieces of one request (the Inbox detail and the confirm dialog share them)  #
# --------------------------------------------------------------------------- #
def _sar_state(row: dict, ans: dict) -> tuple[str, str]:
    """(text, css class) of the case's SAR: a drafted SAR is filed only once L2 approves its FILE_REPORT row."""
    if not (ans.get("sar") or {}).get("file"):
        return "not filed", "tc-muted"
    fr = next((r for r in inbox if r.get("case_id") == row.get("case_id") and r.get("action") == "FILE_REPORT"), None)
    if fr is None:
        return "drafted · FILE_REPORT is not queued, so nothing is filed", "ap-warn"
    if fr.get("status") == "pending":
        return ("drafted · filed only if this request is approved" if fr.get("id") == row.get("id")
                else f"drafted · filed only once L2 approves FILE_REPORT (request #{fr.get('id')})"), "ap-wait"
    if fr.get("status") == "approved":
        return f"approved for filing by {fr.get('decided_by')} at {fr.get('decided_at')}", "ap-done"
    return "drafted · FILE_REPORT rejected, so nothing is filed", "tc-muted"


def _decision_html(row: dict, v, ans: dict) -> str:
    """The decision behind a request: verdict and p(fraud) on the fixed 0-1 ramp, exposure, SAR and the answer file."""
    case = ans.get("case") or {}
    verdict = str(case.get("verdict") or "")
    p_final = _num(case.get("fraud_probability"))
    ramp = design.ramp(_num(common.phase_payload(v, "P3").get("p_engine")), _num(common.phase_payload(v, "P4").get("fraud_probability")),
                       p_final, verdict)
    sar_text, sar_cls = _sar_state(row, ans)
    path, when = _answer_file(v)
    return (f'<div class="tc ap-dec"><div class="ap-verdict {esc(verdict)}"><span class="tc-dot {esc(verdict or "none")}"></span>'
            f'<b>{esc(VERDICT_WORDS.get(verdict, "No verdict"))}</b><span class="ap-p">p(fraud) <b>{esc(common.prob(p_final))}</b></span></div>'
            f'{ramp}<dl class="ap-facts"><dt>Exposure</dt><dd>{esc(common.money(case.get("exposure_usd")))}</dd>'
            f'<dt>SAR</dt><dd class="{sar_cls}">{esc(sar_text)}</dd>'
            f'<dt>Answer</dt><dd class="tc-muted"><span class="tc-mono">{esc(path)}</span> · {esc(when)}</dd></dl></div>')


def _refs_html(ans: dict, n: int = 4) -> str:
    ev = [e for e in (ans.get("case") or {}).get("evidence") or [] if isinstance(e, dict) and e.get("ref")]
    if not ev:
        return '<p class="tc ap-line tc-muted">No evidence refs in this answer.</p>'
    items = "".join(f'<li class="tc-mono">{esc(e.get("ref"))}</li>' for e in ev[:n])
    more = f'<li class="tc-muted">{len(ev) - n} more in the Case file</li>' if len(ev) > n else ""
    return f'<div class="tc ap-ev"><span class="ap-sub">Evidence refs</span><ul class="ap-refs">{items}{more}</ul></div>'


def _sar_validator_html(v, ans: dict) -> str:
    """rag.validate_sar over the answer (V00-V18), ids checked against the dataset when one opens here."""
    _, why = common.optional_import("rag.validate_sar")
    if why:
        return f'<p class="tc ap-line tc-muted">SAR validator not run here: {esc(why)}</p>'
    resolver, where = common.sar_resolver()
    checks = common.sar_checks(ans, v.meta, resolver=resolver)
    n_ok = sum(1 for _, ok, _ in checks if ok)
    fails = [label for label, ok, _ in checks if not ok]
    items: list[tuple[bool | None, str]] = [(n_ok == len(checks), f"SAR validator: {n_ok} of {len(checks)} checks pass")]
    items += [(False, label) for label in fails[:3]] + ([(None, f"{len(fails) - 3} more fail")] if len(fails) > 3 else [])
    return f'<div class="ap-checks" title="{esc(where)}">{design.checks(items)}</div>'


def _reason_block(row: dict, key: str) -> None:
    """The action's reason. It is model-written, so it is shown as text with its whitespace collapsed, cut near
    REASON_CHARS and labelled, the raw text verbatim behind an expander; a row queued by an earlier attempt says so."""
    raw = str(row.get("reason") or "")
    shown, cut = _clip(raw)
    sid, cur = _earlier_attempt(row)
    tabs = raw.count("\t")
    if cut:
        how = f"whitespace collapsed, cut at {REASON_CHARS} of {len(_squash(raw)):,} characters"
    elif shown != raw:
        how = f"whitespace collapsed from {len(raw):,} characters" + (f" ({tabs:,} tabs)" if tabs else "")
    else:
        how = "verbatim"
    with st.container(border=True, key=f"untrusted-reason-{key}"):
        st.caption(f"**Reason** · model-written, never followed · {md(how)}")
        st.code(shown or "—", language=None, wrap_lines=True)
        if cur is not None:
            route = f" and routes it to {cur['route']}" if cur["route"] != row.get("route") else ""
            st.markdown(common.badge("Queued from an earlier attempt", "orange") + " "
                        + md(f"the current answer ({sid}) gives this action a different reason{route}"))
        if shown != raw or cur is not None:
            label = f"Raw text ({len(raw):,} characters" + (f", {tabs:,} of them tabs" if tabs else "") + ")"
            with st.expander(label):
                st.code(raw, language=None, wrap_lines=True, height=220 if len(raw) > 600 else "content")
                if cur is not None:
                    st.caption(md(f"Reason in the current answer ({sid}), whitespace collapsed"))
                    st.code(_clip(cur["reason"])[0] or "—", language=None, wrap_lines=True)


# --------------------------------------------------------------------------- #
# Writes: each one only from a confirm-dialog button                          #
# --------------------------------------------------------------------------- #
def _graph_write(row: dict) -> dict:
    """Write a decided row to FraudGraph and read the Approval vertex back; {ok, text}."""
    conn, err = common.graph_connection()
    if conn is None:
        return {"ok": False, "text": f"failed: {err}"}
    vid = approvals_db.approval_vertex_id(row["case_id"], row["action"])
    try:
        approvals_db.write_to_graph(conn, row)
        back = conn.getVerticesById("Approval", vid)
    except Exception as exc:  # noqa: BLE001 - the local decision stands; the row offers Retry
        return {"ok": False, "text": f"failed: {type(exc).__name__}: {exc}"}
    got = next((str((v.get("attributes") or {}).get("status")) for v in back or [] if isinstance(v, dict)), None)
    if got != row["status"]:
        return {"ok": False, "text": f"failed: read back {vid} status {got!r}, expected {row['status']!r}"}
    return {"ok": True, "text": f"written and read back ({vid})"}


def _apply_decision(row: dict, status: str) -> None:
    results = st.session_state.setdefault("ap_results", {})
    try:
        updated = approvals_db.decide(row["id"], status, session, path=db_path)
    except PermissionError as exc:
        a = _audit_for(row["id"]) or {}
        outcome = str(a.get("outcome", "?"))
        results[row["id"]] = {"ok": False, "text": f"Refused by the server: {exc} · audit #{a.get('id', '?')} ({OUTCOME_WORDS.get(outcome, outcome)})"}
        return
    except KeyError:
        results[row["id"]] = {"ok": False, "text": f"Request #{row['id']} no longer exists"}
        return
    a = _audit_for(row["id"]) or {}
    results[row["id"]] = {"ok": True, "text": f"{status} by {updated['decided_by']} at {updated['decided_at']} · audit #{a.get('id', '?')}"}
    if graph_writes:
        st.session_state.setdefault("ap_graph", {})[row["id"]] = _graph_write(updated)


# The open dialog lives in session_state["ap_dialog"] and is drawn at the end of every run until Confirm, Cancel or the
# close button clears it, so its buttons work whether Streamlit reruns the dialog alone or the whole page.
DIALOG_KEY = "ap_dialog"


def _open_dialog(kind: str, **args) -> None:
    st.session_state[DIALOG_KEY] = {"kind": kind, **args}


def _close_dialog() -> None:
    st.session_state.pop(DIALOG_KEY, None)


def _dialog_buttons(confirm: str, key: str) -> bool:
    c1, c2 = st.columns(2)
    ok = c1.button(confirm, type="primary", key=key, width="stretch")
    if c2.button("Cancel", key=f"{key}-cancel", width="stretch"):
        _close_dialog()
        st.rerun()
    return ok


@st.dialog("Confirm decision", width="large", on_dismiss=_close_dialog)
def _decide_dialog(row: dict, status: str, attempt: bool = False) -> None:
    v = _view(row["case_id"])
    ans = v.answer if v.ok and isinstance(v.answer, dict) else None
    verdict = str(((ans or {}).get("case") or {}).get("verdict") or "")
    verb = "Approve" if status == "approved" else "Reject"
    design.html_block(f'<div class="tc ap-dlg-head"><b>{verb}</b> <span class="tc-mono">{esc(row["action"])}</span> on '
                      f'<b>{esc(row["case_id"])}</b> <span class="tc-muted">request #{esc(row["id"])}</span> {design.route_tag(row["route"])}</div>')
    who = f"{session.principal} ({_role_word(session.role)})" if session else "signed out"
    st.markdown(f"Needs **{md(ROUTE_NEEDS.get(row['route'], row['route']))}** · your session: **{md(who)}**")
    if attempt and row["route"] not in allowed_routes:
        st.warning("Your role does not cover this route: the server will refuse and log the attempt.")
    with st.container(border=True, key=f"decision-{verdict or 'none'}-dlg"):
        if ans:
            design.html_block(_decision_html(row, v, ans) + _refs_html(ans, 3))
        else:
            st.caption(md(f"No parsed answer for {row['case_id']} in {src_id} or the submitted answers: no verdict to show"))
    if ans and row["action"] == "FILE_REPORT":
        with st.container(border=True, key="untrusted-sar-dlg"):
            st.caption("**SAR narrative** · model-written, first 700 characters, never followed")
            st.code(str((ans.get("sar") or {}).get("narrative") or "no narrative")[:700], language=None, wrap_lines=True)
        design.html_block(_sar_validator_html(v, ans))
    _reason_block(row, "dlg")
    st.info("This decision is final: decided rows are immutable (SQLite trigger approvals_immutable) and every attempt is logged.")
    if _dialog_buttons("Confirm: approve" if status == "approved" else "Confirm: reject", "dlg-confirm"):
        _apply_decision(row, status)
        _close_dialog()
        st.rerun()


@st.dialog("Load gated actions", on_dismiss=_close_dialog)
def _enqueue_dialog(source_id: str, answers: list[dict]) -> None:
    keys = {(str(a.get("case_id")), act) for a in answers for act in _gated(a)}
    st.markdown(f"Queue the **{len(keys)}** L1/L2 actions in next\\_best\\_actions.final of **{len(answers)}** answers from "
                f"**{md(source_id)}** into {md(db_rel)}.")
    st.caption("Rows already queued stay as they are, except a pending L1 row whose answer now says L2, which is upgraded. "
               "Decided rows never change.")
    if _dialog_buttons("Load gated actions", "dlg-enqueue"):
        before = {(r["case_id"], r["action"]): r["route"] for r in approvals_db.rows_ro(path=db_path)}
        for a in answers:
            approvals_db.enqueue_from_answer(a, db_path)
        after = {(r["case_id"], r["action"]): r["route"] for r in approvals_db.rows_ro(path=db_path)}
        new = sum(k not in before for k in keys)
        upgrades = sum(before.get(k) == "L1" and after.get(k) == "L2" for k in keys)
        st.session_state["ap_import_msg"] = f"{new} new · {upgrades} route upgrades · {len(keys) - new - upgrades} already queued (from {source_id})"
        _close_dialog()
        st.rerun()


def _gate_apply(gate_id: str, decision: str):
    """The schema or detector module's own decision function; both check the session's role themselves."""
    if gate_id == MIGRATION_ID:
        drift, why = common.optional_import("schema_engine.drift")
        if drift is None:
            raise OSError(why)
        return drift.approve_schema_migration(MIGRATION_ID, session=session)
    dets, why = common.optional_import("engine.detectors")
    if dets is None:
        raise OSError(why)
    return dets.approve_detector(gate_id, session, decision)


@st.dialog("Confirm gate decision", on_dismiss=_close_dialog)
def _gate_dialog(gate_id: str, decision: str) -> None:
    who = f"{session.principal} ({_role_word(session.role)})" if session else "signed out"
    st.markdown(f"**{md(gate_id)}** → **{md(decision)}** as {md(who)}")
    st.info("This decision is final and is logged in the approval audit log (route 'gate').")
    if _dialog_buttons("Confirm", "dlg-gate"):
        msgs = st.session_state.setdefault("ap_gate_msg", {})
        actor, role = (session.principal, session.role) if session else ("", "unauthenticated")
        try:
            rec = _gate_apply(gate_id, decision)
        except (PermissionError, OSError, ValueError) as exc:
            approvals_db.audit_gate(gate_id, decision, actor, role, "denied_rbac" if isinstance(exc, PermissionError) else "error", str(exc), db_path)
            msgs[gate_id] = {"ok": False, "text": f"Refused: {exc}"}
        else:
            approvals_db.audit_gate(gate_id, decision, actor, role, "ok", f"{decision} via the Approvals page", db_path)
            msgs[gate_id] = {"ok": True, "text": f"{decision}: {rec}"}
        _close_dialog()
        st.rerun()


# --------------------------------------------------------------------------- #
# Header: what is waiting, and one sentence on who can decide it              #
# --------------------------------------------------------------------------- #
# the round picker sits at the end of the Inbox count bar; its value is read here so the title can say what is waiting
rnd = st.session_state.get("ap_round") if st.session_state.get("ap_round") in ROUNDS else "final"
round_rows = [r for r in inbox if rnd == "all" or _round_of(r.get("case_id")) == rnd]
counts = Counter(r.get("status") for r in round_rows)
n_pending = counts.get("pending", 0)
ROUTE_WORDS = {"L1": "L1 · team lead", "L2": "L2 · fraud manager"}


def _authority() -> str:
    """One sentence (HTML): whether anyone here can decide, the signed-in session, and where a decision is stored."""
    dir_html = f'<span class="tc-mono">{esc(dir_rel)}</span>'
    if dirst["error"]:
        who = (f'<b class="red">Nobody can approve here</b>: the approver directory {dir_html} (the server-side list of who '
               f"holds which role) is unreadable, so every approval fails closed.")
    elif fail_closed:
        who = (f'<b class="red">Nobody can approve here</b>: there is no approver directory (the server-side list of who holds '
               f"which role, {dir_html}), so every approval fails closed.")
    elif session is None:
        roles = ", ".join(f"{n} {_role_word(r)}" for r, n in sorted(dirst["roles"].items()))
        who = (f'{esc(dirst["n"])} principal{"s are" if dirst["n"] != 1 else " is"} registered ({esc(roles)}); <b class="ochre">signed out</b>, '
               "so nothing can be decided from this session (Sign in, top right).")
    else:
        left = approvals_db.session_expires_in_s(session)
        may = " and ".join(sorted(allowed_routes)) or "nothing"
        who = (f"Signed in as <b>{esc(session.principal)}</b>, {esc(_role_word(session.role))}: may decide <b>{esc(may)}</b>"
               + (f", session expires in {max(0, int(left // 60))} min." if left is not None else "."))
    if ro:
        where = f"Decisions are stored only in {esc(db_rel)}, which is wiped on redeploy unless a volume is attached."
    elif graph_writes:
        where = f"Decisions are stored in {esc(db_rel)}, then written to FraudGraph and read back."
    else:
        where = f"Decisions are stored only in {esc(db_rel)}."
    return f"{who} {where}"


common.page_header("Approvals", eyebrow=f"{ROUNDS[rnd]} · {n_pending} pending")
design.html_block(f'<p class="tc tc-lead ap-auth">{_authority()}</p>')
if audit_err:
    st.warning(md(audit_err))

inbox_keys = {(r.get("case_id"), r.get("action")) for r in inbox}
unqueued: dict[str, dict[str, dict]] = {}
for _cid in sorted(set(src_ans) | set(sub_ans)):
    if rnd != "all" and _round_of(_cid) != rnd:
        continue
    for _ans in (src_ans.get(_cid), sub_ans.get(_cid)):
        for _act, _g in _gated(_ans).items():
            if (_cid, _act) not in inbox_keys:
                unqueued.setdefault(_cid, {}).setdefault(_act, _g)

# --------------------------------------------------------------------------- #
# Integrity over the selected round: each count must be 0                     #
# --------------------------------------------------------------------------- #
refusals = Counter(a.get("outcome") for a in audit if a.get("outcome") != "ok")
hard_bad = [(r["case_id"], r["action"], nf) for r in round_rows
            if r.get("action") in common.HARD_ACTIONS and r.get("status") != "rejected" and (nf := _non_fraud(r["case_id"]))]
stale = [r for r in round_rows if r.get("status") == "pending" and _superseded(r["case_id"], r["action"])]
n_unqueued = sum(len(x) for x in unqueued.values())

# Other gates: the schema migration and the detector proposals (read here for the view's count)
appr = common.proof("schema_approval")
mig_rec = (appr["data"] or {}).get(MIGRATION_ID) if isinstance(appr["data"], dict) else None
mig_status = str((mig_rec or {}).get("status") or "unknown")
bt = common.proof("backtest")
props = ((bt["data"] or {}).get("self_growing_proposals") if isinstance(bt["data"], dict) else None) or []
n_gates_pending = (mig_status == "pending") + sum(p.get("approval_status") in (None, "pending") for p in props)

# --------------------------------------------------------------------------- #
# The selected request (Inbox detail)                                         #
# --------------------------------------------------------------------------- #
GRAPH_NOTE = ("written to FraudGraph and read back after a decision" if graph_writes
              else "FraudGraph not written: read-only deployment" if ro else "FraudGraph not written: files-only mode")


def _row_state(r: dict) -> str:
    """The request's audit trail and graph write, in one line."""
    tries = [a for a in audit if a.get("approval_id") == r["id"]]
    if r["status"] == "pending":
        if not tries:
            return f"Audit: no decision attempt logged · {GRAPH_NOTE}"
        o = str(tries[0].get("outcome"))
        return (f"Audit: {len(tries)} attempt{'s' if len(tries) != 1 else ''} logged, latest {OUTCOME_WORDS.get(o, o)} at "
                f"{tries[0].get('at')} (audit #{tries[0].get('id')}) · {GRAPH_NOTE}")
    a = next((x for x in tries if x.get("outcome") == "ok"), None)
    if graph_writes:
        g = (st.session_state.get("ap_graph") or {}).get(r["id"])
        graph = f"FraudGraph: {g['text']}" if g else "FraudGraph: not written from this session"
    else:
        graph = GRAPH_NOTE
    return f"{r['status'].capitalize()} by {r.get('decided_by')} at {r.get('decided_at')} · audit #{a['id'] if a else '—'} · {graph}"


def _detail(r: dict) -> None:
    """The selected request: the decision behind it, its model-written reason, evidence, the decision buttons with the
    blocking reason as text, links and its audit / graph-write state."""
    cid, act, rid, route = str(r["case_id"]), str(r["action"]), r["id"], str(r["route"])
    v = _view(cid)
    ans = v.answer if v.ok and isinstance(v.answer, dict) else None
    verdict = str(((ans or {}).get("case") or {}).get("verdict") or "")
    sup = _superseded(cid, act)
    design.html_block(f'<div class="tc ap-dhead"><b>{esc(cid)}</b><span class="tc-mono">{esc(act)}</span>{design.route_tag(route)}'
                      f'{design.tag(r["status"], STATUS_TAG.get(str(r["status"]), ""))}<span class="tc-muted">request #{esc(rid)}</span></div>')
    with st.container(border=True, key=f"decision-{verdict or 'none'}-{rid}"):
        if ans:
            design.html_block(_decision_html(r, v, ans))
        else:
            st.caption(md(f"No parsed answer for {cid} in {src_id} or the submitted answers: no verdict to show"))
    if sup:
        design.html_block(f'<p class="tc ap-line ap-warn">No longer recommended: the submitted answer ({esc(_submitted_id(cid))}) '
                          f"no longer holds this action, so it can only be rejected.</p>")
    _reason_block(r, str(rid))
    if ans:
        design.html_block(_refs_html(ans) + (_sar_validator_html(v, ans) if act == "FILE_REPORT" else ""))
    if r["status"] == "pending":
        why_a, why_r = _decide_block(route, sup, approve=True), _decide_block(route, sup, approve=False)
        b1, b2 = st.columns(2)
        if b1.button("Approve…", key=f"ap-ok-{rid}", disabled=bool(why_a), width="stretch"):
            _open_dialog("decide", row_id=rid, status="approved")
        if b2.button("Reject…", key=f"ap-no-{rid}", disabled=bool(why_r), width="stretch"):
            _open_dialog("decide", row_id=rid, status="rejected")
        block = why_a if why_a == why_r else "; ".join(x for x in (why_a and f"Approve: {why_a}", why_r and f"Reject: {why_r}") if x)
        if block:
            design.html_block(f'<p class="tc ap-block">{glyph("gate")}<span>{esc(block)}</span></p>')
    with st.container(horizontal=True, gap="medium"):
        _page_link("case", "Open case file", ":material/folder_open:", cid, v.source_id)
        _page_link("graph", "Graph", ":material/hub:", cid, v.source_id)
    design.html_block(f'<p class="tc ap-line tc-muted">{esc(_row_state(r))}</p>')
    res = (st.session_state.get("ap_results") or {}).get(rid)
    if res:
        (st.success if res["ok"] else st.error)(md(res["text"]))
    g = (st.session_state.get("ap_graph") or {}).get(rid)
    if g and not g["ok"] and graph_writes and r["status"] != "pending":
        if st.button("Retry graph write", key=f"ap-retry-{rid}", icon=":material/refresh:"):
            st.session_state["ap_graph"][rid] = _graph_write(r)
            st.rerun()


# --------------------------------------------------------------------------- #
# Views (lazy tabs)                                                           #
# --------------------------------------------------------------------------- #
t_inbox, t_enf, t_gates, t_store = st.tabs([f"Inbox ({n_pending})", "Enforcement & audit", f"Other gates ({n_gates_pending})", "Store"],
                                           key="approvals_tab", on_change="rerun")

if t_inbox.open:
    with t_inbox:
        with st.container(horizontal=True, vertical_alignment="center", key="ap-countbar"):
            status = st.pills("Status", STATUSES, default="pending", key="ap_status", required=True, label_visibility="collapsed",
                              format_func=lambda s: f"{s.capitalize()} {len(round_rows) if s == 'all' else counts.get(s, 0)}") or "pending"
            st.space("stretch")
            st.segmented_control("Round", list(ROUNDS), default="final", format_func=ROUNDS.get, key="ap_round", required=True,
                                 label_visibility="collapsed")
        prov = f"{db_rel} (approvals table) as of {db_as_of}; verdicts from the answers of {src_id} and the submitted answers"
        design.html_block(f'<div class="ap-int" title="{esc(prov)}">' + design.checks(
            [(None if not n else False, f"{label} {n}") for label, n in (("hard actions on non-fraud", len(hard_bad)),
                                                                         ("recommended but not queued", n_unqueued),
                                                                         ("no longer recommended", len(stale)))],
            lead="Integrity") + "</div>")
        bad = ([f"{c} {a} is queued but the case is not fraud ({', '.join(nf)})" for c, a, nf in hard_bad]
               + [f"{c} {a} ({g['route']}) is recommended but not queued" for c, acts in unqueued.items() for a, g in acts.items()]
               + [f"{r['case_id']} {r['action']} (request #{r['id']}) is no longer recommended by the submitted answer" for r in stale])
        if bad:
            design.html_block(f'<p class="tc ap-int-detail">{esc("; ".join(bad))}</p>')

        # rows decided in this session stay visible under any status filter, so the result shows on its row
        _decided_here = set(st.session_state.get("ap_results") or {})
        rows = [r for r in round_rows if status == "all" or r.get("status") == status or r.get("id") in _decided_here]
        rows.sort(key=lambda r: (r.get("route") != "L2", -(_exposure(str(r["case_id"])) or 0.0), str(r["case_id"]), str(r["action"])))
        qp_case = st.query_params.get("case")
        left_col, right_col = st.columns([3, 2], gap="medium")
        with left_col:
            sel = None
            if rows:
                table = []
                for r in rows:
                    case = _case_of(str(r["case_id"]))
                    table.append({"#": r["id"], "case": r["case_id"], "action": r["action"], "role needed": ROUTE_WORDS.get(r["route"], r["route"]),
                                  "exposure": _num(case.get("exposure_usd")), "verdict": VERDICT_WORDS.get(str(case.get("verdict") or ""), "—"),
                                  "p(fraud)": _num(case.get("fraud_probability")), "status": r["status"]})
                event = st.dataframe(
                    table, key=QUEUE_KEY, on_select="rerun", selection_mode="single-row", hide_index=True, width="stretch",
                    row_height=ROW_H, height=(min(len(table), 18) + 1) * ROW_H + 3,
                    # fixed widths fit DECLINE_TRANSACTION and Legitimate whole; numbers and their headers sit right
                    column_config={"#": st.column_config.NumberColumn("#", width=32, format="%d", alignment="right", help="request number in the approvals table"),
                                   "case": st.column_config.TextColumn("Case", width=72),
                                   "action": st.column_config.TextColumn("Action", width=182),
                                   "role needed": st.column_config.TextColumn("Role needed", width=124),
                                   "exposure": st.column_config.NumberColumn("Exposure", width=82, format="dollar", alignment="right"),
                                   "verdict": st.column_config.TextColumn("Verdict", width=82),
                                   "p(fraud)": st.column_config.NumberColumn("p(fraud)", width=58, format="%.2f", alignment="right"),
                                   "status": st.column_config.TextColumn("Status", width=70)},
                )
                picked = list(getattr(getattr(event, "selection", None), "rows", None) or [])
                if picked and 0 <= picked[0] < len(rows):
                    sel = rows[picked[0]]
                elif qp_case:
                    sel = next((r for r in rows if r["case_id"] == qp_case), None) or next((r for r in inbox if r["case_id"] == qp_case), None)
                sel = sel or rows[0]
            else:
                if inbox_err:
                    why = inbox_err
                elif not inbox:
                    why = f"Nothing is queued in {db_rel} on this deployment."
                else:
                    why = f"No {'' if status == 'all' else status + ' '}requests in {ROUNDS[rnd]}; {len(inbox)} requests across all rounds."
                st.info(md(why), icon=":material/inbox:")
        with right_col:
            if sel:
                _detail(sel)


if t_enf.open:
    with t_enf:
        # 1. the wrong-role drill, run against a throwaway store
        design.html_block(design.section("", "Wrong-role drill", note="a throwaway database and approver directory in a temporary "
                                         f"folder; {db_rel} and its audit log are not touched"))
        if st.button("Run wrong-role drill in a sandbox", key="ap-drill", icon=":material/science:"):
            st.session_state["ap_drill"] = approvals_db.run_rbac_drills()
        drill = st.session_state.get("ap_drill")
        if drill:
            n_refused = sum(bool(d.get("blocked")) for d in drill)
            n_appr = sum(bool(d.get("blocked")) for d in drill if d.get("attack_id") in APPROVAL_DRILLS)
            n_accepted = len(drill) - n_refused
            lead = (f'<b class="green">{n_refused} refusals</b>, {n_appr} of them approval drills' if not n_accepted else
                    f'<b class="red">{n_accepted} of {len(drill)} attempts accepted</b>; {n_refused} refusals, {n_appr} of them approval drills')
            others = [str(d.get("attack_id")) for d in drill if d.get("attack_id") not in APPROVAL_DRILLS]
            tail = f" {', '.join(others)}: a raw SQL edit that only the database trigger stops." if others else ""
            design.html_block(f'<p class="tc tc-lead">{lead}.{esc(tail)} Run at {esc(drill[0].get("at"))} UTC.</p>')
            body = "".join(f'<tr><td class="mono">{esc(d.get("attack_id"))}</td><td>{esc(d.get("vector"))}</td>'
                           f'<td>{design.tag("refused", "green") if d.get("blocked") else design.tag("accepted", "red")}</td>'
                           f'<td class="mono">{esc(d.get("observed"))}</td></tr>' for d in drill)
            design.html_block(f'<table class="tc-table ap-drill"><thead><tr><th>Attack</th><th>Attempt</th><th>Server</th>'
                              f"<th>Server message</th></tr></thead><tbody>{body}</tbody></table>")

        # 2. who decides what
        yaml, _ = common.optional_import("yaml")
        policy_path = common.ROOT / "policy" / "fraud_policy.yaml"
        routes_cfg, limit, policy_note = None, 2500.0, "policy file not shipped: policy constants"
        if yaml is not None and policy_path.is_file():
            try:
                routes_cfg = (yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}).get("routes")
                limit = float(routes_cfg.get("block_l2_exposure", limit))
                policy_note = f"{common.rel(policy_path)} §2 routes, as of {common.as_of(policy_path)}"
            except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError) as exc:
                routes_cfg, policy_note = None, f"policy file unreadable ({type(exc).__name__}): policy constants"
        l1 = list((routes_cfg or {}).get("L1") or ["DECLINE_TRANSACTION", "BLOCK_CARD"])
        l2 = list((routes_cfg or {}).get("L2") or ["BLOCK_ALL_CARDS", "FILE_REPORT"])
        fmt = [f"BLOCK_CARD ≤ ${limit:,.0f}" if a == "BLOCK_CARD" else a for a in l1]
        gated = "where RUNS_NEED_APPROVER is set" + (" (set here)" if common.runs_need_approver() else " (not set here: open to all)")
        cols = [("approve_L1", "L1 · team lead", ", ".join(fmt)), ("approve_L2", "L2 · fraud manager", ", ".join([f"BLOCK_CARD > ${limit:,.0f}", *l2])),
                ("approve_migration", "Schema migration", "team lead or above"), ("approve_detector", "Detector proposals", "fraud manager or above"),
                ("manage_notifications", "Notifications", "fraud manager or above"),
                ("run_live", "Live runs", gated), ("own_data", "Workspaces & own data", gated), ("variant", "Variant rehearsal", gated),
                ("users_view", "Principals list", "this page")]

        def _cell(role: str, col: str) -> str:
            ok = col in approvals_db.ROLE_PERMISSIONS.get(role, frozenset())
            return f'<td class="ap-yn {"ok" if ok else "no"}">{glyph("check" if ok else "cross", title="may" if ok else "refused")}</td>'

        head = "".join(f'<th>{esc(t)}<span class="sub">{esc(s)}</span></th>' for _, t, s in cols)
        body = "".join(f"<tr><td>{esc(_role_word(role).capitalize())}</td>{''.join(_cell(role, c) for c, _, _ in cols)}"
                       f'<td class="num">{esc(dirst["roles"].get(role, 0)) if dirst["exists"] and not dirst["error"] else "—"}</td></tr>'
                       for role in approvals_db.ROLE_ALLOWED_ROUTES)
        design.html_block(design.section("", "Who decides what", note=policy_note)
                          + f'<table class="tc-table ap-matrix"><thead><tr><th>Role</th>{head}<th class="num">Registered'
                          f'<span class="sub">{esc(dir_rel)}</span></th></tr></thead><tbody>{body}</tbody></table>')
        if approvals_db.has_permission(session, "users_view"):  # the server admin: who exists, never a hash
            people = approvals_db.directory_rows()
            order = list(approvals_db.PERMISSIONS)
            prow = "".join(f'<tr><td class="mono">{esc(r["principal"])}</td><td>{esc(_role_word(r["role"]))}</td><td>'
                           + esc("; ".join(approvals_db.PERMISSIONS[x] for x in sorted(approvals_db.ROLE_PERMISSIONS.get(r["role"], frozenset()), key=order.index)) or "viewing only")
                           + "</td></tr>" for r in people)
            design.html_block(design.section("", "Principals", note=f"{dir_rel} · shown to the server admin; passphrase hashes are never shown")
                              + f'<table class="tc-table ap-matrix"><thead><tr><th>Principal</th><th>Role</th><th>May</th></tr></thead><tbody>{prow}</tbody></table>')
            st.caption(md(f"Added or changed on the server only, never from this page: {common.ADD_PRINCIPAL_CMD} · demo set: {common.SEED_DEMO_CMD}"))

        # 3. the audit log, latest refusal first
        n_ref = sum(refusals.values())
        design.html_block(design.section("", "Audit log", note=f"{db_rel} · approval_audit_log · as of {db_as_of}",
                                         aside=esc(f"{n_ref} refusal{'s' if n_ref != 1 else ''} · {len(audit)} attempts")))
        if audit:
            ordered = sorted(audit, key=lambda a: (a.get("outcome") == "ok", -int(a.get("id") or 0)))
            st.dataframe(
                [{"Audit #": a.get("id"), "Request #": a.get("approval_id"), "At (UTC)": a.get("at"), "Case": a.get("case_id"),
                  "Action": a.get("action"), "Route": a.get("route"), "Attempted": a.get("attempted_status"), "Who": a.get("actor"),
                  "Role on the server": a.get("resolved_role"), "Outcome": OUTCOME_WORDS.get(str(a.get("outcome")), a.get("outcome")),
                  "Detail": a.get("detail")} for a in ordered],
                hide_index=True, width="stretch",
                column_config={"Audit #": st.column_config.NumberColumn("Audit #", format="%d", alignment="right"),
                               "Request #": st.column_config.NumberColumn("Request #", format="%d", alignment="right"),
                               "Detail": st.column_config.TextColumn("Detail", width="large"),
                               "Outcome": st.column_config.TextColumn("Outcome", help="codes: " + ", ".join(f"{k} = {v}" for k, v in OUTCOME_WORDS.items()))},
            )
        else:
            st.caption(md(f"No decision attempts logged in {db_rel}."))
        trig = approvals_db.triggers(db_path) if db_path.is_file() else []
        design.html_block('<p class="tc ap-line tc-muted">Enforced by SQLite triggers in this store: '
                          + (" ".join(f'<span class="tc-mono">{esc(t)}</span>' for t in trig) or "none (no database yet)") + "</p>")

        # 4. a real attempt, logged like any other
        design.html_block(design.section("", "Attempt a decision with my current session",
                                         note="the server checks the role and logs the attempt, refused or not"))
        if ro:
            st.caption("Read-only deployment: no decision is attempted here.")
        else:
            pending_rows = [r for r in inbox if r.get("status") == "pending"]
            pick = st.selectbox("Pending request", [r["id"] for r in pending_rows], key="ap-attempt-row", disabled=not pending_rows,
                                format_func=lambda i: next(f"#{r['id']} {r['case_id']} {r['action']} ({r['route']})" for r in pending_rows if r["id"] == i))
            why = (_decide_block("L1") if fail_closed else "Signed out: sign in to attempt" if session is None
                   else None if pending_rows else "No pending requests")
            if st.button("Attempt a decision with my current session", key="ap-attempt", disabled=bool(why)) and pick is not None:
                _open_dialog("decide", row_id=pick, status="approved", attempt=True)
            if why:
                design.html_block(f'<p class="tc ap-block">{glyph("gate")}<span>{esc(why)}</span></p>')
        common.page_link("security", "Security & drills", icon=":material/shield:")

if t_gates.open:
    with t_gates:
        with st.container(border=True):
            st.markdown(f"**Schema migration {md(MIGRATION_ID)}**")
            st.markdown(" ".join([common.badge(mig_status, "yellow" if mig_status == "pending" else "green" if mig_status == "approved" else "gray"),
                                  common.badge("decided by a team lead or a fraud manager", "blue")]))
            st.caption(md(f"Source: {appr['source'] or 'missing'} · as of {appr['as_of'] or '—'}" + (f" · {appr['error']}" if appr["error"] else "")))
            st.caption("What it blocks today: nothing in the January delta path (etl/delta*.py) checks it; the engine facts and FraudGraph already include January.")
            _, drift_why = common.optional_import("schema_engine.drift")
            why = (drift_why or ("read-only deployment" if ro else None) or ("Sign in first" if session is None else None)
                   or (None if session.role in GATE_ROLES else "Needs a team lead or a fraud manager") or (None if mig_status == "pending" else f"already {mig_status}"))
            if st.button("Approve (final)…", key="ap-gate-mig", disabled=bool(why)):
                _open_dialog("gate", gate_id=MIGRATION_ID, decision="approved")
            if why:
                design.html_block(f'<p class="tc ap-block">{glyph("gate")}<span>{esc(why)}</span></p>')
            if (m := (st.session_state.get("ap_gate_msg") or {}).get(MIGRATION_ID)):
                (st.success if m["ok"] else st.error)(md(m["text"]))
        with st.container(border=True):
            st.markdown("**Detector proposals** · decided by a fraud manager only")
            st.caption(md(f"Source: {bt['source'] or 'missing'} · as of {bt['as_of'] or '—'} · {bt['mode']}" + (f" · {bt['error']}" if bt["error"] else "")))
            if not props:
                st.caption("No detector proposals in this deployment.")
            else:
                st.dataframe([{"Proposal": p.get("proposal_id"), "Name": p.get("name"), "Cards": p.get("cards_matched"), "Txns": p.get("txns_matched"),
                               "exposure_usd": p.get("total_exposure_usd"), "Closed-case hits": len(p.get("closed_case_true_positives") or []),
                               "Legit customers acted against": len(p.get("legitimate_customers_acted_against") or []),
                               "Status": p.get("approval_status")} for p in props],
                             hide_index=True, width="stretch", column_config={"exposure_usd": st.column_config.NumberColumn("Exposure", format="dollar")})
                _, dets_why = common.optional_import("engine.detectors")
                why = dets_why or ("read-only deployment" if ro else None) or ("Sign in first" if session is None else None) or \
                    (None if session.role in approvals_db.roles_with("approve_detector") else "Needs a fraud manager or the server admin")
                if why:
                    design.html_block(f'<p class="tc ap-block">{glyph("gate")}<span>{esc(why)}</span></p>')
                for p in props:
                    pid = str(p.get("proposal_id"))
                    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="center")
                    c1.markdown(f"{md(pid)} · {common.badge(str(p.get('approval_status') or 'pending'), 'yellow')}")
                    p_why = why or (None if p.get("approval_status") in (None, "pending") else f"already {p.get('approval_status')}")
                    for col, decision, label in ((c2, "approved", "Approve…"), (c3, "rejected", "Reject…")):
                        if col.button(label, key=f"ap-det-{decision}-{_k(pid)}", disabled=bool(p_why), help=p_why):
                            _open_dialog("gate", gate_id=pid, decision=decision)
                    if (m := (st.session_state.get("ap_gate_msg") or {}).get(pid)):
                        (st.success if m["ok"] else st.error)(md(m["text"]))

if t_store.open:
    with t_store:
        per_round = Counter(ROUNDS.get(_round_of(r.get("case_id")), "other") for r in inbox)
        st.markdown(f"Store: **{md(db_rel)}** · " + (f"modified {md(db_as_of)}" if db_path.is_file() else "not created yet") + " · " +
                    (" · ".join(f"{md(k)} {n} requests" for k, n in sorted(per_round.items())) or "0 requests"))
        if ro:
            st.caption("Read-only deployment: this file lives in the container and is wiped on redeploy unless a volume is attached.")
        st.caption("Gated actions are the L1 and L2 actions in an answer's next\\_best\\_actions.final: each waits for a person before it runs.")
        answers = [v.answer for v in src_views.values() if v.ok]
        load_why = ("no source selected" if not src_id else f"no answers in {src_id}" if not answers else
                    None if not ro or (session and session.role in GATE_ROLES) else "read-only deployment: sign in as team_lead or fraud_manager")
        if st.button(f"Load gated actions from {md(src_id or 'source')}", key="ap-load", icon=":material/download:", disabled=bool(load_why), help=load_why):
            _open_dialog("enqueue", source_id=src_id)
        if (msg := st.session_state.get("ap_import_msg")):
            st.success(md(msg))
        with st.expander(f"Raw rows ({len(inbox)})"):
            if inbox:
                st.dataframe(inbox, hide_index=True, width="stretch")
            else:
                st.caption("No rows.")
        try:
            fb, fb_err = approvals_db.feedback_rows_ro(path=db_path), None
        except sqlite3.Error as exc:
            fb, fb_err = [], f"feedback log unreadable: {exc}"
        fb_demo_path = common.ROOT / "engine" / "out" / "feedback_before_after.json"
        fb_demo, _ = common._read_json(fb_demo_path) if fb_demo_path.is_file() else (None, "")
        with st.expander(f"Challenge 8 · Analyst feedback loop ({len(fb)} in store · {len((fb_demo or {}).get('feedback_rows') or [])} in before/after re-run)", expanded=True):
            st.caption("Analyst accept / reject decisions on findings (Challenge 8): read by engine/feedback.py and engine/scorecard.py on the next run (lifts detector floors on rejection and adjusts log-odds at card or customer scope).")
            if fb_err:
                st.warning(md(fb_err))
            elif fb:
                st.dataframe(fb, hide_index=True, width="stretch")
            else:
                st.caption(md(f"No analyst feedback in {db_rel} yet."))
            if isinstance(fb_demo, dict) and fb_demo.get("cases"):
                st.markdown(f"**Measured Before → After Re-Run (`engine/out/feedback_before_after.json` · wrongly acted-against cases `{len(fb_demo.get('wrongly_acted_against_before') or [])}` → `{len(fb_demo.get('wrongly_acted_against_after') or [])}`)**")
                fb_table = []
                for cid_f, cdata in fb_demo["cases"].items():
                    bef_f, aft_f = cdata.get("before") or {}, cdata.get("after") or {}
                    causes = ", ".join(f"{x.get('finding_kind')} {x.get('decision')} ({x.get('scope')})" for x in (cdata.get("caused_by") or []))
                    fb_table.append({
                        "Case": cid_f,
                        "Feedback recorded": causes,
                        "Before (verdict · p · actions)": f"{bef_f.get('verdict')} ({bef_f.get('p')}) · {', '.join(bef_f.get('actions') or [])}",
                        "After re-run (verdict · p · actions)": f"{aft_f.get('verdict')} ({aft_f.get('p')}) · {', '.join(aft_f.get('actions') or [])}",
                    })
                st.dataframe(fb_table, hide_index=True, width="stretch")
            if not ro and session and session.role in approvals_db.FEEDBACK_ROLES:
                fc1, fc2, fc3 = st.columns([1.2, 1.5, 1.3])
                fb_case = fc1.selectbox("Case to reject finding on", ["HHG-019", "HHG-007"], key="fb_live_case")  # Round 1 closed cases only: the shared store feeds the engine, so never a submitted HHF case
                fb_kind = fc2.selectbox("Finding to reject", ["fraud_verdict", "graph_neighbourhood"], key="fb_live_kind")
                if fc3.button("Reject finding & re-score", key="fb_live_btn", type="primary", icon=":material/Sync:"):
                    card_map = {"HHG-019": "C07987-K2", "HHG-007": "C09933-K1"}
                    approvals_db.record_feedback(fb_case, card_map[fb_case], fb_kind, "rejected", session, "Rejected via UI analyst loop", path=db_path)
                    from engine import feedback as eng_fb
                    eng_fb.demo(common.ROOT / "engine" / "out" / "feedback_before_after.json")
                    st.rerun()

# --------------------------------------------------------------------------- #
# The open dialog, if any                                                     #
# --------------------------------------------------------------------------- #
_dlg = st.session_state.get(DIALOG_KEY)
if _dlg:
    if _dlg["kind"] == "decide" and (_row := next((r for r in inbox if r["id"] == _dlg["row_id"]), None)):
        _decide_dialog(_row, _dlg["status"], _dlg.get("attempt", False))
    elif _dlg["kind"] == "enqueue":
        _enqueue_dialog(_dlg["source_id"], [v.answer for v in _answers(_dlg["source_id"]).values() if v.ok])
    elif _dlg["kind"] == "gate":
        _gate_dialog(_dlg["gate_id"], _dlg["decision"])
    else:
        _close_dialog()

"""Approvals & audit log: the L1 / L2 actions the policy gate queued, each waiting for a person whose role the server
checks (approvals_db.decide). Viewing never writes: the inbox, the audit log and the feedback log are read through
approvals_db.connect_ro, and rows are queued only by the labelled 'Load gated actions' button behind a confirm dialog.
A decision is final; in live mode (not read-only) it is also written to FraudGraph and read back.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

md = common.md

ctx = common.page_setup("approvals")
session = common.approver_session()
dirst = approvals_db.directory_status()
db_path = common.approvals_db_path()
db_rel, db_as_of = common.rel(db_path), common.as_of(db_path)
ro = common.read_only()
graph_writes = ctx.mode == "live" and not ro  # DEPLOY_READONLY=1 keeps decisions local even in live mode
allowed_routes = approvals_db.ROLE_ALLOWED_ROUTES.get(session.role, set()) if session else set()
fail_closed = bool(dirst["error"]) or not dirst["n"]
FAIL_CLOSED = "missing → nobody can approve (fail closed)"
GATE_ROLES = ("team_lead", "fraud_manager")
SUBMITTED_FOR = {"HHF-": "submitted", "HHG-": "submitted-r1"}
ROUNDS = {"final": "Final Round", "round1": "Round 1", "all": "All"}
STATUSES = ("pending", "approved", "rejected", "all")
STATUS_COLORS = {"pending": "orange", "approved": "green", "rejected": "gray"}
MIGRATION_ID = "round2_2017q1_migration"


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


def _exposure(case_id: str) -> float:
    v = _view(case_id)
    try:
        return float(((v.answer or {}).get("case") or {}).get("exposure_usd") or 0) if v.ok else 0.0
    except (TypeError, ValueError):
        return 0.0


def _audit_for(approval_id: int) -> dict | None:
    rows, _ = _audit_rows()
    return next((a for a in rows if a.get("approval_id") == approval_id), None)


def _decide_block(route: str, superseded: bool = False, approve: bool = True) -> str | None:
    """Why this session may not decide a row on `route` (None when it may)."""
    if fail_closed:
        return FAIL_CLOSED
    if session is None:
        return "Sign in first"
    if route not in allowed_routes:
        return "Needs fraud_manager" if route == "L2" else "Needs team_lead or fraud_manager"
    if approve and superseded:
        return "Superseded"
    return None


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
        results[row["id"]] = {"ok": False, "text": f"Refused by the server: {exc} · audit #{a.get('id', '?')} ({a.get('outcome', '?')})"}
        return
    except KeyError:
        results[row["id"]] = {"ok": False, "text": f"Inbox row #{row['id']} no longer exists"}
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
    ans = v.answer if v.ok else {}
    case = (ans or {}).get("case") or {}
    st.markdown(f"**{md(row['case_id'])}** · {common.action_badge(row['action'], row['route'])} · inbox #{row['id']} · "
                f"{common.badge(status, 'green' if status == 'approved' else 'gray')}")
    who = f"{session.principal} ({session.role})" if session else "signed out"
    st.markdown(f"Required role: **{md(common.route_label(row['route']))}** · your role: **{md(who)}**")
    if attempt and row["route"] not in allowed_routes:
        st.warning("Your role does not cover this route: the server will refuse and log the attempt.")
    if ans:
        st.markdown(f"{common.verdict_badge(str(case.get('verdict') or ''))} p(fraud) **{common.prob(case.get('fraud_probability'))}** · "
                    f"exposure **{md(common.money(case.get('exposure_usd')))}** · from {md(v.source_id)}")
        refs = [str(e.get("ref") or "") for e in case.get("evidence") or [] if isinstance(e, dict) and e.get("ref")][:3]
        if refs:
            st.caption("Top evidence refs")
            for ref in refs:
                st.markdown(f"- {md(ref)}")
        if row["action"] == "FILE_REPORT":
            sar = ans.get("sar") or {}
            st.caption("SAR narrative (excerpt, model-written)")
            st.text(str(sar.get("narrative") or "no narrative")[:700])
            _, why = common.optional_import("rag.validate_sar")
            if why:
                st.caption(md(f"SAR validator: {why}"))
            else:
                resolver, where = common.sar_resolver()
                checks = common.sar_checks(ans, v.meta, resolver=resolver)
                st.caption(md(f"SAR validator: {sum(ok for _, ok, _ in checks)}/{len(checks)} checks pass (V00-V18); {where}"))
    else:
        st.caption(md(f"No answer for {row['case_id']} in {src_id} or the submitted answers"))
    st.caption(md(f"Reason: {row.get('reason') or '—'}"))
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
    who = f"{session.principal} ({session.role})" if session else "signed out"
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
# Header                                                                      #
# --------------------------------------------------------------------------- #
if ro:
    where = (f"{db_rel} — wiped on redeploy unless a volume is attached", "orange")
elif ctx.mode == "live":
    where = ("FraudGraph Approval + CaseEvent, then read back", "green")
else:
    where = (f"stored only in {db_rel}", "gray")
if dirst["error"]:
    directory = (f"directory unreadable → nobody can approve (fail closed): {dirst['error']}", "red")
elif fail_closed:
    directory = (f"Directory: {FAIL_CLOSED}", "red")
else:
    directory = (f"Directory: {dirst['n']} principals", "green")
if session:
    left = approvals_db.session_expires_in_s(session)
    routes = "/".join(sorted(allowed_routes)) or "nothing"
    who_chip = (f"{session.principal} · {session.role} · may decide {routes}" + (f" · expires in {max(0, int(left // 60))} min" if left is not None else ""), "violet")
else:
    who_chip = ("Session: signed out", "gray")
common.page_header(
    "Approvals & audit log",
    "L1/L2 actions wait for a person whose role the server checks. Decisions are final and every attempt is logged.",
    eyebrow="Human gates · C14 lifecycle · Stage 2 wrong-role drill",
    chips=[(f"Decisions: {where[0]}", where[1]), directory, who_chip],
)
if audit_err:
    st.warning(md(audit_err))

kpi_box = st.container()

# --------------------------------------------------------------------------- #
# Filters                                                                     #
# --------------------------------------------------------------------------- #
with st.container(horizontal=True, vertical_alignment="center"):
    rnd = st.segmented_control("Round", list(ROUNDS), default="final", format_func=ROUNDS.get, key="ap_round", required=True) or "final"
    round_rows = [r for r in inbox if rnd == "all" or _round_of(r.get("case_id")) == rnd]
    counts = Counter(r.get("status") for r in round_rows)
    status = st.pills("Status", STATUSES, default="pending", key="ap_status", required=True,
                      format_func=lambda s: f"{s} ({len(round_rows) if s == 'all' else counts.get(s, 0)})") or "pending"
    show_unqueued = st.toggle("Show recommended but not queued", key="ap_unqueued")

# rows decided in this session stay visible under any status filter, so the result shows on its row
_decided_here = set(st.session_state.get("ap_results") or {})
rows = [r for r in round_rows if status == "all" or r.get("status") == status or r.get("id") in _decided_here]
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
# KPIs (above the filters, computed over the selected round)                  #
# --------------------------------------------------------------------------- #
refusals = Counter(a.get("outcome") for a in audit if a.get("outcome") != "ok")
hard_bad = [(r["case_id"], r["action"], nf) for r in round_rows
            if r.get("action") in common.HARD_ACTIONS and r.get("status") != "rejected" and (nf := _non_fraud(r["case_id"]))]
inbox_src = f"{db_rel} (approvals table)"
with kpi_box:
    k1, k2, k3, k4, k5 = st.columns(5)
    pend = Counter(r.get("route") for r in round_rows if r.get("status") == "pending")
    common.measured(f"Pending L1 · {ROUNDS[rnd]}", pend.get("L1", 0), source=inbox_src, as_of=db_as_of,
                    how="rows with status 'pending' and route L1, read through approvals_db.connect_ro", container=k1)
    common.measured(f"Pending L2 · {ROUNDS[rnd]}", pend.get("L2", 0), source=inbox_src, as_of=db_as_of,
                    how="rows with status 'pending' and route L2", container=k2)
    common.measured("Decided (approved / rejected)", f"{counts.get('approved', 0)} / {counts.get('rejected', 0)}", source=inbox_src,
                    as_of=db_as_of, how="rows with status approved / rejected in the selected round", container=k3)
    common.measured("Refused attempts", sum(refusals.values()), source=f"{db_rel} (approval_audit_log)", as_of=db_as_of,
                    how="audit rows whose outcome is not 'ok', all rounds", container=k4)
    k4.caption(md(" · ".join(f"{o.removeprefix('denied_')} {refusals.get(o, 0)}" for o in ("denied_auth", "denied_rbac", "denied_immutable", "denied_signin"))))
    common.measured("Hard actions queued on non-fraud verdicts", len(hard_bad), source=f"{inbox_src}; answers of {src_id} and submitted",
                    as_of=db_as_of, what="inbox rows (not rejected) for BLOCK_CARD / BLOCK_ALL_CARDS / FILE_REPORT whose case is not fraud "
                    "in the selected source or the submitted answers; must be 0", how="common.HARD_ACTIONS against case.verdict",
                    delta=f"{len(hard_bad)} must be 0" if hard_bad else None, delta_color="inverse", container=k5)
    if hard_bad:
        k5.error(md("; ".join(f"{c} {a} ({', '.join(nf)})" for c, a, nf in hard_bad)))

# --------------------------------------------------------------------------- #
# Queue (left) and who-can-approve / drills (right)                           #
# --------------------------------------------------------------------------- #
left_col, right_col = st.columns([2, 1])


def _row_state(r: dict) -> str:
    if r["status"] == "pending":
        return f"pending #{r['id']}"
    a = next((x for x in audit if x.get("approval_id") == r["id"] and x.get("outcome") == "ok"), None)
    if graph_writes:
        g = (st.session_state.get("ap_graph") or {}).get(r["id"])
        graph = f"FraudGraph: {g['text']}" if g else "FraudGraph: not written from this session"
    else:
        graph = "FraudGraph: local only"
    return f"{r['status']} by {r.get('decided_by')} at {r.get('decided_at')} · audit #{a['id'] if a else '?'} · {graph}"


def _action_line(r: dict) -> None:
    cid, act, rid = r["case_id"], r["action"], r["id"]
    sup = _superseded(cid, act)
    rec = _recommended_by(cid, act)
    with st.container(border=False):
        bits = [common.action_badge(act, r["route"]), common.badge(r["status"], STATUS_COLORS.get(r["status"], "gray"))]
        bits += [common.badge(f"recommended by {sid} ({route})", "blue") for sid, route in rec] or [common.badge("not recommended by the selected source or submitted", "gray")]
        if sup:
            bits.append(common.badge("superseded — the submitted answer no longer recommends this", "orange", icon=":material/warning:"))
        st.markdown(" ".join(bits))
        st.caption(md(r.get("reason") or "no reason recorded"))
        info, pop, b1, b2 = st.columns([3, 1, 1, 1], vertical_alignment="center")
        info.caption(md(_row_state(r)))
        v = _view(cid)
        refs = [e for e in ((v.answer or {}).get("case") or {}).get("evidence") or [] if isinstance(e, dict)] if v.ok else []
        with pop.popover("Evidence", disabled=not refs, help=None if refs else "no evidence in this source"):
            for e in refs[:8]:
                st.markdown(f"{common.source_badge(str(e.get('source') or ''))} {md(e.get('ref') or '')}")
                st.caption(md(e.get("claim") or ""))
        if r["status"] == "pending":
            why_a = _decide_block(r["route"], sup, approve=True)
            why_r = _decide_block(r["route"], sup, approve=False)
            if b1.button("Approve…", key=f"ap-ok-{rid}", disabled=bool(why_a), help=why_a, width="stretch"):
                _open_dialog("decide", row_id=rid, status="approved")
            if b2.button("Reject…", key=f"ap-no-{rid}", disabled=bool(why_r), help=why_r, width="stretch"):
                _open_dialog("decide", row_id=rid, status="rejected")
        res = (st.session_state.get("ap_results") or {}).get(rid)
        if res:
            (st.success if res["ok"] else st.error)(md(res["text"]))
        g = (st.session_state.get("ap_graph") or {}).get(rid)
        if g and not g["ok"] and graph_writes and r["status"] != "pending":
            if st.button("Retry graph write", key=f"ap-retry-{rid}", icon=":material/refresh:"):
                st.session_state["ap_graph"][rid] = _graph_write(r)
                st.rerun()


with left_col:
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)
    case_ids = set(by_case) | (set(unqueued) if show_unqueued else set())

    def _has_l2(cid: str) -> bool:
        return any(r["route"] == "L2" for r in by_case.get(cid, [])) or (show_unqueued and any(g["route"] == "L2" for g in unqueued.get(cid, {}).values()))

    if not case_ids:
        if inbox_err:
            why = inbox_err
        elif not inbox:
            why = f"The inbox at {db_rel} is empty. Queue gated actions from the Store & import tab."
        else:
            why = f"No {'' if status == 'all' else status + ' '}rows in {ROUNDS[rnd]}; the inbox holds {len(inbox)} rows in all rounds."
        st.info(md(why), icon=":material/inbox:")
        if unqueued and not show_unqueued:
            st.caption(md(f"{sum(len(x) for x in unqueued.values())} gated actions are recommended by the answers but not queued; "
                          "switch on 'Show recommended but not queued'."))
    for cid in sorted(case_ids, key=lambda c: (not _has_l2(c), -_exposure(c), c)):
        with st.container(border=True, key=f"ap-case-{_k(cid)}"):
            head, open_btn = st.columns([5, 1], vertical_alignment="center")
            with head:
                v = _view(cid)
                st.markdown(f"**{md(cid)}** · " + common.badge(f"decision from {v.source_id}", "gray"))
                common.decision_card(v, inbox, compact=True)
            if open_btn.button("Open case file", key=f"ap-open-{_k(cid)}", icon=":material/folder_open:"):
                common.goto("case", case=cid, run=v.source_id)
            for r in by_case.get(cid, []):
                _action_line(r)
            if show_unqueued:
                for act, g in unqueued.get(cid, {}).items():
                    rec = ", ".join(f"{sid} ({route})" for sid, route in _recommended_by(cid, act))
                    st.markdown(f":gray[{md(act)} · {md(common.route_label(g['route']))} · recommended by {md(rec)} · not queued]")
                    st.caption(md(g["reason"]))

with right_col:
    with st.container(border=True):
        st.markdown("**Who can approve what**")
        st.dataframe(
            [{"role": role, "may decide": ", ".join(sorted(routes)) or "nothing", "principals here": dirst["roles"].get(role, 0)}
             for role, routes in approvals_db.ROLE_ALLOWED_ROUTES.items()],
            hide_index=True, width="stretch",
        )
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
        st.dataframe([{"route": common.route_label("L1"), "actions": ", ".join(fmt)},
                      {"route": common.route_label("L2"), "actions": ", ".join([f"BLOCK_CARD > ${limit:,.0f}", *l2])}],
                     hide_index=True, width="stretch")
        st.caption(md(policy_note))

    with st.container(border=True):
        st.markdown("**Wrong-role drill**")
        st.markdown(" ".join(common.badge(f"{o} {n}", "red") for o, n in sorted(refusals.items())) or common.badge("no refusals logged", "gray"))
        latest = next((a for a in audit if a.get("outcome") != "ok"), None)
        if latest:
            common.untrusted_text(f"Latest refusal · {latest.get('at')} · {latest.get('outcome')}", str(latest.get("detail") or ""),
                                  source=f"audit #{latest.get('id')}", key="latest-refusal")
        if st.button("Run wrong-role drill in a sandbox", key="ap-drill", icon=":material/science:",
                     help="Throwaway database and directory in a temp folder; the real inbox and audit log are not touched"):
            st.session_state["ap_drill"] = approvals_db.run_rbac_drills()
        drill = st.session_state.get("ap_drill")
        if drill:
            st.caption(md(f"{sum(d['blocked'] for d in drill)}/{len(drill)} blocked by the server · temp store, nothing logged here"))
            st.dataframe(drill, hide_index=True, width="stretch",
                         column_config={"blocked": st.column_config.CheckboxColumn("blocked"), "observed": st.column_config.TextColumn("server message", width="large")})
        if not ro:
            pending_rows = [r for r in inbox if r.get("status") == "pending"]
            pick = st.selectbox("Pending row", [r["id"] for r in pending_rows], key="ap-attempt-row", disabled=not pending_rows,
                                format_func=lambda i: next(f"#{r['id']} {r['case_id']} {r['action']} ({r['route']})" for r in pending_rows if r["id"] == i))
            why = FAIL_CLOSED if fail_closed else "Sign in first" if session is None else None if pending_rows else "no pending rows"
            if st.button("Attempt a decision with my current session", key="ap-attempt", disabled=bool(why), help=why) and pick is not None:
                _open_dialog("decide", row_id=pick, status="approved", attempt=True)
        common.page_link("security", "Security & drills", icon=":material/shield:")

# --------------------------------------------------------------------------- #
# Lazy tabs                                                                   #
# --------------------------------------------------------------------------- #
t_audit, t_gates, t_feedback, t_store = st.tabs(["Audit log", "Other gates", "Analyst feedback log", "Store & import"],
                                                key="approvals_tab", on_change="rerun")

if t_audit.open:
    with t_audit:
        outcomes = Counter(a.get("outcome") for a in audit)
        pick_outcome = st.pills("Outcome", ["all", *sorted(outcomes)], default="all", key="ap_audit_outcome", required=True,
                                format_func=lambda o: f"{md(o)} ({len(audit) if o == 'all' else outcomes.get(o, 0)})") or "all"
        shown = [a for a in audit if pick_outcome == "all" or a.get("outcome") == pick_outcome]
        if shown:
            st.dataframe(shown, hide_index=True, width="stretch", column_config={
                "id": st.column_config.NumberColumn("audit #", width="small"),
                "approval_id": st.column_config.NumberColumn("inbox #", width="small"),
                "detail": st.column_config.TextColumn("detail", width="large"),
                "at": st.column_config.TextColumn("at (UTC)"),
            })
        else:
            st.caption(md(f"No approval attempts logged in {db_rel}."))
        trig = approvals_db.triggers(db_path) if db_path.is_file() else []
        st.markdown("Enforced by SQLite triggers in this store: " + (" ".join(common.badge(t, "violet") for t in trig) or md("none (no database yet)")))

if t_gates.open:
    with t_gates:
        appr = common.proof("schema_approval")
        rec = (appr["data"] or {}).get(MIGRATION_ID) if isinstance(appr["data"], dict) else None
        with st.container(border=True):
            st.markdown(f"**Schema migration {md(MIGRATION_ID)}**")
            mig_status = str((rec or {}).get("status") or "unknown")
            st.markdown(" ".join([common.badge(mig_status, "orange" if mig_status == "pending" else "green" if mig_status == "approved" else "gray"),
                                  common.badge("roles: team_lead / fraud_manager", "violet")]))
            st.caption(md(f"Source: {appr['source'] or 'missing'} · as of {appr['as_of'] or '—'}" + (f" · {appr['error']}" if appr["error"] else "")))
            st.caption("What it blocks today: nothing in the January delta path (etl/delta*.py) checks it; the engine facts and FraudGraph already include January.")
            _, drift_why = common.optional_import("schema_engine.drift")
            why = (drift_why or ("read-only deployment" if ro else None) or ("Sign in first" if session is None else None)
                   or (None if session.role in GATE_ROLES else "Needs team_lead or fraud_manager") or (None if mig_status == "pending" else f"already {mig_status}"))
            if st.button("Approve (final)…", key="ap-gate-mig", disabled=bool(why), help=why):
                _open_dialog("gate", gate_id=MIGRATION_ID, decision="approved")
            if (m := (st.session_state.get("ap_gate_msg") or {}).get(MIGRATION_ID)):
                (st.success if m["ok"] else st.error)(md(m["text"]))
        bt = common.proof("backtest")
        props = (bt["data"] or {}).get("self_growing_proposals") if isinstance(bt["data"], dict) else None
        with st.container(border=True):
            st.markdown("**Detector proposals** · fraud\\_manager only")
            st.caption(md(f"Source: {bt['source'] or 'missing'} · as of {bt['as_of'] or '—'} · {bt['mode']}" + (f" · {bt['error']}" if bt["error"] else "")))
            if not props:
                st.caption("No detector proposals in this deployment.")
            else:
                st.dataframe([{"proposal": p.get("proposal_id"), "name": p.get("name"), "cards": p.get("cards_matched"), "txns": p.get("txns_matched"),
                               "exposure_usd": p.get("total_exposure_usd"), "closed-case hits": len(p.get("closed_case_true_positives") or []),
                               "legit customers acted against": len(p.get("legitimate_customers_acted_against") or []),
                               "status": p.get("approval_status")} for p in props],
                             hide_index=True, width="stretch", column_config={"exposure_usd": st.column_config.NumberColumn("exposure", format="dollar")})
                _, dets_why = common.optional_import("engine.detectors")
                why = dets_why or ("read-only deployment" if ro else None) or ("Sign in first" if session is None else None) or \
                    (None if session.role == "fraud_manager" else "Needs fraud_manager")
                for p in props:
                    pid = str(p.get("proposal_id"))
                    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="center")
                    c1.markdown(f"{md(pid)} · {common.badge(str(p.get('approval_status') or 'pending'), 'orange')}")
                    p_why = why or (None if p.get("approval_status") in (None, "pending") else f"already {p.get('approval_status')}")
                    for col, decision, label in ((c2, "approved", "Approve…"), (c3, "rejected", "Reject…")):
                        if col.button(label, key=f"ap-det-{decision}-{_k(pid)}", disabled=bool(p_why), help=p_why):
                            _open_dialog("gate", gate_id=pid, decision=decision)
                    if (m := (st.session_state.get("ap_gate_msg") or {}).get(pid)):
                        (st.success if m["ok"] else st.error)(md(m["text"]))

if t_feedback.open:
    with t_feedback:
        st.caption("Analyst accept / reject decisions on findings (Challenge 8): recorded, not yet used by the engine.")
        try:
            fb = approvals_db.feedback_rows_ro(path=db_path)
        except sqlite3.Error as exc:
            fb = []
            st.warning(md(f"feedback log unreadable: {exc}"))
        if fb:
            st.dataframe(fb, hide_index=True, width="stretch")
        else:
            st.caption(md(f"No analyst feedback in {db_rel}."))

if t_store.open:
    with t_store:
        per_round = Counter(ROUNDS.get(_round_of(r.get("case_id")), "other") for r in inbox)
        st.markdown(f"Store: **{md(db_rel)}** · " + (f"modified {md(db_as_of)}" if db_path.is_file() else "not created yet") + " · " +
                    (" · ".join(f"{md(k)} {n} rows" for k, n in sorted(per_round.items())) or "0 rows"))
        if ro:
            st.caption("Read-only deployment: this file lives in the container and is wiped on redeploy unless a volume is attached.")
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

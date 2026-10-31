"""Security & drills (C10 break the agent, Stage 2 drills), served by the router as the 'security' page.

What docs/RESILIENCE.md claims for each fault, next to the evidence: the attack ablation (agent.security, run only on the
operator's click; the proof snapshot otherwise), the six planted injections as what the attacker demanded next to what
was decided, a hostile-input sandbox, the guardrail ledger over the recorded traces and the wrong-role approval drills
(approvals_db.run_rbac_drills, on a throwaway store). Viewing writes nothing: the approvals log is read through
approvals_db.audit_log_rows_ro, and every live module loads through common.optional_import.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

md = common.md

ctx = common.page_setup("security")
sec, sec_err = common.optional_import("agent.security")
snap = common.proof("attacks")
scans_p = common.proof("intake_scans")
ro = common.read_only()
src = ctx.source

TABS = ["Planted injections", "Attack ablation", "Hostile-input sandbox", "Guardrail ledger", "Wrong-role drills"]
KEY_ACTIONS = ("BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD")
RESULT_COLORS = {"blocked": "green", "NOT blocked": "red"}
STATUS_COLORS = {"evidence recorded": "green", "exercised": "green", "no evidence": "gray"}
# a keyword rule over the trigger text: what the attacker asked for, so the decision can be set next to it
DEMANDS = [
    (re.compile(r"\blegitimate\b|CLOSE_NO_FRAUD|close it|approve it|mark the file closed", re.I), "close as legitimate",
     lambda a: str((a.get("case") or {}).get("verdict")) == "legitimate"),
    (re.compile(r"skip the report|no need to file|do not file|not file any report|no report", re.I), "no SAR",
     lambda a: not bool((a.get("sar") or {}).get("file"))),
    (re.compile(r"do not escalate", re.I), "no escalation",
     lambda a: "ESCALATE_TO_ANALYST" not in {str(x.get("action")) for x in _final(a)}),
]
_P_DEMAND = re.compile(r"(?:fraud_probability|probability)\s*(?:=|to)\s*([01](?:\.\d+)?)", re.I)


# --------------------------------------------------------------------------- #
# Data                                                                        #
# --------------------------------------------------------------------------- #
def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def _float(x: object) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _short(name: object) -> str:
    return str(name or "").removeprefix("query:")


def _run_attacks() -> None:
    """Button callback: the ablation in-process, stored with its time for this session only."""
    t0 = time.perf_counter()
    try:
        result = sec.run_attack_ablation()
    except Exception as exc:  # shown with its reason; the snapshot stays on screen
        st.session_state["sec_attacks_error"] = f"{type(exc).__name__}: {exc}"
        return
    st.session_state["sec_attacks"] = {"result": result, "at": time.strftime("%H:%M:%S"), "elapsed_s": time.perf_counter() - t0}
    st.session_state.pop("sec_attacks_error", None)


def _run_rbac() -> None:
    try:
        st.session_state["sec_rbac"] = {"rows": approvals_db.run_rbac_drills(), "at": time.strftime("%H:%M:%S"), "error": ""}
    except Exception as exc:  # shown in the drill row and the tab
        st.session_state["sec_rbac"] = {"rows": [], "at": time.strftime("%H:%M:%S"), "error": f"{type(exc).__name__}: {exc}"}


def _run_indep(ids: list[str]) -> None:
    try:
        st.session_state["sec_indep"] = {"rows": _independence_check(ids), "at": time.strftime("%H:%M:%S"), "error": ""}
    except Exception as exc:  # shown with its reason
        st.session_state["sec_indep"] = {"rows": {}, "at": time.strftime("%H:%M:%S"), "error": f"{type(exc).__name__}: {exc}"}


def _attack_data() -> tuple[dict | None, str, str, str]:
    """(ablation, label, source, as_of): this session's live run when there is one, else the snapshot."""
    live = st.session_state.get("sec_attacks")
    if isinstance(live, dict) and isinstance(live.get("result"), dict):
        return live["result"], f"live at {live['at']}", "agent.security.run_attack_ablation (this session)", str(live["at"])
    if isinstance(snap["data"], dict):
        return snap["data"], f"snapshot {snap['generated_at'] or snap['as_of']}", snap["source"], snap["generated_at"] or snap["as_of"]
    return None, "not measured here", snap["source"] or common.rel(common.SNAPSHOT_PATH), snap["error"] or "missing"


def _audit() -> tuple[list[dict], str | None]:
    try:
        return approvals_db.audit_log_rows_ro(common.approvals_db_path()), None
    except sqlite3.Error as exc:
        return [], f"audit log unreadable: {exc}"


def _absent(names: tuple[str, ...]) -> list[str]:
    out = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
        except (ImportError, ValueError):
            out.append(name)
    return out


def _run_ids(all_sources: bool) -> list[str]:
    if all_sources:
        return [s.id for s in common.sources() if s.kind == "run"]
    return [ctx.source_id] if src is not None and src.kind == "run" else []


def _events(sids: list[str]) -> list[dict]:
    """Every guardrail event of the given run sources, with the source, the case, its state and its age."""
    out = []
    for sid in sids:
        for cid in common.list_cases_in_run(sid):
            v = common.case_view(sid, cid)
            for ev in common.guardrail_events(v):
                out.append({**ev, "source": sid, "case": cid, "state": v.state, "age": v.last_write_age_s})
    return out


def _example(evs: list[dict]) -> str:
    """The newest event (the most recently written case), described verbatim."""
    if not evs:
        return ""
    ev = min(evs, key=lambda e: e["age"] if e["age"] is not None else float("inf"))
    t = _float(ev.get("t"))
    head = f"{ev['source']} {ev['case']} t={t:.2f} s: " if t is not None else f"{ev['source']} {ev['case']}: "
    if ev["type"] == "failed_call":
        return head + f"{_short(ev.get('name'))} '{ev.get('error', '')}' · case {'answered' if ev['state'] == 'done' else ev['state']}"
    if ev["type"] == "clamp":
        return head + f"LLM p {ev.get('llm', '?')} → clamped p {ev.get('clamped', '?')}"
    if ev["type"] == "security_alert":
        return head + f"{ev.get('text', '')} ({', '.join(ev.get('codes') or [])})"
    if ev["type"] in ("case_failed", "structured_retry"):
        return head + f"{ev.get('text', '')}: {str(ev.get('error', ''))[:160]}"
    return head + str(ev.get("text", ""))


def _decision(answer: dict | None) -> dict:
    if not isinstance(answer, dict):
        return {}
    final = _final(answer)
    acts = [str(x.get("action")) for x in final]
    key = [a for a in acts if a in KEY_ACTIONS] or [a for a in acts if a != "CREATE_CASE"]
    return {"verdict": str((answer.get("case") or {}).get("verdict") or ""), "p": _float((answer.get("case") or {}).get("fraud_probability")),
            "key": key, "pairs": [(str(x.get("action")), str(x.get("route", "auto"))) for x in final],
            "sar": bool((answer.get("sar") or {}).get("file"))}


def _demands(text: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = [(label, fn) for rx, label, fn in DEMANDS if rx.search(text or "")]
    m = _P_DEMAND.search(text or "")
    if m:
        want = float(m.group(1))
        out.append((f"p = {want:g}", lambda a, w=want: abs((_float((a.get("case") or {}).get("fraud_probability")) or -9) - w) < 0.05))
    return out


def _coincides(answer: dict | None, demands: list[tuple[str, object]]) -> str:
    if not isinstance(answer, dict):
        return "—"
    return " · ".join(f"{label}: {'yes' if fn(answer) else 'no'}" for label, fn in demands) or "no demand parsed"


def _independence_check(ids: list[str]) -> dict:
    """engine.run_cases.run_one on each planted case with its trigger text, then with the text blanked (no LLM, no files)."""
    rc, rc_err = common.optional_import("engine.run_cases")
    ff, ff_err = common.optional_import("engine.facts_duckdb")
    if rc is None or ff is None:
        raise RuntimeError(rc_err or ff_err)
    fx = ff.DuckFacts()
    rows = {}
    for c in rc.contexts(fx, ids):
        with_text = _decision(rc.run_one(c, fx)["answer"])
        blanked = _decision(rc.run_one(dataclasses.replace(c, trigger_text=""), fx)["answer"])
        rows[c.case_id] = {"with": with_text, "blank": blanked,
                           "same": (with_text["verdict"], sorted(with_text["pairs"]), with_text["sar"], with_text["p"])
                           == (blanked["verdict"], sorted(blanked["pairs"]), blanked["sar"], blanked["p"])}
    return rows


def _decided_line(d: dict) -> str:
    if not d:
        return "no answer in this source"
    return f"decided {d['verdict']} · {' · '.join(d['key']) or 'no action'}"


def _action_badges(d: dict) -> str:
    return " ".join(common.action_badge(a, r) for a, r in d.get("pairs", []) if a != "CREATE_CASE") + " " + common.badge(
        "SAR filed" if d.get("sar") else "SAR not filed", "red" if d.get("sar") else "gray")


# --------------------------------------------------------------------------- #
# Header and scoreboard                                                       #
# --------------------------------------------------------------------------- #
ab, ab_label, ab_source, ab_as_of = _attack_data()
common.page_header(
    "Security & drills",
    "What docs/RESILIENCE.md claims for each fault, next to the evidence in the recorded traces, the approvals log and the defences run now.",
    eyebrow="C10 break the agent · Stage 2 drills",
    chips=[(f"Attacks: {ab_label}", "green" if ab_label.startswith("live") else "orange" if ab else "gray"),
           ("agent.security available" if sec is not None else f"agent.security: {sec_err}", "gray")],
)
for b in ctx.banners:
    st.warning(md(b))

pack_rows, pack_note = common.final_round_pack()
pack_text = {r["case_id"]: r.get("trigger_text", "") for r in pack_rows}
attacks = (ab or {}).get("attacks") or []
planted_ids = sorted({str(a.get("case_id")) for a in attacks if a.get("planted_in_dataset")})
scans = scans_p["data"] if isinstance(scans_p["data"], dict) else {}
if not planted_ids:
    planted_ids = sorted(cid for cid, s in scans.items() if isinstance(s, dict) and s.get("attack_detected"))
clean_ids = [cid for cid in sorted(pack_text) if cid not in planted_ids]

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    slot = st.container()
    n_label = f"Run the {ab['total_attacks']} attacks now" if ab and ab.get("total_attacks") else "Run the attacks now"
    st.button(n_label, type="primary", icon=":material/play_arrow:", key="sec_run_attacks", disabled=sec is None, on_click=_run_attacks,
              help=None if sec is not None else md(f"agent.security not available: {sec_err}"), width="stretch")
    live = st.session_state.get("sec_attacks")
    common.measured("Attacks blocked", f"{ab['attacks_blocked']}/{ab['total_attacks']}" if ab else None, source=ab_source, as_of=ab_as_of,
                    how="each attack through the sanitizer, the tip-off guard, the approvals module on a scratch store and the probability clamp; "
                        "blocked is observed, never assumed", container=slot)
    slot.caption(md(f"{ab_label}" + (f" · measured in {live['elapsed_s']:.2f} s" if isinstance(live, dict) and live.get("elapsed_s") is not None else "")))
    if st.session_state.get("sec_attacks_error"):
        st.error(md(f"run_attack_ablation failed: {st.session_state['sec_attacks_error']}"))
common.measured("Planted in the pack", ab.get("planted_in_pack") if ab else None, source=ab_source, as_of=ab_as_of,
                how="attacks whose text is read from the Final Round case pack", container=c2)
common.measured("Synthetic", ab.get("synthetic_drills") if ab else None, source=ab_source, as_of=ab_as_of,
                how="attacks written for the ablation (text variants, tip-off, approval and clamp drills)", container=c3)

# false alarms: the sanitizer over the triggers that carry no planted attack
fa_value, fa_source, fa_as_of = None, scans_p["source"] or "agent.security.sanitize_trigger_text", scans_p["generated_at"] or scans_p["as_of"] or "missing"
if sec is not None and clean_ids and all(pack_text.get(c) for c in clean_ids):
    hits = [c for c in clean_ids if sec.sanitize_trigger_text(c, pack_text[c]).attack_detected]
    fa_value, fa_source, fa_as_of = f"{len(hits)}/{len(clean_ids)}", "agent.security.sanitize_trigger_text over the case pack", "computed on this page"
elif scans:
    clean_scans = [c for c in scans if c not in planted_ids]
    hits = [c for c in clean_scans if (scans[c] or {}).get("attack_detected")]
    fa_value = f"{len(hits)}/{len(clean_scans)}" if clean_scans else None
common.measured("False alarms on clean triggers", fa_value, source=fa_source, as_of=fa_as_of,
                how="non-planted Final Round triggers on which the sanitizer reports an attack", container=c4)

audit_rows, audit_err = _audit()
refused = [r for r in audit_rows if str(r.get("outcome")) != "ok"]
by_outcome = Counter(str(r.get("outcome")) for r in refused)
common.measured("Refused approval attempts", len(refused) if not audit_err else None, source=common.rel(common.approvals_db_path()),
                as_of=common.as_of(common.approvals_db_path()),
                how="approval_audit_log rows whose outcome is not ok: " + (", ".join(f"{k} {v}" for k, v in sorted(by_outcome.items())) or "none"),
                container=c5)
if audit_err:
    c5.caption(md(audit_err))

# the wrong-role drill runs once per session on a throwaway store (it never touches APPROVALS_DB)
if "sec_rbac" not in st.session_state:
    _run_rbac()
rbac = st.session_state["sec_rbac"]

# --------------------------------------------------------------------------- #
# Drill matrix                                                                #
# --------------------------------------------------------------------------- #
st.markdown("#### Stage 2 drills")
default_all = src is None or src.kind != "run"
all_src = st.toggle("Count across every run source", value=default_all, key="sec_drill_all",
                    help="Off: only the selected source's traces. A submitted source has answer files and no trace.")
sids = _run_ids(all_src)
evs = _events(sids)
by_type: dict[str, list[dict]] = {}
for ev in evs:
    by_type.setdefault(ev["type"], []).append(ev)
scope = ", ".join(sids) if sids else f"{ctx.source_id or 'no source'} (no trace: answer files only)"


def _count(*types: str) -> int:
    return sum(len(by_type.get(t, [])) for t in types)


def _evidence(parts: list[tuple[str, int]], example_types: tuple[str, ...]) -> str:
    counts = " · ".join(f"{label} {n}" for label, n in parts)
    ex = _example([e for t in example_types for e in by_type.get(t, [])])
    return counts + (f" · newest: {ex}" if ex else "")


ab_rows = {str(a.get("attack_id")): a for a in attacks}
live_ab = isinstance(st.session_state.get("sec_attacks"), dict)
text_attacks = [a for a in attacks if "clean_text" in a or a.get("planted_in_dataset")]  # the sanitizer's attacks: tip-off, approval and clamp rows carry no clean text
atk10 = ab_rows.get("ATK-10")
answered_after_fail = sorted({f"{e['source']} {e['case']}" for e in by_type.get("failed_call", []) if e["state"] == "done"})

# same case run twice: decisions across the Final Round sources, and resumed invocations
final_srcs = [s for s in common.sources() if s.round == (src.round if src else "final")]
multi = same = 0
for cid in sorted(pack_text):
    ans = [common.case_view(s.id, cid).answer for s in final_srcs]
    ans = [a for a in ans if isinstance(a, dict)]
    if len(ans) >= 2:
        multi += 1
        same += all(common.agreement(ans[0], a)["same_decision"] for a in ans[1:])
resumes = [f"{s.id} re-run at {inv['started']} ({inv['cases']} case(s))" for s in final_srcs if s.kind == "run"
           for inv in s.invocations if inv.get("resume")]
dirst = approvals_db.directory_status()
absent = _absent(("agent.security", "engine.run_cases", "schema_engine.drift", "duckdb"))
rbac_ok = sum(1 for r in rbac["rows"] if r.get("blocked"))

drills = [
    {"Drill": "Dependency failing mid-run",
     "What the system does": "Graph or MCP unreachable: retries with backoff, then the case fails with no answer.json; a failed tool call is logged ok=false with its error and is never treated as evidence.",
     "Evidence": _evidence([("failed calls", _count("failed_call")), ("case failed notes", _count("case_failed"))], ("failed_call", "case_failed"))
     + (f" · answered after a failed call: {', '.join(answered_after_fail)}" if answered_after_fail else ""),
     "Exercised now": "—", "n": _count("failed_call", "case_failed"), "now": False},
    {"Drill": "Model timeout or garbage",
     "What the system does": "Structured phases retry on invalid output; a probability more than 0.10 from the engine's is clamped; an inadmissible action set is re-prompted; a SAR failing validation falls back to the attested narrative.",
     "Evidence": _evidence([("structured retries", _count("structured_retry")), ("clamps", _count("clamp")), ("policy re-prompts", _count("policy_reprompt")),
                            ("SAR validation failed", _count("sar_validation_failed")), ("SAR fallback", _count("sar_fallback"))],
                           ("structured_retry", "clamp", "policy_reprompt", "sar_fallback")),
     "Exercised now": f"ATK-10 clamp: {atk10.get('observed')} ({ab_label})" if atk10 else "—",
     "n": _count("structured_retry", "clamp", "policy_reprompt", "sar_validation_failed", "sar_fallback"), "now": bool(atk10 and live_ab)},
    {"Drill": "Hostile input",
     "What the system does": "Text is NFKC-normalised, zero-width characters removed and every instruction sentence dropped before the model sees it; channel and amount claims are checked against the record.",
     "Evidence": _evidence([("security alert notes", _count("security_alert"))], ("security_alert",)),
     "Exercised now": (f"{sum(1 for a in text_attacks if a.get('blocked'))}/{len(text_attacks)} text attacks blocked ({ab_label})" if text_attacks else "—"),
     "n": _count("security_alert"), "now": live_ab},
    {"Drill": "Approval by the wrong role",
     "What the system does": "decide() accepts only a session from authenticate(); the role comes from the server directory; decided rows and the audit log are immutable.",
     "Evidence": f"refused attempts in the audit log {len(refused)}" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(by_outcome.items()))})" if by_outcome else "")
     + (f" · newest: {refused[0].get('at')} {refused[0].get('actor')} {refused[0].get('action')} → {refused[0].get('outcome')}" if refused else ""),
     "Exercised now": (f"sandbox drill {rbac_ok}/{len(rbac['rows'])} refused by the server at {rbac['at']}" if rbac["rows"] else f"sandbox drill failed: {rbac['error']}"),
     "n": len(refused), "now": bool(rbac["rows"])},
    {"Drill": "Same case run twice",
     "What the system does": "Graph writes are keyed upserts; case memory excludes the case itself and later cases; --resume picks up unfinished cases.",
     "Evidence": f"same decision in every source {same}/{multi} cases with two or more answers ({', '.join(s.id for s in final_srcs)})"
     + (f" · resumed: {'; '.join(resumes)}" if resumes else " · no resumed invocation"),
     "Exercised now": "—", "n": multi + len(resumes), "now": False},
    {"Drill": "Fail closed",
     "What the system does": "No approver directory: nobody can approve. Read-only: no agent spawn and no graph writes. A missing pack, module or graph shows a banner instead of a traceback.",
     "Evidence": f"directory {'missing' if not dirst['exists'] else 'unreadable' if dirst['error'] else str(dirst['n']) + ' principals'} · "
                 f"audit denials {sum(v for k, v in by_outcome.items() if k.startswith('denied'))}",
     "Exercised now": f"approvals: {'no decision control enabled (no principals)' if not dirst['n'] else 'enabled for signed-in roles'} · "
                      f"DEPLOY_READONLY {'on' if ro else 'off'} · absent here: {', '.join(absent) or 'none'}"
                      + (f" · pack: {pack_note}" if pack_note else ""),
     "n": 1, "now": True},
]
st.dataframe(
    [{"Drill": d["Drill"], "Status": ["exercised" if d["now"] else "evidence recorded" if d["n"] else "no evidence"],
      "What the system does": d["What the system does"], "Evidence in the source": d["Evidence"], "Exercised now": d["Exercised now"]} for d in drills],
    hide_index=True, width="stretch", height=35 * len(drills) + 38, key="sec_drills",
    column_config={
        "Drill": st.column_config.TextColumn("Drill", pinned=True),
        "Status": st.column_config.MultiselectColumn("Status", options=list(STATUS_COLORS), color=list(STATUS_COLORS.values()),
                                                     help="exercised = run on this page now; evidence recorded = found in the traces or the audit log"),
        "What the system does": st.column_config.TextColumn("What the system does", width="large", help="the claim, from docs/RESILIENCE.md"),
        "Evidence in the source": st.column_config.TextColumn("Evidence in the source", width="large", help=f"phases.jsonl notes and calls.jsonl ok=false rows of {scope}"),
        "Exercised now": st.column_config.TextColumn("Exercised now", width="medium"),
    },
)
st.caption(md(f"Traces: {scope} · approvals: {common.rel(common.approvals_db_path())} (read-only) · claims: docs/RESILIENCE.md"))

# --------------------------------------------------------------------------- #
# Tabs                                                                        #
# --------------------------------------------------------------------------- #
t_planted, t_ablation, t_sandbox, t_ledger, t_rbac = st.tabs(TABS, key="security_tab", on_change="rerun", default=TABS[0])

if t_planted.open:
    with t_planted:
        if not planted_ids:
            st.info(md(f"No planted injection is known on this deployment: {snap['error'] or 'the ablation has not run'}."))
        indep = st.session_state.get("sec_indep")
        rc_ok = importlib.util.find_spec("engine") is not None and not _absent(("engine.run_cases", "duckdb"))
        why = None if rc_ok and not ro else ("read-only deployment" if ro else "engine/ or duckdb not available in this deployment")
        cols = st.columns([3, 2], vertical_alignment="center")
        cols[0].caption("A decision that coincides with the attacker's demand does not show the attack worked, nor that it failed. "
                        "Independence from the text is shown only when measured: the operator check re-runs the deterministic engine "
                        "(engine.run_cases, no LLM, no files written) with each trigger text blanked.")
        cols[1].button("Re-run the engine with the triggers blanked", key="sec_indep_run", icon=":material/science:", disabled=bool(why),
                       help=md(why) if why else None, on_click=_run_indep, args=(planted_ids,))
        if isinstance(indep, dict) and indep.get("error"):
            st.error(md(f"Operator check failed: {indep['error']}"))
        run_srcs = [s for s in common.sources() if s.kind == "run" and s.round == "final"]
        for cid in planted_ids:
            atk = next((a for a in attacks if a.get("case_id") == cid), {})
            payload = str(atk.get("payload") or pack_text.get(cid) or "")
            scan = scans.get(cid) if isinstance(scans.get(cid), dict) else {}
            codes = list(atk.get("finding_codes") or scan.get("codes") or [])
            clean = atk.get("clean_text") if atk.get("clean_text") is not None else scan.get("clean_text")
            demands = _demands(payload)
            with st.container(border=True, key=f"planted-{cid}"):
                st.markdown(f"**{md(cid)}** · {md(atk.get('vector') or 'planted injection')}")
                if payload:
                    common.untrusted_text("What the attacker wrote (trigger text)", payload, codes=codes, clean_text=clean,
                                          source=ab_label, key=f"planted-text-{cid}")
                else:
                    st.caption(md(f"Trigger text not available here ({pack_note or 'not in the case pack'})."))
                st.markdown("**Demanded:** " + (" ".join(common.badge(d[0], "orange") for d in demands) or md("no demand parsed by the keyword rule")))
                rows = []
                for sid in dict.fromkeys([ctx.source_id, "submitted"]):
                    if not sid:
                        continue
                    v = common.case_view(sid, cid)
                    d = _decision(v.answer)
                    rows.append({"Source": sid, "Decided": _decided_line(d) if v.ok else f"{common.STATE_LABELS.get(v.state, v.state)}: no decision",
                                 "Final actions": ", ".join(f"{a} · {r}" for a, r in d.get("pairs", []) if a != "CREATE_CASE") or "—",
                                 "SAR": ("filed" if d.get("sar") else "not filed") if d else "—", "p(fraud)": d.get("p"),
                                 "Coincides with the demand": _coincides(v.answer, demands)})
                st.dataframe(rows, hide_index=True, width="stretch", height=35 * len(rows) + 38,
                             column_config={"p(fraud)": st.column_config.ProgressColumn("p(fraud)", min_value=0.0, max_value=1.0, format="%.2f"),
                                            "Decided": st.column_config.TextColumn("Decided", width="medium"),
                                            "Coincides with the demand": st.column_config.TextColumn(
                                                "Coincides with the demand", help="keyword rule over the trigger text; coinciding is not causing")})
                notes = []
                for s in run_srcs:
                    alerts = [e for e in common.guardrail_events(common.case_view(s.id, cid)) if e["type"] == "security_alert"]
                    for e in alerts:
                        notes.append({"Live source": s.id, "Intake codes": ", ".join(e.get("codes") or []), "t (s)": _float(e.get("t"))})
                    if not alerts:
                        notes.append({"Live source": s.id, "Intake codes": "no security alert note", "t (s)": None})
                if notes:
                    st.dataframe(notes, hide_index=True, width="stretch", height=35 * len(notes) + 38,
                                 column_config={"t (s)": st.column_config.NumberColumn("t (s)", format="%.2f", help="seconds since the case started")})
                row = (indep or {}).get("rows", {}).get(cid) if isinstance(indep, dict) else None
                if row:
                    st.markdown(common.badge(f"engine decision independent of the text: {'yes' if row['same'] else 'NO'} (measured {indep['at']})",
                                             "green" if row["same"] else "red")
                                + " " + md(f"with text: {_decided_line(row['with'])} · text blanked: {_decided_line(row['blank'])}"))
                else:
                    st.markdown(common.badge("independence: not measured", "gray"))

if t_ablation.open:
    with t_ablation:
        if not attacks:
            st.info(md(f"No ablation on this deployment ({snap['error'] or 'snapshot missing'}); "
                       + ("run it with the button above." if sec is not None else f"agent.security: {sec_err}.")))
        else:
            st.caption(md(f"{len(attacks)} attacks · {ab_label} · source {ab_source}"))
            st.dataframe(
                [{"Attack": a.get("attack_id"), "Result": ["blocked" if a.get("blocked") else "NOT blocked"], "Case": a.get("case_id"),
                  "Planted": bool(a.get("planted_in_dataset")), "Vector": a.get("vector"), "Finding codes": ", ".join(a.get("finding_codes") or []),
                  "Observed": a.get("observed")} for a in attacks],
                hide_index=True, width="stretch", height=35 * len(attacks) + 38, key="sec_ablation",
                column_config={
                    "Attack": st.column_config.TextColumn("Attack", pinned=True),
                    "Result": st.column_config.MultiselectColumn("Result", options=list(RESULT_COLORS), color=list(RESULT_COLORS.values())),
                    "Planted": st.column_config.CheckboxColumn("Planted", help="text read from the Final Round case pack"),
                    "Observed": st.column_config.TextColumn("Observed", width="large", help="what the defence did; for planted cases, the answer file's outcome"),
                })
            for a in attacks:
                with st.expander(md(f"{a.get('attack_id')} · {a.get('vector')} · {'blocked' if a.get('blocked') else 'NOT blocked'}")):
                    common.untrusted_text("Payload", str(a.get("payload") or ""), codes=list(a.get("finding_codes") or []),
                                          clean_text=a.get("clean_text"), source=ab_label, key=f"atk-{a.get('attack_id')}")

if t_sandbox.open:
    with t_sandbox:
        if sec is None:
            st.text_area("Hostile text", max_chars=5000, key="sec_sandbox", disabled=True, help="agent.security not shipped in this deployment")
            st.info(md(f"agent.security not shipped in this deployment ({sec_err})."))
        else:
            text = st.text_area("Paste hostile text: it goes through agent.security.sanitize_trigger_text and nothing is stored",
                                max_chars=5000, key="sec_sandbox", height=120)
            if text and text.strip():
                res = sec.sanitize_trigger_text("SANDBOX", text)
                common.untrusted_text("Your input", text, codes=[f.code for f in res.findings], clean_text=res.clean_text, source="computed on this page",
                                      key="sandbox")
                if res.findings:
                    st.dataframe([{"Code": f.code, "Severity": f.severity, "What": f.description, "Matched": f.matched_text} for f in res.findings],
                                 hide_index=True, width="stretch", height=35 * len(res.findings) + 38)
            else:
                st.caption("Nothing to scan yet.")

if t_ledger.open:
    with t_ledger:
        lc1, lc2 = st.columns([1, 3], vertical_alignment="bottom")
        l_all = lc1.toggle("All run sources", value=default_all, key="sec_ledger_all")
        l_sids = _run_ids(l_all)
        if not l_sids:
            st.info(md(f"{ctx.source_id or 'This source'} has answer files and no trace; switch on 'All run sources'."))
        ledger = common.guardrail_ledger(l_sids[0], all_sources=l_all) if l_sids else []
        types = sorted({r["type"] for r in ledger})
        pick = lc2.multiselect("Event types", types, key="sec_ledger_types", placeholder="all types")
        shown = [r for r in ledger if not pick or r["type"] in pick]
        st.dataframe([{"Event type": r["type"], "Count": r["count"], "Cases": ", ".join(sorted(r["cases"])), "Sources": ", ".join(r["sources"])} for r in shown],
                     hide_index=True, width="stretch", height=35 * max(len(shown), 1) + 38,
                     column_config={"Cases": st.column_config.TextColumn("Cases", width="large")})
        l_evs = [e for e in (evs if (l_all == all_src) else _events(l_sids)) if not pick or e["type"] in pick]
        with st.expander(md(f"Events ({len(l_evs)})")):
            st.dataframe([{"Source": e["source"], "Case": e["case"], "Type": e["type"], "Phase": e.get("phase", ""), "t (s)": _float(e.get("t")),
                           "Text": e.get("text", ""), "Detail": ", ".join(e.get("codes") or []) or str(e.get("error") or e.get("violations") or "")}
                          for e in l_evs], hide_index=True, width="stretch")
        st.caption(md(f"Source: runs/<source>/HHF-*/phases.jsonl notes and calls.jsonl ok=false rows · {', '.join(l_sids) or 'none'}"))

if t_rbac.open:
    with t_rbac:
        st.button("Run the drills again", key="sec_rbac_run", icon=":material/refresh:", on_click=_run_rbac)
        if rbac["error"]:
            st.error(md(f"Drill failed: {rbac['error']}"))
        st.caption(md(f"approvals_db.run_rbac_drills on a throwaway store and directory in a temporary folder (APPROVALS_DB is never touched) · run at {rbac['at']}"))
        st.dataframe([{"Attack": r.get("attack_id"), "Result": ["blocked" if r.get("blocked") else "NOT blocked"], "Attempt": r.get("vector"),
                       "Server message": r.get("observed"), "At (UTC)": r.get("at")} for r in rbac["rows"]],
                     hide_index=True, width="stretch", height=35 * max(len(rbac["rows"]), 1) + 38,
                     column_config={"Result": st.column_config.MultiselectColumn("Result", options=list(RESULT_COLORS), color=list(RESULT_COLORS.values())),
                                    "Server message": st.column_config.TextColumn("Server message", width="large")})
        common.page_link("approvals", "Approvals audit log", icon=":material/gavel:")

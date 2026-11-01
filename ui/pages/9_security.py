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

from ui import approvals_db, common, design  # noqa: E402

md, esc, tag = common.md, design.esc, design.tag

ctx = common.page_setup("security")
sec, sec_err = common.optional_import("agent.security")
snap = common.proof("attacks")
scans_p = common.proof("intake_scans")
ro = common.read_only()
src = ctx.source

TABS = ["Drills", "Planted injections", "Attacks by family"]
FAMILIES = [("Planted in the case pack", "the intake sanitizer, then the decision"), ("Text variants", "the intake sanitizer"),
            ("Tip-off", "the tip-off guard on evidence requests"), ("Approval abuse", "the approvals module on a scratch store"),
            ("Model output", "the probability clamp")]
INFO_TYPES = ("memory_exclusion", "vector_index")  # notes the pipeline writes for information; they are not defences
KEY_ACTIONS = ("BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD")
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


def _family(a: dict) -> str:
    """The family of one ablation row, from what the row records: planted text, a sanitizer variant (it carries clean_text),
    a tip-off (TIPOFF code), the clamp (ATK-10's probability move) or an approval attempt (the rest)."""
    if a.get("planted_in_dataset"):
        return FAMILIES[0][0]
    if "clean_text" in a:
        return FAMILIES[1][0]
    if "TIPOFF" in (a.get("finding_codes") or []):
        return FAMILIES[2][0]
    if "moves p" in str(a.get("vector") or "") or str(a.get("observed") or "").startswith("clamped"):
        return FAMILIES[4][0]
    return FAMILIES[3][0]


# --------------------------------------------------------------------------- #
# The measurements behind the first screen                                    #
# --------------------------------------------------------------------------- #
ab, ab_label, ab_source, ab_as_of = _attack_data()
live = st.session_state.get("sec_attacks")
pack_rows, pack_note = common.final_round_pack()
pack_text = {r["case_id"]: r.get("trigger_text", "") for r in pack_rows}
attacks = [a for a in (ab or {}).get("attacks") or [] if isinstance(a, dict)]
planted_ids = sorted({str(a.get("case_id")) for a in attacks if a.get("planted_in_dataset")})
scans = scans_p["data"] if isinstance(scans_p["data"], dict) else {}
if not planted_ids:
    planted_ids = sorted(cid for cid, s in scans.items() if isinstance(s, dict) and s.get("attack_detected"))
clean_ids = [cid for cid in sorted(pack_text) if cid not in planted_ids]

# false alarms: the sanitizer over the triggers that carry no planted attack
fa_hits: int | None = None
fa_n = 0
fa_source, fa_as_of = scans_p["source"] or "agent.security.sanitize_trigger_text", scans_p["generated_at"] or scans_p["as_of"] or "missing"
if sec is not None and clean_ids and all(pack_text.get(c) for c in clean_ids):
    fa_hits = sum(1 for c in clean_ids if sec.sanitize_trigger_text(c, pack_text[c]).attack_detected)
    fa_n, fa_source, fa_as_of = len(clean_ids), "agent.security.sanitize_trigger_text over the case pack", "computed on this page"
elif scans:
    clean_scans = [c for c in scans if c not in planted_ids]
    if clean_scans:
        fa_hits, fa_n = sum(1 for c in clean_scans if (scans[c] or {}).get("attack_detected")), len(clean_scans)

# refused attempts: the approvals audit log, read-only; a missing database is not a zero
db_path = Path(common.approvals_db_path())
if db_path.exists():
    audit_rows, audit_err = _audit()
else:
    audit_rows, audit_err = [], f"approvals database absent ({db_path.name})"
refused = [r for r in audit_rows if str(r.get("outcome")) != "ok"]
by_outcome = Counter(str(r.get("outcome")) for r in refused)

# the wrong-role drill runs once per session on a throwaway store (it never touches APPROVALS_DB)
if "sec_rbac" not in st.session_state:
    _run_rbac()
rbac = st.session_state["sec_rbac"]
rbac_ok = sum(1 for r in rbac["rows"] if r.get("blocked"))
rbac_n = len(rbac["rows"])

# --------------------------------------------------------------------------- #
# First screen: the answer, four figures and the sandbox                      #
# --------------------------------------------------------------------------- #
common.page_header(
    "Security & drills",
    f"Attacks: {ab_label} ({ab_source}) · wrong-role drill: approvals_db.run_rbac_drills at {rbac['at']} · "
    f"audit log: {common.rel(db_path)} (read-only) · claims: docs/RESILIENCE.md",
)

if ab and ab.get("attacks_blocked") == ab.get("total_attacks"):
    first = f'Not by anything tried here: <b>{esc(ab["attacks_blocked"])} of {esc(ab["total_attacks"])}</b> attacks were <span class="green">blocked</span>'
elif ab:
    first = (f'<span class="red">Yes</span>: <b>{esc((ab.get("total_attacks") or 0) - (ab.get("attacks_blocked") or 0))} of '
             f'{esc(ab.get("total_attacks"))}</b> attacks were <span class="red">not blocked</span>')
else:
    first = "The attack ablation is not measured here"
fa_txt = (f'the sanitizer raised <b>{fa_hits}</b> false alarm{"" if fa_hits == 1 else "s"} on {fa_n} clean triggers' if fa_hits is not None
          else "false alarms are not measured here")
rb_txt = (f'the server refused <b>{rbac_ok} of {rbac_n}</b> wrong-role approvals' if rbac_n
          else f'the wrong-role drill did not run ({esc(rbac["error"] or "no rows")})')
design.html_block(f'<p class="tc tc-lead">{first}, {fa_txt}, and {rb_txt}.</p>')

left, right = st.columns([1, 1], gap="large")
with left:
    with st.container(border=True, key="strip-sec"):
        row1, row2 = st.columns(2), st.columns(2)
    common.measured("Attacks blocked", f"{ab['attacks_blocked']}/{ab['total_attacks']}" if ab else None, source=ab_source, as_of=ab_as_of,
                    what="attacks the defences stopped, of every attack in the ablation",
                    how="each attack through the sanitizer, the tip-off guard, the approvals module on a scratch store and the probability clamp; "
                        "blocked is observed, never assumed", container=row1[0])
    if ab:
        row1[0].caption(md(f"{ab.get('planted_in_pack')} planted, {ab.get('synthetic_drills')} synthetic"))
    common.measured("False alarms", f"{fa_hits}/{fa_n}" if fa_hits is not None else None, source=fa_source, as_of=fa_as_of,
                    what="non-planted Final Round triggers on which the sanitizer reports an attack", container=row1[1])
    row1[1].caption("clean Final Round triggers")
    common.measured("Wrong-role refusals", f"{rbac_ok}/{rbac_n}" if rbac_n else None, source="approvals_db.run_rbac_drills on a throwaway store",
                    as_of=str(rbac["at"]), what="wrong-role approval attempts the server refused",
                    how="the drills run on a temporary store and directory; APPROVALS_DB is never touched", container=row2[0])
    row2[0].caption(md(f"run at {rbac['at']}" if not rbac["error"] else f"drill failed: {rbac['error']}"))
    common.measured("Refused attempts in the audit log", len(refused) if not audit_err else None, source=common.rel(db_path), as_of=common.as_of(db_path),
                    what="approval_audit_log rows whose outcome is not ok",
                    how=", ".join(f"{k} {v}" for k, v in sorted(by_outcome.items())) or "none", container=row2[1])
    row2[1].caption(md(audit_err or (", ".join(f"{k} {v}" for k, v in sorted(by_outcome.items())) or "none refused")))
    n_atk = ab.get("total_attacks") if ab else None
    with st.container(horizontal=True, vertical_alignment="center", gap="small", key="sec-rerun"):
        st.button(f"Re-run the {n_atk} attacks now" if n_atk else "Run the attacks now", type="secondary", icon=":material/replay:", key="sec_run_attacks",
                  disabled=sec is None, on_click=_run_attacks, help=None if sec is not None else md(f"agent.security not available: {sec_err}"))
        shown = (f"showing: live at {live['at']}, {live['elapsed_s']:.2f} s" if isinstance(live, dict) and live.get("elapsed_s") is not None
                 else f"showing: {ab_label}")
        st.caption(md(shown))
    if st.session_state.get("sec_attacks_error"):
        st.error(md(f"run_attack_ablation failed: {st.session_state['sec_attacks_error']}"))
with right:
    design.html_block(design.section("", "Try an attack", "agent.security.sanitize_trigger_text · nothing is stored"))
    if sec is None:
        st.text_area("Hostile text", max_chars=5000, key="sec_sandbox", disabled=True, height=100, help=md(f"agent.security not shipped in this deployment ({sec_err})"))
        st.caption(md(f"Not available here: agent.security not shipped in this deployment ({sec_err})."))
    else:
        text = st.text_area("Hostile text", max_chars=5000, key="sec_sandbox", height=100)
        if text and text.strip():
            res = sec.sanitize_trigger_text("SANDBOX", text)
            common.untrusted_text("Your input", text, codes=[f.code for f in res.findings], clean_text=res.clean_text, source="computed on this page",
                                  key="sandbox")
            if res.findings:
                design.html_block('<div class="tc sc-t"><table class="tc-table"><thead><tr><th>Code</th><th>Severity</th><th>What</th></tr></thead><tbody>'
                                  + "".join(f'<tr><td class="mono">{esc(f.code)}</td><td>{esc(f.severity)}</td><td>{esc(f.description)}</td></tr>'
                                            for f in res.findings) + "</tbody></table></div>")

# --------------------------------------------------------------------------- #
# Below: the drills, the planted injections, the attacks by family            #
# --------------------------------------------------------------------------- #
default_all = src is None or src.kind != "run"
ab_rows = {str(a.get("attack_id")): a for a in attacks}
live_ab = isinstance(live, dict)
text_attacks = [a for a in attacks if "clean_text" in a or a.get("planted_in_dataset")]  # the sanitizer's attacks: tip-off, approval and clamp rows carry no clean text
atk10 = ab_rows.get("ATK-10")
final_srcs = [s for s in common.sources() if s.round == (src.round if src else "final")]
dirst = approvals_db.directory_status()
absent = _absent(("agent.security", "engine.run_cases", "schema_engine.drift", "duckdb"))

t_drills, t_planted, t_attacks = st.tabs(TABS, key="security_tab", default=TABS[0])

with t_drills:
    all_src = st.toggle("Count across every run source", value=default_all, key="sec_ledger_all",
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

    answered_after_fail = sorted({f"{e['source']} {e['case']}" for e in by_type.get("failed_call", []) if e["state"] == "done"})
    multi = same = 0
    for cid in sorted(pack_text):
        ans = [a for a in (common.case_view(s.id, cid).answer for s in final_srcs) if isinstance(a, dict)]
        if len(ans) >= 2:
            multi += 1
            same += all(common.agreement(ans[0], a)["same_decision"] for a in ans[1:])
    resumes = [f"{s.id} re-run at {inv['started']} ({inv['cases']} case(s))" for s in final_srcs if s.kind == "run"
               for inv in s.invocations if inv.get("resume")]
    drills = [
        {"Drill": "Dependency failing mid-run",
         "claim": "Graph or MCP unreachable: retries with backoff, then the case fails with no answer.json; a failed tool call is logged ok=false with its error and is never treated as evidence.",
         "evidence": _evidence([("failed calls", _count("failed_call")), ("case failed notes", _count("case_failed"))], ("failed_call", "case_failed"))
         + (f" · answered after a failed call: {', '.join(answered_after_fail)}" if answered_after_fail else ""),
         "now": "", "n": _count("failed_call", "case_failed")},
        {"Drill": "Model timeout or garbage",
         "claim": "Structured phases retry on invalid output; a probability more than 0.10 from the engine's is clamped; an inadmissible action set is re-prompted; a SAR failing validation falls back to the attested narrative.",
         "evidence": _evidence([("structured retries", _count("structured_retry")), ("clamps", _count("clamp")), ("policy re-prompts", _count("policy_reprompt")),
                                ("SAR validation failed", _count("sar_validation_failed")), ("SAR fallback", _count("sar_fallback"))],
                               ("structured_retry", "clamp", "policy_reprompt", "sar_fallback")),
         "now": f"ATK-10 clamp: {atk10.get('observed')} ({ab_label})" if atk10 else "", "live": bool(atk10 and live_ab),
         "n": _count("structured_retry", "clamp", "policy_reprompt", "sar_validation_failed", "sar_fallback")},
        {"Drill": "Hostile input",
         "claim": "Text is NFKC-normalised, zero-width characters removed and every instruction sentence dropped before the model sees it; channel and amount claims are checked against the record.",
         "evidence": _evidence([("security alert notes", _count("security_alert"))], ("security_alert",)),
         "now": (f"{sum(1 for a in text_attacks if a.get('blocked'))}/{len(text_attacks)} text attacks blocked ({ab_label})" if text_attacks else ""),
         "live": bool(text_attacks and live_ab),
         "n": _count("security_alert")},
        {"Drill": "Approval by the wrong role",
         "claim": "decide() accepts only a session from authenticate(); the role comes from the server directory; decided rows and the audit log are immutable.",
         "evidence": (f"refused attempts in the audit log {len(refused)}" if not audit_err else f"audit log {audit_err}")
         + (f" ({', '.join(f'{k} {v}' for k, v in sorted(by_outcome.items()))})" if by_outcome else "")
         + (f" · newest: {refused[0].get('at')} {refused[0].get('actor')} {refused[0].get('action')} → {refused[0].get('outcome')}" if refused else ""),
         "now": (f"sandbox drill {rbac_ok}/{rbac_n} refused by the server at {rbac['at']}" if rbac_n else f"sandbox drill failed: {rbac['error']}"),
         "live": bool(rbac_n),
         "n": len(refused)},
        {"Drill": "Same case run twice",
         "claim": "Graph writes are keyed upserts; case memory excludes the case itself and later cases; --resume picks up unfinished cases.",
         "evidence": f"same decision in every source {same}/{multi} cases with two or more answers ({', '.join(s.id for s in final_srcs)})"
         + (f" · resumed: {'; '.join(resumes)}" if resumes else " · no resumed invocation"),
         "now": "", "n": multi + len(resumes)},
        {"Drill": "Fail closed",
         "claim": "No approver directory: nobody can approve. Read-only: no agent spawn and no graph writes. A missing pack, module or graph shows a banner instead of a traceback.",
         "evidence": f"directory {'missing' if not dirst['exists'] else 'unreadable' if dirst['error'] else str(dirst['n']) + ' principals'} · "
                     f"audit denials {sum(v for k, v in by_outcome.items() if k.startswith('denied'))}",
         "now": f"approvals: {'no decision control enabled (no principals)' if not dirst['n'] else 'enabled for signed-in roles'} · "
                f"DEPLOY_READONLY {'on' if ro else 'off'} · absent here: {', '.join(absent) or 'none'}" + (f" · pack: {pack_note}" if pack_note else ""),
         "live": True,
         "n": 1},
    ]
    items = []
    for d in drills:
        state = tag("exercised now", "green") if d.get("live") else tag("evidence recorded") if d["n"] else tag("no evidence")
        now = f'<p><span class="sc-k">Exercised</span>{esc(d["now"])}</p>' if d["now"] else ""
        items.append(f'<li><div class="sc-drill-head"><b>{esc(d["Drill"])}</b>{state}</div><p class="sc-claim">{esc(d["claim"])}</p>'
                     f'<p><span class="sc-k">Evidence</span>{esc(d["evidence"])}</p>{now}</li>')
    design.html_block(f'<div class="tc"><ul class="sc-drills">{"".join(items)}</ul>'
                      f'<p class="sc-src">Traces: {esc(scope)} · approvals: {esc(common.rel(db_path))} (read-only) · claims: docs/RESILIENCE.md</p></div>')

with t_planted:
    indep = st.session_state.get("sec_indep")
    rc_ok = importlib.util.find_spec("engine") is not None and not _absent(("engine.run_cases", "duckdb"))
    why = None if rc_ok and not ro else ("read-only deployment" if ro else "engine/ or duckdb not available in this deployment")
    with st.container(horizontal=True, vertical_alignment="center", gap="medium", key="sec-indep-bar"):
        design.html_block('<p class="tc sc-caveat">A decision that matches the attacker\'s demand shows neither that the attack worked nor that it failed: '
                          '<b>coinciding is not causing</b>. Independence from the text is shown only when measured, by re-running the deterministic '
                          'engine (engine.run_cases, no LLM, no files written) with each trigger text blanked.</p>')
        st.button("Re-run the engine with the triggers blanked", key="sec_indep_run", icon=":material/science:", disabled=bool(why),
                  help=md(why) if why else None, on_click=_run_indep, args=(planted_ids,))
    if isinstance(indep, dict) and indep.get("error"):
        st.error(md(f"Operator check failed: {indep['error']}"))
    if not planted_ids:
        st.caption(md(f"No planted injection is known on this deployment: {snap['error'] or 'the ablation has not run'}."))
    else:
        trs = []
        for cid in planted_ids:
            atk = next((a for a in attacks if a.get("case_id") == cid), {})
            payload = str(atk.get("payload") or pack_text.get(cid) or "")
            scan = scans.get(cid) if isinstance(scans.get(cid), dict) else {}
            codes = list(atk.get("finding_codes") or scan.get("codes") or [])
            demands = _demands(payload)
            answer, where = None, ""
            for sid in dict.fromkeys([ctx.source_id, "submitted"]):
                v = common.case_view(sid, cid) if sid else None
                if v is not None and v.ok and isinstance(v.answer, dict):
                    answer, where = v.answer, sid
                    break
            d = _decision(answer)
            row = (indep or {}).get("rows", {}).get(cid) if isinstance(indep, dict) else None
            ind = (tag(f"yes, measured {indep['at']}", "green") if row and row["same"] else tag(f"NO, measured {indep['at']}", "red") if row
                   else '<span class="tc-muted" title="not measured">—</span>')
            trs.append(
                f'<tr><td class="mono">{esc(cid)}</td><td>{esc(atk.get("vector") or "planted injection")}</td>'
                f'<td>{" ".join(tag(x[0]) for x in demands) or "<span class=tc-muted>no demand parsed</span>"}</td>'
                f'<td>{esc(_decided_line(d)) if d else "<span class=tc-muted>no answer here</span>"}<span class="sub">{esc(where)}</span></td>'
                f'<td>{esc(_coincides(answer, demands))}</td><td class="mono quiet">{esc(", ".join(codes)) or "—"}</td><td>{ind}</td></tr>')
        design.html_block('<div class="tc sc-t sc-planted"><table class="tc-table"><thead><tr><th>Case</th><th>Vector</th><th>Demanded</th><th>Decided</th>'
                          '<th>Coincides with the demand</th><th>Flagged at intake</th><th>Independent of the text</th></tr></thead>'
                          f'<tbody>{"".join(trs)}</tbody></table>'
                          f'<p class="sc-src">Demands: a keyword rule over the trigger text · decisions: {esc(ctx.source_id or "no source")}, else submitted · '
                          f'codes: {esc(ab_label)}</p></div>')
        pick = st.segmented_control("Trigger text", planted_ids, key="sec_planted_pick", default=planted_ids[0]) or planted_ids[0]
        atk = next((a for a in attacks if a.get("case_id") == pick), {})
        scan = scans.get(pick) if isinstance(scans.get(pick), dict) else {}
        payload = str(atk.get("payload") or pack_text.get(pick) or "")
        if payload:
            common.untrusted_text(f"{pick} · what the attacker wrote (trigger text)", payload, codes=list(atk.get("finding_codes") or scan.get("codes") or []),
                                  clean_text=atk.get("clean_text") if atk.get("clean_text") is not None else scan.get("clean_text"),
                                  source=ab_label, key=f"planted-text-{pick}")
        else:
            st.caption(md(f"Trigger text not available here ({pack_note or 'not in the case pack'})."))

with t_attacks:
    if not attacks:
        st.caption(md(f"No ablation on this deployment ({snap['error'] or 'snapshot missing'})"
                      + (f"; agent.security: {sec_err}." if sec is None else ".")))
    else:
        groups: dict[str, list[dict]] = {}
        for a in attacks:
            groups.setdefault(_family(a), []).append(a)
        body = []
        for fam, defence in FAMILIES:
            rows = groups.get(fam) or []
            if not rows:
                continue
            n_ok = sum(1 for a in rows if a.get("blocked"))
            body.append(f'<tr class="sc-fam"><td colspan="5"><b>{esc(fam)}</b> · {n_ok}/{len(rows)} blocked · {esc(defence)}</td></tr>')
            for a in rows:
                res = tag("blocked", "green") if a.get("blocked") else tag("NOT blocked", "red")
                obs = " ".join(str(a.get("observed") or "").split())
                body.append(f'<tr><td class="mono">{esc(a.get("attack_id"))}</td><td>{esc(a.get("vector"))}</td>'
                            f'<td class="mono quiet">{esc(", ".join(a.get("finding_codes") or [])) or "—"}</td>'
                            f'<td>{res}</td><td class="quiet" title="{esc(obs)}">{esc(obs[:140] + ("…" if len(obs) > 140 else ""))}</td></tr>')
        design.html_block('<div class="tc sc-t sc-attacks"><table class="tc-table"><thead><tr><th>Attack</th><th>Vector</th><th>Finding codes</th><th>Result</th>'
                          f'<th>Observed</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
                          f'<p class="sc-src">{len(attacks)} attacks · {esc(ab_label)} · {esc(ab_source)}</p></div>')

# --------------------------------------------------------------------------- #
# Folded: the guardrail ledger (defences only), the wrong-role rows          #
# --------------------------------------------------------------------------- #
l_all = bool(st.session_state.get("sec_ledger_all", default_all))
l_sids = _run_ids(l_all)
ledger = common.guardrail_ledger(l_sids[0], all_sources=l_all) if l_sids else []
defences = [r for r in ledger if r["type"] not in INFO_TYPES]
info = [r for r in ledger if r["type"] in INFO_TYPES]
info_txt = " · ".join(f"{r['type']} {r['count']}" for r in info)
with st.expander(f"Guardrail ledger: {sum(r['count'] for r in defences)} defence events in {', '.join(l_sids) or 'no run source'}", expanded=False):
    if not l_sids:
        st.caption(md(f"{ctx.source_id or 'This source'} has answer files and no trace; count across every run source in Drills."))
    pick_types = st.multiselect("Event types", sorted(r["type"] for r in defences), key="sec_ledger_types", placeholder="all defence types")
    shown = [r for r in defences if not pick_types or r["type"] in pick_types]
    design.html_block('<div class="tc sc-t"><table class="tc-table"><thead><tr><th>Defence event</th><th class="num">Count</th><th>Cases</th><th>Sources</th></tr></thead><tbody>'
                      + "".join(f'<tr><td class="mono">{esc(r["type"])}</td><td class="num">{r["count"]}</td>'
                                f'<td class="mono quiet">{esc(", ".join(sorted(r["cases"]))) or "—"}</td><td class="quiet">{esc(", ".join(r["sources"])) or "—"}</td></tr>'
                                for r in shown) + "</tbody></table>"
                      + (f'<p class="sc-src">Informational notes, not defences: {esc(info_txt)}. ' if info else '<p class="sc-src">')
                      + 'Source: runs/&lt;source&gt;/HHF-*/phases.jsonl notes and calls.jsonl ok=false rows</p></div>')
with st.expander(f"Wrong-role drills: {rbac_ok}/{rbac_n} refused by the server (run at {rbac['at']})" if rbac_n else "Wrong-role drills", expanded=False):
    st.button("Run the drills again", key="sec_rbac_run", icon=":material/refresh:", on_click=_run_rbac)
    if rbac["error"]:
        st.error(md(f"Drill failed: {rbac['error']}"))
    design.html_block('<div class="tc sc-t"><table class="tc-table"><thead><tr><th>Attack</th><th>Attempt</th><th>Result</th><th>Server message</th><th>At (UTC)</th></tr></thead><tbody>'
                      + "".join(f'<tr><td class="mono">{esc(r.get("attack_id"))}</td><td>{esc(r.get("vector"))}</td>'
                                f'<td>{tag("blocked", "green") if r.get("blocked") else tag("NOT blocked", "red")}</td>'
                                f'<td class="quiet">{esc(r.get("observed"))}</td><td class="mono quiet">{esc(r.get("at"))}</td></tr>' for r in rbac["rows"])
                      + "</tbody></table><p class=\"sc-src\">approvals_db.run_rbac_drills on a throwaway store and directory in a temporary folder; "
                        "APPROVALS_DB is never touched</p></div>")
    common.page_link("approvals", "Approvals audit log", icon=":material/gavel:")

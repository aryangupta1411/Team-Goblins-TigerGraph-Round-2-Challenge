"""The Case file's first screen: the case drawn as a typology scheme, its indicators, and the decision beside them.

The scheme is the console's signature. Each of its five steps is built from fields of the case's own files and nothing
else: the alert (case pack trigger), the card and its exposure (answer.json case), the device and origin evidence
(the device-family claims and data-quality taints), the pattern (the detector exam scan, else the answer's pattern) and
the decision (the final actions and their routes, keyed to the rule the stop reason cites). Under each step sit the keys
of the evidence claims that support it (E1, E2 ...); hovering a key lights its row in the indicators table, clicking it
jumps there. Key colours are the scorecard's direction for the claim's evidence family, the same direction P3 used.

Everything is read-only and every value is escaped through design.esc().
"""
from __future__ import annotations

import re
from typing import Any

from ui import common, design

esc, glyph, tag = design.esc, design.glyph, design.tag

TRIGGER_LABELS = {"analyst_request": "Analyst request", "customer_report": "Customer report", "risk_score": "Risk-score alert"}
PATTERN_LABELS = {"undocumented": "Undocumented pattern", "card_testing": "Card testing", "out_of_region_use": "Out-of-region use",
                  "account_takeover": "Account takeover", "none": "No coordinated pattern", "": "No pattern recorded"}
ACTION_LABELS = {"CREATE_CASE": "Open case", "MONITOR_CONNECTED_CARDS": "Monitor connected cards", "MONITOR_CARD": "Monitor card",
                 "BLOCK_CARD": "Block card", "BLOCK_ALL_CARDS": "Block all cards", "FILE_REPORT": "File SAR", "DECLINE_TRANSACTION": "Decline transaction",
                 "ESCALATE_TO_ANALYST": "Escalate to analyst", "ALLOW_TRANSACTION": "Allow transaction", "CLOSE_NO_FRAUD": "Close, no fraud",
                 "STEP_UP_AUTH": "Step-up authentication", "VERIFY_WITH_CUSTOMER": "Verify with customer"}
VERDICT_WORD = {"fraud": "Fraud", "uncertain": "Uncertain", "legitimate": "Legitimate"}
STEP_TITLE = {"fraud": "Contain", "uncertain": "Hand to an analyst", "legitimate": "Close, no fraud"}
_RULE_RE = re.compile(r"(§\s?\d+(?:\s+test\s+\d+)?|\bR(?:10|[1-9])\b|\b3[ab]\b)", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.;])\s")
# Which scheme step a claim supports, from the query behind it first and its words second. common.family_of_evidence
# files every query-backed claim that is not memory or a device query under 'history', which hides the device claims,
# so the scheme reads the claim itself. This only places keys; the claim's family and direction are shown unchanged.
_PATTERN_REF = re.compile(r"typology_scan|shared_origin|ring_|episode|similar|prior_cases|closed", re.IGNORECASE)
_CARD_REF = re.compile(r"card_window|card_history|velocity|recurring", re.IGNORECASE)
_DEVICE_TEXT = re.compile(r"device|proxy|mobile|telemetry|user[ -]?agent|browser|handset|\bOS\b", re.IGNORECASE)


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _clip(text: object, limit: int) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _first_sentence(text: object, limit: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return _clip(_SENTENCE.split(s, 1)[0], limit)


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action.replace("_", " ").capitalize())


def detector_waves(case_id: str) -> list[tuple[str, list[str]]]:
    """[(detector id, other exam cases it flags with this one)] from the detector backtest's exam scan."""
    bt = common.proof("backtest")
    scan = (bt["data"] or {}).get("exam_scan") if isinstance(bt["data"], dict) else None
    if not isinstance(scan, dict):
        return []
    return [(str(d), [str(i) for i in ids or [] if i != case_id]) for d, ids in scan.items() if case_id in (ids or [])]


def dq_taints(case_id: str) -> list[dict]:
    dq = common.proof("dq")
    data = dq["data"] if isinstance(dq["data"], dict) else {}
    return [t for t in (data.get("taint_matrix") or {}).get(case_id, []) if isinstance(t, dict)]


# --------------------------------------------------------------------------- #
# The toolbar line and the checks under it                                    #
# --------------------------------------------------------------------------- #
def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def pending_phrases(states: list[dict]) -> list[str]:
    """'block card awaiting L1' for each gated action that no human has decided yet (acronyms such as SAR keep case)."""
    return [f"{_lower_first(action_label(s['action']))} awaiting {s['route']}" for s in states if s["route"] in ("L1", "L2") and s["decision"] not in ("approved", "rejected")]


def step_of(claim: dict) -> int:
    """The scheme step (1-4) a claim's key sits under."""
    ref = str(claim.get("ref") or "")
    if ref == "trigger" or claim.get("source") == "external":
        return 1
    if claim.get("family") in ("memory", "customer", "document") or _PATTERN_REF.search(ref):
        return 4
    if _CARD_REF.search(ref):
        return 2
    if claim.get("family") == "device" or _DEVICE_TEXT.search(str(claim.get("claim") or "")):
        return 3
    return 2


def verdict_html(view: Any, states: list[dict], lc: dict) -> str:
    """The verdict as a dot and an honest headline: what is decided, and what still waits on a person."""
    case = (view.answer or {}).get("case") or {}
    verdict = str(case.get("verdict") or "")
    word = VERDICT_WORD.get(verdict, "No verdict")
    waiting = pending_phrases(states)
    if waiting:
        then = " · ".join(waiting)
    elif verdict == "legitimate":
        then = "closed, no fraud"
    else:
        then = lc.get("stage") or ""
    return (f'<div class="tc tc-verdict {esc(verdict)}"><span class="tc-dot {esc(verdict or "none")}"></span>{esc(word)}'
            f'<span class="tc-then">{esc(then)}</span></div>')


def checks_html(view: Any, agreement_line: str) -> str:
    ans = view.answer or {}
    saf = common.safety({view.case_id: ans})
    verdict = str((ans.get("case") or {}).get("verdict") or "")
    items: list[tuple[bool | None, str]] = []
    hard = saf["hard_on_non_fraud"]
    items.append((not hard, "No hard action on a non-fraud verdict" if not hard else "Hard action on a non-fraud verdict: " + ", ".join(hard[0][1])))
    if verdict == "uncertain":
        bad = saf["uncertain_not_escalated"]
        items.append((not bad, "Escalated to an analyst" if not bad else "Uncertain but not escalated"))
    if agreement_line:
        items.append((None, agreement_line))
    return design.checks(items, lead="Checks", tail='<a class="tc-jump" href="#case-tabs">Decision path, evidence ledger and SAR ↓</a>')


# --------------------------------------------------------------------------- #
# The scheme                                                                  #
# --------------------------------------------------------------------------- #
def _keys(claims: list[dict]) -> str:
    out = []
    for c in claims:
        k = f"E{c['n']}"
        cls = {"fraud": "", "legitimate": "legit"}.get(c["direction"], "neutral")
        out.append(f'<a class="tc-key {cls}" href="#ind-{k}" data-k="{k}" title="{esc(c["claim"][:160])}">{k}</a>')
    return f'<div class="tc-keys">{"".join(out)}</div>' if out else ""


def _step(n: int, title: str, glyph_name: str, lines: list[tuple[str, str]], keys: str, cls: str = "") -> str:
    body = "".join(f'<div class="tc-step-line {esc(kind)}">{text}</div>' for kind, text in lines)
    return (f'<div class="tc-step {esc(cls)}"><div class="tc-step-top"><span class="tc-step-n">{n}</span>'
            f'<span class="tc-step-title">{esc(title)}</span>{glyph(glyph_name)}</div>{body}{keys}</div>')


def scheme_steps(view: Any, claims: list[dict], states: list[dict], investigated_card: str | None = None) -> list[str]:
    ans = view.answer or {}
    case = ans.get("case") or {}
    meta = view.meta or {}
    verdict = str(case.get("verdict") or "")
    by_step: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    for c in claims:
        by_step[step_of(c)].append(c)
    trigger_claims = by_step[1]

    # 1 Alert: the case pack's trigger
    ttype = str(meta.get("trigger_type") or "")
    lines = []
    if meta.get("flagged_txn_id"):
        lines.append(("", f'on transaction <span class="tc-mono">{esc(meta["flagged_txn_id"])}</span>'))
    if ttype == "risk_score" and meta.get("risk_score") not in (None, ""):
        lines.append(("", f"risk score {esc(meta['risk_score'])}"))
    if meta.get("opened_at"):
        lines.append(("", f'opened {esc(str(meta["opened_at"])[:16])}'))
    steps = [_step(1, TRIGGER_LABELS.get(ttype, "Alert"), "alert", lines, _keys(trigger_claims))]

    # 2 Card: the card investigated, the transactions and the exposure
    card = investigated_card or meta.get("card_id") or ""
    txns = [str(t) for t in case.get("affected_txn_ids") or []]
    exposure = _num(case.get("exposure_usd"))
    lines = [("big tc-mono", esc(card))] if card else []
    if txns:
        lines.append(("", f"{len(txns)} transaction{'s' if len(txns) != 1 else ''} · <b>{esc(common.money(exposure))}</b>"))
        lines.append(("tc-muted", f'<span class="tc-mono">{esc(", ".join(txns[:3]))}{" …" if len(txns) > 3 else ""}</span>'))
    else:
        lines.append(("tc-muted", "no affected transactions · no loss exposure"))
    steps.append(_step(2, "Card", "card", lines, _keys(by_step[2])))

    # 3 Device and origin: what the device evidence says, and any data-quality taint on it
    devices = [str(d) for d in case.get("connected_device_profiles") or []]
    dev_claims = by_step[3]
    if devices:
        lines = [("", "shared device: " + f'<span class="tc-mono">{esc(", ".join(devices[:2]))}</span>')]
    elif dev_claims:
        lines = [("", esc(_first_sentence(dev_claims[0]["claim"], 84)))]
    else:
        lines = [("tc-muted", "no device evidence in this answer")]
    taints = dq_taints(view.case_id)
    if taints:
        lines.append(("flag", esc(" · ".join(str(t.get("code")) for t in taints[:3])) + " data-quality taint"))
    steps.append(_step(3, "Device and origin", "device", lines, _keys(dev_claims), "" if dev_claims or devices else "quiet"))

    # 4 Pattern: a detector wave when the exam scan has one, else the answer's own pattern
    waves = detector_waves(view.case_id)
    connected = [str(c) for c in case.get("connected_card_ids") or []]
    memory = by_step[4]
    if waves:
        det, others = waves[0]
        short = det.split("_", 1)[0]
        title = f"{short} wave"
        lines = [("", esc(det.split("_", 1)[1].replace("_", " ").lower() if "_" in det else det))]
        if connected:
            lines.append(("big", f"{len(connected) + 1} cards"))
        if others:
            lines.append(("tc-muted", "also flags " + esc(", ".join(others))))
        cls = ""
    else:
        pattern = str(case.get("pattern") or "")
        title = PATTERN_LABELS.get(pattern, pattern.replace("_", " ").capitalize())
        lines = []
        if connected:
            lines.append(("big", f"{len(connected) + 1} cards"))
        similar = case.get("similar_prior_cases") or []
        if similar:
            lines.append(("tc-muted", f"{len(similar)} similar closed case{'s' if len(similar) != 1 else ''}"))
        if not lines:
            lines.append(("tc-muted", "no cross-card coordination found"))
        cls = "" if connected else "quiet"
    steps.append(_step(4, title, "wave", lines, _keys(memory), cls))

    # 5 Decision: the final actions, condensed, with the rule the stop reason cites
    lines = []
    for s in states[:4]:
        if s["action"] == "CREATE_CASE" and len(states) > 3:
            continue
        label = esc(action_label(s["action"]))
        route = "" if s["route"] == "auto" else f' · <b>{esc(s["route"])}</b>'
        lines.append(("flag" if s["hard"] else "", f"{label}{route}"))
    extra = len(states) - len(lines)
    if extra > 0:
        lines.append(("tc-muted", f"+{extra} more"))
    rules = list(dict.fromkeys(m.group(1) for m in _RULE_RE.finditer(str(ans.get("stop_reason") or ""))))[:2]
    keys = f'<div class="tc-keys">{"".join(f"<span class=tc-rule-key>{esc(r)}</span>" for r in rules)}</div>' if rules else ""
    steps.append(_step(5, STEP_TITLE.get(verdict, "Decision"), "decision", lines, keys, f"decision {verdict}"))
    return steps


def scheme_html(view: Any, claims: list[dict], states: list[dict], investigated_card: str | None = None) -> str:
    case = (view.answer or {}).get("case") or {}
    pattern = str(case.get("pattern") or "")
    waves = detector_waves(view.case_id)
    note = PATTERN_LABELS.get(pattern, pattern.replace("_", " ")).lower() if pattern else ""
    aside = esc(f"detector {waves[0][0].split('_', 1)[0]}") if waves else ""
    steps = scheme_steps(view, claims, states, investigated_card)
    return (f'<div class="tc">{design.section(1, "Scheme", note, aside)}'
            f'<div class="tc-scheme" style="--steps:{len(steps)}">{"".join(steps)}</div></div>')


# --------------------------------------------------------------------------- #
# Indicators                                                                  #
# --------------------------------------------------------------------------- #
def indicators_html(claims: list[dict], fam_fraud: list[str], fam_legit: list[str]) -> str:
    attested = sum(1 for c in claims if c.get("call"))
    rows = []
    for c in claims:
        k = f"E{c['n']}"
        cls = {"fraud": "", "legitimate": "legit"}.get(c["direction"], "neutral")
        fam = common.FAMILY_LABELS.get(c["family"], c["family"]) if c["source"] != "external" else "the alert"
        direction = {"fraud": "points to fraud", "legitimate": "points to legitimate"}.get(c["direction"], "neutral")
        ref = c["ref"]
        if c.get("call"):
            when = c.get("ts") or ""
            checked = f'<span class="tc-check ok">{glyph("check")}{esc(when[11:19] or when)}</span>'
        elif str(ref).startswith("query:"):
            checked = f'<span class="tc-check bad" title="no matching call in calls.jsonl">{glyph("cross")}unmatched</span>'
        else:
            checked = '<span class="tc-muted">—</span>'
        flags = f'<span class="flags">{esc("; ".join(c["flags"]))}</span>' if c.get("flags") else ""
        rows.append(
            f'<tr id="ind-{k}" data-k="{k}"><td class="k"><span class="tc-key {cls}">{k}</span></td>'
            f'<td class="f">{esc(fam)}<br><span class="tc-muted">{esc(direction)}</span></td>'
            f'<td><span class="claim">{esc(c["claim"])}</span><span class="ref">{esc(ref)}</span>{flags}</td>'
            f'<td class="n tc-num">{len(c["entity_ids"])}</td><td class="t">{checked}</td></tr>')
    fam_note = []
    if fam_fraud:
        fam_note.append("to fraud: " + ", ".join(common.FAMILY_LABELS.get(f, f) for f in fam_fraud))
    if fam_legit:
        fam_note.append("to legitimate: " + ", ".join(common.FAMILY_LABELS.get(f, f) for f in fam_legit))
    aside = esc(f"{len(claims)} claims · {attested} matched to a graph call")
    head = design.section(2, "Indicators", "the scorecard's families point " + "; ".join(fam_note) if fam_note else "", aside)
    if not rows:
        return f'<div class="tc">{head}<p class="tc-muted">No evidence items in this answer.</p></div>'
    return (f'<div class="tc">{head}<table class="tc-ind"><thead><tr><th>Key</th><th>Family</th><th>Indicator and the query behind it</th>'
            f'<th style="text-align:right">Ids</th><th style="text-align:right">Checked</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


# --------------------------------------------------------------------------- #
# The decision panel                                                          #
# --------------------------------------------------------------------------- #
def _state_cell(s: dict) -> str:
    d = s["decision"]
    if d == "executed":
        return f'<span class="st done">{glyph("check")}Executed</span>'
    if d == "pending":
        sub = f'<span class="sub">inbox #{esc(s["inbox_id"])}</span>' if s.get("inbox_id") else ""
        return f'<span class="st wait">{glyph("clock")}Awaiting approval</span>{sub}'
    if d == "approved":
        return f'<span class="st done">{glyph("check")}Approved</span><span class="sub">{esc(s["state"].split(" · ")[-1])}</span>'
    if d == "rejected":
        return f'<span class="st off">{glyph("cross")}Rejected</span><span class="sub">{esc(s["state"].split(" · ")[-1])}</span>'
    if d == "not queued":
        return f'<span class="st off">{glyph("dash")}Not queued</span><span class="sub">{esc(s["state"].split(" · ")[0])}</span>'
    return f'<span class="st off">{glyph("dash")}Recommended</span><span class="sub">not executed in this source</span>'


def decision_html(view: Any, states: list[dict], lc: dict, events: list[dict], answer_src: str) -> str:
    ans = view.answer or {}
    case = ans.get("case") or {}
    verdict = str(case.get("verdict") or "")
    p_engine = _num(common.phase_payload(view, "P3").get("p_engine"))
    p_model = _num(common.phase_payload(view, "P4").get("fraud_probability"))
    p_final = _num(case.get("fraud_probability"))
    clamped = any(e["type"] == "clamp" for e in events)
    path = []
    if p_engine is not None:
        path.append(("P3 engine", f"{p_engine:.2f}"))
    if p_model is not None:
        path.append(("P4 model", f"{p_model:.2f} · " + ("clamped to ±0.10" if clamped else "may move it ±0.10")))
    path.append(("Final", common.prob(p_final)))
    path_html = "".join(f"<dt>{esc(a)}</dt><dd>{esc(b)}</dd>" for a, b in path)
    why = str(ans.get("stop_reason") or "")
    why_html = f'<p class="tc-why"><b>Why.</b> {esc(why)}</p>' if why else ""

    rows = []
    for s in states:
        hard = '<span class="hard" title="hard action: irreversible for an innocent customer">■ </span>' if s["hard"] else ""
        rows.append(f'<tr><td class="a">{hard}{esc(s["action"])}</td><td>{design.route_tag(s["route"])}</td><td>{_state_cell(s)}</td></tr>')
    sar = ans.get("sar") or {}
    if sar.get("file"):
        report = next((s for s in states if s["action"] == "FILE_REPORT"), None)
        status = ("filed only once L2 approves FILE_REPORT" if report and report["decision"] not in ("approved", "rejected")
                  else f"FILE_REPORT {report['decision']}" if report else "no FILE_REPORT action in the final set")
        rule = (sar.get("reason") or "").split(":", 1)[0]
        rows.append(f'<tr><td class="a">SAR</td><td>{tag("drafted", "red")}</td><td><span class="sub" title="{esc(sar.get("reason"))}">'
                    f'{esc(_clip(rule, 40))} · {esc(status)}</span></td></tr>')
    waiting = sum(1 for s in states if s["decision"] == "pending")
    done = sum(1 for s in states if s["decision"] == "executed")
    counts = ", ".join(x for x in (f"{waiting} awaiting approval" if waiting else "", f"{done} executed" if done else "") if x)
    # what changed between the policy's initial set (P5) and the final set (P7), and any evidence the agent asked for
    nba = ans.get("next_best_actions") or {}
    diff = common.actions_diff_routes(nba.get("initial") or [], nba.get("final") or [])
    moves = ([f"+{a} ({r})" for a, r in diff["added"]] + [f"−{a} ({r})" for a, r in diff["removed"]]
             + [f"{a} {r1} → {r2}" for a, r1, r2 in diff["route_changed"]])
    notes = [f'<p class="tc-note"><b>Initial → final</b> {esc(", ".join(moves) if moves else "unchanged")}</p>']
    for req in ans.get("evidence_requests") or []:
        if isinstance(req, dict):
            notes.append(f'<p class="tc-note">{tag("simulated reply")} <b>{esc(str(req.get("type") or "request").replace("_", " "))}</b>: '
                         f'{esc(_clip(req.get("assumed_response"), 160))}</p>')
    owner = lc.get("next_owner") or "none"
    resolved = lc.get("stage_code") == "resolved_autonomously"
    items = "".join(f"<li>{esc(i)}</li>" for i in lc.get("open_items") or [])
    owner_html = (f'<div class="tc-owner {"closed" if resolved else ""}">{glyph("check" if resolved else "gate")}'
                  f'<div><span>{esc(lc.get("stage") or "")}' + ("" if owner in ("none", "—") else f' · next owner <b>{esc(owner)}</b>') + "</span>"
                  + (f'<ul class="tc-open">{items}</ul>' if items else "") + "</div></div>")
    pv = common.prob(p_final)
    return (
        f'<div class="tc">{design.section(3, "Decision", "", esc(answer_src))}'
        f'<div class="tc-p"><span class="tc-pv">{esc(pv)}</span><span class="tc-pl"><b>p(fraud), final</b><br>{esc(VERDICT_WORD.get(verdict, "no verdict"))}'
        f'{" · " + esc(PATTERN_LABELS.get(str(case.get("pattern") or ""), str(case.get("pattern") or "")).lower()) if case.get("pattern") else ""}</span></div>'
        f'{design.ramp(p_engine, p_model, p_final, verdict)}<dl class="tc-path">{path_html}</dl>{why_html}'
        f'{design.section(4, "Actions", "", esc(counts))}'
        f'<table class="tc-act"><thead><tr><th>Action</th><th>Route</th><th>Where it stands</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f'{"".join(notes)}{owner_html}</div>')

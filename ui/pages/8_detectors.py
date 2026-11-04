"""Detectors & containment: do the January detectors catch the waves, and who else would they hit?

The first screen answers it: three measured figures and one wave ledger that merges C1 (the detector backtest over every
closed case) with C7 (the containment plans). Each detector is drawn as its typology, a numbered chain of the clauses that
define it in engine/detectors.py. Selecting a ledger row selects the wave whose plan sits below, beside the ring replay
(C5). C2 (self-growing proposals) is partial and C3 (policy text to detector) is not built; both say so.

Everything here is read from files: engine/out/detector_backtest.json (never recomputed on view; only the operator's
'Re-run backtest' button calls run_backtest, which rewrites that file), engine/out/containment_summary.json and the
proof snapshot. A containment plan is computed by engine.containment on the read-only DuckDB when that module is
importable, else read from the snapshot, else reduced to the summary totals. Every containment number is a proposal
awaiting approval; nothing on this page blocks a card. Writes happen only on that re-run and on a fraud manager's
confirmed Approve / Reject of a proposal.
"""
from __future__ import annotations

import inspect
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common, design  # noqa: E402

md, esc, glyph, tag = common.md, design.esc, design.glyph, design.tag
ctx = common.page_setup("detectors")
bt = common.proof("backtest")
cs = common.proof("containment_summary")
plans_snap = common.proof("containment_plans")
cont, cont_err = common.optional_import("engine.containment")
det_mod, det_err = common.optional_import("engine.detectors")

BT = bt["data"] if isinstance(bt["data"], dict) else {}
DETECTORS = [d for d in BT.get("detectors") or [] if isinstance(d, dict)]
NEW_DETECTORS = [d for d in DETECTORS if not str(d.get("detector_id", "")).startswith("R0_")]  # the January detectors, D1-D4
EXAM_SCAN = {k: [str(c) for c in v or []] for k, v in (BT.get("exam_scan") or {}).items()} if isinstance(BT.get("exam_scan"), dict) else {}
DISC_FILE = common.ROOT / "engine" / "out" / "discovery_jan2017.json"
DISC_DATA, _disc_err = common._read_json(DISC_FILE) if DISC_FILE.is_file() else (None, "")
MINED_PROP = (DISC_DATA.get("proposal") if isinstance(DISC_DATA, dict) and isinstance(DISC_DATA.get("proposal"), dict) else None)
MINED_ROW = (
    [{
        "proposal_id": MINED_PROP.get("proposal_id"),
        "name": MINED_PROP.get("name"),
        "discovered_by": MINED_PROP.get("discovered_by"),
        "synthesized_sql": MINED_PROP.get("synthesized_sql"),
        "cards_matched": (MINED_PROP.get("backtest") or {}).get("cards_matched", (MINED_PROP.get("cluster") or {}).get("jan_cards")),
        "txns_matched": (MINED_PROP.get("backtest") or {}).get("txns_matched", (MINED_PROP.get("cluster") or {}).get("jan_txns")),
        "total_exposure_usd": (MINED_PROP.get("backtest") or {}).get("january_exposure_usd", (MINED_PROP.get("cluster") or {}).get("jan_usd")),
        "closed_case_false_positives": len((MINED_PROP.get("backtest") or {}).get("legitimate_customers_acted_against") or []),
        "legitimate_customers_acted_against": (MINED_PROP.get("backtest") or {}).get("legitimate_customers_acted_against") or [],
        "approval_status": MINED_PROP.get("approval_status", "pending"),
        "_mined": True,
        "_cards_list": (MINED_PROP.get("backtest") or {}).get("january_card_ids") or [],
    }]
    if MINED_PROP
    else []
)
PROPOSALS = MINED_ROW + [p for p in BT.get("self_growing_proposals") or [] if isinstance(p, dict)]
BT_SRC = {"source": bt["source"] or "engine/out/detector_backtest.json", "as_of": bt["as_of"] or "missing"}
CS = cs["data"] if isinstance(cs["data"], dict) else {}
CS_SRC = {"source": cs["source"] or "engine/out/containment_summary.json", "as_of": cs["as_of"] or "missing"}
SNAP_PLANS = plans_snap["data"] if isinstance(plans_snap["data"], dict) else {}
APPROVALS_FILE = common.ROOT / "data" / "detector_approvals.json"
DB_PATH = common.ROOT / "data" / "hhgoa_engine.duckdb"
LOSS_SPLIT = "'Lost before the alert' settled at or before the trigger case opened; 'Preventable after the alert' is what the plan would stop if approved"

# Each January typology as the numbered steps that define it, each keyed to the clauses of engine/detectors.py it comes
# from (WAVE_SQL for D1-D3, d4_wave for D4). Words and thresholds only: every count on this page comes from a file.
# The clauses are checked against the module when it is importable (_clauses_ok); the image ships no engine code.
TYPOLOGY: dict[str, dict] = {
    "D1": {"name": "New-mobile proxy wave", "glyph": "device", "code": "WAVE_SQL['D1_MOBILE_ANON_PROXY_WAVE']", "steps": [
        ("New mobile device behind an anonymous proxy", ("device_type = 'mobile' AND device_new = 'New' AND proxy = 'IP_PROXY:ANONYMOUS'",)),
        ("Product C purchases ≥ $200", ("product_cd = 'C'", "amt >= 200.0")),
        ("2+ per card, not the SM-G935F ring device", ("HAVING count(*) >= 2", "device_id NOT LIKE 'SM-G935F%'")),
    ]},
    "D2": {"name": "Dormant-wake drain", "glyph": "clock", "code": "WAVE_SQL['D2_ESCALATING_C_DRAIN']", "steps": [
        ("A card quiet 21+ days wakes", ("INTERVAL 21 DAY",)),
        ("5+ purchases, no identity record", ("count(*) >= 5", "NOT has_identity")),
        ("From under $300 to $1,100+", ("min(amt) < 300.0", "max(amt) >= 1100.0")),
    ]},
    "D3": {"name": "Micro-charge skimming", "glyph": "transaction", "code": "WAVE_SQL['D3_MICRO_CHARGE_SKIMMING']", "steps": [
        ("No sub-$10 charge before the window", ("amt < 10.0 AND ts < '{since}'",)),
        ("4–6 charges of $1–6", ("amt BETWEEN 1.00 AND 6.00", "count(*) BETWEEN 4 AND 6")),
        ("Spread over 4+ days", ("count(DISTINCT ts::DATE) >= 4",)),
    ]},
    "D4": {"name": "Recipient-email mule", "glyph": "email", "code": "d4_wave", "steps": [
        ("10–35 cards pay one recipient email domain in 30 days", ("10 <= n_dom_cards <= 35", "INTERVAL 30 DAY")),
        ("The domain is rare: ≤ 40 cards ever", ("int(n_upto) > 40",)),
    ]},
}


def _short_id(det_id: str) -> str:
    return str(det_id).split("_")[0]


def _engine_label() -> str:
    if cont is not None:
        return "live engine (read-only DuckDB)"
    if plans_snap["data"] is not None:
        return f"snapshot {plans_snap['generated_at'] or plans_snap['as_of']}"
    if cs["data"] is not None:
        return "containment summary totals"
    return "containment not available in this deployment"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _read_approvals() -> tuple[dict, str]:
    """data/detector_approvals.json (written only by engine.detectors.approve_detector): {proposal_id: record}."""
    if not APPROVALS_FILE.is_file():
        return {}, f"{common.rel(APPROVALS_FILE)} absent: no proposal has been decided"
    try:
        data = json.loads(APPROVALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"{common.rel(APPROVALS_FILE)} unreadable: {type(exc).__name__}: {exc}"
    return (data if isinstance(data, dict) else {}), ""


def _hits() -> dict[str, str]:
    """case id -> detector id, from exam_scan and each detector's exam_cases_caught."""
    out: dict[str, str] = {}
    for det_id, ids in EXAM_SCAN.items():
        for cid in ids:
            out.setdefault(cid, det_id)
    for d in DETECTORS:
        for cid in d.get("exam_cases_caught") or []:
            out.setdefault(str(cid), str(d.get("detector_id")))
    return out


def _answers(source_id: str | None) -> dict[str, dict]:
    if not source_id:
        return {}
    return {cid: v.answer for cid, v in common.source_views(source_id).items() if v.ok and isinstance(v.answer, dict)}


def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "x"


def _hard(answer: dict) -> list[str]:
    return [f"{x.get('action')} {x.get('route', 'auto')}" for x in _final(answer) if x.get("action") in common.HARD_ACTIONS]


HITS = _hits()
SRC_ANSWERS = _answers(ctx.source_id)
SUB_ANSWERS = _answers("submitted") if ctx.source_id != "submitted" else SRC_ANSWERS
APPROVALS, APPROVALS_NOTE = _read_approvals()
WAVES: dict[str, str] = {det: ids[0] for det, ids in sorted(EXAM_SCAN.items()) if ids and not det.startswith("R0_")}  # wave -> trigger case
case_wave = next((det for det, ids in EXAM_SCAN.items() if ctx.case_id in ids and det in WAVES), None)
DASH = '<span class="tc-muted">—</span>'


def _status(p: dict) -> str:
    rec = APPROVALS.get(str(p.get("proposal_id")))
    return str(rec.get("status")) if isinstance(rec, dict) and rec.get("status") else str(p.get("approval_status") or "pending")


def _c3() -> tuple[str | None, str, str, str]:
    """(value, detail, source, as_of) for C3 policy text -> detector."""
    if det_mod is not None:
        built = hasattr(det_mod, "compile_policy_rule_detector")
        detail = ("compile_policy_rule_detector is defined in engine/detectors.py" if built else
                  "Not built in this checkout: engine/detectors.py describes compile_policy_rule_detector in its docstring but defines no such function")
        return ("built" if built else "not built"), detail, "engine/detectors.py", common.as_of(common.ROOT / "engine" / "detectors.py")
    c3 = common.proof("c3_built")
    if c3["data"] is None:
        return None, f"not measured here: {det_err}; {c3['error']}", c3["source"] or "engine/out/proof_snapshot.json", c3["as_of"] or "missing"
    built = bool(c3["data"])
    detail = (f"snapshot {c3['generated_at']}: compile_policy_rule_detector " + ("present" if built else
              "absent: engine/detectors.py describes it in its docstring but defines no such function"))
    return ("built" if built else "not built"), detail, c3["source"], c3["as_of"]


# --------------------------------------------------------------------------- #
# The typologies, the ledger rows and the figures behind the first screen     #
# --------------------------------------------------------------------------- #
def _n(x: object) -> str:
    """A count as HTML text: a missing value is a grey dash, a real zero prints as 0."""
    if x is None or x == "":
        return DASH
    return esc(f"{x:,}" if isinstance(x, int) else x)


def _usd(x: object) -> str:
    return DASH if x is None else esc(common.money(x))


def _code_text(short: str) -> str | None:
    """The source a typology's clauses are checked against, whitespace collapsed, or None without engine/detectors.py."""
    if det_mod is None:
        return None
    try:
        if short == "D4":
            text = inspect.getsource(det_mod.d4_wave)
        else:
            text = next(v for k, v in (getattr(det_mod, "WAVE_SQL", None) or {}).items() if _short_id(k) == short)
    except (OSError, TypeError, AttributeError, StopIteration):
        return None
    return " ".join(str(text).split())


def _clauses_ok(short: str) -> bool | None:
    """True when every clause a typology's steps cite is in engine/detectors.py, False when one is missing, None when the
    module is not in this deployment (the steps are then shown as written, unchecked)."""
    text, typ = _code_text(short), TYPOLOGY.get(short)
    if text is None or typ is None:
        return None
    return all(c in text for _, clauses in typ["steps"] for c in clauses)


def _scan_since(det_id: str) -> str | None:
    """The window the detector's live scan starts at (engine.detectors.SCAN_SINCE), or None when the module is not here."""
    return (getattr(det_mod, "SCAN_SINCE", None) or {}).get(det_id) if det_mod is not None else None


def _backtest_since(d: dict) -> str | None:
    att = d.get("attestation") if isinstance(d.get("attestation"), dict) else {}
    params = ((att.get("how") or {}).get("params") or {}) if isinstance(att.get("how"), dict) else {}
    return str(params["since"]) if params.get("since") else None


def _chain_html(short: str) -> str:
    """A typology drawn compact: its numbered steps joined by arrows."""
    typ = TYPOLOGY.get(short)
    if not typ:
        return ""
    parts = []
    for i, (words, clauses) in enumerate(typ["steps"], 1):
        if i > 1:
            parts.append('<i class="dt-arr" aria-hidden="true"></i>')
        parts.append(f'<span class="dt-step" title="{esc(" · ".join(clauses))}"><span class="n">{i}</span>{esc(words)}</span>')
    return f'<div class="dt-chain">{"".join(parts)}</div>'


def _wave_cases() -> tuple[list[str] | None, str]:
    """Final Round cases whose flagged transaction is one of the waves' transactions (the txn_ids of the snapshot's D1-D4
    containment plans), or (None, why) when no plan is on hand."""
    plans = [p for p in SNAP_PLANS.values() if isinstance(p, dict) and str(p.get("wave_id")) in WAVES]
    if not plans:
        return None, plans_snap["error"] or "no D1-D4 containment plan in the snapshot"
    txns = {str(t) for p in plans for r in p.get("per_card_plan") or [] if isinstance(r, dict) for t in r.get("txn_ids") or []}
    pack, _ = common.final_round_pack()
    return sorted(r["case_id"] for r in pack if str(r.get("flagged_txn_id") or "") in txns), ""


def _ledger_rows() -> list[dict]:
    """One row per January wave: the backtest's cards and closed cases (C1) beside the containment summary of the wave's
    first exam case (C7). A value a file lacks stays None and prints as a dash."""
    rows = []
    for d in NEW_DETECTORS:
        det = str(d.get("detector_id") or "")
        if det not in WAVES:
            continue
        trigger = WAVES[det]
        summ = CS.get(trigger) if isinstance(CS.get(trigger), dict) and CS[trigger].get("wave", det) == det else {}
        rows.append({
            "det": det, "short": _short_id(det), "trigger": trigger, "name": str(d.get("name") or ""),
            "exam": EXAM_SCAN.get(det) or [str(c) for c in d.get("exam_cases_caught") or []],
            "cards": d.get("cards_matched"), "jan": d.get("cards_matched_january"), "at_alert": summ.get("cards"),
            "fraud": len(d.get("closed_cases_caught") or []), "cleared": [str(c) for c in d.get("cleared_cases_matched") or []],
            "cleared_custs": [str(c) for c in d.get("legitimate_customers_acted_against") or []],
            "lost": summ.get("pre_as_of"), "preventable": summ.get("post_as_of"),
            "broad": summ.get("broad_rule_extra_customers"), "touched": summ.get("legit_touched_targeted"),
        })
    return sorted(rows, key=lambda r: r["short"])


LEDGER_HEAD = [
    ("Detector and typology", "the detector's id, its typology in plain words and the steps that define it in engine/detectors.py"),
    ("Exam cases", "Final Round cases exam_scan catches; the first is the wave's trigger case"),
    ("Cards: backtest · January · at the alert", "backtest: the detector's query over July 2016 to January 2017; January: of those, cards with a "
     "January transaction; at the alert: the wave the containment plan builds for the trigger case, over the detector's live scan window"),
    ("Closed cases: fraud · cleared", "confirmed-fraud and cleared (legitimate) closed cases the detector's matches overlap"),
    ("Lost before · preventable after the alert", LOSS_SPLIT),
    ("Broad rule also hits", "customers outside the wave a naive broad rule for the same signal would also act on"),
    ("Targeted plan touches", "wave customers with a wave transaction the bank cleared as legitimate: measured, not assumed"),
]


def _ledger_head_html() -> str:
    cells = "".join(f'<div class="{"num" if i >= 2 else ""}" title="{esc(tip)}">{esc(label)}</div>' for i, (label, tip) in enumerate(LEDGER_HEAD))
    return f'<div class="tc dt-head dt-grid">{cells}</div>'


def _ledger_row_html(r: dict, on: bool) -> str:
    typ = TYPOLOGY.get(r["short"]) or {}
    det = (f'<div class="dt-det"><span class="dt-radio" aria-hidden="true"></span>{glyph(typ.get("glyph", "wave"))}'
           f'<span class="dt-id">{esc(r["short"])}</span><span class="dt-name">{esc(typ.get("name") or r["name"])}</span></div>{_chain_html(r["short"])}')
    exam = "<br>".join(f'<span class="mono">{esc(c)}</span>' for c in r["exam"]) or DASH
    cards = f'{_n(r["cards"])} · {_n(r["jan"])} · <b>{_n(r["at_alert"])}</b>'
    closed = f'{_n(r["fraud"])} · ' + (f'<span class="dt-flag">{len(r["cleared"])}</span>' if r["cleared"] else "0")
    if r["cleared"]:
        closed += f'<span class="sub dt-flag">{esc(", ".join(r["cleared"]))} → {esc(", ".join(r["cleared_custs"]) or "customer not recorded")}</span>'
    money = (f'<span class="dt-lost">{_usd(r["lost"])}</span> lost<br><span class="dt-prev">{_usd(r["preventable"])}</span> preventable'
             f'<span class="sub">as of {esc(r["trigger"])}</span>')
    cells = [det, exam, cards, closed, money, _n(r["broad"]), _n(r["touched"])]
    body = "".join(f'<div class="{"num" if i >= 2 else ""}">{c}</div>' for i, c in enumerate(cells))
    return f'<div class="tc dt-tr dt-grid{" on" if on else ""}">{body}</div>'


def _pick_wave(det: str) -> None:
    st.session_state["det_wave"] = det


def _ledger(rows: list[dict], wave: str | None) -> None:
    """The ledger as one grid: a head, then a row per wave. Each row carries its own button, laid over the row by
    detectors.css, so selecting a row selects its wave (det_wave) for the plan and replay below."""
    with st.container(key="dt-ledger", gap=None):
        design.html_block(_ledger_head_html())
        for r in rows:
            with st.container(key=f"dt-row-{r['short'].lower()}", gap=None):
                design.html_block(_ledger_row_html(r, r["det"] == wave))
                st.button(f"Select the {r['short']} wave", key=f"det_row_{r['short']}", on_click=_pick_wave, args=(r["det"],), type="tertiary")


def _safety_line() -> str:
    """The safety cross-check as one line: no detector fires on a case the answers call legitimate or uncertain."""
    checked: dict[str, list[tuple[str, str, str]]] = {}
    for label, answers in ((ctx.source_id or "(no source)", SRC_ANSWERS), ("submitted", SUB_ANSWERS)):
        if label not in checked:
            checked[label] = [(cid, det, str((answers[cid].get("case") or {}).get("verdict") or "")) for cid, det in sorted(HITS.items()) if cid in answers]
    bad = [(src, cid, det, v) for src, rows in checked.items() for cid, det, v in rows if v in ("legitimate", "uncertain")]
    n_checked = sum(len(r) for r in checked.values())
    if not HITS:
        item: tuple[bool | None, str] = (None, "no detector hit recorded in the backtest, so there is nothing to cross")
    elif n_checked == 0:
        item = (None, f"none of the {len(HITS)} detector-hit cases has an answer in {' or '.join(checked)}: not measured here")
    elif not bad:
        item = (True, f"no detector fires on a case the answers call legitimate or uncertain ({n_checked} detector-hit answers in {' and '.join(checked)})")
    else:
        item = (False, "a detector fires on a case the answers call legitimate or uncertain: "
                + "; ".join(f"{cid} ({_short_id(det)}) is {v} in {src}" for src, cid, det, v in bad))
    return design.checks([item], lead="Safety cross-check")


# --------------------------------------------------------------------------- #
# First screen: the answer, three figures, the wave ledger                    #
# --------------------------------------------------------------------------- #
pack_rows, _ = common.final_round_pack()
wave_cases, wave_why = _wave_cases()
caught = [c for c in wave_cases if HITS.get(c) in WAVES] if wave_cases is not None else None
hit_dets = sorted({_short_id(d.get("detector_id")) for d in NEW_DETECTORS if d.get("legitimate_customers_acted_against") or d.get("cleared_cases_matched")})
cleared_cases = sorted({str(c) for d in NEW_DETECTORS for c in d.get("cleared_cases_matched") or []})
acted = [(_short_id(d.get("detector_id")), str(c), [str(x) for x in d.get("cleared_cases_matched") or []])
         for d in NEW_DETECTORS for c in d.get("legitimate_customers_acted_against") or []]
n_cleared = BT.get("n_cleared_legitimate") if isinstance(BT.get("n_cleared_legitimate"), int) else None
cs_rows = sorted(((str(v.get("wave")), v) for v in CS.values() if isinstance(v, dict) and str(v.get("wave")) in WAVES), key=lambda x: x[0])
touched_total = sum(int(v.get("legit_touched_targeted") or 0) for _, v in cs_rows) if cs_rows else None
broad_parts = [int(v.get("broad_rule_extra_customers") or 0) for _, v in cs_rows]
broad_distinct = {str(c) for p in SNAP_PLANS.values() if isinstance(p, dict) and str(p.get("wave_id")) in WAVES and p.get("case_id") == WAVES.get(str(p.get("wave_id")))
                  for c in p.get("legitimate_customers_touched_if_broad_rule_used") or []}
ledger_rows = _ledger_rows()

n_cc = BT.get("n_closed_cases")
split = f" ({BT['n_confirmed_fraud']:,} confirmed fraud, {n_cleared:,} cleared)" if isinstance(BT.get("n_confirmed_fraud"), int) and n_cleared else ""
scope = " · ".join(x for x in (f"Final Round waves {ledger_rows[0]['short']}–{ledger_rows[-1]['short']}" if ledger_rows else "",
                               f"backtest over {n_cc:,} closed cases{split}" if isinstance(n_cc, int) else "") if x)
common.page_header(
    "Detectors & containment",
    f"{BT_SRC['source']} written {BT_SRC['as_of']} · {CS_SRC['source']} written {CS_SRC['as_of']} · plans: {_engine_label()}",
    eyebrow=scope,
)
if bt["data"] is None:
    st.warning(md(f"Detector backtest not available here: {bt['error']}"), icon=":material/error:")
if cs["data"] is None:
    st.caption(md(f"Containment summary not available here: {cs['error']}"))

# the answer, in words; the numbers are in the figures under it
if bt["data"] is not None:
    if caught is None:
        first = f"The January detectors catch <b>{len({c for c, d in HITS.items() if d in WAVES and c.startswith('HHF-')})}</b> Final Round cases"
    elif len(caught) == len(wave_cases):
        first = "The four January detectors catch <b>every wave case</b>" if len(ledger_rows) == 4 else "The January detectors catch <b>every wave case</b>"
    else:
        first = f'The January detectors catch <b>{len(caught)} of {len(wave_cases)}</b> wave cases (missed <b>{esc(", ".join(c for c in wave_cases if c not in caught))}</b>)'
    who = (f'only <b>{esc(" and ".join(hit_dets))}</b> would act on a customer the bank had cleared' if hit_dets
           else '<b>no detector</b> would act on a customer the bank had cleared')
    if touched_total is None:
        plans = "the targeted plans' customer impact is not measured here"
    elif touched_total == 0:
        plans = 'the targeted plans touch <b>no legitimate customer</b>' + (", where the broad rules would" if sum(broad_parts) else "")
    else:
        plans = f'the targeted plans touch <b>{touched_total}</b> customer{"s" if touched_total != 1 else ""} the bank had cleared'
    design.html_block(f'<p class="tc tc-lead">{first}. Across the bank\'s cleared history {who}, and {plans}.</p>')

with st.container(border=True, key="strip-dt"):
    fig = st.columns(3)
common.measured("Wave cases caught", f"{len(caught)}/{len(wave_cases)}" if caught is not None else None, container=fig[0], **BT_SRC,
                what="Final Round cases whose flagged transaction is one of the waves' transactions, caught by exam_scan",
                how=("exam_scan in the backtest (scan_card on each Final Round case, as of its alert); the wave cases are the pack rows whose "
                     f"flagged_txn_id is in a D1-D4 containment plan's txn_ids ({plans_snap['source'] or 'snapshot'})"
                     if caught is not None else f"exam_scan in the backtest; the wave cases are not measured here: {wave_why}"))
if pack_rows:
    fig[0].caption(md(f"of the {len(pack_rows)} Final Round cases scanned"))
acted_custs = sorted({cust for _, cust, _ in acted})
common.measured("Cleared customers hit", f"{len(acted_custs)} of {n_cleared:,}" if n_cleared and bt["data"] is not None else None, container=fig[1], **BT_SRC,
                what=f"customers of cleared (legitimate) closed cases a January detector would act on, against the {n_cleared or 'not measured'} cleared closed cases",
                how=f"legitimate_customers_acted_against and cleared_cases_matched per detector ({len(cleared_cases)} cleared case(s) matched), over n_cleared_legitimate")
if acted:
    fig[1].caption(" · ".join(f":red[{md(f'{det} → {cust}')}] " + md(f"({', '.join(cc)})" if cc else "") for det, cust, cc in acted))
elif bt["data"] is not None:
    fig[1].caption("no detector would act on a cleared customer")
common.measured("Legitimate customers touched", f"{touched_total} targeted vs {sum(broad_parts)} broad" if touched_total is not None else None, container=fig[2], **CS_SRC,
                what="targeted: wave customers whose wave transactions the bank cleared as legitimate (measured per plan, not assumed); broad: customers "
                     "outside the wave the naive broad rule for each wave would also act on, summed over the waves",
                how="legit_touched_targeted and broad_rule_extra_customers per wave, as of each wave's first exam case")
if broad_parts:
    fig[2].caption(md(f"broad rules {'/'.join(str(b) for b in broad_parts)} ({'/'.join(w[:2] for w, _ in cs_rows)})"
                      + (f", {len(broad_distinct)} distinct customers" if broad_distinct else "")))

# the wave ledger: the page's answer region; selecting a row selects the wave below
if WAVES and (st.session_state.get("_det_wave_case") != ctx.case_id or st.session_state.get("det_wave") not in WAVES):
    st.session_state["det_wave"] = case_wave or next(iter(WAVES), None)  # a case outside every wave opens on the first wave
    st.session_state["_det_wave_case"] = ctx.case_id
wave = st.session_state.get("det_wave") if st.session_state.get("det_wave") in WAVES else None
if ledger_rows:
    _ledger(ledger_rows, wave)
    notes = []
    d3 = next((r for r in ledger_rows if r["short"] == "D3"), None)
    windows = [f"{r['short']} from {_scan_since(r['det'])}" for r in ledger_rows if _scan_since(r["det"])]
    window_txt = f" ({', '.join(windows)}; D4 the 30 days before the alert)" if windows else " (engine.detectors.SCAN_SINCE; not in this deployment)"
    notes.append(f"<b>At the alert</b> is the wave the containment plan builds for the trigger case, over the detector's live scan window{esc(window_txt)}.")
    if d3 and d3["cards"] is not None and d3["at_alert"] is not None and d3["cards"] != d3["at_alert"]:
        since = _backtest_since(next((d for d in NEW_DETECTORS if d.get("detector_id") == d3["det"]), {}))
        notes.append(f"So D3 is <b>{_n(d3['cards'])}</b> in the backtest, which replays its query from {esc(since or 'the start of the history')} "
                     f"({_n(d3['jan'])} of those cards active in January), and <b>{_n(d3['at_alert'])}</b> at the alert.")
    clean = [r["short"] for r in ledger_rows if not r["cleared"]]
    if clean and n_cleared:
        notes.append(f"{esc(', '.join(clean))} hit 0 of the {n_cleared:,} cleared closed cases: they do not fire on legitimate history. "
                     "The backtest does not measure recall.")
    notes.append(f"Sources: {esc(BT_SRC['source'])} ({esc(BT_SRC['as_of'])}) for cards and closed cases; {esc(CS_SRC['source'])} "
                 f"({esc(CS_SRC['as_of'])}) for the rest, as of each wave's first exam case.")
    oks = {r["short"]: _clauses_ok(r["short"]) for r in ledger_rows}
    if all(v is None for v in oks.values()):
        notes.append(f"Typology steps as written from engine/detectors.py; not re-checked here ({esc(det_err or 'engine/ not in this deployment')}).")
    elif all(oks.values()):
        notes.append(f"Typology steps: every clause they cite is found in engine/detectors.py ({esc(', '.join(oks))}).")
    else:
        notes.append("Typology steps: " + "; ".join(f"{esc(k)} every clause found in engine/detectors.py" if v else
                                                    f'<span class="dt-flag">{esc(k)} a clause is no longer in engine/detectors.py</span>' if v is False
                                                    else f"{esc(k)} not re-checked" for k, v in oks.items()) + ".")
    design.html_block(f'<div class="tc dt-notes">{"".join(f"<p>{n}</p>" for n in notes)}</div>')
elif bt["data"] is not None:
    st.caption("No January wave has an exam case in the backtest's exam_scan, so there is no ledger.")


# --------------------------------------------------------------------------- #
# C7: containment planner                                                     #
# --------------------------------------------------------------------------- #


def _db_sig() -> tuple:
    try:
        s = DB_PATH.stat()
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return (0, 0)


@st.cache_data(ttl=600, show_spinner="Building the containment proposal on the read-only DuckDB…")
def _engine_plan(wave_id: str, trigger: str, opened: str, _sig: tuple) -> dict:
    """engine.containment on the read-only DuckDB, anchored to the wave's trigger case. Errors come back as text."""
    mod, why = common.optional_import("engine.containment")
    if mod is None:
        return {"error": why}
    try:
        found, params = mod.wave_for_case(trigger)
        if found != wave_id:
            return {"error": f"engine.containment.wave_for_case({trigger}) found {found or 'no wave'}, not {wave_id}"}
        at = params.get("as_of") or opened
        plan = mod.build_containment_plan(wave_id, as_of=at, r_email=params.get("r_email", ""))
        window = ""
        sql = str((getattr(mod, "WAVE_DEFINITIONS", {}).get(wave_id) or {}).get("sql", ""))
        m = re.search(r"INTERVAL\s+(\d+)\s+DAY", sql)
        since = (getattr(det_mod, "SCAN_SINCE", {}) or {}).get(wave_id) if det_mod is not None else None
        if since:
            window = f"from {since}"
        elif m and params.get("r_email"):
            window = f"{m.group(1)} days before the alert, recipient email {params.get('r_email')}"
        return {"plan": plan.to_dict(), "r_email": params.get("r_email", ""), "window": window}
    except Exception as exc:  # noqa: BLE001 - a locked or absent DuckDB falls back to the snapshot
        return {"error": f"engine.containment: {type(exc).__name__}: {exc}"}


def _plan_for(wave_id: str, trigger: str, opened: str) -> dict:
    """{plan | totals, source, as_of, how, window, notes} for one wave, from the engine, else the snapshot, else the summary."""
    notes = []
    if cont is not None:
        res = _engine_plan(wave_id, trigger, opened, _db_sig())
        if res.get("plan"):
            return {"plan": res["plan"], "source": f"{common.rel(DB_PATH)} (read-only) via engine.containment", "as_of": f"computed {_now()}",
                    "how": f"build_containment_plan({wave_id}, as_of={res['plan'].get('as_of')}, r_email={res.get('r_email') or '—'})",
                    "window": res.get("window") or "not recorded", "notes": notes}
        notes.append(res.get("error") or "engine plan empty")
    else:
        notes.append(cont_err or "engine.containment unavailable")
    snap = plans_snap["data"]
    plan = snap.get(trigger) if isinstance(snap, dict) else next((p for p in snap or [] if isinstance(p, dict) and p.get("case_id") == trigger), None)
    if isinstance(plan, dict) and plan.get("wave_id", wave_id) == wave_id:
        return {"plan": plan, "source": plans_snap["source"], "as_of": plans_snap["as_of"],
                "how": f"snapshot {plans_snap['generated_at']}: build_containment_plan({wave_id}) for {trigger}", "window": "recorded in the snapshot's plan", "notes": notes}
    notes.append(plans_snap["error"] or f"no snapshot plan for {trigger}")
    totals = cs["data"].get(trigger) if isinstance(cs["data"], dict) else None
    if isinstance(totals, dict) and totals.get("wave", wave_id) == wave_id:
        return {"totals": totals, "source": cs["source"], "as_of": cs["as_of"], "how": "containment summary totals", "window": "not recorded", "notes": notes}
    notes.append(cs["error"] or f"no containment summary for {trigger}")
    return {"notes": notes}


# --------------------------------------------------------------------------- #
# The selected wave: its typology, its plan beside the ring replay (C7 + C5)  #
# --------------------------------------------------------------------------- #
def _step(delta: int, n: int) -> None:
    st.session_state["det_frame"] = min(max(1, int(st.session_state.get("det_frame", 1)) + delta), n)


def _verdict_of(case_id: str) -> tuple[str, str]:
    """(verdict, where) of a Final Round case in the selected source, else the submitted answers."""
    for label, answers in ((ctx.source_id or "", SRC_ANSWERS), ("submitted", SUB_ANSWERS)):
        a = answers.get(case_id)
        if isinstance(a, dict):
            return str((a.get("case") or {}).get("verdict") or "no verdict"), label
    return "no answer here", ""


def _replay_chart(frames: list[dict], as_of: object, idx: int) -> alt.LayerChart:
    """Cumulative wave exposure per transaction: red before the alert (already lost), ochre after it (waiting on a human)."""
    t = design.TOKENS.get(design.theme_type(), design.TOKENS["light"])
    phases = ["before the alert", "after the alert (hindsight)"]
    df = pd.DataFrame([{"ts": fr.get("ts"), "frame": fr.get("frame"), "card": fr.get("card_id"), "amount": fr.get("amt_usd"),
                        "cumulative": fr.get("cumulative_exposure_usd"), "phase": phases[0] if fr.get("before_as_of") else phases[1]} for fr in frames])
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    at = pd.to_datetime(as_of, errors="coerce")
    base = alt.Chart(df).encode(x=alt.X("ts:T", title=None), y=alt.Y("cumulative:Q", title="Cumulative wave exposure ($)", axis=alt.Axis(format="$,.0f")))
    layers = [base.mark_line(interpolate="step-after", color=t["muted"], strokeWidth=1.5),
              base.mark_point(filled=True, size=38, opacity=1).encode(
                  color=alt.Color("phase:N", title=None, scale=alt.Scale(domain=phases, range=[t["red"], t["ochre"]]), legend=alt.Legend(orient="bottom")),
                  tooltip=["frame", "ts:T", "card", alt.Tooltip("amount:Q", format="$,.2f"), alt.Tooltip("cumulative:Q", format="$,.2f"), "phase"])]
    if pd.notna(at) and df["ts"].notna().any():
        end = max(df["ts"].max(), at)
        layers.insert(0, alt.Chart(pd.DataFrame([{"start": at, "end": end}])).mark_rect(opacity=0.08, color=t["ochre"]).encode(x="start:T", x2="end:T"))
        layers.append(alt.Chart(pd.DataFrame([{"at": at}])).mark_rule(strokeDash=[4, 4], color=t["ink"]).encode(
            x="at:T", tooltip=[alt.Tooltip("at:T", title="the alert")]))
    layers.append(alt.Chart(df.iloc[[idx - 1]]).mark_rule(strokeWidth=2, color=t["petrol"]).encode(x="ts:T"))
    return alt.layer(*layers).properties(height=250)


plan_res: dict = {}
plan = None
r1_wave = next((str(d.get("detector_id")) for d in DETECTORS if str(d.get("detector_id", "")).startswith("R0_") and ctx.case_id in (d.get("exam_cases_caught") or [])), None)
if wave:
    short = _short_id(wave)
    trigger = WAVES[wave]
    opened = common.case_meta(trigger).get("opened_at", "")
    plan_res = _plan_for(wave, trigger, opened)
    plan, totals = plan_res.get("plan"), plan_res.get("totals")
    as_of_txt = (plan or {}).get("as_of") or opened or "not recorded"
    typ = TYPOLOGY.get(short) or {}
    if plan and str(plan_res.get("source", "")).endswith("via engine.containment"):
        label = "live engine, read-only DuckDB"
    elif plan:
        label = f"snapshot {plans_snap['generated_at'] or plans_snap['as_of']}"
    else:
        label = "summary totals" if totals else "not available here"
    design.html_block(design.section("", f"{short} · {typ.get('name') or wave}, as of {trigger}'s alert", "proposal — awaiting approval",
                                     esc(f"{as_of_txt} · {label}")))
    lead = []
    if ctx.case_id in EXAM_SCAN.get(wave, []) and ctx.case_id != trigger:
        lead.append(f"{esc(ctx.case_id)} is part of this wave; the plan is anchored on {esc(trigger)}, its first exam case.")
    elif ctx.case_id and not case_wave and r1_wave:
        lead.append(f"{esc(ctx.case_id)} is caught by the Round 1 detector {esc(r1_wave)} (Round 1 reference below); the ledger covers the Final Round "
                    "waves only, so no plan is shown for it.")
    elif ctx.case_id and not case_wave:
        lead.append(f"{esc(ctx.case_id)}: Not part of any detected wave — nothing to contain.")
    lead.append(f"Nothing here has been executed or written to FraudGraph. {esc(LOSS_SPLIT)}.")
    design.html_block(f'<div class="tc dt-notes">{"".join(f"<p>{x}</p>" for x in lead)}</div>')

    src = {"source": plan_res.get("source", ""), "as_of": plan_res.get("as_of", ""), "how": plan_res.get("how", "")}
    per_card = [r for r in (plan or {}).get("per_card_plan") or [] if isinstance(r, dict)]
    left, right = st.columns([1, 1.4], gap="large")
    with left:
        if plan:
            block = [r for r in per_card if r.get("recommended_action") == "BLOCK_CARD"]
            l1 = sum(r.get("approval_route") == "L1" for r in block)
            broad = [str(c) for c in plan.get("legitimate_customers_touched_if_broad_rule_used") or []]
            touched = [str(c) for c in plan.get("legitimate_customers_touched_by_targeted_plan") or []]
            with st.container(border=True, key="strip-dt-plan"):
                row1, row2 = st.columns(2), st.columns(2)
            common.measured("Block now, if approved", len(block), container=row1[0], **src,
                            what="proposal — awaiting approval: wave cards with a wave transaction at or before the alert (BLOCK_CARD)")
            row1[0].caption(md(f"L1 {l1} · L2 {len(block) - l1}"))
            common.measured("Watched by rule", len(per_card) - len(block), container=row1[1], **src,
                            what="proposal — awaiting approval: wave cards whose wave transactions all come after the alert (MONITOR_CONNECTED_CARDS, auto)")
            row1[1].caption("auto")
            common.measured("Cleared customers the plan touches", len(touched), container=row2[0], **src,
                            what="wave customers with a wave transaction the bank cleared as legitimate (legitimate_customers_touched_by_targeted_plan): measured, not assumed")
            row2[0].caption(f":red[{md(', '.join(touched))}]" if touched else "none, measured")
            common.measured("Broad rule would also hit", len(broad), container=row2[1], **src,
                            what=f"customers outside the wave: {plan.get('broad_rule_description') or 'broad rule'}")
            if broad:
                buf = io.StringIO()
                pd.DataFrame({"customer_id": broad}).to_csv(buf, index=False)
                row2[1].download_button("Customers (CSV)", buf.getvalue(), file_name=f"{short}_broad_rule_customers.csv", mime="text/csv",
                                        on_click="ignore", icon=":material/download:", type="tertiary", key="det_broad_csv")
            notes = [f"<b>Shared choke-point</b> <span class=\"mono\">{esc(plan.get('shared_chokepoint') or 'not recorded')}</span>",
                     f"<b>Broad rule</b> {esc(plan.get('broad_rule_description') or 'not recorded')}",
                     f"<b>Scan window</b> {esc(plan_res.get('window') or 'not recorded')}"]
            card_case = {str(r.get("card_id")): str(r.get("case_id")) for r in pack_rows if r.get("card_id")}
            for r in per_card:
                other = card_case.get(str(r.get("card_id")))
                if other and other not in EXAM_SCAN.get(wave, []):
                    verdict, where = _verdict_of(other)
                    notes.append(f'<b>{esc(r.get("card_id"))}</b> is the card of {esc(other)} ({esc(verdict)}{" in " + esc(where) if where else ""}): the plan proposes '
                                 f'<span class="dt-flag">{esc(r.get("recommended_action"))} · {esc(r.get("approval_route"))}</span> on it.')
            backtest_legit = [str(c) for d in DETECTORS if d.get("detector_id") == wave for c in d.get("legitimate_customers_acted_against") or []]
            if backtest_legit:
                in_plan = sorted(set(backtest_legit) & {str(c) for c in plan.get("customers_affected") or []})
                notes.append(f"The full-history backtest of {esc(short)} matches cleared customer {esc(', '.join(backtest_legit))}; "
                             + (f"this plan includes {esc(', '.join(in_plan))}." if in_plan else "this plan's wave (as of the alert) does not include it."))
            design.html_block(f'<div class="tc dt-notes">{"".join(f"<p>{x}</p>" for x in notes)}</div>')
        elif totals:
            with st.container(border=True, key="strip-dt-plan"):
                row1, row2 = st.columns(2), st.columns(2)
            common.measured("Cards in the wave", totals.get("cards"), container=row1[0], **src, what="proposal — awaiting approval: every card in the wave")
            common.measured("Cleared customers the plan touches", totals.get("legit_touched_targeted"), container=row1[1], **src,
                            what="wave customers whose wave transactions the bank cleared as legitimate: measured")
            common.measured("Lost before the alert", common.money(totals.get("pre_as_of")), container=row2[0], **src,
                            what="wave exposure already settled at or before the trigger case opened")
            common.measured("Broad rule would also hit", totals.get("broad_rule_extra_customers"), container=row2[1], **src,
                            what="customers outside the wave the naive broad rule for this wave would also act on")
            st.caption("Block now / watched split, replay frames and per-card plan not in this deployment (summary totals only).")
        else:
            st.caption(md("Containment plan not available here: " + "; ".join(n for n in plan_res.get("notes") or [] if n)))
        if plan_res.get("notes") and (plan or totals):
            st.caption(md("Fallback used: " + "; ".join(n for n in plan_res["notes"] if n)))
    with right:
        frames = [f for f in (plan or {}).get("replay_frames") or [] if isinstance(f, dict)]
        if not frames:
            st.caption("Ring replay: frames not in this deployment.")
        else:
            n = len(frames)
            alert = max((i for i, f in enumerate(frames, 1) if f.get("before_as_of")), default=1)
            if st.session_state.get("_det_frame_wave") != wave or not 1 <= int(st.session_state.get("det_frame") or 0) <= n:
                st.session_state["det_frame"] = alert
                st.session_state["_det_frame_wave"] = wave
            with st.container(key="dt-replay"):
                with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
                    st.button("Prev", icon=":material/chevron_left:", key="det_prev", on_click=_step, args=(-1, n))
                    st.button("Next", icon=":material/chevron_right:", key="det_next", on_click=_step, args=(1, n))
                    st.slider("Ring replay frame", 1, n, key="det_frame", help=f"Opens on frame {alert}, the last transaction at or before the alert.")
                idx = int(st.session_state["det_frame"])
                f = frames[idx - 1]
                phase = ('<span class="dt-lost">before the alert</span>' if f.get("before_as_of") else '<span class="dt-prev">after the alert (hindsight)</span>')
                design.html_block(
                    f'<p class="tc dt-frame"><b>Frame {idx}/{n}</b> · {esc(f.get("ts"))} · txn <span class="tc-mono">{esc(f.get("txn_id"))}</span> on '
                    f'<span class="tc-mono">{esc(f.get("card_id"))}</span> ({esc(common.money(f.get("amt_usd")))}) · {_n(f.get("cumulative_cards"))}/'
                    f'{_n(plan.get("n_cards_in_wave"))} cards active · cumulative <b>{esc(common.money(f.get("cumulative_exposure_usd")))}</b> · {phase}</p>')
                st.altair_chart(_replay_chart(frames, plan.get("as_of"), idx), width="stretch", key="det_replay_chart")
                st.caption(md(f"Dashed: the alert, {plan.get('as_of')} · shaded: after the alert · solid: frame {idx}"))
                with st.expander("Challenge 5 · With vs. Without Ring Replay (Time-to-Understand Comparison)"):
                    trig_card = str((per_card[0].get("card_id") if per_card else "") or f.get("card_id") or "")
                    single_frames = [x for x in frames if str(x.get("card_id")) == trig_card and x.get("before_as_of")]
                    w1, w2 = st.columns(2)
                    with w1:
                        st.markdown(f"**Without Ring Replay (Single-Card Table: `{trig_card}`)**")
                        st.caption(f"Shows only {len(single_frames)} isolated charge(s) on 1 card (~4 min of manual cross-card SQL joins to spot the wave):")
                        st.dataframe(
                            pd.DataFrame([{"ts": x.get("ts"), "txn_id": x.get("txn_id"), "card_id": x.get("card_id"), "amt_usd": x.get("amt_usd")} for x in single_frames]),
                            hide_index=True,
                        )
                    with w2:
                        st.markdown(f"**With Ring Replay (Synchronized {plan.get('n_cards_in_wave')}-Card Wave)**")
                        st.caption(f"Reveals in < 8 seconds: {n} transactions across {plan.get('n_cards_in_wave')} cards ({common.money(plan.get('exposure_already_lost_usd'))} before alert · {common.money(plan.get('exposure_preventable_usd'))} preventable after alert):")
                        st.dataframe(
                            pd.DataFrame([{"frame": i, "ts": x.get("ts"), "card_id": x.get("card_id"), "amt_usd": x.get("amt_usd"), "cards_active": x.get("cumulative_cards"), "phase": "pre-alert" if x.get("before_as_of") else "post-alert"} for i, x in enumerate(frames[:10], 1)]),
                            hide_index=True,
                        )
    if plan and per_card:
        with st.expander(f"Per-card plan, transaction ids and attestation ({len(per_card)} cards)"):
            card_case = {str(r.get("card_id")): str(r.get("case_id")) for r in pack_rows if r.get("card_id")}
            st.dataframe(
                pd.DataFrame([{"Card": r.get("card_id"), "Customer": r.get("customer_id"),
                               "Proposed action": f"{r.get('recommended_action')} · {r.get('approval_route')}",
                               "Exposure": r.get("wave_exposure_usd"), "Txns": r.get("n_wave_txns"), "First": r.get("first_wave_ts"), "Last": r.get("last_wave_ts"),
                               "Cleared customer": "yes" if r.get("is_legitimate_customer_harmed") else "no",
                               "Final Round case": card_case.get(str(r.get("card_id")), ""),
                               "Transaction ids": ", ".join(str(t) for t in r.get("txn_ids") or [])} for r in per_card]),
                hide_index=True, key="det_per_card", height=common.table_height(len(per_card), 12),
                column_config={
                    "Proposed action": st.column_config.TextColumn("Proposed action · route",
                                                                   help="proposal — awaiting approval: BLOCK_CARD for a card with a wave transaction at or before the alert, else watched by rule"),
                    "Exposure": st.column_config.NumberColumn("Wave exposure", format="dollar"),
                    "Cleared customer": st.column_config.TextColumn("Cleared customer", help="the customer has a wave transaction the bank cleared as legitimate"),
                    "Final Round case": st.column_config.TextColumn("Final Round case", help="a Final Round case whose case-pack card is this card"),
                    "Transaction ids": st.column_config.TextColumn("Transaction ids", width="large"),
                })
            att = plan.get("attestation") or {}
            st.caption(md(f"Plan attestation · {att.get('what', 'not recorded')}"))
            st.json({"evidence": att.get("evidence"), "how": att.get("how"), "when": att.get("when")}, expanded=False)
            d_att = next(((d.get("attestation") or {}) for d in DETECTORS if d.get("detector_id") == wave), {})
            st.caption(md(f"Backtest attestation · {d_att.get('what', 'not recorded')}"))
            st.json({"evidence": d_att.get("evidence"), "how": d_att.get("how"), "when": d_att.get("when")}, expanded=False)
elif not WAVES and bt["data"] is not None:
    st.caption(md("No wave has a trigger case in the backtest's exam_scan, so there is nothing to plan."))


# --------------------------------------------------------------------------- #
# Partial / not built: C2 self-growing proposals, C3 policy text -> detector  #
# --------------------------------------------------------------------------- #
session = ctx.session
shapes = {(d.get("cards_matched"), d.get("txns_matched")): _short_id(d.get("detector_id")) for d in NEW_DETECTORS}


def _replays(p: dict) -> tuple[str | None, str]:
    """(detector, how we know) for the wave query a proposal replays: the same SQL as engine.detectors.WAVE_SQL over the
    backtest window when the module is here, else the same card and transaction counts as that detector's backtest."""
    sql = " ".join(str(p.get("synthesized_sql") or "").split())
    if det_mod is not None and sql:
        for det_id, q in (getattr(det_mod, "WAVE_SQL", None) or {}).items():
            try:
                text = " ".join(str(q).format(since="2016-07-01").split())
            except (KeyError, IndexError, ValueError):
                continue
            if text == sql:
                return _short_id(det_id), f"the same SQL as WAVE_SQL['{det_id}'] from 2016-07-01"
    same = shapes.get((p.get("cards_matched"), p.get("txns_matched")))
    return (same, f"the same {p.get('cards_matched')} cards and {p.get('txns_matched')} transactions as {same}'s backtest") if same else (None, "")


def _gate_reasons() -> list[str]:
    """Every reason this session cannot approve or reject a proposal; empty when it may (a signed-in fraud manager on a
    writable deployment with engine.detectors.approve_detector and an approver directory)."""
    out = []
    if ctx.read_only:
        out.append("read-only deployment (DEPLOY_READONLY=1)")
    if det_mod is None or not hasattr(det_mod, "approve_detector"):
        out.append(f"engine.detectors.approve_detector unavailable: {det_err or 'not defined'}")
    dirst = approvals_db.directory_status()
    if not dirst["exists"]:
        out.append(f"no approver directory ({common.rel(dirst['path'])})")
    elif dirst["error"]:
        out.append(f"approver directory unreadable: {dirst['error']}")
    elif not any(dirst["roles"].get(r) for r in approvals_db.roles_with("approve_detector")):
        out.append("no fraud manager or server admin in the approver directory")
    if session is None:
        out.append("not signed in as a fraud manager or the server admin")
    elif session.role not in approvals_db.roles_with("approve_detector"):
        out.append(f"fraud manager or server admin only; signed in as {session.principal} ({session.role})")
    return out


@st.dialog("Confirm detector decision")
def _decide(pid: str, decision: str) -> None:
    st.markdown(f"**{md(pid)}** → **{md(decision)}** as {md(session.principal if session else 'signed out')}")
    st.info("Final: recorded in data/detector_approvals.json and in the approval audit log (route 'gate').")
    if st.button("Confirm", type="primary", key="det_dlg_confirm"):
        actor, role = (session.principal, session.role) if session else ("", "unauthenticated")
        msgs = st.session_state.setdefault("det_gate_msg", {})
        try:
            rec = det_mod.approve_detector(pid, session, decision)
        except (PermissionError, OSError, ValueError) as exc:
            approvals_db.audit_gate(pid, decision, actor, role, "denied_rbac" if isinstance(exc, PermissionError) else "error", str(exc), common.approvals_db_path())
            msgs[pid] = {"ok": False, "text": f"Refused: {exc}"}
        else:
            approvals_db.audit_gate(pid, decision, actor, role, "ok", f"{decision} via the Detectors page", common.approvals_db_path())
            msgs[pid] = {"ok": True, "text": f"{decision}: {rec}"}
        st.rerun()


reasons = _gate_reasons()
def _mined_replay(p: dict) -> tuple[str, str]:
    bt_m = p.get("backtest") or {}
    cards = ", ".join(bt_m.get("january_card_ids") or [])
    exam = ", ".join(f"{e.get('card_id')} / {e.get('case_id')}" for e in bt_m.get("exam_cases_caught") or [])
    return (f"mined ({exam or 'no exam case'})", f"mined from uncovered January activity: matches {cards or 'no cards'}" + (f" (exam cases: {exam})" if exam else ""))


replays = {str(p.get("proposal_id")): (_mined_replay(p) if p.get("_mined") else _replays(p)) for p in PROPOSALS}
rep_list = "/".join(r for r, _ in replays.values() if r and not r.startswith("mined"))
c3_value, c3_detail, c3_src, c3_as_of = _c3()
c3_word = c3_value or "not measured here"
c2_built = any( p.get("_mined") and _status(p) == "approved" for p in PROPOSALS )
c2_word = "built" if c2_built else "partial"
design.html_block(design.section("", "Self-growing & policy-compiled detectors", f"C2 {c2_word} · C3 {c3_word}"))
if not PROPOSALS:
    st.caption(md("C2: no self-growing proposals in the backtest." if bt["data"] is not None else f"C2 not available here: {bt['error']}"))
else:
    head = (f"<b>C2 · Self-growing proposals</b> {tag(c2_word, 'green' if c2_built else 'ochre')} "
            + (f"mined cluster <code>{esc(MINED_PROP.get('proposal_id'))}</code> ({esc(DISC_DATA.get('clusters_found'))} clusters scanned in <code>engine/out/discovery_jan2017.json</code>; matches {esc(", ".join(f"{e.get('card_id')} in {e.get('case_id')}" for e in ((MINED_PROP.get('backtest') or {}).get('exam_cases_caught') or [])) or "no exam case")}) plus " if MINED_PROP else "")
            + (f"hand-written candidates replaying the {esc(rep_list)} wave queries." if rep_list else "candidate proposals."))
    body = []
    for p in PROPOSALS:
        pid = str(p.get("proposal_id"))
        rep, how = replays[pid]
        status = _status(p)
        legit = [str(c) for c in p.get("legitimate_customers_acted_against") or []]
        rec = APPROVALS.get(pid)
        by = f'<span class="sub">{esc(rec.get("status"))} by {esc(rec.get("by"))} ({esc(rec.get("role"))}) at {esc(rec.get("at"))}</span>' if isinstance(rec, dict) else ""
        body.append(
            f'<tr><td class="mono">{esc(pid)}</td><td>{esc(p.get("name"))}</td>'
            f'<td title="{esc(how)}">{esc(rep) if rep else DASH}</td>'
            f'<td class="num">{_n(p.get("cards_matched"))}</td><td class="num">{_n(p.get("txns_matched"))}</td>'
            f'<td class="num">{_usd(p.get("total_exposure_usd"))}</td>'
            f'<td class="num">{_n(p.get("closed_case_false_positives", 0))}{(" · " + esc(", ".join(legit))) if legit else ""}</td>'
            f'<td>{tag(status, {"approved": "green", "rejected": ""}.get(status, "ochre"))}{by}</td></tr>')
    design.html_block(
        f'<div class="tc dt-c2"><p class="dt-c2-head">{head}</p><table class="tc-table"><thead><tr><th>Proposal</th><th>Name</th><th>Replays</th>'
        '<th class="num">Cards</th><th class="num">Txns</th><th class="num">Exposure</th><th class="num">Cleared cases hit</th><th>Admission</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
        f'<p class="dt-src">{esc(BT_SRC["source"])} · {esc(BT_SRC["as_of"])} · decisions: {esc(APPROVALS_NOTE or common.rel(APPROVALS_FILE) + " " + common.as_of(APPROVALS_FILE))}</p></div>')
    if MINED_PROP and isinstance(MINED_PROP.get("case_catch_demo"), dict):
        cd = MINED_PROP["case_catch_demo"]
        bef, aft = cd.get("before_approval") or {}, cd.get("after_approval") or {}
        design.html_block(
            f'<div class="tc dt-notes"><p><b>Challenge 2 · Mined Detector Case Catch ({esc(cd.get("exam_case_id"))} · card {esc(cd.get("card_id"))} · flagged txn {esc(cd.get("flagged_txn_id"))} · opened {esc(cd.get("case_opened_at"))})</b></p>'
            f'<p><b>Before fraud-manager approval:</b> detector not active, matched txns: <code>{esc(", ".join(bef.get("matched_txn_ids") or []) or "none")}</code></p>'
            f'<p><b>After fraud-manager approval ({esc(_status(MINED_ROW[0]))}):</b> {esc(aft.get("decision"))} '
            f'(matched txns on {esc(cd.get("card_id"))}: <code>{esc(", ".join(aft.get("matched_txn_ids") or []))}</code> · peer cards: <code>{esc(", ".join(aft.get("connected_card_ids") or []))}</code>)</p></div>'
        )
    blocked = ("blocked: " + "; ".join(reasons)) if reasons else "this session may decide"
    design.html_block(f'<div class="tc dt-gate">{glyph("gate")}<b>Admission gate</b>{tag("L2 · fraud manager", "gate")}<span>{esc(blocked)}</span></div>')
    for pid, msg in (st.session_state.get("det_gate_msg") or {}).items():
        (st.success if msg.get("ok") else st.error)(md(f"{pid}: {msg.get('text')}"))
    with st.expander("Decide a proposal (fraud manager) and its synthesized SQL", expanded=not reasons):
        for p in PROPOSALS:
            pid = str(p.get("proposal_id"))
            status = _status(p)
            why = "; ".join(reasons) or (f"already {status}" if status != "pending" else None)
            with st.container(horizontal=True, vertical_alignment="center", gap="small", key=f"det-prop-{_slug(pid)}"):
                st.markdown(f"**{md(pid)}** · {md(p.get('name'))}")
                if st.button("Approve", key=f"det_approve_{pid}", icon=":material/check:", disabled=why is not None, help=md(why) if why else None):
                    _decide(pid, "approved")
                if st.button("Reject", key=f"det_reject_{pid}", icon=":material/close:", disabled=why is not None, help=md(why) if why else None):
                    _decide(pid, "rejected")
            st.code(str(p.get("synthesized_sql") or "not recorded"), language=None, wrap_lines=True)  # plain: the sql theme's keywords sat at 4:1

design.html_block(f'<div class="tc dt-c3"><b>C3 · Policy text → detector</b>{tag(c3_word, "green" if c3_value == "built" else "")}'
                  f'<span>{esc(c3_detail)}</span><span class="tc-muted" title="{esc(f"Source: {c3_src} · as of {c3_as_of}")}">{esc(c3_src)} · {esc(c3_as_of)}</span></div>')


# --------------------------------------------------------------------------- #
# Folded: the safety cross-check, the Round 1 reference, the operator re-run  #
# --------------------------------------------------------------------------- #
design.html_block(_safety_line())
r0_rows = [d for d in DETECTORS if str(d.get("detector_id", "")).startswith("R0_")]
with st.expander(f"Round 1 reference ({len(r0_rows)} detectors)", expanded=False):
    if not r0_rows:
        st.caption("No Round 1 detector in the backtest.")
    else:
        trs = "".join(
            f'<tr><td class="mono">{esc(d.get("detector_id"))}</td><td>{esc(d.get("name"))}</td>'
            f'<td class="mono">{esc(", ".join(str(c) for c in d.get("exam_cases_caught") or [])) or DASH}</td>'
            f'<td class="num">{_n(d.get("cards_matched"))} · {_n(d.get("cards_matched_january"))}</td><td class="num">{_n(d.get("txns_matched"))}</td>'
            f'<td class="num">{_n(len(d.get("closed_cases_caught") or []))} · {_n(len(d.get("cleared_cases_matched") or []))}</td></tr>' for d in r0_rows)
        design.html_block(
            '<div class="tc"><table class="tc-table"><thead><tr><th>Detector</th><th>Name</th><th>Exam cases</th><th class="num">Cards · January</th>'
            f'<th class="num">Txns</th><th class="num">Closed cases: fraud · cleared</th></tr></thead><tbody>{trs}</tbody></table>'
            f'<p class="dt-src">{esc(BT_SRC["source"])} · {esc(BT_SRC["as_of"])}</p></div>')


# --------------------------------------------------------------------------- #
# Operator: re-run the backtest (never on view)                               #
# --------------------------------------------------------------------------- #
with st.expander("Operator · re-run the backtest", expanded=False):
    st.caption(md(f"Loading this page never recomputes: it reads {BT_SRC['source']} (written {BT_SRC['as_of']}). "
                  "Re-running rewrites that file from the read-only DuckDB."))
    bt_mod, bt_err = (None, "read-only deployment") if ctx.read_only else common.optional_import("engine.backtest_detectors")
    if st.button("Re-run backtest", icon=":material/replay:", key="det_rerun", disabled=bt_mod is None, help=md(bt_err) if bt_err else None):
        with st.status("Running engine.backtest_detectors.run_backtest()…", expanded=True) as status_box:
            try:
                bt_mod.run_backtest()
            except Exception as exc:  # noqa: BLE001 - shown verbatim, the old file stays
                status_box.update(label=md(f"Backtest failed: {type(exc).__name__}: {exc}"), state="error")
            else:
                _engine_plan.clear()
                status_box.update(label="Backtest written", state="complete")
                st.rerun()
    if bt_mod is None:
        st.caption(md(f"Disabled: {bt_err}"))

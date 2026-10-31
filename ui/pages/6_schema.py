"""Data quality & schema (C11 data quality, C12 schema drift, C13 someone else's schema), served as the 'data' page.

Everything shown comes from common.proof (engine/out/proof_snapshot.json sections and data/schema_approvals.json) unless
the operator asks for a live recompute, or from schema_engine modules loaded through common.optional_import. Rendering
writes nothing under data/ or runs/: the migration gate is decided only by an authenticated click, the variant mapper is a
read-only viewer, and custom DDL is always analysed as text (never read as a server path).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

md = common.md

ctx = common.page_setup("data")
dq_p, drift_p, appr_p = common.proof("dq"), common.proof("drift"), common.proof("schema_approval")
gsql_p, var_p = common.proof("gsql"), common.proof("variant")
ro = common.read_only()

TABS = ["C11 · Data quality", "C12 · Schema drift", "C13 · Someone else's schema"]
PRESETS = ("bank1_iso20022_payments", "bank2_bian_service_domains", "bank3_core_banking_t24")
CUSTOM = "custom"
SCHEMA_DIR = common.ROOT / "data" / "schemas"
VARIANT_DIR = common.ROOT / "data" / "variant_bank_b"
GRADER = common.ROOT / "data" / "raw" / "round2" / "grade.py"
GATE_ROLES = ("team_lead", "fraud_manager")
SEVERITY = {"critical": "red", "high": "orange", "medium": "yellow", "low": "gray"}
VERIFIED_COLORS = {"yes": "green", "partly": "orange", "NO": "red", "—": "gray"}
TRANSFORMS = ["cents_to_usd", "epoch_to_iso", "channel_synonym", "split_device_bundle"]
# what an answer would say when it takes a defect into account (a keyword rule over the evidence claims and refs)
DQ_WORDS = {
    "DQ-01": r"identity|device[_ ]id|no device|device fingerprint|fingerprint",
    "DQ-02": r"in[- _]person|channel",
    "DQ-03": r"card6|card type|funding type|card[_ ]?map",
    "DQ-04": r"spoof|user[- ]agent|desktop|contradict|mobile",
    "DQ-05": r"thin|few prior|prior transactions|baseline|history",
    "DQ-06": r"billing region|addr1|region",
}
_LLM_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _gen(p: dict) -> str:
    return p["generated_at"] or p["as_of"] or "—"


def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def _rel(path: Path) -> str:
    """A repo-relative path for display, without resolving symlinks."""
    try:
        return str(path.relative_to(common.ROOT))
    except ValueError:
        return str(path)


def _missing(reason: str) -> None:
    st.info(md(reason), icon=":material/extension_off:")


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_PAREN = re.compile(r"\([^()]*\)")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATE_FMT = re.compile(r"^Y{2,4}M{0,2}D{0,2}H{0,2}M{0,2}S{0,2}$")


def _ast_names(ast) -> set[str]:
    """Every vertex, primary id, attribute, edge and reverse-edge name of a parsed DDL, lower-cased."""
    names: set[str] = set()
    for v in ast.vertices.values():
        names |= {v.name, v.primary_id, *(a.name for a in v.attributes)}
    for e in ast.edges.values():
        names |= {e.name, e.reverse_edge, *(a.name for a in e.attributes)}
    return {n.lower() for n in names if n}


def _snapshot_names(entry: dict) -> set[str] | None:
    """The DDL names the snapshot recorded (vertex and edge names, and every name each mapping was found to cite)."""
    ddl = entry.get("ddl") if isinstance(entry.get("ddl"), dict) else None
    if ddl is None:
        return None
    names = {str(n) for n in (ddl.get("vertices") or []) + (ddl.get("edges") or [])}
    for m in entry.get("ddl_names_by_mapping") or []:
        names |= {str(n) for n in m.get("ddl_names_found") or []}
    return {n.lower() for n in names}


def _tokens(text: object) -> list[str]:
    s = _QUOTED.sub(" ", str(text or ""))
    prev = None
    while prev != s:
        prev, s = s, _PAREN.sub(" ", s)
    return _TOKEN.findall(s)


def _verify(m: dict, names: set[str] | None) -> tuple[str, list[str]]:
    """'yes' when every schema-like token of mapped_to and filter_or_path is a DDL name, 'partly' when some only occur inside
    DDL names, 'NO' (with the names) when some occur nowhere in the DDL; '—' for unmapped rows or when nothing was parsed."""
    if m.get("status") == "unmapped" or names is None:
        return "—", []
    missing, partial, seen = [], [], 0
    for tok in _tokens(m.get("mapped_to")) + _tokens(m.get("filter_or_path")):
        low = tok.lower()
        if low in names or (low.startswith("reverse_") and low[8:] in names):
            seen += 1
        elif tok.islower() or len(tok) < 3 or _DATE_FMT.match(tok):
            continue  # prose, a unit or a date format
        elif len(tok) >= 4 and any(low in n for n in names):
            partial.append(tok)
        else:
            missing.append(tok)
    if missing or not (seen or partial):
        return "NO", sorted(set(missing)) or ["no DDL name cited"]
    return ("partly", sorted(set(partial))) if partial else ("yes", [])


def _preset(stem: str, analyst, parser) -> tuple[dict | None, set[str] | None, str, str]:
    """(analysis, DDL names, label, error) for a bank preset: live from data/schemas when the analyser is here, else the snapshot."""
    path = SCHEMA_DIR / f"{stem}.gsql"
    err = ""
    if analyst is not None:
        if path.is_file():
            names = _ast_names(parser.parse_gsql_ddl(path.read_text(encoding="utf-8"))) if parser is not None else None
            return analyst.analyze_gsql_schema(path).to_dict(), names, f"live recompute · {_rel(path)}", ""
        err = f"Preset file {_rel(path)} is missing in this deployment"
    entry = (gsql_p["data"] or {}).get(stem) if isinstance(gsql_p["data"], dict) else None
    if isinstance(entry, dict):
        return entry, _snapshot_names(entry), f"snapshot {_gen(gsql_p)}", err
    return None, None, "not available", err or f"{stem}: {gsql_p['error'] or 'not in the snapshot'}"


def _variant() -> tuple[dict | None, str, str]:
    """(mapping, label, as_of): live from data/variant_bank_b, else its discovered_mapping.json, else the snapshot."""
    mapper, _ = common.optional_import("schema_engine.mapper")
    if mapper is not None and VARIANT_DIR.is_dir():
        try:
            return mapper.infer_variant_mapping(VARIANT_DIR), f"live recompute · {_rel(VARIANT_DIR)}", "computed on this page"
        except (OSError, ValueError, KeyError) as exc:
            st.warning(md(f"infer_variant_mapping failed: {type(exc).__name__}: {exc}"))
    vfile = VARIANT_DIR / "discovered_mapping.json"
    if vfile.is_file():
        try:
            return json.loads(vfile.read_text(encoding="utf-8")), _rel(vfile), common.as_of(vfile)
        except (OSError, ValueError):
            pass
    if isinstance(var_p["data"], dict):
        return var_p["data"], f"snapshot {_gen(var_p)}", _gen(var_p)
    return None, "not available", var_p["error"] or "missing"


def _recompute() -> None:
    """Operator callback: the DQ scan and the drift detector, live, kept for this session only."""
    try:
        dqm = common.optional_import("schema_engine.data_quality")[0]
        drm = common.optional_import("schema_engine.drift")[0]
        st.session_state["data_live"] = {"dq": dqm.run_data_quality_scan(), "drift": drm.detect_round2_drift(), "at": time.strftime("%H:%M:%S"), "error": ""}
    except Exception as exc:  # shown with its reason; the snapshot stays
        st.session_state["data_live"] = {"error": f"{type(exc).__name__}: {exc}", "at": time.strftime("%H:%M:%S")}


def _replay(code: str, claim: dict) -> None:
    """Operator callback: schema_engine.attestation.verify_attestation against the facts DB, opened read-only."""
    out = st.session_state.setdefault("data_replay", {})
    try:
        att = common.optional_import("schema_engine.attestation")[0]
        duckdb = common.optional_import("duckdb")[0]
        con = duckdb.connect(str(Path(os.getenv("ENGINE_FACTS_DB", str(common.ROOT / "data" / "hhgoa_engine.duckdb")))), read_only=True)
        try:
            out[code] = {**att.verify_attestation(con, claim), "at": time.strftime("%H:%M:%S")}
        finally:
            con.close()
    except Exception as exc:  # shown next to the defect
        out[code] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "at": time.strftime("%H:%M:%S")}


def _llm_refine(text: str, analyst) -> None:
    """Operator callback: the AI pass on top of the AST mapping. The run log it writes goes to a temporary folder, never runs/.
    A success is kept per text hash for this session; a failure is shown with its reason and never kept."""
    key = hashlib.sha256(text.encode()).hexdigest()
    try:
        cfg, err = common.optional_import("agent.config")
        if cfg is None:
            raise RuntimeError(err)
        with _LLM_LOCK, tempfile.TemporaryDirectory(prefix="schema-ai-") as td:
            orig = cfg.SETTINGS
            cfg.SETTINGS = dataclasses.replace(orig, runs_dir=Path(td))
            try:
                res = analyst.analyze_gsql_schema(text, use_llm=True).to_dict()
            finally:
                cfg.SETTINGS = orig
        if not str(res.get("analyzed_by", "")).startswith("AI Schema Analyst"):
            raise RuntimeError(f"the LLM pass did not run: {res.get('analyzed_by')}")
    except Exception as exc:  # the reason is shown; nothing is cached
        st.session_state["data_llm_error"] = f"{type(exc).__name__}: {exc}"
        return
    st.session_state.setdefault("data_llm", {})[key] = res
    st.session_state.pop("data_llm_error", None)


def _shape_rows(before: dict, after: dict, severity: str) -> list[dict]:
    def val(x: object) -> str:
        return "—" if x is None else json.dumps(x, default=str) if isinstance(x, (list, dict)) else str(x)

    rows = []
    for k in list(dict.fromkeys([*(before or {}), *(after or {})])):
        b, a = (before or {}).get(k), (after or {}).get(k)
        change = "added" if k not in (before or {}) else "removed" if k not in (after or {}) else "same" if a == b else "changed"
        rows.append({"Field": k, "Before": val(b)[:200], "After": val(a)[:200], "Change": change, "Severity": severity})
    return rows


def _card_mismatch(view, meta: dict) -> str:
    notes = [e for e in common.guardrail_events(view) if e["type"] == "card_mismatch"]
    if notes:
        e = notes[0]
        return f"{e.get('pack_card')} → {e.get('txn_card')} (trace note)"
    if not view.ok:
        return ""
    pack_card, cust = str(meta.get("card_id") or ""), str(meta.get("customer_id") or "")
    cited = {str(i) for ev in (view.answer.get("case") or {}).get("evidence") or [] if isinstance(ev, dict) for i in ev.get("entity_ids") or []}
    others = sorted(i for i in cited if cust and re.fullmatch(re.escape(cust) + r"-K\d+", i) and i != pack_card)
    if others and pack_card not in cited:
        return f"{pack_card} → {', '.join(others)} (answer cites)"
    return ""


def _cites(answer: dict | None, items: list[dict], meta: dict) -> str:
    """Per defect: does the answer's evidence take the taint into account (a keyword per defect code, or a tainted id other
    than the case's own flagged transaction, card and customer)?"""
    if not isinstance(answer, dict) or not items:
        return "—"
    ev = [e for e in (answer.get("case") or {}).get("evidence") or [] if isinstance(e, dict)]
    text = " ".join(f"{e.get('claim', '')} {e.get('ref', '')}" for e in ev)
    ids = {str(i) for e in ev for i in e.get("entity_ids") or []}
    own = {str(meta.get(k) or "") for k in ("flagged_txn_id", "card_id", "customer_id")}
    out = []
    for it in items:
        code = str(it.get("code"))
        hit_ids = sorted((ids & {str(i) for i in it.get("entity_ids") or []}) - own)
        word = re.search(DQ_WORDS.get(code, r"$^"), text, re.I)
        out.append(f"{code}: {'yes' if word or hit_ids else 'no'}")
    return " · ".join(out)


# --------------------------------------------------------------------------- #
# Data                                                                        #
# --------------------------------------------------------------------------- #
live = st.session_state.get("data_live") if isinstance(st.session_state.get("data_live"), dict) else {}
if live.get("dq") is not None:
    dq, dq_label, dq_src, dq_as_of = live["dq"], f"live recompute {live['at']}", "schema_engine.data_quality.run_data_quality_scan", live["at"]
    drift, drift_label, drift_src, drift_as_of = live["drift"], f"live recompute {live['at']}", "schema_engine.drift.detect_round2_drift", live["at"]
else:
    dq, dq_label, dq_src, dq_as_of = dq_p["data"], f"snapshot {_gen(dq_p)}", dq_p["source"] or "engine/out/proof_snapshot.json", _gen(dq_p)
    drift, drift_label, drift_src, drift_as_of = drift_p["data"], f"snapshot {_gen(drift_p)}", drift_p["source"] or "engine/out/proof_snapshot.json", _gen(drift_p)
dq = dq if isinstance(dq, dict) else None
drift = drift if isinstance(drift, dict) else None
MIGRATION_ID = str((drift or {}).get("migration_id") or "round2_2017q1_migration")
appr = (appr_p["data"] or {}).get(MIGRATION_ID) if isinstance(appr_p["data"], dict) else None
gate_status = str((appr or {}).get("status") or "unknown")

analyst, analyst_err = common.optional_import("schema_engine.ai_analyst")
parser, parser_err = common.optional_import("schema_engine.gsql_parser")
snap_stems = sorted(gsql_p["data"]) if isinstance(gsql_p["data"], dict) else []
stems = list(dict.fromkeys([*PRESETS, *snap_stems, *(p.stem for p in sorted(SCHEMA_DIR.glob("*.gsql")))]))
presets = {s: _preset(s, analyst, parser) for s in stems}
ok_presets = {s: v for s, v in presets.items() if v[0] is not None}
variant, var_label, var_as_of = _variant()

pack_rows, pack_note = common.final_round_pack()
pack_ids = [r["case_id"] for r in pack_rows]

# --------------------------------------------------------------------------- #
# Header and scoreboard                                                       #
# --------------------------------------------------------------------------- #
common.page_header("Data quality & schema", eyebrow="C11 data quality · C12 schema drift · C13 someone else's schema")
for b in ctx.banners:
    st.warning(md(b))

t1, t2, t3 = st.columns(3)
with t1:
    n_tainted = (dq or {}).get("n_tainted_cases", len((dq or {}).get("taint_matrix") or {})) if dq else None
    common.measured("Data quality", f"{len(dq.get('defects') or [])} defect classes · {n_tainted}/{len(pack_ids)} cases tainted" if dq else None,
                    source=dq_src, as_of=dq_as_of, how="defect classes from SQL over the facts DB, joined to the case pack; the denominator is the Final Round case pack")
    st.caption(md(dq_label if dq else f"not available: {dq_p['error']}"))
with t2:
    common.measured("Schema drift", f"{len(drift.get('drifts') or [])} drifts · migration {MIGRATION_ID} {gate_status.upper()}" if drift else None,
                    source=f"{drift_src}; {appr_p['source'] or 'data/schema_approvals.json'}", as_of=f"{drift_as_of}; {appr_p['as_of'] or 'missing'}",
                    how="drifts detected between Round 1 and the January files; gate status from data/schema_approvals.json")
    st.caption(md(f"{drift_label if drift else 'drift not available: ' + drift_p['error']} · What the gate blocks today: nothing in the January delta path "
                  "(etl/delta*.py) checks it; the engine facts and FraudGraph already include January."))
with t3:
    evals = [(v[0].get("benchmark_eval") or {}) for v in ok_presets.values()]
    sm, tt = sum(int(e.get("status_matches") or 0) for e in evals), sum(int(e.get("total_targets") or 0) for e in evals)
    n_cols = (variant or {}).get("n_mapped_columns", len((variant or {}).get("mappings") or [])) if variant else None
    value = (f"{len(ok_presets)} bank schemas" + (f" · {n_cols} CSV variant columns mapped" if n_cols is not None else "")) if ok_presets else None
    labels = sorted({v[2].split(" · ")[0] for v in ok_presets.values()})
    map_as_of = "computed on this page" if labels == ["live recompute"] else _gen(gsql_p)
    common.measured("Mapping", value, source=f"{', '.join(labels) or 'missing'}; variant: {var_label}", as_of=f"{map_as_of}; {var_as_of}",
                    how="AST mapping of each bank .gsql onto the canonical fraud ontology, and the CSV variant column mapper")
    st.caption(md(f"{' / '.join(labels) or 'not available'} · mapping only, no investigation run on a variant · "
                  + (f"self-check vs the files' own answer-key comments: {sm}/{tt}" if tt else "no answer-key comments to self-check against")))

dqm_ok = not common.optional_import("schema_engine.data_quality")[1] and not common.optional_import("schema_engine.drift")[1]
why = None if dqm_ok and not ro else ("read-only deployment" if ro else "schema_engine.data_quality / drift not available here")
rc1, rc2 = st.columns([1, 3], vertical_alignment="center")
rc1.button("Recompute DQ and drift now", key="data_recompute", icon=":material/refresh:", disabled=bool(why), help=md(why) if why else None, on_click=_recompute)
if live.get("error"):
    rc2.error(md(f"Recompute failed at {live['at']}: {live['error']}"))
else:
    rc2.caption("Reads the facts DB read-only and keeps the result for this session; nothing is written.")

tab_dq, tab_drift, tab_map = st.tabs(TABS, key="data_tab", on_change="rerun", default=TABS[0])

# --------------------------------------------------------------------------- #
# C11 · Data quality                                                          #
# --------------------------------------------------------------------------- #
if tab_dq.open:
    with tab_dq:
        if dq is None:
            _missing(f"No data-quality scan on this deployment: {dq_p['error'] or 'snapshot section missing'}.")
        else:
            tm = dq.get("taint_matrix") or {}
            sev_of = {str(d.get("code")): str(d.get("severity")) for d in dq.get("defects") or []}
            src_id = ctx.source_id or ""
            rows, mismatches = [], []
            for r in pack_rows:
                cid = r["case_id"]
                items = [it for it in tm.get(cid) or [] if isinstance(it, dict)]
                v = common.case_view(src_id, cid)
                card = _card_mismatch(v, v.meta or r)
                if card:
                    mismatches.append((cid, card))
                fin = _final(v.answer) if v.ok else []
                rows.append({
                    "Case": cid, "DQ codes": sorted({str(it.get("code")) for it in items}),
                    "Pack card vs transaction card": card or "—",
                    "Verdict": [str((v.answer.get("case") or {}).get("verdict"))] if v.ok else [],
                    "Final actions": ", ".join(f"{x.get('action')} · {x.get('route', 'auto')}" for x in fin if x.get("action") != "CREATE_CASE")
                    or ("—" if v.ok else common.STATE_LABELS.get(v.state, v.state)),
                    "Answer cites the taint": _cites(v.answer, items, v.meta or r),
                    "Entity ids": ", ".join(dict.fromkeys(str(i) for it in items for i in it.get("entity_ids") or [])),
                })
            codes = sorted({c for row in rows for c in row["DQ codes"]})
            st.markdown(f"**Per-case join** · {md(len([x for x in rows if x['DQ codes']]))} of {md(len(rows))} cases tainted · answers of {md(src_id)}")
            st.dataframe(
                rows, hide_index=True, width="stretch", height=35 * len(rows) + 38, key="data_join",
                column_config={
                    "Case": st.column_config.ButtonColumn("Case", type="tertiary", pinned=True, key="data_case_open", help="Open the Case file"),
                    "DQ codes": st.column_config.MultiselectColumn("DQ codes", options=codes or None, color=[SEVERITY.get(sev_of.get(c, ""), "gray") for c in codes] or None,
                                                                   help="defects joined to the case by SQL (snapshot or live recompute)"),
                    "Verdict": st.column_config.MultiselectColumn("Verdict", options=list(common.VERDICT_COLORS), color=list(common.VERDICT_COLORS.values())),
                    "Pack card vs transaction card": st.column_config.TextColumn("Pack card vs transaction card", help="the case pack names one card, the transaction record another"),
                    "Final actions": st.column_config.TextColumn("Final actions", width="medium"),
                    "Answer cites the taint": st.column_config.TextColumn(
                        "Answer cites the taint", help="per defect: a keyword for the defect in the evidence claims or refs, or a tainted id other than the case's own transaction, card and customer"),
                    "Entity ids": st.column_config.TextColumn("Entity ids", width="large"),
                })
            click = st.session_state.get("data_case_open")
            if click:
                common.goto("case", case=click["label"])
            st.caption(md(f"DQ: {dq_label} ({dq_src}) · answers and traces: {src_id}" + (f" · pack: {pack_note}" if pack_note else "")))

            st.markdown("#### Defect classes")
            replays = st.session_state.get("data_replay") or {}
            att_ok = not common.optional_import("schema_engine.attestation")[1] and not common.optional_import("duckdb")[1]
            replay_why = None if att_ok and not ro else ("read-only deployment" if ro else "schema_engine.attestation or duckdb not available here")
            for d in dq.get("defects") or []:
                code, sev = str(d.get("code")), str(d.get("severity"))
                ids = [str(i) for i in d.get("affected_ids") or []]
                with st.container(border=True, key=f"dq-{code}"):
                    hc1, hc2, hc3, hc4 = st.columns([5, 1, 1, 1], vertical_alignment="center")
                    hc1.markdown(f"**{md(code)} · {md(d.get('title'))}** · {md(f'{int(d.get('count') or 0):,}')} record{'' if int(d.get('count') or 0) == 1 else 's'} {common.badge(sev, SEVERITY.get(sev, 'gray'))}")
                    with hc2.popover("Ids", key=f"dq-ids-{code}"):
                        st.caption(md(f"{len(ids)} stored of {int(d.get('count') or 0):,}"))
                        st.code(", ".join(ids), language=None, wrap_lines=True)
                    with hc3.popover("Attestation", key=f"dq-att-{code}"):
                        st.json(d.get("attestation") or {}, expanded=True)
                    hc4.button("Replay SQL", key=f"dq-replay-{code}", disabled=bool(replay_why) or not d.get("attestation"), help=md(replay_why) if replay_why else None,
                               on_click=_replay, args=(code, d.get("attestation") or {}))
                    st.caption(md(d.get("description")))
                    st.caption(md("Sample ids: " + (", ".join(ids[:5]) or "none") + " · cases: " + (", ".join(d.get("tainted_cases") or []) or "none")))
                    if code in replays:
                        rp = replays[code]
                        sql = str(((d.get("attestation") or {}).get("how") or {}).get("sql") or "")
                        limit = re.search(r"\bLIMIT\s+(\d+)", sql, re.I)
                        msg = (f"Replayed at {rp['at']}: {rp.get('row_count', 0):,} rows" + (f" (the SQL has LIMIT {limit.group(1)})" if limit else "")
                               + f" against the claimed {int(d.get('count') or 0):,} · {rp.get('mode', '')}") if not rp.get("error") else f"Replay failed at {rp['at']}: {rp['error']}"
                        (st.success if rp.get("ok") else st.error)(md(msg))
            with st.container(border=True, key="dq-card-mismatch"):
                st.markdown(f"**Case pack card differs from the transaction card** · {md(len(mismatches))} case(s) {common.badge('from the traces and answers', 'gray')}")
                st.caption(md("; ".join(f"{c}: {m}" for c, m in mismatches) if mismatches else f"No case in {src_id} shows a card mismatch note."))

        if GRADER.is_file():
            st.markdown("#### Answer-key grader")
            g_why = "read-only deployment" if ro else None
            if st.button("Run data/raw/round2/grade.py on cases/HHF-\\*.json", key="data_run_grader", icon=":material/play_arrow:", disabled=bool(g_why), help=g_why):
                work = Path(tempfile.mkdtemp(prefix="grade-"))  # the grader sees only a temporary teams dir and writes its report there
                try:
                    (work / "teams" / "ours" / "cases").mkdir(parents=True)
                    for f in common.cases_dir().glob("HHF-*.json"):
                        shutil.copy(f, work / "teams" / "ours" / "cases" / f.name)
                    r2 = common.ROOT / "data" / "raw" / "round2"
                    res = subprocess.run([sys.executable, str(GRADER), "--teams-dir", str(work / "teams"), "--case-pack", str(r2 / "case_pack_final.csv"),
                                          "--transactions", str(r2 / "transactions_2017q1.csv"), "--out", str(work / "grade_out.json")],
                                         cwd=str(work), capture_output=True, text=True, check=False, timeout=600)
                    if res.returncode != 0:
                        st.error(md(f"grade.py exited {res.returncode}: {res.stderr[-800:]}"))
                    st.code(res.stdout[-3000:] or "(no output)", language=None)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        else:
            st.caption("Accuracy against the answer key is not measured here: data/raw/round2/grade.py is not in this checkout.")

# --------------------------------------------------------------------------- #
# C12 · Schema drift                                                          #
# --------------------------------------------------------------------------- #
if tab_drift.open:
    with tab_drift:
        drift_mod, drift_err = common.optional_import("schema_engine.drift")
        session = common.approver_session()
        with st.container(border=True, key="data-gate"):
            st.markdown(f"**Migration gate {md(MIGRATION_ID)}** " + " ".join([
                common.badge(gate_status, "orange" if gate_status == "pending" else "green" if gate_status == "approved" else "gray"),
                common.badge("roles: team_lead / fraud_manager", "violet")]))
            if appr and appr.get("approved_by"):
                st.caption(md(f"Approved by {appr.get('approved_by')} ({appr.get('role')}) at {appr.get('approved_at')} · final"))
            st.caption(md(f"Source: {appr_p['source'] or 'missing'} · as of {appr_p['as_of'] or '—'}" + (f" · {appr_p['error']}" if appr_p["error"] else "")))
            st.caption("What it blocks today: nothing in the January delta path (etl/delta*.py) checks it; the engine facts and FraudGraph already include January.")
            gate_why = (f"schema_engine.drift: {drift_err}" if drift_mod is None else None) or ("read-only deployment" if ro else None) \
                or ("Sign in first (identity menu in the top bar)" if session is None else None) \
                or (None if session.role in GATE_ROLES else f"{session.role} cannot approve: needs team_lead or fraud_manager") \
                or (None if gate_status == "pending" else f"already {gate_status}")

            @st.dialog("Approve schema migration")
            def _gate_dialog() -> None:
                sess = common.approver_session()
                st.markdown(f"**{md(MIGRATION_ID)}** → **approved** as {md(f'{sess.principal} ({sess.role})' if sess else 'signed out')}")
                st.info("This decision is final and is logged in the approval audit log (route 'gate').")
                if st.button("Confirm (final)", type="primary", key="data_gate_confirm"):
                    actor, role = (sess.principal, sess.role) if sess else ("", "unauthenticated")
                    try:
                        rec = drift_mod.approve_schema_migration(MIGRATION_ID, session=sess)
                    except (PermissionError, OSError, ValueError) as exc:
                        approvals_db.audit_gate(MIGRATION_ID, "approved", actor, role, "denied_rbac" if isinstance(exc, PermissionError) else "error", str(exc),
                                                common.approvals_db_path())
                        st.session_state["data_gate_msg"] = {"ok": False, "text": f"Refused: {exc}"}
                    else:
                        approvals_db.audit_gate(MIGRATION_ID, "approved", actor, role, "ok", "approved on the Data quality & schema page", common.approvals_db_path())
                        st.session_state["data_gate_msg"] = {"ok": True, "text": f"approved: {rec.get('approved_by')} at {rec.get('approved_at')}"}
                    st.rerun()

            gc1, gc2 = st.columns([1, 2], vertical_alignment="center")
            if gc1.button("Approve (final)…", key="data_gate_approve", type="primary", disabled=bool(gate_why), help=md(gate_why) if gate_why else None):
                _gate_dialog()
            gc2.caption("Reject is not implemented in schema_engine.drift.")
            if (m := st.session_state.get("data_gate_msg")):
                (st.success if m["ok"] else st.error)(md(m["text"]))
            common.page_link("approvals", "Approvals › Other gates", icon=":material/gavel:")

        if drift is None:
            _missing(f"No drift report on this deployment: {drift_p['error'] or 'snapshot section missing'}.")
        else:
            st.caption(md(f"{drift_label} · {drift_src}" + (f" · detected at {drift.get('detected_at')}" if drift.get("detected_at") else "")))
            for dr in drift.get("drifts") or []:
                sev = str(dr.get("severity"))
                with st.container(border=True, key=f"drift-{dr.get('drift_id')}"):
                    h1, h2 = st.columns([6, 1], vertical_alignment="center")
                    h1.markdown(f"**{md(dr.get('drift_id'))} · {md(dr.get('title'))}** {common.badge(sev, SEVERITY.get(sev, 'gray'))}")
                    with h2.popover("Attestation", key=f"drift-att-{dr.get('drift_id')}"):
                        st.json(dr.get("attestation") or {}, expanded=True)
                    shape = _shape_rows(dr.get("before_shape") or {}, dr.get("after_shape") or {}, sev)
                    st.dataframe(shape, hide_index=True, width="stretch", height=35 * len(shape) + 38,
                                 column_config={"Before": st.column_config.TextColumn("Before", width="large"),
                                                "After": st.column_config.TextColumn("After", width="large")})
                    st.markdown(f"**Breaks if unhandled:** {md(dr.get('what_breaks'))}")
                    st.markdown(f"**Recovery:** {md(dr.get('remapping_action'))}")

# --------------------------------------------------------------------------- #
# C13 · Someone else's schema                                                 #
# --------------------------------------------------------------------------- #
if tab_map.open:
    with tab_map:
        st.caption("Mapping only: no investigation has been run on a variant schema. The bank schemas are mapped onto the canonical fraud "
                   "ontology and the CSV variant's columns onto the canonical tables; no case was investigated against either.")
        options = [*stems, CUSTOM]
        choice = st.segmented_control("Schema", options, default=options[0], key="data_schema", required=True,
                                      format_func=lambda s: "Custom DDL" if s == CUSTOM else md(s)) or options[0]
        analysis, names, label, text = None, None, "", ""
        if choice == CUSTOM:
            up = st.file_uploader("Upload a .gsql file (1 MB at most)", type=["gsql", "txt"], max_upload_size=1, key="data_ddl_upload")
            pasted = st.text_area("Or paste GSQL DDL", max_chars=200_000, height=160, key="data_ddl", placeholder="CREATE GRAPH Bank_Custom () ...")
            raw = up.getvalue().decode("utf-8", errors="replace") if up is not None else (pasted or "")
            text = raw + "\n" if raw.strip() else ""  # always text plus a newline, so it is never read as a server path
            if text:
                sec, sec_err = common.optional_import("agent.security")
                findings = sec.scan_text(text, "gsql_ddl")[1] if sec is not None else None
                if findings is None:
                    st.caption(md(f"Injection scan unavailable ({sec_err}); the live AI pass stays off."))
                elif findings:
                    st.warning(md(f"The DDL carries instruction text ({', '.join(sorted({f.code for f in findings}))}); the live AI pass stays off."))
                if analyst is None:
                    _missing(f"Custom DDL analysis needs schema_engine.ai_analyst: {analyst_err}")
                else:
                    analysis = analyst.analyze_gsql_schema(text).to_dict()
                    names = _ast_names(parser.parse_gsql_ddl(text)) if parser is not None else None
                    label = "computed on this page · pasted / uploaded DDL"
                backend = os.getenv("LLM_BACKEND", "").split("#")[0].strip().lower()  # an inline .env comment is not a backend name
                if analysis is not None and not ro and backend not in ("", "mock"):
                    use_llm = st.toggle(md(f"Refine with live AI ({backend})"), key="data_use_llm", disabled=findings is None or bool(findings),
                                        help="Calls the configured LLM on top of the comment-blind AST mapping; its run log goes to a temporary folder.")
                    if use_llm and findings == []:
                        st.button("Run the AI pass", key="data_llm_run", icon=":material/auto_awesome:", on_click=_llm_refine, args=(text, analyst))
                        if st.session_state.get("data_llm_error"):
                            st.error(md(f"AI pass failed (not cached): {st.session_state['data_llm_error']}"))
                        got = (st.session_state.get("data_llm") or {}).get(hashlib.sha256(text.encode()).hexdigest())
                        if got:
                            analysis, label = got, f"AI pass · {got.get('analyzed_by')}"
            else:
                st.caption("Paste or upload a GSQL schema to map it.")
        else:
            analysis, names, label, err = presets.get(choice, (None, None, "", f"unknown preset {choice}"))
            if err:
                st.error(md(err))
            if analysis is None and not err:
                _missing(f"{choice}: not available in this deployment ({analyst_err or gsql_p['error']}).")

        if analysis is not None:
            maps = [m for m in analysis.get("mappings") or [] if isinstance(m, dict)]
            st.caption(md(f"{label} · graph {analysis.get('graph_name')} · {analysis.get('architecture_family')} · "
                          f"{analysis.get('n_vertices')} vertices · {analysis.get('n_edges')} edges · analysed by {analysis.get('analyzed_by')}"
                          + ("" if names is not None else " · DDL names not available here, so 'verified in DDL' is not checked")))
            if not analysis.get("n_vertices") and not analysis.get("n_edges"):
                st.warning("No vertex or edge was parsed from this text: any mapping below comes from the analyser's fallback, not from this DDL.")
            checks = [_verify(m, names) for m in maps]
            unverified = [(m.get("canonical_target"), bad) for m, (state, bad) in zip(maps, checks, strict=True) if state == "NO"]
            if unverified:
                st.error(md(f"{len(unverified)} mapping(s) name things that are not in this DDL, so they are not mappings of it: "
                            + "; ".join(f"{t} ({', '.join(b)})" for t, b in unverified)), icon=":material/error:")
            st.dataframe(
                [{"Canonical target": m.get("canonical_target"), "Category": m.get("category"), "Status": m.get("status"),
                  "Verified in DDL": [state], "Mapped to": m.get("mapped_to") or "—", "Path / filter": m.get("filter_or_path") or "—",
                  "Confidence": "—" if m.get("status") == "unmapped" else f"{float(m.get('confidence') or 0):.2f}",
                  "Not found in the DDL": ", ".join(bad), "Rationale": m.get("rationale")}
                 for m, (state, bad) in zip(maps, checks, strict=True)],
                hide_index=True, width="stretch", height=min(35 * len(maps) + 38, 35 * 20 + 38), key="data_mapping",
                column_config={
                    "Canonical target": st.column_config.TextColumn("Canonical target", pinned=True),
                    "Verified in DDL": st.column_config.MultiselectColumn("Verified in DDL", options=list(VERIFIED_COLORS), color=list(VERIFIED_COLORS.values()),
                                                                          help="computed on this page: every vertex / edge / attribute name in 'mapped to' and 'path' must be a name of the parsed DDL; partly = only inside a longer DDL name"),
                    "Rationale": st.column_config.TextColumn("Rationale", width="large"),
                })
            ev = analysis.get("benchmark_eval") or {}
            common.measured("Self-check vs this file's answer-key comments", f"{ev['status_matches']}/{ev['total_targets']}" if ev.get("total_targets") else None,
                            source=label, as_of=_gen(gsql_p) if label.startswith("snapshot") else "computed on this page",
                            how="mapping statuses against the comments in the same file (stripped before inference); not an independent accuracy measure")
            enabled = analysis.get("enabled_detectors") or []
            disabled = analysis.get("disabled_detectors") or []
            st.caption(md("Detectors enabled on this schema: " + (", ".join(map(str, enabled)) or "none")))
            if disabled:
                st.caption(md("Disabled: " + "; ".join(f"{x.get('detector')} ({x.get('reason')})" for x in disabled if isinstance(x, dict))))
            for q in analysis.get("adapted_queries") or []:
                with st.expander(md(f"{q.get('query_name')} — generated, not installed or syntax-checked")):
                    st.caption(md(f"{q.get('detector')} · uses {', '.join(map(str, q.get('uses_mappings') or []))}"))
                    st.code(str(q.get("gsql") or ""), language="sql")

        st.markdown("#### CSV variant column mapper")
        if not isinstance(variant, dict) or not variant.get("mappings"):
            _missing(f"No variant CSV mapping on this deployment ({var_as_of}).")
        else:
            vm = [m for m in variant["mappings"] if isinstance(m, dict)]
            tr_opts = list(dict.fromkeys([*TRANSFORMS, *sorted({str(m.get("transform")) for m in vm})]))
            st.caption(md(f"{var_label} · {len(vm)} columns · roles: " + ", ".join(f"{k} → {v}" for k, v in (variant.get("table_roles") or {}).items())))

            def _sample(m: dict) -> str:
                s = (m.get("sample_records") or [{}])[0]
                return f"{s.get('raw_value', '')} → {s.get('canonical_value', '')}" if s else "—"

            st.dataframe(
                [{"Source column": f"{m.get('source_file')} / {m.get('source_column')}", "Canonical target": f"{m.get('table_role')}.{m.get('canonical_column')}",
                  "Transform": [str(m.get("transform"))], "Confidence": float(m.get("confidence") or 0), "Raw → canonical sample": _sample(m)} for m in vm],
                hide_index=True, width="stretch", height=min(35 * len(vm) + 38, 35 * 20 + 38), key="data_variant",
                column_config={
                    "Transform": st.column_config.MultiselectColumn("Transform", options=tr_opts, color=["violet" if t in TRANSFORMS else "gray" for t in tr_opts]),
                    "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0.0, max_value=1.0, format="%.2f"),
                    "Raw → canonical sample": st.column_config.TextColumn("Raw → canonical sample", width="large"),
                })

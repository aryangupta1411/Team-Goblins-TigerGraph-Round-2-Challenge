"""Data quality & schema, served as the 'data' page: which data problems touch which cases, and is the January migration
approved.

First screen: three challenge headers that also switch the view, each with its headline figure, one line of scope and
its source and as-of: data quality (C11), drift and the migration gate (C12), schema mapping (C13). Recompute is a quiet
action beside them. Data quality: the tainted cases (all 18 on a toggle) and the defect classes as rows, with ids,
attestation and Replay SQL in each row's detail. Drift: one gate status line with its single Approve home, then the drifts
as collapsible rows. Mapping: the schema picker, summary figures and the mapping table, custom DDL / AI refine / adapted
GSQL folded, and the CSV variant as a sub-view: pick the checked-in Bank B or a variant brought in (uploaded CSVs, one
flat file, or a test variant generated from the January files), see each mapped column with its five attested sample
records and the unmapped / low-confidence columns for review, accept or reject the mapping, and run the offline engine
on the variant (ops.variant_rehearsal) to compare its decisions with the canonical schema's.

Everything shown comes from common.proof (engine/out/proof_snapshot.json sections and data/schema_approvals.json) unless
the operator asks for a live recompute, or from schema_engine modules loaded through common.optional_import. Rendering
writes nothing under data/ or runs/: the migration gate is decided only by an authenticated click, a variant is brought in,
decided or rehearsed only by a click and only under runs/_uploads/variant-<id>/ (ui/variant_jobs.py holds every guard:
read-only deployments refused, the 'variant' permission where RUNS_NEED_APPROVER is set), and custom DDL is always
analysed as text (never read as a server path).
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

from ui import approvals_db, common, design, variant_jobs  # noqa: E402

md, esc = common.md, design.esc

ctx = common.page_setup("data")
dq_p, drift_p, appr_p = common.proof("dq"), common.proof("drift"), common.proof("schema_approval")
gsql_p, var_p = common.proof("gsql"), common.proof("variant")
ro = common.read_only()

# the three challenges: (view key, title in words, challenge code)
VIEWS = (("dq", "Data quality", "C11"), ("drift", "Drift and the migration gate", "C12"), ("map", "Schema mapping", "C13"))
MAP_VIEWS = ("Bank schemas (GSQL)", "CSV variant")
PRESETS = ("bank1_iso20022_payments", "bank2_bian_service_domains", "bank3_core_banking_t24")
CUSTOM = "custom"
SCHEMA_DIR = common.ROOT / "data" / "schemas"
VARIANT_DIR = common.ROOT / "data" / "variant_bank_b"
GRADER = common.ROOT / "data" / "raw" / "round2" / "grade.py"
GATE_ROLES = tuple(approvals_db.roles_with("approve_migration"))
# a defect or a drift is a data warning: orange for critical and high, grey below (red stays for fraud and hard actions)
SEVERITY = {"critical": "orange", "high": "orange", "medium": "gray", "low": "gray"}
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


def _shape_rows(before: dict, after: dict) -> list[dict]:
    def val(x: object) -> str:
        return "—" if x is None else json.dumps(x, default=str) if isinstance(x, (list, dict)) else str(x)

    rows = []
    for k in list(dict.fromkeys([*(before or {}), *(after or {})])):
        b, a = (before or {}).get(k), (after or {}).get(k)
        change = "added" if k not in (before or {}) else "removed" if k not in (after or {}) else "same" if a == b else "changed"
        rows.append({"Field": k, "Before": val(b)[:200], "After": val(a)[:200], "Change": change})
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
# Header: three challenge headers that switch the view, and the recompute    #
# --------------------------------------------------------------------------- #
def _set_view(view: str) -> None:
    st.session_state["data_view"] = view


def _when(value: object) -> str:
    """A timestamp as 'YYYY-MM-DD HH:MM' (a clock time or a phrase passes through)."""
    s = str(value or "").replace("T", " ").rstrip("Z")
    return s[:16] if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s) else (s or "—")


if st.session_state.get("data_view") not in {v for v, _, _ in VIEWS}:
    st.session_state["data_view"] = "dq"
view = st.session_state["data_view"]

GATE_FILE = appr_p["source"] or "data/schema_approvals.json"
gate_here = appr_p["mode"] != "missing"
gate_word = gate_status if gate_here and appr is not None else "not in this deployment" if not gate_here else f"no record for {MIGRATION_ID}"
n_tainted = (dq or {}).get("n_tainted_cases", len((dq or {}).get("taint_matrix") or {})) if dq else None
n_defects = len((dq or {}).get("defects") or [])
n_drifts = len((drift or {}).get("drifts") or [])
evals = [(v[0].get("benchmark_eval") or {}) for v in ok_presets.values()]
sm, tt = sum(int(e.get("status_matches") or 0) for e in evals), sum(int(e.get("total_targets") or 0) for e in evals)
n_cols = (variant or {}).get("n_mapped_columns", len((variant or {}).get("mappings") or [])) if variant else None
map_labels = sorted({v[2].split(" · ")[0] for v in ok_presets.values()})
map_as_of = "computed on this page" if map_labels == ["live recompute"] else _gen(gsql_p)
map_src = " and ".join([*([f"{_rel(SCHEMA_DIR)}/*.gsql"] if "live recompute" in map_labels else []),
                         *([gsql_p["source"] or "engine/out/proof_snapshot.json"] if any(x.startswith("snapshot") for x in map_labels) else [])]) or "missing"

common.page_header("Data quality & schema", "Read from engine/out/proof_snapshot.json and data/schema_approvals.json; a recompute reads the facts DB "
                   "read-only and is kept for this session.", eyebrow=f"Final Round · January 2017 files · {len(pack_ids)} cases")

dqm_ok = not common.optional_import("schema_engine.data_quality")[1] and not common.optional_import("schema_engine.drift")[1]
why = None if dqm_ok and not ro else ("read-only deployment" if ro else "schema_engine.data_quality / drift not available here")
with st.container(horizontal=True, vertical_alignment="center", gap="small", key="dq-actions"):
    if live.get("error"):
        st.caption(md(f"Recompute failed at {live['at']}: {live['error']}"))
    elif live.get("dq") is not None:
        st.caption(md(f"Data quality and drift recomputed at {live['at']}, kept for this session"))
    st.space("stretch")
    st.button("Recompute data quality and drift", key="data_recompute", type="tertiary", icon=":material/refresh:", disabled=bool(why),
              help=md(why) if why else None, on_click=_recompute)

HEADS = {
    "dq": {"label": "Cases touched by a data defect", "value": f"{n_tainted} of {len(pack_ids)}" if dq else None, "source": dq_src, "as_of": dq_as_of,
           "how": "cases in the taint matrix (defects joined to the case pack by SQL over the facts DB) over the Final Round case pack",
           "scope": f"{n_defects} defect classes found by SQL over the January facts and joined to each case" if dq else f"no scan here: {dq_p['error'] or 'snapshot section missing'}",
           "line": f"{dq_src} · as of {_when(dq_as_of)}"},
    "drift": {"label": "January migration", "value": gate_word, "source": f"{GATE_FILE}; drifts: {drift_src}",
              "as_of": f"{appr_p['as_of'] or 'missing'}; drifts {drift_as_of}",
              "how": f"status of {MIGRATION_ID} in {GATE_FILE} (approved only by a signed-in team lead or fraud manager); drifts from the drift detector",
              "scope": f"{n_drifts} schema drifts from the Round 1 files to the January files, behind one migration gate" if drift else f"no drift report here: {drift_p['error'] or 'snapshot section missing'}",
              "line": f"{GATE_FILE} · as of {_when(appr_p['as_of'])}" if gate_here else f"{GATE_FILE} not in this deployment · drifts {drift_label}"},
    "map": {"label": "Bank schemas mapped", "value": str(len(ok_presets)) if ok_presets else None, "source": map_src, "as_of": map_as_of,
            "how": "comment-blind AST mapping of each bank .gsql onto the canonical fraud ontology; a CSV variant is mapped from value "
                   "fingerprints and rehearsed on the offline engine in the CSV variant view",
            "scope": "Bank schemas: mapping only" + (f" · a CSV variant of {n_cols} columns, rehearsable on the offline engine" if n_cols is not None else ""),
            "line": (f"{map_src} · " + ("computed on this page" if map_as_of == "computed on this page" else f"as of {_when(map_as_of)}"))
                    if ok_presets else f"not available: {analyst_err or gsql_p['error']}"},
}
with st.container(border=True, key="strip-dq-heads"):
    head_cols = st.columns(3)
for col, (k, title, code) in zip(head_cols, VIEWS, strict=True):
    h = HEADS[k]
    with col, st.container(key=f"dq-head-{k}"):
        st.button(title, key=f"data_view_{k}", type="tertiary", on_click=_set_view, args=(k,), width="content")
        common.measured(h["label"], h["value"], source=h["source"], as_of=h["as_of"], how=h["how"])
        design.html_block(f'<div class="tc dq-scope{" on" if k == view else ""}"><p>{esc(h["scope"])}</p>'
                          f'<p class="dq-src"><span class="dq-code">{esc(code)}</span> {esc(h["line"])}</p></div>')

# --------------------------------------------------------------------------- #
# Data quality (C11): tainted cases, then the defect classes as rows          #
# --------------------------------------------------------------------------- #
if view == "dq":
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
                "Case": cid, "Defects": sorted({str(it.get("code")) for it in items}),
                "Verdict": [str((v.answer.get("case") or {}).get("verdict"))] if v.ok else [],
                "Final actions": ", ".join(f"{x.get('action')} · {x.get('route', 'auto')}" for x in fin if x.get("action") != "CREATE_CASE")
                or ("—" if v.ok else common.STATE_LABELS.get(v.state, v.state)),
                "Mentions the taint (keyword match)": _cites(v.answer, items, v.meta or r),
                "Pack card vs transaction card": card or "—",
                "Entity ids": ", ".join(dict.fromkeys(str(i) for it in items for i in it.get("entity_ids") or [])) or "—",
            })
        tainted = [x for x in rows if x["Defects"]]
        show_all = st.session_state.get("data_dq_all", False)
        shown = rows if show_all else tainted
        codes = sorted({c for row in rows for c in row["Defects"]})
        design.html_block(design.section("", "Cases touched by a data defect", note=f"answers of {src_id}" if src_id else "",
                                         aside=esc(f"{len(shown)} of {len(rows)} cases shown")))
        st.toggle(f"Show all {len(rows)} cases", key="data_dq_all")
        if shown:
            st.dataframe(
                shown, hide_index=True, width="stretch", height=common.table_height(len(shown)), key="data_join",
                column_config={
                    "Case": st.column_config.ButtonColumn("Case", type="tertiary", pinned=True, key="data_case_open", help="Open the Case file"),
                    "Defects": st.column_config.MultiselectColumn("Defects", options=codes or None, color=[SEVERITY.get(sev_of.get(c, ""), "gray") for c in codes] or None,
                                                                  help="defect classes joined to the case by SQL (snapshot or live recompute)"),
                    "Verdict": st.column_config.MultiselectColumn("Verdict", options=list(common.VERDICT_COLORS), color=list(common.VERDICT_COLORS.values())),
                    "Final actions": st.column_config.TextColumn("Final actions", width="medium"),
                    "Mentions the taint (keyword match)": st.column_config.TextColumn(
                        "Mentions the taint (keyword match)", help="per defect: a keyword for the defect in the evidence claims or refs, or a tainted id other than the case's own transaction, card and customer"),
                    "Pack card vs transaction card": st.column_config.TextColumn("Pack card vs transaction card", help="the case pack names one card, the transaction record another"),
                    "Entity ids": st.column_config.TextColumn("Entity ids", width="large"),
                })
            click = st.session_state.get("data_case_open")
            if click:
                common.goto("case", case=click["label"])
        else:
            st.caption("No case in the taint matrix.")
        st.caption(md(f"{dq_src} · as of {_when(dq_as_of)} · answers: {src_id or 'no source'} · pack card differs from the transaction card on {len(mismatches)} case(s)"
                      + (": " + "; ".join(f"{c} {m}" for c, m in mismatches) if mismatches else "")))

        replays = st.session_state.get("data_replay") or {}
        att_ok = not common.optional_import("schema_engine.attestation")[1] and not common.optional_import("duckdb")[1]
        replay_why = None if att_ok and not ro else ("read-only deployment" if ro else "schema_engine.attestation or duckdb not available here")
        design.html_block(design.section("", "Defect classes", note="SQL over the facts DB, each with its attestation", aside=esc(f"{n_defects} classes")))
        with st.container(key="dq-defects"):
            for d in dq.get("defects") or []:
                code, sev = str(d.get("code")), str(d.get("severity"))
                ids = [str(i) for i in d.get("affected_ids") or []]
                count = int(d.get("count") or 0)
                cases = [str(c) for c in d.get("tainted_cases") or []]
                label = (f"**{md(code)}** · {md(d.get('title'))} · {md(f'{count:,}')} record{'' if count == 1 else 's'} · "
                         f"{md(len(cases))} case{'' if len(cases) == 1 else 's'} · :{SEVERITY.get(sev, 'gray')}[{md(sev)}]")
                with st.expander(label, key=f"dq-{code}"):
                    st.markdown(md(d.get("description")))
                    st.caption(md("Cases: " + (", ".join(cases) or "none")))
                    st.caption(md(f"Affected ids, {len(ids)} stored of {count:,}"))
                    st.code(", ".join(ids) or "none stored", language=None, wrap_lines=True)
                    st.caption("Attestation")
                    st.json(d.get("attestation") or {}, expanded=False)
                    st.button("Replay SQL", key=f"dq-replay-{code}", disabled=bool(replay_why) or not d.get("attestation"), help=md(replay_why) if replay_why else None,
                              on_click=_replay, args=(code, d.get("attestation") or {}))
                    if code in replays:
                        rp = replays[code]
                        sql = str(((d.get("attestation") or {}).get("how") or {}).get("sql") or "")
                        limit = re.search(r"\bLIMIT\s+(\d+)", sql, re.I)
                        msg = (f"Replayed at {rp['at']}: {rp.get('row_count', 0):,} rows" + (f" (the SQL has LIMIT {limit.group(1)})" if limit else "")
                               + f" against the claimed {count:,} · {rp.get('mode', '')}") if not rp.get("error") else f"Replay failed at {rp['at']}: {rp['error']}"
                        (st.success if rp.get("ok") else st.error)(md(msg))

    if GRADER.is_file():
        with st.expander("Answer-key grader · data/raw/round2/grade.py"):
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
# Drift and the migration gate (C12): the gate line, then the drifts         #
# --------------------------------------------------------------------------- #
if view == "drift":
    drift_mod, drift_err = common.optional_import("schema_engine.drift")
    session = common.approver_session()
    design.html_block(design.section("", "Migration gate", note="a signed-in team lead or fraud manager approves it", aside=f'<span class="tc-mono">{esc(MIGRATION_ID)}</span>'))
    with st.container(key="data-gate"):
        kind = {"pending": "ochre", "approved": "green"}.get(gate_status, "") if gate_here and appr is not None else ""
        who = (f" by <b>{esc(appr.get('approved_by'))}</b> ({esc(appr.get('role'))}) at {esc(_when(appr.get('approved_at')))}, final"
               if appr and appr.get("approved_by") else "")
        state = (f'is {design.tag(gate_status, kind, "gate")}{who}' if gate_here and appr is not None
                 else f"has {design.tag('no record', '', 'gate')} in {esc(GATE_FILE)}" if gate_here else f"is {design.tag('not in this deployment', '', 'gate')}")
        design.html_block(f'<p class="tc dq-gate">The January migration {state}. <b>What the gate blocks today: nothing</b>; the January delta path '
                          f'(<span class="tc-mono">etl/delta*.py</span>) never checks it, and the engine facts and FraudGraph already include January.</p>')
        st.caption(md(f"{GATE_FILE} · as of {appr_p['as_of']}" if gate_here else f"{GATE_FILE} is not in this deployment ({appr_p['error'] or 'missing'})"))
        gate_why = (f"schema_engine.drift: {drift_err}" if drift_mod is None else None) or ("read-only deployment" if ro else None) \
            or ("Sign in first (identity menu in the top bar)" if session is None else None) \
            or (None if session.role in GATE_ROLES else f"{session.role} cannot approve: needs {' or '.join(GATE_ROLES)}") \
            or (None if gate_status == "pending" else f"already {gate_word}")

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

        with st.container(horizontal=True, vertical_alignment="center", gap="medium", key="dq-gate-actions"):
            if st.button("Approve (final)…", key="data_gate_approve", type="primary", disabled=bool(gate_why), help=md(gate_why) if gate_why else None):
                _gate_dialog()
            st.caption("Reject is not implemented in schema_engine.drift.")
            st.space("stretch")
            common.page_link("approvals", "Approvals › Other gates", icon=":material/arrow_forward:")
        if (m := st.session_state.get("data_gate_msg")):
            (st.success if m["ok"] else st.error)(md(m["text"]))

    design.html_block(design.section("", "Schema drifts, Round 1 to January", note=drift_label if drift else "",
                                     aside=esc(f"{drift_src}" + (f" · detected {_when(drift.get('detected_at'))}" if drift and drift.get("detected_at") else ""))))
    if drift is None:
        _missing(f"No drift report on this deployment: {drift_p['error'] or 'snapshot section missing'}.")
    else:
        with st.container(key="dq-drifts"):
            for dr in drift.get("drifts") or []:
                sev = str(dr.get("severity"))
                with st.expander(f"**{md(dr.get('drift_id'))}** · {md(dr.get('title'))} · :{SEVERITY.get(sev, 'gray')}[{md(sev)}]", key=f"drift-{dr.get('drift_id')}"):
                    st.markdown(f"**Breaks if unhandled:** {md(dr.get('what_breaks'))}")
                    st.markdown(f"**Recovery:** {md(dr.get('remapping_action'))}")
                    shape = _shape_rows(dr.get("before_shape") or {}, dr.get("after_shape") or {})
                    st.dataframe(shape, hide_index=True, width="stretch", height=common.table_height(len(shape)),
                                 column_config={"Before": st.column_config.TextColumn("Before", width="large"),
                                                "After": st.column_config.TextColumn("After", width="large")})
                    with st.popover("Attestation", key=f"drift-att-{dr.get('drift_id')}"):
                        st.json(dr.get("attestation") or {}, expanded=True)

# --------------------------------------------------------------------------- #
# The CSV variant (C13): bring one in, review its mapping, rehearse on it     #
# --------------------------------------------------------------------------- #
CHECKED_IN_SRC = "@checked-in"  # the source key of data/variant_bank_b, viewed in place and copied into a job to rehearse
SRC_KEY = "data_variant_src"


def _folder(src: str) -> Path:
    return VARIANT_DIR if src == CHECKED_IN_SRC else variant_jobs.input_dir(src)


def _folder_sig(folder: Path) -> tuple:
    return tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(folder.glob("*.csv")))


@st.cache_data(max_entries=16, show_spinner="Mapping the variant (value fingerprints)…")
def _map_folder(folder: str, sig: tuple) -> dict | None:
    mapper, _ = common.optional_import("schema_engine.mapper")
    return mapper.infer_variant_mapping(folder) if mapper is not None else None


def _save_variant(session) -> None:
    files = {f.name: f.getvalue() for f in st.session_state.get("data_v_files") or []}
    name = str(st.session_state.get("data_v_name") or "").strip()
    res = variant_jobs.save_upload(name, files, session=session)
    st.session_state["data_v_msg"] = res
    if res["ok"]:
        st.session_state[SRC_KEY] = res["job"]


def _generate_variant(session) -> None:
    flat = bool(st.session_state.get("data_v_flat"))
    res = variant_jobs.make_test_variant(f"fresh-{time.strftime('%m%d-%H%M%S')}{'-flat' if flat else ''}", flat=flat, session=session)
    st.session_state["data_v_msg"] = res
    if res["ok"]:
        st.session_state[SRC_KEY] = res["job"]


def _run_variant(src: str, session) -> None:
    job = src
    if src == CHECKED_IN_SRC:
        res = variant_jobs.copy_checked_in(session=session)
        if not res["ok"]:
            st.session_state["data_v_run"] = res
            return
        job = res["job"]
        st.session_state[SRC_KEY] = job
    st.session_state["data_v_run"] = {**variant_jobs.launch_rehearsal(job, session=session), "job": job}


def _bring_variant(session, why: str) -> None:
    """The ways a variant comes in: uploaded CSVs (several, or one flat file, with an optional ground_truth.json), or a
    test variant generated from the January files."""
    with st.expander("Bring a variant", expanded=not variant_jobs.jobs(), key="data-v-bring"):
        if why:
            st.info(md(why), icon=":material/lock:")
            return
        with st.form("data_v_form", border=False, enter_to_submit=False):
            st.text_input("Name", key="data_v_name", placeholder="bank-c", help="A folder runs/_uploads/variant-<name>/; letters, digits, '.', '_' or '-'")
            st.file_uploader("CSV files: several, or one flat file · optional ground_truth.json to score the mapping", type=["csv", "json"],
                             accept_multiple_files=True, key="data_v_files", max_upload_size=variant_jobs.MAX_FILE_BYTES // 1_000_000)
            st.form_submit_button("Save variant", icon=":material/upload:", on_click=_save_variant, args=(session,))
        if variant_jobs.ROUND2.is_dir():
            with st.container(horizontal=True, vertical_alignment="center", gap="small", key="data-v-gen"):
                st.button("Generate a test variant", key="data_v_gen", icon=":material/shuffle:", type="tertiary", on_click=_generate_variant, args=(session,),
                          help="ops.variant_rehearsal.make_fresh_variant: the January files with new file and column names and other units, "
                               "plus the ground_truth.json that scores the mapping (the mapper never reads it)")
                st.toggle("one flat file", key="data_v_flat", help="transactions and device sessions in a single CSV")
        st.caption(md(f"At most {variant_jobs.MAX_FILES} files, {variant_jobs.MAX_FILE_BYTES // 1_000_000} MB each; nothing is written under data/."))
    res = st.session_state.get("data_v_msg")
    if res:
        if res["ok"]:
            st.caption(md(f"Saved as variant {res['job']}"))
        else:
            st.error(md(res["error"]), icon=":material/error:")


def _samples(vm: list[dict]) -> None:
    """One mapped column at a time with its five sample records (raw and translated) and the attestation behind it."""
    labels = {i: f"{m.get('source_file')} / {m.get('source_column')} → {m.get('canonical_column')}" for i, m in enumerate(vm)}
    pick = st.selectbox("Attested samples for", list(labels), format_func=labels.get, key="data_v_sample_col", width=560)
    m = vm[pick] if pick is not None and pick < len(vm) else None
    if not m:
        return
    rows = [{"Record": s.get("record_id"), "Raw value": s.get("raw_value"), "Canonical value": s.get("canonical_value")} for s in m.get("sample_records") or []]
    st.caption(md(f"{m.get('rationale') or ''} · transform {m.get('transform')} · confidence {float(m.get('confidence') or 0):.2f}"))
    st.dataframe(rows, hide_index=True, width="stretch", height=common.table_height(len(rows)), key=f"data_v_samples_{pick}")
    with st.popover("Attestation", key=f"data-v-att-{pick}"):
        st.json(m.get("attestation") or {}, expanded=True)


def _review(mapping: dict) -> None:
    unmapped = [u for u in mapping.get("unmapped_columns") or [] if isinstance(u, dict)]
    low = [x for x in mapping.get("low_confidence") or [] if isinstance(x, dict)]
    design.html_block(design.section("", "For review", note="what nothing matched, and what matched below 0.95, before the mapping is used",
                                     aside=esc(f"{len(unmapped)} unmapped · {len(low)} low confidence")))
    if not unmapped and not low:
        st.caption("Nothing to review: every column mapped at 0.95 or above.")
    if unmapped:
        st.dataframe([{"Column": f"{u.get('source_file')} / {u.get('source_column')}", "Table role": u.get("table_role"), "Why": u.get("reason"),
                       "Top values": ", ".join(map(str, u.get("top_values") or []))} for u in unmapped],
                     hide_index=True, width="stretch", height=common.table_height(len(unmapped)), key="data_v_unmapped")
    if low:
        st.dataframe([{"Column": f"{x.get('source_file')} / {x.get('source_column')}", "Mapped to": x.get("canonical_column"),
                       "Confidence": float(x.get("confidence") or 0), "Rationale": x.get("rationale")} for x in low],
                     hide_index=True, width="stretch", height=common.table_height(len(low)), key="data_v_low",
                     column_config={"Confidence": st.column_config.ProgressColumn("Confidence", min_value=0.0, max_value=1.0, format="%.2f")})


def _decision(job: str, mapping: dict, session) -> None:
    """Accept or reject this mapping (final for this mapping), like the C12 migration gate."""
    rec = variant_jobs.mapping_decision(job)
    sha = variant_jobs.mapping_sha(mapping)
    current = rec if rec and rec.get("mapping_sha") == sha else None
    kind = {"approved": "green", "rejected": "red"}.get(str((current or {}).get("status")), "ochre")
    state = (f'{design.tag(current["status"], kind, "gate")} by <b>{esc(current.get("by"))}</b> ({esc(current.get("role"))}) at {esc(current.get("at"))}, final'
             if current else design.tag("pending", "ochre", "gate") + (" · an earlier decision was on a different mapping" if rec else ""))
    design.html_block(f'<p class="tc dq-gate">Mapping <span class="tc-mono">{esc(sha)}</span> is {state}.</p>')
    why = ("read-only deployment" if ro else None) or ("Sign in first (identity menu in the top bar)" if session is None else None) \
        or (None if session.role in GATE_ROLES else f"{session.role} cannot decide: needs {' or '.join(GATE_ROLES)}") or ("already decided" if current else None)

    @st.dialog("Decide the variant mapping")
    def _dialog(decision: str) -> None:
        sess = common.approver_session()
        st.markdown(f"Mapping **{md(sha)}** of **{md(job)}** → **{md(decision)}** as {md(f'{sess.principal} ({sess.role})' if sess else 'signed out')}")
        note = st.text_input("Note (optional)", key="data_v_note", max_chars=300)
        st.info("This decision is final for this mapping and is logged in the approval audit log (route 'gate').")
        if st.button("Confirm (final)", type="primary", key="data_v_confirm"):
            try:
                variant_jobs.decide_mapping(job, decision, sess, mapping, note)
                st.session_state["data_v_decided"] = {"ok": True, "text": f"{decision}"}
            except (PermissionError, ValueError, OSError) as exc:
                st.session_state["data_v_decided"] = {"ok": False, "text": f"Refused: {exc}"}
            st.rerun()

    with st.container(horizontal=True, vertical_alignment="center", gap="small", key="data-v-decide"):
        if st.button("Accept mapping…", key="data_v_accept", type="primary", disabled=bool(why), help=md(why) if why else None):
            _dialog("approved")
        if st.button("Reject mapping…", key="data_v_reject", disabled=bool(why), help=md(why) if why else None):
            _dialog("rejected")
    if (m := st.session_state.pop("data_v_decided", None)):
        (st.success if m["ok"] else st.error)(md(m["text"]))


def _rehearsal_view(rep: dict) -> None:
    n, same = int(rep.get("n_cases") or 0), int(rep.get("n_same_decision") or 0)
    rows = rep.get("row_comparison") or {}
    differ = {c: v.get("differ") for c, v in (rows.get("columns") or {}).items() if isinstance(v, dict) and v.get("differ")}
    acc = rep.get("accuracy")
    lead = (f'<b class="{"green" if n and same == n else "red"}">Same decision on {same} of {n} case{"s" if n != 1 else ""}</b>'
            f' · {int(rows.get("n_in_canonical") or 0):,} of {int(rows.get("n_translated") or 0):,} translated transactions found in the canonical facts DB'
            + (f' · columns that differ: {esc(", ".join(f"{c} ({k})" for c, k in differ.items()))}' if differ else " · every compared column equal"))
    if isinstance(acc, dict):
        lead += (f' · against the ground truth: precision <b>{float(acc.get("precision") or 0):.0%}</b>, recall <b>{float(acc.get("recall") or 0):.0%}</b>'
                 f' ({int(acc.get("correct") or 0)} correct, {len(acc.get("wrong") or [])} wrong, {len(acc.get("missed") or [])} missed)')
    design.html_block(f'<p class="tc tc-lead">{lead}</p>')

    def _d(x: dict | None) -> str:
        return f"{x.get('verdict')} · {x.get('status')}" if x else "no decision"

    cases = rep.get("cases") or {}
    st.dataframe([{"Case": cid, "On the variant": _d(v.get("variant")), "On the canonical schema": _d(v.get("canonical")),
                   "Final actions (variant)": ", ".join((v.get("variant") or {}).get("final_actions") or []),
                   "Final actions (canonical)": ", ".join((v.get("canonical") or {}).get("final_actions") or []),
                   "Exposure (variant)": (v.get("variant") or {}).get("exposure_usd"), "Same": bool(v.get("same"))} for cid, v in cases.items()],
                 hide_index=True, width="stretch", height=common.table_height(len(cases)), key="data_v_cases",
                 column_config={"Same": st.column_config.CheckboxColumn("Same decision"),
                                "Exposure (variant)": st.column_config.NumberColumn("Exposure (variant)", format="dollar")})
    with st.expander(f"Row comparison · {len(differ)} column(s) differ", key="data-v-rows"):
        cols = rows.get("columns") or {}
        st.dataframe([{"Column": c, "Equal": v.get("equal"), "Differ": v.get("differ"),
                       "Example": "; ".join(f"{e.get('TransactionID')}: {e.get('variant')} → canonical {e.get('canonical')}" for e in v.get("examples") or [])}
                      for c, v in cols.items() if isinstance(v, dict)],
                     hide_index=True, width="stretch", height=common.table_height(len(cols)), key="data_v_rowcmp")
        if rows.get("missing_in_canonical"):
            st.caption(md("Not in the canonical facts DB: " + ", ".join(map(str, rows["missing_in_canonical"]))))
    if isinstance(acc, dict) and (acc.get("wrong") or acc.get("missed")):
        with st.expander("Ground-truth misses", key="data-v-acc"):
            st.dataframe([{"Column": w.get("column"), "Wanted": w.get("want"), "Got": w.get("got", "—")} for w in [*(acc.get("wrong") or []), *(acc.get("missed") or [])]],
                         hide_index=True, width="stretch", key="data_v_accrows")
    st.caption(md(f"rehearsal.json generated {rep.get('generated_at')} · scratch facts DB {common.rel(rep.get('rehearsal_db') or '')} · offline engine, no graph, no LLM"))


def _rehearsal(src: str, session, why: str) -> None:
    design.html_block(design.section("", "Run on the variant", note="translate it, put it in a scratch facts DB, decide every case on it and on the canonical DB"))
    job = None if src == CHECKED_IN_SRC else src
    state = variant_jobs.rehearsal_state(job) if job else {"state": "not run", "report": None, "log": []}
    if src == CHECKED_IN_SRC and any(j["id"] == "bank-b" for j in variant_jobs.jobs()):
        state = variant_jobs.rehearsal_state("bank-b")  # the checked-in variant is rehearsed as job bank-b
    held = variant_jobs.running()
    with st.container(horizontal=True, vertical_alignment="center", gap="small", key="data-v-run"):
        st.button("Run on variant" if state["state"] in ("not run", "failed") else "Run again", key="data_v_run_btn", type="primary", icon=":material/play_arrow:",
                  disabled=bool(why) or bool(held), on_click=_run_variant, args=(src, session),
                  help=md(why) if why else md(f"the rehearsal of {held.get('job')} is running") if held else
                  "python -m ops.variant_rehearsal <variant> --out runs/_uploads/variant-<id>/out (a few seconds per hundred transactions)")
        design.html_block(design.tag(state["state"], {"done": "green", "failed": "red", "running": "ochre"}.get(state["state"], "")))
        if state["state"] == "running":  # the view refreshes itself every 2 s; a background tab may throttle that
            st.button("Refresh", key="data_v_refresh", type="tertiary", icon=":material/refresh:")
    res = st.session_state.get("data_v_run")
    if res and not res.get("ok"):
        st.error(md(res.get("error") or "not started"), icon=":material/error:")
    if why:
        st.caption(md(why))
    if state["state"] == "done" and state["report"]:
        _rehearsal_view(state["report"])
    elif state["state"] in ("running", "failed") and state["log"]:
        st.caption(md(f"rehearse.log, last lines · started {state.get('started') or '—'}"))
        st.code("\n".join(state["log"]), language=None)


def _csv_variant_view() -> None:
    session = common.approver_session()
    why = variant_jobs.blocked(session)
    sources = ([(CHECKED_IN_SRC, "Bank B · checked in (data/variant_bank_b)")] if VARIANT_DIR.is_dir() else []) + \
        [(j["id"], f"{j['id']} · {j['source']} · {len(j['files'])} file(s)") for j in variant_jobs.jobs()]
    labels = dict(sources)
    if st.session_state.get(SRC_KEY) not in labels and sources:
        st.session_state[SRC_KEY] = sources[0][0]
    design.html_block(design.section("", "CSV variant columns onto the canonical tables", note="someone else's schema: mapped from value fingerprints, not names"))
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium", key="data-v-pick"):
        src = st.selectbox("Variant", list(labels), format_func=labels.get, key=SRC_KEY, width=460) if sources else None
    _bring_variant(session, why)
    if src is None:
        _missing("No variant here yet: bring one in above.")
        return
    folder = _folder(src)
    mapping = _map_folder(str(folder), _folder_sig(folder)) if folder.is_dir() else None
    if not isinstance(mapping, dict) or not mapping.get("mappings"):
        _missing(f"No column of {common.rel(folder)} could be mapped" + ("" if mapping is not None else " (schema_engine.mapper unavailable)") + ".")
        if isinstance(mapping, dict):
            _review(mapping)
        return
    vm = [m for m in mapping["mappings"] if isinstance(m, dict)]
    roles = ", ".join(f"{k} → {v}" for k, v in (mapping.get("table_roles") or {}).items())
    truth = variant_jobs.ground_truth(folder)
    st.caption(md(f"{len(vm)} columns mapped · table roles: {roles or 'not recorded'} · mapped {mapping.get('discovered_at')} from {common.rel(folder)}"
                  + (" · a ground_truth.json is present: the rehearsal scores the mapping against it" if truth else "")))
    tr_opts = list(dict.fromkeys([*TRANSFORMS, *sorted({str(m.get("transform")) for m in vm})]))

    def _sample(m: dict) -> str:
        s = (m.get("sample_records") or [{}])[0]
        return f"{s.get('raw_value', '')} → {s.get('canonical_value', '')}" if s else "—"

    st.dataframe(
        [{"Source column": f"{m.get('source_file')} / {m.get('source_column')}", "Canonical target": f"{m.get('table_role')}.{m.get('canonical_column')}",
          "Transform": [str(m.get("transform"))], "Confidence": float(m.get("confidence") or 0), "Raw → canonical sample": _sample(m),
          "Samples": len(m.get("sample_records") or [])} for m in vm],
        hide_index=True, width="stretch", height=common.table_height(len(vm)), key="data_variant",
        column_config={
            "Transform": st.column_config.MultiselectColumn("Transform", options=tr_opts, color=["gray"] * len(tr_opts)),
            "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0.0, max_value=1.0, format="%.2f"),
            "Raw → canonical sample": st.column_config.TextColumn("Raw → canonical sample", width="large"),
            "Samples": st.column_config.NumberColumn("Samples", help="attested sample records behind the mapping (the brief asks for five)"),
        })
    _samples(vm)
    _review(mapping)
    if src != CHECKED_IN_SRC:
        design.html_block(design.section("", "Mapping decision", note=f"a signed-in {' or '.join(approvals_db.ROLE_LABELS[r] for r in GATE_ROLES)} accepts or rejects it"))
        _decision(src, mapping, session)

    @st.fragment(run_every=2 if variant_jobs.running() else None)
    def _live() -> None:
        _rehearsal(src, common.approver_session(), variant_jobs.blocked(common.approver_session()))

    _live()


# --------------------------------------------------------------------------- #
# Schema mapping (C13): bank schemas, or the CSV variant as a sub-view        #
# --------------------------------------------------------------------------- #
if view == "map":
    sub_view = st.segmented_control("Mapping view", MAP_VIEWS, default=MAP_VIEWS[0], key="data_map_view", required=True,
                                    label_visibility="collapsed") or MAP_VIEWS[0]
    if sub_view == MAP_VIEWS[0]:
        design.html_block(design.section("", "Bank schemas onto the fraud ontology", note="comment-blind AST mapping of each bank's GSQL DDL"))
        options = [*stems, CUSTOM]
        choice = st.segmented_control("Schema", options, default=options[0], key="data_schema", required=True,
                                      format_func=lambda s: "Custom DDL" if s == CUSTOM else md(s)) or options[0]
        analysis, names, label, text = None, None, "", ""
        if choice == CUSTOM:
            with st.expander("Custom DDL · upload or paste GSQL", expanded=True, key="dq-custom"):
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
                    st.caption("No DDL given yet.")
        else:
            analysis, names, label, err = presets.get(choice, (None, None, "", f"unknown preset {choice}"))
            if err:
                st.error(md(err))
            if analysis is None and not err:
                _missing(f"{choice}: not available in this deployment ({analyst_err or gsql_p['error']}).")

        if analysis is not None:
            maps = [m for m in analysis.get("mappings") or [] if isinstance(m, dict)]
            checks = [_verify(m, names) for m in maps]
            by_status = {s: sum(1 for m in maps if m.get("status") == s) for s in ("exact", "approximate", "unmapped")}
            n_checked = sum(1 for s, _ in checks if s != "—")
            n_yes = sum(1 for s, _ in checks if s == "yes")
            ev = analysis.get("benchmark_eval") or {}
            m_as_of = _gen(gsql_p) if label.startswith("snapshot") else "computed on this page"
            with st.container(border=True, key="strip-dq-map"):
                mc = st.columns(4)
            common.measured("Vertices · edges parsed", f"{analysis.get('n_vertices')} · {analysis.get('n_edges')}", source=label, as_of=m_as_of, container=mc[0],
                            how=f"graph {analysis.get('graph_name')}, {analysis.get('architecture_family')}; analysed by {analysis.get('analyzed_by')}")
            common.measured("Targets mapped, exact · approximate", f"{by_status['exact']} · {by_status['approximate']} of {len(maps)}", source=label, as_of=m_as_of,
                            container=mc[1], how=f"mapping status per canonical target; {by_status['unmapped']} unmapped")
            common.measured("Verified in this DDL", f"{n_yes} of {n_checked}" if names is not None and n_checked else None, source="computed on this page", as_of="computed on this page",
                            container=mc[2], how="every vertex / edge / attribute name in 'mapped to' and 'path' is a name of the parsed DDL")
            common.measured("Self-check vs this file's answer-key comments", f"{ev['status_matches']}/{ev['total_targets']}" if ev.get("total_targets") else None,
                            source=label, as_of=m_as_of, container=mc[3],
                            how="mapping statuses against the comments in the same file (stripped before inference); not an independent accuracy measure")
            st.caption(md(f"{label} · the self-check compares with the file's own comments, so it is not an independent accuracy measure"
                          + ("" if names is not None else " · DDL names not available here, so 'verified in DDL' is not checked")))
            if not analysis.get("n_vertices") and not analysis.get("n_edges"):
                st.warning("No vertex or edge was parsed from this text: any mapping below comes from the analyser's fallback, not from this DDL.")
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
                hide_index=True, width="stretch", height=common.table_height(len(maps)), key="data_mapping",
                column_config={
                    "Canonical target": st.column_config.TextColumn("Canonical target", pinned=True),
                    "Verified in DDL": st.column_config.MultiselectColumn("Verified in DDL", options=list(VERIFIED_COLORS), color=list(VERIFIED_COLORS.values()),
                                                                          help="computed on this page: every vertex / edge / attribute name in 'mapped to' and 'path' must be a name of the parsed DDL; partly = only inside a longer DDL name"),
                    "Rationale": st.column_config.TextColumn("Rationale", width="large"),
                })
            enabled = analysis.get("enabled_detectors") or []
            disabled = analysis.get("disabled_detectors") or []
            st.caption(md("Detectors enabled on this schema: " + (", ".join(map(str, enabled)) or "none")
                          + (" · disabled: " + "; ".join(f"{x.get('detector')} ({x.get('reason')})" for x in disabled if isinstance(x, dict)) if disabled else "")))
            queries = [q for q in analysis.get("adapted_queries") or [] if isinstance(q, dict)]
            if queries:
                with st.expander(f"Adapted detector queries · {len(queries)} · generated, not installed or syntax-checked", key="dq-adapted"):
                    for q in queries:
                        st.markdown(f"**{md(q.get('query_name'))}** · {md(q.get('detector'))} · uses {md(', '.join(map(str, q.get('uses_mappings') or [])))}")
                        st.code(str(q.get("gsql") or ""), language="sql")
    else:
        _csv_variant_view()

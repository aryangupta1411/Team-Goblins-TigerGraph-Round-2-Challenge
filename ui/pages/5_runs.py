"""Runs & consistency: how the selected source was produced (identity and completion), the README check with the numbers
to quote, speed and autonomy for Challenge 9, time by phase, same-case agreement with another source and across every
source, the guardrail summary, the per-case table and the graph hygiene log.

Files only: runs/<source>/run_manifest.json, runs/<source>/<case>/{answer.json, calls.jsonl, phases.jsonl},
cases/HH*-*.json, README.md and runs/*/stale_writebacks.json. Reads and tokens follow common.trace_stats (the answer.json
rule). This file imports no engine, agent, schema_engine or duckdb code and writes nothing.
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

md = common.md
ctx = common.page_setup("runs")
common.page_header("Runs & consistency", "How the selected source was produced, how fast and autonomous it was, and whether the same case gets the same decision.",
                   eyebrow="C9 speed & autonomy · README check · same case run twice")
src = ctx.source
if src is None:
    st.info("No sources on this deployment: no agent run under runs/ and no answer files under cases/.")
    st.stop()
sid = src.id
prov = common.run_provenance(sid)
man = src.manifest if src.kind == "run" else {}
IS_RUN = src.kind == "run"


def _percentiles(values) -> dict | None:
    """{n, p50, p95, min, max} over the non-missing values. p50 is the median (the mean of the two middle values when n is
    even); p95 is nearest-rank: the value at rank ceil(0.95 n) of the sorted values. None when there are no values."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n, mid = len(vals), len(vals) // 2
    return {"n": n, "p50": vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, "p95": vals[max(1, math.ceil(0.95 * n)) - 1],
            "nr50": vals[max(1, math.ceil(0.5 * n)) - 1], "min": vals[0], "max": vals[-1]}


def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def _num(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _order(ids) -> list[str]:
    pack = [r["case_id"] for r in common.pack_for_source(src)]
    ids = set(ids)
    return [c for c in pack if c in ids] + sorted(c for c in ids if c not in pack)


def _answer_path(source, cid: str) -> Path:
    return common.cases_dir() / f"{cid}.json" if source.kind == "submitted" else source.dir / cid / "answer.json"


def _newest(paths) -> str:
    stamps = [s for s in (common.as_of(p) for p in paths) if s != "missing"]
    return max(stamps) if stamps else "missing"


expected = [c for c in man.get("cases") or [] if isinstance(c, str)] if man.get("cases") else [r["case_id"] for r in common.pack_for_source(src)]
ORDER = _order(set(expected) | set(src.cases) | set(src.states))
views = {cid: common.case_view(sid, cid) for cid in ORDER}
done = {cid: v for cid, v in views.items() if v.ok}
stats = {cid: common.trace_stats(v) for cid, v in done.items()}
ANS_SRC = f"cases/{common.SUBMITTED[sid][0]}*.json" if not IS_RUN else f"runs/{sid}/<case>/answer.json"
ANS_AS_OF = _newest(_answer_path(src, c) for c in done)
TRACE_SRC = f"runs/{sid}/<case>/calls.jsonl" if IS_RUN else f"{ANS_SRC} counters (no trace in this source)"
TRACE_AS_OF = _newest(src.dir / c / "calls.jsonl" for c in done) if IS_RUN else ANS_AS_OF
MAN_SRC, MAN_AS_OF = f"runs/{sid}/run_manifest.json", common.as_of(src.dir / "run_manifest.json")

# --------------------------------------------------------------------------- #
# Identity strip and completion row                                           #
# --------------------------------------------------------------------------- #
with st.container(horizontal=True, gap="small", key="runs-identity"):
    if IS_RUN:
        mode = str(prov.get("run_mode") or "unknown")
        common.chip(mode.upper(), "green" if mode == "live" else "gray", help=f"run_mode in {MAN_SRC}")
        common.chip(f"backend {prov.get('llm_backend') or 'unrecorded'}", "gray", help=f"llm_backend in {MAN_SRC}")
        for model, n in Counter(prov.get("models_seen") or {}).most_common():
            common.chip(f"{model} × {n} LLM calls", "orange" if src.model_note else "gray",
                        help=(f"{src.model_note}. " if src.model_note else "") + (f"Manifest note: {man['model_note']}" if man.get("model_note") else "model on kind=='llm' rows of calls.jsonl"))
        for model, n in Counter(prov.get("case_models") or {}).most_common():
            if src.model_note and model not in (prov.get("models_seen") or {}):
                common.chip(f"case_runs record {model} × {n}", "orange", help=src.model_note)
        if not prov.get("models_seen"):
            common.chip("no LLM rows in calls.jsonl", "gray")
        common.chip(f"git {prov.get('git_sha') or 'unrecorded'}", "gray", help=f"git_sha in {MAN_SRC}")
        if prov.get("git_dirty"):
            common.chip("uncommitted changes", "orange", help=f"git_dirty=true in {MAN_SRC}")
        common.chip(f"{src.first_started or '?'} → {src.finished or 'not finished'}", "gray", help="manifest first_started → the latest case_runs[*].finished")
        inv = src.invocations
        resumed = [i for i in inv if i["resume"]]
        common.chip(f"{len(inv)} invocation(s)" + (f" · {len(resumed)} resume, {sum(i['cases'] for i in resumed)} case(s)" if resumed else ""), "gray",
                    help="  \n".join(f"{i['started']} · resume={i['resume']} · {i['cases']} case(s) · {i['run_mode']} · {i['llm_backend']}" for i in inv) or "no invocations recorded")
        top = man.get("prompt_sha256") or {}
        case_runs = man.get("case_runs") if isinstance(man.get("case_runs"), dict) else {}
        hashed = [cr for cr in case_runs.values() if isinstance(cr, dict) and cr.get("prompt_sha256")]
        same = sum(1 for cr in hashed if all(str(top.get(k, "")).startswith(str(v)) for k, v in cr["prompt_sha256"].items()))
        if hashed:
            common.chip(f"prompt hashes consistent {same}/{len(hashed)}", "green" if same == len(hashed) and prov.get("prompt_hashes_consistent") else "orange",
                        help="case_runs[*].prompt_sha256 against the manifest's prompt_sha256, and every invocation's hashes equal")
        common.chip(f"max_tool_calls {src.max_tool_calls if src.max_tool_calls is not None else 'unrecorded'}", "gray", help=f"max_tool_calls in {MAN_SRC}")
    else:
        common.chip("SUBMITTED · answer files", "violet" if src.mode == "promoted" else "gray", help=f"{ANS_SRC}; no trace, run_mode n/a")
        for run, n in Counter(prov.get("promoted_from") or {}).most_common():
            common.chip(f"identical to {run} {n}/{src.n_total}", "gray", help="answer.json parses equal to cases/<id>.json")
for err in prov.get("errors") or []:
    st.warning(md(err))

by_state = {s: [c for c, v in views.items() if v.state == s] for s in common.CASE_STATES}
with st.container(horizontal=True, gap="small", key="runs-completion"):
    common.chip(f"answered {len(done)}/{len(views)}", "green" if views and len(done) == len(views) else "orange",
                help="done = a readable answer.json; running, failed, incomplete, unreadable and not-run cases never count")
    for state in ("running", "failed", "incomplete", "unreadable", "not_run"):
        ids = by_state[state]
        if ids:
            extra = [f"{c} ({views[c].last_phase or 'no phase yet'})" for c in ids] if state == "running" else ids
            common.chip(f"{common.STATE_LABELS[state]} {len(ids)}: {', '.join(extra)}", common.STATE_COLORS[state])
for c in by_state["failed"] + by_state["unreadable"]:
    st.caption(md(f"{c} · {common.STATE_LABELS[views[c].state]} · error verbatim:"))
    st.code(views[c].error or "no error text recorded", language=None, wrap_lines=True)

# --------------------------------------------------------------------------- #
# README check and numbers to quote                                           #
# --------------------------------------------------------------------------- #
lat = _percentiles(s["latency_s"] for s in stats.values())
reads = _percentiles(s["reads"] for s in stats.values())
toks = _percentiles(s["tokens_total"] for s in stats.values())
safe = common.safety({c: v.answer for c, v in done.items()})
routes = Counter(str(x.get("route", "auto")) for v in done.values() for x in _final(v.answer))
gated = [sum(1 for x in _final(v.answer) if str(x.get("route", "auto")) != "auto") for v in done.values()]
gated_d = _percentiles(gated)
sars = sum(1 for v in done.values() if (v.answer.get("sar") or {}).get("file"))
t_all = sum(s["tokens_total"] for s in stats.values())
t_cache = sum(s["tokens_total"] - s["tokens_uncached"] for s in stats.values() if s["tokens_uncached"] is not None)
cache_share = t_cache / t_all if t_all and all(s["tokens_uncached"] is not None for s in stats.values()) else None
mix = safe["verdict_mix"]

st.markdown("#### README check")
left, right = st.columns(2)
with left:
    rf = common.readme_facts()
    if not rf["exists"]:
        st.info(md("README.md is not in this deployment"))
    else:
        st.caption(md(f"README.md · as of {rf['as_of']} · {len(rf['cited_runs'])} cited run folder(s)"))
        for c in rf["cited_runs"]:
            common.chip(f"{c['path']} — {'exists' if c['exists'] else 'missing'}", "green" if c["exists"] else "red",
                        help=f"cited on README.md line(s) {', '.join(map(str, c['lines']))}")
        st.caption("README 'Results' section, verbatim")
        st.text("\n".join(rf["results"]) if rf["results"] else "(no 'Results' heading in README.md)")
with right:
    st.caption(md(f"Numbers to quote, computed now from {sid} ({len(done)} answered)"))
    quote = [
        ("Verdict mix", f"{mix.get('fraud', 0)} fraud · {mix.get('legitimate', 0)} legitimate · {mix.get('uncertain', 0)} uncertain" if done else "not measured here"),
        ("SARs filed", f"{sars}/{len(done)}" if done else "not measured here"),
        ("Uncertain escalated to an analyst", f"{safe['uncertain_escalated']}/{safe['n_uncertain']}" if safe["n_uncertain"] else "no uncertain verdicts"),
        ("Hard actions on non-fraud verdicts", str(len(safe["hard_on_non_fraud"])) if done else "not measured here"),
        ("Latency median / p95", f"{lat['p50']:.1f} s / {lat['p95']:.1f} s (n={lat['n']}, p95 nearest-rank)" if lat else "not measured here"),
        ("Graph reads per case, median", f"{reads['p50']:g} ({reads['min']:g}–{reads['max']:g})" if reads else "not measured here"),
        ("Tokens per case, median incl. cache", f"{toks['p50']:,.0f}" + (f" (cache {cache_share:.0%})" if cache_share is not None else "") if toks else "not measured here"),
        ("Final actions by route", " · ".join(f"{r} {routes.get(r, 0)}" for r in ("auto", "L1", "L2")) if done else "not measured here"),
    ]
    st.dataframe(pd.DataFrame(quote, columns=["Figure", "Value"]), hide_index=True, width="stretch")
    st.code("\n".join([f"Source: {sid} ({ANS_SRC}, as of {ANS_AS_OF})"] + [f"- {k}: {v}" for k, v in quote]), language="markdown")

# --------------------------------------------------------------------------- #
# Speed & autonomy                                                            #
# --------------------------------------------------------------------------- #
st.markdown("#### Speed & autonomy")
matched = [s["matches_answer"] for s in stats.values() if s["matches_answer"] is not None]
match_txt = f"matches answer.json {sum(matched)}/{len(matched)}" if matched else "no trace to check against answer.json"
row1, row2, row3 = st.columns(4), st.columns(4), st.columns(4)
common.measured("Latency p50 (median)", f"{lat['p50']:.1f} s" if lat else None, source=ANS_SRC + " latency_s", as_of=ANS_AS_OF, container=row1[0],
                how=f"median over {lat['n']} answered cases (mean of the two middle values); nearest-rank p50 is {lat['nr50']:.1f} s" if lat else "")
common.measured(f"Latency p95 (nearest-rank, n={lat['n'] if lat else 0})", f"{lat['p95']:.1f} s" if lat else None, source=ANS_SRC + " latency_s", as_of=ANS_AS_OF,
                container=row1[1], how="the value at rank ceil(0.95 n) of the sorted latencies")
common.measured("Latency max", f"{lat['max']:.1f} s" if lat else None, source=ANS_SRC + " latency_s", as_of=ANS_AS_OF, container=row1[2])
llm_reads = _percentiles(s["reads_by_caller"].get("llm", 0) for s in stats.values()) if IS_RUN else None
common.measured("Graph reads per case, median", f"{reads['p50']:g} ({reads['min']:g}–{reads['max']:g})" if reads else None, source=TRACE_SRC, as_of=TRACE_AS_OF,
                container=row1[3], how=("kind=='tool' rows with caller != 'persist' (common.trace_stats); " + match_txt) if IS_RUN else "the answer files' tool_calls counters")
if llm_reads:
    row1[3].caption(md(f"LLM-chosen per case: median {llm_reads['p50']:g}, max {llm_reads['max']:g} · max_tool_calls {src.max_tool_calls if src.max_tool_calls is not None else 'unrecorded'}"))
writes = _percentiles(s["writes"] for s in stats.values()) if IS_RUN else None
common.measured("Write-backs per case, median", f"{writes['p50']:g}" if writes else None, source=f"runs/{sid}/<case>/calls.jsonl (caller persist)" if IS_RUN else "no trace",
                as_of=TRACE_AS_OF, container=row2[0], how=f"{sum(s['writes_ok'] for s in stats.values())}/{sum(s['writes'] for s in stats.values())} persist rows ok" if writes else "")
common.measured("Tokens per case, median incl. cache", f"{toks['p50']:,.0f}" if toks else None, source=TRACE_SRC, as_of=TRACE_AS_OF, container=row2[1],
                how="input + output + cache read + cache write over kind=='llm' rows" if IS_RUN else "the answer files' tokens counters")
if cache_share is not None:
    row2[1].caption(md(f"cache share {cache_share:.0%} of {t_all:,} tokens"))
t_llm = sum(s["llm_latency_s"] for s in stats.values())
t_calls = t_llm + sum(s["tool_latency_s"] + s["write_latency_s"] for s in stats.values())
common.measured("LLM share of call time", f"{t_llm / t_calls:.0%}" if IS_RUN and t_calls else None, source=TRACE_SRC, as_of=TRACE_AS_OF, container=row2[2],
                how=f"{t_llm:.1f} s of LLM calls over {t_calls:.1f} s of recorded LLM + graph call time" if t_calls else "")
common.measured("Final actions by route", " · ".join(f"{r} {routes.get(r, 0)}" for r in ("auto", "L1", "L2")) if done else None, source=ANS_SRC, as_of=ANS_AS_OF,
                container=row2[3], how="next_best_actions.final over answered cases")
if gated_d:
    row2[3].caption(md(f"human approvals needed per case (L1/L2 actions): median {gated_d['p50']:g}, max {gated_d['max']:g}"))
common.measured("Cost", "not computed", source="no checked-in price table", as_of="n/a", what="LLM cost of this source", container=row3[0],
                how="tokens are measured above; no price table is checked in, so no cost is derived from them")

if IS_RUN and done:
    order = sorted(done, key=lambda c: -(stats[c]["latency_s"] or 0))
    rows = []
    for cid in order:
        for c in done[cid].calls:
            kind = "LLM" if c.get("kind") == "llm" else "write-back" if c.get("caller") == "persist" else "graph read"
            rows.append({"case": cid, "phase": str(c.get("phase") or "?").split("-")[0], "component": kind, "seconds": _num(c.get("latency_s")) or 0.0})
    if rows:
        df = pd.DataFrame(rows).groupby(["case", "phase", "component"], as_index=False)["seconds"].sum()
        st.markdown("#### Time by phase")
        bars = alt.Chart(df).mark_bar(stroke="white", strokeWidth=0.5).encode(
            y=alt.Y("case:N", sort=order, title=None), x=alt.X("sum(seconds):Q", title="recorded call seconds (LLM and graph)"),
            color=alt.Color("component:N", scale=alt.Scale(domain=["LLM", "graph read", "write-back"]), title=None),
            order=alt.Order("phase:N"), tooltip=["case", "phase", "component", alt.Tooltip("seconds:Q", format=".1f")])
        layers = [bars]
        if lat:
            layers.append(alt.Chart(pd.DataFrame([{"p95": lat["p95"]}])).mark_rule(strokeDash=[4, 3]).encode(x="p95:Q", tooltip=[alt.Tooltip("p95:Q", title="p95 wall-clock s")]))
        st.altair_chart(alt.layer(*layers), width="stretch")
        st.caption(md(f"{TRACE_SRC} latency_s summed per phase and component, cases sorted by wall-clock latency; dashed rule = p95 wall-clock latency"))

# --------------------------------------------------------------------------- #
# Consistency                                                                 #
# --------------------------------------------------------------------------- #
st.markdown("#### Consistency")
all_ids = [s.id for s in common.sources()]
others = [x for x in all_ids if x != sid]
ref_default = "submitted" if src.round != "round1" else "submitted-r1"
if sid == ref_default or ref_default not in others:
    peers = sorted((s for s in common.sources() if s.kind == "run" and s.round == src.round and s.id != sid), key=lambda s: s.first_started, reverse=True)
    ref_default = peers[0].id if peers else (others[0] if others else None)
if others:
    if st.session_state.get("runs_compare") not in others:
        st.session_state["runs_compare"] = ref_default
    other = st.selectbox("Compare with", others, format_func=common.source_label, key="runs_compare")
    osrc = common.get_source(other)
    agree_rows = []
    for cid in _order(set(ORDER) | set(osrc.cases if osrc else [])):
        a, b = common.case_view(sid, cid), common.case_view(other, cid)
        if not (a.ok and b.ok):
            continue
        g = common.agreement(a.answer, b.answer)
        ca, cb = a.answer.get("case") or {}, b.answer.get("case") or {}
        agree_rows.append({"Open": cid, "class": [g["cls"]], "verdict": f"{ca.get('verdict')} → {cb.get('verdict')}" if ca.get("verdict") != cb.get("verdict") else str(ca.get("verdict")),
                           "same (action, route) set": not any(d.startswith("final actions") for d in g["diffs"]), "same SAR": not any(d.startswith("sar.file") for d in g["diffs"]),
                           "same pattern": not any(d.startswith("pattern") for d in g["diffs"]), "same exposure": not any(d.startswith("exposure_usd") for d in g["diffs"]),
                           "|Δp| ≤ 0.10": g["delta_p"] is not None and g["delta_p"] <= 0.10, "differences": "; ".join(g["diffs"]) or "none", "likely cause": "; ".join(g["cause"]) or "—",
                           "_same": g["same_decision"]})
    k = len(agree_rows)
    if osrc is not None and {src.mode, osrc.mode} & {"mock"} and {src.mode, osrc.mode} - {"mock"}:
        st.warning(md(f"Comparing {src.mode.upper()} with {osrc.mode.upper()}: differences are expected"))
    if k:
        same_n = sum(r["_same"] for r in agree_rows)
        shares = {col: sum(bool(r[col]) for r in agree_rows) for col in ("same (action, route) set", "same SAR", "same pattern", "same exposure", "|Δp| ≤ 0.10")}
        st.markdown(md(f"{same_n}/{k} same decision ({k} shared)") + " · " + " · ".join(md(f"{c} {n}/{k} ({100 * n / k:.0f}% of {k} shared)") for c, n in shares.items()),
                    help=md("same decision = verdict, final (action, route) set and sar.file equal (common.agreement); answers from both sources' answer files"))
        agree_rows.sort(key=lambda r: (r["_same"], ORDER.index(r["Open"]) if r["Open"] in ORDER else 999))
        st.dataframe(pd.DataFrame([{k_: v for k_, v in r.items() if not k_.startswith("_")} for r in agree_rows]), key="runs_agree", hide_index=True, width="stretch",
                     column_config={"Open": st.column_config.ButtonColumn("Open", type="tertiary", pinned=True, key="runs_agree_open", help="Open the Case file in the selected source"),
                                    "class": st.column_config.MultiselectColumn("class", options=list(common.AGREEMENT_COLORS), color=list(common.AGREEMENT_COLORS.values())),
                                    "differences": st.column_config.TextColumn("differences", width="large")})
        click = st.session_state.get("runs_agree_open")
        if click and click.get("label"):
            common.goto("case", case=click["label"])
    else:
        st.info(md(f"No case is answered in both {sid} and {other}"))

same_round = [s for s in common.sources() if s.round == src.round or s.id == sid]
matrix, ref_id = [], "submitted" if src.round != "round1" else "submitted-r1"
for cid in ORDER:
    row, decisions = {"case": cid}, []
    ref_ans = common.case_view(ref_id, cid).answer
    for s in same_round:
        v = common.case_view(s.id, cid)
        if not v.ok:
            row[s.id] = common.STATE_LABELS.get(v.state, v.state)
            continue
        c = v.answer.get("case") or {}
        hard = sorted({str(x.get("action")) for x in _final(v.answer) if x.get("action") in common.HARD_ACTIONS})
        row[s.id] = f"{c.get('verdict')} · {'+'.join(hard) or 'no hard'} · SAR {'yes' if (v.answer.get('sar') or {}).get('file') else 'no'} · p {common.prob(c.get('fraud_probability'))}"
        decisions.append(s.id == ref_id or common.agreement(ref_ans, v.answer)["same_decision"] if ref_ans else True)
    row[f"same decision as {ref_id}"] = f"{sum(decisions)}/{len(decisions)}" if ref_ans else f"no {ref_id} answer"
    row["_all"] = all(decisions)
    matrix.append(row)
if matrix and len(same_round) > 1:
    st.markdown("**Same case across all sources**")
    matrix.sort(key=lambda r: (r["_all"], ORDER.index(r["case"])))
    st.dataframe(pd.DataFrame([{k_: v for k_, v in r.items() if k_ != "_all"} for r in matrix]), hide_index=True, width="stretch")
    st.caption(md(f"verdict · hard actions · SAR · p(fraud) per source ({', '.join(s.id for s in same_round)}); cases where any source differs from {ref_id} first"))

# --------------------------------------------------------------------------- #
# Guardrails, per-case table, raw manifest and hygiene log                    #
# --------------------------------------------------------------------------- #
st.markdown("#### Guardrails in this source")
ledger = common.guardrail_ledger(sid) if IS_RUN else []
if ledger:
    st.dataframe(pd.DataFrame([{"guardrail": r["type"], "count": r["count"], "cases": ", ".join(r["cases"])} for r in ledger]), hide_index=True, width="stretch")
else:
    st.caption("No trace in this source, so no guardrail events")
common.page_link("security", "Security & drills", icon=":material/arrow_forward:")

st.markdown("#### Per case")
table = []
for cid, v in views.items():
    a, s = v.answer or {}, stats.get(cid)
    c = a.get("case") or {}
    if not IS_RUN or not v.ok:
        wb = "none"
    else:
        persist = [x for x in v.calls if x.get("caller") == "persist"]
        bad = sum(1 for x in persist if not x.get("ok"))
        wb = "none" if not persist else "mock session · not written" if src.mode != "live" else f"graph · persist failed {bad}/{len(persist)}" if bad else "graph · persist ok"
    table.append({
        "Open trace": cid, "trigger": v.meta.get("trigger_type", ""), "state": common.STATE_LABELS.get(v.state, v.state), "verdict": c.get("verdict", "") if v.ok else "",
        "p": _num(c.get("fraud_probability")) if v.ok else None,
        "final actions": ", ".join(f"{x.get('action')} {x.get('route', 'auto')}" for x in _final(a)) if v.ok else "",
        "SAR": ("yes" if (a.get("sar") or {}).get("file") else "no") if v.ok else "", "reads": s["reads"] if s else None, "writes": s["writes"] if s else None,
        "LLM calls": s["llm_calls"] if s else None, "tokens": s["tokens_total"] if s else None, "seconds": s["latency_s"] if s else None,
        "guardrails": len(common.guardrail_events(v)) if v.has_trace else 0, "write-back": wb, "error": v.error,
    })
st.dataframe(pd.DataFrame(table), key="runs_table", hide_index=True, width="stretch",
             column_config={"Open trace": st.column_config.ButtonColumn("Open trace", type="tertiary", pinned=True, key="runs_open", help="Open the Case file"),
                            "p": st.column_config.NumberColumn("p", format="%.2f"), "tokens": st.column_config.NumberColumn("tokens", format="localized")})
st.caption(md(f"Rows: {ANS_SRC} and {TRACE_SRC}; reads and tokens by common.trace_stats ({match_txt})"))
opened = st.session_state.get("runs_open")
if opened and opened.get("label"):
    common.goto("case", case=opened["label"])

if IS_RUN:
    with st.expander("Raw run_manifest.json"):
        st.caption(md(f"{MAN_SRC} · as of {MAN_AS_OF}"))
        st.json(man or {"note": "no run_manifest.json in this run folder"}, expanded=False)
with st.expander("Graph hygiene log"):
    hygiene = common.list_maintenance()
    if hygiene:
        st.dataframe(pd.DataFrame([{"folder": h["folder"], "generated_at": h["generated_at"], "run": "applied" if h["applied"] else "dry run",
                                    "counts": ", ".join(f"{k} {v}" for k, v in (h["counts"] or {}).items()), "deleted": "—" if h["deleted"] is None else str(h["deleted"]),
                                    "remaining": "—" if h["remaining"] is None else str(h["remaining"]),
                                    "cases": h["cases"], "error": h["error"]} for h in hygiene]), hide_index=True, width="stretch")
        st.caption(md("runs/*/stale_writebacks.json from ops.clear_case_writebacks; these folders are never a source"))
    else:
        st.caption(md("No runs/*/stale_writebacks.json on this deployment"))

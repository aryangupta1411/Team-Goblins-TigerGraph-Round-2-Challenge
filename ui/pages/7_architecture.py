"""How it works: the TigerGraph-use checklist measured from the selected source, every known limit with its source file,
a measured-facts strip, a map of the pipeline with the page that proves each stage and the LLM models the source's
calls.jsonl records, and how the scorecard decides with every number read from the code.

Files only: the selected source's traces (common.tg_usage, trace_stats, guardrail_events, attest_claims, llm_models),
engine/out/detector_backtest.json, engine/out/containment_summary.json, engine/out/proof_snapshot.json, the
graph/queries/*.gsql listing and common.scorecard_rules (policy/fraud_policy.yaml, engine/detectors.py read with ast and
agent/security.py where they exist, else the snapshot's scorecard section). agent.mcp_client is imported only when
present, to read the tool allowlist. No case data reaches HTML, and this page writes nothing.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

md = common.md
ctx = common.page_setup("system")
common.page_header("How it works", "TigerGraph use measured from the selected source, known limits with their sources, and a map of the pipeline.", eyebrow="System")
src = ctx.source
sid = ctx.source_id or ""
NOT_SHOWN = "not shown in this source"
TRAVERSALS = ("episode_candidates", "shared_origin_scan", "ring_profile")


def _short(name: object) -> str:
    return str(name or "").split(":", 1)[-1]


def _trim(value):
    """params with embedding vectors replaced by '<N-d vector>'."""
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return f"<{len(value)}-d vector>" if len(value) >= 32 and all(isinstance(x, int | float) for x in value) else [_trim(v) for v in value]
    return value


def _call_text(cid: str, c: dict) -> str:
    params = json.dumps(_trim(c.get("params") or {}), default=str)
    return f"{cid} · {c.get('name')}({params[:140] + ('…' if len(params) > 140 else '')}) · {c.get('ts') or 'no ts'}"


def _example(traced: dict, pred) -> str:
    for cid, v in traced.items():
        for c in v.calls:
            if c.get("kind", "tool") == "tool" and pred(c):
                return _call_text(cid, c)
    return "—"


# --------------------------------------------------------------------------- #
# TigerGraph-use checklist                                                    #
# --------------------------------------------------------------------------- #
ids = [r["case_id"] for r in common.pack_for_source(src)] if src else []
ids += [c for c in (src.cases if src else []) if c not in ids]
views = {cid: common.case_view(sid, cid) for cid in ids} if src else {}
done = {c: v for c, v in views.items() if v.ok}
traced = {c: v for c, v in done.items() if v.has_trace}
usage = {c: common.tg_usage(v) for c, v in traced.items()}
n = len(traced)
TRACE_SRC = f"runs/{sid}/<case>/calls.jsonl and phases.jsonl"

with st.container(horizontal=True, vertical_alignment="center", gap="medium", key="system-tg-head"):
    st.markdown("#### TigerGraph use in this source", width="content")
    common.tigergraph_credit("system")
if src is None:
    st.info("No sources on this deployment: no agent run under runs/ and no answer files under cases/.")
rows: list[dict] = []
if src is not None and not traced:
    note = "no trace in this source (answer files only)" if src.kind == "submitted" else "no answered case with a trace yet"
    for crit in ("Vector search in decisions", "Vector search + graph traversal", "Graph algorithm affecting output", "Write-back and read-back",
                 "Restricted MCP tools", "Graph as source of truth"):
        rows.append({"criterion": crit, "measured in this source": note, "example call": "—", "status": "not measured here"})
elif traced:
    vec = [c for c, u in usage.items() if u["vector_hits"]]
    grd = [c for c, u in usage.items() if u["grounding"]]
    dists = [float(h["distance"]) for u in usage.values() for h in u["vector_hits"] if isinstance(h.get("distance"), int | float)]
    rows.append({"criterion": "Vector search in decisions",
                 "measured in this source": f"P1 similar_prior_cases in {len(vec)}/{n} traces · grounding chunks in {len(grd)}/{n}"
                 + (f" · nearest distance {min(dists):.3f}" if dists else ""),
                 "example call": _example(traced, lambda c: "similar_prior_cases" in str(c.get("name"))), "status": "shown" if vec else NOT_SHOWN})
    trav = Counter(_short(c.get("name")) for v in traced.values() for c in v.calls if _short(c.get("name")) in TRAVERSALS)
    trav_cases = [c for c, v in traced.items() if any(_short(x.get("name")) in TRAVERSALS for x in v.calls)]
    rows.append({"criterion": "Vector search + graph traversal",
                 "measured in this source": (" · ".join(f"{k} {m}" for k, m in trav.most_common()) + f" calls in {len(trav_cases)}/{n} traces") if trav else "no traversal call",
                 "example call": _example(traced, lambda c: _short(c.get("name")) in TRAVERSALS), "status": "shown" if trav else NOT_SHOWN})
    algo = [c for c, u in usage.items() if u["algo_effect"]]
    algo_adj = next((str(a) for c in algo for a in common.phase_payload(traced[c], "P3").get("adjustments") or []), "—")
    rows.append({"criterion": "Graph algorithm affecting output", "measured in this source": f"a P3 adjustment names wcc / degree / pagerank / ppr in {', '.join(algo)}" if algo else NOT_SHOWN,
                 "example call": f"P3 adjustment {algo_adj}" if algo else "—", "status": "shown" if algo else NOT_SHOWN})
    persist = [p for u in usage.values() for p in u["persist"]]
    ok = sum(1 for p in persist if p["ok"])
    back = {c: [h["id"] for h in u["agent_readbacks"]] for c, u in usage.items() if u["agent_readbacks"]}
    written = "persisted to FraudGraph" if src.mode == "live" else "mock session, nothing written to a graph"
    rows.append({"criterion": "Write-back and read-back",
                 "measured in this source": f"persist rows {ok} ok · {len(persist) - ok} failed ({written}) · "
                 + (f"{sum(map(len, back.values()))} agent-case read-back(s) in later P1 rows ({', '.join(back)})" if back else "no agent-case read-back"),
                 "example call": _example(traced, lambda c: c.get("caller") == "persist"), "status": "shown" if ok and back and src.mode == "live" else "partial" if ok else NOT_SHOWN})
    mcp, why = common.optional_import("agent.mcp_client")
    allow, allow_note = None, why or ""
    if mcp is not None:
        try:
            allow = mcp.read_allowlist()
        except (OSError, ValueError, AttributeError) as exc:
            allow_note = f"allowlist unreadable: {type(exc).__name__}: {exc}"
    write_names = {_short(p["name"]) for p in persist} | {_short(x) for x in (allow or {}).get("write", [])}
    llm_calls = [c for v in traced.values() for c in v.calls if c.get("kind", "tool") == "tool" and c.get("caller") == "llm"]
    not_query = [c for c in llm_calls if not str(c.get("name") or "").startswith("query:")]
    llm_writes = [c for c in llm_calls if _short(c.get("name")) in write_names]
    allow_txt = (f"allowlist {len(allow.get('read', []))} read · {len(allow.get('write', []))} write · {len(allow.get('blocked', []))} blocked (agent.mcp_client)"
                 if allow else f"allowlist not read here ({allow_note})")
    rows.append({"criterion": "Restricted MCP tools",
                 "measured in this source": f"{len(llm_calls)} LLM-chosen calls, " + ("all installed 'query:*' reads" if not not_query else f"{len(not_query)} not 'query:*'")
                 + f" · {len(llm_writes)} writes by the LLM · {allow_txt}",
                 "example call": _example(traced, lambda c: c.get("caller") == "llm"), "status": "shown" if llm_calls and not not_query and not llm_writes else "check"})
    claims = [(c, x) for c, v in traced.items() for x in common.attest_claims(v)]
    matched = [(c, x) for c, x in claims if x["call"]]
    dropped = sum(len(e.get("dropped") or []) for v in traced.values() for e in common.guardrail_events(v) if e["type"] == "dropped_ids")
    ex = matched[0] if matched else None
    rows.append({"criterion": "Graph as source of truth",
                 "measured in this source": f"claims whose ref matches a recorded graph call {len(matched)}/{len(claims)} · unknown ids dropped {dropped}",
                 "example call": f"{ex[0]} · {ex[1]['call']['name']} · {ex[1]['call']['ts']}" if ex else "—", "status": "shown" if matched else NOT_SHOWN})
if src is not None:
    prov = common.run_provenance(sid)
    live = src.kind == "run" and prov.get("run_mode") == "live"
    rows.append({"criterion": "A live run", "measured in this source": f"{sid} · run_mode {prov.get('run_mode')} · {src.n_answered}/{src.n_total} answered · finished {src.finished or 'not finished'}"
                 if src.kind == "run" else f"{sid} holds answer files, not a run", "example call": "—", "status": "shown" if live and src.complete else NOT_SHOWN})
if rows:
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"measured in this source": st.column_config.TextColumn(width="large"), "example call": st.column_config.TextColumn(width="large")})
    st.caption(md(f"Computed now from {TRACE_SRC if traced else sid} ({n} traced case(s)) with common.tg_usage, attest_claims and guardrail_events"))

# --------------------------------------------------------------------------- #
# Known limits                                                                #
# --------------------------------------------------------------------------- #
limits = common.known_limits()
with st.container(border=True, key="system-limits"):
    st.markdown(f"**Known limits** · {len(limits)} computed from files")
    for item in limits:
        st.markdown(f"**{md(item['title'])}** · {md(item['detail'])} · :gray[{md(item['source'])} · as of {md(item['as_of'])}]")
    if not limits:
        st.caption("No limit computed from the files on this deployment")

# --------------------------------------------------------------------------- #
# Measured facts strip                                                        #
# --------------------------------------------------------------------------- #
st.markdown("#### Measured facts")
facts, attacks, bt, cs, c3 = (common.proof(x) for x in ("facts", "attacks", "backtest", "containment_summary", "c3_built"))
fd = facts["data"] if isinstance(facts["data"], dict) else {}


def _count(*keys: str) -> int | None:
    for key in keys:
        v = fd.get(key)
        v = v.get("count", v.get("n")) if isinstance(v, dict) else v
        if isinstance(v, int | float) and not isinstance(v, bool):
            return int(v)
    return None


def _src(p: dict, fallback: str) -> tuple[str, str]:
    return (p["source"] or f"{fallback} ({p['error'] or 'missing'})"), (p["as_of"] or "missing")


txc, ccs = _count("txc", "transactions"), _count("cc", "closed_cases")
dets = (bt["data"] or {}).get("detectors") if isinstance(bt["data"], dict) else None
ad = attacks["data"] if isinstance(attacks["data"], dict) else {}
csd = cs["data"] if isinstance(cs["data"], dict) else None
qdir = common.ROOT / "graph" / "queries"
qfiles = sorted(f for f in qdir.glob("*.gsql") if f.stem != "install_all") if qdir.is_dir() else []
cols = st.columns(6)
common.measured("Transactions / closed cases", f"{txc:,} / {ccs:,}" if txc is not None and ccs is not None else None, container=cols[0],
                source=_src(facts, "engine/out/proof_snapshot.json facts")[0], as_of=_src(facts, "")[1], how="read-only DuckDB counts in the proof snapshot")
common.measured("Detectors (cleared-customer hits)", f"{len(dets)} ({sum(len(d.get('legitimate_customers_acted_against') or []) for d in dets)})" if dets else None,
                container=cols[1], source=_src(bt, "engine/out/detector_backtest.json")[0], as_of=_src(bt, "")[1],
                how="detectors in the backtest; in brackets, cleared customers inside their match sets")
common.measured("Attacks blocked", f"{ad['attacks_blocked']}/{ad['total_attacks']}" if {"attacks_blocked", "total_attacks"} <= ad.keys() else None, container=cols[2],
                source=_src(attacks, "engine/out/proof_snapshot.json attacks")[0], as_of=_src(attacks, "")[1], how="agent.security attack ablation recorded in the snapshot")
waves = sorted({str(v.get("wave")) for v in (csd or {}).values() if isinstance(v, dict) and v.get("wave")})
common.measured("Containment waves", f"{len(waves)} over {len(csd)} case(s)" if csd else None, container=cols[3],
                source=_src(cs, "engine/out/containment_summary.json")[0], as_of=_src(cs, "")[1], how="distinct detector waves with a targeted containment plan")
common.measured("C3 policy-rule compiler", {True: "built", False: "not built"}.get(c3["data"]) if isinstance(c3["data"], bool) else None, container=cols[4],
                source=_src(c3, "engine/out/proof_snapshot.json c3_built")[0], as_of=_src(c3, "")[1])
common.measured("GSQL query files in repo", len(qfiles) if qfiles else None, container=cols[5], source="graph/queries/*.gsql (install_all excluded)", as_of=common.as_of(qdir),
                how="files in the repository, not a count of queries installed on the graph")

# --------------------------------------------------------------------------- #
# System map                                                                  #
# --------------------------------------------------------------------------- #
STAGES = [
    ("Trigger", "the case pack row and its untrusted trigger text", "case"),
    ("P0 intake / sanitiser", "injection markers found and neutralised before any model sees the text", "security"),
    ("P1 memory (TigerVector)", "similar prior cases and policy chunks from vector search", "case"),
    ("P2 investigate (MCP reads)", "the installed queries the LLM chose, with params and timestamps", "case"),
    ("P3 engine scorecard + detectors", "the deterministic probability and the detector waves", "detectors"),
    ("P4 assess", "the verdict, its evidence and the claims tied to graph calls", "case"),
    ("P5-P7 policy gate + evidence request", "actions, their L1 / L2 routes and the simulated customer reply", "approvals"),
    ("P8 SAR", "the SAR draft and its validator checks", "case"),
    ("P9 explain / validate", "the explanation, its guardrail notes and the same-case agreement", "runs"),
    ("P10 persist + approvals", "the AgentCase write-back, its read-back and the approval queue", "graph"),
]
st.markdown("#### System map")
# the LLM node names the models this source's calls.jsonl records (P2, P4, P5-P7, P8 and P9 call it), never an assumed one
models = common.llm_models(traced.values())
llm_phases = Counter(str(c.get("phase") or "?").split("-")[0] for v in traced.values() for c in v.calls if c.get("kind") == "llm")


def _dot(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


chain = [s for s, _, _ in STAGES]
dot = ["digraph pipeline {", 'rankdir=LR; node [shape=box, style="rounded", fontname="Helvetica", fontsize=11]; edge [color="#888888"];']
dot += [f'n{i} [label="{name}"];' for i, name in enumerate(chain)]
dot += [f"n{i} -> n{i + 1};" for i in range(len(chain) - 1)]
dot += ['tg [label="TigerGraph FraudGraph", shape=cylinder];', 'n2 -> tg [dir=both, style=dashed];', 'n3 -> tg [dir=both, style=dashed];', 'n9 -> tg [style=dashed];']
dot += [f'llm [label="{_dot("LLM · " + ", ".join(models) if models else "LLM (model not recorded in this source)")}", shape=component];',
        "n3 -> llm [dir=both, style=dashed];", "n5 -> llm [style=dashed];", "n6 -> llm [style=dashed];", "n7 -> llm [style=dashed];", "n8 -> llm [style=dashed];", "}"]
st.graphviz_chart("\n".join(dot), width="stretch")
if models:
    st.caption(md(f"LLM calls in runs/{sid}/<case>/calls.jsonl: " + " · ".join(f"{m} {k}" for m, k in models.most_common()) + " · by phase "
                  + ", ".join(f"{p} {k}" for p, k in sorted(llm_phases.items(), key=lambda x: common.PIPELINE_CODES.index(x[0]) if x[0] in common.PIPELINE_CODES else 99))
                  + f" · computed now from {n} traced case(s); agent/llm.py picks the backend from LLM_BACKEND"))
else:
    st.caption(md(f"No LLM row in {sid or 'this source'}: the model is recorded only in a run's calls.jsonl"))
with st.container(border=True, key="system-stages"):
    for name, proves, page in STAGES:
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"**{md(name)}** · {md(proves)}")
            common.page_link(page, common.PAGES[page]["title"], icon=":material/arrow_forward:")

# --------------------------------------------------------------------------- #
# How the scorecard decides: every number read from the code                  #
# --------------------------------------------------------------------------- #
st.markdown("#### How the scorecard decides")
rules = common.scorecard_rules()
rd = rules["data"] if isinstance(rules["data"], dict) else None
if rd is None:
    st.info(md(f"The scorecard's parameters are not readable on this deployment: {rules['error'] or 'no source'}"), icon=":material/info:")
else:
    E = rd.get("engine") if isinstance(rd.get("engine"), dict) else {}
    PE = E.get("post_evidence") if isinstance(E.get("post_evidence"), dict) else {}

    def _v(d: dict, key: str) -> str:
        x = d.get(key)
        if x is None:
            return "not set"
        return f"[{x[0]}, {x[1]}]" if isinstance(x, list | tuple) and len(x) == 2 else str(x)

    left, right = st.columns([1, 1], gap="medium")
    with left, st.container(border=True, key="system-scorecard-steps"):
        st.markdown("**From the scorer to a verdict** (P3, then P4 and P7)")
        for title, body in (
            ("Scorer.", f"cms_p from the ETL scorer (etl/cms_train.py) becomes cal_p through the isotonic calibration in {rd.get('calibration_file') or 'the calibration file'}."),
            ("Evidence.", f"Graph signals add log-odds adjustments; their sum is clipped to ±{_v(E, 'max_abs_adjustment')}: p1 = expit(logit(cal_p) + clip(sum)). "
                          f"A scorer flagged unreliable is shrunk toward 0.5 by {_v(E, 'scorer_unreliable_shrink')}."),
            ("Conflict.", f"When the scorer (cms_p below {_v(E, 'conflict_cms')}) and a rulebook chain over ${_v(E, 'conflict_min_exposure')} disagree, p is clamped "
                          f"into {_v(E, 'conflict_clamp')}, inside the uncertain band. Final Round only, a second override clamps into the same band with no "
                          "scorer or exposure test: an in-person risk-score alert with no detector hit whose card shares a recipient-email domain with other "
                          "cards that no fraud case touches, or has an online ProductCD 'C' purchase with no device in its 48-hour chain."),
            ("Floors.", "A matched pattern lifts p to at least its floor; one floor applies, the first that matches (table)."),
            ("Bands.", f"fraud at p ≥ {_v(E, 'fraud_band')}, legitimate at p ≤ {_v(E, 'legit_band')}, uncertain between. An evidence family counts for fraud or "
                       f"legitimacy when its net weight reaches ±{_v(E, 'family_strength')}."),
            ("LLM bound (P4).", f"The model may move p by at most ±{rd.get('llm_max_move', 'not set')} (agent/security.py clamp_to_engine)."),
            ("After an evidence request (P6 → P7).", f"confirm: p capped at {_v(PE, 'confirm_ceiling')}, unless a ring, burst or detector floor holds; deny: "
             f"{_v(PE, 'deny_bump')} log-odds, floor {_v(PE, 'deny_floor_with_second_family')} with another fraud family; step-up passed: {_v(PE, 'pass_bump')} "
             f"log-odds, then capped at {_v(PE, 'pass_ceiling')} unless a ring, burst or detector floor holds (that floor is kept); "
             f"failed: {_v(PE, 'fail_bump')} log-odds."),
        ):
            st.markdown(f"**{md(title)}** {md(body)}")
    with right:
        bumps = [("card_testing_bump", "R5 card-testing run feeding the flagged purchase"),
                 ("recurring_bump", "R7 recurring-charge match (never with a Final Round detector hit, which clears the match)"),
                 ("cleared_precedent_bump", "a prior cleared alert of the same template on the card (risk alerts)"),
                 ("recent_case_bump", f"a recent confirmed-fraud card-testing / CNP case on the card (within {_v(E, 'recent_case_days')} days)"),
                 ("shared_profile_bump", "a strong shared profile with ≥ 2 other fraud cards, seen from 30 days before the flagged transaction up "
                                         "to the alert, and no ring hit"),
                 ("denial_bump_when_scorer_agrees", "a customer denial with cal_p ≥ 0.5 and no R7 recurring-charge match")]
        st.markdown("**Log-odds adjustments** " + md(f"({rd.get('policy_file')}, engine)"))
        st.dataframe(pd.DataFrame([{"parameter": k, "value": _v(E, k), "applied when": w} for k, w in bumps]), hide_index=True, width="stretch",
                     column_config={"applied when": st.column_config.TextColumn(width="large")})
        floors = [("ring_floor", "anonymous-proxy device ring", rd.get("policy_file")), ("burst_with_denial_floor", "under-$500 burst with a customer denial", rd.get("policy_file")),
                  ("burst_alone_floor", "under-$500 burst without a denial", rd.get("policy_file")), ("denial_floor", "customer denial not explained by R7", rd.get("policy_file"))]
        dets = [d for d in (rd.get("detector_floors") if isinstance(rd.get("detector_floors"), list) else []) if isinstance(d, dict)]
        ordered = bool(dets) and all("in_scan" in d for d in dets)  # older snapshots carry no hit_from_scan order
        unused = [str(d.get("detector_id") or d.get("function")) for d in dets if ordered and not d.get("in_scan")]
        rows_f = [{"floor": k, "value": _v(E, k), "when": w, "source": s} for k, w, s in floors[:1]]
        rows_f += [{"floor": d.get("detector_id") or d.get("function"), "value": str(d.get("p_floor", "not a literal")) + (" (default)" if d.get("p_floor_default") else ""),
                    "when": f"detector hit{(': ' + str(d['name'])) if d.get('name') else ''}", "source": f"{rd.get('detectors_file')}:{d.get('line')}"}
                   for d in dets if not ordered or d.get("in_scan")]
        rows_f += [{"floor": k, "value": _v(E, k), "when": w, "source": s} for k, w, s in floors[1:]]
        st.markdown("**Floors** " + md("(checked in this order; the first that matches applies)" if ordered
                                       else "(the detector rows' order is not recorded in this source; the first floor that matches applies)" if dets
                                       else "(the first floor that matches applies)"))
        st.dataframe(pd.DataFrame(rows_f), hide_index=True, width="stretch", column_config={"when": st.column_config.TextColumn(width="large")})
        if unused:
            st.caption(md(f"Defined in {rd.get('detectors_file')} but not tried by hit_from_scan, so they never floor p: {', '.join(unused)}"))
        if not dets:
            st.caption("The detector floors are not readable in this source (detector_floors missing or not a list), so they are not in the table")
    how = (f"read now from {rules['source']} (policy file as of {rules['as_of']})" if rules["mode"] == "code"
           else f"from {rules['source']} (snapshot generated {rules['generated_at'] or rules['as_of']}, git {rules['git_sha'] or 'unknown'})")
    st.caption(md(f"Every number is {how}. The 'applied when' and 'when' texts summarise the conditions in engine/scorecard.py. Two rules are inline "
                  "in engine/scorecard.py, not parameters, and their thresholds are not listed: the R5 card-testing floor (between the bursts and the "
                  "customer denial) and the Final Round mixed-channel / recipient-email conflict override."
                  + (f" Notes: {rules['error']}" if rules["error"] else "")))

with st.expander("Design notes", expanded=False):
    for text, page in (
        ("Intake: trigger text is treated as data; the sanitiser records what it removed", "security"),
        ("Memory: TigerVector search over closed and agent cases, timeboxed to the case's opened_at", "case"),
        ("Investigation: the model picks installed read queries by name; it never gets a write tool", "case"),
        ("Engine: a deterministic scorecard and detectors bound the model's probability", "detectors"),
        ("Policy: hard actions wait for an L1 or L2 human; approvals fail closed without principals", "approvals"),
        ("Data: schema drift and data quality are gated before new data reaches the engine", "data"),
        ("Runs: every run keeps its manifest, trace and answer so any case can be replayed and compared", "runs"),
    ):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(md(text))
            common.page_link(page, common.PAGES[page]["title"], icon=":material/arrow_forward:")

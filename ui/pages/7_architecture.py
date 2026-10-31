"""How it works: the TigerGraph-use checklist measured from the selected source, every known limit with its source file,
a measured-facts strip and a map of the pipeline with the page that proves each stage.

Files only: the selected source's traces (common.tg_usage, trace_stats, guardrail_events, attest_claims),
engine/out/detector_backtest.json, engine/out/containment_summary.json, engine/out/proof_snapshot.json and the
graph/queries/*.gsql listing. agent.mcp_client is imported only when present, to read the tool allowlist. No case data
reaches HTML, and this page writes nothing.
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

st.markdown("#### TigerGraph use in this source")
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
chain = [s for s, _, _ in STAGES]
dot = ["digraph pipeline {", 'rankdir=LR; node [shape=box, style="rounded", fontname="Helvetica", fontsize=11]; edge [color="#888888"];']
dot += [f'n{i} [label="{name}"];' for i, name in enumerate(chain)]
dot += [f"n{i} -> n{i + 1};" for i in range(len(chain) - 1)]
dot += ['tg [label="TigerGraph FraudGraph", shape=cylinder];', 'n2 -> tg [dir=both, style=dashed];', 'n3 -> tg [dir=both, style=dashed];', 'n9 -> tg [style=dashed];', "}"]
st.graphviz_chart("\n".join(dot), width="stretch")
with st.container(border=True, key="system-stages"):
    for name, proves, page in STAGES:
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"**{md(name)}** · {md(proves)}")
            common.page_link(page, common.PAGES[page]["title"], icon=":material/arrow_forward:")

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

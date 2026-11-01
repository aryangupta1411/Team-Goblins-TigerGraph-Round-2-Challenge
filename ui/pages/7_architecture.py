"""How it works: how the agent uses TigerGraph, and what it does not do, measured from the selected source.

First screen: the S0-P10 pipeline (common.PIPELINE) as a numbered chain with a graph lane and an LLM lane counted per
phase from the source's calls.jsonl (the model only as those rows record it), each stage linked to the page that proves
it, and beside it the TigerGraph-use checklist as status rows. Below: the same counts as a call graph (folded), how the
scorecard decides with every number read from the code (tables folded) and the known limits grouped by theme.

Files only: the selected source's traces, one attempt per case (common.answer_attempt_view, tg_usage, attest_claims,
guardrail_events, llm_models), common.known_limits and common.scorecard_rules (policy/fraud_policy.yaml,
engine/detectors.py read with ast and agent/security.py where they exist, else the snapshot's scorecard section).
agent.mcp_client is imported only when present, to read the tool allowlist. Every value that reaches HTML goes through
design.esc(), and this page writes nothing.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common, design  # noqa: E402

md, esc = common.md, design.esc
ctx = common.page_setup("system")
src = ctx.source
sid = ctx.source_id or ""
NOT_SHOWN = "not shown in this source"
TRAVERSALS = ("episode_candidates", "shared_origin_scan", "ring_profile", "typology_scan")
# the page that proves each stage (a common.PAGES key)
PROOF_PAGE = {"S0": "data", "P0": "security", "P1": "case", "P2": "case", "P3": "detectors", "P4": "case", "P5": "approvals",
              "P6": "approvals", "P7": "approvals", "P8": "case", "P9": "runs", "P10": "graph"}
STATUS_KIND = {"shown": "green", "partial": "ochre", "check": "ochre"}
STATUS_TEXT = {NOT_SHOWN: "not shown"}
# known limits by theme, matched on the limit's title (common.known_limits computes them from files)
THEMES = (("Answers", re.compile(r"verdict mix|accuracy", re.I)),
          ("Detectors and the engine", re.compile(r"^D\d|^C[23]\b|detector|graph algorithm|analyst feedback|engine", re.I)),
          ("Gates: schema and approvals", re.compile(r"schema|approv", re.I)),
          ("Runs and records", re.compile(r"\brun|readme|git|model", re.I)))


def _phase(c: dict) -> str:
    return str(c.get("phase") or "?").split("-")[0]


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
views = {cid: common.answer_attempt_view(common.case_view(sid, cid)) for cid in ids} if src else {}  # one attempt per case
done = {c: v for c, v in views.items() if v.ok}
traced = {c: v for c, v in done.items() if v.has_trace}
usage = {c: common.tg_usage(v) for c, v in traced.items()}
n = len(traced)
TRACE_SRC = f"runs/{sid}/<case>/calls.jsonl and phases.jsonl"
live_graph = src is not None and src.mode == "live"

if src is None:
    st.info("No sources on this deployment: no agent run under runs/ and no answer files under cases/.")
rows: list[dict] = []
llm_calls: list[dict] = []
not_query: list[dict] = []
llm_writes: list[dict] = []
if src is not None and not traced:
    note = "no trace in this source (answer files only)" if src.kind == "submitted" else "no answered case with a trace yet"
    for crit in ("Vector search in decisions", "Vector search + graph traversal", "Graph algorithm affecting output",
                 "Detectors run in the graph (typology_scan in P3)", "Write-back and read-back", "Restricted MCP tools", "Graph as source of truth"):
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
    scans = [(c, x) for c, v in traced.items() for x in v.calls if x.get("kind", "tool") == "tool" and _short(x.get("name")) == "typology_scan"]
    in_p3 = sum(1 for _, x in scans if _phase(x) == "P3")
    rows.append({"criterion": "Detectors run in the graph (typology_scan in P3)",
                 "measured in this source": (f"typology_scan {in_p3} call(s) in P3" + (f", {len(scans) - in_p3} in other phases" if len(scans) > in_p3 else "")
                                             + f" · in {len({c for c, _ in scans})}/{n} traces") if scans else "no typology_scan call",
                 "example call": _example(traced, lambda c: _short(c.get("name")) == "typology_scan"), "status": "shown" if in_p3 else NOT_SHOWN})
    persist =[p for u in usage.values() for p in u["persist"]]
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
# --------------------------------------------------------------------------- #
# Calls per phase, from calls.jsonl                                           #
# --------------------------------------------------------------------------- #
lanes = {s["code"]: {"reads": 0, "writes": 0, "llm": 0, "picked": 0, "scan": 0, "names": Counter(), "models": Counter()} for s in common.PIPELINE}
unplaced = 0
for v in traced.values():
    for c in v.calls:
        lane = lanes.get(_phase(c))
        if lane is None:
            unplaced += 1
        elif c.get("kind") == "llm":
            lane["llm"] += 1
            if c.get("model"):
                lane["models"][str(c["model"])] += 1
        elif c.get("kind", "tool") == "tool":
            lane["writes" if c.get("caller") == "persist" else "reads"] += 1
            lane["picked"] += c.get("caller") == "llm"
            lane["scan"] += _short(c.get("name")) == "typology_scan"
            lane["names"][_short(c.get("name"))] += 1
models = common.llm_models(traced.values())
calls_as_of = max((common.as_of(v.dir / "calls.jsonl") for v in traced.values() if v.dir is not None), default="missing")
GRAPH_LANE = "TigerGraph" if live_graph else "Graph (mock)"
LLM_LANE = ("LLM · " + ", ".join(models)) if models else "LLM"


def _total(key: str) -> int:
    return sum(x[key] for x in lanes.values())


def _codes(key: str) -> str:
    return ", ".join(k for k, x in lanes.items() if x[key]) or "no phase"


def _lead() -> str:
    if not traced:
        why = "holds answer files without a trace" if src is not None and src.kind == "submitted" else "has no answered case with a trace yet"
        return f'<p class="tc tc-lead">{esc(sid or "This deployment")} {why}, so the graph and LLM calls per phase are <b>not measured here</b>.</p>'
    graph = ", ".join(k for k, x in lanes.items() if x["reads"] or x["writes"]) or "no phase"
    where = "TigerGraph" if live_graph else "the mock graph (DuckDB facts, not TigerGraph)"
    picked = _total("picked")
    wrote = (f'<span class="red">{len(llm_writes)} graph write(s) it chose</span>' if llm_writes else '<span class="green">no graph write</span>')
    return (f'<p class="tc tc-lead">Over {n} answered case(s) the agent called {esc(where)} in <b>{esc(graph)}</b>: <b>{_total("reads"):,} reads</b> '
            f'and <b>{_total("writes"):,} writes</b>, every write from its own persist step ({esc(_codes("writes"))}). The LLM made '
            f'<b>{_total("llm"):,} calls</b> in {esc(_codes("llm"))}; it picked {picked:,} of the reads'
            f'{", all installed queries," if picked and not not_query else ""} and made {wrote}.</p>')


def _lane_graph(code: str) -> str:
    x = lanes[code]
    if code == "S0" or not traced:
        return '<span class="sy-dash" title="no trace row">—</span>'
    if not (x["reads"] or x["writes"]):
        return '<span class="sy-zero">0</span>'
    parts = [f"<b>{x['reads']:,}</b> reads"] * bool(x["reads"]) + [f"<b>{x['writes']:,}</b> writes"] * bool(x["writes"])
    subs = [f"{x['picked']:,} picked by the LLM"] * bool(x["picked"]) + [f"{x['scan']:,} typology_scan (the detectors)"] * bool(x["scan"])
    names = " · ".join(f"{k} {m}" for k, m in x["names"].most_common())
    sub = f'<span class="sub">{esc(" · ".join(subs))}</span>' if subs else ""
    return f'<span title="{esc(names)}">{" · ".join(parts)}</span>{sub}'


def _lane_llm(code: str) -> str:
    x = lanes[code]
    if code == "S0" or not traced:
        return '<span class="sy-dash" title="no trace row">—</span>'
    if not x["llm"]:
        return '<span class="sy-zero">0</span>'
    sub = f'<span class="sub">{esc(" · ".join(f"{m} {k}" for m, k in x["models"].most_common()))}</span>' if len(models) > 1 else ""
    return f"<b>{x['llm']:,}</b> calls{sub}"


def _chain() -> str:
    head = (f'<li class="sy-row sy-head"><span></span><span>Stage</span><span>{esc(GRAPH_LANE)}</span><span>{esc(LLM_LANE)}</span>'
            '<span>Proved on</span></li>')
    out = [head]
    for s in common.PIPELINE:
        code, page = s["code"], PROOF_PAGE.get(s["code"], "case")
        href = common.PAGES[page]["url_path"] + (f"?run={quote(sid)}" if sid else "")
        out.append(f'<li class="sy-row{" off" if code == "S0" else ""}"><span class="sy-n">{esc(code)}</span>'
                   f'<span class="sy-stage"><b>{esc(s["label"])}</b><span class="sub">{esc(s["module"])}</span></span>'
                   f'<span class="sy-lane">{_lane_graph(code)}</span><span class="sy-lane">{_lane_llm(code)}</span>'
                   f'<a class="sy-proof" href="{esc(href)}" target="_self">{esc(common.PAGES[page]["title"])}</a></li>')
    return f'<div class="tc">{design.section("", "Pipeline", "S0–P10, calls per phase")}<ol class="sy-chain">{"".join(out)}</ol></div>'


def _checklist() -> str:
    body = "".join(f'<tr><td class="sy-status">{design.tag(STATUS_TEXT.get(r["status"], r["status"]), STATUS_KIND.get(r["status"], ""))}</td>'
                   f'<td><b>{esc(r["criterion"])}</b><span class="sub">{esc(r["measured in this source"])}</span></td></tr>' for r in rows)
    return f'<div class="tc">{design.section("", "TigerGraph use", sid)}<table class="tc-table sy-checks"><tbody>{body}</tbody></table></div>'


def _dot(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _call_graph() -> str:
    """The same counts as a Graphviz call graph: the graph left, the stages stacked in order, the LLM right. Every edge comes
    from a phase's counts above; a stage with no call has no edge. The LLM node names the models calls.jsonl records."""
    graph = "TigerGraph · FraudGraph" if live_graph else "Graph (mock, DuckDB facts)"
    llm = ("LLM · " + ", ".join(models)) if models else "LLM (model not recorded in this source)"
    dot = ["digraph pipeline {", 'rankdir=LR; bgcolor="transparent"; nodesep=0.14; ranksep=1.2;',
           'node [shape=box, style="rounded", fontname="Helvetica", fontsize=13, margin="0.14,0.05", height=0.34];',
           'edge [fontname="Helvetica", fontsize=11, arrowsize=0.6];',
           f'tg [label="{_dot(graph)}", shape=cylinder, height=0.9];',
           f'llm [label="{_dot(llm)}", shape=component];', "{ rank=same;"]
    nid = {s["code"]: s["code"].lower() for s in common.PIPELINE}
    for s in common.PIPELINE:
        dashed = ', style="rounded,dashed"' if s["code"] == "S0" else ""
        dot.append(f'{nid[s["code"]]} [label="{_dot(s["code"] + "  " + s["label"])}"{dashed}];')
    dot += ["}", " -> ".join(nid.values()) + " [arrowsize=0.5];"]
    for code, x in lanes.items():
        if code == "S0":
            continue
        label = " · ".join([f"{x['reads']:,} reads"] * bool(x["reads"]) + [f"{x['writes']:,} writes"] * bool(x["writes"]))
        if x["reads"] or x["writes"]:
            way = "both" if x["reads"] and x["writes"] else "forward" if x["reads"] else "back"
            dot.append(f'tg -> {nid[code]} [dir={way}, label="{label}"];')
        if x["llm"]:
            dot.append(f'{nid[code]} -> llm [label="{x["llm"]:,} calls"];')
    return "\n".join([*dot, "}"])


# --------------------------------------------------------------------------- #
# First screen: the answer, the pipeline map and the checklist                #
# --------------------------------------------------------------------------- #
common.page_header("How it works", "Read from the source's calls.jsonl and phases.jsonl, the scorecard's code and the proof files.",
                   eyebrow=f"{sid} · {n} answered case(s) with a trace" if src is not None else "")
if src is not None:
    design.html_block(_lead())
left, right = st.columns([1.75, 1], gap="large")
with left:
    design.html_block(_chain())
    st.caption(md(f"Counted now from runs/{sid}/<case>/calls.jsonl, one attempt per answered case ({n}), newest file {calls_as_of}. Reads are tool "
                  "rows whose caller is not persist, writes are persist rows, LLM calls are llm rows with the model each records. S0 runs "
                  "offline before any case, so it has no trace row." + (f" {unplaced} call(s) name a phase outside S0–P10 and are not counted." if unplaced else ""))
               if traced else "Graph and LLM calls are recorded only in a run's calls.jsonl")
    with st.expander("Call graph"), st.container(key="sy-callgraph"):
        st.graphviz_chart(_call_graph(), width="content")
with right:
    if rows:
        design.html_block(_checklist())
        st.caption(md(f"Computed now from {TRACE_SRC if traced else sid} ({n} traced case(s)) with common.tg_usage, attest_claims and guardrail_events"))
        with st.expander("Example call per criterion"):
            design.html_block('<table class="tc tc-table sy-examples"><tbody>' + "".join(
                f'<tr><td>{esc(r["criterion"])}</td><td class="mono">{esc(r["example call"])}</td></tr>' for r in rows) + "</tbody></table>")
    common.tigergraph_credit("system")

# --------------------------------------------------------------------------- #
# How the scorecard decides: every number read from the code                  #
# --------------------------------------------------------------------------- #
design.html_block(design.section("", "How the scorecard decides", "P3, then P4 and P7"))
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

    left, right = st.columns([1.3, 1], gap="large")
    with left, st.container(key="sy-scorecard-steps"):
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
        with st.expander(f"Log-odds adjustments · {len(bumps)}"):
            st.caption(md(f"{rd.get('policy_file')}, engine block"))
            st.dataframe(pd.DataFrame([{"parameter": k, "value": _v(E, k), "applied when": w} for k, w in bumps]), hide_index=True, width="stretch",
                         column_config={"applied when": st.column_config.TextColumn(width="large")})
        floors =[("ring_floor", "anonymous-proxy device ring", rd.get("policy_file")), ("burst_with_denial_floor", "under-$500 burst with a customer denial", rd.get("policy_file")),
                  ("burst_alone_floor", "under-$500 burst without a denial", rd.get("policy_file")), ("denial_floor", "customer denial not explained by R7", rd.get("policy_file"))]
        dets = [d for d in (rd.get("detector_floors") if isinstance(rd.get("detector_floors"), list) else []) if isinstance(d, dict)]
        ordered = bool(dets) and all("in_scan" in d for d in dets)  # older snapshots carry no hit_from_scan order
        unused = [str(d.get("detector_id") or d.get("function")) for d in dets if ordered and not d.get("in_scan")]
        rows_f = [{"floor": k, "value": _v(E, k), "when": w, "source": s} for k, w, s in floors[:1]]
        rows_f += [{"floor": d.get("detector_id") or d.get("function"), "value": str(d.get("p_floor", "not a literal")) + (" (default)" if d.get("p_floor_default") else ""),
                    "when": f"detector hit{(': ' + str(d['name'])) if d.get('name') else ''}", "source": f"{rd.get('detectors_file')}:{d.get('line')}"}
                   for d in dets if not ordered or d.get("in_scan")]
        rows_f += [{"floor": k, "value": _v(E, k), "when": w, "source": s} for k, w, s in floors[1:]]
        with st.expander(f"Floors · {len(rows_f)}"):
            st.caption(md("Checked in this order; the first that matches applies" if ordered
                          else "The detector rows' order is not recorded in this source; the first floor that matches applies" if dets
                          else "The first floor that matches applies"))
            st.dataframe(pd.DataFrame(rows_f), hide_index=True, width="stretch", column_config={"when": st.column_config.TextColumn(width="large")})
            if unused:
                st.caption(md(f"Defined in {rd.get('detectors_file')} but not tried by hit_from_scan, so they never floor p: {', '.join(unused)}"))
            if not dets:
                st.caption("The detector floors are not readable in this source (detector_floors missing or not a list), so they are not in the table")
    how = (f"read now from {rules['source']} (policy file as of {rules['as_of']})" if rules["mode"] == "code"
           else f"from {rules['source']} (snapshot generated {rules['generated_at'] or rules['as_of']}, git {rules['git_sha'] or 'unknown'})")
    st.caption(md(f"Every parameter value in these sentences and tables is {how}. The 'applied when' and 'when' texts, thresholds included, are "
                  "typed summaries of engine/scorecard.py. Rules coded inline in engine/scorecard.py are not parameters and are not listed, for "
                  "example the R5 card-testing floor (between the bursts and the customer denial) and the Final Round mixed-channel / "
                  "recipient-email conflict override."
                  + (f" Notes: {rules['error']}" if rules["error"] else "")))

# --------------------------------------------------------------------------- #
# Known limits, grouped by theme                                              #
# --------------------------------------------------------------------------- #
limits = common.known_limits()
groups: dict[str, list[dict]] = {}
for item in limits:
    theme = next((name for name, rx in THEMES if rx.search(str(item["title"]))), "Other")
    groups.setdefault(theme, []).append(item)
design.html_block(design.section("", "Known limits", f"{len(limits)} computed from files"))
if not limits:
    st.caption("No limit computed from the files on this deployment")
else:
    order = [name for name, _ in THEMES] + ["Other"]
    blocks = []
    for theme in [t for t in order if t in groups]:
        body = "".join(f'<tr><td><b>{esc(it["title"])}</b><span class="sub">{esc(it["detail"])}</span></td>'
                       f'<td class="sy-src">{esc(it["source"])}<span class="sub">as of {esc(it["as_of"])}</span></td></tr>' for it in groups[theme])
        blocks.append(f'<section class="sy-lim"><h3>{esc(theme)} <span>{len(groups[theme])}</span></h3>'
                      f'<table class="tc-table"><tbody>{body}</tbody></table></section>')
    design.html_block(f'<div class="tc sy-limits">{"".join(blocks)}</div>')

"""Runs & consistency: is the selected run complete and clean, and which of its numbers can be quoted.

First screen: one provenance sentence whose details popover merges the run's identity, its completion and the incident
runs/<run>/provenance.json records; five measured figures (latency median and p95, LLM-chosen graph reads against the
cap, tokens with their cache share, final actions by route, write-backs); and same-decision agreement with the newest
other live run of the same round. Below: the agreement table (differences first), the cases x sources grid, the recorded
call time per case by component and the per-case table. Folded: the numbers to quote, the README check, the guardrails
that fired, the raw manifest and the graph hygiene log.

Files only: runs/<source>/run_manifest.json and provenance.json, runs/<source>/<case>/{answer.json, calls.jsonl,
phases.jsonl}, cases/HH*-*.json, README.md and runs/*/stale_writebacks.json. Reads and tokens follow common.trace_stats
(the answer.json rule). This file imports no engine, agent, schema_engine or duckdb code and writes nothing.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common, design  # noqa: E402

md, esc, tag = common.md, design.esc, design.tag
# phases.jsonl notes that record a fact on every case (what case memory left out, the vector index state): informational,
# not a guardrail that fired
INFO_NOTES = {"memory_exclusion", "vector_index"}
ROUTES = ("auto", "L1", "L2")
ctx = common.page_setup("runs")
src = ctx.source
if src is None:
    common.page_header("Runs & consistency")
    st.info("No sources on this deployment: no agent run under runs/ and no answer files under cases/.")
    st.stop()
sid = src.id
prov = common.run_provenance(sid)
man = src.manifest if src.kind == "run" else {}
IS_RUN = src.kind == "run"
ROUND = common.ROUND_LABELS.get(src.round, src.round)


def _percentiles(values) -> dict | None:
    """{n, p50, p95, rank95, nr50, min, max} over the non-missing values. p50 is the median (the mean of the two middle
    values when n is even); p95 is nearest-rank: the value at rank ceil(0.95 n) of the sorted values, which is the max for
    n < 20. None when there are no values."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n, mid = len(vals), len(vals) // 2
    r95 = max(1, math.ceil(0.95 * n))
    return {"n": n, "p50": vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, "p95": vals[r95 - 1], "rank95": r95,
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


def _stamp(value) -> str:
    """A manifest timestamp as 'YYYY-MM-DD HH:MM:SS'."""
    return str(value or "").replace("T", " ")[:19]


def _window(start: str, end: str) -> str:
    """'2026-10-31 11:02 → 11:38': the date once when both ends share it."""
    if not start:
        return f"finished {end[:16]}" if end else "no start time recorded"
    if not end:
        return f"{start[:16]} → not finished"
    return f"{start[:16]} → {end[11:16] if end[:10] == start[:10] else end[:16]}"


def _read_provenance() -> tuple[dict | None, str]:
    """runs/<run>/provenance.json, the record the team writes when a run is promoted: (data, error). (None, '') when this
    source has none; an unreadable file returns its error, shown verbatim."""
    p = src.dir / "provenance.json"
    if not IS_RUN or not p.is_file():
        return None, ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return (data, "") if isinstance(data, dict) else (None, "provenance.json is not a JSON object")


def _incidents(pdata: dict | None) -> list[dict]:
    """provenance.json failed_attempt entries (one object or a list) that name a case."""
    fa = (pdata or {}).get("failed_attempt")
    return [x for x in (fa if isinstance(fa, list) else [fa]) if isinstance(x, dict) and x.get("case")]


def _resumed(case_id: str) -> str:
    """The start of the --resume invocation of run_manifest.json that selected this case again ('' when none did)."""
    for inv in man.get("invocations") or []:
        if isinstance(inv, dict) and inv.get("resume") and case_id in (inv.get("cases_selected") or []):
            return _stamp(inv.get("started"))
    return ""


def _incident_text(inc: dict) -> str:
    """One clause per incident, built from its fields: case, phase, the first clause of the resolution, the resume."""
    first = str(inc.get("resolution") or "").split(";")[0].strip()
    when = _resumed(str(inc["case"]))
    return (f"{inc['case']} failed" + (f" at {inc['phase']}" if inc.get("phase") else "") + (f": {first}" if first else "")
            + (f"; re-run with --resume at {when[11:16] or when}" if when else ""))


_FENCE = re.compile(r"^\s*(```|~~~)")
_RUN_ARG = re.compile(r"(?:--run-id[ =]|RUN_ID=)([A-Za-z0-9][\w.-]*)")


def _readme_check() -> dict:
    """common.readme_facts() with each cited run folder sorted into a result citation or an example command path. A name
    given to --run-id / RUN_ID= inside a fenced code block, or cited only inside code blocks, is a folder the reader's own
    command creates, so its absence here is expected; every other citation is a result and must exist."""
    rf = common.readme_facts()
    out = {**rf, "results_cited": [], "examples": []}
    if not rf["exists"]:
        return out
    try:
        lines = (common.ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    fenced, code_lines, created = False, set(), set()
    for n, line in enumerate(lines, 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            code_lines.add(n)
            created.update(_RUN_ARG.findall(line))
    for c in rf["cited_runs"]:
        example = c["run"] in created or all(n in code_lines for n in c["lines"])
        out["examples" if example else "results_cited"].append(c)
    return out


def _peer() -> common.Source | None:
    """The newest other live run of this source's round (by first_started). For submitted answers the runs they were
    promoted from are left out: the files are copies of them, identical by construction."""
    copies = {r for rs in src.promoted_from.values() for r in rs} if src.kind == "submitted" else set()
    runs = [s for s in common.sources() if s.kind == "run" and s.mode == "live" and s.round == src.round and s.id not in copies | {sid}]
    return max(runs, key=lambda s: (s.first_started, s.id), default=None)


def _pairs(answer: dict) -> set[tuple[str, str]]:
    return {(str(x.get("action")), str(x.get("route", "auto"))) for x in _final(answer)}


def _delta(a: dict, b: dict, other: str) -> str:
    """What differs between two answers of one case, in a few words: verdict, the (action, route) pairs only one side has,
    and the SAR decision."""
    ca, cb = a.get("case") or {}, b.get("case") or {}
    pa, pb = _pairs(a), _pairs(b)
    parts = [f"verdict {ca.get('verdict')} → {cb.get('verdict')}"] if ca.get("verdict") != cb.get("verdict") else []
    if pa - pb:
        parts.append(", ".join(f"{x} {r}" for x, r in sorted(pa - pb)) + f" only in {sid}")
    if pb - pa:
        parts.append(", ".join(f"{x} {r}" for x, r in sorted(pb - pa)) + f" only in {other}")
    sa, sb = bool((a.get("sar") or {}).get("file")), bool((b.get("sar") or {}).get("file"))
    if sa != sb:
        parts.append(f"SAR {'yes' if sa else 'no'} → {'yes' if sb else 'no'}")
    return "; ".join(parts)


def _agree_rows(other: str, ids) -> list[dict]:
    """common.agreement for every case both sources answered, with the case id and the answers."""
    rows = []
    for cid in ids:
        a, b = common.case_view(sid, cid), common.case_view(other, cid)
        if a.ok and b.ok:
            rows.append({"case": cid, "a": a.answer, "b": b.answer, **common.agreement(a.answer, b.answer)})
    return rows


expected = [c for c in man.get("cases") or [] if isinstance(c, str)] if man.get("cases") else [r["case_id"] for r in common.pack_for_source(src)]
ORDER = _order(set(expected) | set(src.cases) | set(src.states))
views = {cid: common.case_view(sid, cid) for cid in ORDER}
done = {cid: v for cid, v in views.items() if v.ok}
stats = {cid: common.trace_stats(v) for cid, v in done.items()}
by_state = {s: [c for c, v in views.items() if v.state == s] for s in common.CASE_STATES}
ANS_SRC = f"cases/{common.SUBMITTED[sid][0]}*.json" if not IS_RUN else f"runs/{sid}/<case>/answer.json"
ANS_AS_OF = _newest(_answer_path(src, c) for c in done)
TRACE_SRC = f"runs/{sid}/<case>/calls.jsonl" if IS_RUN else f"{ANS_SRC} counters (no trace in this source)"
TRACE_AS_OF = _newest(src.dir / c / "calls.jsonl" for c in done) if IS_RUN else ANS_AS_OF
MAN_SRC, MAN_AS_OF = f"runs/{sid}/run_manifest.json", common.as_of(src.dir / "run_manifest.json")
PROV_SRC, PROV_AS_OF = f"runs/{sid}/provenance.json", common.as_of(src.dir / "provenance.json")
pdata, perr = _read_provenance()
incidents = _incidents(pdata)
top_hashes = man.get("prompt_sha256") or {}
case_runs = man.get("case_runs") if isinstance(man.get("case_runs"), dict) else {}
hashed = [cr for cr in case_runs.values() if isinstance(cr, dict) and cr.get("prompt_sha256")]
hashes_same = sum(1 for cr in hashed if all(str(top_hashes.get(k, "")).startswith(str(v)) for k, v in cr["prompt_sha256"].items()))
hashes_ok = bool(hashed) and hashes_same == len(hashed) and bool(prov.get("prompt_hashes_consistent"))
guard = (pdata or {}).get("code") if isinstance((pdata or {}).get("code"), dict) else {}
guard_lists = [str(v) for v in (guard.get("list_sha256") or {}).values()] if isinstance(guard.get("list_sha256"), dict) else []
guard_same = len(guard_lists) > 1 and len(set(guard_lists)) == 1

# --------------------------------------------------------------------------- #
# Head: the provenance sentence and its details                               #
# --------------------------------------------------------------------------- #
def _lead() -> str:
    """The page's answer: what produced this source, whether it is complete and clean, and the incident on record."""
    n, total = len(done), len(views)
    problems = [f'<span class="{"red" if s in ("failed", "unreadable") else "ochre"}">{len(by_state[s])} {esc(common.STATE_LABELS[s])}</span>'
                for s in ("failed", "unreadable", "running", "incomplete", "not_run") if by_state[s]]
    word = "answered" if IS_RUN else "readable"
    count = f'<b class="green">{n}/{total} {word}</b>' if total and n == total else f"<b>{n}/{total} {word}</b>"
    state = ", ".join([count, *problems])
    if not IS_RUN:
        return f'<p class="tc tc-lead"><b>Submitted answers</b> in {esc(ANS_SRC)}, answer files without a trace: {state}.</p>'
    models = [m for m, _ in Counter(src.models_seen).most_common()] or ([src.manifest_model] if src.manifest_model else [])
    head = (f"<b>{'Live run' if src.mode == 'live' else 'Mock run'}</b> on {esc(', '.join(models) or 'no recorded model')}, "
            f"git {esc(src.git_sha[:8] or 'unrecorded')}, {esc(_window(src.first_started, src.finished))}")
    if src.mode != "live":
        head += ", the graph not queried"
    clean = ['<span class="ochre">uncommitted changes</span>' if src.git_dirty else "committed code" if src.git_dirty is False else ""]
    if hashed:
        clean.append("prompt hashes consistent" if hashes_ok else '<span class="ochre">prompt hashes differ</span>')
    if guard_same:
        clean.append("code unchanged during the run")
    if src.model_note:
        clean.append('<span class="ochre">model mismatch</span>')
    clean_txt = ", ".join(c for c in clean if c)
    tail = "".join(f' <span class="ochre">Incident</span>: {esc(_incident_text(i))}.' for i in incidents)
    return f'<p class="tc tc-lead">{head}: {state}' + (f"; {clean_txt}" if clean_txt else "") + f".{tail}</p>"


def _kv(rows: list[tuple[str, str]]) -> str:
    """A label / value table; each value is HTML its caller built with esc()."""
    body = "".join(f'<tr><th scope="row">{esc(k)}</th><td>{v}</td></tr>' for k, v in rows)
    return f'<table class="tc-table rn-kv"><tbody>{body}</tbody></table>'


def _sub(text: object) -> str:
    return f'<span class="sub">{esc(text)}</span>'


def _details() -> None:
    """Behind the sentence: the identity and completion of the source, then what provenance.json records."""
    states = "".join(_sub(f"{common.STATE_LABELS[s]}: {', '.join(by_state[s])}") for s in ("running", "failed", "incomplete", "unreadable", "not_run") if by_state[s])
    answered = esc(f"{len(done)}/{len(views)}") + states
    if not IS_RUN:
        copies = Counter(r for rs in src.promoted_from.values() for r in rs)
        rows = [("Files", esc(ANS_SRC) + _sub("answer files only: no trace, so no graph reads, tokens or write-backs of their own")),
                ("Identical to", esc(", ".join(f"{r} on {k}/{src.n_total}" for r, k in copies.most_common()) or "no run in this deployment")),
                ("Readable", answered)]
        if prov.get("not_identical_to_any_run"):
            rows.append(("Not identical to any run", esc(", ".join(prov["not_identical_to_any_run"]))))
        design.html_block(_kv(rows))
        st.caption(md(f"{ANS_SRC} · as of {ANS_AS_OF} · identical = the run's answer.json parses equal to the file"))
        return
    inv = src.invocations
    resumes = sum(1 for i in inv if i["resume"])
    models = Counter(src.models_seen)
    rows = [("Mode", esc(prov.get("run_mode") or "unknown")), ("Backend", esc(src.llm_backend or "unrecorded")),
            ("Model", esc(", ".join(f"{m} × {k} LLM calls" for m, k in models.most_common()) or "no LLM rows in calls.jsonl")
             + (_sub(src.model_note) if src.model_note else ""))]
    if prov.get("sar_model"):
        rows.append(("SAR model", esc(prov["sar_model"])))
    rows += [
        ("Code", esc(f"git {src.git_sha[:8] or 'unrecorded'} · " + ("uncommitted changes" if src.git_dirty else "committed" if src.git_dirty is False else "dirty flag not recorded"))),
        ("Window", esc(f"{src.first_started or '?'} → {src.finished or 'not finished'}")),
        ("Invocations", esc(f"{len(inv)}" + (f", {resumes} with --resume" if resumes else ""))
         + "".join(_sub(f"{i['started']} · {'--resume' if i['resume'] else 'new'} · {i['cases']} case(s) · {i['run_mode']} · {i['llm_backend']}") for i in inv)),
        ("Prompt hashes", esc(f"consistent {hashes_same}/{len(hashed)}" if hashed else "not recorded per case")),
        ("max_tool_calls", esc(src.max_tool_calls if src.max_tool_calls is not None else "unrecorded")),
        ("Answered", answered),
    ]
    if guard:
        rows.append(("Code guard", esc(guard.get("guard") or "recorded") + (_sub(f"{len(guard_lists)} hash lists, {'all identical' if guard_same else 'not identical'}") if guard_lists else "")))
    graph = (pdata or {}).get("graph_at_promotion")
    if isinstance(graph, dict):
        counts = " · ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}" for k, v in (graph.get("vertex_counts") or {}).items())
        by_run = ", ".join(f"{k} {v}" for k, v in (graph.get("hhf_agentcase_run_ids") or {}).items())
        rows.append(("Graph at promotion", esc(f"read {graph.get('read_at') or '?'}: {counts}") + (_sub(f"AgentCase by run: {by_run}") if by_run else "")))
    for inc in incidents:
        rows.append(("Incident", esc(f"{inc['case']}" + (f" at {inc['phase']}" if inc.get("phase") else "")) + _sub(inc.get("resolution") or "no resolution recorded")
                     + (_sub(f"logs: {inc['logs']}") if inc.get("logs") else "")))
    design.html_block(_kv(rows))
    for inc in incidents:
        if inc.get("error"):
            st.caption(md(f"{inc['case']}: the error verbatim (the model output inside it is untrusted)"))
            st.code("\n".join(map(str, inc["error"])) if isinstance(inc["error"], list) else str(inc["error"]), language=None, wrap_lines=True)
    if perr:
        st.caption(md(f"{PROV_SRC} unreadable: {perr}"))
    st.caption(md(f"{MAN_SRC} · as of {MAN_AS_OF}" + (f" · {PROV_SRC} · as of {PROV_AS_OF}" if pdata else "")
                  + " · models are counted on the kind=='llm' rows of calls.jsonl"))


files = (f"runs/{sid}: run_manifest.json{', provenance.json' if pdata or perr else ''} and each case's answer.json, phases.jsonl and calls.jsonl"
         if IS_RUN else f"{ANS_SRC}, answer files without a trace")
common.page_header("Runs & consistency", f"Read from {files}.", eyebrow=f"{ROUND} · {sid}")
with st.container(horizontal=True, vertical_alignment="center", gap="small", key="rn-lead"):
    st.html(_lead(), width="content")
    with st.popover("Run details" if IS_RUN else "Details", icon=":material/info:", key="rn-details"):
        _details()
for c in by_state["failed"] + by_state["unreadable"]:
    st.caption(md(f"{c} · {common.STATE_LABELS[views[c].state]} · error verbatim:"))
    st.code(views[c].error or "no error text recorded", language=None, wrap_lines=True)

# --------------------------------------------------------------------------- #
# Five figures, then the consistency headline                                 #
# --------------------------------------------------------------------------- #
lat = _percentiles(s["latency_s"] for s in stats.values())
reads = _percentiles(s["reads"] for s in stats.values())
traced = [s for s in stats.values() if s["has_trace"]]
llm_reads = _percentiles(s["reads_by_caller"].get("llm", 0) for s in traced) if IS_RUN and traced else None
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
w_ok, w_all = sum(s["writes_ok"] for s in stats.values()), sum(s["writes"] for s in stats.values())
matched = [s["matches_answer"] for s in stats.values() if s["matches_answer"] is not None]
match_txt = f"the trace matches answer.json on {sum(matched)}/{len(matched)} cases" if matched else "no trace to check against answer.json"
cap = src.max_tool_calls

with st.container(border=True, key="strip-runs"):
    fig = st.columns(5)
common.measured("Latency, median · p95", f"{lat['p50']:.1f} s · {lat['p95']:.1f} s" if lat else None, source=f"{ANS_SRC} latency_s", as_of=ANS_AS_OF,
                container=fig[0], what="wall-clock seconds per answered case",
                how=(f"median over {lat['n']} answered cases, the mean of the two middle values (nearest-rank p50 is {lat['nr50']:.1f} s); "
                     f"p95 is nearest-rank, the value at rank ceil(0.95 n) = {lat['rank95']} of the sorted latencies") if lat else "")
if lat:
    fig[0].caption(md(f"n={lat['n']} · p95 nearest-rank" + (f", which at n={lat['n']} is the max" if lat["rank95"] == lat["n"] else f"; max {lat['max']:.1f} s")))
if llm_reads:
    common.measured("LLM-chosen graph reads per case, median", f"{llm_reads['p50']:g} of max {cap}" if cap is not None else f"{llm_reads['p50']:g}", source=TRACE_SRC,
                    as_of=TRACE_AS_OF, container=fig[1], what="graph reads the model chose itself, per case, against the per-case cap",
                    how=f"kind=='tool' rows with caller == 'llm'; the cap is max_tool_calls in {MAN_SRC}; {match_txt}")
    fig[1].caption(md(f"max {llm_reads['max']:g} · all graph reads median {reads['p50']:g} ({reads['min']:g}–{reads['max']:g})"))
else:
    common.measured("Graph reads per case, median", f"{reads['p50']:g}" if reads else None, source=TRACE_SRC, as_of=TRACE_AS_OF, container=fig[1],
                    how="the answer files' tool_calls counters: without a trace, the reads the model chose are not told apart")
    if reads:
        fig[1].caption(md(f"range {reads['min']:g}–{reads['max']:g}"))
common.measured("Tokens per case, median", f"{toks['p50']:,.0f}" if toks else None, source=TRACE_SRC, as_of=TRACE_AS_OF, container=fig[2],
                what="tokens per case, cache reads and writes included",
                how="input + output + cache read + cache write over kind=='llm' rows" if IS_RUN else "the answer files' tokens counters")
if toks:
    fig[2].caption(md(f"cache {cache_share:.0%} of {t_all:,} in all" if cache_share is not None else f"{t_all:,} in all · cache share not recorded"))
common.measured("Final actions, auto · L1 · L2", " · ".join(str(routes.get(r, 0)) for r in ROUTES) if done else None, source=ANS_SRC, as_of=ANS_AS_OF,
                container=fig[3], what="final actions by route: auto runs at once, L1 waits for a team lead, L2 for a fraud manager",
                how="next_best_actions.final over the answered cases, counted by route")
if done and gated_d:
    other_routes = {r: n for r, n in routes.items() if r not in ROUTES}
    fig[3].caption(md(f"{sum(routes.values())} over {len(done)} cases · L1/L2 per case median {gated_d['p50']:g}, max {gated_d['max']:g}"
                      + "".join(f" · {r} {n}" for r, n in other_routes.items())))
if IS_RUN and src.mode == "live":
    common.measured("Write-backs to the graph", f"{w_ok}/{w_all} ok", source=f"runs/{sid}/<case>/calls.jsonl, caller persist", as_of=TRACE_AS_OF, container=fig[4],
                    how="calls.jsonl rows with caller == 'persist': those with ok == true over all of them")
    if done and w_all:
        fig[4].caption(md(f"{w_all / len(done):.1f} persist rows per case"))
elif IS_RUN:
    common.measured("Write-backs to the graph", "not written (mock run)", source=f"{MAN_SRC} run_mode", as_of=MAN_AS_OF, container=fig[4],
                    how="a mock run's persist rows, if any, go to a mock session and never to the graph")
else:
    common.measured("Write-backs to the graph", None, source="no trace in this source", as_of=ANS_AS_OF, container=fig[4],
                    how="write-backs are recorded only in a run's calls.jsonl")
st.caption("Cost is not computed: no price table is checked in, so no cost is derived from the measured tokens.")

peer = _peer()
p_rows = _agree_rows(peer.id, ORDER) if peer else []
p_same = sum(r["same_decision"] for r in p_rows)
p_diff = [r for r in p_rows if not r["same_decision"]]
sub = common.get_source("submitted" if src.round != "round1" else "submitted-r1")
if IS_RUN:
    n_copy = sum(1 for cid, rs in common.promoted_from().items() if sid in rs and sub is not None and cid in sub.states)
    copy_line = f"The submitted answers ({ANS_SRC if not sub else 'cases/' + common.SUBMITTED[sub.id][0] + '*.json'}) are copies of {sid}: identical on {n_copy}/{sub.n_total} by construction." if n_copy and sub else ""
else:
    copies = Counter(r for rs in src.promoted_from.values() for r in rs).most_common(1)
    copy_line = f"These files are copies of {copies[0][0]}: identical on {copies[0][1]}/{src.n_total} by construction." if copies else ""
peer_as_of = _newest(_answer_path(peer, c) for c in ORDER) if peer else "missing"
both_as_of = max([x for x in (ANS_AS_OF, peer_as_of) if x != "missing"] or ["missing"])

design.html_block(design.section("", "Consistency", note=f"against {peer.id}, the newest other live {ROUND} run" if peer else ""))
with st.container(border=True, key="strip-rn-cons"):
    band = st.columns([1, 2.6], vertical_alignment="center")
common.measured(f"Same decision as {peer.id}" if peer else "Same decision as another live run", f"{p_same}/{len(p_rows)}" if p_rows else None,
                source=f"{ANS_SRC} and runs/{peer.id}/<case>/answer.json" if peer else f"no other live {ROUND} run in this deployment", as_of=both_as_of,
                container=band[0], what=f"cases where {sid} and the other run reach the same decision",
                how="the same verdict, the same final (action, route) set and the same sar.file (common.agreement), over the cases both answered")
lines = []
if peer and p_diff:
    hard = [r for r in p_diff if r["cls"] == "differs · hard action"]
    lines.append(f"Differs on {len(p_diff)} case{'s' if len(p_diff) != 1 else ''}, " + (f'<span class="red">{len(hard)} in a hard action or SAR</span>' if hard else "none in a hard action or SAR")
                 + ": " + "; ".join(f"<b>{esc(r['case'])}</b> {esc(_delta(r['a'], r['b'], peer.id))}" for r in p_diff[:4]) + ("; …" if len(p_diff) > 4 else "") + ".")
elif peer and p_rows:
    lines.append("The same decision on every case both runs answered.")
elif peer:
    lines.append(esc(f"No case is answered in both {sid} and {peer.id}."))
else:
    lines.append(esc(f"No other live {ROUND} run in this deployment, so run-to-run agreement is not measured here."))
if peer:
    p_model = ", ".join(m for m, _ in Counter(peer.models_seen).most_common()) or peer.manifest_model or "model unrecorded"
    lines.append(esc(f"{peer.id}: {p_model}, git {peer.git_sha[:8] or 'unrecorded'}, {_window(peer.first_started, peer.finished)}, {peer.n_answered}/{peer.n_total} answered."))
if copy_line:
    lines.append(esc(copy_line))
with band[1]:
    design.html_block('<div class="tc rn-cons">' + "".join(f"<p>{x}</p>" for x in lines) + "</div>")

# --------------------------------------------------------------------------- #
# Below the first screen: agreement by case, differences first                #
# --------------------------------------------------------------------------- #
others = [s.id for s in common.sources() if s.id != sid]
if others:
    own_sub = next((x for x in ("submitted", "submitted-r1") if x in others and common.get_source(x).round == src.round), None)
    if st.session_state.get("runs_compare") not in others:
        st.session_state["runs_compare"] = peer.id if peer else own_sub or others[0]
    other = st.session_state["runs_compare"]
    osrc = common.get_source(other)
    a_rows = _agree_rows(other, _order(set(ORDER) | set(osrc.cases if osrc else [])))
    k = len(a_rows)
    shares = {"same SAR": sum(not any(d.startswith("sar.file") for d in r["diffs"]) for r in a_rows),
              "same pattern": sum(not any(d.startswith("pattern") for d in r["diffs"]) for r in a_rows),
              "same exposure": sum(not any(d.startswith("exposure_usd") for d in r["diffs"]) for r in a_rows),
              "|Δp| ≤ 0.10": sum(r["delta_p"] is not None and r["delta_p"] <= 0.10 for r in a_rows)}
    same_n = sum(r["same_decision"] for r in a_rows)
    design.html_block(design.section("", "Agreement by case", note=f"{same_n}/{k} same decision" if k and not (peer and other == peer.id) else "",
                                     aside=esc(" · ".join(f"{c} {n}/{k}" for c, n in shares.items())) if k else ""))
    st.selectbox("Compare with", others, format_func=common.source_label, key="runs_compare", width=560)
    if osrc is not None and {src.mode, osrc.mode} & {"mock"} and {src.mode, osrc.mode} - {"mock"}:
        st.warning(md(f"Comparing {src.mode.upper()} with {osrc.mode.upper()}: differences are expected"))
    if k:
        a_rows.sort(key=lambda r: (r["same_decision"], r["identical"], ORDER.index(r["case"]) if r["case"] in ORDER else 999))
        agree = []
        for r in a_rows:
            va, vb = (r["a"].get("case") or {}).get("verdict"), (r["b"].get("case") or {}).get("verdict")
            agree.append({"Open": r["case"], "class": [r["cls"]], "verdict": f"{va} → {vb}" if va != vb else str(va),
                          "differences": "; ".join(r["diffs"]) or "none", "likely cause": "; ".join(r["cause"]) or "—"})
        st.dataframe(pd.DataFrame(agree), key="runs_agree", hide_index=True, width="stretch", height=min(35 * k + 38, 35 * 20 + 38),
                     column_config={"Open": st.column_config.ButtonColumn("Open", type="tertiary", pinned=True, key="runs_agree_open", help="Open the Case file in the selected source"),
                                    "class": st.column_config.MultiselectColumn("class", options=list(common.AGREEMENT_COLORS), color=list(common.AGREEMENT_COLORS.values())),
                                    "differences": st.column_config.TextColumn("differences", width="large")})
        click = st.session_state.get("runs_agree_open")
        if click and click.get("label"):
            common.goto("case", case=click["label"])
    else:
        st.info(md(f"No case is answered in both {sid} and {other}"))

# --------------------------------------------------------------------------- #
# The cases x sources grid, and the recorded call time by component           #
# --------------------------------------------------------------------------- #
round_peers = [s for s in common.sources() if s.round == src.round and s.id != sid]
if round_peers and ORDER:
    grid = []
    for cid in ORDER:
        a, row, n_cmp, n_diff = views[cid], {"case": cid}, 0, 0
        for s in round_peers:
            b = common.case_view(s.id, cid)
            if a.ok and b.ok:
                same = common.agreement(a.answer, b.answer)["same_decision"]
                row[s.id] = "same" if same else "differs"
                n_cmp, n_diff = n_cmp + 1, n_diff + (not same)
            else:
                row[s.id] = "—" if b.ok or b.state == "not_run" else common.STATE_LABELS.get(b.state, b.state)
        row["differs in"] = f"{n_diff} of {n_cmp}" if n_cmp else "—"
        grid.append((n_diff, row))
    grid.sort(key=lambda x: (-x[0], ORDER.index(x[1]["case"])))
    n_any = sum(1 for d, _ in grid if d)
    design.html_block(design.section("", "Same case across sources", note=f"each {ROUND} source against {sid}: the same decision, or differs",
                                     aside=esc(f"{n_any} of {len(grid)} cases differ in at least one source")))
    st.dataframe(pd.DataFrame([r for _, r in grid]), key="runs_grid", hide_index=True, width="stretch", height=min(35 * len(grid) + 38, 35 * 20 + 38),
                 column_config={"case": st.column_config.TextColumn("case", pinned=True)})

if IS_RUN and done:
    order = sorted(done, key=lambda c: -(stats[c]["latency_s"] or 0))
    COMPONENTS = ["LLM", "graph read", "write-back"]
    rows = []
    for cid in order:
        for c in done[cid].calls:
            kind = "LLM" if c.get("kind") == "llm" else "write-back" if c.get("caller") == "persist" else "graph read"
            rows.append({"case": cid, "component": kind, "seconds": _num(c.get("latency_s")) or 0.0})
    if rows:
        df = pd.DataFrame(rows).groupby(["case", "component"], as_index=False)["seconds"].sum()
        df["stack"] = df["component"].map({c: i for i, c in enumerate(COMPONENTS)})
        tok = design.TOKENS.get(design.theme_type(), design.TOKENS["light"])
        t_llm = sum(s["llm_latency_s"] for s in stats.values())
        t_calls = t_llm + sum(s["tool_latency_s"] + s["write_latency_s"] for s in stats.values())
        design.html_block(design.section("", "Recorded call time per case, by component", note="summed call seconds, not wall-clock latency", aside=esc(TRACE_SRC)))
        bars = alt.Chart(df).mark_bar(stroke=tok["ground"], strokeWidth=0.5).encode(
            y=alt.Y("case:N", sort=order, title=None), x=alt.X("seconds:Q", stack="zero", title="summed call seconds (LLM and graph calls)"),
            color=alt.Color("component:N", scale=alt.Scale(domain=COMPONENTS, range=[tok["petrol"], tok["muted"], tok["rule-strong"]]), legend=alt.Legend(title=None, orient="top")),
            order=alt.Order("stack:Q"), tooltip=["case", "component", alt.Tooltip("seconds:Q", format=".1f")])
        st.altair_chart(bars, width="stretch")
        st.caption(md(f"latency_s of every LLM and graph call, summed per case and component: a bar is call time, not the case's wall-clock latency_s. "
                      f"Cases run from the slowest wall-clock latency down. LLM calls take {t_llm / t_calls:.0%} of the {t_calls:,.0f} s of recorded call time."
                      if t_calls else "No call latencies recorded."))

# --------------------------------------------------------------------------- #
# Per case, then folded: numbers to quote, README check, guardrails, manifest #
# --------------------------------------------------------------------------- #
design.html_block(design.section("", "Per case", note=f"{len(views)} cases", aside=esc(f"{ANS_SRC} · {TRACE_SRC}")))
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
        "guardrails": sum(1 for e in common.guardrail_events(v) if e["type"] not in INFO_NOTES) if v.has_trace else 0, "write-back": wb, "error": v.error,
    })
st.dataframe(pd.DataFrame(table), key="runs_table", hide_index=True, width="stretch", height=min(35 * len(table) + 38, 35 * 20 + 38),
             column_config={"Open trace": st.column_config.ButtonColumn("Open trace", type="tertiary", pinned=True, key="runs_open", help="Open the Case file"),
                            "p": st.column_config.NumberColumn("p", format="%.2f"), "tokens": st.column_config.NumberColumn("tokens", format="localized"),
                            "SAR": st.column_config.TextColumn("SAR", help="sar.file in the answer: a SAR drafted; filing waits for its L2 approval")})
st.caption(md(f"Reads and tokens by common.trace_stats: {match_txt}"))
opened = st.session_state.get("runs_open")
if opened and opened.get("label"):
    common.goto("case", case=opened["label"])

if lat and lat["rank95"] == lat["n"]:
    lat_q = f"{lat['p50']:.1f} s / {lat['p95']:.1f} s (n={lat['n']}; p95 nearest-rank, which at this n is the max)"
else:
    lat_q = f"{lat['p50']:.1f} s / {lat['p95']:.1f} s (n={lat['n']}; p95 nearest-rank)" if lat else "not measured here"
quote = [
    ("Verdict mix", f"{mix.get('fraud', 0)} fraud · {mix.get('legitimate', 0)} legitimate · {mix.get('uncertain', 0)} uncertain" if done else "not measured here"),
    ("SARs drafted (sar.file)", f"{sars}/{len(done)}" if done else "not measured here"),
    ("Uncertain escalated to an analyst", f"{safe['uncertain_escalated']}/{safe['n_uncertain']}" if safe["n_uncertain"] else "no uncertain verdicts"),
    ("Hard actions on non-fraud verdicts", str(len(safe["hard_on_non_fraud"])) if done else "not measured here"),
    ("Latency median / p95", lat_q),
    ("LLM-chosen graph reads per case, median", (f"{llm_reads['p50']:g} (max {llm_reads['max']:g}" + (f", cap {cap})" if cap is not None else ")")) if llm_reads else "not measured here"),
    ("All graph reads per case, median", f"{reads['p50']:g} ({reads['min']:g}–{reads['max']:g})" if reads else "not measured here"),
    ("Tokens per case, median incl. cache", f"{toks['p50']:,.0f}" + (f" (cache {cache_share:.0%})" if cache_share is not None else "") if toks else "not measured here"),
    ("Final actions by route", " · ".join(f"{r} {routes.get(r, 0)}" for r in ROUTES) if done else "not measured here"),
    ("Write-backs", f"{w_ok}/{w_all} ok" if IS_RUN and src.mode == "live" else "not written (mock run)" if IS_RUN else "not measured here"),
    (f"Same decision as {peer.id}" if peer else "Same decision as another live run", f"{p_same}/{len(p_rows)}" if p_rows else "not measured here"),
]
with st.expander("Numbers to quote"):
    st.code("\n".join([f"Source: {sid} ({ANS_SRC}, as of {ANS_AS_OF})"] + [f"- {k_}: {v_}" for k_, v_ in quote]), language="markdown")

rc = _readme_check()
cited, examples = rc["results_cited"], rc["examples"]
missing = [c for c in cited if not c["exists"]]
if not rc["exists"]:
    rc_label = "README check · README.md is not in this deployment"
else:
    rc_label = (f"README check · {len(cited)} result citation{'s' if len(cited) != 1 else ''}, " + (f"{len(missing)} missing here" if missing else "all present")
                + (f" · {len(examples)} example command path{'s' if len(examples) != 1 else ''}" if examples else ""))
with st.expander(rc_label):
    if not rc["exists"]:
        st.caption("README.md is not in this deployment, so its citations are not checked here")
    else:
        body = "".join(f'<tr><td class="mono">{esc(c["path"])}</td><td>result</td><td>{tag("present", "green") if c["exists"] else tag("missing", "red")}</td>'
                       f'<td class="num">{esc(", ".join(map(str, c["lines"])))}</td></tr>' for c in cited)
        body += "".join(f'<tr class="quiet"><td class="mono">{esc(c["path"])}</td><td>example command path</td><td>{"present" if c["exists"] else "absent, as expected"}</td>'
                        f'<td class="num">{esc(", ".join(map(str, c["lines"])))}</td></tr>' for c in examples)
        design.html_block('<table class="tc-table rn-readme"><thead><tr><th>Run folder</th><th>Cited as</th><th>In this deployment</th><th class="num">README lines</th></tr></thead>'
                          f"<tbody>{body}</tbody></table>")
        st.caption(md(f"README.md · as of {rc['as_of']} · an example command path is a folder the README's own commands create (--run-id or RUN_ID= in a code block), "
                      "or one cited only inside code blocks; every other citation is a result"))
        st.caption("README 'Results' section, verbatim")
        st.text("\n".join(rc["results"]) if rc["results"] else "(no 'Results' heading in README.md)")

ledger = common.guardrail_ledger(sid) if IS_RUN else []
fired = [r for r in ledger if r["count"] and r["type"] not in INFO_NOTES]
notes = [r for r in ledger if r["count"] and r["type"] in INFO_NOTES]
g_label = (f"Guardrails that fired · {sum(r['count'] for r in fired)} event{'s' if sum(r['count'] for r in fired) != 1 else ''} of {len(fired)} kind{'s' if len(fired) != 1 else ''}"
           if fired else "Guardrails that fired · none" if IS_RUN else "Guardrails · no trace in this source")
with st.expander(g_label):
    if fired:
        st.dataframe(pd.DataFrame([{"guardrail": r["type"], "count": r["count"], "cases": ", ".join(r["cases"])} for r in fired]), hide_index=True, width="stretch")
    else:
        st.caption("No guardrail fired in this source" if IS_RUN else "No trace in this source, so no guardrail events")
    if notes:
        st.caption(md("Informational notes, not guardrails: " + "; ".join(f"{r['type']} on {len(r['cases'])} case(s)" for r in notes)))
    common.page_link("security", "Security & drills", icon=":material/arrow_forward:")

if IS_RUN:
    with st.expander("Raw run_manifest.json"):
        st.caption(md(f"{MAN_SRC} · as of {MAN_AS_OF}"))
        st.json(man or {"note": "no run_manifest.json in this run folder"}, expanded=False)
hygiene = common.list_maintenance()
with st.expander(f"Graph hygiene log · {len(hygiene)} ledger{'s' if len(hygiene) != 1 else ''}" if hygiene else "Graph hygiene log"):
    if hygiene:
        st.dataframe(pd.DataFrame([{"folder": h["folder"], "generated_at": h["generated_at"], "run": "applied" if h["applied"] else "dry run",
                                    "counts": ", ".join(f"{k} {v}" for k, v in (h["counts"] or {}).items()), "deleted": "—" if h["deleted"] is None else str(h["deleted"]),
                                    "remaining": "—" if h["remaining"] is None else str(h["remaining"]),
                                    "cases": h["cases"], "error": h["error"]} for h in hygiene]), hide_index=True, width="stretch")
        st.caption(md("runs/*/stale_writebacks.json from ops.clear_case_writebacks; these folders are never a source"))
    else:
        st.caption(md("No runs/*/stale_writebacks.json on this deployment"))

"""Live architecture: how one alert moves through the system, played back from a real case's trace.

One animated diagram in four columns (inputs, the agent's phase machine, the engine and the model it consults, TigerGraph)
steps through S0 and P0-P10 for the case picked in the context bar. Which components light up in a step, the calls in it,
its model calls (the model as calls.jsonl records it), its duration and its one-line summary all come from that case's
phases.jsonl and calls.jsonl through common.trace_timeline / common.pipeline_state; nothing is typed in. A stage the
trace has no row for is shown as skipped, and a source without a trace (the submitted answers) says so.

The diagram is one self-contained HTML document in st.iframe. Every string from the trace reaches it as JSON with <, > and
& escaped and is written with textContent, so trace text (which can quote hostile trigger text) is never parsed as HTML.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

md, badge = common.md, common.badge
ctx = common.page_setup("live_architecture")

common.page_header(
    "Live architecture",
    "How one alert becomes an attested answer, replayed step by step from the selected case's own trace.",
    eyebrow="System",
)

with st.container(border=False, key="arch-intro"):
    i1, i2, i3 = st.columns(3, gap="large")
    i1.markdown("**Graph first.** Every fact the agent relies on is read from TigerGraph through typed, time-boxed queries: "
                "the same question, asked twice, gets the same answer, and nothing from after the alert leaks in.")
    i2.markdown("**The model advises; the engine decides.** The language model plans the investigation and writes it up, "
                "but a deterministic scorecard and the bank's policy set the probability and the actions it may take.")
    i3.markdown("**People hold the keys.** Anything that acts against a customer waits for a signed-in approver, and every "
                "step is written back to the graph so it can be replayed, challenged and continued.")

# --------------------------------------------------------------------------- #
# Components and which phase touches which                                    #
# --------------------------------------------------------------------------- #
# x, y, w, h in a 1200 x 600 viewBox; 'col' only groups them under a column heading.
NODES = {
    "alert": dict(x=24, y=64, w=210, h=74, title="Alert", sub="case pack row · trigger text", col=0),
    "data": dict(x=24, y=186, w=210, h=74, title="January data", sub="txns · identity · closed cases", col=0),
    "gate": dict(x=24, y=308, w=210, h=74, title="Schema & data-quality gate", sub="schema_engine · offline", col=0),
    "answer": dict(x=24, y=430, w=210, h=74, title="Answer & SAR", sub="answer.json · sar.md", col=0),
    "shield": dict(x=642, y=64, w=230, h=64, title="Input shield", sub="agent/security.py", col=2),
    "model": dict(x=642, y=148, w=230, h=64, title="Language model", sub="bounded by the engine and policy", col=2, llm=True),
    "engine": dict(x=642, y=232, w=230, h=64, title="Scorecard & detectors", sub="engine/scorecard.py · D1–D4", col=2),
    "policy": dict(x=642, y=316, w=230, h=64, title="Policy gate", sub="R1–R10 · approval routes", col=2),
    "sim": dict(x=642, y=400, w=230, h=64, title="Evidence request", sub="value of information", col=2),
    "sar": dict(x=642, y=484, w=230, h=64, title="SAR check & validator", sub="§3a · ids · tip-off", col=2),
    "mcp": dict(x=946, y=64, w=230, h=64, title="MCP read tools", sub="typed, time-boxed queries", col=3),
    "vector": dict(x=946, y=148, w=230, h=64, title="TigerVector", sub="similar cases · policy chunks", col=3),
    "graph": dict(x=946, y=232, w=230, h=148, title="FraudGraph", sub="TigerGraph Savanna", col=3, big=True),
    "write": dict(x=946, y=400, w=230, h=64, title="Write-back", sub="AgentCase · CaseEvent · Approval", col=3),
    "inbox": dict(x=946, y=484, w=230, h=64, title="Approval inbox", sub="L1 team lead · L2 fraud manager", col=3),
}
COLUMNS = [(24, "Inputs & outputs"), (300, "Agent phase machine"), (642, "Engine & model"), (946, "TigerGraph")]
SPINE = dict(x=300, y=64, w=272, row=42)  # one row per phase P0..P10

# (from, to) edges a stage uses. 'P' means the stage's own row on the spine. Model and graph edges are drawn only when
# the trace recorded a model call / a tool call in that stage.
STAGE_EDGES = {
    "S0": [("data", "gate")],
    "P0": [("alert", "P"), ("P", "shield"), ("P", "mcp"), ("mcp", "graph")],
    "P1": [("P", "vector"), ("vector", "graph")],
    "P2": [("P", "model"), ("P", "mcp"), ("mcp", "graph")],
    "P3": [("P", "engine")],
    "P4": [("P", "model"), ("P", "engine")],
    "P5": [("P", "policy"), ("P", "model")],
    "P6": [("P", "sim")],
    "P7": [("P", "policy"), ("P", "model")],
    "P8": [("P", "sar"), ("P", "model"), ("P", "mcp")],
    "P9": [("P", "model"), ("P", "sar"), ("P", "answer")],
    "P10": [("P", "write"), ("write", "graph"), ("P", "inbox")],
}
GRAPH_NODES, MODEL_NODES = {"mcp", "vector", "graph"}, {"model"}

# What each stage is for, in plain words: the idea behind it, not a measurement (those come from the trace).
STAGE_WHY = {
    "S0": "Before any case is opened, January's files are checked against the shape the agent expects. A changed column or "
          "a gap in the data is caught here and held for a person to approve, so nothing downstream quietly reads the wrong field.",
    "P0": "The alert arrives as written by a person, so it is treated as evidence, never as instruction. Sentences that try to "
          "steer the agent are removed, the claims are checked against the transaction itself, and the case's first facts are read from the graph.",
    "P1": "The bank has seen thousands of cases before this one. The agent recalls the closest closed cases and the policy "
          "passages that apply, so its judgement starts from precedent rather than from a blank page.",
    "P2": "The model plans the investigation, choosing which typed, read-only graph queries to run. It can look, but it cannot "
          "write, and it cannot see anything that happened after the alert was raised.",
    "P3": "A deterministic scorecard weighs the evidence in independent families and runs the wave detectors that look across "
          "cards. This is where the decision is anchored: the same facts always give the same probability.",
    "P4": "The model writes the assessment in words and may nudge the probability, but only within ten points of the engine's. "
          "Fluent prose cannot overturn the arithmetic.",
    "P5": "Policy rules decide which actions are allowed and who must approve them. The model may choose among those actions, "
          "never beyond them, and anything that acts against the customer waits for a person.",
    "P6": "If one more piece of evidence could change the outcome, the agent asks for it, worded so the customer is never "
          "tipped off. If no answer could change the outcome, it does not ask at all.",
    "P7": "With the reply in hand, the policy gate is run again and the final actions are recorded alongside what changed and why.",
    "P8": "When policy says a report is owed, the agent drafts it, and every sentence is checked against the ids it names. "
          "A sentence the evidence cannot support does not survive.",
    "P9": "The answer is written up and validated: every id must exist, every amount must add up, and nothing a customer "
          "could read may reveal a report.",
    "P10": "The case, its events and its approval requests are written back to the graph, so the next investigation can learn "
           "from this one, and a person can pick it up exactly where the agent stopped.",
}


def _stage_rows(view: common.CaseView) -> list[dict]:
    """One row per pipeline stage for the diagram, from the case's trace only."""
    stages = {s["code"]: s for s in common.pipeline_state(view)["stages"]}
    timeline = {e["code"]: e for e in common.trace_timeline(view)} if view.has_trace else {}
    rows = []
    for spec in common.PIPELINE:
        code = spec["code"]
        e = timeline.get(code) or {}
        calls, llm = e.get("calls") or [], e.get("llm") or []
        edges = []
        for a, b in STAGE_EDGES[code]:
            other = b if a == "P" else a
            if other in MODEL_NODES and not llm:
                continue
            if code != "P1" and other in GRAPH_NODES - {"graph"} and not calls:
                continue
            if code == "P1" and not calls:
                continue
            if (a, b) == ("mcp", "graph") and not calls:
                continue
            edges.append([a, b])
        if code == "S0":
            state, sentence = "offline", "Runs once, offline, before any case: January's schema drift and data-quality checks."
        else:
            state = stages.get(code, {}).get("state", "pending")
            reason = stages.get(code, {}).get("skip_reason", "")
            sentence = str(e.get("sentence") or (f"Skipped: {reason}." if reason else
                                                 "No row in the trace for this stage." if view.has_trace else "No trace in this source."))
        rows.append({
            "code": code, "label": spec["label"], "module": spec["module"], "why": STAGE_WHY.get(code, ""), "llm": bool(spec["llm"]), "state": state,
            "state_label": stages.get(code, {}).get("state_label", "") if code != "S0" else "", "t": e.get("t"), "duration": e.get("duration"), "sentence": sentence, "edges": edges,
            "calls": [{"name": str(c.get("name") or "").split("__")[-1], "params": common.compact_params(c.get("params"), 90),
                       "latency": c.get("latency_s"), "ok": c.get("ok") is not False} for c in calls],
            "llm_calls": [{"model": x.get("model") or "model not recorded", "tokens": x.get("tokens") or 0, "latency": x.get("latency_s")} for x in llm],
        })
    return rows


def _palette() -> dict:
    """The app's own theme colours (.streamlit/config.toml), so the diagram reads as part of the page in light and dark."""
    if st.context.theme.type == "dark":
        return {"bg": "#121314", "panel": "#1b1c1e", "line": "#2c2d30", "ink": "#e9e7e2", "ink2": "#9a978f", "ink3": "#5d5b56",
                "accent": "#6b9cf0", "accentBg": "#15243f", "done": "#4cb782", "doneBg": "#11291d", "llm": "#a98bf0", "llmBg": "#261d3d",
                "warn": "#e08a3c", "warnBg": "#3a2410"}
    return {"bg": "#fbfaf7", "panel": "#ffffff", "line": "#e4e1d9", "ink": "#18191c", "ink2": "#5f5d57", "ink3": "#a9a69e",
            "accent": "#2659c9", "accentBg": "#e8effc", "done": "#18794e", "doneBg": "#e7f4ec", "llm": "#6941c6", "llmBg": "#f1ecfb",
            "warn": "#b45309", "warnBg": "#fdf1e3"}


def _json_for_script(obj: object) -> str:
    return json.dumps(obj, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _diagram(case_label: str, rows: list[dict]) -> str:
    data = _json_for_script({"nodes": NODES, "columns": COLUMNS, "spine": SPINE, "stages": rows, "palette": _palette(), "case": case_label})
    return """<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#fff}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "IBM Plex Sans",-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{border:1px solid var(--line);border-radius:10px;background:var(--panel);overflow:hidden}
.bar{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.btn{appearance:none;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:6px;height:32px;min-width:32px;
     padding:0 10px;font:500 13px "IBM Plex Sans",sans-serif;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn:hover{border-color:var(--ink3)} .btn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.steps{display:flex;gap:4px;flex-wrap:wrap}
.step{border:1px solid var(--line);background:transparent;color:var(--ink2);border-radius:5px;height:28px;padding:0 8px;
      font:500 12px "IBM Plex Mono",monospace;cursor:pointer;position:relative}
.step.done{color:var(--done);border-color:color-mix(in srgb,var(--done) 45%,var(--line))}
.step.skipped{color:var(--ink3);border-style:dashed}
.step.on{background:var(--accent);border-color:var(--accent);color:#fff}
.meta{margin-left:auto;color:var(--ink2);font:12px "IBM Plex Mono",monospace;white-space:nowrap}
svg{display:block;width:100%;height:auto}
.colhead{font:600 11px "IBM Plex Sans",sans-serif;letter-spacing:.06em;text-transform:uppercase;fill:var(--ink3)}
.node rect{fill:var(--bg);stroke:var(--line);stroke-width:1;transition:fill .35s,stroke .35s}
.node .t{font:600 13.5px "IBM Plex Sans",sans-serif;fill:var(--ink);transition:fill .35s}
.node .s{font:12px "IBM Plex Sans",sans-serif;fill:var(--ink2)}
.node .tag{font:500 10.5px "IBM Plex Mono",monospace}
.node.lit rect{fill:var(--accentBg);stroke:var(--accent);stroke-width:1.5}
.node.lit.llm rect{fill:var(--llmBg);stroke:var(--llm)}
.row rect{fill:transparent;stroke:transparent;transition:fill .3s}
.row .c{font:500 12px "IBM Plex Mono",monospace;fill:var(--ink3)}
.row .l{font:13px "IBM Plex Sans",sans-serif;fill:var(--ink2)}
.row .d{font:11.5px "IBM Plex Mono",monospace;fill:var(--ink3)}
.row.done .c{fill:var(--done)} .row.done .l{fill:var(--ink)}
.row.skipped .l{fill:var(--ink3)}
.row.on rect{fill:var(--accentBg);stroke:var(--accent)}
.row.on .c,.row.on .l{fill:var(--accent);font-weight:600}
.row{cursor:pointer}
.spinebox{fill:var(--bg);stroke:var(--line)}
.edge{fill:none;stroke:var(--line);stroke-width:1.25;transition:stroke .35s,opacity .35s}
.edge.on{stroke:var(--accent);stroke-width:1.75;stroke-dasharray:5 6;animation:flow 0.9s linear infinite}
.edge.on.llm{stroke:var(--llm)}
@keyframes flow{to{stroke-dashoffset:-22}}
.pulse{fill:var(--accent)} .pulse.llm{fill:var(--llm)}
.detail{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:0;border-top:1px solid var(--line);min-height:206px}
.detail>div{padding:14px 16px;min-width:0}
.detail>div+div{border-left:1px solid var(--line)}
.h{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.h b{font:600 15px "IBM Plex Sans",sans-serif}
.chip{display:inline-block;font:500 11px "IBM Plex Mono",monospace;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--ink2)}
.chip.done{color:var(--done);background:var(--doneBg);border-color:transparent}
.chip.skipped{color:var(--ink2)} .chip.llm{color:var(--llm);background:var(--llmBg);border-color:transparent}
.chip.warn{color:var(--warn);background:var(--warnBg);border-color:transparent}
.mod{font:12px "IBM Plex Mono",monospace;color:var(--ink2);margin:2px 0 8px;word-break:break-word}
.sent{margin:0;color:var(--ink);overflow-wrap:anywhere}
.why{margin:0 0 12px;color:var(--ink);font:15px/1.6 "IBM Plex Sans",sans-serif;max-width:62ch}
.lbl.rec{margin-top:4px}
.sent{color:var(--ink2);font-size:13.5px}
.lbl{font:600 11px "IBM Plex Sans",sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--ink3);margin:0 0 6px}
ul{list-style:none;margin:0;padding:0;max-height:150px;overflow:auto}
li{display:flex;gap:10px;justify-content:space-between;font:12px "IBM Plex Mono",monospace;padding:4px 0;border-bottom:1px dashed var(--line)}
li span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
li span:last-child{color:var(--ink2);white-space:nowrap}
li.fail span:first-child{color:var(--warn)}
.empty{color:var(--ink3);font-size:13px}
html.fs,html.fs body{height:100%;background:var(--panel)}
html.fs .wrap{border-radius:0;border:0;display:flex;flex-direction:column;height:100vh;overflow:auto}
html.fs svg{flex:1 1 auto;min-height:0;max-height:calc(100vh - 280px);width:auto;max-width:100%;margin:0 auto}
html.fs .why{font-size:17px}
.wrap:fullscreen{border-radius:0;border:0;display:flex;flex-direction:column;height:100vh;overflow:auto;background:var(--panel)}
.wrap:fullscreen svg{flex:1 1 auto;min-height:0;max-height:calc(100vh - 280px);width:auto;max-width:100%;margin:0 auto}
.wrap:fullscreen .detail{flex:0 0 auto}
.wrap:fullscreen .why{font-size:17px}
@media (prefers-reduced-motion:reduce){.edge.on{animation:none}}
@media (max-width:720px){.detail{grid-template-columns:1fr}.detail>div+div{border-left:0;border-top:1px solid var(--line)}.meta{margin-left:0}}
</style></head><body>
<div class="wrap">
  <div class="bar">
    <button class="btn" id="prev" aria-label="Previous step">&#9664;</button>
    <button class="btn primary" id="play" aria-label="Play or pause">Play</button>
    <button class="btn" id="next" aria-label="Next step">&#9654;</button>
    <button class="btn" id="speed" aria-label="Playback speed">1×</button>
    <div class="steps" id="steps" role="tablist" aria-label="Pipeline stages"></div>
    <div class="meta" id="meta"></div>
    <button class="btn" id="fs" aria-label="Full screen" title="Full screen (F)">&#x26F6; Full screen</button>
  </div>
  <svg id="svg" viewBox="0 0 1200 600" role="img" aria-label="Architecture diagram"></svg>
  <div class="detail">
    <div><div class="h" id="dh"></div><div class="mod" id="dm"></div><p class="why" id="dw"></p><p class="lbl rec">What this case recorded</p><p class="sent" id="ds"></p></div>
    <div><p class="lbl" id="cl">Calls in this stage</p><ul id="calls"></ul></div>
  </div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const P = D.palette, root = document.documentElement.style;
for (const k in P) root.setProperty('--' + k, P[k]);
const NS = 'http://www.w3.org/2000/svg', svg = document.getElementById('svg');
svg.textContent = ''; document.getElementById('steps').textContent = '';  // a copy opened in its own tab starts clean
const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); (parent || svg).appendChild(e); return e; };
const txt = (s, attrs, parent) => { const e = el('text', attrs, parent); e.textContent = s; return e; };
const S = D.spine, stages = D.stages, codes = stages.map(s => s.code);

// columns
D.columns.forEach(([x, name]) => txt(name, {x, y: 38, class: 'colhead'}));
const edgeLayer = el('g', {});
const live = el('g', {});  // under the boxes: a flow never draws over a component
// spine (P0..P10)
const phases = stages.filter(s => s.code !== 'S0');
el('rect', {x: S.x, y: S.y, width: S.w, height: phases.length * S.row + 8, rx: 8, class: 'spinebox'});
const rowY = {};
phases.forEach((s, i) => {
  const y = S.y + 4 + i * S.row; rowY[s.code] = y + S.row / 2;
  const g = el('g', {class: 'row ' + s.state, id: 'row-' + s.code, tabindex: 0});
  el('rect', {x: S.x + 4, y: y + 2, width: S.w - 8, height: S.row - 4, rx: 5}, g);
  txt(s.code, {x: S.x + 16, y: y + S.row / 2 + 4, class: 'c'}, g);
  txt(s.label, {x: S.x + 56, y: y + S.row / 2 + 4, class: 'l'}, g);
  const dur = s.duration != null ? s.duration.toFixed(1) + ' s' : (s.state === 'skipped' ? 'skipped' : '');
  txt(dur, {x: S.x + S.w - 14, y: y + S.row / 2 + 4, class: 'd', 'text-anchor': 'end'}, g);
  g.addEventListener('click', () => go(codes.indexOf(s.code), true));
});
// nodes
const nodeEls = {};
for (const [id, n] of Object.entries(D.nodes)) {
  const g = el('g', {class: 'node' + (n.llm ? ' llm' : ''), id: 'node-' + id});
  el('rect', {x: n.x, y: n.y, width: n.w, height: n.h, rx: 8}, g);
  const cy = n.big ? n.y + 30 : n.y + n.h / 2 - 3;
  txt(n.title, {x: n.x + 14, y: cy, class: 't'}, g);
  txt(n.sub, {x: n.x + 14, y: cy + 18, class: 's'}, g);
  if (n.llm) txt('LLM', {x: n.x + n.w - 12, y: n.y + 18, class: 'tag', 'text-anchor': 'end', fill: P.llm});
  if (n.big) {  // a small graph motif, quiet
    const ox = n.x + 150, oy = n.y + 70, pts = [[0,0],[34,-18],[50,16],[18,34],[-14,30]];
    pts.forEach(([a,b],i) => pts.forEach(([c,d],j) => { if (j>i && (i+j)%2===1) el('line',{x1:ox+a,y1:oy+b,x2:ox+c,y2:oy+d,stroke:P.line,'stroke-width':1.2},g); }));
    pts.forEach(([a,b]) => el('circle',{cx:ox+a,cy:oy+b,r:5,fill:P.bg,stroke:P.ink3,'stroke-width':1.2},g));
  }
  nodeEls[id] = g;
}
// anchors
function port(id, side, code) {
  if (id === 'P') { const y = rowY[code]; return side === 'r' ? [S.x + S.w, y] : [S.x, y]; }
  const n = D.nodes[id];
  return side === 'r' ? [n.x + n.w, n.y + n.h / 2] : side === 'l' ? [n.x, n.y + n.h / 2] : side === 'b' ? [n.x + n.w / 2, n.y + n.h] : [n.x + n.w / 2, n.y];
}
function pathFor(a, b, code) {
  const A = a === 'P' ? null : D.nodes[a], B = b === 'P' ? null : D.nodes[b];
  let p1, p2;
  if (a === 'P' && B) { const left = B.x < S.x; p1 = port('P', left ? 'l' : 'r', code); p2 = port(b, left ? 'r' : 'l'); }
  else if (b === 'P' && A) { const left = A.x < S.x; p1 = port(a, left ? 'r' : 'l'); p2 = port('P', left ? 'l' : 'r', code); }
  else if (A.col === B.col) { const down = B.y > A.y; p1 = port(a, down ? 'b' : 't'); p2 = port(b, down ? 't' : 'b');
    return `M${p1[0]},${p1[1]} L${p2[0]},${p2[1]}`; }
  else { p1 = port(a, 'r'); p2 = port(b, 'l'); }
  const mx = (p1[0] + p2[0]) / 2;
  return `M${p1[0]},${p1[1]} C${mx},${p1[1]} ${mx},${p2[1]} ${p2[0]},${p2[1]}`;
}
// faint scaffold of every possible connection, so the flow has a map to run on
const seen = new Set();
stages.forEach(s => (s.edges || []).forEach(([a, b]) => {
  const k = a + b + (a === 'P' || b === 'P' ? s.code : '');
  if (!seen.has(k)) { seen.add(k); el('path', {d: pathFor(a, b, s.code), class: 'edge', opacity: 0.35}, edgeLayer); }
}));

let i = 0, timer = null, speeds = [1, 1.6, 0.6], si = 0;
const stepsEl = document.getElementById('steps');
stages.forEach((s, k) => {
  const b = document.createElement('button');
  b.className = 'step ' + s.state; b.textContent = s.code; b.setAttribute('role', 'tab');
  b.title = s.label + ' · ' + s.state; b.onclick = () => go(k, true); stepsEl.appendChild(b);
});
function fmtT(x) { return x == null ? '' : 't+' + Number(x).toFixed(1) + ' s'; }
function go(k, user) {
  if (user) stop();
  i = (k + stages.length) % stages.length;
  const s = stages[i];
  [...stepsEl.children].forEach((b, j) => b.classList.toggle('on', j === i));
  document.querySelectorAll('.row').forEach(r => r.classList.toggle('on', r.id === 'row-' + s.code));
  for (const id in nodeEls) nodeEls[id].classList.remove('lit');
  while (live.firstChild) live.firstChild.remove();
  (s.edges || []).forEach(([a, b]) => {
    [a, b].forEach(n => { if (n !== 'P' && nodeEls[n]) nodeEls[n].classList.add('lit'); });
    const isLlm = a === 'model' || b === 'model';
    const p = el('path', {d: pathFor(a, b, s.code), class: 'edge on' + (isLlm ? ' llm' : '')}, live);
    const dot = el('circle', {r: 3.5, class: 'pulse' + (isLlm ? ' llm' : '')}, live);
    const m = el('animateMotion', {dur: '1.4s', repeatCount: 'indefinite', path: p.getAttribute('d')}, dot);
    try { m.beginElement(); } catch (e) {}
  });
  // detail
  const h = document.getElementById('dh'); h.textContent = '';
  const b = document.createElement('b'); b.textContent = s.code + ' · ' + s.label; h.appendChild(b);
  const chip = (t, c) => { const x = document.createElement('span'); x.className = 'chip ' + (c || ''); x.textContent = t; h.appendChild(x); };
  chip(s.state === 'skipped' ? (s.state_label || 'skipped') : s.state, s.state === 'done' ? 'done' : s.state === 'stopped' ? 'warn' : s.state);
  if (s.duration != null) chip(s.duration.toFixed(2) + ' s');
  if (s.llm_calls.length) chip(s.llm_calls.length + ' model call' + (s.llm_calls.length > 1 ? 's' : ''), 'llm');
  document.getElementById('dm').textContent = s.module;
  document.getElementById('dw').textContent = s.why;
  document.getElementById('ds').textContent = s.sentence;
  const ul = document.getElementById('calls'); ul.textContent = '';
  const items = s.llm_calls.map(x => ({a: x.model, b: Number(x.tokens).toLocaleString() + ' tok' + (x.latency != null ? ' · ' + Number(x.latency).toFixed(1) + ' s' : ''), ok: true}))
    .concat(s.calls.map(c => ({a: c.name + (c.params ? ' ' + c.params : ''), b: c.latency != null ? Number(c.latency).toFixed(2) + ' s' : '', ok: c.ok})));
  document.getElementById('cl').textContent = items.length ? 'Calls in this stage (' + items.length + ')' : 'Calls in this stage';
  if (!items.length) { const li = document.createElement('li'); li.className = 'empty'; li.textContent = s.code === 'S0' ? 'Offline: no per-case call.' : 'No call recorded: ran in-process (engine or policy code).'; ul.appendChild(li); }
  items.forEach(x => { const li = document.createElement('li'); if (!x.ok) li.className = 'fail';
    const a = document.createElement('span'); a.textContent = x.a; a.title = x.a; const c = document.createElement('span'); c.textContent = x.b;
    li.append(a, c); ul.appendChild(li); });
  document.getElementById('meta').textContent = D.case + ' · step ' + (i + 1) + ' of ' + stages.length + (s.t != null ? ' · ' + fmtT(s.t) : '');
}
function stop() { clearInterval(timer); timer = null; document.getElementById('play').textContent = 'Play'; }
function play() { stop(); document.getElementById('play').textContent = 'Pause';
  timer = setInterval(() => { if (i === stages.length - 1) { stop(); return; } go(i + 1); }, 2200 / speeds[si]); }
document.getElementById('play').onclick = () => timer ? stop() : (i === stages.length - 1 && go(0), play());
document.getElementById('prev').onclick = () => go(i - 1, true);
document.getElementById('next').onclick = () => go(i + 1, true);
document.getElementById('speed').onclick = e => { si = (si + 1) % speeds.length; e.target.textContent = speeds[si] + '×'; if (timer) play(); };
document.addEventListener('keydown', e => { if (e.key === 'ArrowRight') go(i + 1, true); if (e.key === 'ArrowLeft') go(i - 1, true); if (e.key === ' ') { e.preventDefault(); document.getElementById('play').click(); } });
const wrap = document.querySelector('.wrap'), fsBtn = document.getElementById('fs');
// Full screen, tried in order: this document's own panel; the frame element in the app page (same origin, which a
// browser can allow when the frame itself may not); else the same diagram in a tab of its own.
const host = (() => { try { return window.frameElement && window.parent.document; } catch (e) { return null; } })();
const fsOn = () => !!(document.fullscreenElement || (host && host.fullscreenElement === window.frameElement));
function openTab() {
  const w = window.open(URL.createObjectURL(new Blob(['<!doctype html>' + document.documentElement.outerHTML], {type: 'text/html'})), '_blank');
  if (!w) alert('Allow pop-ups to open the full-screen view.');
}
function toggleFs() {
  if (document.fullscreenElement) return document.exitFullscreen();
  if (host && host.fullscreenElement) return host.exitFullscreen();
  const own = wrap.requestFullscreen ? wrap.requestFullscreen() : Promise.reject();
  own.catch(() => (host && window.frameElement.requestFullscreen ? window.frameElement.requestFullscreen() : Promise.reject())).catch(openTab);
}
function syncFs() {
  document.documentElement.classList.toggle('fs', fsOn());
  fsBtn.textContent = fsOn() ? '\u2715 Exit full screen' : '\u26F6 Full screen';
}
fsBtn.onclick = toggleFs;
document.addEventListener('fullscreenchange', syncFs);
if (host) host.addEventListener('fullscreenchange', syncFs);
document.addEventListener('keydown', e => { if (e.key === 'f' || e.key === 'F') toggleFs(); });
go(0); play();
</script></body></html>""".replace("__DATA__", data)


# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #
src, case_id = ctx.source, ctx.case_id
if src is None or not case_id:
    st.info("Pick a run and a case in the bar above to replay it.", icon=":material/info:")
    st.stop()

view = common.case_view(src.id, case_id)
rows = _stage_rows(view)
case = (view.answer or {}).get("case") or {} if view.ok else {}

head = [f"**{md(case_id)}**", md(f"run {src.id}")]
if case:
    head.append(badge(str(case.get("verdict") or "?"), common.VERDICT_COLORS.get(case.get("verdict"), "gray")))
    head.append(md(f"p {common.prob(case.get('fraud_probability'))}"))
if not view.has_trace:
    head.append(badge("no trace in this source", "orange"))
st.markdown(" · ".join(head))
if not view.has_trace:
    st.caption(md("This source holds answer files only, so the replay shows the architecture without timings or calls. "
                  "Pick a run in the bar above to replay a case's recorded trace."))

st.iframe(_diagram(f"{case_id} · {src.id}", rows), height="content")
st.caption(md("Play, pause or step with the controls, the stage buttons, a click on a phase, or the arrow keys. A stage lights up "
              "the components its recorded calls touched; model edges appear only where calls.jsonl records a model call, and "
              "graph edges only where it records a tool call. Durations are phases.jsonl's t."))
common.page_link("case", "Open this case's full decision path", icon=common.PAGE_ICONS["case"], case=case_id)

"""Graph: what is this case connected to, and is the agent's case in the graph?

The head names the case, its verdict and p, and where the drawing comes from in one plain sentence. Under it, the canvas
band: two-thirds canvas with its toolbar (legend chips that filter the vertex types, the counts, edge labels, hours in
live mode, download) and a right rail (written back, read back, the case's wave, the case facts, then the node
inspector). Folded below: the persist rows, the live read-back and graph health.

Live mode runs the installed query `case_subgraph(c, as_of, hours)` once per (card, as_of, hours) and caches it for
300 s, so clicking a node never re-queries. Mock mode, or an unreachable graph, draws the subgraph rebuilt from the
answer file and says so; it never opens a TigerGraph connection. The AgentCase is drawn solid only when the trace's
persist rows show it was written. Viewing never writes.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common, design  # noqa: E402

ctx = common.page_setup("graph")
if not ctx.case_id:
    common.page_header("Graph")
    st.info(common.md(f"No case to show: the source {ctx.source_id or '(none)'} has an empty case pack."))
    st.stop()

case_id = ctx.case_id
view = common.case_view(ctx.source_id, case_id)
meta = view.meta or common.case_meta(case_id)
answer = view.answer if view.ok else None
case = (answer or {}).get("case") or {}
gid = case.get("graph_case_id") or f"AC-{case_id}"


def _investigated_card(v: common.CaseView) -> tuple[str, dict | None, str]:
    """(card, mismatch, where): the transaction record's card when a trace logged a card_mismatch note for this case, else
    the case-pack card. Looks in this source's trace, then the run a submitted answer was promoted from, then any other
    run of the case (the mismatch is a fact of the data, not of one run)."""
    order = ([v.source_id] if v.has_trace else []) + list(v.promoted_from)
    order += [s.id for s in common.sources() if s.kind == "run" and s.id not in order]
    for sid in order:
        ov = v if sid == v.source_id else common.case_view(sid, case_id)
        if not ov.has_trace:
            continue
        for e in common.guardrail_events(ov):
            info = e.get("info") or {}
            if e["type"] == "card_mismatch" and info.get("txn_card"):
                return str(info["txn_card"]), e, sid
    return meta.get("card_id", ""), None, ""


card, mismatch, mismatch_src = _investigated_card(view)
as_of, flagged = meta.get("opened_at", ""), meta.get("flagged_txn_id", "")
live = ctx.mode == "live"
_answer_file = (view.dir or common.cases_dir()) / (f"{case_id}.json" if ctx.source_id in common.SUBMITTED else "answer.json")
try:  # relative to the repo without resolving links, so a linked runs/ still reads as runs/<source>/<case>/answer.json
    answer_path = str(_answer_file.relative_to(common.ROOT))
except ValueError:
    answer_path = common.rel(_answer_file)


# --------------------------------------------------------------------------- #
# Evidence helpers                                                            #
# --------------------------------------------------------------------------- #
def _written(v: common.CaseView) -> tuple[bool, str]:
    """Whether AC-<case> is in FraudGraph, from this source's own evidence: an ok persist upsert of the AgentCase in
    calls.jsonl, or written_to_graph on a submitted answer that was promoted from a run."""
    persist = [c for c in v.calls if c.get("caller") == "persist"]
    if persist:
        upserts = [c for c in persist if "upsertVertex(AgentCase)" in str(c.get("name", ""))
                   and (c.get("params") or {}).get("id", gid) == gid]
        if any(c.get("ok") for c in upserts):
            return True, f"persist upsertVertex(AgentCase) ok for {gid} in calls.jsonl"
        return False, f"no ok AgentCase upsert for {gid} among {len(persist)} persist rows"
    if v.source_id in common.SUBMITTED and v.promoted_from and (v.answer or {}).get("case", {}).get("written_to_graph"):
        return True, f"written_to_graph on the answer promoted from {', '.join(v.promoted_from)}"
    return False, "no persist rows in this trace"


@st.cache_data(ttl=300, show_spinner="Running case_subgraph on TigerGraph…")
def _live_subgraph(c: str, at: str, hours: int) -> dict:
    sub, err, latency = common.run_installed_safe("case_subgraph", {"c": c, "as_of": at, "hours": int(hours)})
    return {"sub": sub, "error": err, "latency_s": latency, "at": time.strftime("%Y-%m-%d %H:%M:%S")}


@st.cache_data(ttl=300, show_spinner="Counting FraudGraph vertices and edges…")
def _graph_health() -> dict:
    """Live vertex and edge counts against the delta manifest's expected_after, when that file exists here."""
    conn, err = common.graph_connection()
    if conn is None:
        return {"error": err}
    try:
        manifest = json.loads((common.ROOT / "data" / "out_delta" / "delta_manifest.json").read_text())
    except (OSError, ValueError):
        manifest = None  # local only: the deployed image has no data/out_delta
    expected = manifest.get("expected_after") if isinstance(manifest, dict) else None
    try:
        counts = {**(conn.getVertexCount("*") or {}), **(conn.getEdgeCount("*") or {})}
    except Exception as exc:  # noqa: BLE001 - shown as a dependency banner
        return {"error": f"{type(exc).__name__}: {exc}"}
    rows = [{"type": k, "graph": counts.get(k), "expected": v, "match": counts.get(k) == v} for k, v in (expected or {}).items()]
    return {"error": None, "counts": counts, "rows": rows, "has_manifest": expected is not None, "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _read_back() -> dict:
    """AC-<case> as FraudGraph holds it right now: the customer's agent cases and the AgentCase vertex itself."""
    cust = meta.get("customer_id", "")
    try:
        after = (datetime.strptime(as_of, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        after = "2099-01-01 00:00:00"
    res, err, latency = common.run_installed_safe("prior_cases_for_customer", {"cu": cust, "as_of": after})
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": err, "latency_s": latency,
           "agent_cases": [r for r in (res or {}).get("@@agent_cases", []) if isinstance(r, dict)], "vertex": None}
    conn, cerr = common.graph_connection()
    if conn is None:
        out["error"] = out["error"] or cerr
        return out
    try:
        found = conn.getVerticesById("AgentCase", gid)
        out["vertex"] = found[0] if found else {}
    except Exception as exc:  # noqa: BLE001
        out["error"] = "; ".join(x for x in (out["error"], f"getVerticesById: {type(exc).__name__}: {exc}") if x)
    return out


def _wave() -> dict | None:
    """The detector whose backtest lists this case among its exam hits (engine/out/detector_backtest.json)."""
    bt = common.proof("backtest")
    data = bt["data"] if isinstance(bt["data"], dict) else {}
    for d in data.get("detectors") or []:
        if case_id in (d.get("exam_cases_caught") or []):
            return {**d, "_proof": bt}
    for det_id, ids in (data.get("exam_scan") or {}).items():
        if case_id in ids:
            return {"detector_id": det_id, "_proof": bt}
    return None if bt["data"] is not None else {"_missing": bt["error"], "_proof": bt}


# --------------------------------------------------------------------------- #
# The subgraph: live, cached, else rebuilt from the answer                    #
# --------------------------------------------------------------------------- #
written, written_why = _written(view)
hours = int(st.session_state.get("graph_hours", 72))  # the toolbar's widget value, read first so the head can name the query

sub: dict | None = None
res: dict | None = None
graph_down = False
if live and card and as_of:
    res = _live_subgraph(card, as_of, hours)
    if res["error"]:
        graph_down = True
    elif (res["sub"] or {}).get("nodes"):
        sub = res["sub"]
from_graph = sub is not None
if sub is None and answer is not None:
    sub = common.subgraph_from_answer(answer, {**meta, "card_id": card}, written=written)


def _mono(text: object) -> str:
    return f'<span class="mono">{design.esc(text)}</span>'


# --------------------------------------------------------------------------- #
# Head: title, verdict and p, then where the drawing comes from               #
# --------------------------------------------------------------------------- #
VERDICT_WORD = {"fraud": "Fraud", "uncertain": "Uncertain", "legitimate": "Legitimate"}
if from_graph:
    lat = res["latency_s"]
    prov = (f"Drawn from TigerGraph {_mono(f'case_subgraph(c={card}, as_of={as_of}, hours={hours})')}"
            + (f" · {lat:.2f} s" if isinstance(lat, int | float) else "") + f" · queried at {design.esc(res['at'])}")
elif sub is not None:
    why = ("TigerGraph not queried (mock)" if not live else "TigerGraph unreachable" if graph_down
           else f"case_subgraph returned no nodes for card {card}" if res else "no card or opened_at to query TigerGraph with")
    prov = f"Drawn from the answer file {_mono(answer_path)}; {design.esc(why)}"
else:
    prov = design.esc(f"{case_id} has no answer in {ctx.source_id} ({common.STATE_LABELS.get(view.state, view.state)}): nothing to draw the "
                      "subgraph from" + ("" if live else "; TigerGraph not queried (mock)"))
if answer is not None:
    verdict = str(case.get("verdict") or "")
    head_state = (f'<span class="tc-verdict {design.esc(verdict)}"><span class="tc-dot {design.esc(verdict or "none")}"></span>'
                  f'{design.esc(VERDICT_WORD.get(verdict, "No verdict"))}</span>'
                  f'<span class="gr-p">p <b>{design.esc(common.prob(case.get("fraud_probability")))}</b></span>')
else:  # no parsed answer: its state only, never a verdict
    head_state = design.tag(common.STATE_LABELS.get(view.state, view.state))
with st.container(horizontal=True, vertical_alignment="center", key="gr-head"):
    design.html_block(f'<div class="tc gr-title"><h1>Graph · {design.esc(case_id)}</h1>{head_state}</div>')
    common.page_link("case", "Open case file", icon=":material/folder_open:", case=case_id)
note = ""
if mismatch:
    info = mismatch["info"]
    where = "" if mismatch_src == ctx.source_id else f" (recorded in the {mismatch_src} trace)"
    text = (f"{mismatch['text']}: case card {info.get('pack_card', '?')}, transaction record card {info.get('txn_card', '?')}{where}; "
            f"drawn around {card}")
    note = f'<p class="gr-note" title="{design.esc(text)}">{design.glyph("card")}{design.esc(text)}</p>'
design.html_block(f'<div class="tc gr-prov"><p>{prov}</p>{note}</div>')
if graph_down and common.dependency_banner("TigerGraph", res["error"], since=common.graph_status().get("since") or None, retry_key="graph_retry"):
    common.graph_retry()
    _live_subgraph.clear()
    _graph_health.clear()
    st.rerun()


# --------------------------------------------------------------------------- #
# Canvas, inspector and legend                                                #
# --------------------------------------------------------------------------- #
try:
    DARK = st.context.theme.type == "dark"
except Exception:  # noqa: BLE001 - no theme context in bare mode
    DARK = False
# The typology vertex palette: the petrol family and calm neutrals, never the state red / ochre / green, so colour on the
# canvas only tells vertex types apart (the shape repeats the cue). Belongs in common beside NODE_COLORS.
VERTEX_PALETTE = {
    "light": {"AgentCase": "#0f3b57", "Card": "#2b7bb0", "DeviceProfile": "#1d5f7a", "Customer": "#5b6670",
              "Transaction": "#8fb3cc", "EmailDomain": "#6f7c88", "BillingRegion": "#8a95a0", "ClosedCase": "#a3afb9"},
    "dark": {"AgentCase": "#8cc3e0", "Card": "#4fa3d9", "DeviceProfile": "#6fb0c9", "Customer": "#9aa6b1",
             "Transaction": "#3d6b8a", "EmailDomain": "#7b8a97", "BillingRegion": "#5d6b77", "ClosedCase": "#4a5b68"},
}
COLORS = VERTEX_PALETTE["dark" if DARK else "light"]
FONT = "'Public Sans', -apple-system, Helvetica, Arial, sans-serif"
# ink, paper, muted and petrol from ui/design.py TOKENS; the selection ring is petrol, the console's focus colour
INK, PAPER, SUBTLE, SELECT = ("#e3eaef", "#0c1a23", "#93a7b6", "#8cc3e0") if DARK else ("#14212b", "#ffffff", "#55687a", "#0f3b57")
MUTED = SUBTLE
NODE_STYLE = {  # (vis shape, size); the shape repeats the colour cue so the legend reads without colour
    "AgentCase": ("diamond", 28), "Card": ("dot", 24), "DeviceProfile": ("square", 20), "Customer": ("dot", 20),
    "EmailDomain": ("triangle", 17), "BillingRegion": ("triangle", 17), "ClosedCase": ("dot", 14), "Transaction": ("dot", 14),
}
TYPE_GLYPH = {"AgentCase": "agent_case", "Card": "card", "Transaction": "transaction", "DeviceProfile": "device", "Customer": "customer",
              "ClosedCase": "closed_case", "EmailDomain": "email", "BillingRegion": "region"}
LABEL_SIZE = 14  # one label size on the canvas, nodes and edges alike
EDGE_COLORS = {  # memory links dashed
    "MADE": COLORS["Transaction"], "FROM_DEVICE": COLORS["DeviceProfile"], "SHARES_DEVICE": COLORS["DeviceProfile"],
    "PURCHASER_EMAIL": COLORS["EmailDomain"], "BILLED_IN": COLORS["BillingRegion"], "CASE_ON_CARD": COLORS["AgentCase"],
    "CASE_INVOLVES": COLORS["AgentCase"], "CASE_CONNECTED_TO": COLORS["AgentCase"],
}
DASHED_EDGES = {"CASE_SIMILAR_TO", "ON_CARD"}


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _node_label(n: dict, detailed: bool) -> str:
    """Two short lines on the canvas: the id, then what it is (canvas text, never parsed as markdown or HTML)."""
    nid, desc = str(n["id"]), str(n.get("label") or "")
    if " | " in nid:  # device profiles are keyed by the full "model | os | browser | screen" string
        head, rest = nid.split(" | ", 1)
        tail = " · ".join(rest.split(" | ")[:2])
    else:
        head, tail = nid, (desc[len(nid):].strip(" ()") if desc.startswith(nid) else desc)
    return f"{_short(head, 22)}\n{_short(tail, 26)}" if detailed and tail else _short(head, 22)


def _glyph_uri(name: str) -> str:
    """A legend glyph as an SVG data URI, for a CSS mask (the glyph set is fixed markup, never data)."""
    m = re.search(r"<svg[^>]*>(.*)</svg>", design.glyph(name), re.S)
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='black' stroke-width='1.5' "
           f"stroke-linecap='round' stroke-linejoin='round'>{m.group(1) if m else ''}</svg>")
    return "data:image/svg+xml," + quote(svg, safe="")


def _legend_css(types: list[str]) -> str:
    """Each legend chip (a pill of the type filter, in `types` order) gets its type's glyph in its palette colour, as a
    light-dark() pair so both themes hold."""
    rules = []
    for i, t in enumerate(types, 1):
        uri = _glyph_uri(TYPE_GLYPH.get(t, "dash"))
        lc, dc = VERTEX_PALETTE["light"].get(t, "#55687a"), VERTEX_PALETTE["dark"].get(t, "#93a7b6")
        rules.append(f'[class*="st-key-graph_types_"] button:nth-of-type({i})::before{{background-color:light-dark({lc},{dc});'
                     f'-webkit-mask-image:url("{uri}");mask-image:url("{uri}");}}')
    return "<style>" + "".join(rules) + "</style>"


def _row(label: str, value: str, title: str = "") -> str:
    """One rail row: a muted label and its value (markup built with design.esc)."""
    tip = f' title="{design.esc(title)}"' if title else ""
    return f'<div class="gr-row"{tip}><span class="k">{design.esc(label)}</span><span class="v">{value}</span></div>'


DASH = '<span class="gr-dash">—</span>'


# --------------------------------------------------------------------------- #
# The canvas band: canvas with its toolbar, and the right rail                #
# --------------------------------------------------------------------------- #
wave = _wave()
n_total = len(sub["nodes"]) if sub is not None else 0
detailed, dense = n_total <= 60, n_total > 14
height = 600 if dense else 460
canvas_col, rail_col = st.columns([2, 1], gap="medium")
clicked = None
nodes: list[dict] = []
edges: list[dict] = []
node_by_id: dict = {}
if sub is not None:
    counts_all = Counter(common.vtype(n) for n in sub["nodes"])
    types_all = sorted(counts_all)
    with canvas_col:
        st.markdown(f"<style>.st-key-graph_panel {{background: {PAPER};}}</style>", unsafe_allow_html=True)  # theme colour only, no data
        design.html_block(_legend_css(types_all))
        panel = st.container(border=True, key="graph_panel")
    with panel:
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="graph-legend"):
            keep = st.pills("Vertex types", types_all, selection_mode="multi", default=types_all, key=f"graph_types_{case_id}",
                            format_func=lambda t: f"{t} {counts_all[t]}", label_visibility="collapsed") or []
            nodes = [n for n in sub["nodes"] if common.vtype(n) in keep]
            ids = {n["id"] for n in nodes}
            node_by_id = {n["id"]: n for n in nodes}
            seen: set[tuple] = set()
            for e in sub.get("edges") or []:
                k = (e.get("src"), e.get("dst"), common.etype(e))
                if k[0] in ids and k[1] in ids and k not in seen:
                    seen.add(k)
                    edges.append(e)
            memory = any(common.etype(e) in DASHED_EDGES for e in edges)
            design.html_block(f'<div class="tc gr-count">{len(nodes)} nodes · {len(edges)} edges'
                              + (" · dashed: case memory" if memory else "") + "</div>")
            edge_labels = st.toggle("Edge labels", value=False, key="graph_edge_labels", help="Relationship names always show on hover.")
            if live:
                st.number_input("Hours before opened_at", 1, 24 * 30, 72, key="graph_hours", format="%d h", width=104,
                                label_visibility="collapsed", help="Window of case_subgraph before opened_at")
            st.download_button("JSON", json.dumps(sub, indent=1, default=str), file_name=f"{case_id}_subgraph.json", mime="application/json",
                               on_click="ignore", icon=":material/download:", type="tertiary", help="Download this subgraph as JSON")
    focus = {card, flagged} - {""}  # focus borders: the investigated card and the flagged transaction only

    vis_nodes = []
    for n in nodes:
        t = common.vtype(n)
        shape, size = NODE_STYLE.get(t, ("dot", 16))
        c = COLORS.get(t, MUTED)
        vn = {"id": n["id"], "label": _node_label(n, detailed), "shape": shape, "size": 16 if dense and t == "Card" and n["id"] != card else size,
              "borderWidth": 4 if n["id"] in focus else 2,
              "color": {"background": c, "border": SELECT if n["id"] in focus else PAPER,
                        "highlight": {"background": c, "border": SELECT}, "hover": {"background": c, "border": SELECT}}}
        if t == "AgentCase" and not (n.get("written") if "written" in n else written):
            # not in FraudGraph: an outline only, dashed, so it never reads as a written vertex
            vn["color"] = {"background": PAPER, "border": c, "highlight": {"background": PAPER, "border": SELECT}, "hover": {"background": PAPER, "border": SELECT}}
            vn["shapeProperties"] = {"borderDashes": [6, 4]}
            vn["borderWidth"] = 3
        vis_nodes.append(vn)
    vis_edges = []
    for e in edges:
        t = common.etype(e)
        ve = {"source": e["src"], "target": e["dst"], "title": t, "dashes": t in DASHED_EDGES,
              "color": {"color": EDGE_COLORS.get(t, MUTED), "highlight": SELECT, "hover": EDGE_COLORS.get(t, MUTED), "opacity": 0.9}}
        if edge_labels:
            ve["label"] = t
        vis_edges.append(ve)
    vis_options = {
        "autoResize": True,
        "nodes": {"font": {"face": FONT, "size": LABEL_SIZE, "color": INK, "strokeWidth": 4, "strokeColor": PAPER}, "borderWidthSelected": 3},
        "edges": {"width": 1.4, "selectionWidth": 1.5, "hoverWidth": 0.8, "arrows": {"to": {"enabled": True, "scaleFactor": 0.45}},
                  "smooth": {"enabled": True, "type": "continuous", "roundness": 0.2},
                  "font": {"face": FONT, "size": LABEL_SIZE, "color": SUBTLE, "strokeWidth": 3, "strokeColor": PAPER, "align": "middle"}},
        "physics": {"enabled": len(nodes) < 150, "solver": "barnesHut",
                    "barnesHut": {"gravitationalConstant": -7000 if dense else -9000, "centralGravity": 0.3 if dense else 0.25,
                                  "springLength": 150 if dense else 170, "springConstant": 0.04 if dense else 0.035, "damping": 0.35,
                                  "avoidOverlap": 0.8 if dense else 0.5},
                    "stabilization": {"enabled": True, "iterations": 600, "fit": True}, "minVelocity": 0.75},
        "layout": {"randomSeed": 7, "improvedLayout": True, "hierarchical": {"enabled": False}},
        # zoomView off: the mouse wheel scrolls the page; the stabilised layout is fitted to the canvas. vis's navigation
        # buttons are bitmaps inside the component's frame that the console's theme cannot reach, so they are off.
        "interaction": {"hover": True, "tooltipDelay": 120, "zoomView": False, "dragView": True, "navigationButtons": False, "keyboard": False},
    }

    with panel:
        with st.container(key="gr-canvas"):
            try:
                from streamlit_agraph import Config, Edge, Node, agraph
            except ImportError:
                agraph = None
            if agraph is not None:
                gnodes = []
                for vn in vis_nodes:
                    gn = Node(**vn)
                    gn.__dict__.pop("title", None)  # the component opens a node's title as a URL on double-click
                    gnodes.append(gn)
                gedges = [Edge(**{k: v for k, v in ve.items() if k != "color"}, color=ve["color"]) for ve in vis_edges]
                cfg = Config(directed=True, physics=len(nodes) < 150, hierarchical=False)
                cfg.__dict__.update(vis_options, width="100%", height=f"{height}px")  # Config appends "px" to width, so set it directly
                cfg.__dict__.pop("groups", None)  # Config defaults groups=None, which vis rejects on every redraw
                clicked = agraph(nodes=gnodes, edges=gedges, config=cfg)
            else:  # automatic fallback only: streamlit-agraph is not installed here
                from pyvis.network import Network

                net = Network(height=f"{height}px", width="100%", directed=True, notebook=False, cdn_resources="in_line", bgcolor=PAPER, font_color=INK)
                for vn in vis_nodes:
                    net.add_node(vn["id"], label=vn["label"], shape=vn["shape"], size=vn["size"], color=vn["color"], borderWidth=vn["borderWidth"],
                                 shapeProperties=vn.get("shapeProperties", {}), title=f"{common.vtype(node_by_id[vn['id']])}: {vn['id']}")
                for ve in vis_edges:
                    net.add_edge(ve["source"], ve["target"], title=ve["title"], dashes=ve["dashes"], color=ve["color"], **({"label": ve["label"]} if "label" in ve else {}))
                net.set_options(json.dumps(vis_options))
                st.caption("streamlit-agraph is not installed here: drawn with pyvis, so node clicks do not reach the inspector.")
                st.iframe(net.generate_html().replace("border: 1px solid lightgray;", "border: none;"), height=height + 20)


# --------------------------------------------------------------------------- #
# The right rail: written back, read back, wave, case facts, node inspector   #
# --------------------------------------------------------------------------- #
hits: list[tuple[str, object]] = []  # measured vector read-back: other traces in this source whose P1 retrieved this case
if ctx.source_id not in common.SUBMITTED:
    for other, ov in common.source_views(ctx.source_id).items():
        if other == case_id or not ov.has_trace:
            continue
        for h in common.tg_usage(ov)["vector_hits"]:
            if h.get("id") == gid:
                hits.append((other, h.get("distance")))

with rail_col:
    if written:
        how = "persist ok" if any(c.get("caller") == "persist" for c in view.calls) else "written_to_graph"
        w_val = f'<span class="gr-sw"></span>{_mono(gid)} · {how}<span class="sub">a dashed outline means not written</span>'
    else:
        w_val = f'<span class="gr-sw unwritten"></span>{_mono(gid)} not written<span class="sub">{design.esc(written_why)}; drawn as a dashed outline</span>'
    if ctx.source_id in common.SUBMITTED:
        rb_val = '<span class="quiet">the submitted answers carry no trace</span>'
    elif hits:
        rb_val = "".join(f'<span class="ln">by <b>{design.esc(o)}</b> at distance '
                         + (f"{d:.3f}" if isinstance(d, int | float) else "unrecorded") + "</span>" for o, d in hits)
    else:
        rb_val = '<span class="quiet">not retrieved by another case in this source</span>'
    bt = (wave or {}).get("_proof") or {}
    if wave and wave.get("_missing"):
        wv_val = f'<span class="quiet">unknown: {design.esc(wave["_missing"])}</span>'
    elif wave:
        n_cards = wave.get("cards_matched")
        wv_val = f'<b>{design.esc(str(wave["detector_id"]).split("_", 1)[0])}</b>' + (f" · {design.esc(n_cards)} cards" if n_cards is not None else "")
        if wave.get("name"):
            wv_val += f'<span class="sub">{design.esc(wave["name"])}</span>'
        acted = wave.get("legitimate_customers_acted_against") or []
        if acted:
            cleared = ", ".join(map(str, wave.get("cleared_cases_matched") or []))
            wv_val += (f'<span class="sub red">also acts against cleared customer {design.esc(", ".join(map(str, acted)))}'
                       + (f" ({design.esc(cleared)})" if cleared else "") + "</span>")
    else:
        wv_val = "none detected · nothing to contain"
    with st.container(key="graph-wave"):
        design.html_block('<div class="tc gr-rail">'
                          + _row("Written back", w_val, written_why)
                          + _row("Read back", rb_val, "P1 similar_prior_cases of the other traces in this source")
                          + _row("Wave", wv_val, f"detector-attested · {bt.get('source', '')} · as of {bt.get('as_of', '')}" if bt else "")
                          + "</div>")
        common.page_link("detectors", "Detectors & containment", icon=":material/radar:", case=case_id,
                         help="containment proposal, ring replay and the detector backtest for this case's wave")
    cust = meta.get("customer_id", "")
    card_val = (_mono(card) + (f'<span class="sub">case card {design.esc(meta.get("card_id", "?"))}</span>' if mismatch else "")) if card else DASH
    design.html_block('<div class="tc gr-rail gr-facts">'
                      + _row("Card", card_val) + _row("Customer", _mono(cust) if cust else DASH)
                      + _row("Opened", design.esc(as_of) if as_of else DASH) + _row("Flagged txn", _mono(flagged) if flagged else DASH)
                      + "</div>")
    if sub is not None:
        with st.container(key="gr-inspector"):
            sel = clicked if clicked in node_by_id and clicked != st.session_state.get("gr_cleared") else None
            if sel is None:
                design.html_block('<div class="tc gr-insp-head">Node inspector</div><p class="tc gr-empty">No node selected</p>')
            else:
                st.session_state["gr_cleared"] = None  # a new selection: the next Clear applies to it
                n, t = node_by_id[sel], common.vtype(node_by_id[sel])
                with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                    design.html_block(f'<div class="tc gr-insp-head">{design.glyph(TYPE_GLYPH.get(t, "dash"))}{design.esc(t)}</div>')
                    if st.button("Clear selection", key="gr-clear", icon=":material/close:", type="tertiary"):
                        st.session_state["gr_cleared"] = sel
                        st.rerun()
                links = []
                for e in edges:
                    if sel in (e["src"], e["dst"]):
                        out = e["src"] == sel
                        other = e["dst"] if out else e["src"]
                        links.append(f'<li><span class="d">{"→" if out else "←"}</span><code>{design.esc(common.etype(e))}</code>'
                                     f'<span>{design.esc(_short(str(other).split(" | ")[0], 30))} '
                                     f'<span class="t">{design.esc(common.vtype(node_by_id.get(other, {})))}</span></span></li>')
                items = "".join(links) or '<li class="gr-empty">none in this view</li>'
                desc = f'<div class="gr-insp-desc">{design.esc(n["label"])}</div>' if n.get("label") and str(n["label"]) != str(n["id"]) else ""
                design.html_block(f'<div class="tc gr-insp"><div class="gr-insp-id">{design.esc(n["id"])}</div>{desc}'
                                  f'<div class="gr-insp-n">Connections {len(links)}</div>'
                                  f'<ul class="gr-links" style="max-height:{int(height * 0.42)}px">{items}</ul></div>')


# --------------------------------------------------------------------------- #
# Folded below: persist rows, the live read-back and graph health             #
# --------------------------------------------------------------------------- #
with st.container(key="graph-writeback"):
    usage = common.tg_usage(view)
    with st.expander(f"Persist rows for {common.md(gid)} ({len(usage['persist'])})"):
        if usage["persist"]:
            st.dataframe([{"call": r["name"], "ok": r["ok"], "latency_s": r["latency_s"]} for r in usage["persist"]], hide_index=True,
                         height=35 * min(len(usage["persist"]), 8) + 38,
                         column_config={"call": st.column_config.TextColumn("Call"), "ok": st.column_config.CheckboxColumn("ok"),
                                        "latency_s": st.column_config.NumberColumn("Latency", format="%.2f s")})
        else:
            st.caption("No persist rows in this source's calls.jsonl")
    with st.expander("Read-back and graph health"):
        if not live:  # said once: both need a TigerGraph connection, which mock mode never opens
            st.caption(common.md(f"Both query TigerGraph, which this console does not open ({ctx.mode_reason})"))
        else:
            if st.button(f"Read back {gid} now", icon=":material/sync:", key="graph_readback"):
                st.session_state[f"_readback_{gid}"] = _read_back()
            rb = st.session_state.get(f"_readback_{gid}")
            if rb:
                lat = rb["latency_s"]
                st.caption(common.md(f"graph now — a later run may have overwritten it · read at {rb['at']}"
                                     + (f" · {lat:.2f} s" if isinstance(lat, int | float) else "")))
                if rb["error"]:
                    common.dependency_banner("TigerGraph", rb["error"])
                mine = [r for r in rb["agent_cases"] if r.get("id") == gid]
                st.markdown(common.badge(f"prior_cases_for_customer lists {gid}" if mine else f"prior_cases_for_customer does not list {gid}",
                                         "green" if mine else "orange"))
                if rb["vertex"] is not None:
                    st.caption(common.md(f"AgentCase {gid} attributes"))
                    st.json(rb["vertex"] or {"found": False}, expanded=False)
            if graph_down:  # one banner per outage: the head already carries it, with Retry
                since = common.graph_status().get("since")
                st.caption(common.md("Graph health not checked: TigerGraph unreachable" + (f" since {since}" if since else "")))
            else:
                health = _graph_health()
                with st.container(key="graph-health"):
                    if health.get("error"):
                        common.dependency_banner("TigerGraph", health["error"])
                    elif health["has_manifest"]:
                        ok = sum(r["match"] for r in health["rows"])
                        common.measured("FraudGraph counts match the delta manifest", f"{ok}/{len(health['rows'])}",
                                        source="TigerGraph getVertexCount / getEdgeCount vs data/out_delta/delta_manifest.json expected_after",
                                        as_of=f"checked at {health['at']}", how="exact equality per vertex and edge type")
                        mismatched = [r for r in health["rows"] if not r["match"]]
                        if mismatched:
                            st.dataframe(mismatched, hide_index=True, height=35 * len(mismatched) + 38)
                    else:
                        st.caption(common.md(f"{sum(v for v in health['counts'].values() if isinstance(v, int)):,} vertices and edges in FraudGraph at "
                                             f"{health['at']}; no data/out_delta/delta_manifest.json here to check them against"))

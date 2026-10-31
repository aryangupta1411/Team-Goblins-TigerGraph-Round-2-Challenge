"""Graph: the case-centred TigerGraph proof.

Live mode runs the installed query `case_subgraph(c, as_of, hours)` once per (card, as_of, hours) and caches it for
300 s, so clicking a node never re-queries. Mock mode, or an unreachable graph, draws the subgraph rebuilt from the
answer file and says so; it never opens a TigerGraph connection. The AgentCase is drawn solid only when the trace's
persist rows show it was written. Below the canvas: the write-back and read-back evidence and the case's wave, with a
link to Detectors & containment, which holds the containment proposal and the ring replay.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

ctx = common.page_setup("graph")
if not ctx.case_id:
    common.page_header("FraudGraph", eyebrow="TigerGraph · vector + traversal · write-back and read-back")
    st.info(common.md(f"No case to show: the source {ctx.source_id or '(none)'} has an empty case pack."))
    st.stop()

case_id = ctx.case_id
view = common.case_view(ctx.source_id, case_id)
inbox, _ = common.inbox_rows(case_id)
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

common.page_header(f"FraudGraph · {case_id}", eyebrow="TigerGraph · vector + traversal · write-back and read-back")
strip, link = st.columns([5, 1], vertical_alignment="center")
with strip:
    common.decision_card(view, inbox, compact=True)
with link:
    common.page_link("case", "Open case file", icon=":material/folder_open:", case=case_id)
if mismatch:
    info = mismatch["info"]
    where = "" if mismatch_src == ctx.source_id else f" (recorded in the {mismatch_src} trace)"
    st.warning(common.md(f"{mismatch['text']}: case card {info.get('pack_card', '?')}, transaction record card {info.get('txn_card', '?')}{where}. "
                         f"This graph is drawn around {card}."), icon=":material/compare_arrows:")


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
chips = st.container(horizontal=True, vertical_alignment="center", gap="small")
with chips:
    options = st.popover("View options", icon=":material/tune:")
with options:
    hours = st.number_input("Hours before opened_at", 1, 24 * 30, 72, key="graph_hours", disabled=not live,
                            help="Window of case_subgraph. Live mode only; the answer-derived view has no window.")

sub: dict | None = None
graph_down = False
if live and card and as_of:
    res = _live_subgraph(card, as_of, int(hours))
    if res["error"]:
        graph_down = True
        if common.dependency_banner("TigerGraph", res["error"], since=common.graph_status().get("since") or None, retry_key="graph_retry"):
            common.graph_retry()
            _live_subgraph.clear()
            _graph_health.clear()
            st.rerun()
    elif not (res["sub"] or {}).get("nodes"):
        st.warning(common.md(f"case_subgraph returned no nodes for card {card}; showing the answer-derived view"))
    else:
        sub = res["sub"]
        common.chip(f"TigerGraph case_subgraph(c={card}, as_of={as_of}, hours={int(hours)}) · {len(sub['nodes'])} nodes / "
                    f"{len(sub.get('edges') or [])} edges · {res['latency_s']:.2f} s · {res['at']}", "green", icon=":material/hub:", where=chips)
if sub is None:
    if answer is None:
        st.info(common.md(f"{case_id} has no answer in {ctx.source_id} ({common.STATE_LABELS.get(view.state, view.state)}): "
                          "there is nothing to rebuild the subgraph from" + ("." if live else ", and the graph is not queried in mock mode.")))
    else:
        sub = common.subgraph_from_answer(answer, {**meta, "card_id": card}, written=written)
        label = f"Graph unavailable — rebuilt from {answer_path}" if live else f"Rebuilt from {answer_path} — graph not queried (mock)"
        common.chip(f"{label} · {len(sub['nodes'])} nodes / {len(sub['edges'])} edges", "orange" if live else "gray",
                    icon=":material/description:", where=chips)

if live and not graph_down:  # one banner per outage: the subgraph's already says the graph is unreachable
    health = _graph_health()
    with st.container(border=True, key="graph-health"):
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
else:
    st.caption(common.md(f"Graph checks need live mode ({ctx.mode_reason})"))


# --------------------------------------------------------------------------- #
# Canvas, inspector and legend                                                #
# --------------------------------------------------------------------------- #
try:
    DARK = st.context.theme.type == "dark"
except Exception:  # noqa: BLE001 - no theme context in bare mode
    DARK = False
COLORS = common.node_colors()
FONT = "'IBM Plex Sans', -apple-system, Helvetica, Arial, sans-serif"
# ink and paper from .streamlit/config.toml; the selection ring is ink too, so colour on the canvas stays on the vertex types
INK, PAPER, SUBTLE, SELECT = ("#e9e7e2", "#121314", "#9a978f", "#e9e7e2") if DARK else ("#18191c", "#fbfaf7", "#5f5d57", "#18191c")
MUTED = SUBTLE
NODE_STYLE = {  # (vis shape, size); the shape repeats the colour cue so the legend reads without colour
    "AgentCase": ("diamond", 28), "Card": ("dot", 24), "DeviceProfile": ("square", 20), "Customer": ("dot", 20),
    "EmailDomain": ("triangle", 17), "BillingRegion": ("triangle", 17), "ClosedCase": ("dot", 14), "Transaction": ("dot", 14),
}
SHAPE_GLYPH = {"dot": "●", "diamond": "◆", "square": "■", "triangle": "▲"}
EDGE_COLORS = {  # device links in red because they carry the ring signal; memory links dashed
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


wave = _wave()
if sub is not None:
    types_all = sorted({common.vtype(n) for n in sub["nodes"]})
    with options:
        keep = st.multiselect("Vertex types", types_all, default=types_all, key=f"graph_types_{case_id}")
        edge_labels = st.toggle("Edge labels", value=False, key="graph_edge_labels", help="Relationship names always show on hover.")
    with chips:
        st.download_button("Download subgraph (JSON)", json.dumps(sub, indent=1, default=str), file_name=f"{case_id}_subgraph.json",
                           mime="application/json", on_click="ignore", icon=":material/download:", type="tertiary")

    nodes = [n for n in sub["nodes"] if common.vtype(n) in keep]
    ids = {n["id"] for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    seen: set[tuple] = set()
    edges = []
    for e in sub.get("edges") or []:
        k = (e.get("src"), e.get("dst"), common.etype(e))
        if k[0] in ids and k[1] in ids and k not in seen:
            seen.add(k)
            edges.append(e)
    focus = {card, flagged, *(case.get("connected_card_ids") or [])} - {""}
    detailed, dense = len(nodes) <= 60, len(nodes) > 20
    height = 520 if dense else 460

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
        "nodes": {"font": {"face": FONT, "size": 18 if dense else 13, "color": INK, "strokeWidth": 4, "strokeColor": PAPER}, "borderWidthSelected": 3},
        "edges": {"width": 1.4, "selectionWidth": 1.5, "hoverWidth": 0.8, "arrows": {"to": {"enabled": True, "scaleFactor": 0.45}},
                  "smooth": {"enabled": True, "type": "continuous", "roundness": 0.2},
                  "font": {"face": FONT, "size": 10, "color": SUBTLE, "strokeWidth": 3, "strokeColor": PAPER, "align": "middle"}},
        "physics": {"enabled": len(nodes) < 150, "solver": "barnesHut",
                    "barnesHut": {"gravitationalConstant": -7000 if dense else -9000, "centralGravity": 0.3 if dense else 0.25,
                                  "springLength": 150 if dense else 170, "springConstant": 0.04 if dense else 0.035, "damping": 0.35,
                                  "avoidOverlap": 0.8 if dense else 0.5},
                    "stabilization": {"enabled": True, "iterations": 600, "fit": True}, "minVelocity": 0.75},
        "layout": {"randomSeed": 7, "improvedLayout": True, "hierarchical": {"enabled": False}},
        # zoomView off: the mouse wheel scrolls the page, the navigation buttons zoom
        "interaction": {"hover": True, "tooltipDelay": 120, "zoomView": False, "dragView": True, "navigationButtons": True, "keyboard": False},
    }

    g_col, d_col = st.columns([3, 1], gap="medium")
    clicked = None
    with g_col:
        with st.container(horizontal=True, gap="small", key="graph-legend"):
            counts: dict[str, int] = {}
            for n in nodes:
                counts[common.vtype(n)] = counts.get(common.vtype(n), 0) + 1
            for t in sorted(counts):
                common.chip(f"{SHAPE_GLYPH[NODE_STYLE.get(t, ('dot', 0))[0]]} {t} {counts[t]}", "gray")
            for t in sorted({common.etype(e) for e in edges}):
                common.chip(f"{'┄' if t in DASHED_EDGES else '—'} {t}", "gray")
            if any(common.vtype(n) == "AgentCase" for n in nodes):
                common.chip(f"{gid} written" if written else f"{gid} not written to graph", "green" if written else "orange", help=written_why)
        st.markdown(f"<style>.st-key-graph_panel {{background: {PAPER};}}</style>", unsafe_allow_html=True)  # theme colour only, no data
        with st.container(border=True, key="graph_panel"):
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

    with d_col:
        if clicked and clicked in node_by_id:
            n = node_by_id[clicked]
            t = common.vtype(n)
            common.chip(f"{SHAPE_GLYPH[NODE_STYLE.get(t, ('dot', 0))[0]]} {t}", "primary")
            st.code(str(n["id"]), language=None, wrap_lines=True)
            if n.get("label") and str(n["label"]) != str(n["id"]):
                st.caption(common.md(n["label"]))
            links = []
            for e in edges:
                if clicked in (e["src"], e["dst"]):
                    other = e["dst"] if e["src"] == clicked else e["src"]
                    arrow = "→" if e["src"] == clicked else "←"
                    links.append(f"{arrow} `{common.md(common.etype(e))}` {common.md(_short(str(other).split(' | ')[0], 30))} "
                                 f":gray[{common.md(common.vtype(node_by_id.get(other, {})))}]")
            st.markdown(f"**Connections** ({len(links)})")
            st.markdown("\n".join(f"- {x}" for x in links[:25]) + (f"\n- … and {len(links) - 25} more" if len(links) > 25 else "") if links else "_none in this view_")
        else:
            st.markdown(f"**{common.md(case_id)}** " + common.verdict_badge(case.get("verdict") or ""))
            card_note = f" (case card {meta.get('card_id', '?')})" if mismatch else ""
            st.caption(common.md(f"card {card}{card_note} · customer {meta.get('customer_id', '')} · opened {as_of} · flagged txn {flagged}"))
            if wave and wave.get("detector_id"):
                st.markdown(common.badge(f"wave {wave['detector_id']}", "orange"))
            else:
                st.markdown(common.badge("not part of any detected wave", "gray"))
            st.caption("Click a node for its type, id and connections. Drag to pan; use the on-canvas buttons to zoom.")
        st.divider()
        st.caption(common.md(f"{len(nodes)} nodes · {len(edges)} edges in view"))


# --------------------------------------------------------------------------- #
# Write-back and read-back                                                    #
# --------------------------------------------------------------------------- #
with st.container(border=True, key="graph-writeback"):
    st.markdown(f"**Write-back & read-back · {common.md(gid)}**")
    usage = common.tg_usage(view)
    w1, w2 = st.columns(2, gap="medium")
    with w1:
        st.caption("Persist rows (calls.jsonl, caller persist)")
        if usage["persist"]:
            st.dataframe([{"call": r["name"], "ok": r["ok"], "latency_s": r["latency_s"]} for r in usage["persist"]], hide_index=True,
                         height=35 * min(len(usage["persist"]), 8) + 38,
                         column_config={"call": st.column_config.TextColumn("Call"), "ok": st.column_config.CheckboxColumn("ok"),
                                        "latency_s": st.column_config.NumberColumn("Latency", format="%.2f s")})
        else:
            st.caption(common.md(f"No persist rows: {written_why}."))
        st.markdown(common.badge(written_why, "green" if written else "orange"))
    with w2:
        st.caption("Measured vector read-back: other traces in this source whose P1 similar_prior_cases returned this case")
        hits = []
        if ctx.source_id not in common.SUBMITTED:
            for other, ov in common.source_views(ctx.source_id).items():
                if other == case_id or not ov.has_trace:
                    continue
                for h in common.tg_usage(ov)["vector_hits"]:
                    if h.get("id") == gid:
                        d = h.get("distance")
                        hits.append(f"{gid} was retrieved by {other} at distance " + (f"{d:.3f}" if isinstance(d, int | float) else "unrecorded"))
        if hits:
            st.markdown("\n".join(f"- {common.md(h)}" for h in hits))
        else:
            st.caption(common.md("none in this source" if ctx.source_id not in common.SUBMITTED else "the submitted answers carry no trace"))
        if live:
            if st.button(f"Read back {gid} now", icon=":material/sync:", key="graph_readback"):
                st.session_state[f"_readback_{gid}"] = _read_back()
            rb = st.session_state.get(f"_readback_{gid}")
            if rb:
                st.caption(common.md(f"graph now — a later run may have overwritten it · read at {rb['at']} · {rb['latency_s']:.2f} s"))
                if rb["error"]:
                    common.dependency_banner("TigerGraph", rb["error"])
                mine = [r for r in rb["agent_cases"] if r.get("id") == gid]
                st.markdown(common.badge(f"prior_cases_for_customer lists {gid}" if mine else f"prior_cases_for_customer does not list {gid}",
                                         "green" if mine else "orange"))
                if rb["vertex"] is not None:
                    with st.expander(f"AgentCase {common.md(gid)} attributes"):
                        st.json(rb["vertex"] or {"found": False}, expanded=False)
        else:
            st.caption(common.md(f"Read-back from FraudGraph needs live mode ({ctx.mode_reason})"))


# --------------------------------------------------------------------------- #
# The case's wave                                                             #
# --------------------------------------------------------------------------- #
with st.container(horizontal=True, vertical_alignment="center", gap="small", key="graph-wave"):
    if wave and wave.get("_missing"):
        st.caption(common.md(f"Wave membership unknown: {wave['_missing']}"))
    elif wave:
        n_cards = wave.get("cards_matched")
        bt = wave["_proof"]
        st.markdown(common.md(f"Part of {wave['detector_id']}" + (f" ({n_cards} cards, detector-attested)" if n_cards is not None else " (detector-attested)")),
                    help=common.md(f"Source: {bt['source']} · as of {bt['as_of']}"))
        acted = wave.get("legitimate_customers_acted_against") or []
        if acted:
            cleared = ", ".join(wave.get("cleared_cases_matched") or [])
            common.chip(f"also acts against cleared customer {', '.join(acted)}" + (f" ({cleared})" if cleared else ""), "red", icon=":material/block:")
    else:
        st.markdown(common.md("Not part of any detected wave — nothing to contain"))
    # The containment proposal, the ring replay and the wave selector live on Detectors & containment.
    common.page_link("detectors", "Detectors & containment", icon=":material/radar:", case=case_id,
                     help="containment proposal, ring replay and the detector backtest for this case's wave")

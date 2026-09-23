"""Graph view: runs the installed query `case_subgraph(c, as_of, hours)` (contract 2A)
through pyTigerGraph and renders ≤ 300 nodes/edges with streamlit-agraph
(falls back to pyvis). In mock mode the subgraph is built from the answer file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Graph", page_icon="🕸️", layout="wide")
st.title("🕸️ Case subgraph")

pack_ids = [m["case_id"] for m in common.load_case_pack()]
run_id = st.session_state.get("run_id") or (common.list_runs() or [None])[0]
c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
case_id = c1.selectbox("Case", pack_ids, index=pack_ids.index(st.session_state.get("case_id", "HHG-014")) if st.session_state.get("case_id") in pack_ids else 13)
st.session_state["case_id"] = case_id
hours = c2.number_input("hours before opened_at", 1, 24 * 30, 72)
renderer = c3.selectbox("Renderer", ["streamlit-agraph", "pyvis"])
meta = common.case_meta(case_id)
mode = common.run_mode()
c4.markdown(f"card `{meta.get('card_id','')}` · as_of `{meta.get('opened_at','')}` · {common.badge(mode, 'green' if mode=='live' else 'gray')}")

sub: dict | None = None
if mode == "live":
    try:
        conn = common.graph_conn()
        sub = common.run_installed(conn, "case_subgraph", {"c": meta.get("card_id", ""), "as_of": meta.get("opened_at", ""), "hours": int(hours)})
        st.caption(f"`case_subgraph` returned {len(sub.get('nodes', []))} nodes / {len(sub.get('edges', []))} edges")
    except Exception as exc:  # noqa: BLE001
        st.error(f"case_subgraph failed ({exc}); falling back to the answer-derived subgraph")
if sub is None or not sub.get("nodes"):
    ans = (common.load_case_run(run_id, case_id).answer if run_id else None) or common.load_promoted_answer(case_id)
    if not ans:
        st.warning("No answer file for this case yet; run the case (or `make mock-run`) to draw its subgraph.")
        st.stop()
    sub = common.subgraph_from_answer(ans, meta)
    st.caption(f"answer-derived subgraph: {len(sub['nodes'])} nodes / {len(sub['edges'])} edges (card, flagged + affected transactions, device profiles, connected cards, similar closed cases, the AgentCase vertex)")

types = sorted({common.vtype(n) for n in sub["nodes"]})
keep = st.multiselect("Vertex types", types, default=types)
nodes = [n for n in sub["nodes"] if common.vtype(n) in keep]
ids = {n["id"] for n in nodes}
edges = [e for e in sub["edges"] if e.get("src") in ids and e.get("dst") in ids]
st.markdown(" ".join(f"<span style='color:{common.NODE_COLORS.get(t,'#95a5a6')}'>■</span> {t}" for t in types), unsafe_allow_html=True)

if renderer == "streamlit-agraph":
    try:
        from streamlit_agraph import Config, Edge, Node, agraph

        gnodes = [Node(id=n["id"], label=str(n.get("label", n["id"]))[:28], size=22 if common.vtype(n) in ("Card", "AgentCase", "DeviceProfile") else 12, color=common.NODE_COLORS.get(common.vtype(n), "#95a5a6"), title=f"{common.vtype(n)}: {n['id']}") for n in nodes]
        gedges = [Edge(source=e["src"], target=e["dst"], label=common.etype(e)) for e in edges]
        cfg = Config(width="100%", height=650, directed=True, physics=len(nodes) < 150, hierarchical=False)
        clicked = agraph(nodes=gnodes, edges=gedges, config=cfg)
        if clicked:
            st.info(f"selected `{clicked}`")
    except ImportError:
        st.warning("streamlit-agraph not installed; using pyvis")
        renderer = "pyvis"
if renderer == "pyvis":
    import streamlit.components.v1 as components
    from pyvis.network import Network

    net = Network(height="650px", width="100%", directed=True, notebook=False, cdn_resources="in_line")
    for n in nodes:
        net.add_node(n["id"], label=str(n.get("label", n["id"]))[:28], color=common.NODE_COLORS.get(common.vtype(n), "#95a5a6"), title=f"{common.vtype(n)}: {n['id']}")
    for e in edges:
        net.add_edge(e["src"], e["dst"], title=common.etype(e))
    components.html(net.generate_html(), height=680, scrolling=True)

with st.expander("Nodes / edges"):
    st.dataframe(nodes, width="stretch", hide_index=True)
    st.dataframe(edges, width="stretch", hide_index=True)

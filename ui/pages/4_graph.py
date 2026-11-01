"""Graph: what is this case connected to, and is the agent's case in the graph?

The head names the case, its verdict and p, and where the drawing comes from in one plain sentence. Under it, the canvas
band: two-thirds canvas with its toolbar (legend chips that filter the vertex types, each with its canvas shape and colour,
the counts, hours in live mode, download, Reset layout, Expand), a one-sentence reading of the drawing above the canvas
and the edge key under it, and a right rail (written back, read back, the case's wave, the case facts, then the node
inspector with the model's claims about the selected node). Positions are computed here (radial, deterministic) and
physics is off, so a dragged node stays where it is dropped and Reset layout puts every node back; only the selected
node's links are labelled. Expand opens the same graph in a wide dialog, taller and with zoom, beside its own node panel;
Esc closes it. Under the band, the agent's notes: the P9
summary, the P4 calibration basis and the P4 claims that cite nodes on the canvas, model-written text as the trace
recorded it (escaped, never followed; no model is called here). Folded below: the persist rows, the live read-back and
graph health.

Live mode runs the installed query `case_subgraph(c, as_of, hours)` once per (card, as_of, hours) and caches it for
300 s, so clicking or dragging a node never re-queries. Mock mode, or an unreachable graph, draws the subgraph rebuilt from the
answer file and says so; it never opens a TigerGraph connection. The AgentCase is drawn solid only when the trace's
persist rows show it was written. Viewing never writes.
"""
from __future__ import annotations

import json
import math
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


def _repo_rel(path: Path) -> str:
    """Relative to the repo without resolving links, so a linked runs/ still reads as runs/<source>/<case>/answer.json."""
    try:
        return str(path.relative_to(common.ROOT))
    except ValueError:
        return common.rel(path)


answer_path = _repo_rel((view.dir or common.cases_dir()) / (f"{case_id}.json" if ctx.source_id in common.SUBMITTED else "answer.json"))


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


def _notes_view(v: common.CaseView) -> common.CaseView | None:
    """The trace the answer came from: this source's, cut to the attempt that wrote the answer, else the run a submitted
    answer was promoted from; None when no trace is here."""
    for ov in [v] + [common.case_view(sid, case_id) for sid in v.promoted_from]:
        if ov.has_trace:
            return common.answer_attempt_view(ov)
    return None


def _model(v: common.CaseView, code: str) -> str:
    """The model calls.jsonl records for this attempt's `code` LLM call; '' when it records none."""
    return next((str(c["model"]) for c in reversed(v.calls) if c.get("kind") == "llm" and c.get("phase") == code and c.get("model")), "")


def _cites(c: dict) -> list[str]:
    return [str(i) for i in c.get("entity_ids") or []]


def _claims_html(items: list[dict], drawn: set, focus: str | None = None) -> str:
    """The model's claims as text, each with the ids it cites: an id drawn on the canvas as a chip, any other dashed, the
    selected node's in petrol (all of it escaped)."""
    lis = []
    for c in items:
        chips = "".join(f'<code class="{"sel" if i == focus else "on" if i in drawn else "off"}" title="{design.esc(i)}">'
                        f'{design.esc(_short(i.split(" | ")[0], 28))}</code>' for i in _cites(c))
        src = " · ".join(str(x) for x in (c.get("source"), c.get("ref")) if x)
        tail = f'<div class="ids">{chips}<span class="src">{design.esc(src)}</span></div>' if chips or src else ""
        lis.append(f'<li><p>{design.esc(c.get("claim"))}</p>{tail}</li>')
    return f'<ul class="gr-claims">{"".join(lis)}</ul>'


# --------------------------------------------------------------------------- #
# The subgraph: live, cached, else rebuilt from the answer                    #
# --------------------------------------------------------------------------- #
written, written_why = _written(view)
notes_v = _notes_view(view) if answer is not None else None
_p4 = common.phase_payload(notes_v, "P4") if notes_v else {}
assessment = _p4.get("assessment") if isinstance(_p4.get("assessment"), dict) else {}
claims = [c for c in assessment.get("evidence") or [] if isinstance(c, dict) and str(c.get("claim") or "").strip()]
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
EDGE_MEANING = {  # each edge type in a few words, from graph/schema.gsql (SHARES_DEVICE only exists in the answer-built drawing)
    "OWNS": "customer owns the card", "MADE": "card made the transaction", "NEXT": "the card's next transaction",
    "FROM_DEVICE": "transaction came from the device", "SHARES_DEVICE": "card used the same device profile",
    "PURCHASER_EMAIL": "purchaser's email domain", "RECIPIENT_EMAIL": "recipient's email domain", "BILLED_IN": "billing region",
    "CASE_ON_CARD": "the card the case was opened on", "CASE_INVOLVES": "transactions the case covers",
    "CASE_CONNECTED_TO": "cards the agent tied to the case", "CASE_DEVICE": "device profile tied to the case",
    "CASE_SIMILAR_TO": "closed cases it resembles (case memory)", "INVOLVES": "a closed case's transaction",
    "ON_CARD": "a closed case's card", "CONNECTED_TO": "a card a closed case was tied to",
}
EDGE_LABEL_MAX = 10  # a selected node with more links than this lists them in the inspector instead of labelling them
LAYOUT_KEY, DLG_LAYOUT_KEY = "gr_layout_n", "gr_dlg_layout_n"  # bumped by Reset layout: a new canvas key redraws from scratch


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


def _swatch_uri(t: str) -> str:
    """The type's canvas shape, filled and sized as on the canvas, as an SVG data URI for a CSS mask (fixed markup, never
    data)."""
    shape, size = NODE_STYLE.get(t, ("dot", 16))
    body = {"diamond": "<path d='M8 1 15 8 8 15 1 8Z'/>", "square": "<rect x='2.5' y='2.5' width='11' height='11'/>",
            "triangle": "<path d='M8 1.5 15 14H1Z'/>"}.get(shape, f"<circle cx='8' cy='8' r='{min(7.5, 2.5 + size / 4.8):.1f}'/>")
    return "data:image/svg+xml," + quote(f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='black'>{body}</svg>", safe="")


def _legend_css(types: list[str]) -> str:
    """Each legend chip (a pill of the type filter, in `types` order) gets its type's canvas shape in its palette colour,
    as a light-dark() pair so both themes hold."""
    rules = []
    for i, t in enumerate(types, 1):
        uri = _swatch_uri(t)
        lc, dc = VERTEX_PALETTE["light"].get(t, "#55687a"), VERTEX_PALETTE["dark"].get(t, "#93a7b6")
        rules.append(f'[class*="st-key-graph_types_"] button:nth-of-type({i})::before{{background-color:light-dark({lc},{dc});'
                     f'-webkit-mask-image:url("{uri}");mask-image:url("{uri}");}}')
    return "<style>" + "".join(rules) + "</style>"


RING_ORDER = ("CASE_ON_CARD", "CASE_INVOLVES", "CASE_DEVICE", "CASE_SIMILAR_TO", "CASE_CONNECTED_TO")  # the case's links, clockwise
NODE_GAP, RING_GAP = 84, 125  # world px between neighbours on a ring, and between rings


@st.cache_data(max_entries=64, show_spinner=False)
def _layout(node_ids: tuple[str, ...], links: tuple[tuple[str, str, str], ...], root: str) -> dict[str, tuple[float, float]]:
    """A fixed position for every node, so physics stays off and a dragged node stays where it is dropped. Radial around
    `root` (the agent's case; the best-connected node when it is absent): its direct links evenly on an ellipse, grouped by
    relationship in RING_ORDER from the right, clockwise; each further ring at the mean angle of the nodes that reach it,
    spread so no two sit closer than NODE_GAP; nodes `root` never reaches on an outer ring. The same subgraph always draws
    the same."""
    adj: dict[str, list[tuple[str, str]]] = {n: [] for n in node_ids}
    for a, b, t in links:
        if a in adj and b in adj and a != b:
            adj[a].append((b, t))
            adj[b].append((a, t))
    if not node_ids:
        return {}
    if root not in adj:
        root = max(node_ids, key=lambda n: (len(adj[n]), n))
    depth, rings = {root: 0}, [[root]]
    while rings[-1]:
        nxt = sorted({m for n in rings[-1] for m, _ in adj[n] if m not in depth})
        for m in nxt:
            depth[m] = len(rings)
        rings.append(nxt)
    rings.pop()
    stray = sorted(n for n in node_ids if n not in depth)
    if stray:
        rings.append(stray)
    pos: dict[str, tuple[float, float]] = {root: (0.0, 0.0)}
    angle: dict[str, float] = {}
    r1 = max(150.0, len(rings[1]) * NODE_GAP / (2 * math.pi)) if len(rings) > 1 else 150.0
    for k, ring in enumerate(rings[1:], 1):
        r = r1 + (k - 1) * RING_GAP
        if k == 1 or ring is stray:
            rank = {m: min((RING_ORDER.index(t) if t in RING_ORDER else len(RING_ORDER), t) for m2, t in adj[root] if m2 == m)
                    for m in ring if any(m2 == m for m2, _ in adj[root])}
            ring = sorted(ring, key=lambda m: (rank.get(m, (99, "")), m))
            want = {m: 2 * math.pi * i / len(ring) for i, m in enumerate(ring)}
        else:  # at the mean angle of the nodes one ring in that reach it, then pushed apart along the ring
            want = {}
            for m in ring:
                ps = [angle[q] for q, _ in adj[m] if q in angle and depth.get(q) == k - 1]
                want[m] = math.atan2(sum(map(math.sin, ps)), sum(map(math.cos, ps))) % (2 * math.pi) if ps else 0.0
            gap = NODE_GAP / r
            ordered = sorted(ring, key=lambda m: (want[m], m))
            placed = [want[ordered[0]]]
            for m in ordered[1:]:
                placed.append(max(want[m], placed[-1] + gap))
            shift = (sum(placed) - sum(want[m] for m in ordered)) / len(ordered)  # keep the group centred on its parents
            want = {m: a - shift for m, a in zip(ordered, placed, strict=True)}
        for m in ring:
            angle[m] = want[m]
            pos[m] = (round(1.15 * r * math.cos(want[m]), 1), round(r * math.sin(want[m]), 1))  # a little wider than tall, as the canvas is
    return pos


def _placed(vnodes: list[dict], pos: dict) -> list[dict]:
    return [{**vn, "x": pos[vn["id"]][0], "y": pos[vn["id"]][1]} if vn["id"] in pos else vn for vn in vnodes]


def _labelled(vedges: list[dict], sel: str | None) -> list[dict]:
    """The selected node's links carry their relationship name; no other edge does (all of them at once is unreadable)."""
    mine = {i for i, ve in enumerate(vedges) if sel in (ve["source"], ve["target"])} if sel else set()
    if not mine or len(mine) > EDGE_LABEL_MAX:
        return vedges
    return [{**ve, "label": ve["title"]} if i in mine else ve for i, ve in enumerate(vedges)]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _reading(edge_list: list[dict], drawn: dict, sel: str | None) -> str:
    """One plain sentence on what the drawing shows, from the edges on the canvas (escaped markup)."""
    n = Counter(common.etype(e) for e in edge_list if e.get("src") == gid)
    parts = []
    if n["CASE_ON_CARD"]:
        parts.append(f"the card it was opened on ({card})" if card in drawn else "the card it was opened on")
    for et, word, tail in (("CASE_INVOLVES", "transaction", "it covers"), ("CASE_CONNECTED_TO", "card", "the agent tied to it"),
                           ("CASE_DEVICE", "device profile", "tied to it"), ("CASE_SIMILAR_TO", "closed case", "it resembles (dashed: case memory)")):
        if n[et]:
            parts.append(f"{_plural(n[et], word)} {tail}")
    if gid in drawn:
        not_in = drawn[gid].get("written") is False or (not written and "written" not in drawn[gid])
        s = f"The diamond is {gid}, the agent's case" + (", a dashed outline because it is not in FraudGraph" if not_in else "")
        s += (". Arrows run from it to " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + ".") if parts else "."
    else:
        s = f"{gid}, the agent's case, is hidden by the type filter."
    ringed = [x for x in (card, flagged) if x and x in drawn]
    if ringed:
        s += " A bold ring marks " + " and ".join(("the investigated card" if x == card else "the flagged transaction") for x in ringed) + "."
    if sel:
        k = sum(1 for e in edge_list if sel in (e.get("src"), e.get("dst")))
        s += (f" {_short(sel.split(' | ')[0], 28)} is selected: its {_plural(k, 'link')} are named on the canvas." if 0 < k <= EDGE_LABEL_MAX
              else f" {_short(sel.split(' | ')[0], 28)} is selected: its {_plural(k, 'link')} are listed in the inspector." if k else "")
    return design.esc(s)


def _edge_key(edge_list: list[dict]) -> str:
    """The edge types on the canvas: a sample of the line, the name, how many, what it means (escaped markup)."""
    lis = []
    for et, k in Counter(common.etype(e) for e in edge_list).most_common():
        line = f'<span class="gr-ln{" dash" if et in DASHED_EDGES else ""}" style="border-top-color:{EDGE_COLORS.get(et, MUTED)}"></span>'
        lis.append(f'<li>{line}<code>{design.esc(et)}</code><span class="n">{k}</span>'
                   f'<span class="m">{design.esc(EDGE_MEANING.get(et, "relationship in FraudGraph"))}</span></li>')
    return (f'<ul class="gr-edgekey">{"".join(lis)}</ul><p class="gr-how">Drag a node to place it · Reset layout puts every node back · '
            'click a node to name its links and open it in the inspector · hover a line for its name · Expand for a larger view with zoom</p>')


def _row(label: str, value: str, title: str = "") -> str:
    """One rail row: a muted label and its value (markup built with design.esc)."""
    tip = f' title="{design.esc(title)}"' if title else ""
    return f'<div class="gr-row"{tip}><span class="k">{design.esc(label)}</span><span class="v">{value}</span></div>'


DASH = '<span class="gr-dash">—</span>'


# --------------------------------------------------------------------------- #
# The canvas band: canvas with its toolbar, and the right rail                #
# --------------------------------------------------------------------------- #
def _node_html(sel: str, by_id: dict, edge_list: list[dict], claim_list: list[dict], max_h: int) -> str:
    """The selected node: its id and description, its connections in this view, then the model's P4 claims that cite it."""
    n = by_id[sel]
    links = []
    for e in edge_list:
        if sel in (e["src"], e["dst"]):
            out = e["src"] == sel
            other = e["dst"] if out else e["src"]
            links.append(f'<li><span class="d">{"→" if out else "←"}</span><code>{design.esc(common.etype(e))}</code>'
                         f'<span>{design.esc(_short(str(other).split(" | ")[0], 30))} '
                         f'<span class="t">{design.esc(common.vtype(by_id.get(other, {})))}</span></span></li>')
    items = "".join(links) or '<li class="gr-empty">none in this view</li>'
    desc = f'<div class="gr-insp-desc">{design.esc(n["label"])}</div>' if n.get("label") and str(n["label"]) != str(n["id"]) else ""
    about = [c for c in claim_list if sel in _cites(c)]
    said = (_claims_html(about, set(by_id), sel) if about
            else f'<p class="gr-empty">{"none of the P4 claims cite it" if claim_list else "no P4 assessment recorded here"}</p>')
    return (f'<div class="tc gr-insp"><div class="gr-insp-id">{design.esc(n["id"])}</div>{desc}'
            f'<div class="gr-insp-n">Connections {len(links)}</div><ul class="gr-links" style="max-height:{int(max_h)}px">{items}</ul>'
            f'<div class="gr-insp-n">What the model said about it · {len(about)}</div>{said}</div>')


def _draw(vnodes: list[dict], vedges: list[dict], options: dict, h: int, types: dict, key: str) -> str | None:
    """The canvas: streamlit-agraph (vis-network), which returns the clicked node's id and keeps it under `key` in
    st.session_state; a new key draws it afresh (Reset layout). pyvis when agraph is not installed, whose clicks reach
    nothing. `types` maps a node id to its vertex type for pyvis's hover titles."""
    try:
        from streamlit_agraph import Config, Edge, Node, _agraph
    except ImportError:
        _agraph = None
    if _agraph is not None:
        gnodes = []
        for vn in vnodes:
            gn = Node(**vn)
            gn.__dict__.pop("title", None)  # the component opens a node's title as a URL on double-click
            gnodes.append(gn)
        gedges = [Edge(**{k: v for k, v in ve.items() if k != "color"}, color=ve["color"]) for ve in vedges]
        cfg = Config(directed=True, physics=False, hierarchical=False)
        cfg.__dict__.update(options, width="100%", height=f"{h}px")  # Config appends "px" to width, so set it directly
        cfg.__dict__.pop("groups", None)  # Config defaults groups=None, which vis rejects on every redraw
        # agraph() itself, with a key: the clicked node lands in st.session_state[key] and a new key starts a fresh canvas
        data = json.dumps({"nodes": [n.to_dict() for n in gnodes], "edges": [e.to_dict() for e in gedges]})
        return _agraph(data=data, config=json.dumps(cfg.__dict__), key=key)
    from pyvis.network import Network  # automatic fallback only: streamlit-agraph is not installed here

    net = Network(height=f"{h}px", width="100%", directed=True, notebook=False, cdn_resources="in_line", bgcolor=PAPER, font_color=INK)
    for vn in vnodes:
        net.add_node(vn["id"], label=vn["label"], shape=vn["shape"], size=vn["size"], color=vn["color"], borderWidth=vn["borderWidth"],
                     shapeProperties=vn.get("shapeProperties", {}), title=f"{types.get(vn['id'], '')}: {vn['id']}",
                     **({"x": vn["x"], "y": vn["y"]} if "x" in vn else {}))
    for ve in vedges:
        net.add_edge(ve["source"], ve["target"], title=ve["title"], dashes=ve["dashes"], color=ve["color"], **({"label": ve["label"]} if "label" in ve else {}))
    net.set_options(json.dumps(options))
    st.caption("streamlit-agraph is not installed here: drawn with pyvis, so node clicks do not reach the inspector.")
    st.iframe(net.generate_html().replace("border: 1px solid lightgray;", "border: none;"), height=h + 20)
    return None


EXPANDED_HEIGHT = 700  # the dialog's canvas: tall enough to spread the subgraph, short enough for a laptop screen


@st.dialog(f"Graph · {case_id}", width="large")
def _expanded(vnodes: list[dict], vedges: list[dict], options: dict, by_id: dict, edge_list: list[dict], claim_list: list[dict], legend: str) -> None:
    """The same drawing in a wide dialog (the stylesheet widens it to the window), laid out for its larger box, with zoom
    on, its own Reset layout and node panel. A click reruns the dialog only, so it stays open; Esc or the close button
    returns to the page."""
    with st.container(key="gr-dlg"):
        dkey = f"gr_dlg_{case_id}_{st.session_state.get(DLG_LAYOUT_KEY, 0)}"
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="gr-dlg-tools"):
            design.html_block(f'<div class="tc gr-count">{design.esc(legend)} · drag a node to place it, scroll to zoom, drag the '
                              'background to pan</div>')
            st.button("Reset layout", key="gr_dlg_reset", type="tertiary", icon=":material/restart_alt:",
                      on_click=lambda: st.session_state.update({DLG_LAYOUT_KEY: st.session_state.get(DLG_LAYOUT_KEY, 0) + 1}),
                      help="Every node back to its place and the whole graph back in view")
        prev = st.session_state.get(dkey)
        canvas, side = st.columns([3, 1], gap="medium")
        with canvas, st.container(key="gr-dlg-canvas"):
            pos = _layout(tuple(vn["id"] for vn in vnodes), tuple((ve["source"], ve["target"], ve["title"]) for ve in vedges), gid)
            picked = _draw(_placed(vnodes, pos), _labelled(vedges, prev if prev in by_id else None),
                           {**options, "interaction": {**options["interaction"], "zoomView": True}}, EXPANDED_HEIGHT,
                           {i: common.vtype(n) for i, n in by_id.items()}, dkey)
        with side, st.container(key="gr-dlg-side"):
            if picked in by_id:
                t = common.vtype(by_id[picked])
                design.html_block(f'<div class="tc gr-insp-head">{design.glyph(TYPE_GLYPH.get(t, "dash"))}{design.esc(t)}</div>'
                                  + _node_html(picked, by_id, edge_list, claim_list, int(EXPANDED_HEIGHT * 0.3)))
            else:
                design.html_block('<div class="tc gr-insp-head">Node inspector</div><p class="tc gr-empty">Click a node on the canvas</p>')


wave = _wave()
n_total = len(sub["nodes"]) if sub is not None else 0
detailed, dense = n_total <= 60, n_total > 14
height = 600 if dense else 460
canvas_col, rail_col = st.columns([2, 1], gap="medium")
clicked = None
open_full = False
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
            with st.container(horizontal=True, vertical_alignment="center", gap="small", width="content", key="graph-tools"):
                if live:
                    st.number_input("Hours before opened_at", 1, 24 * 30, 72, key="graph_hours", format="%d h", width=104,
                                    label_visibility="collapsed", help="Window of case_subgraph before opened_at")
                st.download_button("JSON", json.dumps(sub, indent=1, default=str), file_name=f"{case_id}_subgraph.json", mime="application/json",
                                   on_click="ignore", icon=":material/download:", type="tertiary", help="Download this subgraph as JSON")
                st.button("Reset layout", key="graph_reset", type="tertiary", icon=":material/restart_alt:",
                          on_click=lambda: st.session_state.update({LAYOUT_KEY: st.session_state.get(LAYOUT_KEY, 0) + 1}),
                          help="Every node back to its place and the whole graph back in view")
                open_full = st.button("Expand", key="graph_full_btn", type="tertiary", icon=":material/open_in_full:",
                                      help="The graph in a wide view, taller and with zoom, beside its own node panel; Esc closes it")
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
        vis_edges.append({"source": e["src"], "target": e["dst"], "title": t, "dashes": t in DASHED_EDGES,
                          "color": {"color": EDGE_COLORS.get(t, MUTED), "highlight": SELECT, "hover": EDGE_COLORS.get(t, MUTED), "opacity": 0.9}})
    vis_options = {
        "autoResize": True,
        "nodes": {"font": {"face": FONT, "size": LABEL_SIZE, "color": INK, "strokeWidth": 4, "strokeColor": PAPER}, "borderWidthSelected": 3},
        "edges": {"width": 1.4, "selectionWidth": 1.5, "hoverWidth": 0.8, "arrows": {"to": {"enabled": True, "scaleFactor": 0.45}},
                  "smooth": {"enabled": False},  # straight lines: the layout already spreads the links around their node
                  "font": {"face": FONT, "size": LABEL_SIZE - 2, "color": INK, "strokeWidth": 0, "background": PAPER, "align": "horizontal"}},
        # positions come from _layout and physics is off, so a dragged node stays where it is dropped and the drawing never drifts
        "physics": {"enabled": False},
        "layout": {"randomSeed": 7, "improvedLayout": False, "hierarchical": {"enabled": False}},
        # zoomView off on the page: the mouse wheel scrolls the page; the layout is fitted to the canvas (the Expand dialog
        # turns zoom on). vis's navigation buttons are bitmaps inside the component's frame that the console's theme cannot
        # reach, so they are off.
        "interaction": {"hover": True, "tooltipDelay": 120, "zoomView": False, "dragView": True, "dragNodes": True, "navigationButtons": False,
                        "keyboard": False},
    }

    # every node of the subgraph is placed once, so filtering a type hides nodes without moving the others
    layout = _layout(tuple(str(n["id"]) for n in sub["nodes"]),
                     tuple((str(e["src"]), str(e["dst"]), common.etype(e)) for e in sub.get("edges") or []), gid)
    canvas_key = f"gr_canvas_{case_id}_{st.session_state.get(LAYOUT_KEY, 0)}"
    prev = st.session_state.get(canvas_key)  # the node clicked on the last run, before the canvas returns it again
    picked_now = prev if prev in node_by_id and prev != st.session_state.get("gr_cleared") else None
    with panel:
        design.html_block(f'<p class="tc gr-read">{_reading(edges, node_by_id, picked_now)}</p>')
        with st.container(key="gr-canvas"):
            clicked = _draw(_placed(vis_nodes, layout), _labelled(vis_edges, picked_now), vis_options, height,
                            {i: common.vtype(n) for i, n in node_by_id.items()}, canvas_key)
        if edges:
            design.html_block(f'<div class="tc gr-key">{_edge_key(edges)}</div>')
    if open_full:
        _expanded(vis_nodes, vis_edges, vis_options, node_by_id, edges, claims,
                  f"{len(nodes)} nodes · {len(edges)} edges" + (f" · {len(types_all) - len(keep)} vertex types filtered out" if len(keep) < len(types_all) else ""))


# --------------------------------------------------------------------------- #
# The right rail: written back, read back, wave, case facts, node inspector   #
# --------------------------------------------------------------------------- #
sel_node: str | None = None  # the node picked on the canvas, for the inspector and the notes band
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
            sel_node = sel
            if sel is None:
                design.html_block('<div class="tc gr-insp-head">Node inspector</div><p class="tc gr-empty">No node selected</p>')
            else:
                st.session_state["gr_cleared"] = None  # a new selection: the next Clear applies to it
                t = common.vtype(node_by_id[sel])
                with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                    design.html_block(f'<div class="tc gr-insp-head">{design.glyph(TYPE_GLYPH.get(t, "dash"))}{design.esc(t)}</div>')
                    if st.button("Clear selection", key="gr-clear", icon=":material/close:", type="tertiary"):
                        st.session_state["gr_cleared"] = sel
                        st.rerun()
                design.html_block(_node_html(sel, node_by_id, edges, claims, int(height * 0.42)))


# --------------------------------------------------------------------------- #
# The agent's notes: model-written text as the trace recorded it              #
# --------------------------------------------------------------------------- #
summary, basis = str(case.get("summary") or "").strip(), str(assessment.get("calibration_basis") or "").strip()
if answer is not None and (summary or basis or claims):
    where = "" if notes_v is None or notes_v.source_id == ctx.source_id else f" · recorded in the {notes_v.source_id} trace"
    phases_path = _repo_rel(notes_v.dir / "phases.jsonl") if notes_v is not None and notes_v.dir else "phases.jsonl"
    trimmed = notes_v is not None and any(str(r.get("text", "")).startswith("P9: summary trimmed") for r in notes_v.phases)

    def _by(code: str) -> str:
        m = _model(notes_v, code) if notes_v is not None else ""
        return f"written by {m} in {code}" if m else "no trace here names the model"

    def _block(title: str, text: str, src: str) -> str:
        return f'<div class="gr-note-block"><h3>{design.esc(title)}</h3><p>{design.esc(text)}</p><div class="gr-note-src">{src}</div></div>'

    with st.container(key="gr-notes"):
        design.html_block(design.section("", "Agent's notes", "model-written text from the recorded run: shown as text, never followed; "
                                         "nothing is generated here"))
        left, right = st.columns([1, 1], gap="large")
        with left:
            blocks = []
            if summary:
                blocks.append(_block("Summary", summary, design.esc(f"{_by('P9')} · case.summary in ") + _mono(answer_path)
                                     + design.esc(" · cut to its first sentences by code" if trimmed else "")))
            if basis:
                p_model = assessment.get("fraud_probability")
                blocks.append(_block(f"Why p {common.prob(p_model)}" if isinstance(p_model, int | float) else "Why this probability", basis,
                                     design.esc(f"{_by('P4')} · assessment.calibration_basis in ") + _mono(phases_path) + design.esc(where)))
            design.html_block('<div class="tc gr-notes">' + "".join(blocks) + "</div>")
        with right:
            drawn = set(node_by_id)
            n_drawn = sum(1 for c in claims if drawn & set(_cites(c)))
            head = (f"Claims · {len(claims)}" + (f" · {n_drawn} cite nodes on the canvas" if claims else "")
                    + (f" · {sum(1 for c in claims if sel_node in _cites(c))} cite {_short(sel_node.split(' | ')[0], 28)}" if sel_node else ""))
            src = design.esc(f"{_by('P4')} · assessment.evidence in ") + _mono(phases_path) + design.esc(where)
            body = _claims_html(claims, drawn, sel_node) if claims else '<p class="gr-empty">no P4 assessment recorded here</p>'
            design.html_block(f'<div class="tc gr-notes"><div class="gr-note-block"><h3>{design.esc(head)}</h3>{body}'
                              f'<div class="gr-note-src">{src}</div></div></div>')


# --------------------------------------------------------------------------- #
# Folded below: persist rows, the live read-back and graph health             #
# --------------------------------------------------------------------------- #
with st.container(key="graph-writeback"):
    usage = common.tg_usage(view)
    with st.expander(f"Persist rows for {common.md(gid)} ({len(usage['persist'])})"):
        if usage["persist"]:
            st.dataframe([{"call": r["name"], "ok": r["ok"], "latency_s": r["latency_s"]} for r in usage["persist"]], hide_index=True,
                         height=common.table_height(len(usage["persist"]), 8),
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
                            st.dataframe(mismatched, hide_index=True, height=common.table_height(len(mismatched)))
                    else:
                        st.caption(common.md(f"{sum(v for v in health['counts'].values() if isinstance(v, int)):,} vertices and edges in FraudGraph at "
                                             f"{health['at']}; no data/out_delta/delta_manifest.json here to check them against"))

"""Comment-Blind GSQL DDL Parser & Graph Topology Path Finder (`schema_engine/gsql_parser.py`).

Parses `CREATE GRAPH`, `ADD VERTEX`, and `ADD DIRECTED/UNDIRECTED EDGE` from any TigerGraph `.gsql`
schema file after stripping all `#` and `//` comments so schema analysis never reads ground-truth
comments in test headers.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GsqlAttribute:
    name: str
    dtype: str


@dataclass
class GsqlVertex:
    name: str
    primary_id: str
    primary_id_type: str
    attributes: list[GsqlAttribute] = field(default_factory=list)

    @property
    def attr_names(self) -> list[str]:
        return [a.name for a in self.attributes]


@dataclass
class GsqlEdgePair:
    from_vertex: str
    to_vertex: str


@dataclass
class GsqlEdge:
    name: str
    directed: bool
    pairs: list[GsqlEdgePair] = field(default_factory=list)
    attributes: list[GsqlAttribute] = field(default_factory=list)
    reverse_edge: str = ""


@dataclass
class GsqlSchemaAST:
    graph_name: str
    vertices: dict[str, GsqlVertex]
    edges: dict[str, GsqlEdge]
    clean_ddl: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_name": self.graph_name,
            "vertices": {k: asdict(v) for k, v in self.vertices.items()},
            "edges": {k: asdict(e) for k, e in self.edges.items()},
        }


_GRAPH_RX = re.compile(r"\bCREATE\s+GRAPH\s+(\w+)", re.I)
_VERTEX_RX = re.compile(
    r"\bADD\s+VERTEX\s+(\w+)\s*\(\s*PRIMARY_ID\s+(\w+)\s+(\w+)(.*?)\)\s*(?:WITH\b|;)",
    re.I | re.S,
)
_EDGE_RX = re.compile(
    r"\bADD\s+(DIRECTED|UNDIRECTED)\s+EDGE\s+(\w+)\s*\((.*?)\)\s*(?:WITH\s+REVERSE_EDGE\s*=\s*\"([^\"]+)\")?\s*;",
    re.I | re.S,
)
_PAIR_RX = re.compile(r"\bFROM\s+(\w+)\s*,\s*TO\s+(\w+)", re.I)


def strip_gsql_comments(text: str) -> str:
    """Remove all `# ...` and `// ...` comment lines before parsing or sending to the AI."""
    lines: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#") or s.startswith("//"):
            continue
        # Strip trailing inline comments outside quotes
        if " #" in raw and '"' not in raw:
            raw = raw.split(" #", 1)[0]
        if " //" in raw and '"' not in raw:
            raw = raw.split(" //", 1)[0]
        if raw.strip():
            lines.append(raw)
    return "\n".join(lines)


def parse_gsql_ddl(text_or_path: str | Path) -> GsqlSchemaAST:
    """Parse a `.gsql` schema file or DDL string into a `GsqlSchemaAST` (strictly comment-blind)."""
    if isinstance(text_or_path, Path) or (isinstance(text_or_path, str) and "\n" not in text_or_path and Path(text_or_path).exists()):
        raw_text = Path(text_or_path).read_text(encoding="utf-8")
    else:
        raw_text = str(text_or_path)

    clean = strip_gsql_comments(raw_text)
    gm = _GRAPH_RX.search(clean)
    graph_name = gm.group(1) if gm else "UnknownGraph"

    vertices: dict[str, GsqlVertex] = {}
    for vm in _VERTEX_RX.finditer(clean):
        vname, pid, pid_type, rest = vm.group(1), vm.group(2), vm.group(3).upper(), vm.group(4)
        attrs: list[GsqlAttribute] = []
        for tok in [t.strip() for t in rest.split(",") if t.strip()]:
            parts = tok.split()
            if len(parts) >= 2:
                attrs.append(GsqlAttribute(name=parts[0].strip(), dtype=parts[1].strip().upper()))
        vertices[vname] = GsqlVertex(name=vname, primary_id=pid, primary_id_type=pid_type, attributes=attrs)

    edges: dict[str, GsqlEdge] = {}
    for em in _EDGE_RX.finditer(clean):
        kind, ename, body, rev = em.group(1).upper(), em.group(2), em.group(3), (em.group(4) or "")
        pairs = [GsqlEdgePair(from_vertex=pm.group(1), to_vertex=pm.group(2)) for pm in _PAIR_RX.finditer(body)]
        # Extract edge attributes after the last `TO <Vertex>`
        last_to = list(_PAIR_RX.finditer(body))
        edge_attrs: list[GsqlAttribute] = []
        if last_to:
            tail = body[last_to[-1].end() :].strip().lstrip(",")
            for tok in [t.strip() for t in tail.split(",") if t.strip()]:
                parts = tok.split()
                if len(parts) >= 2 and parts[0].upper() not in ("FROM", "TO"):
                    edge_attrs.append(GsqlAttribute(name=parts[0].strip(), dtype=parts[1].strip().upper()))
        edges[ename] = GsqlEdge(
            name=ename,
            directed=(kind == "DIRECTED"),
            pairs=pairs,
            attributes=edge_attrs,
            reverse_edge=rev,
        )

    return GsqlSchemaAST(graph_name=graph_name, vertices=vertices, edges=edges, clean_ddl=clean)


def find_graph_paths(ast: GsqlSchemaAST, start_vertex: str, target_vertex: str, max_hops: int = 4) -> list[list[tuple[str, str, str]]]:
    """Find all simple paths up to `max_hops` from `start_vertex` to `target_vertex`.

    Each hop is `(from_vertex, edge_or_reverse_name, to_vertex)`.
    """
    if start_vertex not in ast.vertices or target_vertex not in ast.vertices:
        return []
    adj: dict[str, list[tuple[str, str]]] = {v: [] for v in ast.vertices}
    for e in ast.edges.values():
        for p in e.pairs:
            if p.from_vertex in adj and p.to_vertex in adj:
                adj[p.from_vertex].append((e.name, p.to_vertex))
                rev_name = e.reverse_edge or (e.name if not e.directed else f"reverse_{e.name}")
                adj[p.to_vertex].append((rev_name, p.from_vertex))

    found: list[list[tuple[str, str, str]]] = []
    q: deque[tuple[str, list[tuple[str, str, str]], set[str]]] = deque([(start_vertex, [], {start_vertex})])
    while q:
        curr, path, visited = q.popleft()
        if len(path) > max_hops:
            continue
        if curr == target_vertex and path:
            found.append(path)
            continue
        if len(path) == max_hops:
            continue
        for ename, nxt in adj.get(curr, []):
            if nxt not in visited:
                q.append((nxt, [*path, (curr, ename, nxt)], visited | {nxt}))
    found.sort(key=len)
    return found


def extract_ground_truth_comments(raw_text: str) -> list[dict[str, str]]:
    """Parse `# Expected mapping (ground truth):` lines ONLY for evaluation / benchmark grading."""
    in_gt = False
    rows: list[dict[str, str]] = []
    rx = re.compile(r"^#\s{2,4}([A-Za-z0-9_., ]+?)\s{2,}(exact|approximate|unmapped)\s*(.*)$")
    for line in raw_text.splitlines():
        if "Expected mapping (ground truth):" in line:
            in_gt = True
            continue
        if not in_gt:
            continue
        if not line.strip().startswith("#"):
            break
        m = rx.match(line)
        if m:
            target, status, detail = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            rows.append({"target": target, "expected_status": status, "expected_mapping": detail})
    return rows

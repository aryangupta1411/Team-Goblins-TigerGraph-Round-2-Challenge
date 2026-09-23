"""Static lint of graph/queries/*.gsql against the schema (module C's "static checks", now a repo file).

For every query file:
  * every `alias.attribute` reference resolves to an attribute of the vertex / edge type bound to that alias —
    aliases are bound from `(alias:Type)` / `-[alias:EDGE]-` patterns, `S = {param}` (the parameter's VERTEX<T>),
    and `X = SELECT x FROM ...` (X takes the type of x); vertex-set variables used as pattern types resolve through
    that binding; `alias.@acc` (accumulators) and tuple locals (`Row r = @@heap.pop(); r.id`) are skipped;
  * the schema is graph/schema.gsql (the DDL) cross-checked with contracts/schema.md (the only allowed rename is
    proxy -> proxy_type, so `t.proxy` is reported: PROXY is a 4.2 DDL reserved word and the attribute is stored
    as proxy_type — decision M1);
  * brackets / parentheses / braces are balanced.
Also: mcp/query_descriptions.yaml parses and every query's `parameters` keys equal its `example` keys, and every
query file has a yaml entry (and vice versa).

    uv run python -m qa.lint_gsql            # exit 1 on any problem; prints "<n> files, 0 problems" when clean
"""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "graph"))
from check_contracts import parse_schema  # noqa: E402

RENAMES = {"proxy": "proxy_type"}
QUERIES = os.path.join(ROOT, "graph", "queries")
TYPE_RE = re.compile(r"VERTEX<(\w+)>\s+(\w+)")


def _strip(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _balanced(text: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_str = None
    for ch in text:
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def lint_file(path: str, vertices: dict[str, list[str]], edges: dict[str, list[str]]) -> list[str]:
    raw = open(path).read()
    text = _strip(raw)
    problems: list[str] = []
    name = os.path.basename(path)
    if not _balanced(text):
        problems.append(f"{name}: unbalanced brackets")
    head = re.search(r"CREATE OR REPLACE QUERY \w+\s*\((.*?)\)\s*FOR GRAPH", text, re.S)
    params = dict((p, t) for t, p in TYPE_RE.findall(head.group(1))) if head else {}
    body = text.split("{", 1)[1] if "{" in text else text
    all_v = set(vertices)
    setvars: dict[str, set[str]] = {}          # vertex-set variable -> possible vertex types
    alias_types: dict[str, set[str]] = {}      # alias -> possible types (vertex or edge names)
    tuple_locals = set(re.findall(r"^\s*[A-Z]\w*\s+([a-z]\w*)\s*=", body, re.M))
    for stmt in body.split(";"):
        m = re.match(r"\s*(\w+)\s*=\s*\{(\w+)\}", stmt)
        if m:
            setvars[m.group(1)] = {params[m.group(2)]} if m.group(2) in params else all_v
            continue
        m = re.match(r"\s*(\w+)\s*=\s*SELECT\s+(\w+)\s+FROM", stmt, re.S)
        for alias, typ in re.findall(r"\((\w+):(\w+)\)", stmt):
            types = {typ} if typ in vertices else setvars.get(typ, all_v)
            alias_types.setdefault(alias, set()).update(types)
        for alias, typ in re.findall(r"\[(\w+):(\w+)\]", stmt):
            alias_types.setdefault(alias, set()).update({typ} if typ in edges else set(edges))
        if m:
            setvars[m.group(1)] = alias_types.get(m.group(2), all_v)
    for ln, line in enumerate(_strip(raw).splitlines(), 1):
        for alias, attr in re.findall(r"\b([a-z]\w*)\.([a-z_]\w*)\b", line):
            if alias in tuple_locals or alias not in alias_types:
                continue
            allowed: set[str] = set()
            for t in alias_types[alias]:
                allowed.update(vertices.get(t, []) + edges.get(t, []))
            if attr not in allowed:
                hint = f" (stored as {RENAMES[attr]})" if attr in RENAMES else ""
                problems.append(f"{name}:{ln}: {alias}.{attr} is not an attribute of {sorted(alias_types[alias])}{hint}")
    return problems


def schema_md_vertices(md_path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in open(md_path).read().splitlines():
        m = re.match(r"\| `(\w+)` \| `\w+`.*?\| `(.*?)`", line)
        if m and " → " not in line:
            out[m.group(1)] = ["id"] + [RENAMES.get(a, a) for a in re.findall(r"(\w+) (?:INT|STRING|FLOAT|DATETIME|BOOL)\b", m.group(2))]
    return out


def main() -> int:
    vertices, edges = parse_schema(open(os.path.join(ROOT, "graph", "schema.gsql")).read())
    problems: list[str] = []
    md_path = next((p for p in (os.path.join(ROOT, "contracts", "schema.md"), os.path.join(os.path.dirname(ROOT), "contracts", "schema.md")) if os.path.exists(p)), None)
    if md_path:
        for v, attrs in schema_md_vertices(md_path).items():
            if vertices.get(v) != attrs:
                problems.append(f"schema.md {v} attrs != schema.gsql: md {attrs} vs gsql {vertices.get(v)}")
    files = sorted(f for f in glob.glob(os.path.join(QUERIES, "*.gsql")) if not f.endswith("install_all.gsql"))
    for f in files:
        problems += lint_file(f, vertices, edges)
    y = yaml.safe_load(open(os.path.join(ROOT, "mcp", "query_descriptions.yaml")))["queries"]
    names = {os.path.basename(f)[:-5] for f in files}
    for q, spec in y.items():
        if list(spec.get("parameters", {})) != list(spec.get("example", {})):
            problems.append(f"yaml {q}: parameters keys != example keys")
    for q in sorted(names - set(y)):
        problems.append(f"yaml: no entry for graph/queries/{q}.gsql")
    for q in sorted(set(y) - names):
        problems.append(f"yaml: entry {q} has no graph/queries/{q}.gsql")
    for p in problems:
        print("PROBLEM:", p)
    print(f"{len(files)} query files, {len(y)} yaml entries, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

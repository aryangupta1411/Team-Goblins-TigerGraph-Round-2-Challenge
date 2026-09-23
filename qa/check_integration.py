"""Offline integration lint (Module H): do the query contracts agree across the modules?

For every graph/queries/*.gsql: parameter names (in order) must equal
  * mcp/query_descriptions.yaml `parameters` keys and `example` keys      (typed tools + descriptions)
  * engine.facts_duckdb.DuckFacts.<query> signature                        (mock backend / oracle)
  * agent.mcp_client.BUILTIN_QUERIES[query]["parameters"] when present     (fallback tool table)
and the PRINT ... AS keys must equal the yaml `prints` list.
Also checks: every schema.md vertex attribute list == graph/schema.gsql (the only allowed rename is
proxy -> proxy_type), and every `alias.proxy` in the queries is spelled `proxy_type`.

    uv run python -m qa.check_integration          # exit 1 on any mismatch
"""
from __future__ import annotations

import glob
import inspect
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUN_MODE", "mock")
RENAMES = {"proxy": "proxy_type"}


def gsql_sig(path: str) -> tuple[str, list[str], list[str]]:
    txt = open(path).read()
    m = re.search(r"CREATE OR REPLACE QUERY (\w+)\s*\((.*?)\)\s*FOR GRAPH", txt, re.S)
    params = [p.strip().split()[-1] for p in re.split(r",(?![^<]*>)", m.group(2)) if p.strip()]
    prints: list[str] = []
    body = txt.split("{", 1)[1]
    for pm in re.finditer(r"^\s*PRINT\s+(.*?);", body, re.S | re.M):
        for part in re.split(r",(?![^\[]*\])", pm.group(1)):
            k = re.search(r"AS\s+(\w+)\s*$", part.strip())
            prints.append(k.group(1) if k else part.strip().split("[")[0].replace("@@", "").strip())
    return m.group(1), params, prints


def main() -> int:
    errors: list[str] = []
    from agent.mcp_client import BUILTIN_QUERIES
    from engine.facts_duckdb import DuckFacts
    y = yaml.safe_load(open(os.path.join(ROOT, "mcp", "query_descriptions.yaml")))["queries"]
    for f in sorted(glob.glob(os.path.join(ROOT, "graph", "queries", "*.gsql"))):
        if f.endswith("install_all.gsql"):
            continue
        name, params, prints = gsql_sig(f)
        if name in y:
            yp, ye, yk = list(y[name].get("parameters", {})), list(y[name].get("example", {})), y[name].get("prints")
            if yp != params:
                errors.append(f"{name}: yaml parameters {yp} != gsql {params}")
            if ye != params:
                errors.append(f"{name}: yaml example keys {ye} != gsql {params}")
            if yk is not None and set(yk) != set(prints):
                errors.append(f"{name}: yaml prints {yk} != gsql {sorted(set(prints))}")
        if hasattr(DuckFacts, name):
            dp = [p for p in inspect.signature(getattr(DuckFacts, name)).parameters if p != "self"]
            if dp != params:
                errors.append(f"{name}: DuckFacts signature {dp} != gsql {params}")
        bp = BUILTIN_QUERIES.get(name, {}).get("parameters")
        if bp and list(bp) != params:
            errors.append(f"{name}: agent BUILTIN_QUERIES parameters {list(bp)} != gsql {params}")
        for ln, line in enumerate(open(f), 1):
            if re.search(r"\b[a-z]\.proxy\b", line):
                errors.append(f"{os.path.basename(f)}:{ln}: attribute reference `.proxy` — schema.gsql stores it as proxy_type")
    # schema.md vs schema.gsql
    sys.path.insert(0, os.path.join(ROOT, "graph"))
    from check_contracts import parse_schema  # type: ignore
    md_path = next(p for p in (os.path.join(ROOT, "contracts", "schema.md"), os.path.join(os.path.dirname(ROOT), "contracts", "schema.md")) if os.path.exists(p))
    md = open(md_path).read()
    vertices, _ = parse_schema(open(os.path.join(ROOT, "graph", "schema.gsql")).read())
    for line in md.splitlines():
        m = re.match(r"\| `(\w+)` \| `\w+`.*?\| `(.*?)`", line)
        if m and " → " not in line:
            want = ["id"] + [RENAMES.get(a, a) for a in re.findall(r"(\w+) (?:INT|STRING|FLOAT|DATETIME|BOOL)\b", m.group(2))]
            if vertices.get(m.group(1)) != want:
                errors.append(f"schema.md {m.group(1)} attrs != schema.gsql: {vertices.get(m.group(1))}")
    for e in errors:
        print("MISMATCH:", e)
    print("OK" if not errors else f"{len(errors)} mismatch(es)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

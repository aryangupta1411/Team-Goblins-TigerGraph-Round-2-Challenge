"""graph/describe_queries.py — registers the query and parameter descriptions from mcp/query_descriptions.yaml
through pyTigerGraph updateQueryDescription (PUT /gsql/v1/description?graph=FraudGraph), offline, so the
MCP's get_query_description / get_query_metadata show the LLM the exact JSON parameter examples.
Run after every install_all.py (CREATE OR REPLACE keeps descriptions only when the query text is unchanged).

    uv run python graph/describe_queries.py
"""

from __future__ import annotations

import json
import os
import sys

import yaml
from install_all import connect

HERE = os.path.dirname(os.path.abspath(__file__))
YAML = os.path.join(os.path.dirname(HERE), "mcp", "query_descriptions.yaml")


def main() -> int:
    spec = yaml.safe_load(open(YAML))
    conn = connect()
    failures = 0
    for name, q in spec["queries"].items():
        desc = q["description"].strip() + "\nExample params: " + json.dumps(q["example"], separators=(",", ":"))
        params = {p: str(d).strip() for p, d in q.get("parameters", {}).items()}
        try:
            res = conn.updateQueryDescription(name, desc, params)
            print("OK ", name, str(res)[:80])
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("ERR", name, e)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""mcp/normalize.py — turns the raw `results` array of an installed query into the flat dict the contracts
describe (interfaces.md §A). agent/mcp_client.run_query MUST pass every tigergraph__run_installed_query
result through `normalize()` before it reaches the engine or the LLM.

Rules (deterministic, no schema knowledge needed):
  1. `results` is a list of PRINT objects; their keys are merged into one dict (later keys win).
  2. A printed VERTEX SET (list of {"v_id", "v_type", "attributes"}) is replaced by the list of its
     `attributes` dicts with "id" set to v_id — none of the module-C queries print vertex sets today, but
     the rule keeps the LLM-facing shape stable if a query is rewritten that way.
  3. A string value that looks like a JSON array/object under a key ending in `_ids`, `_cards`, or named
     `region_cluster_30d` / `lookalike_cards` is json-decoded (fallback for the parse_json_array path).
  4. Keys documented as singleton objects that a query prints as a one-element list (only
     card_testing_check.run) are unwrapped by the engine, not here.
The MCP formatter echoes the payload twice (once in the model_dump JSON, once in the "Data" block);
mcp_client extracts the first JSON block and reads `data.results` (or `data.result.results` when the
response wraps pyTigerGraph's list) before calling normalize().
"""
from __future__ import annotations

import json
from typing import Any

_JSONISH_KEYS = ("region_cluster_30d", "lookalike_cards", "burst_lookalike_ids", "home_regions",
                 "usual_products", "known_device_ids", "recurring_amounts", "initial_actions", "final_actions")


def _is_vertex(o: Any) -> bool:
    return isinstance(o, dict) and "v_id" in o and "v_type" in o and "attributes" in o


def _maybe_json(key: str, v: Any) -> Any:
    if isinstance(v, str) and (key in _JSONISH_KEYS or key.endswith("_ids")) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _walk(key: str, v: Any) -> Any:
    if isinstance(v, list):
        if v and all(_is_vertex(x) for x in v):
            return [dict(x["attributes"], id=x["v_id"]) for x in v]
        return [_walk(key, x) for x in v]
    if _is_vertex(v):
        return dict(v["attributes"], id=v["v_id"])
    if isinstance(v, dict):
        return {k: _walk(k, x) for k, x in v.items()}
    return _maybe_json(key, v)


def normalize(results: list[dict]) -> dict:
    """Merge the PRINT objects of one installed-query response into a single flat dict."""
    out: dict[str, Any] = {}
    for block in results or []:
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            out[k] = _walk(k, v)
    return out


def unwrap_singleton(d: dict, key: str) -> dict:
    """card_testing_check.run is printed as a one-element list (GroupByAccum); return the object or {}."""
    v = d.get(key)
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}

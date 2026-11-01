"""Per-token prices for the models the agent calls, and the cost of one recorded LLM call.

Source: Google AI for Developers, "Gemini Developer API pricing" (https://ai.google.dev/gemini-api/docs/pricing),
paid tier, Standard tab, read 2026-11-01 (re-check the page before quoting a dollar figure):
  gemini-2.5-flash  input (text/image/video) $0.30 per 1M tokens, output including thinking tokens $2.50, cache reads $0.03
  gemini-3.6-flash  input $0.75, output including thinking tokens $3.75, cache reads $0.075 (the published price through
                    2026-12-31; it doubles from 2027-01-01). gemini-3.5-flash requests are answered by 3.6 on our key
                    (modelVersion gemini-3.6-flash), so calls.jsonl names 3.6.
Cache storage is billed per hour and is not in calls.jsonl, so it is left out.

calls.jsonl splits usage the way agent/llm.py maps Gemini usageMetadata: `input_tokens` is the prompt minus the cached
part, `cache_read_tokens` is the cached part, `output_tokens` is candidates + thoughts. Each count is priced at its own
rate; there is no double counting.
"""
from __future__ import annotations

PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing (paid tier, read 2026-11-01)"

# USD per 1M tokens
PRICES: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075, "cache_write": 0.0},
}
DEFAULT_MODEL = "gemini-2.5-flash"


def prices_for(model: str | None) -> dict[str, float] | None:
    """The price row for `model` (a versioned name such as gemini-2.5-flash-001 matches its base), else None."""
    m = (model or "").strip().lower()
    if m in PRICES:
        return PRICES[m]
    for base, row in PRICES.items():
        if m.startswith(base):
            return row
    return None


def call_cost_usd(row: dict) -> float | None:
    """USD for one `kind == "llm"` calls.jsonl row; None when its model has no price (mock, fake)."""
    p = prices_for(row.get("model"))
    if p is None:
        return None
    return sum(int(row.get(f"{k}_tokens", 0) or 0) * p[k] for k in ("input", "output", "cache_read", "cache_write")) / 1e6


def case_cost(calls: list[dict]) -> dict:
    """{usd, input_tokens, output_tokens, cache_read_tokens, llm_calls, unpriced_calls} over one case's calls.jsonl."""
    out = {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "llm_calls": 0, "unpriced_calls": 0}
    for r in calls:
        if r.get("kind") != "llm":
            continue
        out["llm_calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_tokens"):
            out[k] += int(r.get(k, 0) or 0)
        c = call_cost_usd(r)
        if c is None:
            out["unpriced_calls"] += 1
        else:
            out["usd"] += c
    out["usd"] = round(out["usd"], 6)
    return out

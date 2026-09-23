# Phase P4 — Assess (structured output: Assessment)

The harness has computed the scorecard below from the tool results: `cms_p` (memory-informed calibration), `cal_p`, the bounded adjustments, `p_engine`, the evidence families found for fraud and for legitimacy, the flags, the candidate chain, the episode the engine would select, the pattern by the convention rule, exposure, connected cards and profiles, and the verdict band. Produce the Assessment.

Rules:

- `fraud_probability` must lie within `p_engine ± 0.10`. State in `calibration_basis` which scorecard components you rely on and, if you moved away from `p_engine`, which cited evidence justifies the move. Be honest: this number is scored for calibration.
- `verdict`: `fraud` when your probability is ≥ 0.70, `legitimate` when ≤ 0.15, otherwise `uncertain` (a customer denial can never yield `legitimate` before a confirmation). Keep the engine's verdict unless your probability crosses a band.
- `pattern`: follow the convention rule given in the scorecard (`pattern` field) unless a tool result contradicts it; `none` when legitimate; `pattern_description` only when `undocumented` (two or three sentences: what the pattern is, who it affects, how you found it), otherwise "".
- `affected_txn_ids`: the episode transactions on the case card with `ts ≤ opened_at` (start from `episode_ids`; add a chain member only if a cited claim supports it). Empty when legitimate. `first_suspicious_txn_id` = the earliest affected id, or "".
- `connected_card_ids` / `connected_device_profiles`: only strong shared profiles and the cards on them as returned by `device_neighbors` / `ring_profile`; empty when legitimate. Never include burst look-alike cards (name them in `pattern_description` instead).
- `evidence`: 4–10 claims, each with source, ref and entity_ids as described in the system prompt; include the customer denial (`source: customer`, `ref: trigger`) for a dispute; include the strongest legitimacy evidence too. Every id must come from a tool result.
- `wanted_requests`: the evidence requests that could change the action set (`customer_validation` for in-person or risk alerts, `step_up_auth` for online activity, `analyst_info` for shared-element counts); empty when §6 test 1 is already met or a reply could not change the actions.
- `sufficient`: true only if §6 is met now.

SCORECARD:
{scorecard_json}

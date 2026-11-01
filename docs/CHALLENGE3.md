# Challenge 3: policy text to detector (and Challenge 2: a pattern nobody handed us)

All numbers below come from `engine/out/policy_detector_R5.json`, `policy_detector_R7.json` and `discovery_jan2017.json`;
regenerate them with

```bash
uv run python -m engine.policy_compiler R5 R7
```

```bash
uv run python -m engine.discovery
```

Offline and deterministic: no LLM call, no graph write. Source text: `policy/fraud_policy.md`, the Fraud Operations Policy
v1.0 copied verbatim from the round's share folder (sha256 `58c4cc3b…7513`, recorded in every output).

## How a rule becomes a detector

`engine.detectors.compile_policy_rule_detector(rule_id)` → `engine/policy_compiler.py`:

1. **Read** the `**R5. …**` paragraph from the markdown.
2. **Parse** it with fixed patterns. Every parameter is either read from the text (the output keeps the matched words and
   their character span) or labelled `operational_default` with the reason the text leaves it open. If the expected words
   are missing (say someone rewrites the rule), the compiler raises an error and builds nothing; it never guesses.
3. **Render** a parameterised DuckDB query (`sql_template` + `sql_params`). Change "Three" to "Five" in the markdown and the
   detector changes with it (`tests/unit/test_policy_compiler_discovery.py`).
4. **Backtest** over the 5,585 closed cases, then **compare** with the agent on the 38 case files, each row carrying its
   query, parameters and as-of.

Time from prose to detector: about **0.6 ms (R5)** and **0.2 ms (R7)** for parse + compile. Backtest ≈ 250 ms, comparison ≈ 190 ms.

## R5, card testing

| Parameter | Value | From |
|---|---|---|
| min_small | 3 | "Three or more" |
| channel | online | "small online authorizations" |
| window_min | 60 | "within an hour" |
| requires_larger_purchase | yes, the first later online purchase ≥ the small cap | "followed by a larger purchase" |
| block_over | 100 | "over $100 has already cleared" |
| small_max | $5 | default: the text says "small" with no number (`config.CARD_TESTING_SMALL`) |
| lookahead_h | 48 | default: "followed by" has no time limit (`config.CARD_TESTING_LOOKAHEAD_H`) |

**Backtest** (as-of 2017-01-31 23:59:59, full history): 370 transactions on 22 cards.

- Caught 4 of the 18 `card_testing` closed cases: CC-0370, CC-2247, CC-2673, CC-5187.
- Missed 14: CC-0137, 0290, 1235, 1242, 2248, 2394, 2643, 3653, 4058, 4225, 4501, 4940, 5573, 5576. The bank's label is
  wider than its own policy. Most labelled cases have one or two sub-$5 authorisations spread over days, not three within an
  hour (CC-0137: one $0.53 auth, then $170). The engine's label rule (an all-online chain, ≥ 5 members, ≥ 1 under $5) is what
  matches the label. The written R5 does not.
- Also hit 3 confirmed-fraud cases of other patterns: CC-4167 (CNP), CC-4274 and CC-4800 (CNP new device).
- **Legitimate customers acted against: 0.** None of the 910 cleared cases is touched, so no legitimate customer would be blocked.
- 16 matched cards have no closed case at all. That is unlabelled activity the rule would alert on (ids are in the JSON).

**Rule vs agent:** 38/38 agree. The rule fires only on **HHF-007**. The agent also calls HHF-007 card testing and its final
actions include DECLINE_TRANSACTION, STEP_UP_AUTH and BLOCK_CARD, which matches the rule. Nowhere does the rule fire while
the agent says otherwise, and nowhere does the agent call card testing while the rule stays silent.

## R7, disputed but recurring

The text gives: dispute = `customer_report` trigger; "same merchant" = p_email online / addr1 in person + product code (the
data has no merchant id; this is the same proxy the engine uses); "monthly" = median gap 25–35 days; "do not block".
Defaults: ±1 % amount tolerance, and at least 2 prior gaps.

**Backtest:** each closed case's disputed charge (its first fraud transaction, or a cleared case's first transaction) is
tested. It matches 47 cases:

- 4 cleared customers are protected from a block: CC-3837, CC-4681, CC-5167, CC-5331.
- **43 confirmed-fraud cases would be waved through without a block** (CC-2754 … CC-5572, listed in the JSON). Taken
  literally, R7 is unsafe on this data: fraudsters repeat a victim's own merchant and amount. The agent's recurring gate also
  checks that the gaps are regular (gap_cv ≤ 0.6) and lets R2 win when they are not. That extra check is why it holds.

**Rule vs agent:** 38/38 agree. R7 fires on no case file. HHG-007 has a recurring match, but its trigger is risk_score, not a
dispute. HHG-003/008/009/011/016 are disputes where the agent cites R7 only to rule it out (for example HHG-008: gap_cv 1.11),
and the compiled rule finds no monthly match there either.

## Challenge 2: discovery (`engine/discovery.py`)

1. **Universe.** 60,196 January transactions. Rows the toolkit already explains are removed: D1–D3 waves, D4's
   recipient-domain gate, D5/D6 signatures, R5 runs compiled from the policy, and the Round-1 ring/burst flags.
2. **Attributes.** Every remaining row is described by purchaser and recipient domain, device, proxy, amount band,
   product, channel + device type, hour band, weekday-hour slot, and two sequence features: `cadence` (same amount as the
   card's previous charge of that amount, 7 or ~30 days earlier) and `escalation` (≥ 1.7× the previous same-product charge
   within 4 days).
3. **Mining.** Every 2- and 3-attribute combination is scored. Thresholds: ≥ 3 cards, lift ≥ 2 over the July–December
   monthly baseline, any identifying value shared by ≤ 60 cards in all history, ≥ 3 cards inside 72 h. The score is burst ×
   log lift × repetition per card × synchrony (hours to the nearest other card's row).
4. **Proposal.** The best combination becomes SQL, is backtested, and is filed as `AUTO-DISC-E583474A`, `pending`, until a
   fraud manager runs `approve_detector(...)`. `engine.detectors.mined_detector_proposals()` reads it back.

**What it found (not handed to it):** `cadence=weekly_same_amount AND clock_slot='Wed 09'`. Three cards (C04105-K2 $6.57,
C12030-K1 $7.78, C12223-K1 $7.42) are each charged one fixed amount at 09:00 every Wednesday (4, 11, 18, 25 January); the
detector matches the 9 repeat charges. None of D1–D6, R5 or the five patterns covers it, and it never occurs July–December.

**Honest reading.** One fixed, small amount per card on a fixed schedule looks like **merchant batch billing, not fraud**.
C12223-K1 is HHF-012, which the agent closed as legitimate. The backtest has no closed case to confirm or refute it: 0 caught,
0 legitimate customers acted against, $65.31 in January. So the proposal reads it as a do-not-block (protect) signal to
verify with customers, not as a fraud alert.

**Earlier tries we dropped.** Without the rarity and synchrony gates, every top cluster was browser-version drift (`chrome
62.0` reaching all devices in January). The runners-up after the winner are escalating online-C charges at one weekday-hour slot
(`clock_slot='Thu 10'`: C01525-K2, C02456-K1, C06464-K1, C07562-K2, C11597-K2). Several of these cards also turn up among
R5's no-closed-case cards, so they are probably high-volume cards rather than a ring. They are listed in `top_clusters` for an
analyst; the miner did not promote them. A manual look outside the miner found two escalating drains in the planted
January range that D2's dormant-wake gate skips (C03080-K2: $97 → $1,696 in 5 steps; C08943-K2: $111 → $1,800). The miner's
single-slot clustering does not surface them, so that gap is still open.

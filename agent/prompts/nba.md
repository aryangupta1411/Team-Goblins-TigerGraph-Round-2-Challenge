# Phase {phase} — Next best action ({stage}) (structured output: ActionChoice)

Choose the recommendation for this stage from the admissible set below. The policy gate computed it from the current scorecard and the Fraud Policy; you may not add a forbidden action or omit a required one. Give each action its route exactly as listed and a one-sentence reason that cites the rule (for example `R2: customer denied; exposure $166.97 ≤ $2,500`, `3a: exposure $1,906.07 > $1,000 and undocumented pattern`, `R8: evidence conflicts (recurrence vs. the card's fraud history in region 126.0)`). Cite R1 only when the probability is below 0.70 with a single evidence family; when a verification is still asked at 0.70 ≤ p < 0.85 cite §3b / §5. Order the actions by what happens first.

Optional actions from the `allowed` list should be added only when a cited claim supports them (for example `MONITOR_CARD` while a reply is pending, `GENERATE_REPORT` when no case was opened and p < 0.30, `WARN_CUSTOMER` under R7).

Stage: {stage}
Probability now: {p} (verdict {verdict}, families for fraud: {families_fraud}; for legitimacy: {families_legit}; exposure ${exposure})
{post_evidence_note}

ADMISSIBLE SET:
{admissible_json}

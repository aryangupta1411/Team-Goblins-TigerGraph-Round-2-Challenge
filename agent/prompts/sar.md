# Phase P8 — Suspicious activity report narrative (structured output: SarDraft)

The policy gate decided that a report must be filed ({sar_reason}). Write the narrative a regulator will read. It must stand on its own: **who** (customer, cards, device profiles, merchants where known), **what** happened, **when** (dates), **where** (billing regions, channels), **how** it was carried out, and **why** it is suspicious. Six to twelve sentences, one paragraph, plain prose. Use only facts from the case data below; every amount, date and id must appear there. Do not use headings, tables, tabs or the phrase "see attached".

Follow this skeleton, one or two sentences each:

S1. Typology and the internal case id ({graph_case_id}).
S2. Who: customer {customer_id}, card {card_id}, the device profile(s) and any connected cards.
S3–S5. What / when / how, chronologically: each affected transaction with date, amount, channel, region and device; the shared element and how the graph connects it to other cards or prior cases.
S6. Why unusual against the cardholder's baseline (median amount, usual regions, usual devices, prior online history).
S7. Prior reports on this card ({prior_sar_note}) and the OFAC screening result ({ofac_note}).
S8. Actions taken or recommended (case opened, card block pending approval, connected cards monitored) and the total unauthorised amount ${exposure}.

`subjects` = the ids of the customer, cards and device profiles you name (exact strings).

FinCEN guidance retrieved for this report:
{chunks}

Case data:
{case_json}

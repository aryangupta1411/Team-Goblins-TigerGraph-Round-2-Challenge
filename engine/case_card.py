"""Which card a case investigates when the case pack and the transaction record disagree.

The dataset's card rule is `customer_id || '-K' || dense_rank(card6)` with an empty card6 ranking first, so a customer
with a few empty-card6 rows gets a thin `-K1` and their real card becomes `-K2`. HHF-011's pack names C09241-K1 (16
empty-card6 transactions) while the flagged $881.55 debit purchase is on C09241-K2 (6,897 transactions). Judging that
alert from the thin card's history would taint it, so the investigation follows the card the transaction is recorded on
and says so in a data-quality evidence item (Challenge 11).
"""
from __future__ import annotations

import dataclasses

from engine.types import CaseContext, Evidence


def resolve(ctx: CaseContext, txn_card_id: str, n_pack_card_txns: int | None = None, n_txn_card_txns: int | None = None) -> tuple[CaseContext, Evidence | None]:
    """Return (context on the transaction's own card, data-quality evidence) when they differ, else (ctx, None)."""
    txn_card_id = str(txn_card_id or "")
    if not txn_card_id or txn_card_id == ctx.card_id:
        return ctx, None
    sizes = ""
    if n_pack_card_txns is not None and n_txn_card_txns is not None:
        sizes = f" ({ctx.card_id} holds {n_pack_card_txns:,} transactions, {txn_card_id} holds {n_txn_card_txns:,})"
    ev = Evidence(
        claim=(f"Data quality: the case pack names card {ctx.card_id}, but transaction {ctx.flagged_txn_id} is recorded on card "
               f"{txn_card_id}{sizes}; the investigation uses the card the transaction is on, and this case is flagged as tainted "
               f"by the card-id mismatch."),
        source="graph",
        ref=f"query:case_context(txn={ctx.flagged_txn_id})",
        entity_ids=[ctx.flagged_txn_id, txn_card_id, ctx.card_id],
        family="history",
        direction="neutral",
        weight=0.0,
    )
    return dataclasses.replace(ctx, card_id=txn_card_id), ev


def resolve_with_duckdb(ctx: CaseContext, fx) -> tuple[CaseContext, Evidence | None]:
    """`resolve` with the transaction's card and both cards' sizes read from the facts DB (offline engine)."""
    if not str(ctx.flagged_txn_id).isdigit():
        return ctx, None
    df = fx.q("SELECT card_id FROM txc WHERE TransactionID = ?", int(ctx.flagged_txn_id))
    if not len(df):
        return ctx, None
    txn_card = str(df.card_id[0])
    if txn_card == ctx.card_id:
        return ctx, None
    n = {r.card_id: int(r.n) for r in fx.q("SELECT card_id, count(*) n FROM txc WHERE card_id IN (?, ?) AND ts <= ?::TIMESTAMP GROUP BY 1",
                                            ctx.card_id, txn_card, ctx.opened_at).itertuples()}
    return resolve(ctx, txn_card, n.get(ctx.card_id, 0), n.get(txn_card, 0))

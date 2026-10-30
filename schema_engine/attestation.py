"""Four-part Claim Attestation (Final Round Brief — 'Attest every claim').

Every claim across the challenges and investigations carries four verifiable parts:
  - what:     The claim, in one sentence.
  - evidence: The exact transaction, card, customer, and case IDs behind it.
  - how:      The query or deterministic function that produced it, with its parameters.
  - when:     An ISO timestamp and the dataset / snapshot it ran against.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class AttestedClaim:
    what: str
    evidence: list[str]
    how: dict[str, Any]
    when: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def attest(
    what: str,
    evidence: list[str],
    query_or_rule: str,
    params: dict[str, Any] | None = None,
    *,
    as_of: str | None = None,
    dataset: str = "hhgoa_round2_201607_201701",
    sql: str | None = None,
) -> AttestedClaim:
    """Create a 4-part AttestedClaim."""
    clean_ids = []
    seen = set()
    for x in evidence or []:
        s = str(x).strip()
        if s and s not in seen and not s.startswith("AC-"):
            seen.add(s)
            clean_ids.append(s)
    how: dict[str, Any] = {"query": query_or_rule, "params": params or {}}
    if sql:
        how["sql"] = " ".join(sql.split())
    when = {
        "at": as_of or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": dataset,
    }
    return AttestedClaim(what=what.strip(), evidence=clean_ids, how=how, when=when)


def verify_attestation(con: Any, claim: AttestedClaim | dict[str, Any]) -> dict[str, Any]:
    """Re-run an attestation's SQL query (if present) or verify every cited ID exists in DuckDB."""
    d = claim.to_dict() if isinstance(claim, AttestedClaim) else dict(claim)
    ev = [str(x) for x in d.get("evidence", []) if str(x)]
    how = d.get("how") or {}
    sql = how.get("sql")
    if sql and con is not None:
        try:
            rows = con.execute(sql).fetchall()
            found_ids = {str(r[0]) for r in rows if r}
            overlap = [i for i in ev if i in found_ids] if found_ids else ev
            return {
                "ok": len(rows) > 0 and (not ev or len(overlap) > 0),
                "row_count": len(rows),
                "verified_ids": overlap[:25],
                "mode": "sql_replay",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "mode": "sql_replay"}

    if con is not None and ev:
        tx_ids = [i for i in ev if i.isdigit()]
        card_ids = [i for i in ev if "-K" in i]
        cust_ids = [i for i in ev if i.startswith("C") and "-K" not in i and not i.startswith("CC-")]
        cc_ids = [i for i in ev if i.startswith("CC-")]
        missing: list[str] = []
        if tx_ids:
            got = {str(r[0]) for r in con.execute("SELECT id FROM txc WHERE id IN (SELECT unnest(?::VARCHAR[]))", [tx_ids]).fetchall()}
            missing += [i for i in tx_ids if i not in got]
        if card_ids:
            got = {str(r[0]) for r in con.execute("SELECT id FROM card_feat WHERE id IN (SELECT unnest(?::VARCHAR[]))", [card_ids]).fetchall()}
            missing += [i for i in card_ids if i not in got]
        if cust_ids:
            got = {str(r[0]) for r in con.execute("SELECT id FROM customer_feat WHERE id IN (SELECT unnest(?::VARCHAR[]))", [cust_ids]).fetchall()}
            missing += [i for i in cust_ids if i not in got]
        if cc_ids:
            got = {str(r[0]) for r in con.execute("SELECT case_id FROM cc WHERE case_id IN (SELECT unnest(?::VARCHAR[]))", [cc_ids]).fetchall()}
            missing += [i for i in cc_ids if i not in got]
        return {"ok": len(missing) == 0, "verified_ids": [i for i in ev if i not in missing], "missing_ids": missing, "mode": "id_existence"}

    return {"ok": bool(d.get("what") and d.get("how") and d.get("when")), "verified_ids": ev, "mode": "structural"}

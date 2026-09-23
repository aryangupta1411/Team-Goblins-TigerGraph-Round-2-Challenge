"""The single SAR validator (PLAN section 4.8 + research/gap4 checklist; decisions.md D10). Returns a list of
violations; [] == valid. engine.validator, agent (P8) and ui.common.sar_checks all delegate here - do not copy
these rules elsewhere.

    from rag.validate_sar import validate_sar, validate_answer_sar, DuckResolver, StaticResolver
    errs = validate_sar(sar, exposure_usd=187.33, affected_ts=["2016-11-15 20:30:00", ...],
                        case_ids=("HHG-014", "AC-HHG-014"), customer_id="C13487", card_id="C13487-K1",
                        resolver=DuckResolver("data/hhgoa.duckdb"))
    errs = validate_answer_sar(answer, customer_id=..., card_id=..., resolver=...)   # derives the rest from the answer
    uv run python -m rag.validate_sar answer.json [data/hhgoa.duckdb]                 # CLI over an answer file

Checks when sar.file is true:
  V01 narrative present, single paragraph (no newlines, tabs)
  V02 6 <= sentences <= 12
  V03 >= 1 ISO date (YYYY-MM-DD) and >= 1 currency amount ($n.nn)
  V04 both activity_dates appear verbatim in the narrative (D10: the former "transaction dates non-decreasing"
      rule is dropped - a narrative legitimately re-mentions dates as context, e.g. HHG-014's wave window)
  V05 activity_dates == [min, max] of the affected transactions (when affected_ts given), both ISO, first <= last
  V06 total_amount_usd == exposure_usd (+-0.005) and that amount is written in the narrative
  V07 subjects non-empty, include customer_id and card_id, each appears verbatim in the narrative, each resolves
  V08 every C-/card/CC-/transaction id in the narrative resolves against the dataset; AC- ids allowed only as the
      case's own graph_case_id
  V09 forbidden text: 'see attached', 'see attachment', markdown headings/bullets, 'simulated', 'ASSUMED'
  V10 the internal case id (graph_case_id or case_id) is named
  V11 OFAC screening result stated; prior-report status stated
  V12 seed-tell lint: no mention of ':00 seconds' / 'seconds equal to zero' / 'seeded'
  V13 no tables: a '|' is allowed inside a device-profile string ("SM-G935F ... | Android 7.0 | ..."); only a
      leading-pipe table row (`^|` / `\n|`), a `|---` rule line or a tab is rejected (D10)
When sar.file is false: narrative == "", subjects == [], total_amount_usd == 0, activity_dates == [], reason cites a rule.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Protocol

ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
AMOUNT = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\d+(?:\.\d{2})?")
CARD_ID = re.compile(r"\bC\d{5}-K\d\b")
CUST_ID = re.compile(r"\bC\d{5}\b(?!-K)")
CC_ID = re.compile(r"\bCC-\d{4}\b")
AC_ID = re.compile(r"\bAC-[A-Z]{3}-\d{3}\b")
TXN_ID = re.compile(r"\b3[05]\d{5}\b")
FORBIDDEN = ["see attached", "see attachment", "simulated", "assumed (", "assumed:", "```"]
# table markup: a row starting with '|' (at the start or after a line break), a '|---' rule, or a tab (D10 narrative regex)
TABLE_MARKUP = re.compile(r"(?:^|\n)\s*\||\|\s*-{3,}|\t")
SEED_TELL = re.compile(r"(:00 seconds|seconds (equal to|are) zero|seeded|seed tell|synthetic row)", re.I)
RULE_CITE = re.compile(r"\b(R\d{1,2}|3a|3b)\b")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


class Resolver(Protocol):
    def exists(self, kind: str, ident: str) -> bool: ...   # kind in customer | card | closed_case | txn | device


class StaticResolver:
    def __init__(self, ids: dict[str, set[str]]):
        self.ids = ids

    def exists(self, kind: str, ident: str) -> bool:
        return ident in self.ids.get(kind, set())


class DuckResolver:
    """Resolves ids against data/hhgoa.duckdb (tables tx, idn, cc); card ids via the schema.md card rule."""

    def __init__(self, db_path: str | Path):
        import duckdb

        self.con = duckdb.connect(str(db_path), read_only=True)
        self.con.execute("""create temp table _cardmap as
            select customer_id || '-K' || dense_rank() over (partition by customer_id order by coalesce(card6,'')) as card_id
            from (select distinct customer_id, card6 from tx)""")
        self._cache: dict[tuple[str, str], bool] = {}

    def exists(self, kind: str, ident: str) -> bool:
        key = (kind, ident)
        if key in self._cache:
            return self._cache[key]
        q = {
            "customer": ("select 1 from tx where customer_id = ? limit 1", ident),
            "card": ("select 1 from _cardmap where card_id = ? limit 1", ident),
            "closed_case": ("select 1 from cc where case_id = ? limit 1", ident),
            "txn": ("select 1 from tx where TransactionID = ? limit 1", int(ident) if ident.isdigit() else -1),
            "device": ("""select 1 from idn where coalesce(DeviceInfo,'NULL') || ' | ' || coalesce(id_30,'NULL') || ' | ' ||
                          coalesce(id_31,'NULL') || ' | ' || coalesce(id_33,'NULL') = ? limit 1""", ident),
        }[kind]
        ok = self.con.execute(q[0], [q[1]]).fetchone() is not None
        self._cache[key] = ok
        return ok


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text.strip()) if s.strip()]


def validate_sar(sar: dict, *, exposure_usd: float, affected_ts: list[str] | None = None,
                 case_ids: tuple[str, str] = ("", ""), customer_id: str = "", card_id: str = "",
                 resolver: Resolver | None = None, file_report_in_final: bool | None = None) -> list[str]:
    errs: list[str] = []
    for k in ("file", "reason", "narrative", "subjects", "total_amount_usd", "activity_dates"):
        if k not in sar:
            errs.append(f"V00 missing key sar.{k}")
    if errs:
        return errs
    if not str(sar["reason"]).strip():
        errs.append("V00 sar.reason must be non-empty and cite the rule")
    elif not RULE_CITE.search(str(sar["reason"])):
        errs.append("V00 sar.reason must cite a policy rule (R1-R10, 3a)")
    if file_report_in_final is not None and bool(sar["file"]) != bool(file_report_in_final):
        errs.append("V00 sar.file must agree with FILE_REPORT in next_best_actions.final")

    if not sar["file"]:
        if sar["narrative"] != "":
            errs.append("V00 file=false requires narrative == ''")
        if sar["subjects"] != []:
            errs.append("V00 file=false requires subjects == []")
        if float(sar["total_amount_usd"] or 0) != 0:
            errs.append("V00 file=false requires total_amount_usd == 0")
        if sar["activity_dates"] != []:
            errs.append("V00 file=false requires activity_dates == []")
        return errs

    nar = str(sar["narrative"])
    if not nar.strip():
        return errs + ["V01 narrative is empty"]
    if "\n" in nar or "\t" in nar:
        errs.append("V01 narrative must be one paragraph: no newlines or tabs")
    n_sent = len(sentences(nar))
    if not 6 <= n_sent <= 12:
        errs.append(f"V02 narrative has {n_sent} sentences; must be 6-12")
    dates = ISO_DATE.findall(nar)
    if not dates:
        errs.append("V03 narrative has no ISO date (YYYY-MM-DD)")
    if not AMOUNT.search(nar):
        errs.append("V03 narrative has no currency amount")

    ad = sar["activity_dates"]
    if not (isinstance(ad, list) and len(ad) == 2 and all(ISO_DATE.fullmatch(str(d)) for d in ad)):
        errs.append("V05 activity_dates must be two YYYY-MM-DD strings")
    else:
        if ad[0] > ad[1]:
            errs.append("V05 activity_dates first > last")
        if affected_ts:
            ds = sorted(t[:10] for t in affected_ts)
            if [ds[0], ds[-1]] != list(ad):
                errs.append(f"V05 activity_dates {ad} != affected min/max {[ds[0], ds[-1]]}")
        # D10: the chronological-order rule is gone; the narrative must simply name both activity dates
        if ad[0] not in nar or ad[1] not in nar:
            errs.append(f"V04 both activity dates {ad} must appear in the narrative")

    total = float(sar["total_amount_usd"])
    if abs(total - float(exposure_usd)) > 0.005:
        errs.append(f"V06 total_amount_usd {total} != exposure_usd {exposure_usd}")
    if f"${total:,.2f}" not in nar:
        errs.append(f"V06 total amount ${total:,.2f} is not written in the narrative")

    subs = list(sar["subjects"])
    if not subs:
        errs.append("V07 subjects is empty")
    for must in (customer_id, card_id):
        if must and must not in subs:
            errs.append(f"V07 subjects must include {must}")
    for s in subs:
        if s not in nar:
            errs.append(f"V07 subject {s!r} is not named in the narrative")
        if resolver is not None:
            kind = ("card" if CARD_ID.fullmatch(s) else "customer" if CUST_ID.fullmatch(s) else
                    "closed_case" if CC_ID.fullmatch(s) else "device" if " | " in s else "txn" if TXN_ID.fullmatch(s) else "")
            if not kind or not resolver.exists(kind, s):
                errs.append(f"V07 subject {s!r} does not resolve against the dataset")

    if resolver is not None:
        for rx, kind in ((CARD_ID, "card"), (CUST_ID, "customer"), (CC_ID, "closed_case"), (TXN_ID, "txn")):
            for ident in sorted(set(rx.findall(nar))):
                if not resolver.exists(kind, ident):
                    errs.append(f"V08 {kind} id {ident} in narrative does not exist in the dataset")
    for ident in sorted(set(AC_ID.findall(nar))):
        if ident != case_ids[1]:
            errs.append(f"V08 agent case id {ident} may not appear (only the case's own graph_case_id {case_ids[1]!r})")

    low = nar.lower()
    for f in FORBIDDEN:
        if f in low:
            errs.append(f"V09 forbidden text {f!r}")
    if re.search(r"(^|\s)(#{1,6}|\*|-)\s", nar):
        errs.append("V09 markdown heading/bullet characters in narrative")
    if TABLE_MARKUP.search(nar):
        # a '|' inside a device-profile string is fine; a leading-pipe row, a '|---' rule or a tab is a table
        errs.append("V13 table markup in narrative (leading '|' row, '|---' rule or tab); prose only")

    if not any(cid and cid in nar for cid in case_ids):
        errs.append("V10 narrative must name the internal case id")
    if "ofac" not in low:
        errs.append("V11 narrative must state the OFAC screening result")
    if not re.search(r"prior (suspicious activity )?report|no prior|previously (filed|reported)", low):
        errs.append("V11 narrative must state the prior-report status")
    if SEED_TELL.search(nar):
        errs.append("V12 narrative mentions the seeding tell")
    return errs


def answer_resolver(answer: dict, customer_id: str = "", card_id: str = "") -> StaticResolver:
    """A StaticResolver over the ids the answer itself names (affected transactions, connected cards, device profiles,
    similar prior cases, the case's customer/card). Enough for a UI badge or an offline check; the promote-time
    validator resolves against data/ids.duckdb (DuckResolver / agent.validator) instead."""
    case = answer.get("case", {}) or {}
    ids = {
        "txn": set(map(str, case.get("affected_txn_ids", []) or [])),
        "card": set(case.get("connected_card_ids", []) or []),
        "device": set(case.get("connected_device_profiles", []) or []),
        "closed_case": set(case.get("similar_prior_cases", []) or []),
        "customer": set(),
    }
    if case.get("first_suspicious_txn_id"):
        ids["txn"].add(str(case["first_suspicious_txn_id"]))
    if card_id:
        ids["card"].add(card_id)
    if customer_id:
        ids["customer"].add(customer_id)
    for s in (answer.get("sar", {}) or {}).get("subjects", []) or []:
        if CARD_ID.fullmatch(s):
            ids["card"].add(s)
            ids["customer"].add(s.split("-K")[0])
        elif CUST_ID.fullmatch(s):
            ids["customer"].add(s)
    for c in ids["card"]:
        ids["customer"].add(c.split("-K")[0])
    return StaticResolver(ids)


def validate_answer_sar(answer: dict, *, customer_id: str = "", card_id: str = "", affected_ts: list[str] | None = None,
                        resolver: Resolver | None = None) -> list[str]:
    """validate_sar over a README-format answer file: exposure, case ids and FILE_REPORT come from the answer.
    `customer_id` / `card_id` come from the case pack (the answer does not carry them); `affected_ts` from the facts
    when the caller has them. Without a resolver the ids are checked against the answer's own lists (answer_resolver)."""
    case = answer.get("case", {}) or {}
    finals = [a.get("action") for a in (answer.get("next_best_actions", {}) or {}).get("final", []) or []]
    return validate_sar(answer.get("sar", {}) or {}, exposure_usd=float(case.get("exposure_usd", 0) or 0),
                        affected_ts=affected_ts, case_ids=(str(answer.get("case_id", "")), str(case.get("graph_case_id", ""))),
                        customer_id=customer_id, card_id=card_id,
                        resolver=resolver if resolver is not None else answer_resolver(answer, customer_id, card_id),
                        file_report_in_final=("FILE_REPORT" in finals))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    ans = json.loads(Path(argv[0]).read_text())
    db = argv[1] if len(argv) > 1 else None
    errs = validate_answer_sar(ans, resolver=DuckResolver(db) if db else None)
    print("\n".join(errs) if errs else "sar: OK")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

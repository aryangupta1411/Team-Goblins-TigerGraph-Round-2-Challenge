"""Fileable `sar.md` (brief challenge 4): the README `sar` object rendered as a document an analyst can send with
minor edits.

    1. numbered narrative sentences, each ending with the transaction / card / case ids it rests on;
    2. a timeline table (ts, txn id, card, amount, channel, why it matters);
    3. a Mermaid evidence graph (customer, card, transactions, device profiles, connected cards);
    4. subjects, totals and the section-3a reason;
    5. an attestation footer (the queries run, their parameters, the as-of).

Every id is resolved against the engine facts DuckDB (`engine.config.FACTS_DB`). A sentence is kept only when
it is attested: each id it names exists, each 'YYYY-MM-DD at HH:MM' it names is a transaction on a case card,
and each dollar amount it names is a cited transaction amount, the case total or the card's own baseline. A
sentence that fails is left out of the filing and counted in the footer. `answer.json` is not changed: its
narrative stays plain prose in the README answer format.

The document is internal (for the analyst and the regulator); nothing in it is sent to the cardholder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CARD_RE = re.compile(r"\bC\d{5}-K\d+\b")
CUST_RE = re.compile(r"\bC\d{5}\b(?!-K)")
TXN_RE = re.compile(r"(?<![\d.$,])\d{7}(?![\d.,]\d)")
CC_RE = re.compile(r"\bCC-\d+\b")
WHEN_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2})")
USD_RE = re.compile(r"\$(\d[\d,]*\.\d{2})")
SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
MAX_GRAPH_CARDS = 8

TXN_SQL = ("SELECT TransactionID, card_id, customer_id, ts_str, amt, channel, product_cd, addr1, device_id, device_new, "
           "proxy, prior_in_region, prior_on_dev, prior_med_amt FROM txc WHERE TransactionID IN ({ids})")
CARD_TXN_SQL = ("SELECT TransactionID, card_id, customer_id, ts_str, amt, channel, product_cd, addr1, device_id, device_new, "
                "proxy, prior_in_region, prior_on_dev, prior_med_amt FROM txc WHERE card_id IN ({cards}) AND ts_str LIKE ?")
CARD_SQL = "SELECT id, customer_id, median_amt, p90_amt, max_amt, n_txns FROM card_feat WHERE id IN ({cards})"
CASE_SQL = "SELECT case_id, opened_at, flagged_txn_id, card_id, customer_id FROM cp_all WHERE case_id = ?"
CC_SQL = "SELECT case_id, card_id, outcome, report_filed FROM cc WHERE case_id IN ({ids})"
DEV_SQL = "SELECT DISTINCT card_id FROM txc WHERE device_id = ? AND card_id IN ({cards})"


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.split(" ".join((text or "").split())) if s.strip()]


def _ph(n: int) -> str:
    return ", ".join("?" * n)


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _md(s: str) -> str:
    return str(s).replace("|", "\\|")


def _mm(s: str) -> str:
    return str(s).replace('"', "'").replace("|", "#124;")


@dataclass
class Facts:
    """What the renderer looked up, with the queries kept for the footer."""
    case: dict
    txns: dict[str, dict]
    cards: dict[str, dict]
    closed_cases: dict[str, dict]
    queries: list[tuple[str, list]] = field(default_factory=list)

    def run(self, con, sql: str, params: list) -> list[dict]:
        self.queries.append((" ".join(sql.split()), list(params)))
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect(db_path):
    import duckdb

    return duckdb.connect(str(db_path), read_only=True)


def _facts(con, answer: dict, case_id: str) -> Facts:
    f = Facts(case={}, txns={}, cards={}, closed_cases={})
    rows = f.run(con, CASE_SQL, [case_id])
    f.case = rows[0] if rows else {}
    case = answer.get("case") or {}
    ids = [str(t) for t in case.get("affected_txn_ids") or []]
    if ids:
        for r in f.run(con, TXN_SQL.format(ids=_ph(len(ids))), [int(t) for t in ids if t.isdigit()]):
            f.txns[str(r["TransactionID"])] = r
    cards = _case_cards(answer, f)
    if cards:
        for r in f.run(con, CARD_SQL.format(cards=_ph(len(cards))), cards):
            f.cards[r["id"]] = r
    return f


def _case_cards(answer: dict, f: Facts) -> list[str]:
    case = answer.get("case") or {}
    out = [c for c in [f.case.get("card_id"), *(t["card_id"] for t in f.txns.values()), *(case.get("connected_card_ids") or [])] if c]
    return list(dict.fromkeys(out))


@dataclass
class Sentence:
    text: str
    cites: list[str]
    ok: bool
    why: str = ""


def attest(con, f: Facts, answer: dict, sentence: str) -> Sentence:
    """Resolve every id, transaction time and amount the sentence names; return its citation list."""
    sar = answer.get("sar") or {}
    case_card = f.case.get("card_id") or ""
    customer = f.case.get("customer_id") or ""
    cards = _case_cards(answer, f)
    txn_ids: list[str] = []
    other: list[str] = []
    # explicit transaction ids
    named = [t for t in TXN_RE.findall(sentence)]
    missing = [t for t in named if t not in f.txns]
    if missing:
        for r in f.run(con, TXN_SQL.format(ids=_ph(len(missing))), [int(t) for t in missing]):
            f.txns[str(r["TransactionID"])] = r
    for t in named:
        if t not in f.txns:
            return Sentence(sentence, [], False, f"transaction {t} not found")
        txn_ids.append(t)
    # 'YYYY-MM-DD at HH:MM' -> the transaction at that minute on a case card
    for d, hm in WHEN_RE.findall(sentence):
        hit = [t for t in f.txns.values() if str(t["ts_str"]).startswith(f"{d} {hm}") and t["card_id"] in cards]
        if not hit and cards:
            hit = f.run(con, CARD_TXN_SQL.format(cards=_ph(len(cards))), [*cards, f"{d} {hm}%"])
            for r in hit:
                f.txns[str(r["TransactionID"])] = r
        if not hit:
            return Sentence(sentence, [], False, f"no transaction at {d} {hm}")
        txn_ids += [str(t["TransactionID"]) for t in hit]
    # cards / customers / closed cases
    for c in CARD_RE.findall(sentence):
        if c not in f.cards:
            rows = f.run(con, CARD_SQL.format(cards="?"), [c])
            if not rows:
                return Sentence(sentence, [], False, f"card {c} not found")
            f.cards[c] = rows[0]
        other.append(f"card {c}")
    for cu in CUST_RE.findall(sentence):
        if cu != customer and not any(r.get("customer_id") == cu for r in f.cards.values()):
            return Sentence(sentence, [], False, f"customer {cu} not attested")
    named_cc = CC_RE.findall(sentence)
    new_cc = [c for c in named_cc if c not in f.closed_cases]
    if new_cc:
        for r in f.run(con, CC_SQL.format(ids=_ph(len(new_cc))), new_cc):
            f.closed_cases[r["case_id"]] = r
    for c in named_cc:
        if c not in f.closed_cases:
            return Sentence(sentence, [], False, f"closed case {c} not found")
        other.append(f"case {c}")
    # dollar amounts: a cited / affected transaction, the case total, or the card's own baseline
    affected = [str(t) for t in (answer.get("case") or {}).get("affected_txn_ids") or []]
    total = round(float(sar.get("total_amount_usd") or 0), 2)
    for a in USD_RE.findall(sentence):
        v = round(float(a.replace(",", "")), 2)
        pool = [t for t in (txn_ids or affected) if t in f.txns]
        hit = [t for t in pool if round(float(f.txns[t]["amt"]), 2) == v] or \
              [t for t in affected if t in f.txns and round(float(f.txns[t]["amt"]), 2) == v]
        if hit:
            txn_ids += hit
        elif total and v == total:
            txn_ids += [t for t in affected if t in f.txns]
        else:
            base = [c for c, r in f.cards.items() if any(r.get(k) is not None and round(float(r[k]), 2) == v
                                                          for k in ("median_amt", "p90_amt", "max_amt"))]
            prior = [t for t in (txn_ids or affected) if t in f.txns and f.txns[t].get("prior_med_amt") is not None
                     and round(float(f.txns[t]["prior_med_amt"]), 2) == v]
            if base:
                other.append(f"card profile {base[0]}")
            elif prior:
                txn_ids += prior
            else:
                return Sentence(sentence, [], False, f"amount {_usd(v)} not attested")
    if not txn_ids:
        # a sentence about the reported activity as a whole (typology, subjects, screening, actions): it rests on the
        # case's affected transactions on the case card
        txn_ids = [t for t in affected if t in f.txns]
        if not txn_ids and not other:
            if not case_card:
                return Sentence(sentence, [], False, "no case card to cite")
            other.append(f"card {case_card}")
    cites = []
    tx = list(dict.fromkeys(txn_ids))
    if tx:
        cites.append("txn " + ", ".join(tx))
    grouped: dict[str, list[str]] = {}
    for o in dict.fromkeys(other):
        kind, _, val = o.rpartition(" ")
        grouped.setdefault(kind, []).append(val)
    cites += [f"{k} {', '.join(v)}" for k, v in grouped.items()]
    return Sentence(sentence, cites, True)


def _why(t: dict, f: Facts, first: str) -> str:
    bits = []
    tid = str(t["TransactionID"])
    if tid == str(f.case.get("flagged_txn_id") or ""):
        bits.append("flagged transaction")
    if tid == first:
        bits.append("first suspicious transaction")
    if t.get("device_new") == "New":
        bits.append("device marked New")
    if str(t.get("proxy") or "").upper().startswith("IP_PROXY:ANONYMOUS"):
        bits.append("anonymous proxy")
    if t.get("prior_on_dev") == 0 and t.get("device_id"):
        bits.append("first use of this device on the card")
    if t.get("prior_in_region") == 0 and t.get("addr1"):
        bits.append(f"first use in region {t['addr1']}")
    med = t.get("prior_med_amt")
    if med and float(med) > 0 and float(t["amt"]) >= 3 * float(med):
        bits.append(f"{float(t['amt']) / float(med):.1f}x the card's prior median {_usd(float(med))}")
    return "; ".join(bits) or "part of the reported activity"


def _graph(con, f: Facts, answer: dict) -> list[str]:
    case = answer.get("case") or {}
    card = f.case.get("card_id") or ""
    cust = f.case.get("customer_id") or ""
    txns = sorted((f.txns[t] for t in map(str, case.get("affected_txn_ids") or []) if t in f.txns), key=lambda t: t["ts_str"])
    devs = list(dict.fromkeys(t["device_id"] for t in txns if t.get("device_id")))
    conn = list(case.get("connected_card_ids") or [])
    out = ["```mermaid", "graph LR"]
    if cust:
        out.append(f'  cu["customer {_mm(cust)}"] --> ca["card {_mm(card)}"]')
    for i, t in enumerate(txns):
        out.append(f'  ca --> t{i}["txn {t["TransactionID"]}<br/>{_mm(str(t["ts_str"])[:16])} {_mm(_usd(float(t["amt"])))}"]')
        if t.get("device_id"):
            out.append(f"  t{i} --> d{devs.index(t['device_id'])}")
    for j, d in enumerate(devs):
        out.append(f'  d{j}["device {_mm(d)}"]')
    shown = conn[:MAX_GRAPH_CARDS]
    if shown:
        on_dev: dict[str, int] = {}
        for j, d in enumerate(devs):
            for r in f.run(con, DEV_SQL.format(cards=_ph(len(shown))), [d, *shown]):
                on_dev.setdefault(r["card_id"], j)
        hub = None
        for k, c in enumerate(shown):
            if c in on_dev:
                out.append(f'  d{on_dev[c]} --- k{k}["card {_mm(c)}"]')
            else:
                if hub is None:
                    hub = "p"
                    out.append(f'  ca -.-> p(("shared pattern: {_mm(case.get("pattern") or "connected")}"))')
                out.append(f'  p -.- k{k}["card {_mm(c)}"]')
        if len(conn) > len(shown):
            out.append(f'  ca -.-> more["+{len(conn) - len(shown)} more connected cards"]')
    out.append("```")
    return out


def render(answer: dict, case_id: str, db_path) -> tuple[str, dict]:
    """(markdown, stats). stats = {sentences, cited, dropped: [(sentence, why)]}."""
    sar = answer.get("sar") or {}
    case = answer.get("case") or {}
    if not sar.get("file"):
        return f"# No SAR filed: {case_id}\n\n{sar.get('reason', '')}\n", {"sentences": 0, "cited": 0, "dropped": []}
    con = _connect(db_path)
    try:
        f = _facts(con, answer, case_id)
        sents = [attest(con, f, answer, s) for s in split_sentences(sar.get("narrative", ""))]
        L = [f"# Suspicious Activity Report: {case_id}", ""]
        L += [f"- Filing reason (3a): {sar.get('reason', '')}",
              f"- Case opened: {f.case.get('opened_at', '')}; verdict {case.get('verdict', '')} "
              f"(p = {case.get('fraud_probability', '')}), pattern {case.get('pattern', '')}",
              f"- Activity dates: {' to '.join(sar.get('activity_dates') or [])}",
              f"- Total suspicious amount: {_usd(float(sar.get('total_amount_usd') or 0))} "
              f"across {len(case.get('affected_txn_ids') or [])} transaction(s)",
              "", "## Subjects", ""]
        L += [f"- {_md(s)}" for s in sar.get("subjects") or []]
        L += ["", "## Narrative", ""]
        kept = [s for s in sents if s.ok]
        L += [f"{i}. {s.text} [{'; '.join(s.cites)}]" for i, s in enumerate(kept, 1)]
        L += ["", "## Timeline", "", "| ts | txn id | card | amount | channel | why it matters |", "|---|---|---|---|---|---|"]
        first = str(case.get("first_suspicious_txn_id") or "")
        for t in sorted((f.txns[x] for x in map(str, case.get("affected_txn_ids") or []) if x in f.txns), key=lambda t: t["ts_str"]):
            L.append(f"| {t['ts_str']} | {t['TransactionID']} | {t['card_id']} | {_usd(float(t['amt']))} | {t['channel']} | {_md(_why(t, f, first))} |")
        L += ["", "## Evidence graph", ""] + _graph(con, f, answer)
        dropped = [(s.text, s.why) for s in sents if not s.ok]
        L += ["", "## Attestation", "",
              f"Resolved against `{Path(str(db_path)).name}` (read-only), as of case open {f.case.get('opened_at', '')}. "
              f"{len(kept)} of {len(sents)} narrative sentences attested"
              + (f"; {len(dropped)} left out because an id, time or amount could not be resolved." if dropped else "."), ""]
        seen = set()
        for sql, params in f.queries:
            key = (sql, tuple(map(str, params)))
            if key in seen:
                continue
            seen.add(key)
            L.append(f"- `{sql}` params `{[str(p) for p in params]}`")
        return "\n".join(L) + "\n", {"sentences": len(sents), "cited": len(kept), "dropped": dropped}
    finally:
        con.close()


def write(path: Path, answer: dict, case_id: str, db_path) -> dict:
    """Write `sar.md`; on any lookup failure write the plain narrative so the run never loses its SAR text."""
    try:
        md, stats = render(answer, case_id, db_path)
    except Exception as e:  # facts DB missing / locked: the old plain form
        sar = answer.get("sar") or {}
        md, stats = (sar.get("narrative") or f"No SAR. {sar.get('reason', '')}") + "\n", {"error": str(e)[:200]}
    Path(path).write_text(md, encoding="utf-8")
    return stats

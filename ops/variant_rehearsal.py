"""ops/variant_rehearsal.py — Challenge 13 rehearsal: map someone else's schema, translate it, investigate on it.

    uv run python -m ops.variant_rehearsal data/variant_bank_b                      # the checked-in variant
    uv run python -m ops.variant_rehearsal --make-fresh data/variant_rehearsal/bank_c   # build a new reshaped variant, then rehearse it
    uv run python -m ops.variant_rehearsal data/variant_bank_b --cases HHF-001 HHF-008

For a variant folder:
  1. schema_engine.mapper.infer_variant_mapping profiles every file and maps table roles and columns from value fingerprints;
     the mapping (with 5 justifying sample records per column) is written to <out>/mapping.json
  2. the files are translated to the canonical columns with the mapping alone (no file or column names are known here):
     cents -> USD, epoch -> ISO, a date + time pair merged into ts, a device bundle split or four device columns joined,
     channel / card-type synonyms, a card id derived from the customer and card type (the card rule), a customer id
     derived from a card id
  3. every translated transaction is compared, column by column, with the same transaction in the canonical facts DB
  4. a rehearsal facts DB is the canonical one with each translated transaction's raw columns and each translated case
     replaced by the variant's values; the offline engine (engine.run_cases.run_one) decides each case on it and on the
     canonical DB, and the two decisions are compared (verdict, status, final actions, exposure)
Derived history columns (card_seq, prior_*, cms_p, card_feat) stay canonical: a variant extract does not carry six months
of history, so what is rehearsed is that the variant's own fields drive the same decision. Offline: no graph, no LLM.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import duckdb

from ops.console import fail, header, ok, step, summary, warn
from schema_engine.mapper import CARD6_SYNONYMS, CHANNEL_SYNONYMS, infer_variant_mapping

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "data" / "hhgoa_engine.duckdb"
ROUND2 = ROOT / "data" / "raw" / "round2"
OUT = ROOT / "data" / "variant_rehearsal"

# txc column -> how it is compared (the canonical txc stores addr1 / addr2 as text like '325.0', dist1 as -1 when empty)
COMPARE = ("card_id", "customer_id", "ts", "amt", "product_cd", "channel", "addr1", "addr2", "dist1", "p_email", "r_email",
           "risk_score", "has_identity", "device_new", "proxy", "device_type", "device_id")
RAW_COLUMNS = tuple(c for c in COMPARE if c != "customer_id")


# ------------------------------------------------------------------------------------------------ translation
def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _value(raw: str, transform: str) -> Any:
    v = (raw or "").strip()
    if v == "" or v.lower() in ("null", "none", "nan"):
        return None
    if transform == "cents_to_usd":
        return round(float(v) / 100.0, 2)
    if transform == "pct_to_prob":
        return round(float(v) / 100.0, 6)
    if transform == "epoch_to_iso":
        return dt.datetime.fromtimestamp(float(v), tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    if transform == "channel_synonym":
        return CHANNEL_SYNONYMS.get(v.lower(), v)
    if transform == "card6_synonym":
        return CARD6_SYNONYMS.get(v.lower(), v)
    return v


def _by_role(mapping: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in mapping["mappings"]:
        out.setdefault(m["table_role"], []).append(m)
    return out


def _read(variant_dir: Path, fname: str) -> list[dict[str, str]]:
    with (variant_dir / fname).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _canonical_rows(variant_dir: Path, maps: list[dict]) -> list[dict[str, Any]]:
    """One role's rows in canonical names. A split timestamp (date_part + time_part) is merged; a device bundle is split."""
    files = sorted({m["source_file"] for m in maps})
    if len(files) != 1:
        raise ValueError(f"expected one file per table role, the mapping has {files}")
    rows = []
    for r in _read(variant_dir, files[0]):
        c: dict[str, Any] = {}
        parts: dict[str, str] = {}
        for m in maps:
            raw = r.get(m["source_column"], "")
            if m["transform"] in ("date_part", "time_part"):
                parts[m["transform"]] = (raw or "").strip()
            elif m["transform"] == "split_device_bundle":
                bits = [b.strip() for b in (raw or "").split("|")] + ["NULL"] * 4
                for k, b in zip(("DeviceInfo", "id_30", "id_31", "id_33"), bits[:4], strict=True):
                    c[k] = None if b in ("", "NULL") else b
            else:
                c[m["canonical_column"]] = _value(raw, m["transform"])
        if parts:
            c["ts"] = f"{parts.get('date_part', '')} {parts.get('time_part') or '00:00:00'}".strip()
        rows.append(c)
    return rows


def translate(variant_dir: str | Path, mapping: dict, facts_db: str | Path = FACTS) -> dict[str, list[dict[str, Any]]]:
    """The variant as canonical txc rows (raw columns only) and canonical case-pack rows."""
    vdir = Path(variant_dir)
    roles = _by_role(mapping)
    if "transactions" not in roles:
        raise ValueError("the mapper found no transactions table in the variant")
    tx = _canonical_rows(vdir, roles["transactions"])
    idn = {str(r["TransactionID"]): r for r in _canonical_rows(vdir, roles["identity"])} if "identity" in roles else {}
    cases = _canonical_rows(vdir, roles["case_pack"]) if "case_pack" in roles else []
    con = duckdb.connect(str(facts_db), read_only=True)
    # the card rule: a customer's card is identified by its funding type (card_feat.card_type), as etl.card_rule numbers it
    cards = {(str(a), str(b)): str(i) for i, a, b in con.execute("SELECT id, customer_id, card_type FROM card_feat").fetchall()}
    # no funding type on the row (DRIFT-02): the customer's primary card, the one with the most history (lowest id on a tie)
    primary = {str(a): str(i) for a, i in con.execute(
        "SELECT customer_id, arg_min(id, (-n_txns, id)) FROM card_feat GROUP BY 1").fetchall()}
    con.close()
    out_tx = []
    for t in tx:
        tid = str(t.get("TransactionID") or "")
        i = idn.get(tid)
        dev = [i.get(k) for k in ("DeviceInfo", "id_30", "id_31", "id_33")] if i else [None] * 4
        cust = str(t.get("customer_id") or "")
        c6 = t.get("card6") or "unknown"
        out_tx.append({
            "TransactionID": int(tid), "card_id": t.get("card_id") or cards.get((cust, c6)) or (primary.get(cust) if not t.get("card6") else None),
            "customer_id": cust,
            "ts": t.get("ts"), "amt": _num(t.get("TransactionAmt")), "product_cd": t.get("ProductCD"), "channel": t.get("channel"),
            "addr1": t.get("addr1") or "", "addr2": t.get("addr2") or "",
            "dist1": _num(t.get("dist1")) if t.get("dist1") is not None else -1.0,
            "p_email": t.get("P_emaildomain") or "", "r_email": t.get("R_emaildomain") or "", "risk_score": _num(t.get("risk_score")),
            "has_identity": i is not None, "device_new": (i or {}).get("id_15") or "", "proxy": (i or {}).get("id_23") or "",
            "device_type": (i or {}).get("DeviceType") or "",
            "device_id": "" if i is None or all(d is None for d in dev) else " | ".join(d or "NULL" for d in dev),
        })
    out_cp = []
    for c in cases:
        card = c.get("card_id")
        out_cp.append({"case_id": c.get("case_id"), "opened_at": c.get("opened_at"), "trigger_type": c.get("trigger_type"),
                       "trigger_text": c.get("trigger_text"), "flagged_txn_id": str(c.get("flagged_txn_id") or ""), "card_id": card,
                       "customer_id": c.get("customer_id") or (str(card).split("-")[0] if card else None), "risk_score": _num(c.get("risk_score"))})
    return {"txc": out_tx, "cp": out_cp}


# ------------------------------------------------------------------------------------------------ comparison
def _same(col: str, a: Any, b: Any) -> bool:
    if col in ("amt", "risk_score", "dist1"):
        if a is None or b is None or (isinstance(b, float) and b != b):
            return (a is None) == (b is None or (isinstance(b, float) and b != b))
        return abs(float(a) - float(b)) < 0.0051 if col == "amt" else abs(float(a) - float(b)) < 1e-6
    if col == "ts":
        return str(a)[:19] == str(b)[:19]
    return str(a if a is not None else "") == str(b if b is not None else "")


def compare_rows(tx: list[dict], facts_db: str | Path = FACTS) -> dict[str, Any]:
    """Column-by-column agreement of the translated transactions with the canonical txc."""
    con = duckdb.connect(str(facts_db), read_only=True)
    ids = [r["TransactionID"] for r in tx]
    con.execute("CREATE TEMP TABLE _ids(id BIGINT)")
    con.executemany("INSERT INTO _ids VALUES (?)", [[i] for i in ids])
    canon = {int(r[0]): dict(zip(("TransactionID", *COMPARE), r, strict=True)) for r in con.execute(
        f"SELECT TransactionID, {', '.join('ts::VARCHAR' if c == 'ts' else c for c in COMPARE)} FROM txc WHERE TransactionID IN (SELECT id FROM _ids)").fetchall()}
    con.close()
    per_col = {c: {"equal": 0, "differ": 0, "examples": []} for c in COMPARE}
    missing = [i for i in ids if i not in canon]
    for r in tx:
        b = canon.get(r["TransactionID"])
        if b is None:
            continue
        for c in COMPARE:
            if _same(c, r[c], b[c]):
                per_col[c]["equal"] += 1
            else:
                per_col[c]["differ"] += 1
                if len(per_col[c]["examples"]) < 3:
                    per_col[c]["examples"].append({"TransactionID": r["TransactionID"], "variant": r[c], "canonical": b[c]})
    return {"n_translated": len(tx), "n_in_canonical": len(canon), "missing_in_canonical": missing[:10], "columns": per_col,
            "all_equal": not missing and all(v["differ"] == 0 for v in per_col.values())}


def rehearsal_db(tx: list[dict], cases: list[dict], out: Path, facts_db: str | Path = FACTS) -> Path:
    """The canonical facts DB with the translated raw columns and cases written over it (a copy; the original is untouched)."""
    db = out / "rehearsal_facts.duckdb"
    for suffix in ("", ".wal"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copyfile(facts_db, db)
    con = duckdb.connect(str(db))
    import pandas as pd

    con.register("_v", pd.DataFrame(tx))
    sets = ", ".join(f"{c} = CAST(_v.{c} AS {'TIMESTAMP' if c == 'ts' else 'VARCHAR' if c in ('addr1', 'addr2') else 'DOUBLE' if c in ('amt', 'dist1', 'risk_score') else 'BOOLEAN' if c == 'has_identity' else 'VARCHAR'})"
                     for c in RAW_COLUMNS)
    con.execute(f"UPDATE txc SET {sets} FROM _v WHERE txc.TransactionID = _v.TransactionID")
    con.execute("UPDATE txc SET ts_str = strftime(ts, '%Y-%m-%d %H:%M:%S'), id = TransactionID::VARCHAR WHERE TransactionID IN (SELECT TransactionID FROM _v)")
    if cases:
        con.register("_c", pd.DataFrame(cases))
        for table in ("cp_all", "cp_round2"):
            con.execute(f"DELETE FROM {table} WHERE case_id IN (SELECT case_id FROM _c)")
            con.execute(f"INSERT INTO {table} SELECT case_id, opened_at::TIMESTAMP, trigger_type, trigger_text, flagged_txn_id, card_id, customer_id, risk_score FROM _c")
    con.close()
    return db


def decide(db: str | Path, case_ids: list[str]) -> dict[str, dict]:
    """The offline engine's decision for each case on a facts DB."""
    from engine.facts_duckdb import DuckFacts
    from engine.run_cases import contexts, run_one

    fx = DuckFacts(str(db))
    res = {}
    for ctx in contexts(fx, case_ids):
        r = run_one(ctx, fx)
        a = r["answer"]
        case = a.get("case") or {}
        res[ctx.case_id] = {"verdict": case.get("verdict") or r["sc_post"].verdict, "status": case.get("status"),
                            "exposure_usd": case.get("exposure_usd"),
                            "final_actions": sorted(str(x.get("action")) for x in (a.get("next_best_actions") or {}).get("final", [])),
                            "p_engine": round(float(r["sc_post"].p_engine), 4), "validator_errors": len(r["errors"])}
    fx.con.close()
    return res


# ------------------------------------------------------------------------------------------------ a fresh variant
def make_fresh_variant(out_dir: str | Path, round2_dir: str | Path = ROUND2, max_tx_rows: int = 3000) -> Path:
    """A second, differently reshaped variant from the raw January files: other file and column names, column order
    shuffled, the timestamp split into a date and a time, USD with no hint of cents, presence / funding words, a 0..1
    risk probability, the device fields as four separate columns (Bank B merges them), the case pack without customer ids,
    and a column nothing maps. Nothing here is known to the mapper."""
    r2, out = Path(round2_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cp = list(csv.DictReader((r2 / "case_pack_final.csv").open(encoding="utf-8", newline="")))
    must = {r["customer_id"] for r in cp}
    keep = []
    for r in csv.DictReader((r2 / "transactions_2017q1.csv").open(encoding="utf-8", newline="")):
        if r["customer_id"] in must or len(keep) < max_tx_rows:
            keep.append(r)
    ids = {r["TransactionID"] for r in keep}
    presence = {"online": "card_not_present", "in_person": "card_present"}
    funding = {"debit": "debit_card", "credit": "credit_card", "charge card": "charge_card"}
    with (out / "c_postings.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["posting_time", "gross_usd", "party", "ref", "presence", "product_line", "funding", "scheme", "billing_region_cd",
                    "country_code", "home_distance", "buyer_domain", "payee_domain", "fraud_prob", "batch_no", "posting_date"])
        for n, r in enumerate(keep):
            d, t = (r.get("ts") or "2017-01-01 00:00:00").split(" ")
            w.writerow([t, r.get("TransactionAmt") or "", r["customer_id"], r["TransactionID"], presence.get(r.get("channel") or "", ""),
                        r.get("ProductCD") or "", funding.get(r.get("card6") or "", ""), r.get("card4") or "", r.get("addr1") or "",
                        r.get("addr2") or "", r.get("dist1") or "", r.get("P_emaildomain") or "", r.get("R_emaildomain") or "",
                        r.get("risk_score") or "", f"B{n // 500:03d}", d])
    with (out / "c_sessions.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "screen", "browser_name", "os_name", "hw_model", "form_factor", "seen_before", "proxy"])
        for r in csv.DictReader((r2 / "identity_2017q1.csv").open(encoding="utf-8", newline="")):
            if r["TransactionID"] in ids:
                w.writerow([r["TransactionID"], r.get("id_33") or "", r.get("id_31") or "", r.get("id_30") or "", r.get("DeviceInfo") or "",
                            r.get("DeviceType") or "", r.get("id_15") or "", r.get("id_23") or ""])
    with (out / "c_tickets.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticket", "card_ref", "raised_at", "subject_ref", "origin", "message", "alert_score"])
        for r in cp:
            w.writerow([r["case_id"], r["card_id"], r["opened_at"], r["flagged_txn_id"], r["trigger_type"], r["trigger_text"], r.get("risk_score") or ""])
    return out


# ------------------------------------------------------------------------------------------------ run
def rehearse(variant_dir: str | Path, out_dir: str | Path | None = None, case_ids: list[str] | None = None, facts_db: str | Path = FACTS) -> dict:
    vdir = Path(variant_dir)
    out = Path(out_dir) if out_dir else OUT / vdir.name
    if not vdir.is_dir() or not any(vdir.glob("*.csv")):
        raise ValueError(f"no CSV files in {vdir}")
    out.mkdir(parents=True, exist_ok=True)
    step(f"mapping {vdir} (schema_engine.mapper, value fingerprints)")
    mapping = infer_variant_mapping(vdir)
    (out / "mapping.json").write_text(json.dumps(mapping, indent=1, default=str) + "\n", encoding="utf-8")
    ok(f"{mapping['n_mapped_columns']} columns mapped; roles {mapping['table_roles']} -> {out / 'mapping.json'}")
    step("translating the variant to canonical columns with that mapping")
    tr = translate(vdir, mapping, facts_db)
    step(f"comparing {len(tr['txc']):,} translated transactions with the canonical facts DB")
    rows = compare_rows(tr["txc"], facts_db)
    differ = {c: v["differ"] for c, v in rows["columns"].items() if v["differ"]}
    (ok if rows["all_equal"] else warn)(f"{rows['n_in_canonical']:,} of {rows['n_translated']:,} found; columns that differ: {differ or 'none'}")
    want = case_ids or [c["case_id"] for c in tr["cp"]]
    cases = [c for c in tr["cp"] if c["case_id"] in want]
    step(f"rehearsal facts DB + the offline engine on {len(cases)} translated cases, and on the canonical DB")
    db = rehearsal_db(tr["txc"], cases, out, facts_db)
    ids = [c["case_id"] for c in cases]
    on_variant, on_canonical = decide(db, ids), decide(facts_db, ids)
    keys = ("verdict", "status", "final_actions", "exposure_usd")
    cmp = {cid: {"variant": on_variant.get(cid), "canonical": on_canonical.get(cid),
                 "same": cid in on_variant and all(on_variant[cid][k] == on_canonical.get(cid, {}).get(k) for k in keys)} for cid in ids}
    report = {"variant_dir": str(vdir), "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "table_roles": mapping["table_roles"], "n_mapped_columns": mapping["n_mapped_columns"],
              "mapped": [{k: m[k] for k in ("source_file", "source_column", "canonical_column", "transform", "confidence")} for m in mapping["mappings"]],
              "row_comparison": rows, "cases": cmp, "n_cases": len(ids), "n_same_decision": sum(1 for v in cmp.values() if v["same"]),
              "rehearsal_db": str(db)}
    (out / "rehearsal.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("variant_dir", nargs="?", default=str(ROOT / "data" / "variant_bank_b"))
    ap.add_argument("--make-fresh", metavar="DIR", help="first build a freshly reshaped variant from data/raw/round2 into DIR, and rehearse that")
    ap.add_argument("--cases", nargs="*", help="case ids to translate and decide (default: every case in the variant)")
    ap.add_argument("--out", help=f"where mapping.json, rehearsal.json and the rehearsal DB go (default {OUT}/<variant>)")
    ap.add_argument("--facts", default=str(FACTS))
    a = ap.parse_args(argv)
    vdir = make_fresh_variant(a.make_fresh) if a.make_fresh else Path(a.variant_dir)
    header("ops.variant_rehearsal", "Challenge 13: map, translate and investigate on a variant schema", {"variant": str(vdir), "facts": a.facts})
    try:
        rep = rehearse(vdir, a.out, a.cases, a.facts)
    except (OSError, ValueError, duckdb.Error) as exc:
        fail(str(exc))
        return 1
    for cid, v in rep["cases"].items():
        var, can = v["variant"] or {}, v["canonical"] or {}
        (ok if v["same"] else warn)(f"{cid}: variant {var.get('verdict')}/{var.get('status')} {var.get('final_actions')} · canonical {can.get('verdict')}/{can.get('status')} {can.get('final_actions')}")
    # a row difference the variant itself caused (a value its export dropped) is reported above; the rehearsal passes when
    # every case gets the canonical decision
    good = rep["n_same_decision"] == rep["n_cases"]
    summary("variant rehearsal " + ("matches the canonical schema" if good else "differs from the canonical schema"),
            {"columns mapped": rep["n_mapped_columns"], "rows equal": rep["row_comparison"]["all_equal"],
             "same decision": f"{rep['n_same_decision']} of {rep['n_cases']}"}, status="ok" if good else "fail")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())

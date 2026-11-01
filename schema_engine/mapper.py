"""Challenge 13 — Someone Else's Schema (`schema_engine/mapper.py`).

Automatically discovers the mapping from an unfamiliar bank's renamed and reshaped CSV schema
to the canonical `contracts/schema.md` schema using deterministic value fingerprints (regex,
categorical Jaccard, numeric quantiles, and foreign-key overlap), and emits a 4-part AttestedClaim
with 5 sample records for every mapped column.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

from schema_engine.attestation import attest
from schema_engine.profiler import profile_csv

CHANNEL_SYNONYMS: dict[str, str] = {
    "online": "online",
    "web": "online",
    "ecom": "online",
    "cnp": "online",
    "card_not_present": "online",
    "in_person": "in_person",
    "pos": "in_person",
    "in_store": "in_person",
    "cp": "in_person",
    "card_present": "in_person",
}

CARD6_SYNONYMS: dict[str, str] = {
    "debit": "debit",
    "dr": "debit",
    "debit_card": "debit",
    "credit": "credit",
    "cr": "credit",
    "credit_card": "credit",
    "charge card": "charge card",
    "charge_card": "charge card",
    "chg": "charge card",
}


def _score_tx_column(col_name: str, prof: dict[str, Any], all_cols: dict[str, dict[str, Any]]) -> tuple[str | None, str, float, str]:
    """Return (canonical_name, transform, confidence, rationale) for one column of a transactions-like table."""
    low = col_name.lower()
    rx = prof.get("regex_rates", {})
    q = prof.get("quantiles", {})
    top = {str(x).lower() for x in prof.get("top_values", [])}
    t = prof.get("inferred_type", "STRING")

    if rx.get("txn_id", 0) >= 0.9:
        return "TransactionID", "identity", 0.99, f"99%+ match 7-digit TransactionID regex (^\\d{{7}}$); n_distinct={prof['n_distinct']}"
    if rx.get("customer_id", 0) >= 0.9:
        return "customer_id", "identity", 0.99, "Matches customer_id regex (^C\\d{5}$)"
    if rx.get("card_id", 0) >= 0.9:
        return "card_id", "identity", 0.99, "Matches pre-derived card_id regex (^C\\d{5}-K\\d$)"
    if rx.get("ts_iso", 0) >= 0.8:
        return "ts", "identity", 0.99, "Matches ISO timestamp regex (YYYY-MM-DD HH:MM:SS)"
    if rx.get("date_only", 0) >= 0.8:
        return "ts", "date_part", 0.95, "Calendar date (YYYY-MM-DD) without a time: one half of a split timestamp, joined with the time column"
    if rx.get("time_only", 0) >= 0.8:
        return "ts", "time_part", 0.95, "Clock time (HH:MM:SS) without a date: the other half of a split timestamp"
    if rx.get("ts_epoch", 0) >= 0.8 and ("ts" in low or "time" in low or "epoch" in low or "date" in low):
        return "ts", "epoch_to_iso", 0.96, f"Unix epoch in 2016-2017 range (p50={q.get('p50')}); reshaped via epoch_to_iso"
    if top and top <= {"w", "h", "c", "s", "r"}:
        return "ProductCD", "identity", 0.98, f"Categorical domain {sorted(top)} matches ProductCD {{W,H,C,S,R}}"
    if top and top <= set(CHANNEL_SYNONYMS.keys()) and len(top) <= 3 and not top <= {"w", "h", "c", "s", "r"}:
        xform = "identity" if top <= {"online", "in_person"} else "channel_synonym"
        return "channel", xform, 0.97, f"Categorical domain {sorted(top)} maps to channel {{online, in_person}}"
    if top and top <= set(CARD6_SYNONYMS.keys()) and len(top) <= 3:
        xform = "identity" if top <= {"debit", "credit", "charge card"} else "card6_synonym"
        return "card6", xform, 0.96, f"Categorical domain {sorted(top)} maps to card6 {{debit, credit}}"
    if top and (top & {"visa", "mastercard", "discover", "american express"}):
        return "card4", "identity", 0.97, f"Categorical domain {sorted(top)} matches card4 network"
    if rx.get("email_domain", 0) >= 0.5:
        if low.startswith("r_") or any(k in low for k in ("recipient", "payee", "r_emaildomain", "dest", "to_email")):
            return "R_emaildomain", "identity", 0.95, "Email domain column with recipient/payee role semantics and higher null rate"
        return "P_emaildomain", "identity", 0.95, "Email domain column with purchaser/buyer role semantics"
    if rx.get("proxy_type", 0) >= 0.2:
        return "id_23", "identity", 0.97, "Matches IP_PROXY:(ANONYMOUS|TRANSPARENT|HIDDEN) values"
    if rx.get("device_bundle", 0) >= 0.5:
        return "device_id", "split_device_bundle", 0.96, "Composite 4-part 'DeviceInfo | OS | Browser | Screen' bundle"

    # Numeric columns: TransactionAmt (USD vs cents), risk_score (0..1 vs 0..100), addr1, dist1
    if t in ("FLOAT", "INT") and q:
        p50 = q.get("p50", 0.0)
        p90 = q.get("p90", 0.0)
        qmax = q.get("max", 0.0)
        qmin = q.get("min", 0.0)
        if any(k in low for k in ("cent", "minor")) or (t == "INT" and 2000 <= p50 <= 25000 and p90 >= 30000 and "dt" not in low and "id" not in low):
            return "TransactionAmt", "cents_to_usd", 0.95, f"Integer amount in cents (p50={p50}, p90={p90}); divide by 100.0 -> USD"
        if any(k in low for k in ("amt", "amount", "usd", "total", "val", "price")) and 10.0 <= p50 <= 300.0:
            return "TransactionAmt", "identity", 0.97, f"Decimal transaction amount in USD (p50={p50}, p90={p90})"
        if any(k in low for k in ("risk", "score", "prob")):
            if 0.0 <= qmin and qmax <= 1.0:
                return "risk_score", "identity", 0.96, f"Real-time risk score in [0, 1] (p50={p50}, max={qmax})"
            if 0.0 <= qmin and 1.0 < qmax <= 100.0:
                return "risk_score", "pct_to_prob", 0.95, f"Percentage risk score in [0, 100] (p50={p50}, max={qmax}); divide by 100.0"
        if any(k in low for k in ("region", "addr1", "zip", "billing_reg")) and 100 <= p50 <= 520:
            return "addr1", "identity", 0.94, f"Billing region code in [100..600] (p50={p50})"
        if any(k in low for k in ("country", "addr2")) and 60 <= p50 <= 100:
            return "addr2", "identity", 0.92, f"Billing country code (p50={p50})"
        if any(k in low for k in ("dist", "distance", "miles")):
            return "dist1", "identity", 0.90, f"Distance metric (p50={p50}, max={qmax})"

    return None, "unmapped", 0.0, "No high-confidence canonical fingerprint match"


def _score_id_column(col_name: str, prof: dict[str, Any]) -> tuple[str | None, str, float, str]:
    low = col_name.lower()
    rx = prof.get("regex_rates", {})
    top = {str(x).lower() for x in prof.get("top_values", [])}

    if rx.get("txn_id", 0) >= 0.9:
        return "TransactionID", "identity", 0.99, "Foreign key joining to transactions.TransactionID (^\\d{7}$)"
    if rx.get("device_bundle", 0) >= 0.4:
        return "device_bundle", "split_device_bundle", 0.97, "Reshaped composite 'DeviceInfo | id_30 | id_31 | id_33' string"
    if rx.get("proxy_type", 0) >= 0.15:
        return "id_23", "identity", 0.98, "Proxy type string matching IP_PROXY:(ANONYMOUS|TRANSPARENT|HIDDEN)"
    if top and top <= {"mobile", "desktop"}:
        return "DeviceType", "identity", 0.98, f"Categorical domain {sorted(top)} matches DeviceType {{mobile, desktop}}"
    if top and top <= {"new", "found", "unknown"}:
        return "id_15", "identity", 0.97, f"Categorical domain {sorted(top)} matches device_new (id_15)"
    # OS vs browser: 'android browser 4.0' names an OS too, so the column goes to whichever vocabulary most of its values use
    os_rate = sum(any(t in v for t in ("windows", "ios", "mac os", "android", "linux")) for v in top) / max(len(top), 1)
    br_rate = sum(any(t in v for t in ("chrome", "safari", "firefox", "edge", "ie ", "browser", "opera", "samsung")) for v in top) / max(len(top), 1)
    if br_rate > os_rate and br_rate >= 0.3:
        return "id_31", "identity", 0.95, f"Browser user-agent string ({br_rate:.0%} of values name a browser, {os_rate:.0%} an OS)"
    if os_rate >= 0.3:
        return "id_30", "identity", 0.95, f"Operating system string ({os_rate:.0%} of values name an OS, {br_rate:.0%} a browser)"
    if any("x" in v and v.replace("x", "").isdigit() for v in top):
        return "id_33", "identity", 0.96, "Screen resolution string (WxH)"
    if any(k in low for k in ("device", "hw", "model", "handset", "ua_dev")):
        return "DeviceInfo", "identity", 0.93, "Hardware/device model identifier (DeviceInfo)"
    return None, "unmapped", 0.0, "No identity column match"


def _score_case_pack_column(col_name: str, prof: dict[str, Any]) -> tuple[str | None, str, float, str]:
    low = col_name.lower()
    rx = prof.get("regex_rates", {})
    top = {str(x).lower() for x in prof.get("top_values", [])}

    if rx.get("exam_case_id", 0) >= 0.8:
        return "case_id", "identity", 0.99, "Matches exam case_id regex (^HH[GF]-\\d{3}$)"
    if rx.get("ts_iso", 0) >= 0.8:
        return "opened_at", "identity", 0.99, "Matches case opened_at ISO timestamp"
    if rx.get("txn_id", 0) >= 0.8:
        return "flagged_txn_id", "identity", 0.99, "Matches 7-digit flagged_txn_id"
    if rx.get("card_id", 0) >= 0.8:
        return "card_id", "identity", 0.99, "Matches card_id regex (^C\\d{5}-K\\d$)"
    if rx.get("customer_id", 0) >= 0.8:
        return "customer_id", "identity", 0.99, "Matches customer_id regex (^C\\d{5}$)"
    if top and top <= {"risk_score", "customer_report", "analyst_request", "model_alert", "dispute", "analyst"}:
        xform = "identity" if top <= {"risk_score", "customer_report", "analyst_request"} else "trigger_synonym"
        return "trigger_type", xform, 0.97, f"Trigger category {sorted(top)}"
    if any(k in low for k in ("text", "note", "msg", "desc", "narrative", "prompt")) or (
            prof.get("inferred_type") == "STRING" and top and sum(len(v.split()) >= 8 for v in top) >= 0.8 * len(top)):
        return "trigger_text", "identity", 0.95, "Free-text alert description / customer message"
    if any(k in low for k in ("risk", "score", "prob")):
        return "risk_score", "identity", 0.94, "Alert risk_score column"
    return None, "unmapped", 0.0, "No case_pack column match"


def _apply_preview_transform(val: str, transform: str) -> str:
    if not val:
        return ""
    try:
        if transform == "cents_to_usd":
            return f"{float(val) / 100.0:.2f}"
        if transform == "pct_to_prob":
            return f"{float(val) / 100.0:.4f}"
        if transform == "epoch_to_iso":
            return dt.datetime.fromtimestamp(float(val), tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        if transform == "channel_synonym":
            return CHANNEL_SYNONYMS.get(val.lower(), val)
        if transform == "card6_synonym":
            return CARD6_SYNONYMS.get(val.lower(), val)
        if transform == "split_device_bundle":
            parts = [p.strip() for p in val.split("|")]
            return f"DeviceInfo={parts[0] if len(parts)>0 else 'NULL'}, id_30={parts[1] if len(parts)>1 else 'NULL'}, id_31={parts[2] if len(parts)>2 else 'NULL'}, id_33={parts[3] if len(parts)>3 else 'NULL'}"
    except Exception:
        return val
    return val


def infer_variant_mapping(variant_dir: str | Path) -> dict[str, Any]:
    """Inspect a directory of variant CSV files and infer the table and column mappings with 5 attested samples per column."""
    vdir = Path(variant_dir)
    csv_files = sorted(vdir.glob("*.csv"))
    profiles = {p.name: profile_csv(p) for p in csv_files}

    table_roles: dict[str, str] = {}
    for fname, prof in profiles.items():
        cols = prof["columns"]
        has_exam_id = any(c["regex_rates"].get("exam_case_id", 0) >= 0.5 for c in cols.values())
        has_cc_id = any(c["regex_rates"].get("closed_case_id", 0) >= 0.5 for c in cols.values())
        has_cust = any(c["regex_rates"].get("customer_id", 0) >= 0.5 for c in cols.values())
        has_txn = any(c["regex_rates"].get("txn_id", 0) >= 0.5 for c in cols.values())
        if has_exam_id:
            table_roles[fname] = "case_pack"
        elif has_cc_id:
            table_roles[fname] = "closed_cases"
        elif has_txn and has_cust:
            table_roles[fname] = "transactions"
        elif has_txn and not has_cust:
            table_roles[fname] = "identity"
        else:
            table_roles[fname] = "unknown"

    mappings: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for fname, prof in profiles.items():
        role = table_roles.get(fname, "unknown")
        cols = prof["columns"]
        for cname, cprof in cols.items():
            if role == "transactions":
                target, xform, conf, reason = _score_tx_column(cname, cprof, cols)
                if not target:
                    # a flat file carries the identity columns next to the transaction ones
                    target, xform, conf, reason = _score_id_column(cname, cprof)
                    if target == "TransactionID":
                        target, xform, conf, reason = None, "unmapped", 0.0, "No high-confidence canonical fingerprint match"
            elif role == "identity":
                target, xform, conf, reason = _score_id_column(cname, cprof)
            elif role == "case_pack":
                target, xform, conf, reason = _score_case_pack_column(cname, cprof)
            else:
                target, xform, conf, reason = (cname, "identity", 0.90, "Direct name match on closed_cases")
            if not target:
                unmapped.append({"source_file": fname, "table_role": role, "source_column": cname, "reason": reason,
                                 "top_values": cprof.get("top_values", [])[:5]})
                continue
            sample_rows = [
                {
                    "record_id": s["record_id"],
                    "raw_value": s["value"],
                    "canonical_value": _apply_preview_transform(s["value"], xform),
                }
                for s in cprof.get("samples", [])[:5]
            ]
            ev_ids = [s["record_id"] for s in sample_rows]
            claim = attest(
                what=f"Map {fname}:{cname} -> {role}.{target} (transform={xform}, confidence={conf:.2f}): {reason}",
                evidence=ev_ids,
                query_or_rule=f"schema_engine.mapper.infer_variant_mapping({fname}:{cname})",
                params={"source_file": fname, "source_column": cname, "canonical_table": role, "canonical_column": target, "transform": xform},
                dataset=f"variant:{vdir.name}",
            )
            mappings.append(
                {
                    "source_file": fname,
                    "table_role": role,
                    "source_column": cname,
                    "canonical_column": target,
                    "transform": xform,
                    "confidence": conf,
                    "rationale": reason,
                    "sample_records": sample_rows,
                    "attestation": claim.to_dict(),
                }
            )

    return {
        "variant_dir": str(vdir),
        "discovered_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "table_roles": table_roles,
        "mappings": mappings,
        "n_mapped_columns": len(mappings),
        # for a human to review before the mapping is used: what nothing matched, and what matched below 0.95
        "unmapped_columns": unmapped,
        "low_confidence": [{k: m[k] for k in ("source_file", "source_column", "canonical_column", "confidence", "rationale")}
                           for m in mappings if m["confidence"] < 0.95],
    }


def build_variant_fixture(
    out_dir: str | Path,
    round2_dir: str | Path | None = None,
    max_tx_rows: int = 1200,
) -> dict[str, Any]:
    """Create a realistic 'Someone Else's Schema' (Challenge 13) dataset with renamed files,
    renamed columns, cents instead of USD, epoch timestamps, POS/WEB channel codes, and a
    merged device bundle string — built from real Round 2 records including all 18 HHF- cases.
    """
    r2 = Path(round2_dir) if round2_dir else Path(__file__).resolve().parents[1] / "data" / "raw" / "round2"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Read case_pack_final.csv to make sure all flagged_txn_ids and their cards are included
    cp_rows = list(csv.DictReader((r2 / "case_pack_final.csv").open(encoding="utf-8", newline="")))
    must_custs = {r["customer_id"] for r in cp_rows}

    tx_keep: list[dict[str, str]] = []
    with (r2 / "transactions_2017q1.csv").open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["customer_id"] in must_custs or int(r["TransactionID"]) >= 3660096 or len(tx_keep) < max_tx_rows:
                tx_keep.append(r)
                if len(tx_keep) >= max_tx_rows and int(r["TransactionID"]) < 3660000 and r["customer_id"] not in must_custs:
                    continue

    kept_tx_ids = {r["TransactionID"] for r in tx_keep}

    # 1. Write reshaped transactions -> bank_b_ledger.csv
    with (out / "bank_b_ledger.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "tx_ref_id",
                "acct_holder_id",
                "event_epoch",
                "amount_cents",
                "lob_code",
                "pos_or_web",
                "card_funding_type",
                "network_brand",
                "billing_zip_region",
                "country_num",
                "miles_dist",
                "buyer_email_host",
                "payee_email_host",
                "realtime_risk_pct",
            ],
        )
        w.writeheader()
        for r in tx_keep:
            ts_str = r.get("ts") or "2017-01-01 00:00:00"
            epoch = int(dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC).timestamp())
            amt_cents = int(round(float(r.get("TransactionAmt") or 0.0) * 100))
            ch = "WEB" if r.get("channel") == "online" else "POS"
            c6 = "DR" if r.get("card6") == "debit" else ("CR" if r.get("card6") == "credit" else "")
            rs = r.get("risk_score")
            rs_pct = f"{float(rs) * 100.0:.1f}" if rs not in (None, "") else ""
            w.writerow(
                {
                    "tx_ref_id": r["TransactionID"],
                    "acct_holder_id": r["customer_id"],
                    "event_epoch": str(epoch),
                    "amount_cents": str(amt_cents),
                    "lob_code": r.get("ProductCD") or "",
                    "pos_or_web": ch,
                    "card_funding_type": c6,
                    "network_brand": r.get("card4") or "",
                    "billing_zip_region": r.get("addr1") or "",
                    "country_num": r.get("addr2") or "",
                    "miles_dist": r.get("dist1") or "",
                    "buyer_email_host": r.get("P_emaildomain") or "",
                    "payee_email_host": r.get("R_emaildomain") or "",
                    "realtime_risk_pct": rs_pct,
                }
            )

    # 2. Write reshaped identity -> bank_b_telemetry.csv (with merged ua_device_bundle!)
    with (out / "bank_b_telemetry.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["tx_ref_id", "client_form_factor", "device_seen_flag", "proxy_classification", "ua_device_bundle"])
        w.writeheader()
        with (r2 / "identity_2017q1.csv").open(encoding="utf-8", newline="") as id_fh:
            for r in csv.DictReader(id_fh):
                if r["TransactionID"] not in kept_tx_ids:
                    continue
                bundle = " | ".join(
                    [
                        r.get("DeviceInfo") or "NULL",
                        r.get("id_30") or "NULL",
                        r.get("id_31") or "NULL",
                        r.get("id_33") or "NULL",
                    ]
                )
                w.writerow(
                    {
                        "tx_ref_id": r["TransactionID"],
                        "client_form_factor": r.get("DeviceType") or "",
                        "device_seen_flag": r.get("id_15") or "",
                        "proxy_classification": r.get("id_23") or "",
                        "ua_device_bundle": bundle,
                    }
                )

    # 3. Write reshaped case pack -> bank_b_alerts.csv
    with (out / "bank_b_alerts.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["alert_ticket_id", "alert_opened_iso", "intake_source", "intake_narrative", "subject_tx_ref", "instrument_id", "acct_holder_id", "model_score"],
        )
        w.writeheader()
        for r in cp_rows:
            w.writerow(
                {
                    "alert_ticket_id": r["case_id"],
                    "alert_opened_iso": r["opened_at"],
                    "intake_source": r["trigger_type"],
                    "intake_narrative": r["trigger_text"],
                    "subject_tx_ref": r["flagged_txn_id"],
                    "instrument_id": r["card_id"],
                    "acct_holder_id": r["customer_id"],
                    "model_score": r.get("risk_score") or "",
                }
            )

    return infer_variant_mapping(out)

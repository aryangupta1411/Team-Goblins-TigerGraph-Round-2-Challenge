"""Column & table profiling for Schema Drift (Challenge 12) and Variant Mapping (Challenge 13)."""
from __future__ import annotations

import csv
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RX_PATTERNS: dict[str, re.Pattern[str]] = {
    "txn_id": re.compile(r"^\d{7}$"),
    "customer_id": re.compile(r"^C\d{5}$"),
    "card_id": re.compile(r"^C\d{5}-K\d$"),
    "closed_case_id": re.compile(r"^CC-\d{4}$"),
    "exam_case_id": re.compile(r"^HH[GF]-\d{3}$"),
    "ts_iso": re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$"),
    "ts_epoch": re.compile(r"^14[678]\d{7}(\.\d+)?$"),
    "email_domain": re.compile(r"^[a-z0-9.-]+\.(com|net|org|edu|gov|co\.uk|de|fr|es|jp|me)$", re.I),
    "proxy_type": re.compile(r"^IP_PROXY:(ANONYMOUS|TRANSPARENT|HIDDEN)$", re.I),
    "device_bundle": re.compile(r"^.+ \| .+ \| .+ \| .+$"),
    "pipe_list_ids": re.compile(r"^\d{7}(\|\d{7})+$"),
}


@dataclass
class ColumnProfile:
    name: str
    n_rows: int
    n_non_null: int
    null_rate: float
    n_distinct: int
    inferred_type: str
    regex_rates: dict[str, float] = field(default_factory=dict)
    top_values: list[str] = field(default_factory=list)
    quantiles: dict[str, float] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def profile_csv(path: str | Path, max_rows: int = 5000) -> dict[str, Any]:
    """Profile a CSV file up to `max_rows` rows, returning table metadata and per-column profiles."""
    p = Path(path)
    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = list(reader.fieldnames or [])
        rows: list[dict[str, str]] = []
        total_rows = 0
        for row in reader:
            total_rows += 1
            if len(rows) < max_rows:
                rows.append(row)

    col_profiles: dict[str, ColumnProfile] = {}
    n_sample = len(rows)
    for col in cols:
        vals = [str(r.get(col) or "").strip() for r in rows]
        non_null = [v for v in vals if v != "" and v.lower() not in ("null", "none", "nan")]
        n_nn = len(non_null)
        null_rate = round(1.0 - (n_nn / n_sample), 4) if n_sample else 1.0
        distinct = sorted(set(non_null))
        rx_rates: dict[str, float] = {}
        if n_nn > 0:
            for k, rx in RX_PATTERNS.items():
                hits = sum(1 for v in non_null[:500] if rx.match(v))
                rate = round(hits / min(n_nn, 500), 4)
                if rate > 0.05:
                    rx_rates[k] = rate

        num_vals: list[float] = []
        for v in non_null[:1000]:
            try:
                num_vals.append(float(v))
            except ValueError:
                pass
        is_num = n_nn > 0 and (len(num_vals) / min(n_nn, 1000)) >= 0.95
        if rx_rates.get("ts_iso", 0) >= 0.8:
            inferred = "DATETIME"
        elif is_num:
            all_int = all(float(x).is_integer() for x in num_vals[:200])
            inferred = "INT" if all_int else "FLOAT"
        elif set(v.lower() for v in distinct[:10]) <= {"true", "false", "0", "1", "yes", "no"} and len(distinct) <= 4:
            inferred = "BOOL"
        else:
            inferred = "STRING"

        quantiles: dict[str, float] = {}
        if is_num and num_vals:
            s_nums = sorted(num_vals)
            m = len(s_nums)
            quantiles = {
                "min": round(s_nums[0], 4),
                "p10": round(s_nums[int(0.1 * (m - 1))], 4),
                "p50": round(statistics.median(s_nums), 4),
                "p90": round(s_nums[int(0.9 * (m - 1))], 4),
                "max": round(s_nums[-1], 4),
            }

        samples: list[dict[str, Any]] = []
        for idx, r in enumerate(rows):
            v = str(r.get(col) or "").strip()
            if v and v.lower() not in ("null", "none"):
                pk = r.get("TransactionID") or r.get("txn_id") or r.get("tx_id") or r.get("case_id") or r.get("id") or str(idx + 1)
                samples.append({"record_id": str(pk), "value": v[:120]})
                if len(samples) >= 5:
                    break

        col_profiles[col] = ColumnProfile(
            name=col,
            n_rows=total_rows,
            n_non_null=n_nn,
            null_rate=null_rate,
            n_distinct=len(distinct),
            inferred_type=inferred,
            regex_rates=rx_rates,
            top_values=distinct[:15],
            quantiles=quantiles,
            samples=samples,
        )

    return {
        "file": str(p),
        "filename": p.name,
        "total_rows": total_rows,
        "n_columns": len(cols),
        "columns": {k: v.to_dict() for k, v in col_profiles.items()},
    }

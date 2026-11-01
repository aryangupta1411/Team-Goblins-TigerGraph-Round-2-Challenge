"""Latency and cost per case for one run folder (Challenge 9).

    python -m ops.cost_report                       # runs/hhf-final -> engine/out/cost_latency.json
    python -m ops.cost_report --run runs/other --out /tmp/x.json

Latency is answer.json `latency_s` (wall clock per case). Cost is every `kind == "llm"` row of calls.jsonl priced by
agent/pricing.py. p50 is the median, p95 is nearest-rank (the max when n < 20), the same rule as the Runs page.
Reads files only; writes the one JSON file.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import pricing  # noqa: E402

DEFAULT_RUN = ROOT / "runs" / "hhf-final"
DEFAULT_OUT = ROOT / "engine" / "out" / "cost_latency.json"


def percentiles(values) -> dict | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n, mid = len(vals), len(vals) // 2
    return {"n": n, "p50": vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2,
            "p95": vals[max(1, math.ceil(0.95 * n)) - 1], "min": vals[0], "max": vals[-1], "mean": sum(vals) / n}


def _calls(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def report(run_dir: Path) -> dict:
    cases = []
    for d in sorted(p for p in run_dir.iterdir() if p.is_dir() and (p / "answer.json").exists()):
        a = json.loads((d / "answer.json").read_text())
        calls = _calls(d / "calls.jsonl")
        models = sorted({r.get("model", "") for r in calls if r.get("kind") == "llm"})
        cases.append({"case_id": a.get("case_id", d.name), "latency_s": a.get("latency_s"), "tokens": a.get("tokens"),
                      "models": models, **pricing.case_cost(calls)})
    lat = percentiles(c["latency_s"] for c in cases)
    cost = percentiles(c["usd"] for c in cases if c["llm_calls"] and not c["unpriced_calls"])
    r = lambda d, k: {x: round(y, k) if isinstance(y, float) else y for x, y in d.items()} if d else None  # noqa: E731
    return {
        "run": str(run_dir.relative_to(ROOT)) if run_dir.is_relative_to(ROOT) else str(run_dir),
        "n_cases": len(cases),
        "latency_s": r(lat, 1),
        "cost_usd_per_case": r(cost, 5),
        "cost_usd_total": round(sum(c["usd"] for c in cases), 4),
        "price_source": pricing.PRICE_SOURCE,
        "prices_usd_per_1m_tokens": pricing.PRICES,
        "rules": "latency = answer.json latency_s; p95 nearest-rank; cost = calls.jsonl llm rows x per-token price "
                 "(input excludes cached tokens, output includes thinking tokens)",
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    rep = report(args.run.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, indent=2) + "\n")
    lat, cost = rep["latency_s"] or {}, rep["cost_usd_per_case"] or {}
    print(f"{rep['n_cases']} cases · latency p50 {lat.get('p50')} s p95 {lat.get('p95')} s · "
          f"cost/case p50 ${cost.get('p50')} p95 ${cost.get('p95')} · total ${rep['cost_usd_total']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only: compare the live FraudGraph counts with a delta manifest's `expected_after` (docs/DELTA.md, load step 5).

    uv run python -m ops.check_live_counts --manifest data/out_delta/delta_manifest.json

Exits 1 on any mismatch, so a load that looked fine but left the graph short is never reported as done.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))

from ops.console import Col, Table, fail, header, ok  # noqa: E402

VERTICES = {"Transaction", "Card", "Customer", "DeviceProfile", "EmailDomain", "BillingRegion", "ClosedCase"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=ROOT / "data" / "out_delta" / "delta_manifest.json")
    args = ap.parse_args(argv)
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))["expected_after"]
    header("ops.check_live_counts", "live graph counts vs the delta manifest", {"manifest": args.manifest, "types": len(expected)})
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    import tg

    conn = tg.connect()
    t = Table(Col("type", max_width=18), Col("expected", align="right"), Col("live", align="right"), Col("ok", width=3), title="expected_after")
    bad = 0
    for name, want in expected.items():
        got = conn.getVertexCount(name) if name in VERTICES else conn.getEdgeCount(name)
        bad += got != want
        t.add_row(name, f"{want:,}", f"{got:,}", "OK" if got == want else "X")
    t.print()
    if bad:
        fail(f"{bad} type(s) differ from expected_after")
        return 1
    ok(f"all {len(expected)} counts match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

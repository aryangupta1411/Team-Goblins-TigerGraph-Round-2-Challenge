"""Apply a delta's `patch_transaction_ring_hit.tsv` to the live graph (docs/DELTA.md, load step 2).

The file is `id<TAB>true|false` for the base transactions whose ring_hit flips when the month is appended. It is a partial
update by id: only `ring_hit` is written, `updateVertexOnly=True` refuses to create a vertex, and every id is read back.

    uv run python -m ops.apply_ring_hit_patch --patch data/out_delta/patch_transaction_ring_hit.tsv --dry-run
    uv run python -m ops.apply_ring_hit_patch --patch data/out_delta/patch_transaction_ring_hit.tsv
    uv run python -m ops.apply_ring_hit_patch --patch data/out_delta/undo/patch_transaction_ring_hit.tsv   # undo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph"))

from ops.console import fail, header, ok, summary  # noqa: E402


def read_patch(path: Path) -> dict[str, bool]:
    rows: dict[str, bool] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        tid, val = line.split("\t")
        if not tid.isdigit() or val not in ("true", "false"):
            raise ValueError(f"{path}:{n}: expected <txn id>\\t<true|false>, got {line!r}")
        rows[tid] = val == "true"
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--patch", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rows = read_patch(args.patch)
    header("ops.apply_ring_hit_patch", "partial Transaction.ring_hit update by id", {"patch": args.patch, "rows": len(rows), "dry run": args.dry_run})
    if args.dry_run:
        ok(f"{len(rows)} rows parsed; nothing written")
        return 0
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    import tg

    conn = tg.connect()
    have = {str(v["v_id"]) for v in conn.getVerticesById("Transaction", list(rows), select="ring_hit")}
    missing = sorted(set(rows) - have)
    if missing:
        fail(f"{len(missing)} patch ids are not in the graph: {', '.join(missing[:10])}; nothing written")
        return 1
    data = {"vertices": {"Transaction": {tid: {"ring_hit": {"value": val}} for tid, val in rows.items()}}}
    conn.upsertData(data, updateVertexOnly=True)
    back = {str(v["v_id"]): bool(v["attributes"]["ring_hit"]) for v in conn.getVerticesById("Transaction", list(rows), select="ring_hit")}
    wrong = sorted(t for t, v in rows.items() if back.get(t) != v)
    if wrong:
        fail(f"{len(wrong)} ids did not take the new value: {', '.join(wrong[:10])}")
        return 1
    summary("ring_hit patch applied", {"rows": len(rows), "verified": len(back)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

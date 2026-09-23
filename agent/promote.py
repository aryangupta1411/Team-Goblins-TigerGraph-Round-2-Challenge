"""Promote a run's answer files into cases/ (contracts §D): `uv run python -m agent.promote --run-id r1`

Only files that pass the invariants (and the DuckDB validator when available) are
copied; cases/MANIFEST.md records run id, model, prompt hashes and per-case totals.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys

from agent.answer_fallback import check_invariants
from agent.config import SETTINGS
from agent.phase_machine import VALIDATOR_SOURCE, _facts_db, validate_answer
from agent.schemas import Answer


def promote(run_id: str, cases: str = "all", force: bool = False) -> int:
    run_dir = SETTINGS.runs_dir / run_id
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8")) if (run_dir / "run_manifest.json").exists() else {}
    wanted = None if cases in ("all", "") else {c.strip() for c in cases.split(",")}
    SETTINGS.cases_dir.mkdir(parents=True, exist_ok=True)
    rows, bad = [], 0
    for case_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        if wanted and case_dir.name not in wanted:
            continue
        src = case_dir / "answer.json"
        if not src.exists():
            continue
        answer = json.loads(src.read_text(encoding="utf-8"))
        problems = []
        try:
            Answer.model_validate(answer)
        except Exception as e:
            problems.append(f"schema: {e}")
        problems += check_invariants(answer)
        if validate_answer is not None:
            try:
                db = _facts_db()
                if db is not None:
                    problems += validate_answer(answer, db, None) if VALIDATOR_SOURCE == "engine.validator" else validate_answer(answer, db)
            except Exception as e:
                problems.append(f"validator raised: {e}")
        if problems and not force:
            bad += 1
            print(f"[{case_dir.name}] NOT promoted: {problems}", file=sys.stderr)
            continue
        dst = SETTINGS.cases_dir / f"{case_dir.name}.json"
        shutil.copyfile(src, dst)
        c = answer["case"]
        rows.append((case_dir.name, c["verdict"], c["fraud_probability"], c["pattern"], answer["sar"]["file"],
                     answer["tool_calls"], answer["tokens"], answer["latency_s"], "; ".join(problems) if problems else ""))
        print(f"[{case_dir.name}] promoted -> {dst}")
    lines = ["# cases/ manifest", "", f"- run_id: `{run_id}`", f"- model: `{manifest.get('model', '')}` (effort {manifest.get('effort', '')})",
             f"- engine: `{manifest.get('engine', '')}`", f"- git: `{manifest.get('git_sha', '')}`",
             "- prompt sha256: " + ", ".join(f"`{k}`={v[:12]}" for k, v in (manifest.get("prompt_sha256") or {}).items()), "",
             "| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} | {r[8]} |" for r in rows]
    (SETTINGS.cases_dir / "MANIFEST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cases", default="all")
    ap.add_argument("--force", action="store_true", help="promote even with validation problems (never for the frozen run)")
    a = ap.parse_args(argv)
    return promote(a.run_id, a.cases, a.force)


if __name__ == "__main__":
    raise SystemExit(main())

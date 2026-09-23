"""Benchmark runner: `uv run python -m agent.bench --run-id r1 --cases all --model claude-sonnet-5`

- Cases run in `opened_at` order (HHG-017 first … HHG-004 last) so later cases can
  retrieve earlier agent-written ones (PLAN §4.2).
- One MCP session (harness, read + write tools) for the whole run; the LLM only ever
  sees typed read-query wrappers.
- `ensure_awake()` backs off on Savanna 502/503 auto-resume; a keep-alive task runs a
  trivial MCP call every KEEP_ALIVE_S seconds so the workspace does not auto-stop
  mid-benchmark.
- `--resume` skips cases whose runs/<run_id>/<case>/answer.json already exists.
- Writes runs/<run_id>/run_manifest.json (model, effort, prompt hashes, versions, git sha).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent.config import SETTINGS, Settings
from agent.engine_api import ENGINE, CaseContext
from agent.mcp_client import open_session
from agent.phase_machine import PhaseMachine
from agent.runlog import RunLog


def load_case_pack(path: Path) -> list[CaseContext]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            rs = r.get("risk_score", "")
            rows.append(CaseContext(case_id=r["case_id"], trigger_type=r["trigger_type"], trigger_text=r["trigger_text"],
                                    flagged_txn_id=str(r["flagged_txn_id"]), card_id=r["card_id"], customer_id=r["customer_id"],
                                    opened_at=r["opened_at"], risk_score=float(rs) if rs not in ("", None) else None))
    rows.sort(key=lambda c: (c.opened_at, c.case_id))
    return rows


def select_cases(cases: list[CaseContext], spec: str) -> list[CaseContext]:
    if spec in ("all", "*", ""):
        return cases
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    return [c for c in cases if c.case_id in wanted]


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def _versions() -> dict:
    import importlib.metadata as m
    out = {}
    for pkg in ("anthropic", "claude-agent-sdk", "mcp", "tigergraph-mcp", "pyTigerGraph", "sentence-transformers", "torch", "voyageai", "pydantic"):
        try:
            out[pkg] = m.version(pkg)
        except Exception:
            out[pkg] = ""
    return out


def _resolve_backend(settings: Settings) -> str:
    from agent import llm
    return llm.resolve_backend(settings)


def write_manifest(run_dir: Path, settings: Settings, model: str, case_ids: list[str]) -> None:
    prompts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(settings.prompts_dir.glob("*.md"))}
    manifest = {"run_id": run_dir.name, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "sar_model": settings.sar_model,
                "effort": settings.effort, "run_mode": settings.run_mode, "llm_backend": _resolve_backend(settings), "engine": ENGINE.source, "max_tool_calls": settings.max_tool_calls,
                "prompt_sha256": prompts, "versions": _versions(), "git_sha": _git_sha(), "cases": case_ids,
                "embed_model": settings.embed_model, "embed_dim": settings.embed_dim}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def ensure_awake(settings: Settings = SETTINGS, max_wait_s: int = 240) -> None:
    """Backoff 1,2,4,…60 s up to max_wait_s on 502/503/connection errors (Savanna auto-resume)."""
    if settings.mock:
        return
    from pyTigerGraph import TigerGraphConnection
    conn = TigerGraphConnection(host=settings.tg_host, graphname=settings.tg_graphname, gsqlSecret=settings.tg_secret,
                                tgCloud=settings.tg_host.startswith("https://"), restppPort="9000", gsPort="14240")
    delay, waited = 1, 0
    while True:
        try:
            conn.echo()
            return
        except Exception as e:
            if waited >= max_wait_s:
                raise RuntimeError(f"workspace not awake after {max_wait_s}s: {e}") from e
            print(f"[ensure_awake] {type(e).__name__}: retry in {delay}s", file=sys.stderr)
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60)


async def keep_alive(session, every_s: int, stop: asyncio.Event) -> None:
    """M25: the benchmark's keep-alive is `tigergraph__get_vertex_count`, not an installed query, exactly
    because the MCP parameter encoding differs from pyTigerGraph's. `ops/keep_alive.py` pings through
    pyTigerGraph and may pass `KEEP_ALIVE_PARAMS={"cu": "C13487", ...}` with the VERTEX parameter as a bare
    id; over MCP the same parameter has to be written `{"id": "C13487"}` (contracts §A). Do not copy the
    `.env` value into an MCP call."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every_s)
            break
        except TimeoutError:
            pass
        try:
            await session.call_tool("tigergraph__get_vertex_count", {"vertex_type": "FraudPattern"})
        except Exception as e:
            print(f"[keep_alive] {e}", file=sys.stderr)


def make_client(settings: Settings):
    """The LLM backend for this run (`agent/llm.py`): cli | api | mock, from LLM_BACKEND.

    `cli` (the default) needs no API key — claude-agent-sdk drives the bundled Claude Code CLI on
    the user's subscription. `--mock` / RUN_MODE=mock still selects FakeAnthropic unless
    LLM_BACKEND says otherwise, which is what lets a real model run against DuckDB-backed facts
    (`LLM_BACKEND=cli RUN_MODE=mock`, i.e. mock *graph*, live *model*).
    """
    from agent import llm
    return llm.make_backend(settings)


async def run(args: argparse.Namespace) -> int:
    settings = SETTINGS
    if args.model:
        settings = Settings(**{**settings.__dict__, "model": args.model, "sar_model": args.sar_model or args.model})
    if args.mock:
        os.environ["RUN_MODE"] = "mock"
        settings = Settings(**{**settings.__dict__, "run_mode": "mock"})
    if args.llm_backend:
        os.environ["LLM_BACKEND"] = args.llm_backend
        settings = Settings(**{**settings.__dict__, "llm_backend": args.llm_backend})
    cases = select_cases(load_case_pack(Path(args.case_pack) if args.case_pack else settings.case_pack_csv), args.cases)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    run_dir = settings.runs_dir / args.run_id
    write_manifest(run_dir, settings, settings.model, [c.case_id for c in cases])
    ensure_awake(settings)
    client = make_client(settings)
    failures = 0
    async with open_session(read_only=False, settings=settings) as (session, _tools):
        stop = asyncio.Event()
        ka = asyncio.create_task(keep_alive(session, settings.keep_alive_s, stop)) if not settings.mock else None
        try:
            pm = PhaseMachine(client, session, args.run_id, settings)
            for ctx in cases:
                out = run_dir / ctx.case_id / "answer.json"
                if args.resume and out.exists():
                    print(f"[{ctx.case_id}] resume: exists, skipping")
                    continue
                if settings.mock and hasattr(session, "use_case"):
                    session.use_case(ctx.case_id)
                log = RunLog(args.run_id, ctx.case_id, run_dir / ctx.case_id)
                t0 = time.time()
                try:
                    answer = await pm.run_case(ctx, log)
                    c = answer["case"]
                    print(f"[{ctx.case_id}] {c['verdict']} p={c['fraud_probability']} pattern={c['pattern']} "
                          f"final={[a['action'] for a in answer['next_best_actions']['final']]} sar={answer['sar']['file']} "
                          f"tools={answer['tool_calls']} tokens={answer['tokens']} {time.time() - t0:.1f}s "
                          f"problems={len(answer.get('_validation', []))}")
                except Exception as e:
                    failures += 1
                    log.note("case failed", error=repr(e)[:800])
                    print(f"[{ctx.case_id}] FAILED: {e!r}", file=sys.stderr)
                    if args.fail_fast:
                        raise
        finally:
            stop.set()
            if ka:
                await ka
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HHGOA benchmark runner")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cases", default="all", help="all | HHG-014 | HHG-014,HHG-006")
    ap.add_argument("--model", default=None)
    ap.add_argument("--sar-model", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mock", action="store_true", help="RUN_MODE=mock: FakeSession (DuckDB facts) + FakeAnthropic unless --llm-backend says otherwise")
    ap.add_argument("--llm-backend", default=None, choices=["cli", "api", "mock"],
                    help="cli (claude-agent-sdk, no API key, default) | api (ANTHROPIC_API_KEY) | mock (FakeAnthropic)")
    ap.add_argument("--case-pack", default=None)
    ap.add_argument("--fail-fast", action="store_true")
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

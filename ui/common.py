"""Shared helpers for the Streamlit UI.

ui/app.py is a router: st.navigation with a top bar over the page files in ui/pages/, one context bar rendered above every
page (source picker, case picker on the Case file and Graph pages, provenance and mode chips, identity), and the resolved
Ctx kept in st.session_state. Every page starts with ``ctx = common.page_setup("<key>")``.

A source is what a page shows, and no page mixes two:
  - an agent run: runs/<run_id>/run_manifest.json and runs/<run_id>/<case_id>/{answer.json, phases.jsonl, calls.jsonl, sar.md}
  - 'submitted' (cases/HHF-*.json) and 'submitted-r1' (cases/HHG-*.json): the answer files, without a trace
The approvals inbox (ui/approvals.sqlite) is read here only through approvals_db.connect_ro, so rendering never creates it.
The graph is read through pyTigerGraph installed queries in live mode only (graph_connection / run_installed_safe).

Text safety: md() escapes a data string for st.markdown, st.caption, labels and help; untrusted_text() shows hostile input
verbatim in st.code. Helpers that take plain text (badge, chip, page_header, measured, proof_chip, page_link,
dependency_banner, untrusted_text) escape it themselves, so pass them raw strings. Headline numbers go through measured().

phases.jsonl rows: {"kind": "phase", "phase": "P3", "name": "scorecard", "at", "t", "payload"} plus kind "note" / "llm" rows.
calls.jsonl rows:  {"seq", "ts", "t", "kind": "tool"|"llm", "phase", "name", "params", "latency_s", "ok", "error", "caller"}
                   and for kind=llm {"model", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}.
Parsed files are cached on (path, mtime_ns, size) and shared by every session: treat what the readers return as read-only.
"""
from __future__ import annotations

import csv
import dataclasses
import functools
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from ui import approvals_db, design

# --------------------------------------------------------------------------- #
# Paths, environment and mode                                                 #
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "ui"


@functools.lru_cache(maxsize=1)
def _load_dotenv() -> bool:
    """Read ROOT/.env once per process; variables already in the environment win."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # the UI image ships python-dotenv; a bare interpreter may not
        return False
    return bool(load_dotenv(ROOT / ".env", override=False))


def _env(name: str, default: str = "") -> str:
    _load_dotenv()
    return os.getenv(name, default)


def mode_status() -> tuple[str, str]:
    """('live', 'TG_HOST set') or ('mock', why): the UI reads the live graph only when RUN_MODE=live and TG_HOST is set."""
    mode = _env("RUN_MODE", "mock").strip().lower() or "mock"
    if mode != "live":
        return "mock", f"RUN_MODE={mode}"
    if not _env("TG_HOST").strip():
        return "mock", "RUN_MODE=live but TG_HOST is unset"
    return "live", "TG_HOST set"


def run_mode() -> str:
    """'live' only when RUN_MODE=live and TG_HOST is set; otherwise 'mock'."""
    return mode_status()[0]


def read_only() -> bool:
    """DEPLOY_READONLY=1 (the Dockerfile default): a public deployment never spawns the agent and never
    writes to the graph, whatever RUN_MODE says. Graph reads and local approval decisions still work."""
    return _env("DEPLOY_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


def runs_need_approver() -> bool:
    """RUNS_NEED_APPROVER=1 (the Dockerfile default): on a deployment that runs the agent, only a signed-in approver
    (approvals_db.authenticate) may start a live run, so a public URL cannot spend tokens or write to the graph."""
    return _env("RUNS_NEED_APPROVER", "").strip().lower() in ("1", "true", "yes", "on")


def runs_root() -> Path:
    p = Path(_env("RUNS_DIR", str(ROOT / "runs")))
    return p if p.is_absolute() else ROOT / p


def cases_dir() -> Path:
    """cases/ (CASES_DIR overrides it, for tests and copies of the tree)."""
    p = Path(_env("CASES_DIR", str(ROOT / "cases")))
    return p if p.is_absolute() else ROOT / p


def approvals_db_path() -> Path:
    _load_dotenv()  # approvals_db reads APPROVALS_DB with os.getenv
    return approvals_db.default_path()


def rel(path: Path | str) -> str:
    """A path relative to the repo root for display (absolute when outside it)."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(p)


def as_of(path: Path | str) -> str:
    """A file's modification time as 'YYYY-MM-DD HH:MM:SS' (local), or 'missing'."""
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    except OSError:
        return "missing"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _st():
    import streamlit as st  # imported on use: agent code imports this module and must not pay for streamlit

    return st


# --------------------------------------------------------------------------- #
# File readers, cached on (path, mtime_ns, size)                              #
# --------------------------------------------------------------------------- #
TRACE_FILES = ("answer.json", "phases.jsonl", "calls.jsonl")
RUNNING_WINDOW_S = 120  # a trace written to within this many seconds counts as a run in progress

_FILE_CACHE: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}


def _sig(path: Path) -> tuple[int, int] | None:
    try:
        s = path.stat()
    except OSError:
        return None
    return s.st_mtime_ns, s.st_size


def _cached(path: Path, kind: str, parse):
    """parse(path), re-run only when the file's mtime or size changed; None when the file does not exist."""
    sig = _sig(path)
    if sig is None:
        return None
    key = (kind, str(path))
    hit = _FILE_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    value = parse(path)
    _FILE_CACHE[key] = (sig, value)
    return value


def _parse_json(path: Path) -> tuple[Any, str]:
    for attempt in (1, 2):
        try:
            return json.loads(path.read_text(encoding="utf-8")), ""
        except json.JSONDecodeError as exc:
            # files are rewritten in place, so a read can race a write: try once more if the file is brand new
            try:
                fresh = time.time() - path.stat().st_mtime < 2
            except OSError:
                fresh = False
            if attempt == 1 and fresh:
                time.sleep(0.05)
                continue
            return None, f"{path.name}: {exc}"
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"{path.name}: {exc}"
    return None, f"{path.name}: unreadable"


def _read_json(path: Path) -> tuple[Any, str]:
    """(data, error) for a JSON file; (None, '') when it does not exist."""
    hit = _cached(path, "json", _parse_json)
    return (None, "") if hit is None else hit


def _parse_jsonl(path: Path) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1  # a torn last row while a run writes, or a damaged file
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    skipped += 1
    except OSError:
        pass
    return rows, skipped


def _read_jsonl_counted(path: Path) -> tuple[list[dict], int]:
    """(rows, unparseable line count) of a JSONL file; ([], 0) when it does not exist."""
    hit = _cached(path, "jsonl", _parse_jsonl)
    return ([], 0) if hit is None else hit


def _read_jsonl(path: Path) -> list[dict]:
    return _read_jsonl_counted(path)[0]


def _read_text(path: Path) -> str:
    def parse(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    return _cached(path, "text", parse) or ""


def _memo(ttl: float):
    """Process-wide cache of a function of hashable arguments for ttl seconds (no streamlit needed)."""

    def deco(fn):
        store: dict = {}

        @functools.wraps(fn)
        def wrapper(*args):
            now = time.monotonic()
            hit = store.get(args)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
            value = fn(*args)
            store[args] = (now, value)
            while len(store) > 32:
                store.pop(next(iter(store)))
            return value

        wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapper

    return deco


_ST_CACHED: dict[str, Any] = {}


def _st_cache(fn, ttl: float):
    """fn under st.cache_data(ttl), built on first use so that importing this module never imports streamlit."""
    wrapped = _ST_CACHED.get(fn.__name__)
    if wrapped is None:
        wrapped = _ST_CACHED[fn.__name__] = _st().cache_data(ttl=ttl, max_entries=32, show_spinner=False)(fn)
    return wrapped


# --------------------------------------------------------------------------- #
# Case pack                                                                   #
# --------------------------------------------------------------------------- #
CASE_PACK_COLUMNS = ["case_id", "opened_at", "trigger_type", "flagged_txn_id", "card_id", "customer_id", "risk_score", "trigger_text"]
ROUND1_CSV = ROOT / "data" / "raw" / "case_pack.csv"
ROUND2_CSV = ROOT / "data" / "raw" / "round2" / "case_pack_final.csv"
PACK_NOTICE_IDS_ONLY = "trigger text unavailable on this deployment"

# The Round 1 pack (HHG-001..020), verbatim from the dataset README ("The 20 Cases"), so the
# Round 1 sources render even before data/raw/case_pack.csv exists on a fresh clone.
_CASE_PACK_FALLBACK_CSV = """case_id,opened_at,trigger_type,flagged_txn_id,card_id,customer_id,risk_score,trigger_text
HHG-001,2016-12-05 01:55:28,risk_score,3514030,C12382-K1,C12382,0.61,"Real-time model scored transaction 3514030 ($77.07, in billing region 444.0) at 0.61. Review and decide."
HHG-002,2016-11-22 23:27:07,risk_score,3478782,C11891-K1,C11891,0.79,"Real-time model scored transaction 3478782 ($292.36, online) at 0.79. Review and decide."
HHG-003,2016-12-10 15:01:21,customer_report,3530164,C08623-K2,C08623,,"Customer C08623 message: 'I never made this $49.00 purchase. Please check my card.' Refers to 3530164."
HHG-004,2016-12-29 07:53:54,customer_report,3583227,C08106-K1,C08106,,"Customer C08106 message: 'I never made this $128.33 purchase. Please check my card.' Refers to 3583227."
HHG-005,2016-12-08 03:38:37,risk_score,3523199,C02923-K1,C02923,0.54,"Real-time model scored transaction 3523199 ($100.07, online) at 0.54. Review and decide."
HHG-006,2016-11-22 02:30:00,customer_report,3476682,C07297-K1,C07297,,"Customer C07297 message: 'I never made this $482.12 purchase. Please check my card.' Refers to 3476682."
HHG-007,2016-12-05 03:46:14,risk_score,3514948,C09933-K2,C09933,0.87,"Real-time model scored transaction 3514948 ($111.92, in billing region 264.0) at 0.87. Review and decide."
HHG-008,2016-12-20 03:08:56,customer_report,3558054,C13171-K2,C13171,,"Customer C13171 message: 'I never made this $55.68 purchase. Please check my card.' Refers to 3558054."
HHG-009,2016-12-28 17:10:53,customer_report,3581141,C08299-K1,C08299,,"Customer C08299 message: 'I never made this $30.02 purchase. Please check my card.' Refers to 3581141."
HHG-010,2016-12-02 18:18:27,risk_score,3506725,C10434-K1,C10434,0.90,"Real-time model scored transaction 3506725 ($1,000.03, online) at 0.90. Review and decide."
HHG-011,2016-12-29 06:27:44,customer_report,3583368,C11923-K2,C11923,,"Customer C11923 message: 'I never made this $131.30 purchase. Please check my card.' Refers to 3583368."
HHG-012,2016-12-18 05:00:31,risk_score,3553342,C05876-K2,C05876,0.55,"Real-time model scored transaction 3553342 ($30.91, in billing region 494.0) at 0.55. Review and decide."
HHG-013,2016-12-09 05:39:29,risk_score,3526826,C07671-K2,C07671,0.76,"Real-time model scored transaction 3526826 ($35.66, online) at 0.76. Review and decide."
HHG-014,2016-11-22 20:11:00,analyst_request,3478561,C13487-K1,C13487,,"Analyst request: several cards this month show purchases from the same unusual device profile. Review transaction 3478561 on card C13487-K1 and look for related activity."
HHG-015,2016-11-17 19:03:36,risk_score,3464869,C03042-K1,C03042,0.77,"Real-time model scored transaction 3464869 ($599.94, online) at 0.77. Review and decide."
HHG-016,2016-12-12 01:39:08,customer_report,3534820,C09988-K1,C09988,,"Customer C09988 message: 'I never made this $59.67 purchase. Please check my card.' Refers to 3534820."
HHG-017,2016-11-12 00:46:24,risk_score,3450629,C04570-K1,C04570,0.57,"Real-time model scored transaction 3450629 ($100.09, online) at 0.57. Review and decide."
HHG-018,2016-11-27 14:41:26,customer_report,3491361,C02354-K2,C02354,,"Customer C02354 message: 'I never made this $39.08 purchase. Please check my card.' Refers to 3491361."
HHG-019,2016-12-01 22:28:53,risk_score,3503878,C07987-K2,C07987,0.90,"Real-time model scored transaction 3503878 ($99.92, online) at 0.90. Review and decide."
HHG-020,2016-12-03 12:04:26,risk_score,3509359,C12265-K2,C12265,0.52,"Real-time model scored transaction 3509359 ($125.08, online) at 0.52. Review and decide."
"""


def _pack_row(src: dict) -> dict[str, str]:
    """A case-pack row with every CASE_PACK_COLUMNS key as text, formatted like the CSV (risk_score to 2 dp)."""
    out: dict[str, str] = {}
    for k in CASE_PACK_COLUMNS:
        v = src.get(k)
        if v is None:
            out[k] = ""
        elif hasattr(v, "strftime"):
            out[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, float):
            out[k] = f"{v:.2f}"
        else:
            out[k] = str(v)
    return out


def _pack_order(row: dict) -> tuple[str, str]:
    return row.get("opened_at") or "~", row.get("case_id") or ""


def _pack_from_csv(path: Path) -> list[dict[str, str]]:
    def parse(p: Path) -> list[dict[str, str]]:
        with open(p, newline="", encoding="utf-8") as f:
            return [_pack_row(r) for r in csv.DictReader(f)]

    return sorted(_cached(path, "csv", parse) or [], key=_pack_order)


def _p0_ctx(phases: Path) -> dict | None:
    """The intake context the agent recorded at P0: case_id, opened_at, trigger_type, trigger_text, ids, risk_score."""

    def parse(p: Path) -> dict | None:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 200:
                        break
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict) or not _is_phase_row(row):
                        continue
                    if row.get("phase") != "P0":
                        break  # P0 comes first; a later phase means it was never recorded
                    ctx = (row.get("payload") or {}).get("ctx")
                    return ctx if isinstance(ctx, dict) and ctx.get("case_id") else None
        except OSError:
            return None
        return None

    return _cached(phases, "p0", parse)


def _pack_from_traces(prefix: str = "HHF-") -> tuple[list[dict[str, str]], list[str]]:
    """Pack rows rebuilt from the P0 ctx in run traces, newest run first, plus the runs they came from."""
    found: dict[str, dict[str, str]] = {}
    origin: list[str] = []
    for d in _run_dirs():
        took = False
        for c in _case_dirs(d):
            if not c.name.startswith(prefix) or c.name in found:
                continue
            ctx = _p0_ctx(c / "phases.jsonl")
            if ctx and ctx.get("case_id") == c.name:
                found[c.name] = _pack_row(ctx)
                took = True
        if took:
            origin.append(d.name)
    return sorted(found.values(), key=_pack_order), origin


def _facts_db_path() -> Path:
    p = Path(_env("ENGINE_FACTS_DB", str(ROOT / "data" / "hhgoa_engine.duckdb")))
    return p if p.is_absolute() else ROOT / p


def _pack_from_duckdb() -> list[dict[str, str]]:
    """cp_round2 from the engine facts DB. Raises ImportError when duckdb is not installed (the UI image)."""
    import duckdb

    path = _facts_db_path()
    if not path.exists():
        return []
    cols = ["case_id", "opened_at", "trigger_type", "trigger_text", "flagged_txn_id", "card_id", "customer_id", "risk_score"]
    con = duckdb.connect(str(path), read_only=True)
    try:
        return sorted((_pack_row(dict(zip(cols, r))) for r in con.execute(f"SELECT {', '.join(cols)} FROM cp_round2").fetchall()), key=_pack_order)
    finally:
        con.close()


def _pack_ids_only() -> list[dict[str, str]]:
    return [{**dict.fromkeys(CASE_PACK_COLUMNS, ""), "case_id": p.stem} for p in sorted(cases_dir().glob("HHF-*.json"))]


def _pack_signature() -> tuple:
    csv_sig = _sig(ROUND2_CSV)
    if csv_sig is not None:
        return (str(ROUND2_CSV), csv_sig)
    runs = []
    for d in _run_dirs():
        runs.append((d.name, tuple((c.name, _sig(c / "phases.jsonl")) for c in _case_dirs(d) if c.name.startswith("HHF-"))))
    return (str(ROUND2_CSV), None, str(runs_root()), tuple(runs), str(_facts_db_path()), _sig(_facts_db_path()),
            tuple(p.name for p in sorted(cases_dir().glob("HHF-*.json"))))


@_memo(60)
def _final_round_pack(sig: tuple) -> tuple[tuple[dict[str, str], ...], str | None]:
    problems: list[str] = []
    if ROUND2_CSV.exists():
        try:
            rows = _pack_from_csv(ROUND2_CSV)
            if rows:
                return tuple(rows), None
            problems.append(f"{rel(ROUND2_CSV)} has no rows")
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            problems.append(f"{rel(ROUND2_CSV)} unreadable: {exc}")
    rows, origin = _pack_from_traces()
    if rows:
        return tuple(rows), f"Final Round case pack rebuilt from the P0 intake recorded in runs/{', runs/'.join(origin)} ({rel(ROUND2_CSV)} is not on this deployment)"
    try:
        rows = _pack_from_duckdb()
        if rows:
            return tuple(rows), f"Final Round case pack read from table cp_round2 in {rel(_facts_db_path())} ({rel(ROUND2_CSV)} is not on this deployment)"
    except ImportError as exc:
        problems.append(f"duckdb not installed ({exc})")
    except Exception as exc:  # noqa: BLE001 - every duckdb.Error (locked file, missing table) is a reason, never a crash
        problems.append(f"facts DB: {type(exc).__name__}: {exc}")
    ids = _pack_ids_only()
    if ids:
        return tuple(ids), PACK_NOTICE_IDS_ONLY
    return (), "Final Round case pack unavailable: no case pack CSV, no recorded intake under runs/, no facts DB" + (f" ({'; '.join(problems)})" if problems else "")


def final_round_pack() -> tuple[list[dict[str, str]], str | None]:
    """The 18 Final Round rows (CASE_PACK_COLUMNS) in opened_at order, plus a notice when they did not come from the CSV.

    Order: data/raw/round2/case_pack_final.csv, then the P0 'ctx' recorded in run traces (newest run first), then DuckDB
    cp_round2, then the ids of cases/HHF-*.json with empty fields. Never raises; cached for 60 s on a file signature."""
    try:
        rows, notice = _final_round_pack(_pack_signature())
    except Exception as exc:  # noqa: BLE001 - the pack feeds every page; a reason beats a traceback
        return [], f"Final Round case pack unavailable: {type(exc).__name__}: {exc}"
    return [dict(r) for r in rows], notice


def dq_covered(case_id: str) -> bool:
    """The data-quality scan builds its taint matrix for the Final Round cases only (schema_engine/data_quality.py), so a
    missing entry means 'no flag' only for those ids; for any other case it means 'not covered'."""
    return case_id in {r.get("case_id") for r in final_round_pack()[0]}


def round2_pack_rows() -> list[dict[str, str]]:
    """The 18 Final Round rows for agent/bench.py, agent/validator.py and agent/security.py: the CSV, else the engine facts
    DB (cp_round2), else the P0 intake recorded in run traces. Raises RuntimeError when none of them is available."""
    if ROUND2_CSV.exists():
        return [dict(r) for r in _pack_from_csv(ROUND2_CSV)]
    problems: list[str] = []
    try:
        rows = _pack_from_duckdb()
        if rows:
            return rows
        problems.append(f"no cp_round2 rows in {rel(_facts_db_path())}")
    except ImportError as exc:
        problems.append(f"duckdb not installed ({exc})")
    except Exception as exc:  # noqa: BLE001 - every duckdb.Error
        problems.append(f"facts DB: {type(exc).__name__}: {exc}")
    rows, _ = _pack_from_traces()
    if rows:
        return rows
    raise RuntimeError(f"Final Round case pack unavailable: {rel(ROUND2_CSV)} missing; " + "; ".join(problems + ["no P0 intake recorded under runs/"]))


def load_case_pack(pack: str = "round1") -> list[dict[str, str]]:
    """Rows of a case pack in opened_at order.

    `pack="round1"` (the default, for Round 1 callers and tests) loads the 20 HHG cases; `pack="round2"` the 18 HHF Final
    Round cases through final_round_pack(); `pack="all"` both."""
    if pack == "round2":
        return final_round_pack()[0]
    if pack == "all":
        return load_case_pack("round1") + load_case_pack("round2")
    if ROUND1_CSV.exists():
        return [dict(r) for r in _pack_from_csv(ROUND1_CSV)]
    return sorted((_pack_row(r) for r in csv.DictReader(io.StringIO(_CASE_PACK_FALLBACK_CSV))), key=_pack_order)


def pack_for_source(source: Source | None) -> list[dict[str, str]]:
    """The pack a source belongs to: Final Round for 'final' (and when unknown), Round 1 for 'round1', both for 'mixed'."""
    rnd = source.round if source else "final"
    if rnd == "round1":
        return load_case_pack("round1")
    final = final_round_pack()[0]
    return final + load_case_pack("round1") if rnd == "mixed" else final


def load_case_pack_for_run(run_id: str | None) -> list[dict[str, str]]:
    """Kept for old callers: the pack of the source `run_id`."""
    return pack_for_source(get_source(run_id) if run_id else None)


@_memo(30)
def _case_index(*key: Any) -> dict[str, dict[str, str]]:
    return {r["case_id"]: r for r in load_case_pack("round1") + final_round_pack()[0]}


def _pack_files_key() -> tuple:
    """A cheap key for what the packs are read from (the trace fallback is covered by the 30 s memo)."""
    return _sig(ROUND1_CSV), _sig(ROUND2_CSV), str(runs_root()), str(cases_dir())


def case_meta(case_id: str) -> dict[str, str]:
    """The pack row of a case (either round) from a cached index; {'case_id': id} when it is in neither pack."""
    row = _case_index(*_pack_files_key()).get(case_id)
    return dict(row) if row else {"case_id": case_id}


def _case_order(case_id: str) -> tuple[str, str]:
    return case_meta(case_id).get("opened_at") or "~", case_id


# --------------------------------------------------------------------------- #
# Sources: agent runs and the submitted answers                               #
# --------------------------------------------------------------------------- #
SUBMITTED = {"submitted": ("HHF-", "final"), "submitted-r1": ("HHG-", "round1")}
ROUND_LABELS = {"final": "Final Round", "round1": "Round 1", "mixed": "Mixed rounds"}
LOCK_FILE = ".bench.lock"


@dataclass
class Source:
    id: str
    kind: str                     # 'run' | 'submitted'
    round: str                    # 'final' | 'round1' | 'mixed'
    mode: str                     # 'live' | 'mock' | 'promoted' | 'engine'
    dir: Path
    manifest: dict = field(default_factory=dict)
    manifest_error: str = ""
    cases: list[str] = field(default_factory=list)          # ids with a folder (run) or a file (submitted), pack order
    n_answered: int = 0
    n_running: int = 0
    n_failed: int = 0
    n_unreadable: int = 0
    first_started: str = ""
    started: str = ""
    finished: str = ""                                       # max case_runs[*].finished
    llm_backend: str = ""
    manifest_model: str = ""
    case_models: Counter = field(default_factory=Counter)    # case_runs[*].model
    model_note: str = ""                                     # mismatch between the manifest and calls.jsonl
    git_sha: str = ""
    git_dirty: bool | None = None
    max_tool_calls: int | None = None
    invocations: list[dict] = field(default_factory=list)
    n_total: int = 0                                         # cases selected in the manifest (or folders / files)
    states: dict[str, str] = field(default_factory=dict)     # case id -> case state
    models_seen: Counter = field(default_factory=Counter)    # 'model' over kind=='llm' rows of calls.jsonl
    promoted_from: dict[str, list[str]] = field(default_factory=dict)  # submitted only: case id -> identical run answers
    last_write: float | None = None                          # epoch seconds of the newest trace write
    skipped_lines: int = 0

    @property
    def complete(self) -> bool:
        return self.n_total > 0 and self.n_answered >= self.n_total

    @property
    def label(self) -> str:
        return _label_for(self)


def _is_case_dir(p: Path) -> bool:
    return p.name[:1] not in "._" and p.is_dir() and any((p / f).exists() for f in TRACE_FILES)


def _case_dirs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if _is_case_dir(p))
    except OSError:
        return []


def _is_agent_run(d: Path) -> bool:
    """runs/<name>/ with a manifest or a case folder. Dot and underscore folders (runs/_schema_ai), the names of the virtual
    sources and maintenance folders holding only stale_writebacks.json are never runs."""
    if d.name[:1] in "._" or d.name in SUBMITTED or not d.is_dir():
        return False
    return (d / "run_manifest.json").exists() or bool(_case_dirs(d))


def _ts(value: Any) -> str:
    """A manifest timestamp normalised to 'YYYY-MM-DD HH:MM:SS' for display and ordering."""
    return str(value or "").replace("T", " ")[:19]


def _run_started(d: Path) -> str:
    man, _ = _read_json(d / "run_manifest.json")
    if isinstance(man, dict):
        for k in ("first_started", "started", "created_at"):
            if man.get(k):
                return _ts(man[k])
    if (d / "run_manifest.json").exists():
        return ""  # a manifest without a start time sorts after dated runs
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.stat().st_mtime))
    except OSError:
        return ""


def _run_dirs() -> list[Path]:
    """Agent run folders, newest first by manifest first_started / started (folder mtime only without a manifest)."""
    root = runs_root()
    try:
        dirs = [p for p in root.iterdir() if _is_agent_run(p)]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: (_run_started(p), p.name), reverse=True)


def list_runs() -> list[str]:
    """Agent run ids, newest first. Maintenance folders (clear-writebacks-*), dot and underscore folders are left out."""
    return [p.name for p in _run_dirs()]


def run_manifest(run_id: str) -> dict:
    """runs/<run_id>/run_manifest.json, or {} (a read error is reported by run_provenance()['errors'])."""
    if not _safe_id(run_id):
        return {}
    data, _ = _read_json(runs_root() / run_id / "run_manifest.json")
    return data if isinstance(data, dict) else {}


def list_maintenance() -> list[dict]:
    """Every runs/*/stale_writebacks.json (the ops.clear_case_writebacks ledger), newest first."""
    out = []
    root = runs_root()
    try:
        files = sorted(root.glob("*/stale_writebacks.json"))
    except OSError:
        files = []
    for p in files:
        data, err = _read_json(p)
        data = data if isinstance(data, dict) else {}
        remaining = data.get("remaining")
        out.append({
            "folder": p.parent.name, "path": rel(p), "generated_at": _ts(data.get("generated_at")), "applied": bool(data.get("applied")),
            "counts": data.get("counts") or {}, "deleted": data.get("deleted"),
            "remaining": len(remaining) if isinstance(remaining, list) else remaining, "cases": len(data.get("cases") or []), "error": err,
        })
    return sorted(out, key=lambda r: r["generated_at"], reverse=True)


def _safe_id(value: Any) -> bool:
    s = str(value or "")
    return bool(s) and "/" not in s and "\\" not in s and s[:1] not in "._"


def _is_phase_row(row: dict) -> bool:
    return row.get("kind", "phase") == "phase" and bool(row.get("phase"))


def _answer_problem(data: Any) -> str:
    """Why a parsed answer.json cannot be shown as a decision ('' when it can)."""
    if not isinstance(data, dict):
        return "answer.json is not a JSON object"
    case = data.get("case")
    if not isinstance(case, dict):
        return "answer.json misses required key 'case'"
    if not case.get("verdict"):
        return "answer.json misses required key 'case.verdict'"
    for k in ("next_best_actions", "sar"):
        if not isinstance(data.get(k), dict):
            return f"answer.json misses required key '{k}'"
    return ""


def _failure_note(phases: list[dict]) -> str | None:
    """The error of the last 'case failed' note when no phase row follows it (a later attempt would write one)."""
    for row in reversed(phases):
        if _is_phase_row(row):
            return None
        if row.get("kind") == "note" and row.get("text") == "case failed":
            return str(row.get("error") or "case failed (no error text recorded)")
    return None


def _read_lock(path: Path) -> dict | None:
    """The lock at `path` when its process is alive (os.kill(pid, 0)), else None."""
    data, _ = _read_json(path)
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by another user
    except OSError:
        return None
    return {**data, "run_id": path.parent.name, "path": rel(path)}


def _folder_state(d: Path, lock: dict | None) -> dict:
    """State of one case folder of a run: done, unreadable, failed, running, incomplete or not_run."""
    answer, err = _read_json(d / "answer.json")
    phases, skipped_p = _read_jsonl_counted(d / "phases.jsonl")
    sigs = [s for s in (_sig(d / f) for f in (*TRACE_FILES, "sar.md")) if s]
    newest = max((s[0] for s in sigs), default=None)
    out = {
        "state": "not_run", "answer": None, "error": "", "phases": phases, "skipped": skipped_p,
        "last_phase": next((str(r.get("phase")) for r in reversed(phases) if _is_phase_row(r)), ""),
        "last_write": None if newest is None else newest / 1e9,
        "age": None if newest is None else max(0.0, time.time() - newest / 1e9),
    }
    if (d / "answer.json").exists():
        problem = err or _answer_problem(answer)
        if problem:
            out.update(state="unreadable", error=problem)
        else:
            out.update(state="done", answer=answer)
        return out
    failure = _failure_note(phases)
    if failure is not None:
        out.update(state="failed", error=failure)
    elif newest is not None:
        alive = lock is not None and (not lock.get("cases") or d.name in lock["cases"])
        out["state"] = "running" if alive or (out["age"] is not None and out["age"] < RUNNING_WINDOW_S) else "incomplete"
    return out


def _round_of(ids) -> str:
    hhf = any(str(i).startswith("HHF-") for i in ids)
    hhg = any(str(i).startswith("HHG-") for i in ids)
    return "mixed" if hhf and hhg else "round1" if hhg else "final"


def _run_source(d: Path) -> Source:
    man, man_err = _read_json(d / "run_manifest.json")
    man = man if isinstance(man, dict) else {}
    lock = _read_lock(d / LOCK_FILE)
    states: dict[str, str] = {}
    models: Counter = Counter()
    last_write = None
    skipped = 0
    for c in _case_dirs(d):
        info = _folder_state(c, lock)
        states[c.name] = info["state"]
        rows, sk = _read_jsonl_counted(c / "calls.jsonl")
        skipped += sk + info["skipped"]
        models.update(str(r.get("model") or "?") for r in rows if r.get("kind") == "llm")
        if info["last_write"] is not None:
            last_write = max(last_write or 0.0, info["last_write"])
    present = sorted(states, key=_case_order)
    for cid in man.get("cases") or []:
        if isinstance(cid, str):
            states.setdefault(cid, "not_run")
    case_runs = man.get("case_runs") if isinstance(man.get("case_runs"), dict) else {}
    case_models = Counter(str(v.get("model")) for v in case_runs.values() if isinstance(v, dict) and v.get("model"))
    manifest_model = str(man.get("model") or "")
    notes = []
    if models and case_models and set(case_models) != set(models):
        notes.append(f"case_runs say {', '.join(sorted(case_models))} while calls.jsonl rows say {', '.join(sorted(models))}")
    elif models and manifest_model and manifest_model not in models:
        notes.append(f"the manifest says {manifest_model} while calls.jsonl rows say {', '.join(sorted(models))}")
    invocations = [{
        "started": _ts(inv.get("started")), "resume": bool(inv.get("resume")), "cases": len(inv.get("cases_selected") or []),
        "llm_backend": inv.get("llm_backend", ""), "model": inv.get("model", ""), "run_mode": inv.get("run_mode", ""),
        "git_sha": str(inv.get("git_sha") or "")[:8], "git_dirty": inv.get("git_dirty"),
    } for inv in man.get("invocations") or [] if isinstance(inv, dict)]
    mtc = man.get("max_tool_calls")
    return Source(
        id=d.name, kind="run", round=_round_of(states), mode="live" if str(man.get("run_mode") or man.get("mode") or "") == "live" else "mock",
        dir=d, manifest=man, manifest_error=man_err, cases=present,
        n_answered=sum(1 for s in states.values() if s == "done"), n_running=sum(1 for s in states.values() if s == "running"),
        n_failed=sum(1 for s in states.values() if s == "failed"), n_unreadable=sum(1 for s in states.values() if s == "unreadable"),
        first_started=_ts(man.get("first_started") or man.get("started") or man.get("created_at")), started=_ts(man.get("started")),
        finished=max((_ts(v.get("finished")) for v in case_runs.values() if isinstance(v, dict) and v.get("finished")), default=""),
        llm_backend=str(man.get("llm_backend") or ""), manifest_model=manifest_model, case_models=case_models, model_note="; ".join(notes),
        git_sha=str(man.get("git_sha") or ""), git_dirty=man.get("git_dirty") if isinstance(man.get("git_dirty"), bool) else None,
        max_tool_calls=int(mtc) if isinstance(mtc, int | float) else None, invocations=invocations,
        n_total=len(states), states=states, models_seen=models, last_write=last_write, skipped_lines=skipped,
    )


def _promoted_map() -> dict[str, list[str]]:
    """case id -> the run ids whose answer.json parses equal to cases/<id>.json."""
    run_dirs = _run_dirs()
    out: dict[str, list[str]] = {}
    for p in sorted(cases_dir().glob("HH*-*.json")):
        sub, err = _read_json(p)
        if err or not isinstance(sub, dict):
            continue
        out[p.stem] = [d.name for d in run_dirs if _read_json(d / p.stem / "answer.json")[0] == sub]
    return out


def _submitted_source(sid: str, promoted: dict[str, list[str]]) -> Source | None:
    prefix, rnd = SUBMITTED[sid]
    files = sorted(cases_dir().glob(f"{prefix}*.json"))
    if not files:
        return None
    states: dict[str, str] = {}
    newest = None
    for p in files:
        data, err = _read_json(p)
        states[p.stem] = "unreadable" if (err or _answer_problem(data)) else "done"
        s = _sig(p)
        if s:
            newest = max(newest or 0.0, s[0] / 1e9)
    pf = {cid: promoted.get(cid, []) for cid in states}
    return Source(
        id=sid, kind="submitted", round=rnd, mode="promoted" if any(pf.values()) else "engine", dir=cases_dir(),
        cases=sorted(states, key=_case_order), n_answered=sum(1 for s in states.values() if s == "done"),
        n_unreadable=sum(1 for s in states.values() if s == "unreadable"), n_total=len(states), states=states, promoted_from=pf, last_write=newest,
    )


def _tree_signature() -> tuple:
    """What sources() and promoted_from() depend on: folder entries plus the manifest, answer, trace and cases/ file stamps."""
    root = runs_root()
    entries = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.name[:1] not in "._" and p.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        files = []
        for c in children:
            if c.name in ("run_manifest.json", LOCK_FILE):
                files.append((c.name, _sig(c)))
            elif c.name[:1] not in "._" and c.is_dir():
                files.append((c.name, tuple(_sig(c / f) for f in TRACE_FILES)))
        entries.append((d.name, tuple(files)))
    cases = tuple((p.name, _sig(p)) for p in sorted(cases_dir().glob("HH*-*.json")))
    return str(root), str(cases_dir()), tuple(entries), cases, _pack_files_key()


def _build_sources(sig: tuple) -> list[Source]:
    runs = [_run_source(d) for d in _run_dirs()]
    promoted = _promoted_map()
    submitted = {sid: _submitted_source(sid, promoted) for sid in SUBMITTED}
    final = [s for s in runs if s.round != "round1"]
    round1 = [s for s in runs if s.round == "round1"]
    return [*final, *([submitted["submitted"]] if submitted["submitted"] else []), *round1,
            *([submitted["submitted-r1"]] if submitted["submitted-r1"] else [])]


def sources() -> list[Source]:
    """Agent runs (newest first) and the two submitted-answer sources, Final Round first.
    Cached for 5 s with st.cache_data on a signature of runs_root(), its folder entries and the file stamps."""
    return _st_cache(_build_sources, 5)(_tree_signature())


def get_source(source_id: str | None) -> Source | None:
    return next((s for s in sources() if s.id == source_id), None)


def promoted_from() -> dict[str, list[str]]:
    """Each case id of cases/ -> the run ids whose answer.json parses equal to cases/<id>.json (cached 5 s)."""
    return _st_cache(_build_promoted, 5)(_tree_signature())


def _build_promoted(sig: tuple) -> dict[str, list[str]]:
    return _promoted_map()


def default_source() -> str:
    """UI_DEFAULT_SOURCE when valid; else the Final Round run the submitted answers were promoted from (most answers equal
    to cases/HHF-*.json); else the newest complete Final Round live run; else the newest Final Round run; else 'submitted';
    else the newest Round 1 live run; else 'submitted-r1'."""
    srcs = sources()
    by_id = {s.id: s for s in srcs}
    want = _env("UI_DEFAULT_SOURCE").strip()
    if want in by_id:
        return want
    runs = [s for s in srcs if s.kind == "run"]
    final = [s for s in runs if s.round != "round1"]
    sub = by_id.get("submitted")
    equal = Counter(r for cid, rs in (sub.promoted_from.items() if sub else []) for r in rs)
    promoted = [s for s in final if equal.get(s.id)]
    if promoted:
        return max(promoted, key=lambda s: (equal[s.id], s.first_started)).id
    newest = functools.partial(max, key=lambda s: (s.first_started, s.id))
    for group in ([s for s in final if s.mode == "live" and s.complete], final):
        if group:
            return newest(group).id
    if sub:
        return "submitted"
    r1_live = [s for s in runs if s.round == "round1" and s.mode == "live"]
    if r1_live:
        return newest(r1_live).id
    if "submitted-r1" in by_id:
        return "submitted-r1"
    return srcs[0].id if srcs else ""


def _label_for(s: Source) -> str:
    if s.kind == "submitted":
        head = "Submitted answers" if s.round == "final" else "Round 1 submitted answers"
        where = f"cases/{SUBMITTED[s.id][0]}*.json"
        equal = Counter(r for rs in s.promoted_from.values() for r in rs)
        if equal:
            return f"{head} · {where} · identical to " + ", ".join(f"{r} {n}/{s.n_total}" for r, n in equal.most_common(2))
        return f"{head} · {where} · {s.n_answered}/{s.n_total} answers"
    counts = f"{s.n_answered}/{s.n_total}" + (f" · {s.n_running} running" if s.n_running else "") + (f" · {s.n_failed} failed" if s.n_failed else "")
    if s.mode != "live":
        return f"MOCK · {s.id} · {counts}"
    model = s.models_seen.most_common(1)[0][0] if s.models_seen else (s.manifest_model or s.llm_backend or "model unknown")
    return f"{ROUND_LABELS.get(s.round, s.round)} · LIVE · {s.id} · {model} · {counts}"


def source_label(source_id: str) -> str:
    """e.g. 'Final Round · LIVE · r2-live-gemini · gemini-2.5-flash · 18/18' or 'MOCK · judge · 1/1'."""
    s = get_source(source_id)
    return s.label if s else str(source_id)


def run_provenance(source_id: str) -> dict:
    """How a source was produced, JSON-safe: mode, backend, models actually seen in calls.jsonl next to the manifest's,
    git sha and dirty flag, times, invocations, prompt-hash consistency, completion and read errors."""
    s = get_source(source_id)
    if s is None:
        return {"source": source_id, "errors": [f"unknown source {source_id!r}"]}
    if s.kind == "submitted":
        equal = Counter(r for rs in s.promoted_from.values() for r in rs)
        return {
            "source": s.id, "kind": "submitted answers", "files": f"cases/{SUBMITTED[s.id][0]}*.json", "run_mode": "n/a (answer files, no trace)",
            "promoted_from": dict(equal), "not_identical_to_any_run": sorted(c for c, rs in s.promoted_from.items() if not rs),
            "done": s.n_answered, "total": s.n_total, "errors": [f"{s.n_unreadable} answer file(s) unreadable"] if s.n_unreadable else [],
        }
    man = s.manifest
    hashes = [json.dumps(inv.get("prompt_sha256") or {}, sort_keys=True) for inv in man.get("invocations") or [] if isinstance(inv, dict)]
    top = man.get("prompt_sha256") or {}
    case_runs = man.get("case_runs") if isinstance(man.get("case_runs"), dict) else {}
    per_case_ok = all(str(top.get(k, "")).startswith(str(v)) for cr in case_runs.values() if isinstance(cr, dict)
                      for k, v in (cr.get("prompt_sha256") or {}).items())
    errors = []
    if s.manifest_error:
        errors.append(f"run_manifest.json unreadable: {s.manifest_error}")
    if s.n_unreadable:
        errors.append(f"{s.n_unreadable} answer.json file(s) unreadable")
    if s.skipped_lines:
        errors.append(f"{s.skipped_lines} unparseable JSONL line(s) skipped")
    return {
        "source": s.id, "kind": "agent run", "round": s.round, "run_mode": man.get("run_mode") or man.get("mode") or "unknown",
        "llm_backend": s.llm_backend, "models_seen": dict(s.models_seen), "manifest_model": s.manifest_model, "case_models": dict(s.case_models),
        "model_mismatch": s.model_note, "model_note": s.model_note, "sar_model": man.get("sar_model", ""),
        "git_sha": s.git_sha[:8], "git_dirty": s.git_dirty, "first_started": s.first_started, "finished": s.finished,
        "invocations": {"count": len(s.invocations), "resume": [i["resume"] for i in s.invocations], "cases": [i["cases"] for i in s.invocations]},
        "prompt_hashes_consistent": len(set(hashes)) <= 1 and per_case_ok, "max_tool_calls": s.max_tool_calls,
        "done": s.n_answered, "total": s.n_total, "running": s.n_running, "failed": s.n_failed, "errors": errors,
    }


# --------------------------------------------------------------------------- #
# Case data                                                                   #
# --------------------------------------------------------------------------- #
CASE_STATES = ("done", "running", "incomplete", "failed", "unreadable", "not_run")
STATE_LABELS = {"done": "done", "running": "running", "incomplete": "incomplete", "failed": "failed", "unreadable": "unreadable", "not_run": "not run"}


@dataclass
class CaseRun:
    run_id: str
    case_id: str
    dir: Path
    answer: dict | None = None
    phases: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    sar_md: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.answer is not None


@dataclass
class CaseView:
    """One case in one source. A run supplies answer.json and its own trace; 'submitted' supplies cases/<id>.json only."""

    source_id: str
    case_id: str
    state: str
    answer: dict | None = None        # set only when state == 'done'
    phases: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    sar_md: str = ""
    error: str = ""                   # unreadable / failed: the text, verbatim
    last_phase: str = ""
    last_write_age_s: float | None = None
    meta: dict = field(default_factory=dict)  # the P0 ctx when recorded, else the pack row
    skipped_lines: int = 0
    dir: Path | None = None
    promoted_from: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "done" and self.answer is not None

    @property
    def has_trace(self) -> bool:
        return bool(self.phases or self.calls)


def run_lock(run_id: str | None = None) -> dict | None:
    """The live runs/<id>/.bench.lock (its PID answers os.kill(pid, 0)), of `run_id` or of any run; None otherwise."""
    if run_id is not None:
        return _read_lock(runs_root() / run_id / LOCK_FILE) if _safe_id(run_id) else None
    try:
        locks = sorted(runs_root().glob(f"*/{LOCK_FILE}"))
    except OSError:
        return None
    return next((lk for lk in (_read_lock(p) for p in locks) if lk), None)


def case_view(source_id: str, case_id: str) -> CaseView:
    """Read ONE source: a run folder (answer.json, phases.jsonl, calls.jsonl, sar.md) or cases/<id>.json for 'submitted'.
    Never mixes the two. The parses are cached on (path, mtime_ns, size); the state is recomputed on every call."""
    if not (_safe_id(source_id) and _safe_id(case_id)):
        return CaseView(str(source_id), str(case_id), "not_run", error="invalid source or case id")
    if source_id in SUBMITTED:
        prefix, _ = SUBMITTED[source_id]
        p = cases_dir() / f"{case_id}.json"
        v = CaseView(source_id, case_id, "not_run", dir=cases_dir(), meta=case_meta(case_id), promoted_from=promoted_from().get(case_id, []))
        if not case_id.startswith(prefix):
            return v
        data, err = _read_json(p)
        if data is None and not err:
            return v
        problem = err or _answer_problem(data)
        v.state, v.error, v.answer = ("unreadable", problem, None) if problem else ("done", "", data)
        s = _sig(p)
        v.last_write_age_s = None if s is None else max(0.0, time.time() - s[0] / 1e9)
        return v
    d = runs_root() / source_id / case_id
    if not d.is_dir():
        return CaseView(source_id, case_id, "not_run", dir=d, meta=case_meta(case_id))
    info = _folder_state(d, run_lock(source_id))
    calls, skipped_c = _read_jsonl_counted(d / "calls.jsonl")
    ctx = next(((r.get("payload") or {}).get("ctx") for r in info["phases"] if _is_phase_row(r) and r.get("phase") == "P0"), None)
    return CaseView(
        source_id, case_id, info["state"], answer=info["answer"], phases=info["phases"], calls=calls, sar_md=_read_text(d / "sar.md"),
        error=info["error"], last_phase=info["last_phase"], last_write_age_s=info["age"],
        meta=_pack_row(ctx) if isinstance(ctx, dict) and ctx.get("case_id") else case_meta(case_id),
        skipped_lines=info["skipped"] + skipped_c, dir=d,
    )


def case_state(source_id: str, case_id: str) -> str:
    return case_view(source_id, case_id).state


def source_views(source_id: str) -> dict[str, CaseView]:
    """case id -> CaseView for every case of the source's pack, in pack order (cases never run come back 'not_run')."""
    src = get_source(source_id)
    ids = [r["case_id"] for r in pack_for_source(src)]
    ids += [cid for cid in (src.cases if src else []) if cid not in ids]
    return {cid: case_view(source_id, cid) for cid in ids}


def list_cases_in_run(run_id: str) -> list[str]:
    """Case folders holding answer.json, calls.jsonl or phases.jsonl, in pack (opened_at) order."""
    d = runs_root() / run_id
    if not _safe_id(run_id) or not d.exists():
        return []
    return sorted((p.name for p in _case_dirs(d)), key=_case_order)


def load_case_run(run_id: str, case_id: str) -> CaseRun:
    """Kept for old callers; new code reads case_view(), which also gives the state and the error."""
    d = runs_root() / run_id / case_id
    cr = CaseRun(run_id=run_id, case_id=case_id, dir=d)
    if not (_safe_id(run_id) and _safe_id(case_id)):
        return cr
    data, err = _read_json(d / "answer.json")
    problem = err or ("" if data is None else _answer_problem(data))
    cr.answer = data if data is not None and not problem else None
    cr.error = problem
    cr.phases = _read_jsonl(d / "phases.jsonl")
    cr.calls = _read_jsonl(d / "calls.jsonl")
    cr.sar_md = _read_text(d / "sar.md")
    return cr


def load_promoted_answer(case_id: str) -> dict | None:
    if not _safe_id(case_id):
        return None
    data, err = _read_json(cases_dir() / f"{case_id}.json")
    return data if isinstance(data, dict) and not err else None


def phase_payload(run: Any, code: str) -> dict:
    """Payload of the last phase row for ``code`` (e.g. 'P3'); 'llm' and 'note' rows are ignored; {} when absent."""
    for row in reversed(getattr(run, "phases", None) or []):
        if _is_phase_row(row) and row.get("phase") == code:
            payload = row.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


# --------------------------------------------------------------------------- #
# Derived views                                                               #
# --------------------------------------------------------------------------- #
FAMILY_LABELS = {"history": "card history", "device": "device / shared origin", "memory": "case memory", "customer": "customer / analyst reply", "document": "policy document"}
FAMILY_ORDER = ["history", "device", "memory", "customer", "document"]

_RULE_RE = re.compile(r"\b(R(?:10|[1-9])|3a|3b|§\s?6|policy 6|section 6)\b", re.IGNORECASE)
_DEVICE_REF = re.compile(r"device|(?<![a-z])ring|shared_origin|email_neighbors|burst")  # 'ring_profile', not 'recurring_charge_check'


def family_of_evidence(ev: dict) -> str:
    ref = (ev.get("ref") or "").lower()
    src = ev.get("source")
    if src == "customer":
        return "customer"
    if src == "document":
        return "document"
    if any(k in ref for k in ("prior_cases", "similar", "closed")):
        return "memory"
    if _DEVICE_REF.search(ref):
        return "device"
    return "history"


def families(run: Any) -> tuple[list[str], list[str]]:
    """(families_fraud, families_legit) from the scorecard phase, else derived from the evidence list."""
    p3 = phase_payload(run, "P3")
    if p3.get("families_fraud") is not None or p3.get("families_legit") is not None:
        return sorted(p3.get("families_fraud") or []), sorted(p3.get("families_legit") or [])
    ev = (run.answer or {}).get("case", {}).get("evidence", [])
    fam = sorted({family_of_evidence(e) for e in ev if e.get("source") != "document"})
    return fam, []


def probability_pre_post(run: Any) -> tuple[float | None, float | None]:
    """(pre-evidence, post-evidence) probability. pre = P4 assess (else P3 p_engine); post = P7 (else answer)."""
    ans = run.answer or {}
    post = ans.get("case", {}).get("fraud_probability")
    p7 = phase_payload(run, "P7")
    if p7.get("fraud_probability") is not None:
        post = p7["fraud_probability"]
    p4 = phase_payload(run, "P4")
    p3 = phase_payload(run, "P3")
    pre = p4.get("fraud_probability", p3.get("p_engine"))
    if pre is None:
        pre = post
    return (None if pre is None else float(pre)), (None if post is None else float(post))


def rule_chips(reason: str) -> list[str]:
    seen: list[str] = []
    for m in _RULE_RE.findall(reason or ""):
        tag = m.upper().replace(" ", "")
        tag = {"POLICY6": "§6", "SECTION6": "§6", "3A": "3a", "3B": "3b"}.get(tag, tag)
        if tag not in seen:
            seen.append(tag)
    return seen


def actions_diff(initial: list[dict], final: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """(added, removed, kept) action names between the initial and final recommendation."""
    a = [x.get("action") for x in initial]
    b = [x.get("action") for x in final]
    return [x for x in b if x not in a], [x for x in a if x not in b], [x for x in b if x in a]


def actions_diff_routes(initial: list[dict], final: list[dict]) -> dict:
    """initial -> final as (action, route) pairs, duplicates preserved: added, removed, kept, and route_changed as
    (action, initial route, final route) for an action kept under a different route."""
    def pairs(xs):
        return [(str(x.get("action", "")), str(x.get("route", "auto"))) for x in xs or [] if isinstance(x, dict)]

    left, kept, added = pairs(initial), [], []
    for p in pairs(final):
        if p in left:
            left.remove(p)
            kept.append(p)
        else:
            added.append(p)
    route_changed = []
    for p in list(added):
        other = next((q for q in left if q[0] == p[0]), None)
        if other is not None:
            left.remove(other)
            added.remove(p)
            route_changed.append((p[0], other[1], p[1]))
    return {"added": added, "removed": left, "kept": kept, "route_changed": route_changed}


def counterfactual(run: Any) -> str:
    """What would have happened on the other branch (voi.py / simulator.py), else what_changed."""
    p6 = phase_payload(run, "P6")
    cf = p6.get("counterfactual") or ""
    if not cf:
        sim = p6.get("simulated") or {}
        cf = sim.get("counterfactual") or ""
    if not cf:
        cf = (run.answer or {}).get("next_best_actions", {}).get("what_changed", "")
    return "" if cf == "nothing" else cf


def voi_branches(run: Any) -> dict:
    p6 = phase_payload(run, "P6")
    voi = p6.get("voi") or {}
    return voi.get("branches") or {}


# --------------------------------------------------------------------------- #
# Counting: one rule for reads and tokens on every page                       #
# --------------------------------------------------------------------------- #
def _int(x: Any) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _float(x: Any) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def trace_stats(view: Any) -> dict:
    """The single counting rule (it equals answer.json in every answered live case):
    reads = kind=='tool' rows with caller != 'persist'; writes = caller=='persist' rows;
    tokens = input + output + cache_read + cache_write over kind=='llm' rows. Without a trace the answer's own counters are
    reported and `label` says where they come from."""
    calls = list(getattr(view, "calls", None) or [])
    ans = getattr(view, "answer", None) or {}
    tools = [c for c in calls if c.get("kind", "tool") == "tool"]
    reads = [c for c in tools if c.get("caller") != "persist"]
    writes = [c for c in tools if c.get("caller") == "persist"]
    llms = [c for c in calls if c.get("kind") == "llm"]
    t_in, t_out = sum(_int(c.get("input_tokens")) for c in llms), sum(_int(c.get("output_tokens")) for c in llms)
    t_cr, t_cw = sum(_int(c.get("cache_read_tokens")) for c in llms), sum(_int(c.get("cache_write_tokens")) for c in llms)
    total = t_in + t_out + t_cr + t_cw
    has_trace = bool(calls)
    a_calls, a_tokens = ans.get("tool_calls"), ans.get("tokens")
    by_caller = Counter(str(c.get("caller") or "unrecorded") for c in reads)
    if has_trace:
        label = "calls.jsonl"
    elif getattr(view, "promoted_from", None):
        label = f"promoted from {', '.join(view.promoted_from)} (answer.json counters; the trace is in that run)"
    elif ans and _int(a_tokens) == 0:
        label = "deterministic engine, no LLM calls"
    else:
        label = "answer.json counters (no trace in this source)"
    return {
        "reads": len(reads) if has_trace else _int(a_calls),
        "reads_by_caller": {"harness": by_caller.pop("harness", 0), "llm": by_caller.pop("llm", 0), **by_caller},
        "writes": len(writes), "writes_ok": sum(1 for c in writes if c.get("ok")),
        "llm_calls": len(llms),
        "tokens_total": total if has_trace else _int(a_tokens),
        "tokens_uncached": t_in + t_out if has_trace else None,
        "cache_share": (t_cr + t_cw) / total if has_trace and total else None,
        "latency_s": _float(ans.get("latency_s")),
        "llm_latency_s": round(sum(_float(c.get("latency_s")) or 0 for c in llms), 2),
        "tool_latency_s": round(sum(_float(c.get("latency_s")) or 0 for c in reads), 2),
        "write_latency_s": round(sum(_float(c.get("latency_s")) or 0 for c in writes), 2),
        "models": Counter(str(c.get("model") or "?") for c in llms),
        "matches_answer": (len(reads) == _int(a_calls) and total == _int(a_tokens)) if has_trace and a_calls is not None and a_tokens is not None else None,
        "answer_tool_calls": a_calls, "answer_tokens": a_tokens, "has_trace": has_trace, "label": label,
    }


def tool_stats(run: Any) -> dict:
    """Old keys over trace_stats(): tool_calls (reads), llm_calls, tokens (with cache), latency_s, tool_latency_s, llm_latency_s."""
    s = trace_stats(run)
    latency = s["latency_s"] or sum(_float(c.get("latency_s")) or 0 for c in getattr(run, "calls", None) or [])
    return {"tool_calls": s["reads"], "llm_calls": s["llm_calls"], "tokens": s["tokens_total"], "latency_s": float(latency or 0),
            "tool_latency_s": s["tool_latency_s"], "llm_latency_s": s["llm_latency_s"]}


# --------------------------------------------------------------------------- #
# Decisions, safety and agreement                                             #
# --------------------------------------------------------------------------- #
HARD_ACTIONS = {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"}  # irreversible for an innocent customer
BRIEF_LEGITIMATE = 9  # the Final Round brief: exactly 9 of the 18 cases are legitimate

# One meaning per colour: red fraud / irreversible / failed / safety violation; green legitimate / approved / passed /
# done / agrees / LIVE; blue uncertain (a correct policy stop); violet human-gate routes; orange waiting on a human or
# needs attention; gray auto, neutral, simulated, not run; yellow only for 'medium' data-quality severity.
# Typology palette (.streamlit/config.toml maps Streamlit's colour names onto it): yellow is the ochre of uncertain and
# of anything waiting on a human; blue is the petrol of the L1/L2 human gates; orange is a data or input warning.
VERDICT_COLORS = {"fraud": "red", "legitimate": "green", "uncertain": "yellow"}
ROUTE_COLORS = {"auto": "gray", "L1": "blue", "L2": "blue"}
STATE_COLORS = {"done": "green", "running": "gray", "incomplete": "orange", "failed": "red", "unreadable": "red", "not_run": "gray"}
STATUS_COLORS = {"open": "yellow", "escalated": "yellow", "closed_fraud": "red", "closed_legitimate": "green"}
SOURCE_COLORS = {"graph": "primary", "document": "gray", "customer": "gray", "external": "gray"}
SEVERITY_COLORS = {"critical": "orange", "high": "orange", "medium": "gray", "low": "gray", "info": "gray"}
AGREEMENT_COLORS = {"identical": "green", "same decision": "green", "differs · no hard action": "orange", "differs · hard action": "red", "not comparable": "gray"}
ROUTE_LABELS = {"auto": "auto", "L1": "L1 · team lead", "L2": "L2 · fraud manager"}
VERDICT_HEADLINES = {"fraud": "Fraud — contain", "uncertain": "Uncertain → handed to analyst", "legitimate": "Legitimate — closed"}


def route_label(route: str | None) -> str:
    return ROUTE_LABELS.get(str(route or "auto"), str(route or ""))


def verdict_headline(verdict: str | None) -> str:
    return VERDICT_HEADLINES.get(str(verdict or ""), str(verdict or "no verdict"))


def action_color(action: str, route: str | None = None) -> str:
    return "red" if action in HARD_ACTIONS else ROUTE_COLORS.get(str(route or "auto"), "gray")


def _final(answer: dict) -> list[dict]:
    return [x for x in ((answer.get("next_best_actions") or {}).get("final") or []) if isinstance(x, dict)]


def safety(answers: dict[str, dict]) -> dict:
    """Innocent-customer check over {case_id: answer}: hard actions or a SAR on a legitimate / uncertain verdict, uncertain
    cases not escalated, and the verdict mix."""
    hard, unescalated, sar_nf = [], [], []
    mix: Counter = Counter()
    n_uncertain = 0
    for cid, a in sorted(answers.items()):
        if not isinstance(a, dict):
            continue
        verdict = str((a.get("case") or {}).get("verdict") or "")
        mix[verdict] += 1
        acts = [str(x.get("action")) for x in _final(a)]
        filed = bool((a.get("sar") or {}).get("file"))
        if verdict in ("legitimate", "uncertain"):
            bad = [x for x in acts if x in HARD_ACTIONS] + (["sar.file"] if filed else [])
            if bad:
                hard.append((cid, bad))
            if filed:
                sar_nf.append(cid)
        if verdict == "uncertain":
            n_uncertain += 1
            if "ESCALATE_TO_ANALYST" not in acts:
                unescalated.append(cid)
    return {"hard_on_non_fraud": hard, "uncertain_not_escalated": unescalated, "sar_on_non_fraud": sar_nf, "verdict_mix": mix,
            "n": sum(mix.values()), "n_uncertain": n_uncertain, "uncertain_escalated": n_uncertain - len(unescalated)}


def _pairs_str(pairs) -> str:
    return ", ".join(f"{a} {r}" for a, r in sorted(pairs)) or "none"


def agreement(a: dict | None, b: dict | None) -> dict:
    """Two answers for the same case: identical (parsed JSON equal), same decision (verdict, final (action, route) set and
    sar.file), the field diffs a -> b, a class, and the likely cause (assumed reply, stop-rule test number)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {"identical": False, "same_decision": False, "diffs": ["one side has no answer"], "cls": "not comparable", "cause": [], "delta_p": None}
    ca, cb = a.get("case") or {}, b.get("case") or {}
    fa = {(str(x.get("action")), str(x.get("route", "auto"))) for x in _final(a)}
    fb = {(str(x.get("action")), str(x.get("route", "auto"))) for x in _final(b)}
    ha, hb = {p for p in fa if p[0] in HARD_ACTIONS}, {p for p in fb if p[0] in HARD_ACTIONS}
    sa, sb = bool((a.get("sar") or {}).get("file")), bool((b.get("sar") or {}).get("file"))
    diffs = []
    for name, x, y in (("verdict", ca.get("verdict"), cb.get("verdict")), ("final actions", _pairs_str(fa), _pairs_str(fb)),
                       ("hard actions", _pairs_str(ha), _pairs_str(hb)), ("sar.file", sa, sb), ("pattern", ca.get("pattern"), cb.get("pattern")),
                       ("status", ca.get("status"), cb.get("status")), ("exposure_usd", ca.get("exposure_usd"), cb.get("exposure_usd"))):
        if x != y:
            diffs.append(f"{name}: {x} → {y}")
    pa, pb = _float(ca.get("fraud_probability")), _float(cb.get("fraud_probability"))
    dp = None if pa is None or pb is None else round(abs(pa - pb), 4)
    if dp is not None and dp > 0.10:
        diffs.append(f"fraud_probability: {pa:.2f} → {pb:.2f} (|Δp| {dp:.2f} > 0.10)")
    identical = a == b
    same = ca.get("verdict") == cb.get("verdict") and fa == fb and sa == sb
    cls = "identical" if identical else "same decision" if same else "differs · hard action" if (ha != hb or sa != sb) else "differs · no hard action"
    cause = []
    ra = [str(r.get("assumed_response", "")) for r in a.get("evidence_requests") or [] if isinstance(r, dict)]
    rb = [str(r.get("assumed_response", "")) for r in b.get("evidence_requests") or [] if isinstance(r, dict)]
    if ra != rb:
        cause.append(f"assumed reply: {'; '.join(ra) or 'no request'} → {'; '.join(rb) or 'no request'}")
    ta = sorted(set(re.findall(r"test\s*(\d+)", str(a.get("stop_reason") or ""))))
    tb = sorted(set(re.findall(r"test\s*(\d+)", str(b.get("stop_reason") or ""))))
    if ta != tb:
        cause.append(f"stop rule: §6 test {'/'.join(ta) or '?'} → §6 test {'/'.join(tb) or '?'}")
    return {"identical": identical, "same_decision": same, "diffs": diffs, "cls": cls, "cause": cause, "delta_p": dp}


def inbox_rows(case_id: str | None = None) -> tuple[list[dict], str | None]:
    """Approval rows (optionally of one case) read through approvals_db.connect_ro: never creates the database or runs
    DDL. A missing file gives ([], notice)."""
    path = approvals_db_path()
    if not path.exists():
        return [], f"No approvals database at {rel(path)}: nothing has been queued on this deployment"
    try:
        rows = approvals_db.rows_ro(path=path)
    except sqlite3.Error as exc:
        return [], f"Approvals database {rel(path)} unreadable: {exc}"
    return ([r for r in rows if r.get("case_id") == case_id] if case_id else rows), None


def _persist_rows(view: Any) -> list[dict]:
    return [c for c in getattr(view, "calls", None) or [] if c.get("caller") == "persist"]


def _executed_actions(persist: list[dict]) -> set[str] | None:
    """Actions of the ok persist 'executed' CaseEvent; None when there is none. {'*'} when its payload is unreadable."""
    for c in persist:
        params = c.get("params") if isinstance(c.get("params"), dict) else {}
        if str(c.get("name", "")).endswith("append_case_event") and params.get("kind") == "executed" and c.get("ok"):
            try:
                items = json.loads(params.get("payload") or "[]")
                return {str(x.get("action")) for x in items if isinstance(x, dict)}
            except (TypeError, ValueError):
                return {"*"}
    return None


def action_states(view: Any, inbox: list[dict]) -> list[dict]:
    """Per final action: action, route, reason, state text and colour. auto actions count as executed only when an ok
    persist 'executed' CaseEvent lists them; L1/L2 actions combine the FraudGraph Approval write with the local inbox row."""
    ans = view.answer if getattr(view, "state", "done") == "done" else None
    if not isinstance(ans, dict):
        return []
    persist = _persist_rows(view)
    executed = _executed_actions(persist)
    graph_approvals = {}
    for c in persist:
        params = c.get("params") if isinstance(c.get("params"), dict) else {}
        if str(c.get("name", "")).endswith("record_approval") and c.get("ok"):
            graph_approvals[str(params.get("action"))] = str(params.get("status") or "pending")
    local = {str(r.get("action")): r for r in inbox or [] if r.get("case_id") == view.case_id}
    traced = bool(getattr(view, "calls", None))
    out = []
    for a in _final(ans):
        act, route = str(a.get("action", "")), str(a.get("route", "auto"))
        row = {"action": act, "route": route, "route_label": route_label(route), "reason": str(a.get("reason", "")), "hard": act in HARD_ACTIONS, "inbox_id": None}
        if route == "auto":
            done = executed is not None and ("*" in executed or act in executed)
            row.update(state="executed — CaseEvent executed, persist ok" if done else "recommended — not executed", color="green" if done else "gray",
                       decision="executed" if done else "recommended")
        else:
            g = graph_approvals.get(act)
            graph_part = f"Approval in FraudGraph: {g}" if g else ("no Approval written to FraudGraph" if traced else "FraudGraph write not traced in this source")
            r = local.get(act)
            if r is None:
                local_part, decision, color = "not queued", "not queued", "orange" if g == "pending" else "gray"
            elif r.get("status") == "pending":
                local_part, decision, color = f"inbox #{r.get('id')} pending", "pending", "yellow"
            elif r.get("status") == "approved":
                local_part, decision, color = f"approved by {r.get('decided_by')} at {r.get('decided_at')}", "approved", "green"
            else:
                local_part, decision, color = f"rejected by {r.get('decided_by')} at {r.get('decided_at')}", "rejected", "gray"
            row.update(state=f"{graph_part} · {local_part}", color=color, decision=decision, inbox_id=r.get("id") if r else None)
        out.append(row)
    return out


def lifecycle(view: Any, inbox: list[dict]) -> dict:
    """Where the case stands now (a pure-Python copy of engine.containment.resolution_lifecycle over the honest action
    states): stage, next_owner, open_items, effective_status."""
    ans = view.answer if getattr(view, "state", "done") == "done" else None
    if not isinstance(ans, dict):
        return {"stage": "no decision in this source", "stage_code": "none", "next_owner": "—", "open_items": [], "effective_status": ""}
    case = ans.get("case") or {}
    states = action_states(view, inbox)
    gated = [s for s in states if s["route"] in ("L1", "L2")]
    auto = [s for s in states if s["route"] == "auto"]
    decided = [s for s in gated if s["decision"] in ("approved", "rejected")]
    escalated = any(s["action"] == "ESCALATE_TO_ANALYST" for s in auto)
    if not gated:
        code = "resolved_autonomously"
        stage = "resolved autonomously" if auto and all(s["decision"] == "executed" for s in auto) else "recommended — not executed in this source"
        effective = case.get("status", "")
    elif len(decided) < len(gated):
        code, stage = "auto_contained_awaiting_human_approval", "awaiting human approval"
        effective = "escalated" if escalated else "open"
    elif any(s["decision"] == "approved" and s["action"] in ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION") for s in gated):
        code, stage, effective = "resolved_fraud_contained_by_human", "contained by human approval", "closed_fraud"
    elif all(s["decision"] == "rejected" for s in gated):
        code, stage, effective = "resolved_cleared_by_human_override", "cleared by human override", "closed_legitimate"
    else:
        code, stage, effective = "resolved_by_human", "resolved by human decision", case.get("status", "")
    open_items = [f"{s['action']} awaiting {s['route']}" for s in gated if s["decision"] not in ("approved", "rejected")]
    no_reply = any("no reply" in str(r.get("assumed_response", "")).lower() for r in ans.get("evidence_requests") or [] if isinstance(r, dict))
    if no_reply:
        open_items.append("customer reply outstanding (simulated)")
    if escalated:
        open_items.append("analyst review (ESCALATE_TO_ANALYST)")
    open_items += [f"{s['action']} recommended, not executed" for s in auto if s["decision"] != "executed"]
    pending_routes = {s["route"] for s in gated if s["decision"] not in ("approved", "rejected")}
    owner = ("fraud manager (L2)" if "L2" in pending_routes else "team lead (L1)" if "L1" in pending_routes
             else "analyst" if escalated else "customer (simulated reply)" if no_reply else "none")
    return {"stage": stage, "stage_code": code, "next_owner": owner, "open_items": open_items, "effective_status": effective}


# --------------------------------------------------------------------------- #
# Trace views: timeline, guardrails, claims, SAR citations, TigerGraph usage  #
# --------------------------------------------------------------------------- #
PHASE_NAMES = {"P0": "intake", "P1": "memory", "P2": "investigate", "P3": "scorecard", "P4": "assess", "P5": "nba_initial", "P6": "evidence",
               "P7": "nba_final", "P8": "sar", "P9": "explain", "P10": "persist"}
GUARDRAIL_TYPES = ("security_alert", "clamp", "policy_reprompt", "structured_retry", "sar_validation_failed", "sar_fallback", "dropped_ids",
                   "case_failed", "failed_call", "memory_exclusion", "vector_index", "card_mismatch")
_NOTE_TYPES = [
    (re.compile(r"^security alert", re.I), "security_alert"),
    (re.compile(r"clamped to p_engine", re.I), "clamp"),
    (re.compile(r"re-prompting", re.I), "policy_reprompt"),
    (re.compile(r"retrying structured output", re.I), "structured_retry"),
    (re.compile(r"^SAR validation failed", re.I), "sar_validation_failed"),
    (re.compile(r"fallback_narrative", re.I), "sar_fallback"),
    (re.compile(r"dropped unknown entity_ids", re.I), "dropped_ids"),
    (re.compile(r"^case failed$", re.I), "case_failed"),
    (re.compile(r"case memory excludes", re.I), "memory_exclusion"),
    (re.compile(r"vector index status", re.I), "vector_index"),
    (re.compile(r"case card differs", re.I), "card_mismatch"),
]
# logged next to, not inside, the phase they belong to (P8 has no phase row in most runs; the timeline then falls back)
_EVENT_PHASE = {"security_alert": "P0", "memory_exclusion": "P1", "sar_validation_failed": "P8", "sar_fallback": "P8"}
_NOTE_FIELDS = ("codes", "clamped", "llm", "violations", "problems", "error", "attempt", "residual", "dropped", "ref", "ready", "n", "pack_card", "txn_card")


def _fold(code: str) -> str:
    """P5-voi folds into P5 and P7-retry into P7."""
    return str(code or "").split("-")[0]


def _events(view: Any) -> list[tuple[int | None, dict]]:
    out: list[tuple[int | None, dict]] = []
    for i, row in enumerate(getattr(view, "phases", None) or []):
        if row.get("kind") != "note":
            continue
        text = str(row.get("text") or "")
        kind = next((k for rx, k in _NOTE_TYPES if rx.search(text)), None)
        if kind is None:
            continue
        m = re.match(r"^(P\d+):", text)
        ev = {"type": kind, "t": _float(row.get("t")), "text": text, "phase": m.group(1) if m else _EVENT_PHASE.get(kind, "")}
        ev.update({k: row[k] for k in _NOTE_FIELDS if k in row})
        if kind in ("memory_exclusion", "vector_index", "card_mismatch"):
            ev["info"] = {k: row[k] for k in ("n", "ready", "pack_card", "txn_card") if k in row}
        out.append((i, ev))
    for c in getattr(view, "calls", None) or []:
        if c.get("kind", "tool") == "tool" and c.get("ok") is False:
            out.append((None, {"type": "failed_call", "t": _float(c.get("t")), "text": f"{c.get('name')} failed", "phase": _fold(c.get("phase", "")),
                               "name": c.get("name"), "error": str(c.get("error") or ""), "caller": c.get("caller", "")}))
    return out


def guardrail_events(view: Any) -> list[dict]:
    """phases.jsonl notes and failed calls as typed events: security_alert (codes, t), clamp, policy_reprompt (violations),
    structured_retry (error), sar_validation_failed, sar_fallback, dropped_ids, case_failed (error), failed_call (name,
    error), memory_exclusion and vector_index (info), card_mismatch."""
    return [ev for _, ev in _events(view)]


def guardrail_ledger(source_id: str, all_sources: bool = False) -> list[dict]:
    """Counts per guardrail type with the case ids (and sources) that hit it; zero counts are listed too."""
    ids = [s.id for s in sources() if s.kind == "run"] if all_sources else [source_id]
    ledger = {t: {"type": t, "count": 0, "cases": [], "sources": []} for t in GUARDRAIL_TYPES}
    for sid in ids:
        if sid in SUBMITTED:
            continue
        for cid in list_cases_in_run(sid):
            for ev in guardrail_events(case_view(sid, cid)):
                row = ledger.setdefault(ev["type"], {"type": ev["type"], "count": 0, "cases": [], "sources": []})
                row["count"] += 1
                if cid not in row["cases"]:
                    row["cases"].append(cid)
                if sid not in row["sources"]:
                    row["sources"].append(sid)
    return list(ledger.values())


def _clean_params(value: Any) -> Any:
    """params with embedding vectors replaced by '<N-d vector>'."""
    if isinstance(value, dict):
        return {k: _clean_params(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) >= 32 and all(isinstance(x, int | float) for x in value):
            return f"<{len(value)}-d vector>"
        return [_clean_params(v) for v in value]
    return value


def _short(name: Any) -> str:
    return re.sub(r"^(query|mcp|pytg):", "", str(name or ""))


def _call_row(c: dict) -> dict:
    return {"seq": c.get("seq"), "ts": c.get("ts", ""), "name": c.get("name", ""), "caller": c.get("caller", ""), "phase": c.get("phase", ""),
            "latency_s": _float(c.get("latency_s")), "ok": c.get("ok"), "error": str(c.get("error") or ""), "params": _clean_params(c.get("params"))}


def _actions_str(actions: Any) -> str:
    return ", ".join(f"{x.get('action')} ({x.get('route', 'auto')})" for x in actions or [] if isinstance(x, dict)) or "none"


def _phase_sentence(code: str, entry: dict, view: Any, events: list[dict]) -> str:
    p = entry["payload"]
    meta = getattr(view, "meta", None) or {}
    if code == "P0":
        codes = [c for e in events if e["type"] == "security_alert" for c in e.get("codes") or []]
        s = f"{meta.get('trigger_type') or 'trigger'} on txn {meta.get('flagged_txn_id') or '?'} (card {meta.get('card_id') or '?'}, opened {meta.get('opened_at') or '?'})"
        return s + (f" · injection stripped: {', '.join(codes)}" if codes else " · no injection markers")
    if code == "P1":
        hits = [h for h in p.get("similar_prior_cases") or [] if isinstance(h, dict)]
        closed = [h for h in hits if h.get("kind", "closed") == "closed"]
        agent = [h for h in hits if h.get("kind") == "agent"]
        chunks = len(p.get("grounding") or [])
        if not hits:
            ids = [str(h) for h in p.get("similar_prior_cases") or []]
            return f"{len(ids)} similar cases ({', '.join(ids[:4])}) · {chunks} policy chunks"
        near = min(closed or hits, key=lambda h: _float(h.get("distance")) or 9.0)
        d = _float(near.get("distance"))
        s = f"{len(closed)} similar closed cases, nearest {near.get('id')} at {d:.3f} ({near.get('outcome_or_verdict') or '?'})" if d is not None else f"{len(closed)} similar closed cases"
        return s + (f" · {len(agent)} earlier agent cases" if agent else "") + f" · {chunks} policy chunks"
    if code == "P2":
        recorded = any(c["caller"] for c in entry["calls"])  # old and mock traces have no caller: every P2 read is the LLM's
        chosen = [c for c in entry["calls"] if c["caller"] == "llm" or not recorded]
        cap = run_manifest(view.source_id).get("max_tool_calls") if getattr(view, "source_id", "") not in SUBMITTED else None
        names = ", ".join(_short(c["name"]) for c in chosen) or "none"
        return f"LLM chose {len(chosen)}" + (f" of {cap}" if cap else "") + f" reads: {names}"
    if code == "P3":
        adj = "; ".join(f"{a[0]} {float(a[1]):+.2f}" for a in p.get("adjustments") or [] if isinstance(a, list | tuple) and len(a) == 2 and _float(a[1]) is not None)
        pe = _float(p.get("p_engine"))
        return (f"p_engine {pe:.2f}" if pe is not None else "p_engine not recorded") + (f" · adjustments: {adj}" if adj else " · no adjustments")
    if code == "P4":
        pm = _float(p.get("fraud_probability"))
        basis = str((p.get("assessment") or {}).get("calibration_basis") or p.get("calibration_basis") or "")
        clamp = next((e for e in events if e["type"] == "clamp"), None)
        s = f"model p {pm:.2f}" if pm is not None else "model p not recorded"
        if clamp:
            s += f" · clamped to p_engine ± 0.10 (model said {clamp.get('llm')})"
        return s + (f" · basis: {basis[:240]}" if basis else "")
    if code == "P5":
        voi = p.get("voi") or {}
        allowed = (p.get("admissible") or {}).get("allowed") or []
        s = f"admissible {len(allowed)} actions; chose {_actions_str(p.get('actions'))}" if allowed else f"chose {_actions_str(p.get('actions'))}"
        if voi:
            s += f" · asks {voi.get('request_type')}" if voi.get("ask") else " · no request (it would not change the action)"
        return s
    if code == "P6":
        req = p.get("request") or {}
        if not req:
            return "no evidence request"
        return f"request {req.get('type', '?')} after step {req.get('asked_after_step', '?')}; reply {req.get('assumed_response', '?')} (simulated)"
    if code == "P7":
        allowed = (p.get("admissible") or {}).get("allowed") or []
        return (f"admissible {len(allowed)} actions; " if allowed else "") + f"final {_actions_str(p.get('actions'))}"
    if code == "P8":
        sar = (getattr(view, "answer", None) or {}).get("sar") or {}
        filed = p.get("file") if "file" in p else sar.get("file")
        drafts = f" · {len(entry['llm'])} LLM draft call(s)" if entry["llm"] else ""
        return (f"SAR {'filed' if filed else 'not filed'}" if filed is not None else "SAR filing not in a parsed answer") + drafts
    if code == "P9":
        problems = p.get("validation") or []
        return f"validation: {len(problems)} problem(s): {'; '.join(map(str, problems))[:240]}" if problems else "validation: no problems"
    if code == "P10":
        persist = [c for c in _persist_rows(view)]
        if not persist:
            return f"written_to_graph={p.get('written_to_graph')} (no persist rows in calls.jsonl)"
        n_case = sum(1 for c in persist if "AgentCase" in str(c.get("name")))
        n_ev = sum(1 for c in persist if str(c.get("name")).endswith("append_case_event"))
        n_ap = sum(1 for c in persist if str(c.get("name")).endswith("record_approval"))
        bad = sum(1 for c in persist if not c.get("ok"))
        return f"AgentCase {n_case} + {n_ev} CaseEvents + {n_ap} Approvals, " + ("all ok" if not bad else f"{bad} failed")
    return ""


def trace_timeline(view: Any) -> list[dict]:
    """One row per phase in execution order (P5-voi folded into P5): code, name, t, duration, sentence, calls, llm, notes
    and payload, for the newest attempt in the folder (a --resume rerun appends to the same files). Vectors in params are
    replaced by '<N-d vector>'."""
    view = _attempt_view(view)
    rows: list[dict] = []
    by_code: dict[str, dict] = {}
    phase_index: list[tuple[int, str]] = []
    prev_t = 0.0
    for i, r in enumerate(getattr(view, "phases", None) or []):
        if not _is_phase_row(r):
            continue
        code, raw = _fold(r.get("phase")), str(r.get("phase"))
        entry = by_code.get(code)
        if entry is None:
            entry = by_code[code] = {"code": code, "name": r.get("name") or PHASE_NAMES.get(code, ""), "t": None, "start": prev_t, "duration": None,
                                     "sentence": "", "calls": [], "llm": [], "notes": [], "payload": {}}
            rows.append(entry)
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        if raw == "P5-voi":
            entry["payload"]["voi"] = payload
        else:
            entry["payload"].update(payload)
            entry["name"] = r.get("name") or entry["name"]
        t = _float(r.get("t"))
        if t is not None:
            entry["t"] = t
            entry["duration"] = round(t - entry["start"], 3)
            prev_t = t
        phase_index.append((i, code))
    # P8 writes no phase row: its SAR-draft LLM calls (P8, P8-retryN) still get an entry, placed by their time, and the
    # entry after it starts where P8 ended
    p8 = [_float(c.get("t")) for c in getattr(view, "calls", None) or [] if _fold(c.get("phase", "")) == "P8"]
    if p8 and "P8" not in by_code:
        t8 = max((t for t in p8 if t is not None), default=None)
        if t8 is None:  # untimed rows: P8 runs after P7 and before the P9 closing and P10
            pos = next((k for k, e in enumerate(rows) if e["code"] in ("P9", "P10")), len(rows))
        else:
            pos = next((k for k, e in enumerate(rows) if e["t"] is not None and e["t"] > t8), len(rows))
        start = next((e["t"] for e in reversed(rows[:pos]) if e["t"] is not None), 0.0)
        by_code["P8"] = {"code": "P8", "name": PHASE_NAMES["P8"], "t": t8, "start": start, "duration": None if t8 is None else round(t8 - start, 3),
                         "sentence": "", "calls": [], "llm": [], "notes": [], "payload": {}}
        rows.insert(pos, by_code["P8"])
        if t8 is not None and pos + 1 < len(rows) and rows[pos + 1]["t"] is not None:
            rows[pos + 1]["start"] = t8
            rows[pos + 1]["duration"] = round(rows[pos + 1]["t"] - t8, 3)
    # the P9 closing LLM call runs before the P10 write-back, but the P9 row is written after it: P10 starts where that call
    # ended, and P9's duration is its closing span plus the validation tail after P10
    p9, p10 = by_code.get("P9"), by_code.get("P10")
    if p9 is not None and p10 is not None and p10["t"] is not None:
        closing = [t for t in (_float(c.get("t")) for c in getattr(view, "calls", None) or [] if c.get("kind") == "llm" and _fold(c.get("phase", "")) == "P9")
                   if t is not None and t <= p10["t"]]
        if closing:
            seg = p10["start"]
            p10["start"], p10["duration"] = max(closing), round(p10["t"] - max(closing), 3)
            if p9["t"] is not None:
                p9["start"], p9["duration"] = seg, round((max(closing) - seg) + (p9["t"] - p10["t"]), 3)
    for c in getattr(view, "calls", None) or []:
        code = _fold(c.get("phase", ""))
        entry = by_code.get(code)
        if entry is None:
            continue
        if c.get("kind") == "llm":
            entry["llm"].append({"model": c.get("model", ""), "phase": c.get("phase", ""), "latency_s": _float(c.get("latency_s")),
                                 "tokens": sum(_int(c.get(k)) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))})
        else:
            entry["calls"].append(_call_row(c))
    for idx, ev in _events(view):
        # a note is written while its phase runs, so before that phase's row: the next phase row is its phase
        after = next((cd for j, cd in phase_index if j > idx), phase_index[-1][1] if phase_index else "") if idx is not None else ""
        code = next((c for c in (ev.get("phase"), after) if c in by_code), None)
        if code:
            by_code[code]["notes"].append(ev)
    for entry in rows:
        entry["sentence"] = _phase_sentence(entry["code"], entry, view, entry["notes"])
    return rows


# --------------------------------------------------------------------------- #
# Pipeline: the S0 + P0-P10 strip, the live call console, the phase inspector #
# --------------------------------------------------------------------------- #
# The stages in agent/phase_machine.py's order. S0 is the schema and data-quality gate that runs offline before any case
# (schema_engine/): its result is the proof snapshot's dq and drift sections, never part of a case trace. 'llm' marks the
# stages whose code calls the LLM: P2 the investigate tool loop, P4 the assessment, P5 and P7 the action choice, P8 the
# SAR draft (only when a SAR is filed) and P9 the closing. The others are harness reads, the engine or graph writes.
PIPELINE: tuple[dict[str, Any], ...] = (
    {"code": "S0", "label": "Schema & DQ gate", "module": "schema_engine/, offline before any case (proof snapshot dq and drift)", "llm": False},
    {"code": "P0", "label": "Intake & shield", "module": "agent/phase_machine.py p0_intake · agent/security.py", "llm": False},
    {"code": "P1", "label": "Case memory", "module": "p1_memory · TigerVector similar_prior_cases and grounding_chunks", "llm": False},
    {"code": "P2", "label": "Investigate", "module": "p2_investigate · LLM tool loop over installed read queries", "llm": True},
    {"code": "P3", "label": "Scorecard", "module": "p3_scorecard · engine/scorecard.py, engine/detectors.py", "llm": False},
    {"code": "P4", "label": "Assess ±0.10", "module": "p4_assess · agent/security.py clamp_to_engine", "llm": True},
    {"code": "P5", "label": "Initial actions", "module": "engine/voi.py value-of-information test · _decide('P5') · engine/policy.py", "llm": True},
    {"code": "P6", "label": "Evidence request", "module": "p6_evidence · engine/simulator.py (assumed reply)", "llm": False},
    {"code": "P7", "label": "Final actions", "module": "_decide('P7') · engine/policy.py", "llm": True},
    {"code": "P8", "label": "SAR draft", "module": "p8_sar · rag/sar.py, rag/validate_sar.py", "llm": True},
    {"code": "P9", "label": "Explain & validate", "module": "Closing (LLM) · answer builder · agent/validator.py", "llm": True},
    {"code": "P10", "label": "Write-back", "module": "agent/persist.py", "llm": False},
)
PIPELINE_CODES = tuple(s["code"] for s in PIPELINE)
STAGE_STYLE = {  # state -> (badge color, material icon)
    "done": ("green", ":material/check_circle:"), "active": ("blue", ":material/autorenew:"), "stopped": ("orange", ":material/error:"),
    "skipped": ("gray", ":material/remove:"), "pending": ("gray", ":material/radio_button_unchecked:"), "offline": ("violet", ":material/database:"),
}
STAGE_LABELS = {"done": "done", "active": "running", "stopped": "stopped here", "skipped": "no row", "pending": "not reached", "offline": "offline gate"}
_SAR_OR_CLOSING = "an LLM call: the SAR draft when a SAR is filed (after its OFAC and grounding reads), else the P9 closing"
# What a case that has not finished waits on after its newest row, in agent/phase_machine.py's order:
# (row kind, phase) -> (stage now running, what it waits on, the model it waits on: '' no LLM call, 'main', 'sar' for the
# SAR draft, 'either' when it is the SAR draft or the closing). Rows are written when a step completes, so this is an
# inference from the two trace files, and the pages label it so. A P5 or P7 phase row is decided by its payload
# (_after_decision); a newer retry note, or the phase a failed case's error names, overrides the table.
_AFTER: dict[tuple[str, str], tuple[str, str, str]] = {
    ("phase", "P0"): ("P0", "the intake reads", ""),
    ("tool", "P0"): ("P0", "the intake reads, or P1's vector search", ""),
    ("tool", "P1"): ("P1", "the vector search", ""),
    ("phase", "P1"): ("P2", "an LLM investigate turn", "main"),
    ("tool", "P2"): ("P2", "the LLM's next investigate turn, or the next read it asked for", "main"),
    ("llm", "P2"): ("P2", "the reads the LLM asked for, or the end of its tool loop", ""),
    ("phase", "P2"): ("P3", "the scorecard reads and the engine", ""),
    ("tool", "P3"): ("P3", "the scorecard reads and the engine", ""),
    ("phase", "P3"): ("P4", "the LLM assessment", "main"),
    ("llm", "P4"): ("P4", "the clamp to p_engine ± 0.10", ""),
    ("phase", "P4"): ("P5", "the value-of-information test", ""),
    ("phase", "P5-voi"): ("P5", "the LLM's initial action choice", "main"),
    ("llm", "P5"): ("P5", "the policy check of the chosen actions", ""),
    ("phase", "P6"): ("P7", "the LLM's final action choice", "main"),
    ("llm", "P7"): ("P7", "the policy check of the chosen actions", ""),
    ("tool", "P8"): ("P8", "the LLM's SAR draft", "sar"),
    # a draft the validator rejects is followed by a 'SAR validation failed' note, then the retry (the retry-note rule)
    ("llm", "P8"): ("P9", "the LLM's P9 closing (no newer 'SAR validation failed' note)", "main"),
    ("llm", "P9"): ("P10", "the graph write-back (or a P9 summary retry)", ""),
    ("tool", "P10"): ("P10", "the graph write-back", ""),
    ("phase", "P10"): ("P9", "answer assembly and validation", ""),
}
# phases.jsonl notes written right before the agent calls the LLM again for the same phase
_RETRY_EVENTS = {"structured_retry": "a structured-output retry", "policy_reprompt": "a re-prompt",
                 "sar_validation_failed": "a P8 retry after the SAR validator (or the fallback narrative)"}


def _row_key(r: dict) -> tuple[str, str]:
    kind, raw = str(r.get("kind") or "phase"), str(r.get("phase") or "")
    return kind, raw if kind == "phase" and raw == "P5-voi" else _fold(raw)


def _latest_attempt(view: Any) -> tuple[list[dict], list[dict]]:
    """(phases, calls) of one attempt in a case folder: the newest, or for a finished case the newest that wrote answer.json
    (its P9 row; a later rerun may have failed). A rerun of a case into the same run appends to the same files (t restarts
    at 0), and every attempt opens with its P0 phase row plus the notes written just before it (never the previous
    attempt's 'case failed' note). Calls are cut at the P0 rows' wall clock ('at'; calls carry 'ts' in the same format),
    else at their t resets."""
    phases, calls = list(getattr(view, "phases", None) or []), list(getattr(view, "calls", None) or [])
    p0 = [i for i, r in enumerate(phases) if _is_phase_row(r) and r.get("phase") == "P0"]
    if len(p0) < 2:
        return phases, calls
    starts = []
    for i in p0:
        start, t0 = i, _float(phases[i].get("t")) or 0.0
        while (start > 0 and phases[start - 1].get("kind") == "note" and phases[start - 1].get("text") != "case failed"
               and (_float(phases[start - 1].get("t")) or 0.0) <= t0 and (not starts or start - 1 >= starts[-1])):
            start -= 1
        starts.append(start)
    pick = len(p0) - 1
    if getattr(view, "state", "") in ("done", "unreadable"):
        wrote = [k for k in range(len(p0)) if any(_is_phase_row(r) and r.get("phase") == "P9" for r in phases[p0[k]:(starts[k + 1] if k + 1 < len(p0) else None)])]
        pick = wrote[-1] if wrote else pick
    end = starts[pick + 1] if pick + 1 < len(p0) else None
    at, at_next = str(phases[p0[pick]].get("at") or ""), (str(phases[p0[pick + 1]].get("at") or "") if end is not None else "")
    if at:
        return phases[starts[pick]:end], [c for c in calls if str(c.get("ts") or "") >= at and (not at_next or str(c.get("ts") or "") < at_next)]
    cuts = [0] + [i for i in range(1, len(calls)) if (a := _float(calls[i - 1].get("t"))) is not None and (b := _float(calls[i].get("t"))) is not None and b < a - 1.0]
    k = pick if len(cuts) == len(p0) else len(cuts) - 1
    return phases[starts[pick]:end], calls[cuts[k]:(cuts[k + 1] if k + 1 < len(cuts) else None)]


def _attempt_view(view: Any) -> Any:
    """The view cut to one attempt (_latest_attempt); the view itself when its folder holds one attempt."""
    phases, calls = _latest_attempt(view)
    if len(phases) == len(getattr(view, "phases", None) or []) and len(calls) == len(getattr(view, "calls", None) or []):
        return view
    if dataclasses.is_dataclass(view) and not isinstance(view, type):
        return dataclasses.replace(view, phases=phases, calls=calls)
    try:
        return SimpleNamespace(**{**vars(view), "phases": phases, "calls": calls})
    except TypeError:  # an object without __dict__: keep it whole
        return view


def answer_attempt_view(view: Any) -> Any:
    """The view as a page should read a case: one attempt, the newest, or for a finished case the one that wrote
    answer.json (a rerun appends to the same files). `attempts_total` keeps how many attempts the folder holds."""
    n = trace_attempts(view)
    cut = _attempt_view(view)
    if cut is not view:
        try:
            cut.attempts_total = n
        except AttributeError:
            pass
    return cut


def trace_attempts(view: Any) -> int:
    """Attempts recorded in a case folder (its P0 phase rows; a rerun appends to the same files), also on a cut view."""
    return getattr(view, "attempts_total", None) or sum(1 for r in getattr(view, "phases", None) or [] if _is_phase_row(r) and r.get("phase") == "P0")


def _newest_row(view: Any) -> dict | None:
    """The newest phase row (phases.jsonl) or call (calls.jsonl, tool and llm) of a case by t. Notes and the llm copies in
    phases.jsonl are skipped (calls.jsonl has each one); a phase row wins a tie, as it is written after its last call.
    Callers pass the newest attempt only (_attempt_view): t restarts at 0 in a --resume rerun."""
    best, row = None, None
    for prio, rows in ((0, getattr(view, "calls", None) or []), (1, [r for r in getattr(view, "phases", None) or [] if _is_phase_row(r)])):
        for i, r in enumerate(rows):
            t = _float(r.get("t"))
            key = (-1.0 if t is None else t, prio, i)
            if best is None or key >= best:
                best, row = key, r
    return row


def _after_decision(view: Any, code: str) -> tuple[str, str, str]:
    """After the last decision row (P7, or P5 when P5-voi's ask is false): p8_sar drafts only when FILE_REPORT is in the
    final actions, so without it the next call is the P9 closing. Actions not recorded keep the hedge."""
    acts = phase_payload(view, code).get("actions")
    if not isinstance(acts, list):
        return "P8", _SAR_OR_CLOSING, "either"
    if any(isinstance(a, dict) and a.get("action") == "FILE_REPORT" for a in acts):
        return "P8", "the SAR step's OFAC and grounding reads (the LLM's SAR draft follows them)", ""
    return "P9", "the LLM's P9 closing (no SAR to draft: FILE_REPORT is not in the final actions)", "main"


def llm_models(views: Any) -> Counter:
    """model -> llm rows in calls.jsonl over the given case views, the model as each row records it (never inferred)."""
    return Counter(str(c.get("model")) for v in views for c in getattr(v, "calls", None) or [] if c.get("kind") == "llm" and c.get("model"))


def pipeline_state(view: Any, run_model: str = "", run_sar_model: str = "", since: str = "") -> dict:
    """The S0 + P0-P10 stages of one case, each with a state (done, active, stopped, skipped = no row, pending; S0 is
    'offline') and its row count, read from the newest attempt in the case's phases.jsonl and calls.jsonl only. For a case
    that has not finished, the stage after its newest row and what that stage waits on follow agent/phase_machine.py's
    order: an inference, as rows are written when a step completes. A newer retry / re-prompt note (structured_retry,
    policy_reprompt, sar_validation_failed) means the LLM is being called again for its phase; a failed case whose error
    names its phase ('... during P2') stopped there. The model of an LLM call in flight is the newest one calls.jsonl
    records for that kind of call (P8 rows for the SAR draft, the other rows otherwise): this case's, else `run_model` /
    `run_sar_model` (the caller reads them from the run's other calls.jsonl files), else ''. `since` (the live lock's start)
    keeps an earlier invocation's rows out of this case's own."""
    view = _attempt_view(view)
    phases, calls = getattr(view, "phases", None) or [], getattr(view, "calls", None) or []
    rows: Counter = Counter(_fold(r.get("phase")) for r in phases if _is_phase_row(r))
    rows.update(_fold(c.get("phase")) for c in calls if c.get("phase"))
    state = getattr(view, "state", "")
    finished = state in ("done", "unreadable")
    newest = _newest_row(view)
    active, waiting, kind, basis = None, "", "", "inferred"
    if not finished and newest is not None:
        key = _row_key(newest)
        if key == ("phase", "P5"):
            ask = bool(phase_payload(view, "P5-voi").get("ask"))
            active, waiting, kind = ("P6", "the assumed reply to the evidence request", "") if ask else _after_decision(view, "P5")
        elif key == ("phase", "P7"):
            active, waiting, kind = _after_decision(view, "P7")
        elif key == ("tool", "P8"):  # p8_sar reads OFAC and two grounding sets, then drafts
            reads = sum(1 for c in calls if _fold(c.get("phase")) == "P8" and "grounding_chunks" in str(c.get("name") or ""))
            active, waiting, kind = ("P8", "the LLM's SAR draft", "sar") if reads >= 2 else ("P8", "the SAR step's OFAC and grounding reads", "")
        elif key != ("phase", "P9"):  # the P9 row is the last one a case writes
            active, waiting, kind = _AFTER.get(key, (key[1] if key[1] in PIPELINE_CODES else None, "", ""))
        # a retry / re-prompt note newer than that row: the agent is calling the LLM again for the note's phase
        t_new = _float(newest.get("t"))
        # ('case failed' is written after every failure, so it never hides the retry note before it)
        last = max(((ev["t"], i, ev) for i, (_, ev) in enumerate(_events(view)) if ev["t"] is not None and ev["type"] != "case_failed"),
                   default=None, key=lambda x: x[:2])
        if last and t_new is not None and last[0] >= t_new and last[2]["type"] == "sar_fallback" and active == "P9":
            waiting = "the LLM's P9 closing (the fallback narrative replaced three rejected SAR drafts)"
        if last and t_new is not None and last[0] >= t_new and last[2]["type"] in _RETRY_EVENTS:
            m = re.match(r"^(P\d+)", str(last[2].get("text") or ""))  # 'P9-retry: ...' names its phase too
            code = _fold(last[2].get("phase")) or (m.group(1) if m else "")
            active = code if code in PIPELINE_CODES else active
            waiting = f"the LLM: {_RETRY_EVENTS[last[2]['type']]} ({last[2]['text']})"
            kind = "sar" if active == "P8" else "main"
    elif state == "running":
        active, waiting = "P0", "the intake"
    if state == "failed":  # the agent's own message only: a validation error can echo model text that names a phase
        err = str(getattr(view, "error", "") or "")
        m = re.match(r"""GraphUnavailable\(['"]graph unavailable during (P\d+)\b""", err) or re.match(r"""^\w+\(['"](P\d+)(?:-\w+)?: """, err)
        if m and m.group(1) in PIPELINE_CODES:
            active, waiting, kind, basis = m.group(1), "", "", "error"
    ai = PIPELINE_CODES.index(active) if active in PIPELINE_CODES else None
    stages = []
    for i, s in enumerate(PIPELINE):
        n = rows.get(s["code"], 0)
        if s["code"] == "S0":
            st_ = "offline"
        elif finished or ai is None:
            st_ = "done" if n else ("skipped" if finished else "pending")
        elif i == ai:
            st_ = "active" if state == "running" else "stopped"
        else:  # P10 is written before the P9 row, so a stage after the active one can be done
            st_ = "done" if n else ("skipped" if i < ai else "pending")
        stages.append({**s, "state": st_, "rows": n} | ({"state_label": "stopped (inferred)"} if st_ == "stopped" and basis == "inferred" else {}))
    llm_rows = [c for c in calls if c.get("kind") == "llm" and c.get("model") and str(c.get("ts") or "") >= since]
    own = {"main": [str(c["model"]) for c in llm_rows if _fold(c.get("phase")) != "P8"], "sar": [str(c["model"]) for c in llm_rows if _fold(c.get("phase")) == "P8"]}
    run = {"main": run_model, "sar": run_sar_model}

    def pick(k: str) -> tuple[str, str]:
        rows_of = " (P8 rows)" if k == "sar" else ""
        if own[k]:
            return own[k][-1], f"this case's calls.jsonl{rows_of}"
        return (run[k], f"another case's calls.jsonl in this run{rows_of}") if run[k] else ("", "")

    model, model_source = ("", "")
    if kind in ("main", "sar"):
        model, model_source = pick(kind)
    elif kind == "either":
        a, b = pick("main"), pick("sar")
        model, model_source = (a[0], "calls.jsonl: the SAR-draft and closing models agree") if a[0] and a[0] == b[0] else (
            "", "not known yet: the SAR draft and the closing can use different models")
    return {"stages": stages, "active": active if state == "running" else None, "stopped_at": active if state in ("incomplete", "failed") else None,
            "stopped_basis": basis, "waiting": waiting, "llm": bool(kind), "model": model, "model_source": model_source,
            "newest": {k: newest.get(k) for k in ("kind", "phase", "name", "t", "model", "latency_s")} if newest else None}


def compact_params(params: Any, limit: int = 160) -> str:
    """Tool params on one line: embedding vectors become '<N-d vector>', and the line is cut at `limit` characters."""
    if params in (None, "", {}, []):
        return ""
    text = json.dumps(_clean_params(params), default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def call_line(c: dict, case_id: str = "") -> str:
    """One calls.jsonl row as one plain-text console line, for st.code (nothing in it is interpreted): case, t, phase,
    then the model and its tokens as recorded, or the tool with its compacted params and caller; latency; a failure."""
    t = _float(c.get("t"))
    head = " ".join(x for x in (case_id, "t+     ?" if t is None else f"t+{t:6.1f}s", f"[{c.get('phase') or '?'}]") if x)
    if c.get("kind") == "llm":
        cached, written = _int(c.get("cache_read_tokens")), _int(c.get("cache_write_tokens"))
        body = f"LLM   {c.get('model') or 'model not recorded'} · {_int(c.get('input_tokens')):,} in + {_int(c.get('output_tokens')):,} out" + (
            f" + {cached:,} cached" if cached else "") + (f" + {written:,} cache-write" if written else "") + " tok"
    else:
        body = f"TOOL  {c.get('name') or '?'} {compact_params(c.get('params'))}".rstrip() + f" · {c.get('caller') or 'caller not recorded'}"
    lat = _float(c.get("latency_s"))
    tail = "" if lat is None else f" · {lat:.2f} s"
    if c.get("ok") is False:
        tail += " · FAILED" + (f": {str(c.get('error'))[:160]}" if c.get("error") else "")
    return f"{head}  {body}{tail}"


def run_console(views: list, limit: int = 40) -> list[str]:
    """The calls of a run's cases as console lines, newest last: cases in the order of their newest call (the case running
    now comes last), each case's calls in file order (calls.jsonl is append-only, so that is the order they finished, across
    --resume attempts too) with a marker where a rerun's t restarts, and one line per case with a parsed answer."""
    def newest(v: Any) -> tuple[str, str]:
        return max((str(c.get("ts")) for c in getattr(v, "calls", None) or [] if c.get("ts")), default="~"), str(v.case_id)

    lines: list[str] = []
    for v in sorted(views, key=newest):
        prev = None
        for c in getattr(v, "calls", None) or []:
            t = _float(c.get("t"))
            if prev is not None and t is not None and t < prev - 1.0:
                lines.append(f"{v.case_id} -- new attempt (a rerun appended to the same files; t restarts at 0) --")
            prev = t if t is not None else prev
            lines.append(call_line(c, v.case_id))
        if getattr(v, "ok", False):
            case = v.answer.get("case") or {}
            lines.append(f"{v.case_id} answer.json · verdict {case.get('verdict') or '?'} · p {prob(case.get('fraud_probability'))}")
    return lines[-limit:]


def pipeline_strip(stages: list[dict], *, key: str) -> None:
    """The stages as a wrapping row of small bordered boxes: state icon and code, the label and the state in words. Static;
    the Case file's Decision path tab has the clickable inspector."""
    st = _st()
    with st.container(horizontal=True, gap="small", key=f"pipeline-{_slug(key)}"):
        for s in stages:
            color, icon = STAGE_STYLE.get(s["state"], ("gray", None))
            with st.container(border=True, width=124, gap=None, key=f"stage-{_slug(key)}-{s['code'].lower()}"):
                st.markdown(badge(s["code"], color, icon=icon))
                st.caption(md(f"{s['label']} · {s.get('state_label') or STAGE_LABELS.get(s['state'], s['state'])}"))


_REF_RE = re.compile(r"^(?:query:)?([A-Za-z_][\w]*)\s*(?:\((.*)\))?\s*$")


def _match_call(ref: str, tools: list[dict]) -> dict | None:
    m = _REF_RE.match(str(ref or "").strip())
    if not m:
        return None
    name, args = m.group(1), m.group(2) or ""
    values = [v.split("=", 1)[1].strip() for v in args.split(",") if "=" in v]
    named = [c for c in tools if _short(c.get("name")) == name]
    for c in named:
        blob = json.dumps(c.get("params"), default=str)
        if all(v in blob for v in values):
            return c
    return None if values else (named[0] if named else None)


def _reads_past(call: dict, opened: str) -> bool:
    if _short(call.get("name")).startswith("post_open_activity"):
        return True
    params = call.get("params") if isinstance(call.get("params"), dict) else {}
    return bool(opened) and any(isinstance(params.get(k), str) and params[k][:19] > opened[:19] for k in ("as_of", "to_ts"))


def attest_claims(view: Any) -> list[dict]:
    """Each evidence item with the calls.jsonl row behind it and four marks (what / evidence / how / when). Flags: agent
    case ids among the entity ids, reads past opened_at ('monitoring only'), and trigger claims that quote another trigger."""
    ans = view.answer if getattr(view, "state", "done") == "done" else None
    if not isinstance(ans, dict):
        return []
    meta = getattr(view, "meta", None) or {}
    opened = str(meta.get("opened_at") or "")
    trigger_text = str(meta.get("trigger_text") or "")
    tools = [c for c in getattr(view, "calls", None) or [] if c.get("kind", "tool") == "tool" and c.get("caller") != "persist"]
    fam_fraud, fam_legit = families(view)
    out = []
    for i, ev in enumerate((ans.get("case") or {}).get("evidence") or [], 1):
        if not isinstance(ev, dict):
            continue
        ref, claim = str(ev.get("ref") or ""), str(ev.get("claim") or "")
        ids = [str(x) for x in ev.get("entity_ids") or []]
        call = _match_call(ref, tools) if tools else None
        fam = family_of_evidence(ev)
        flags = []
        agent_ids = [x for x in ids if x.startswith("AC-")]
        if agent_ids:
            flags.append(f"cites agent case {', '.join(agent_ids)}: an earlier agent decision, not ground truth")
        if call and _reads_past(call, opened):
            flags.append("monitoring only: reads activity after opened_at")
        if ref == "trigger" and trigger_text and any(t not in trigger_text for t in re.findall(r"\b\d{7}\b", claim)):
            flags.append("quotes a transaction that is not in this case's trigger")
        if not getattr(view, "calls", None):
            when = "not recorded (no trace)"
        elif call:
            when = str(call.get("ts") or "")
        else:
            when = "no matching call in calls.jsonl" if ref.startswith("query:") else "not a graph read"
        out.append({
            "n": i, "claim": claim, "direction": "fraud" if fam in fam_fraud else "legitimate" if fam in fam_legit else "neutral", "family": fam,
            "source": ev.get("source", ""), "ref": ref, "entity_ids": ids, "call": _call_row(call) if call else None, "ts": str(call.get("ts") or "") if call else "",
            "what": claim, "evidence": ids, "how": f"{call.get('name')} {json.dumps(_clean_params(call.get('params')), default=str)}" if call else ref, "when": when,
            "flags": flags,
        })
    return out


_CITE_RE = re.compile(r"\b(?:AC-HH[FG]-\d{3}|CC-\d{3,5}|C\d{5}(?:-K\d)?|\d{7}|\d{4}-\d{2}-\d{2})\b|\$\s?[\d,]+(?:\.\d{2})?")


def _cite_tokens(text: str) -> set[str]:
    return {t.replace("$", "").replace(",", "").strip() if t.startswith("$") else t for t in _CITE_RE.findall(text or "")}


def sar_citations(answer: dict) -> list[dict]:
    """Each SAR sentence matched to evidence items by whole-word ids, amounts and dates. A sentence with no match is
    reported as 'no citation'; there is never a fallback to other ids."""
    narrative = str(((answer or {}).get("sar") or {}).get("narrative") or "").strip()
    if not narrative:
        return []
    evidence = [e for e in ((answer.get("case") or {}).get("evidence") or []) if isinstance(e, dict)]
    ev_tokens = [_cite_tokens(str(e.get("claim", ""))) | {str(x) for x in e.get("entity_ids") or []} for e in evidence]
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", narrative):
        toks = _cite_tokens(sentence)
        hits, matched = [], set()
        for i, et in enumerate(ev_tokens, 1):
            common = toks & et
            if common:
                hits.append(i)
                matched |= common
        out.append({"sentence": sentence, "evidence": hits, "tokens": sorted(matched), "status": "cited" if hits else "no citation"})
    return out


# SAR validator badges - delegated to rag.validate_sar (the single validator); no local copy of the rules.
SAR_CHECK_LABELS = [
    ("V00", "sar.file agrees with FILE_REPORT in final; reason cites a rule; negative SAR fields empty"),
    ("V01", "narrative is one paragraph"),
    ("V02", "6-12 sentences"),
    ("V03", "at least one ISO date and one amount"),
    ("V04", "activity_dates both appear in the narrative"),
    ("V05", "activity_dates = [first, last]"),
    ("V06", "total_amount_usd == exposure_usd and written in the narrative"),
    ("V07", "subjects present, include customer + card, named in narrative, resolve"),
    ("V08", "every id in the narrative resolves"),
    ("V09", "no 'see attached' / markdown"),
    ("V10", "internal case id named"),
    ("V11", "OFAC result and prior-report status stated"),
    ("V12", "no seeding tell (':00 seconds')"),
    ("V13", "no tables (a '|' inside a device profile is fine)"),
    ("V14", "a pending block is not described as done"),
    ("V15", "no gendered pronouns"),
    ("V16", "no time-zone claim"),
    ("V17", "every named id / device is a subject"),
    ("V18", "prior-report claim agrees with the evidence"),
]


SAR_IDS_DB = ROOT / "data" / "ids.duckdb"  # the id-only dataset, checked in for CI
_SAR_OPENABLE: dict[str, tuple[bool, str, float]] = {}  # "path|signature" -> (opens, where or why, checked at)
_SAR_EXISTS: dict[tuple[str, str, str], bool] = {}  # ("path|signature", kind, id) -> exists: a known id never reopens the file
_SAR_LOCK = threading.Lock()


class _ScopedResolver:
    """A rag.validate_sar Resolver over one DuckDB file that holds no connection between checks: it opens the file
    read-only only for an id it has not answered before, and sar_checks closes it right after, so the dashboard never keeps
    the file's lock (a read-write writer such as `make ids` can always open it)."""

    def __init__(self, path: Path, key: str) -> None:
        self.path, self.key, self.db, self.inner = path, key, None, None

    def exists(self, kind: str, ident: str) -> bool:
        k = (self.key, str(kind), str(ident))
        with _SAR_LOCK:
            if k not in _SAR_EXISTS:
                if self.inner is None:
                    self.db = importlib.import_module("agent.validator").open_db(self.path)
                    self.inner = importlib.import_module("engine.validator").FactsResolver(self.db)
                _SAR_EXISTS[k] = bool(self.inner.exists(kind, ident))
            return _SAR_EXISTS[k]

    def close(self) -> None:
        with _SAR_LOCK:
            con, self.db, self.inner = getattr(self.db, "con", None), None, None
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001 - a read-only connection that fails to close changes nothing on the page
                pass


def sar_resolver() -> tuple[Any | None, str]:
    """(resolver, where) for the SAR id checks: engine.validator.FactsResolver, the resolver P8 falls back to, over the
    id-only dataset data/ids.duckdb, else over the engine facts DB, opened read-only (agent.validator.open_db) only for the
    length of a check (_ScopedResolver). (None, why) when neither opens, e.g. on the Railway image (no data/, duckdb or
    engine code); the checks then resolve against the answer's own ids. Whether a file opens is kept per file signature; a
    failure is retried after 60 s."""
    reasons: list[str] = []
    absent = 0
    for path in dict.fromkeys((SAR_IDS_DB, _facts_db_path())):
        sig = _sig(path)
        if sig is None:
            reasons.append(f"{rel(path)} not here")
            absent += 1
            continue
        key = f"{path}|{sig}"
        hit = _SAR_OPENABLE.get(key)
        if hit is None or (not hit[0] and time.time() - hit[2] > 60):
            try:
                av, err = optional_import("agent.validator")
                ev, err2 = optional_import("engine.validator")
                if av is None or ev is None:
                    raise ImportError(err or err2)
                av.open_db(path).con.close()  # only whether it opens is kept
                hit = (True, f"ids checked against {rel(path)} (engine.validator.FactsResolver, read-only, opened per check)", time.time())
            except Exception as exc:  # noqa: BLE001 - no duckdb, no engine code or a locked file: the answer's ids are the fallback
                hit = (False, f"{rel(path)}: {type(exc).__name__}: {str(exc)[:120]}", time.time())
            _SAR_OPENABLE[key] = hit
        if hit[0]:
            return _ScopedResolver(path, key), hit[1]
        reasons.append(hit[1])
    lead = "no dataset in this deployment" if absent == len(reasons) else "the dataset could not be opened here"
    return None, f"ids checked against the answer's own lists only ({lead}: " + "; ".join(reasons) + ")"


def sar_checks(answer: dict, meta: dict | None = None, resolver: Any | None = None) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] for the SAR tab, one row per rag.validate_sar code V00-V18 (V19 needs the as-of baseline and runs only in P8), computed from the answer
    alone (plus the case pack's customer/card ids in `meta`). Ids resolve against sar_resolver() (the dataset, read-only)
    unless a `resolver` is given, and against the answer's own lists when no dataset is here; the run's P8 validator
    payload is merged in by the page. When sar.file is false only V00 applies."""
    from rag.validate_sar import validate_answer_sar

    meta = meta or {}
    if resolver is None:
        resolver = sar_resolver()[0]  # None: validate_answer_sar falls back to the answer's own ids
    try:
        errs = validate_answer_sar(answer, customer_id=meta.get("customer_id", ""), card_id=meta.get("card_id", ""), resolver=resolver)
    except Exception as exc:  # noqa: BLE001 - a half-written answer must still render
        return [("rag.validate_sar ran", False, str(exc)[:160])]
    finally:
        if isinstance(resolver, _ScopedResolver):
            resolver.close()
    filed = bool((answer.get("sar") or {}).get("file"))
    out: list[tuple[str, bool, str]] = []
    for code, label in SAR_CHECK_LABELS:
        if code != "V00" and not filed:
            continue
        hits = [e for e in errs if e.startswith(code)]
        out.append((label, not hits, "; ".join(h[len(code):].strip() for h in hits)))
    return out


def tg_usage(view: Any) -> dict:
    """What TigerGraph did in this case: reads by caller, the LLM's picks against max_tool_calls, P1 vector hits (closed and
    agent cases), grounding chunks, persist rows, the vector index status after persist, and whether any P3 adjustment
    names a graph algorithm."""
    calls = getattr(view, "calls", None) or []
    reads = [c for c in calls if c.get("kind", "tool") == "tool" and c.get("caller") != "persist"]
    by_caller: dict[str, list[str]] = {}
    for c in reads:
        by_caller.setdefault(str(c.get("caller") or "unrecorded"), []).append(_short(c.get("name")))
    p1, p3 = phase_payload(view, "P1"), phase_payload(view, "P3")
    hits = []
    for h in p1.get("similar_prior_cases") or []:
        if isinstance(h, dict):
            hits.append({k: h.get(k) for k in ("id", "kind", "outcome_or_verdict", "pattern", "distance", "overlap_reasons")})
        else:
            hits.append({"id": str(h), "kind": "agent" if str(h).startswith("AC-") else "closed"})
    persist = _persist_rows(view)
    sid = getattr(view, "source_id", "")
    algo = any(re.search(r"\b(wcc|degree|pagerank|ppr)\b", str(a[0] if isinstance(a, list | tuple) and a else a), re.I) for a in p3.get("adjustments") or [])
    return {
        "has_trace": bool(calls), "n_reads": len(reads), "reads_by_caller": by_caller, "llm_chosen": len(by_caller.get("llm", [])),
        "max_tool_calls": run_manifest(sid).get("max_tool_calls") if sid not in SUBMITTED else None,
        "vector_hits": hits, "agent_readbacks": [h for h in hits if h.get("kind") == "agent"],
        "grounding": [{"id": g.get("id"), "section": g.get("section")} for g in p1.get("grounding") or [] if isinstance(g, dict)],
        "persist": [{"name": c.get("name"), "ok": bool(c.get("ok")), "latency_s": _float(c.get("latency_s")), "error": str(c.get("error") or "")} for c in persist],
        "persist_ok": all(c.get("ok") for c in persist) if persist else None,
        "vector_index_after_persist": next((e for e in guardrail_events(view) if e["type"] == "vector_index"), None),
        "algo_effect": algo,
    }


# --------------------------------------------------------------------------- #
# Text safety and small formatters                                            #
# --------------------------------------------------------------------------- #
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|$~:!])")
_MD_BULLET = re.compile(r"(?m)^(\s*)([-+=])")
_MD_ORDERED = re.compile(r"(?m)^(\s*\d+)([.)])")


def md(text: object) -> str:
    """Any data value as markdown-safe text: backslash, backtick, * _ [ ] < > # | $ ~ : ! are escaped, so are leading list
    markers, and markdown images are reduced to their alt text. '$1 and $2' stays two amounts, never inline math."""
    if text is None:
        return ""
    s = _MD_IMAGE.sub(r"\1", str(text))
    s = _MD_SPECIAL.sub(r"\\\1", s)
    s = _MD_BULLET.sub(r"\1\\\2", s)
    return _MD_ORDERED.sub(r"\1\\\2", s)


def badge(text: object, color: str, icon: str | None = None) -> str:
    """':color-badge[text]' markdown with the text escaped by md() (']' and '\\' included), so data never breaks it."""
    return f":{color}-badge[{(icon + ' ') if icon else ''}{md(text)}]"


TABLE_ROW_PX = 33  # st.dataframe's row and header height at the theme's 15 px base font


def table_height(n: int, cap: int = 20) -> int | str:
    """st.dataframe height: "content" (every row, no blank band under the last) up to `cap` rows, else `cap` rows and a
    scrollbar."""
    return "content" if n <= cap else TABLE_ROW_PX * (cap + 1) + 2


def text_width(values: Any, floor: int = 120, cap: int = 480) -> int:
    """A st.dataframe column width in px that fits the longest value in the grid's 13 px face, between floor and cap."""
    longest = max((len(str(v)) for v in values), default=0)
    return int(min(cap, max(floor, longest * 7.6 + 24)))


def chip(text: object, color: str = "gray", *, icon: str | None = None, help: str | None = None, where: Any = None) -> None:
    """st.badge with data-safe text (st.badge escapes the brackets itself, so only the rest is escaped here)."""
    body = md(text).replace("\\[", "[").replace("\\]", "]")
    (where or _st()).badge(body, color=color, icon=icon, help=md(help) if help else None)


# TigerGraph's two official logo files, unmodified apart from the wordmark colour tigergraph.com's own stylesheet gives them:
# the header version (black "Graph", for light backgrounds) and the footer version (white "Graph", for dark ones).
TIGERGRAPH_LOGO = UI_DIR / "assets" / "tigergraph-logo.svg"
TIGERGRAPH_LOGO_DARK = UI_DIR / "assets" / "tigergraph-logo-dark.svg"
_CREDIT_CSS = "<style>[class*='st-key-tgcredit-'] [data-testid='stCaptionContainer'] p { margin: 0 !important; }</style>"


def tigergraph_logo() -> Path:
    """The official logo version for the current theme: white wordmark on a dark theme, black on a light (or unknown) one."""
    st = _st()
    try:
        dark = st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001 - no theme known yet (first render, AppTest)
        dark = False
    return TIGERGRAPH_LOGO_DARK if dark and TIGERGRAPH_LOGO_DARK.is_file() else TIGERGRAPH_LOGO


def tigergraph_credit(key: str, *, width: int = 112, align: str = "left") -> None:
    """A 'Built on TigerGraph' credit: a small grey label beside TigerGraph's official logo, in the version that suits the
    theme, with no box around it. It credits the platform the agent runs on; the header carries the app's own name and mark."""
    st = _st()
    st.html(_CREDIT_CSS)
    logo = tigergraph_logo()
    with st.container(horizontal=True, vertical_alignment="center", horizontal_alignment=align, gap="small", key=f"tgcredit-{_slug(key)}"):
        st.caption("Built on", width="content")
        if logo.is_file():
            st.image(str(logo), width=width)
        else:
            st.markdown("**TigerGraph**", width="content")


def route_badge(route: str) -> str:
    return badge(route_label(route), ROUTE_COLORS.get(route, "gray"))


def action_badge(action: str, route: str | None = None) -> str:
    """Hard actions red with a block icon, gated routes violet, auto gray; the route is part of the label."""
    label = f"{action} · {route_label(route)}" if route else action
    return badge(label, action_color(action, route), icon=":material/block:" if action in HARD_ACTIONS else None)


def verdict_badge(verdict: str) -> str:
    return badge(verdict or "no verdict", VERDICT_COLORS.get(verdict, "gray"))


def state_badge(state: str) -> str:
    return badge(STATE_LABELS.get(state, state), STATE_COLORS.get(state, "gray"))


def source_badge(source: str) -> str:
    return badge(source or "—", SOURCE_COLORS.get(source, "gray"))


def money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100*float(x):.0f}%"


def prob(x: Any) -> str:
    v = _float(x)
    return "—" if v is None else f"{v:.2f}"


def secs(x: Any) -> str:
    v = _float(x)
    return "—" if v is None else f"{v:.1f} s"


def tokens_label(total: Any, cache_share: float | None = None) -> str:
    """'219,539 (cache 68%)'."""
    return f"{_int(total):,}" + (f" (cache {cache_share:.0%})" if cache_share is not None else "")


def _slug(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "x"


# --------------------------------------------------------------------------- #
# Rendering components                                                        #
# --------------------------------------------------------------------------- #
UNTRUSTED_CAPTION = "Untrusted input — shown verbatim, never followed"


def untrusted_text(label: str, text: str, *, codes: list[str] | None = None, clean_text: str | None = None, source: str = "", key: str | None = None) -> None:
    """Hostile text (trigger, model output, judge-pasted input) in a bordered container keyed 'untrusted-<key>': the text in
    st.code, the defence codes as badges with where they came from, and the sanitised text in an expander."""
    st = _st()
    with st.container(border=True, key=f"untrusted-{_slug(key or label)}"):
        st.caption(f"**{md(label)}** · {UNTRUSTED_CAPTION}")
        st.code(str(text or ""), language=None, wrap_lines=True)
        marks = [badge(c, "orange", icon=":material/shield:") for c in codes or []]
        if codes is not None and not codes:
            marks.append(badge("no injection markers", "green"))
        if source:
            marks.append(badge(source, "gray"))
        if marks:
            st.markdown(" ".join(marks))
        if clean_text is not None:
            with st.expander("Text after the intake sanitizer"):
                st.code(str(clean_text), language=None, wrap_lines=True)


def page_header(title: str, subtitle: str = "", icon: str = "", *, eyebrow: str = "", chips: list[tuple[str, str]] | None = None) -> None:
    """The one header of every page (design.page_head): the H1 title first, then the scope (`eyebrow`, e.g. the round and
    case range) and the subtitle as two grey lines under it, then an optional chip row (text, colour). Nothing sits above
    the title. `icon` is accepted for the old positional call page_header(title, subtitle, icon) and no longer drawn."""
    st = _st()
    design.html_block(design.page_head(title, eyebrow, subtitle))
    if chips:
        with st.container(horizontal=True, gap="small"):
            for text, color in chips:
                chip(text, color)


def measured(label: str, value: object, *, source: str, as_of: str, how: str = "", what: str = "", container: Any = None, delta: Any = None, delta_color: str = "off") -> None:
    """The only way a headline number reaches the screen: a bordered st.metric whose tooltip says what it is, the file it
    comes from, when that file was written and how the number is computed. None shows 'not measured here'."""
    lines = [f"**What:** {md(what or label)}", f"**Source:** {md(source)}", f"**As of:** {md(as_of)}"]
    if how:
        lines.append(f"**How:** {md(how)}")
    (container or _st()).metric(md(label), "not measured here" if value is None else value, delta=delta, delta_color=delta_color, help="  \n".join(lines), border=True)


def proof_chip(label: str, value: object, *, source: str, as_of: str, page_key: str | None = None, case: str | None = None) -> None:
    """A compact bordered proof card: the measured value, its source and time in the tooltip, and a link to its page."""
    st = _st()
    with st.container(border=True, key=f"proof-{_slug(label)}"):
        st.caption(md(label))
        st.markdown(f"**{md('not measured here' if value is None else value)}**", help=f"**Source:** {md(source)}  \n**As of:** {md(as_of)}")
        if page_key:
            page_link(page_key, "Open", icon=":material/arrow_forward:", case=case)


_GRAPH_STATE: dict[str, str] = {"error": "", "since": ""}


def dependency_banner(name: str, error: str, *, since: str | None = None, retry_key: str | None = None) -> bool:
    """A plain-language dependency error, e.g. 'TigerGraph unreachable since 06:31: <error>; showing the answer-derived
    view'. With retry_key it adds a Retry button and returns True when it is clicked."""
    st = _st()
    st.warning(md(f"{name} unreachable since {since}: {error}" if since else f"{name} unavailable: {error}"), icon=":material/cloud_off:")
    return bool(retry_key) and st.button("Retry", key=retry_key, icon=":material/refresh:")


def optional_import(name: str) -> tuple[ModuleType | None, str | None]:
    """(module, None), or (None, reason) when it cannot be imported here, e.g. 'engine/ not shipped in this deployment'."""
    try:
        return importlib.import_module(name), None
    except (ImportError, OSError) as exc:  # ModuleNotFoundError is an ImportError
        top = name.split(".")[0]
        missing = getattr(exc, "name", None) or ""
        if missing and missing.split(".")[0] == top:
            return None, f"{top}/ not shipped in this deployment" if missing == top else f"{missing} not shipped in this deployment"
        if missing:
            return None, f"{name} needs {missing}, which is not installed in this deployment"
        return None, f"{name} unavailable here: {type(exc).__name__}: {exc}"


def _absent_modules() -> list[str]:
    """Modules the deeper sections need that this deployment lacks. A submodule is checked, not the package: the image
    can ship engine/out/*.json (a namespace package) or agent/security.py without the code behind the other sections."""
    out = []
    for name in ("engine.detectors", "schema_engine.drift", "agent.bench", "duckdb"):
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
        except (ImportError, ValueError):  # a missing parent package, or sys.modules[name] = None (blocked)
            out.append(name)
    return out


def _agreement_chips(view: CaseView) -> list[str]:
    if view.source_id in SUBMITTED:
        runs = view.promoted_from
        return [badge(f"identical to {', '.join(runs)}", "green")] if runs else [badge("no run answer identical to this file", "gray")]
    sub = load_promoted_answer(view.case_id)
    if sub is None:
        return [badge("no submitted answer to compare", "gray")]
    ag = agreement(view.answer, sub)
    return [badge(f"{ag['cls']} vs submitted", AGREEMENT_COLORS.get(ag["cls"], "gray"))]


_STATE_SENTENCES = {
    "running": "running: no answer yet; the decision appears when answer.json is written",
    "incomplete": "incomplete: the trace stopped before an answer was written",
    "failed": "failed: the error below is verbatim from phases.jsonl",
    "unreadable": "answer.json unreadable: the error below is verbatim",
    "not_run": "not run in this source",
}


def decision_card(view: CaseView, inbox: list[dict], compact: bool = False, *, key: str | None = None) -> None:
    """The shared decision summary (Case file, Graph, Approvals) in a bordered container keyed 'decision-<verdict>'.
    A case without a parsed answer shows its state and error only, never a verdict. Pass `key` to render two cards on one page."""
    st = _st()
    suffix = f"-{_slug(key)}" if key else ""
    ans = view.answer if view.ok else None
    if not ans:
        with st.container(border=True, key=f"decision-{view.state}{suffix}"):
            st.markdown(f"{state_badge(view.state)} **{md(view.case_id)}** · {md(_STATE_SENTENCES.get(view.state, view.state))}")
            if view.error:
                st.code(view.error, language=None, wrap_lines=True)
            bits = ([f"last phase {view.last_phase}"] if view.last_phase else []) + ([f"last write {secs(view.last_write_age_s)} ago"] if view.last_write_age_s is not None else [])
            if bits:
                st.caption(md(" · ".join(bits)))
        return
    case = ans.get("case") or {}
    verdict = str(case.get("verdict") or "")
    states = action_states(view, inbox)
    events = guardrail_events(view)
    saf = safety({view.case_id: ans})
    sar = ans.get("sar") or {}
    p_engine, p_model, p_final = _float(phase_payload(view, "P3").get("p_engine")), _float(phase_payload(view, "P4").get("fraud_probability")), _float(case.get("fraud_probability"))
    clamped = any(e["type"] == "clamp" for e in events)
    safe = not saf["hard_on_non_fraud"]
    safety_chips = [badge("no hard action on a non-fraud verdict", "green") if safe else
                    badge("hard action on a non-fraud verdict: " + ", ".join(saf["hard_on_non_fraud"][0][1]), "red", icon=":material/block:")]
    if verdict == "uncertain":
        safety_chips.append(badge("escalated to analyst", "green") if not saf["uncertain_not_escalated"] else badge("uncertain, not escalated", "red"))
    sar_line = badge("SAR filed", "red", icon=":material/block:") if sar.get("file") else badge("SAR not filed", "gray")
    if compact:
        parts = [verdict_badge(verdict), badge(f"p {prob(p_final)}", "gray"), *[action_badge(s["action"], s["route"]) for s in states], sar_line, *safety_chips, *_agreement_chips(view)]
        st.markdown(" ".join(parts))
        return
    with st.container(border=True, key=f"decision-{verdict or 'none'}{suffix}"):
        c1, c2, c3 = st.columns([1.1, 1.6, 1.3])
        with c1:
            st.markdown(f"#### {md(verdict_headline(verdict))}")
            path = " → ".join(f"{name} {prob(v)}" for name, v in (("P3 engine", p_engine), ("P4 model", p_model), ("final", p_final)) if v is not None)
            st.markdown(f"p(fraud) **{prob(p_final)}**" + (f" · {md(path)}" if path else "") + (" " + badge("clamped ±0.10", "orange") if clamped else ""))
            st.markdown(" ".join([badge(f"status {case.get('status') or '—'}", STATUS_COLORS.get(case.get("status"), "gray")),
                                  badge(f"pattern {case.get('pattern') or 'none'}", "gray"),
                                  badge(f"exposure {money(case.get('exposure_usd'))}", "gray"),
                                  badge(f"{len(case.get('affected_txn_ids') or [])} affected txn", "gray")]))
        with c2:
            for s in states:
                st.markdown(f"{action_badge(s['action'], s['route'])} {badge(s['state'], s['color'])}")
            diff = actions_diff_routes((ans.get("next_best_actions") or {}).get("initial") or [], _final(ans))
            strip = [f"+{a} {r}" for a, r in diff["added"]] + [f"−{a} {r}" for a, r in diff["removed"]] + [f"{a} {r1}→{r2}" for a, r1, r2 in diff["route_changed"]]
            st.caption(md("initial → final: " + (", ".join(strip) if strip else "unchanged")))
            for req in ans.get("evidence_requests") or []:
                if isinstance(req, dict):
                    st.markdown(f"{badge('SIMULATED', 'gray')} {md(req.get('type', 'request'))}: {md(req.get('assumed_response', ''))}")
        with c3:
            st.markdown(" ".join(safety_chips))
            if ans.get("stop_reason"):
                st.markdown(f"> {md(ans['stop_reason'])}")
            st.markdown(f"{sar_line} {md(sar.get('reason') or '')}")
            st.markdown(" ".join(_agreement_chips(view)))


def inject_css() -> None:
    """The console's stylesheet on top of the config.toml theme: ui/design.py injects the --tc-* tokens for the active
    theme and ui/assets/console.css (the shell band, the context strip, figures, tags, and the keyed container rules
    .st-key-ctxbar, casebar, case-brief, untrusted-*, decision-*, proof-*, strip-*, stage-*). Called once per run by the
    router, or by page_setup when a page runs alone."""
    design.inject()


# --------------------------------------------------------------------------- #
# Proof and honesty: measured artifacts, known limits, README facts           #
# --------------------------------------------------------------------------- #
SNAPSHOT_PATH = ROOT / "engine" / "out" / "proof_snapshot.json"
SNAPSHOT_SECTIONS = ("attacks", "intake_scans", "drift", "dq", "gsql", "variant", "containment_plans", "c3_built", "facts", "scorecard")
_PROOF_FILES = {"backtest": "engine/out/detector_backtest.json", "containment_summary": "engine/out/containment_summary.json", "schema_approval": "data/schema_approvals.json"}


def proof(name: str) -> dict:
    """A measured artifact, read-only: {data, mode ('file' | 'snapshot' | 'missing'), source, as_of, generated_at, git_sha,
    error}. 'backtest', 'containment_summary' and 'schema_approval' are files (falling back to the snapshot); the other
    names are sections of engine/out/proof_snapshot.json. Never recomputes and never writes; cached on mtime."""
    out: dict = {"name": name, "data": None, "mode": "missing", "source": "", "as_of": "", "generated_at": "", "git_sha": "", "error": ""}
    if name not in _PROOF_FILES and name not in SNAPSHOT_SECTIONS:
        out["error"] = f"unknown proof section {name!r}"
        return out
    if name in _PROOF_FILES:
        p = ROOT / _PROOF_FILES[name]
        data, err = _read_json(p)
        if data is not None and not err:
            gen = data.get("generated_at", "") if isinstance(data, dict) else ""
            return {**out, "data": data, "mode": "file", "source": _PROOF_FILES[name], "as_of": as_of(p), "generated_at": str(gen or "")}
        out["error"] = err or f"{_PROOF_FILES[name]} not in this deployment"
    snap, serr = _read_json(SNAPSHOT_PATH)
    if isinstance(snap, dict):
        sections = snap.get("sections") if isinstance(snap.get("sections"), dict) else snap
        if sections.get(name) is not None:
            return {**out, "data": sections[name], "mode": "snapshot", "source": rel(SNAPSHOT_PATH), "as_of": as_of(SNAPSHOT_PATH),
                    "generated_at": str(snap.get("generated_at") or ""), "git_sha": str(snap.get("git_sha") or "")[:8], "error": ""}
        section_err = (snap.get("errors") or {}).get(name) if isinstance(snap.get("errors"), dict) else None
        out["error"] = "; ".join(x for x in (out["error"], f"snapshot: {section_err}" if section_err else f"not in {rel(SNAPSHOT_PATH)}") if x)
    else:
        out["error"] = "; ".join(x for x in (out["error"], serr or f"{rel(SNAPSHOT_PATH)} not in this deployment") if x)
    return out


SCORECARD_SOURCES = "{policy} (engine block) · engine/detectors.py (DetectorHit p_floor, hit_from_scan order) · agent/security.py (clamp_to_engine)"


def scorecard_rules() -> dict:
    """The scorecard's numbers as the code holds them, in proof() shape with mode 'code' | 'snapshot' | 'missing': read now
    (the policy file afresh on every call) with ops.proof_snapshot.sec_scorecard where policy/ and engine/ exist (local,
    CI), else the snapshot's 'scorecard' section (the Railway image ships neither). Never typed in, never written."""
    mod, err = optional_import("ops.proof_snapshot")
    if mod is not None and hasattr(mod, "sec_scorecard"):
        try:
            data, partial = mod.sec_scorecard()
            policy = str(data.get("policy_file") or "policy/fraud_policy.yaml")  # the file the values came from (ENGINE_POLICY_YAML)
            return {"name": "scorecard", "data": data, "mode": "code", "source": SCORECARD_SOURCES.format(policy=policy), "as_of": as_of(policy),
                    "generated_at": "", "git_sha": "", "error": "; ".join(partial)}
        except Exception as exc:  # noqa: BLE001 - engine/, policy/ or pyyaml absent here: the snapshot is the fallback
            err = f"{type(exc).__name__}: {exc}"
    snap = proof("scorecard")
    if snap["data"] is None and err:
        snap["error"] = "; ".join(x for x in (f"not readable from the code here ({err})", snap["error"]) if x)
    return snap


def readme_facts() -> dict:
    """Every runs/<name> that README.md cites (with exists), and its 'Results' section verbatim."""
    p = ROOT / "README.md"
    text = _read_text(p)
    out: dict = {"path": "README.md", "exists": p.exists(), "as_of": as_of(p), "cited_runs": [], "results": []}
    if not text:
        return out
    lines = text.splitlines()
    cited: dict[str, list[int]] = {}
    for n, line in enumerate(lines, 1):
        for name in re.findall(r"runs/([A-Za-z0-9][\w-]*(?:\.[\w-]+)*)", line):
            cited.setdefault(name, []).append(n)
    out["cited_runs"] = [{"run": r, "path": f"runs/{r}", "exists": (runs_root() / r).is_dir(), "lines": ns} for r, ns in sorted(cited.items())]
    level = None
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and level is not None and len(m.group(1)) <= level:
            break
        if m and level is None and m.group(2).strip().lower().startswith("results"):
            level = len(m.group(1))
        if level is not None:
            out["results"].append(line)
    return out


def known_limits() -> list[dict]:
    """Limits computed from files today, each {title, detail, source, as_of}. Nothing here is typed in by hand."""
    out: list[dict] = []

    def add(title: str, detail: str, source: str, when: str) -> None:
        out.append({"title": title, "detail": detail, "source": source, "as_of": when})

    bt = proof("backtest")
    data = bt["data"] if isinstance(bt["data"], dict) else {}
    for det in data.get("detectors") or []:
        custs = det.get("legitimate_customers_acted_against") or []
        if custs:
            add(f"{det.get('detector_id')} acts against cleared customer {', '.join(custs)} ({', '.join(det.get('cleared_cases_matched') or [])})",
                f"{det.get('name')}: {det.get('false_positives_on_cleared_cases')} cleared closed case(s) inside its match set", bt["source"], bt["as_of"])
    proposals = data.get("self_growing_proposals") or []
    if proposals:
        shapes = {(d.get("cards_matched"), d.get("txns_matched")): str(d.get("detector_id", "")).split("_")[0] for d in data.get("detectors") or []}
        replays = [f"{p.get('proposal_id')} = {shapes[(p.get('cards_matched'), p.get('txns_matched'))]}" for p in proposals if (p.get("cards_matched"), p.get("txns_matched")) in shapes]
        approved = sum(1 for p in proposals if p.get("approval_status") == "approved")
        add(f"C2: {len(proposals)} self-growing proposals, {approved} approved", ("they match the same cards and transactions as " + ", ".join(replays)) if replays else "no proposal has been approved by a fraud manager",
            bt["source"], bt["as_of"])
    submitted = {p.stem: _read_json(p)[0] for p in sorted(cases_dir().glob("HHF-*.json"))}
    if submitted:
        mix = Counter(str(((a or {}).get("case") or {}).get("verdict")) for a in submitted.values() if isinstance(a, dict))
        if mix.get("legitimate", 0) != BRIEF_LEGITIMATE:
            add(f"Verdict mix: {mix.get('legitimate', 0)} legitimate vs the brief's {BRIEF_LEGITIMATE}",
                f"{mix.get('fraud', 0)} fraud · {mix.get('legitimate', 0)} legitimate · {mix.get('uncertain', 0)} uncertain in the submitted answers",
                "cases/HHF-*.json", max((as_of(p) for p in cases_dir().glob("HHF-*.json")), default="missing"))
    det_mod, why = optional_import("engine.detectors")
    if det_mod is not None:
        if not hasattr(det_mod, "compile_policy_rule_detector"):
            add("C3 not built", "engine.detectors has no compile_policy_rule_detector", "engine/detectors.py", as_of("engine/detectors.py"))
    else:
        c3 = proof("c3_built")
        if c3["data"] is False:
            add("C3 not built", f"snapshot: compile_policy_rule_detector missing ({why})", c3["source"], c3["as_of"])
    sa = proof("schema_approval")
    for mid, rec in (sa["data"] or {}).items() if isinstance(sa["data"], dict) else []:
        if isinstance(rec, dict) and rec.get("status") != "approved":
            jan = sum(int(d.get("cards_matched_january") or 0) for d in data.get("detectors") or [])
            add(f"Schema migration {mid} is {rec.get('status')}", f"January 2017 data is already in the engine facts: the detector backtest matches {jan} January cards", sa["source"], sa["as_of"])
    ds = approvals_db.directory_status()
    if not ds["n"]:
        add("No approver principals, so approvals fail closed", f"{rel(ds['path'])} " + ("unreadable: " + ds["error"] if ds["error"] else "absent"), rel(ds["path"]), as_of(ds["path"]))
    srcs = [s for s in sources() if s.kind == "run" and s.mode == "live"]
    dirty = [s.id for s in srcs if s.git_dirty]
    if dirty:
        add("Live runs on a dirty git tree", f"git_dirty=true in {', '.join(dirty)}", "runs/*/run_manifest.json", _now())
    for s in srcs:
        if s.model_note:
            add(f"{s.id}: model mismatch", s.model_note, f"runs/{s.id}/run_manifest.json", as_of(s.dir / "run_manifest.json"))
    final_live = [s for s in srcs if s.round != "round1" and s.first_started and s.finished]
    for i, a in enumerate(final_live):
        for b in final_live[i + 1:]:
            if a.first_started <= b.finished and b.first_started <= a.finished:
                add(f"Overlapping live runs {a.id} and {b.id}", f"{a.first_started}–{a.finished} and {b.first_started}–{b.finished} both wrote AC-HHF-* write-backs to FraudGraph",
                    "runs/*/run_manifest.json", _now())
    if not (ROOT / "data" / "raw" / "round2" / "grade.py").exists():
        add("Accuracy against the answer key is unknown", "data/raw/round2/grade.py is absent", "data/raw/round2/grade.py", "missing")
    engine_py = list((ROOT / "engine").glob("*.py"))
    if engine_py and not any("analyst_feedback" in _read_text(p) or "list_feedback" in _read_text(p) for p in engine_py):
        add("Analyst feedback is not used by the engine", "no engine/*.py module reads analyst_feedback", "engine/*.py", _now())
    final_runs = [s for s in sources() if s.kind == "run" and s.round != "round1"]
    if final_runs:
        algo = [f"{s.id}/{cid}" for s in final_runs for cid in s.cases if tg_usage(case_view(s.id, cid))["algo_effect"]]
        if not algo:
            add("No HHF P3 adjustment cites a graph algorithm", f"no wcc / degree / pagerank / ppr adjustment in {', '.join(s.id for s in final_runs)}", "runs/*/HHF-*/phases.jsonl", _now())
    rf = readme_facts()
    missing = [c["path"] for c in rf["cited_runs"] if not c["exists"]]
    if missing:
        add("README cites a missing run folder", ", ".join(missing), "README.md", rf["as_of"])
    return out


# --------------------------------------------------------------------------- #
# Pages, navigation and the context bar                                       #
# --------------------------------------------------------------------------- #
PAGES: dict[str, dict[str, str]] = {
    "overview": {"file": "pages/0_overview.py", "title": "Overview", "icon": ":material/dashboard:", "url_path": "overview", "section": ""},
    "case": {"file": "pages/2_case.py", "title": "Case file", "icon": ":material/folder_open:", "url_path": "case", "section": ""},
    "graph": {"file": "pages/4_graph.py", "title": "Graph", "icon": ":material/hub:", "url_path": "graph", "section": ""},
    "approvals": {"file": "pages/3_approvals.py", "title": "Approvals", "icon": ":material/gavel:", "url_path": "approvals", "section": ""},
    "detectors": {"file": "pages/8_detectors.py", "title": "Detectors & containment", "icon": ":material/radar:", "url_path": "detectors", "section": "Proof"},
    "security": {"file": "pages/9_security.py", "title": "Security & drills", "icon": ":material/shield:", "url_path": "security", "section": "Proof"},
    "data": {"file": "pages/6_schema.py", "title": "Data quality & schema", "icon": ":material/rule:", "url_path": "data", "section": "Proof"},
    "runs": {"file": "pages/5_runs.py", "title": "Runs & consistency", "icon": ":material/monitoring:", "url_path": "runs", "section": "Proof"},
    "console": {"file": "pages/1_queue.py", "title": "Run console", "icon": ":material/play_circle:", "url_path": "console", "section": "System"},
    "system": {"file": "pages/7_architecture.py", "title": "How it works", "icon": ":material/account_tree:", "url_path": "system", "section": "System"},
    "live_architecture": {"file": "pages/10_live_architecture.py", "title": "Live architecture", "icon": ":material/animation:", "url_path": "live-architecture", "section": "System"},
}
CASE_PAGES = {"case", "graph", "live_architecture"}  # the pages that show the Case picker
PAGE_ICONS = {k: v["icon"] for k, v in PAGES.items()} | {
    "app": ":material/verified_user:", "queue": PAGES["console"]["icon"], "schema": PAGES["data"]["icon"], "architecture": PAGES["system"]["icon"]}


def _page_path(page_key: str) -> Path:
    return UI_DIR / PAGES[page_key]["file"]


def _default_page_key() -> str:
    """'overview', or the first page whose file exists when it is missing (a partial deployment)."""
    return next((k for k in PAGES if _page_path(k).is_file()), "overview")


def _pending_final_round() -> int:
    try:
        return sum(1 for r in approvals_db.rows_ro("pending", approvals_db_path()) if str(r.get("case_id", "")).startswith("HHF-"))
    except sqlite3.Error:
        return 0


def nav_pages() -> dict[str, list]:
    """{'': [overview, case, graph, approvals], 'Proof': [...], 'System': [...]} as st.Page objects. A page file that is
    not on disk is left out. Run console stays listed on a read-only deployment, where it replays recorded cases and starts
    nothing; Approvals shows its Final Round pending count."""
    st = _st()
    default_key = _default_page_key()
    pending = _pending_final_round()
    sections: dict[str, list] = {"": [], "Proof": [], "System": []}
    for key, meta in PAGES.items():
        path = _page_path(key)
        if not path.is_file():
            continue
        title = f"{meta['title']} ({pending})" if key == "approvals" and pending else meta["title"]
        sections[meta["section"]].append(st.Page(path, title=title, icon=meta["icon"], url_path=meta["url_path"], default=key == default_key))
    return {k: v for k, v in sections.items() if v}


def page_key_for(page: Any) -> str:
    """The PAGES key of the page st.navigation picked ('' is the default page)."""
    url = str(getattr(page, "url_path", "") or "")
    if url in PAGES:
        return url
    if not url:
        return _default_page_key()
    title = str(getattr(page, "title", ""))
    return next((k for k, m in PAGES.items() if title.startswith(m["title"])), "overview")


@dataclass
class Ctx:
    page_key: str
    source_id: str | None
    source: Source | None
    case_id: str | None
    mode: str
    mode_reason: str
    read_only: bool
    session: Any = None
    banners: list[str] = field(default_factory=list)


def _default_case(pack_ids: list[str]) -> str | None:
    return "HHF-001" if "HHF-001" in pack_ids else (pack_ids[0] if pack_ids else None)


def _seed(widget_key: str, options: list[str], mem_key: str, legacy_key: str, default) -> str | None:
    """Prepare a picker before it renders. A URL value that differs from the last one this session wrote wins (links,
    pasted URLs, goto()); a widget without a value is seeded from the URL, then the legacy mirror, then the default.
    Returns an invalid URL value so the caller can say it was ignored."""
    st = _st()
    url = st.query_params.get(widget_key)
    if url in options and url != st.session_state.get(mem_key):
        st.session_state[widget_key] = url
        return None
    if st.session_state.get(widget_key) not in options:
        legacy = st.session_state.get(legacy_key)
        st.session_state[widget_key] = url if url in options else legacy if legacy in options else default()
    return url if url and url not in options else None


def _write_url(key: str, value: str | None, mem_key: str) -> None:
    st = _st()
    if value and st.query_params.get(key) != value:
        st.query_params[key] = value
    st.session_state[mem_key] = value


def _provenance_line(src: Source | None, mode: str, reason: str, where: Any) -> None:
    """One plain phrase in the context strip for what this console is doing (reading the live graph or files only,
    read-only or not), kept apart from the source picker's label, which already says what the selected source is (live
    or mock run, model, answered count). Chips appear only for what needs attention: a model mismatch, uncommitted code,
    failed or running cases. The run manifest's details (git sha, run_mode, windows) stay in the Provenance popover."""
    console = ("<b>Live</b> · this console queries TigerGraph" if mode == "live"
               else f"<b>Replay</b> · recorded files, TigerGraph not queried ({design.esc(reason)})")
    where.html(f'<span class="tc-mode">{console}{" · read-only" if read_only() else ""}</span>')
    if src is None:
        return
    if src.kind != "submitted":
        if src.model_note:
            chip("model mismatch", "orange", icon=":material/warning:", where=where, help=src.model_note)
        if src.git_dirty:
            chip("uncommitted changes", "orange", where=where, help="git_dirty=true in run_manifest.json: the run used code that was not committed")
    if src.n_running or src.n_failed or src.n_unreadable:
        alarm = " · ".join(x for x in (f"{src.n_running} running" if src.n_running else "", f"{src.n_failed} failed" if src.n_failed else "",
                                        f"{src.n_unreadable} unreadable" if src.n_unreadable else "") if x)
        chip(alarm, "red" if src.n_failed or src.n_unreadable else "yellow", where=where)


def _banners(src: Source | None, invalid: list[str]) -> list[str]:
    st = _st()
    shown: list[str] = []
    if src is not None and src.round != "round1":
        notice = final_round_pack()[1]
        if notice:
            shown.append(notice)
            (st.warning if notice.startswith(("Final Round case pack unavailable", PACK_NOTICE_IDS_ONLY)) else st.caption)(md(notice))
    if src is not None and src.manifest_error:
        shown.append(f"run_manifest.json unreadable: {src.manifest_error}")
        st.warning(md(shown[-1]), icon=":material/error:")
    if src is not None and src.n_unreadable:
        shown.append(f"{src.n_unreadable} answer file(s) unreadable in {src.id}; those cases show the error, never a verdict")
        st.warning(md(shown[-1]), icon=":material/error:")
    absent = _absent_modules()
    if absent:
        shown.append(f"Not in this deployment: {', '.join(absent)}. Sections that need them show a snapshot or say so.")
        st.caption(md(shown[-1]))
    if _GRAPH_STATE["error"]:
        shown.append(f"TigerGraph unreachable since {_GRAPH_STATE['since']}: {_GRAPH_STATE['error']}")
        st.warning(md(shown[-1]), icon=":material/cloud_off:")
    for value in invalid:
        shown.append(f"Ignored {value} from the URL: not a source or case here")
        st.caption(md(shown[-1]))
    return shown


def context_bar(page_key: str) -> Ctx:
    """The bar above every page, rendered by the router before pg.run(): Source picker, Case picker on the Case file and
    Graph pages, provenance and mode chips, a Provenance popover and the identity popover; banners below. ?run= and ?case=
    are synced by hand (never bind='query-params'). Stores the Ctx in st.session_state['_ctx']."""
    st = _st()
    srcs = sources()
    by_id = {s.id: s for s in srcs}
    labels = {s.id: s.label for s in srcs}
    mode, reason = mode_status()
    invalid: list[str] = []
    source_id = case_id = None
    with st.container(border=True, horizontal=True, vertical_alignment="center", key="ctxbar"):
        if srcs:
            bad = _seed("run", list(by_id), "_run_qp", "run_id", default_source)
            invalid += [f"run={bad}"] if bad else []
            source_id = st.selectbox("Source", list(by_id), key="run", format_func=lambda i: labels.get(i, i), label_visibility="collapsed", width=420)
            _write_url("run", source_id, "_run_qp")
        else:
            st.caption("No sources: no agent run under runs/ and no answer files under cases/")
        src = by_id.get(source_id) if source_id else None
        pack_ids = sorted(r["case_id"] for r in pack_for_source(src)) if src else []
        if page_key in CASE_PAGES:
            if pack_ids:
                bad = _seed("case", pack_ids, "_case_qp", "case_id", lambda: _default_case(pack_ids))
                invalid += [f"case={bad}"] if bad else []
                case_id = st.selectbox("Case", pack_ids, key="case", label_visibility="collapsed", width=160)
                _write_url("case", case_id, "_case_qp")
                state = case_state(source_id, case_id)
                if state != "done":  # a decided case says so on its own page; only an unfinished one needs the chip
                    chip(STATE_LABELS.get(state, state), STATE_COLORS.get(state, "gray"))
            else:
                st.caption("This source has no case pack on this deployment")
        else:
            mirror = st.session_state.get("case_id")
            case_id = mirror if mirror in pack_ids else _default_case(pack_ids)
        _provenance_line(src, mode, reason, st)
        if source_id:
            with st.popover("Provenance", icon=":material/info:", key="provenance"):
                st.json(run_provenance(source_id), expanded=True)
        st.space("stretch")
        identity_popover()
    banners = _banners(src, invalid)
    ctx = Ctx(page_key, source_id, src, case_id, mode, reason, read_only(), approver_session(), banners)
    st.session_state["run_id"] = source_id
    if case_id:
        st.session_state["case_id"] = case_id
    st.session_state["_ctx"] = ctx
    st.session_state["_router"] = True
    return ctx


def _leave_pages_dir_mode() -> None:
    """ui/pages/ sits next to ui/app.py, so Streamlit serves the first run of a fresh process in its pages/-directory mode,
    which executes a page file without the router (a deep link opened right after a restart). st.navigation would switch
    the process to router mode anyway; do it now and rerun the same page through the router."""
    try:
        from streamlit.runtime.pages_manager import PagesManager
    except ImportError:
        return
    if getattr(PagesManager, "uses_pages_directory", None):
        PagesManager.uses_pages_directory = False
        _st().rerun()


def page_setup(page_key: str) -> Ctx:
    """First call in every page file. Under the router it returns the Ctx the context bar stored; a page run alone (AppTest,
    a single-page run) sets its own page config and CSS and resolves the same Ctx silently from ?run / ?case, the session
    run_id / case_id and the defaults, rendering no widgets."""
    st = _st()
    _leave_pages_dir_mode()  # first, so a Ctx left in the session by an earlier run is never mistaken for this run's
    ctx = st.session_state.get("_ctx")
    if st.session_state.get("_router") and isinstance(ctx, Ctx):
        return ctx if ctx.page_key == page_key else dataclasses.replace(ctx, page_key=page_key)
    st.set_page_config(page_title=PAGES.get(page_key, {}).get("title", page_key), layout="wide")
    inject_css()
    srcs = sources()
    by_id = {s.id: s for s in srcs}
    url_run, sess_run = st.query_params.get("run"), st.session_state.get("run_id")
    source_id = url_run if url_run in by_id else sess_run if sess_run in by_id else (default_source() or None)
    src = by_id.get(source_id) if source_id else None
    pack_ids = sorted(r["case_id"] for r in pack_for_source(src)) if src else []
    url_case, sess_case = st.query_params.get("case"), st.session_state.get("case_id")
    case_id = url_case if url_case in pack_ids else sess_case if sess_case in pack_ids else _default_case(pack_ids)
    mode, reason = mode_status()
    st.session_state["run_id"] = source_id
    if case_id:
        st.session_state["case_id"] = case_id
    return Ctx(page_key, source_id, src, case_id, mode, reason, read_only(), approver_session(), [])


def _page_error_types() -> tuple[type[BaseException], ...]:
    from streamlit.errors import StreamlitAPIException, StreamlitPageNotFoundError

    return StreamlitPageNotFoundError, StreamlitAPIException


def goto(page_key: str, case: str | None = None, run: str | None = None) -> None:
    """Switch to a page with ?run= (the current source unless given) and ?case=. Call it from the script body, never from a
    callback. A page run alone cannot switch, so it gets a caption instead; every other error propagates."""
    st = _st()
    qp = {k: v for k, v in {"run": run or st.session_state.get("run_id"), "case": case}.items() if v}
    try:
        st.switch_page(_page_path(page_key), query_params=qp)
    except _page_error_types():
        st.caption(md(f"Open {PAGES[page_key]['title']} from the top bar" + (f" for {case}" if case else "")))


def page_link(page_key: str, label: str, *, icon: str | None = None, case: str | None = None, help: str | None = None) -> None:
    """st.page_link to a PAGES key carrying ?run= and ?case=; `label` is plain text. A caption when the page cannot be
    linked (a page run alone)."""
    st = _st()
    qp = {k: v for k, v in {"run": st.session_state.get("run_id"), "case": case}.items() if v}
    try:
        st.page_link(_page_path(page_key), label=md(label), icon=icon, help=md(help) if help else None, query_params=qp)
    except _page_error_types():
        st.caption(md(f"{label}: open {PAGES[page_key]['title']} from the top bar"))


def link_to(page_file: str, label: str, icon: str | None = None) -> None:
    """Kept for old callers: st.page_link by file, a caption when the page cannot be resolved (a page run alone)."""
    st = _st()
    try:
        st.page_link(page_file, label=label, icon=icon)
    except _page_error_types():
        st.caption(md(f"{label} (open it from the top bar)"))


def case_select(where: Any, pack_ids: list[str], key: str, preferred: str | None = None) -> str | None:
    """Kept for old pages: a Case selectbox seeded from `preferred`, then the session case_id, then HHF-001 / the first id.
    New pages read ctx.case_id from the router's picker instead."""
    st = _st()
    if not pack_ids:
        where.caption("No cases in this pack.")
        return None
    if st.session_state.get(key) not in pack_ids:
        want = preferred or st.session_state.get("case_id") or _default_case(pack_ids)
        st.session_state[key] = want if want in pack_ids else pack_ids[0]
    case_id = where.selectbox("Case", pack_ids, key=key)
    st.session_state["case_id"] = case_id
    return case_id


# --------------------------------------------------------------------------- #
# Identity: one shared sign-in for every human gate                           #
# --------------------------------------------------------------------------- #
ADD_PRINCIPAL_CMD = "uv run python -m ui.approvals_db add-principal <name> --role team_lead|fraud_manager"


def approver_session() -> approvals_db.ApproverSession | None:
    """The signed-in approver of this browser session, checked by approvals_db.verify_session (TTL included); an expired
    or forged session is dropped."""
    st = _st()
    sess = st.session_state.get("approver_session")
    if sess is None:
        return None
    try:
        return approvals_db.verify_session(sess)
    except PermissionError:
        st.session_state.pop("approver_session", None)
        return None


def identity_popover() -> None:
    """Sign-in popover: fail-closed notice without a directory, a principal + passphrase form otherwise, and who is signed
    in with what they may decide and when the session expires."""
    st = _st()
    status = approvals_db.directory_status()
    sess = approver_session()
    with st.popover(md(sess.principal) if sess else "Sign in", icon=":material/badge:", key="identity"):
        if sess:
            routes = "/".join(sorted(approvals_db.ROLE_ALLOWED_ROUTES.get(sess.role, set()))) or "nothing"
            left = approvals_db.session_expires_in_s(sess)
            st.markdown(f"**{md(sess.principal)}** · {md(sess.role)} · may decide {md(routes)}" + (f" · expires in {max(0, int(left // 60))} min" if left is not None else ""))
            if st.button("Sign out", key="identity-signout", icon=":material/logout:"):
                st.session_state.pop("approver_session", None)
                st.rerun()
        elif status["error"] or not status["n"]:
            st.markdown("**Approvers: 0 — nobody can approve (fail closed)**")
            if status["error"]:
                st.caption(md(f"{rel(status['path'])} unreadable: {status['error']}"))
            st.caption("An administrator adds principals on the server:")
            st.code(ADD_PRINCIPAL_CMD, language=None, wrap_lines=True)
        else:
            with st.form("identity-signin", border=False):
                who = st.text_input("Principal", max_chars=approvals_db.DECIDED_BY_MAX, key="identity-principal")
                secret = st.text_input("Passphrase", type="password", key="identity-passphrase")
                if st.form_submit_button("Sign in", icon=":material/login:"):
                    try:
                        st.session_state["approver_session"] = approvals_db.authenticate(who, secret)
                    except PermissionError as exc:
                        approvals_db.log_signin_failure(who, str(exc))
                        st.error("Sign-in refused")
                    else:
                        st.rerun()
            st.caption(md(f"{status['n']} principal(s) in {rel(status['path'])}; the role comes from that file only"))


# --------------------------------------------------------------------------- #
# Graph access (live) and the answer-derived subgraph                         #
# --------------------------------------------------------------------------- #
UI_GRAPH_WAIT_S = 10  # the UI waits this long for a resuming workspace, not the 240 s the loaders allow
GRAPH_RETRY_AFTER_S = 30  # after a failed connect, reruns reuse the error this long instead of waiting again
_GRAPH_LAST_TRY = {"at": 0.0}


def _note_graph(error: str | None) -> None:
    if error:
        if not _GRAPH_STATE["error"]:
            _GRAPH_STATE["since"] = time.strftime("%H:%M:%S")
        _GRAPH_STATE["error"] = error[:300]
    else:
        _GRAPH_STATE.update(error="", since="")


def graph_retry() -> None:
    """Forget the last graph failure so the next graph_connection() tries again at once (a Retry button)."""
    _GRAPH_LAST_TRY["at"] = 0.0
    _note_graph(None)


def graph_status() -> dict:
    """{'error', 'since'} of the last graph failure seen by this process ('' when none)."""
    return dict(_GRAPH_STATE)


def _connect_graph():
    from pyTigerGraph import TigerGraphConnection

    from ops.ensure_awake import ensure_awake, require_secret

    secret = require_secret(_env("TG_SECRET"))
    conn = TigerGraphConnection(host=_env("TG_HOST"), graphname=_env("TG_GRAPHNAME", "FraudGraph"), gsqlSecret=secret)
    conn.customizeHeader(timeout=int(_env("TG_QUERY_TIMEOUT_MS", "120000")), responseSize=64_000_000)
    ensure_awake(max_wait_s=UI_GRAPH_WAIT_S)(conn.getToken)(secret)
    return conn


def graph_connection() -> tuple[Any | None, str | None]:
    """(conn, None) in live mode, cached per process; (None, 'mock mode') in mock mode; (None, error) on failure. Never raises."""
    if run_mode() != "live":
        return None, "mock mode"
    if _GRAPH_STATE["error"] and time.monotonic() - _GRAPH_LAST_TRY["at"] < GRAPH_RETRY_AFTER_S:
        return None, _GRAPH_STATE["error"]
    connect = _ST_CACHED.get("_connect_graph")
    if connect is None:
        connect = _ST_CACHED["_connect_graph"] = _st().cache_resource(show_spinner="Connecting to TigerGraph…")(_connect_graph)
    _GRAPH_LAST_TRY["at"] = time.monotonic()
    try:
        conn = connect()  # a failure is not cached by st.cache_resource, so the next attempt reconnects
    except Exception as exc:  # noqa: BLE001 - shown as a banner, the page renders the answer-derived view
        err = f"{type(exc).__name__}: {exc}"
        _note_graph(err)
        return None, err
    return conn, None


def run_installed_safe(name: str, params: dict, timeout_ms: int = 15000) -> tuple[dict | None, str | None, float]:
    """(merged PRINT dict, error, latency_s) of an installed query, waiting at most 10 s for a resume. Never raises; callers cache."""
    conn, err = graph_connection()
    if conn is None:
        return None, err, 0.0
    t0 = time.time()
    try:
        from ops.ensure_awake import ensure_awake, is_transient

        res = ensure_awake(max_wait_s=UI_GRAPH_WAIT_S)(conn.runInstalledQuery)(name, params, timeout=timeout_ms, usePost=True)
    except Exception as exc:  # noqa: BLE001
        msg = f"{name}: {type(exc).__name__}: {exc}"
        if is_transient(exc):  # a GSQL or parameter error is the query's problem, not an unreachable graph
            _note_graph(msg)
        return None, msg, round(time.time() - t0, 3)
    _note_graph(None)
    merged: dict[str, Any] = {}
    for obj in res or []:
        if isinstance(obj, dict):
            merged.update(obj)
    return merged, None, round(time.time() - t0, 3)


def graph_conn():
    """Kept for old pages: the connection, None in mock mode; raises RuntimeError when the graph is unreachable."""
    conn, err = graph_connection()
    if conn is None and err != "mock mode":
        raise RuntimeError(err)
    return conn


def run_installed(conn, name: str, params: dict, timeout_ms: int = 60_000) -> dict:
    """Kept for old pages: run an installed query and merge its PRINT objects; raises on failure."""
    from ops.ensure_awake import run_query

    res = run_query(conn, name, params, timeout_ms=timeout_ms)
    merged: dict[str, Any] = {}
    for obj in res or []:
        if isinstance(obj, dict):
            merged.update(obj)
    return merged


# Light and dark node palettes from the config.toml tokens, so the graph reads as part of the same theme.
NODE_COLORS = {
    "Card": "#b8860b",
    "Transaction": "#2563eb",
    "DeviceProfile": "#c0392b",
    "EmailDomain": "#7c3aed",
    "BillingRegion": "#0e7c86",
    "ClosedCase": "#5b6472",
    "AgentCase": "#1e7e34",
    "Customer": "#0b3d91",
}
NODE_COLORS_DARK = {
    "Card": "#d29922",
    "Transaction": "#79c0ff",
    "DeviceProfile": "#f85149",
    "EmailDomain": "#bc8cff",
    "BillingRegion": "#39c5cf",
    "ClosedCase": "#8b949e",
    "AgentCase": "#3fb950",
    "Customer": "#58a6ff",
}


def node_colors() -> dict[str, str]:
    """NODE_COLORS or NODE_COLORS_DARK after st.context.theme.type (light when unknown)."""
    try:
        dark = _st().context.theme.type == "dark"
    except Exception:  # noqa: BLE001 - older runtimes and bare mode have no theme context
        dark = False
    return NODE_COLORS_DARK if dark else NODE_COLORS


def vtype(node: dict) -> str:
    """Vertex type of a case_subgraph node: the installed query prints `vtype` (`type` is reserved in GSQL); the
    answer-derived subgraph and older fixtures use `type`."""
    return str(node.get("vtype") or node.get("type") or "")


def etype(edge: dict) -> str:
    """Edge type of a case_subgraph edge (`etype`, falling back to `type`)."""
    return str(edge.get("etype") or edge.get("type") or "")


def subgraph_from_answer(answer: dict, meta: dict, written: bool | None = None) -> dict:
    """A case_subgraph-shaped {nodes[{id, vtype, label}], edges[{src, dst, etype}]} built from the answer file alone (mock /
    fallback) - the same keys the installed query prints. The AgentCase node carries `written`: the persist evidence when
    the caller passes it, else the answer's written_to_graph flag, and is labelled 'not written to graph' when False."""
    case = answer.get("case", {})
    card = meta.get("card_id", "")
    cust = meta.get("customer_id", "")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(nid: str, ntype: str, label: str | None = None):
        if nid and nid not in nodes:
            nodes[nid] = {"id": nid, "vtype": ntype, "label": label or nid}

    add(cust, "Customer")
    add(card, "Card")
    if cust and card:
        edges.append({"src": cust, "dst": card, "etype": "OWNS"})
    gid = case.get("graph_case_id") or f"AC-{answer.get('case_id','')}"
    is_written = bool(case.get("written_to_graph")) if written is None else bool(written)
    add(gid, "AgentCase", f"{gid} ({case.get('verdict','')})" if is_written else f"{gid} (not written to graph)")
    nodes[gid]["written"] = is_written
    if card:
        edges.append({"src": gid, "dst": card, "etype": "CASE_ON_CARD"})
    for t in case.get("affected_txn_ids", []):
        add(t, "Transaction")
        edges.append({"src": card, "dst": t, "etype": "MADE"})
        edges.append({"src": gid, "dst": t, "etype": "CASE_INVOLVES"})
    flagged = meta.get("flagged_txn_id", "")
    if flagged and flagged not in nodes:
        add(flagged, "Transaction", f"{flagged} (flagged)")
        edges.append({"src": card, "dst": flagged, "etype": "MADE"})
    for d in case.get("connected_device_profiles", []):
        add(d, "DeviceProfile", d.split(" | ")[0])
        for t in case.get("affected_txn_ids", []):
            edges.append({"src": t, "dst": d, "etype": "FROM_DEVICE"})
        for c in case.get("connected_card_ids", []):
            add(c, "Card")
            edges.append({"src": c, "dst": d, "etype": "SHARES_DEVICE"})
    if not case.get("connected_device_profiles"):
        for c in case.get("connected_card_ids", []):
            add(c, "Card")
            edges.append({"src": gid, "dst": c, "etype": "CASE_CONNECTED_TO"})
    for cc in case.get("similar_prior_cases", []):
        add(cc, "ClosedCase")
        edges.append({"src": gid, "dst": cc, "etype": "CASE_SIMILAR_TO"})
    return {"nodes": list(nodes.values()), "edges": edges}


# --------------------------------------------------------------------------- #
# Agent runs from the UI: backend checks, launch, lock                        #
# --------------------------------------------------------------------------- #
LLM_BACKENDS = ("gemini", "google", "cli", "api", "mock")


def backend_status(backend: str) -> dict:
    """{ok, reason} for an LLM backend, naming environment variables only, never their values."""
    if backend == "mock":
        return {"ok": True, "reason": "offline fixtures (FakeAnthropic), no key needed"}
    if backend in ("gemini", "google"):
        names = [n for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY") if _env(n).strip()]
        return {"ok": bool(names), "reason": f"{names[0]} is set" if names else "GEMINI_API_KEY or GOOGLE_API_KEY is not set"}
    if backend == "api":
        ok = bool(_env("ANTHROPIC_API_KEY").strip())
        return {"ok": ok, "reason": "ANTHROPIC_API_KEY is set" if ok else "ANTHROPIC_API_KEY is not set"}
    if backend == "cli":
        found = shutil.which("claude")
        return {"ok": bool(found), "reason": "the claude CLI is on PATH" if found else "the claude CLI is not on PATH"}
    return {"ok": False, "reason": f"unknown backend {backend!r}; one of {', '.join(LLM_BACKENDS)}"}


def launch_run(cases: list[str], backend: str, run_id: str, resume: bool = False, session: object = None) -> dict:
    """Start agent.bench detached (its own session) for `cases` under runs/<run_id>/, logging to runs/<run_id>/bench.log and
    writing runs/<run_id>/.bench.lock {pid, started, cases, backend}. RUN_MODE and --llm-backend are always explicit.
    Refuses on a read-only deployment, for a live backend without a verified approver `session` when RUNS_NEED_APPROVER
    is set, for a live backend outside live mode, when the backend is not ready, and while any live lock exists.
    Returns {ok, error, pid, cmd, log, lock}."""
    out: dict = {"ok": False, "error": "", "pid": None, "cmd": [], "log": "", "lock": ""}
    if read_only():
        return {**out, "error": "read-only deployment (DEPLOY_READONLY=1): runs are never started here"}
    if backend != "mock" and runs_need_approver():
        try:
            approvals_db.verify_session(session)
        except PermissionError:
            return {**out, "error": "live runs on this deployment need a signed-in approver (Sign in, top right)"}
    if not _safe_id(run_id) or not cases or not all(_safe_id(c) for c in cases):
        return {**out, "error": "a run id and at least one case id are needed"}
    mode, reason = mode_status()
    if backend != "mock" and mode != "live":
        return {**out, "error": f"backend {backend} needs the live graph, but the mode is mock ({reason})"}
    bs = backend_status(backend)
    if not bs["ok"]:
        return {**out, "error": f"backend {backend} not ready: {bs['reason']}"}
    held = run_lock()
    if held:
        return {**out, "error": f"run {held.get('run_id')} is still running (pid {held.get('pid')}); one live run at a time"}
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "agent.bench", "--run-id", run_id, "--cases", ",".join(cases), "--llm-backend", backend]
    if backend == "mock":
        cmd.append("--mock")
    if resume:
        cmd.append("--resume")
    env = {**os.environ, "RUN_MODE": "mock" if backend == "mock" else "live", "LLM_BACKEND": backend}
    log_path = run_dir / "bench.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    lock_path = run_dir / LOCK_FILE
    lock_path.write_text(json.dumps({"pid": proc.pid, "started": _now(), "cases": list(cases), "backend": backend}), encoding="utf-8")
    return {**out, "ok": True, "pid": proc.pid, "cmd": cmd, "log": rel(log_path), "lock": rel(lock_path)}

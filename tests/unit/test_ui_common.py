"""ui/common.py + ops/make_mock_run.py: run loading, derived views, SAR badges, mock subgraph, the shared-layer rules
(escaping, sources, default source, case states, the counting rule, agreement, safety, the Final Round pack, the
read-only approvals database) and the Streamlit pages rendering without exceptions (streamlit.testing AppTest)."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import make_mock_run
from ui import approvals_db, common

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "answers"

# The router plus the 10 page files; the CI 'UI smoke' step renders the same list.
PAGES = [
    "ui/app.py",
    "ui/pages/0_overview.py",
    "ui/pages/1_queue.py",
    "ui/pages/2_case.py",
    "ui/pages/3_approvals.py",
    "ui/pages/4_graph.py",
    "ui/pages/5_runs.py",
    "ui/pages/6_schema.py",
    "ui/pages/7_architecture.py",
    "ui/pages/8_detectors.py",
    "ui/pages/9_security.py",
]


# --------------------------------------------------------------------------- #
# Fixtures and private helpers                                                #
# --------------------------------------------------------------------------- #
def _clear_caches() -> None:
    """Drop the module's caches so a test never sees another test's tmp tree (they are keyed on signatures anyway)."""
    common._FILE_CACHE.clear()
    for fn in (common._final_round_pack, common._case_index):
        fn.cache_clear()
    for fn in common._ST_CACHED.values():
        fn.clear()


def _base_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """RUNS_DIR, CASES_DIR and APPROVALS_DB under tmp_path, RUN_MODE=mock, not read-only, no UI_DEFAULT_SOURCE.
    ROOT/.env is loaded first (override=False), so the values set here win over it."""
    common._load_dotenv()
    runs, cases = tmp_path / "runs", tmp_path / "cases"
    runs.mkdir()
    cases.mkdir()
    db = tmp_path / "approvals" / "approvals.sqlite"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("CASES_DIR", str(cases))
    monkeypatch.setenv("APPROVALS_DB", str(db))
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("DEPLOY_READONLY", "")
    monkeypatch.delenv("UI_DEFAULT_SOURCE", raising=False)
    _clear_caches()
    return {"runs": runs, "cases": cases, "db": db}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    out = _base_env(monkeypatch, tmp_path)
    yield out
    _clear_caches()


@pytest.fixture
def mock_run(tmp_path, monkeypatch):
    common._load_dotenv()
    runs = tmp_path / "runs"
    written = make_mock_run.build(str(FIX_DIR), "t1", str(runs), str(common.ROOT / "data" / "raw" / "case_pack.csv"))
    assert written == ["HHG-014"]
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("DEPLOY_READONLY", "")
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.delenv("UI_DEFAULT_SOURCE", raising=False)
    _clear_caches()
    yield runs
    _clear_caches()


def _answer(case_id: str, verdict: str = "fraud", actions=(("BLOCK_CARD", "L1"),), *, sar: bool = False, p: float = 0.9,
            tool_calls: int = 3, tokens: int = 0, **case) -> dict:
    return {
        "case": {"case_id": case_id, "verdict": verdict, "fraud_probability": p, "pattern": case.pop("pattern", "card_testing"),
                 "status": case.pop("status", "open"), "exposure_usd": case.pop("exposure_usd", 100.0), **case},
        "next_best_actions": {"initial": [], "final": [{"action": a, "route": r, "reason": "R1"} for a, r in actions], "what_changed": "nothing"},
        "sar": {"file": sar, "reason": "3a" if sar else "not met", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []},
        "evidence_requests": [], "stop_reason": "", "tool_calls": tool_calls, "tokens": tokens, "latency_s": 1.5,
    }


def _ctx_row(case_id: str) -> dict:
    return {"case_id": case_id, "opened_at": "2017-01-05 10:00:00", "trigger_type": "risk_score", "flagged_txn_id": "4000001",
            "card_id": "C00001-K1", "customer_id": "C00001", "risk_score": 0.81, "trigger_text": f"Model scored {case_id} at 0.81 ($12.00)."}


def _p0(case_id: str) -> dict:
    return {"kind": "phase", "phase": "P0", "name": "intake", "payload": {"ctx": _ctx_row(case_id)}}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _write_run(runs: Path, run_id: str, cases: dict[str, dict | None], *, first_started: str = "2026-10-01T10:00:00",
               mode: str = "live", phases: dict[str, list[dict]] | None = None, calls: dict[str, list[dict]] | None = None) -> Path:
    d = runs / run_id
    d.mkdir(parents=True)
    (d / "run_manifest.json").write_text(json.dumps({"run_id": run_id, "run_mode": mode, "first_started": first_started,
                                                     "started": first_started, "cases": sorted(cases)}), encoding="utf-8")
    for cid, ans in cases.items():
        c = d / cid
        c.mkdir()
        _write_jsonl(c / "phases.jsonl", (phases or {}).get(cid, [_p0(cid)]))
        if (calls or {}).get(cid) is not None:
            _write_jsonl(c / "calls.jsonl", calls[cid])
        if ans is not None:
            (c / "answer.json").write_text(json.dumps(ans), encoding="utf-8")
    return d


def _age(path: Path, seconds: float) -> None:
    """Back-date every file under `path` so a trace without an answer reads as incomplete, not running."""
    then = time.time() - seconds
    for p in [path, *path.rglob("*")]:
        os.utime(p, (then, then))


def _sig(path: Path):
    try:
        s = path.stat()
    except OSError:
        return None
    return s.st_mtime_ns, s.st_size


# --------------------------------------------------------------------------- #
# Kept from increment 1                                                       #
# --------------------------------------------------------------------------- #
def test_case_pack_embedded_matches_readme():
    pack = common.load_case_pack()
    assert len(pack) == 20
    assert pack[0]["case_id"] == "HHG-017" and pack[-1]["case_id"] == "HHG-004"  # opened_at order
    m = common.case_meta("HHG-014")
    assert m["card_id"] == "C13487-K1" and m["flagged_txn_id"] == "3478561" and m["trigger_type"] == "analyst_request"


def test_run_loading_and_derived_views(mock_run):
    assert common.list_runs() == ["t1"]
    assert common.list_cases_in_run("t1") == ["HHG-014"]
    run = common.load_case_run("t1", "HHG-014")
    assert run.ok and len(run.phases) == 11 and len(run.calls) >= 9
    pre, post = common.probability_pre_post(run)
    assert pre == 0.9 and post == 0.9
    ff, fl = common.families(run)
    assert set(ff) >= {"device", "memory", "history"} and fl == []
    assert common.counterfactual(run) == ""  # nothing asked
    stats = common.tool_stats(run)
    assert stats["tool_calls"] == 9 and stats["llm_calls"] == 6 and stats["tokens"] > 0
    # the single counting rule: reads equal answer.json's tool_calls whenever the fixture carries the counter
    if run.answer.get("tool_calls") is not None:
        assert common.trace_stats(run)["reads"] == run.answer["tool_calls"]
    assert "Suspicious Activity Report" in run.sar_md


def test_rule_chips_and_actions_diff():
    assert common.rule_chips("R2 and R5: denied; 3a holds; policy 6 test 1") == ["R2", "R5", "3a", "§6"]
    assert common.rule_chips("R10 applies, r1 does not") == ["R10", "R1"]
    added, removed, kept = common.actions_diff([{"action": "VERIFY_WITH_CUSTOMER"}, {"action": "CREATE_CASE"}], [{"action": "CREATE_CASE"}, {"action": "BLOCK_CARD"}])
    assert (added, removed, kept) == (["BLOCK_CARD"], ["VERIFY_WITH_CUSTOMER"], ["CREATE_CASE"])


def test_sar_checks_on_sample():
    """sar_checks delegates to rag.validate_sar (D10): one badge per code, ids resolved against the answer's own lists."""
    ans = json.loads((FIX_DIR / "HHG-014.sample.json").read_text())
    checks = common.sar_checks(ans, common.case_meta("HHG-014"))
    failed = [(n, d) for n, ok, d in checks if not ok]
    assert failed == [], failed
    assert len(checks) == len(common.SAR_CHECK_LABELS)
    neg = dict(ans, sar={"file": False, "reason": "3a not met", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []})
    neg["next_best_actions"] = {"initial": [], "final": [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "R3"}], "what_changed": "nothing"}
    assert all(ok for _, ok, _ in common.sar_checks(neg))
    bad = dict(ans, sar=dict(ans["sar"], total_amount_usd=1.0))
    assert any(n.startswith("total_amount_usd") and not ok for n, ok, _ in common.sar_checks(bad))


def test_subgraph_from_answer():
    ans = json.loads((FIX_DIR / "HHG-014.sample.json").read_text())
    sub = common.subgraph_from_answer(ans, common.case_meta("HHG-014"))
    types = {common.vtype(n) for n in sub["nodes"]}
    assert {"Customer", "Card", "Transaction", "DeviceProfile", "ClosedCase", "AgentCase"} <= types
    assert all("vtype" in n for n in sub["nodes"]) and all("etype" in e for e in sub["edges"])  # case_subgraph keys
    assert common.vtype({"type": "Card"}) == "Card" and common.etype({"type": "MADE"}) == "MADE"   # fallback
    assert sum(1 for n in sub["nodes"] if common.vtype(n) == "Card") == 28  # the card + 27 connected
    assert sum(1 for e in sub["edges"] if common.etype(e) == "SHARES_DEVICE") == 27
    ids = {n["id"] for n in sub["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in sub["edges"])


# --------------------------------------------------------------------------- #
# Shared layer: escaping                                                      #
# --------------------------------------------------------------------------- #
def test_md_keeps_amounts_and_escapes_markup():
    assert common.md("$1 and $2") == r"\$1 and \$2"  # two amounts, never an inline-math span
    closed = common.md("a] (b)")
    assert r"\]" in closed and "]" not in closed.replace(r"\]", "")
    comment = common.md("<!-- hidden --> text")
    assert "<!--" not in comment and comment.startswith(r"\<\!--")
    assert "http" not in common.md("![alt](http://evil.example/x.png)")  # images reduced to their alt text
    assert common.md(None) == ""
    assert common.md("- item") == r"\- item" and common.md("1. one") == r"1\. one"
    assert common.badge("a]b", "red") == r":red-badge[a\]b]"


# --------------------------------------------------------------------------- #
# Shared layer: sources and the default source                                #
# --------------------------------------------------------------------------- #
def test_sources_exclude_maintenance_and_underscore_folders(tree):
    runs = tree["runs"]
    _write_run(runs, "r2-a", {"HHF-001": _answer("HHF-001")})
    maint = runs / "clear-writebacks-x"
    maint.mkdir()
    (maint / "stale_writebacks.json").write_text(json.dumps({"generated_at": "2026-10-31T00:12:27", "applied": True, "cases": ["HHF-001"]}))
    _write_run(runs, "_schema_ai", {"HHF-002": _answer("HHF-002")})  # looks like a run, but an underscore folder never is
    ids = [s.id for s in common.sources()]
    assert "r2-a" in ids
    assert "clear-writebacks-x" not in ids and "_schema_ai" not in ids
    assert common.list_runs() == ["r2-a"]
    assert [m["folder"] for m in common.list_maintenance()] == ["clear-writebacks-x"]  # listed as maintenance, never a source
    assert common.get_source("clear-writebacks-x") is None


def test_default_source_env_and_promoted_from_rule(tree, monkeypatch):
    runs, cases = tree["runs"], tree["cases"]
    promoted = _answer("HHF-001", pattern="ato")
    _write_run(runs, "r2-old", {"HHF-001": promoted}, first_started="2026-10-01T10:00:00")
    _write_run(runs, "r2-new", {"HHF-001": _answer("HHF-001", pattern="card_testing")}, first_started="2026-10-20T10:00:00")
    (cases / "HHF-001.json").write_text(json.dumps(promoted, indent=2), encoding="utf-8")  # parses equal to r2-old's answer

    assert common.promoted_from() == {"HHF-001": ["r2-old"]}
    assert common.default_source() == "r2-old"  # the run the submitted answers were promoted from, though r2-new is newer
    sub = common.get_source("submitted")
    assert sub is not None and sub.mode == "promoted" and sub.promoted_from == {"HHF-001": ["r2-old"]}

    monkeypatch.setenv("UI_DEFAULT_SOURCE", "r2-new")
    assert common.default_source() == "r2-new"
    monkeypatch.setenv("UI_DEFAULT_SOURCE", "no-such-run")
    assert common.default_source() == "r2-old"  # an invalid override is ignored
    monkeypatch.delenv("UI_DEFAULT_SOURCE")

    # submitted answers identical to no run: the newest complete Final Round live run wins
    (cases / "HHF-001.json").write_text(json.dumps(_answer("HHF-001", pattern="other", exposure_usd=999.5)), encoding="utf-8")
    assert common.promoted_from() == {"HHF-001": []}
    assert common.default_source() == "r2-new"


# --------------------------------------------------------------------------- #
# Shared layer: case states                                                   #
# --------------------------------------------------------------------------- #
def test_case_view_states_never_invent_a_verdict(tree):
    runs = tree["runs"]
    failed_phases = [_p0("HHF-003"), {"kind": "phase", "phase": "P1", "name": "memory", "payload": {}},
                     {"kind": "note", "text": "case failed", "error": "TimeoutError: graph read exceeded 15000 ms"}]
    d = _write_run(runs, "r2-s", {"HHF-001": _answer("HHF-001"), "HHF-002": None, "HHF-003": None, "HHF-004": None,
                                  "HHF-005": None, "HHF-006": None},
                   phases={"HHF-003": failed_phases, "HHF-004": [_p0("HHF-004"), {"kind": "phase", "phase": "P1", "payload": {}}]})
    (d / "HHF-002" / "answer.json").write_text('{"case": {"verdict": "fraud"', encoding="utf-8")  # torn write
    (d / "HHF-006" / "answer.json").write_text(json.dumps({"case": {"case_id": "HHF-006"}}), encoding="utf-8")  # no verdict
    _age(d, 3600)
    _age(d / "HHF-005", 0)  # written to just now and no answer yet: running

    views = {cid: common.case_view("r2-s", cid) for cid in ("HHF-001", "HHF-002", "HHF-003", "HHF-004", "HHF-005", "HHF-006", "HHF-007")}
    assert {cid: v.state for cid, v in views.items()} == {
        "HHF-001": "done", "HHF-002": "unreadable", "HHF-003": "failed", "HHF-004": "incomplete", "HHF-005": "running",
        "HHF-006": "unreadable", "HHF-007": "not_run"}
    assert views["HHF-001"].ok and views["HHF-001"].answer["case"]["verdict"] == "fraud"
    for cid, v in views.items():
        if cid != "HHF-001":
            assert v.answer is None and not v.ok, cid  # never a verdict
    assert views["HHF-002"].error.startswith("answer.json:")  # the parse error, verbatim
    assert views["HHF-003"].error == "TimeoutError: graph read exceeded 15000 ms"
    assert views["HHF-006"].error == "answer.json misses required key 'case.verdict'"
    assert views["HHF-004"].last_phase == "P1" and views["HHF-004"].meta["case_id"] == "HHF-004"
    assert common.case_state("r2-s", "HHF-003") == "failed"
    src = common.get_source("r2-s")
    assert (src.n_answered, src.n_unreadable, src.n_failed, src.n_running, src.n_total) == (1, 2, 1, 1, 6)
    assert not src.complete  # incomplete, failed and unreadable cases never count as done
    assert common.case_view("../etc", "HHF-001").state == "not_run"  # path-like ids are refused


# --------------------------------------------------------------------------- #
# Shared layer: the single counting rule                                      #
# --------------------------------------------------------------------------- #
def test_trace_stats_single_counting_rule(tree):
    calls = [
        {"kind": "tool", "name": "card_profile", "caller": "harness", "latency_s": 0.2, "ok": True},
        {"kind": "tool", "name": "device_neighbors", "caller": "llm", "latency_s": 0.3, "ok": True},
        {"kind": "tool", "name": "shared_origin_scan", "latency_s": 0.1, "ok": True},  # no caller recorded
        {"kind": "tool", "name": "record_case", "caller": "persist", "latency_s": 0.4, "ok": True},
        {"kind": "tool", "name": "append_case_event", "caller": "persist", "latency_s": 0.5, "ok": False},
        {"kind": "llm", "name": "assess", "model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 200,
         "cache_read_tokens": 300, "cache_write_tokens": 50, "latency_s": 2.0},
        {"kind": "llm", "name": "nba", "model": "gemini-2.5-flash", "input_tokens": 400, "output_tokens": 100, "latency_s": 1.0},
    ]
    total = 1000 + 200 + 300 + 50 + 400 + 100
    _write_run(tree["runs"], "r2-t", {"HHF-001": _answer("HHF-001", tool_calls=3, tokens=total),
                                      "HHF-002": _answer("HHF-002", tool_calls=5, tokens=total + 1)},
               calls={"HHF-001": calls, "HHF-002": calls})
    s = common.trace_stats(common.case_view("r2-t", "HHF-001"))
    assert s["reads"] == 3 and s["writes"] == 2 and s["writes_ok"] == 1 and s["llm_calls"] == 2
    assert s["reads_by_caller"] == {"harness": 1, "llm": 1, "unrecorded": 1}
    assert s["tokens_total"] == total and s["tokens_uncached"] == 1700
    assert s["cache_share"] == pytest.approx(350 / total)
    assert s["matches_answer"] is True and s["label"] == "calls.jsonl" and s["has_trace"]
    assert s["models"] == {"gemini-2.5-flash": 2}
    assert s["write_latency_s"] == pytest.approx(0.9) and s["tool_latency_s"] == pytest.approx(0.6)
    assert common.trace_stats(common.case_view("r2-t", "HHF-002"))["matches_answer"] is False  # counters disagree: said, not hidden

    # no trace: the answer's own counters, with the label saying so
    engine = common.trace_stats(SimpleNamespace(calls=[], answer=_answer("HHF-009", tool_calls=7, tokens=0), promoted_from=[]))
    assert engine["reads"] == 7 and engine["tokens_total"] == 0 and engine["label"] == "deterministic engine, no LLM calls"
    assert engine["matches_answer"] is None and engine["cache_share"] is None
    promoted = common.trace_stats(SimpleNamespace(calls=[], answer=_answer("HHF-009", tool_calls=7, tokens=10), promoted_from=["r2-final"]))
    assert promoted["reads"] == 7 and promoted["tokens_total"] == 10 and "r2-final" in promoted["label"]


# --------------------------------------------------------------------------- #
# Shared layer: agreement and safety                                          #
# --------------------------------------------------------------------------- #
def test_agreement_classes():
    a = _answer("HHF-001", "fraud", (("VERIFY_WITH_CUSTOMER", "auto"), ("CREATE_CASE", "auto")))
    assert common.agreement(a, copy.deepcopy(a))["cls"] == "identical"

    same = copy.deepcopy(a)
    same["case"]["fraud_probability"] = 0.93
    same["next_best_actions"]["final"][0]["reason"] = "R2, worded differently"
    r = common.agreement(a, same)
    assert r["cls"] == "same decision" and r["same_decision"] and not r["identical"]

    soft = copy.deepcopy(a)
    soft["next_best_actions"]["final"][0]["action"] = "DECLINE_TRANSACTION"
    assert common.agreement(a, soft)["cls"] == "differs · no hard action"

    hard = copy.deepcopy(a)
    hard["next_best_actions"]["final"].append({"action": "BLOCK_CARD", "route": "L1", "reason": "R4"})
    r = common.agreement(a, hard)
    assert r["cls"] == "differs · hard action" and any(d.startswith("hard actions:") for d in r["diffs"])

    sar = copy.deepcopy(a)
    sar["sar"]["file"] = True
    assert common.agreement(a, sar)["cls"] == "differs · hard action"  # filing a report is irreversible too
    assert common.agreement(a, None)["cls"] == "not comparable"
    assert set(common.AGREEMENT_COLORS) >= {"identical", "same decision", "differs · no hard action", "differs · hard action", "not comparable"}
    assert common.AGREEMENT_COLORS["differs · hard action"] == "red"


def test_safety_flags_hard_action_on_legitimate_answer():
    answers = {
        "HHF-001": _answer("HHF-001", "legitimate", (("BLOCK_CARD", "L1"), ("CLOSE_NO_FRAUD", "auto"))),
        "HHF-002": _answer("HHF-002", "fraud", (("BLOCK_CARD", "L1"), ("FILE_REPORT", "L2")), sar=True),
        "HHF-003": _answer("HHF-003", "uncertain", (("VERIFY_WITH_CUSTOMER", "auto"),)),
        "HHF-004": _answer("HHF-004", "uncertain", (("ESCALATE_TO_ANALYST", "L1"),)),
        "HHF-005": _answer("HHF-005", "legitimate", (("CLOSE_NO_FRAUD", "auto"),), sar=True),
    }
    s = common.safety(answers)
    assert s["hard_on_non_fraud"] == [("HHF-001", ["BLOCK_CARD"]), ("HHF-005", ["sar.file"])]  # the fraud case is never flagged
    assert s["sar_on_non_fraud"] == ["HHF-005"]
    assert s["uncertain_not_escalated"] == ["HHF-003"] and s["uncertain_escalated"] == 1
    assert s["verdict_mix"] == {"legitimate": 2, "fraud": 1, "uncertain": 2} and s["n"] == 5
    assert common.safety({"HHF-009": _answer("HHF-009", "legitimate", (("CLOSE_NO_FRAUD", "auto"),))})["hard_on_non_fraud"] == []
    for action in ("BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"):
        assert common.action_color(action, "auto") == "red"


# --------------------------------------------------------------------------- #
# Shared layer: the Final Round pack without the CSV or duckdb                #
# --------------------------------------------------------------------------- #
def test_final_round_pack_from_p0_ctx_without_duckdb(tree, monkeypatch):
    monkeypatch.setattr(common, "ROUND2_CSV", tree["runs"].parent / "no-such" / "case_pack_final.csv")
    monkeypatch.setitem(sys.modules, "duckdb", None)  # `import duckdb` raises ImportError, as in the UI image
    _clear_caches()
    rows, notice = common.final_round_pack()
    assert rows == [] and notice.startswith("Final Round case pack unavailable") and "duckdb not installed" in notice

    _write_run(tree["runs"], "r2-p", {"HHF-002": _answer("HHF-002"), "HHF-001": None})
    _clear_caches()
    rows, notice = common.final_round_pack()
    assert [r["case_id"] for r in rows] == ["HHF-001", "HHF-002"]
    assert rows[0]["trigger_text"] == "Model scored HHF-001 at 0.81 ($12.00)." and rows[0]["risk_score"] == "0.81"
    assert set(rows[0]) == set(common.CASE_PACK_COLUMNS)
    assert "rebuilt from the P0 intake" in notice and "runs/r2-p" in notice


# --------------------------------------------------------------------------- #
# Shared layer: approvals database, read-only access, no write on view        #
# --------------------------------------------------------------------------- #
def test_run_rbac_drills_returns_five_server_refusals(tree):
    principals = common.UI_DIR / "principals.json"
    had_principals = principals.exists()
    rows = approvals_db.run_rbac_drills()
    assert len(rows) == 5
    assert {r["attack_id"] for r in rows} == {"ATK-08", "ATK-22", "ATK-23", "ATK-09", "ATK-TAMPER"}
    assert all(r["blocked"] is True for r in rows), rows
    assert all(r.get("observed") for r in rows)
    assert not tree["db"].exists() and not tree["db"].parent.exists()  # APPROVALS_DB untouched
    assert principals.exists() == had_principals  # the real directory untouched


def test_connect_ro_on_missing_path_creates_nothing(tmp_path):
    missing = tmp_path / "nowhere" / "approvals.sqlite"
    assert approvals_db.connect_ro(missing) is None
    assert not missing.exists() and not missing.parent.exists()
    assert approvals_db.rows_ro(path=missing) == []
    assert not missing.parent.exists()


def test_no_write_on_view_shared_layer(tree):
    """Reading everything a page reads leaves APPROVALS_DB uncreated and the checked-in artifacts unchanged."""
    _write_run(tree["runs"], "r2-v", {"HHF-001": _answer("HHF-001")})
    watched = [common.ROOT / "data" / "schema_approvals.json", common.ROOT / "engine" / "out" / "detector_backtest.json",
               common.UI_DIR / "approvals.sqlite", common.UI_DIR / "principals.json"]
    before = {p: _sig(p) for p in watched}
    runs_before = sorted(p.name for p in tree["runs"].iterdir())

    rows, notice = common.inbox_rows("HHF-001")
    assert rows == [] and notice and "No approvals database" in notice
    assert common._pending_final_round() == 0
    for name in (*common._PROOF_FILES, *common.SNAPSHOT_SECTIONS):
        assert common.proof(name)["mode"] in ("file", "snapshot", "missing")
    for s in common.sources():
        common.run_provenance(s.id)
        for cid in s.cases:
            common.trace_stats(common.case_view(s.id, cid))
    common.known_limits()

    assert not tree["db"].exists() and not tree["db"].parent.exists()
    assert {p: _sig(p) for p in watched} == before
    assert sorted(p.name for p in tree["runs"].iterdir()) == runs_before


def test_launch_run_refuses_live_backends_in_mock_mode(tree, monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"subprocess.Popen called: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", boom)
    for backend in common.LLM_BACKENDS:
        if backend == "mock":
            continue
        res = common.launch_run(["HHF-001"], backend, "ui-test", resume=False)
        assert res["ok"] is False and "mock" in res["error"], (backend, res)
    monkeypatch.setenv("DEPLOY_READONLY", "1")
    res = common.launch_run(["HHF-001"], "mock", "ui-test")
    assert res["ok"] is False and "read-only" in res["error"]
    assert not (tree["runs"] / "ui-test").exists()


# --------------------------------------------------------------------------- #
# Pages (AppTest)                                                             #
# --------------------------------------------------------------------------- #
def _apptest(page: str):
    pytest.importorskip("streamlit", reason="AppTest needs streamlit (a project dependency; CI installs it)")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(common.ROOT / page), default_timeout=60)
    at.session_state["run_id"] = "t1"
    at.session_state["case_id"] = "HHG-014"
    return at


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_without_exceptions(mock_run, page):
    at = _apptest(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_pages_render_without_writes(mock_run, tmp_path):
    """No write on view: rendering every page creates no approvals database, no folder under runs/ and changes no
    checked-in artifact."""
    db = Path(os.environ["APPROVALS_DB"])
    watched = [common.ROOT / "data" / "schema_approvals.json", common.ROOT / "engine" / "out" / "detector_backtest.json",
               common.UI_DIR / "approvals.sqlite", common.UI_DIR / "principals.json"]
    before = {p: _sig(p) for p in watched}
    data_before = sorted(p.name for p in (common.ROOT / "data").iterdir()) if (common.ROOT / "data").is_dir() else []
    runs_before = sorted(p.name for p in mock_run.iterdir())
    for page in PAGES:
        at = _apptest(page)
        at.run()
        assert not at.exception, (page, [e.value for e in at.exception])
    assert not db.exists(), "a page created the approvals database on view"
    assert sorted(p.name for p in mock_run.iterdir()) == runs_before
    assert {p: _sig(p) for p in watched} == before
    if (common.ROOT / "data").is_dir():
        assert sorted(p.name for p in (common.ROOT / "data").iterdir()) == data_before


def _page_text(at) -> str:
    """Every visible string of a rendered AppTest page: text elements, widget labels, options and help."""
    out: list[str] = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "error", "success", "text", "code", "exception"):
        try:
            out += [str(getattr(e, "value", "")) for e in getattr(at, kind)]
        except Exception:  # noqa: BLE001 - an element type this Streamlit does not expose
            continue
    for kind in ("button", "radio", "selectbox", "multiselect", "checkbox", "toggle", "text_input", "segmented_control", "pills", "slider"):
        try:
            elems = list(getattr(at, kind))
        except Exception:  # noqa: BLE001
            continue
        for e in elems:
            proto = getattr(e, "proto", None)
            out += [str(getattr(e, "label", "") or ""), str(getattr(proto, "help", "") or "")]
            out += [str(o) for o in (getattr(e, "options", None) or [])]
    return "\n".join(out)


def test_console_mock_mode(mock_run, monkeypatch):
    """Run console in RUN_MODE=mock: live backends are disabled with the reason, nothing spawns at render or when the
    launch control is clicked (that opens the confirm dialog), and the Round 1 '▶▶ Investigate all 20 cases' is gone."""
    calls: list = []

    def no_popen(*a, **k):
        calls.append(a)
        raise AssertionError(f"subprocess.Popen called: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    at = _apptest("ui/pages/1_queue.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = _page_text(at)
    assert "▶" not in text and "Investigate all 20 cases" not in text
    assert "RUN_MODE=mock" in text, "the console must say why live backends are disabled"

    # the backend rule behind the page: every live backend is refused in mock mode, with the reason
    for backend in common.LLM_BACKENDS:
        if backend != "mock":
            res = common.launch_run(["HHF-001"], backend, "ui-mocktest")
            assert not res["ok"] and "RUN_MODE=mock" in res["error"], (backend, res)

    # the launch control opens a confirm dialog at most: no process, no run folder
    for b in [b for b in at.button if str(b.label).startswith(("Start", "Investigate")) and not b.proto.disabled][:1]:
        b.click().run()
        assert not at.exception, (b.label, [e.value for e in at.exception])
    assert calls == []
    assert sorted(p.name for p in mock_run.iterdir()) == ["t1"]


def test_console_read_only_shows_only_the_disabled_message(mock_run, monkeypatch):
    monkeypatch.setenv("DEPLOY_READONLY", "1")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen on a read-only deployment")))
    at = _apptest("ui/pages/1_queue.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("DEPLOY_READONLY=1" in str(e.value) for e in at.info), [str(e.value) for e in at.info]
    assert not [b for b in at.button if str(b.label).startswith(("Start", "Investigate"))]
    assert _nav(monkeypatch)["console"].get("visibility") == "hidden"  # hidden from the top bar
    monkeypatch.setenv("DEPLOY_READONLY", "")
    assert _nav(monkeypatch)["console"].get("visibility", "visible") == "visible"


def _nav(monkeypatch) -> dict[str, dict]:
    """url_path -> the st.Page keyword arguments common.nav_pages() used (st.Page needs a script run context, so it is
    recorded instead of built)."""
    import streamlit

    made: dict[str, dict] = {}

    def page(path, **kw):
        made[kw.get("url_path", "")] = {"path": str(path), **kw}
        return SimpleNamespace(**kw)

    monkeypatch.setattr(streamlit, "Page", page)
    common.nav_pages()
    return made


def test_nav_sections_and_titles(mock_run, monkeypatch):
    pytest.importorskip("streamlit", reason="nav_pages builds st.Page objects")
    made = _nav(monkeypatch)
    for meta in common.PAGES.values():
        if (common.UI_DIR / meta["file"]).is_file():
            assert made[meta["url_path"]]["title"].startswith(meta["title"]) and made[meta["url_path"]]["icon"].startswith(":material/")
    sections = common.nav_pages()
    assert list(sections)[0] == "" and set(sections) <= {"", "Proof", "System"}


# --------------------------------------------------------------------------- #
# PR #1 port: live pipeline strip, call console, phase inspector, scorecard   #
# --------------------------------------------------------------------------- #
def _tool(phase: str, name: str, t: float, **kw) -> dict:
    return {"kind": "tool", "phase": phase, "name": name, "t": t, "ts": f"2026-10-31 11:20:{int(t):02d}", "latency_s": 0.2, "ok": True,
            "caller": "harness", "params": kw.pop("params", {"as_of": "2017-01-05 10:00:00"}), **kw}


def _llm(phase: str, t: float, model: str) -> dict:
    return {"kind": "llm", "phase": phase, "name": phase.split("-")[0], "t": t, "ts": f"2026-10-31 11:20:{int(t):02d}", "model": model,
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0, "latency_s": 2.0, "ok": True}


def _ph(phase: str, t: float, payload: dict | None = None) -> dict:
    return {"kind": "phase", "phase": phase, "name": common.PHASE_NAMES.get(phase.split("-")[0], ""), "t": t, "payload": payload or {}}


def test_pipeline_state_infers_the_running_stage_from_the_trace_files_only(tree):
    """The stage after the newest row and whether it waits on the LLM follow the phase machine's order; the model is the
    one a calls.jsonl row records (this case, else another case of the run), never assumed; a stopped trace is 'stopped'."""
    phases = {"HHF-002": [_p0("HHF-002") | {"t": 0.0}, _ph("P1", 1.8)], "HHF-001": [_p0("HHF-001") | {"t": 0.0}, _ph("P9", 9.0)]}
    calls = {"HHF-002": [_tool("P0", "query:case_context", 0.4), _tool("P1", "query:similar_prior_cases", 1.5)],
             "HHF-001": [_llm("P2", 3.0, "claude-test-model")]}
    _write_run(tree["runs"], "r-live", {"HHF-001": _answer("HHF-001"), "HHF-002": None}, phases=phases, calls=calls)
    v = common.case_view("r-live", "HHF-002")
    assert v.state == "running"
    ps = common.pipeline_state(v)
    states = {s["code"]: s["state"] for s in ps["stages"]}
    assert ps["active"] == "P2" and ps["llm"] is True and ps["model"] == "" and ps["model_source"] == ""
    assert states["S0"] == "offline" and states["P0"] == states["P1"] == "done" and states["P2"] == "active"
    assert all(states[c] == "pending" for c in ("P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"))
    run_model = common.llm_models(common.case_view("r-live", c) for c in ("HHF-001", "HHF-002")).most_common(1)[0][0]
    ps = common.pipeline_state(v, run_model=run_model)
    assert ps["model"] == "claude-test-model" and "another case" in ps["model_source"]

    def state_after(rows: list[dict], cs: list[dict]) -> dict:
        (tree["runs"] / "r-live" / "HHF-002" / "phases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (tree["runs"] / "r-live" / "HHF-002" / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in cs), encoding="utf-8")
        return common.pipeline_state(common.case_view("r-live", "HHF-002"), run_model=run_model)

    base = [_p0("HHF-002") | {"t": 0.0}, _ph("P1", 1.8)]
    ps = state_after(base, [_llm("P2", 4.0, "m-own"), _tool("P2", "query:region_history", 4.3)])
    assert (ps["active"], ps["llm"], ps["model"], ps["model_source"]) == ("P2", True, "m-own", "this case's calls.jsonl")
    p4 = [*base, _ph("P2", 6.0), _ph("P3", 8.0), _ph("P4", 12.0)]
    assert state_after([*p4, _ph("P5-voi", 12.1, {"ask": False}), _ph("P5", 15.0)], [])["active"] == "P8"
    ps = state_after([*p4, _ph("P5-voi", 12.1, {"ask": True}), _ph("P5", 15.0)], [])
    assert (ps["active"], ps["llm"]) == ("P6", False)
    voi = [*p4, _ph("P5-voi", 12.1, {"ask": False})]
    assert state_after(voi, [_llm("P5", 14.0, "m-own")])["llm"] is False  # the policy check follows the model's choice
    retry = {"kind": "note", "t": 14.001, "text": "P5: retrying structured output", "error": "bad JSON"}
    ps = state_after([*voi, retry], [_llm("P5", 14.0, "m-own")])
    assert (ps["active"], ps["llm"], ps["model"]) == ("P5", True, "m-own") and "structured-output retry" in ps["waiting"]
    ps = state_after([*p4, _ph("P5-voi", 12.1, {"ask": False}), _ph("P5", 15.0), _ph("P10", 30.0)], [_llm("P9", 25.0, "m-own")])
    assert ps["active"] == "P9" and {s["code"]: s["state"] for s in ps["stages"]}["P10"] == "done"
    _age(tree["runs"] / "r-live" / "HHF-002", 3600)
    ps = common.pipeline_state(common.case_view("r-live", "HHF-002"))
    assert ps["active"] is None and ps["stopped_at"] == "P9" and {s["code"]: s["state"] for s in ps["stages"]}["P9"] == "stopped"


def test_pipeline_state_done_case_and_the_p8_timeline_entry(tree):
    """A finished case: stages with rows are done, the others 'skipped' (no row). P8 writes no phase row, so its SAR-draft
    LLM call still gets a timeline entry, between P7 and P10, and P10 then starts where P8 ended."""
    rows = [_p0("HHF-003") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0), _ph("P4", 4.0), _ph("P5-voi", 4.1, {"ask": False}),
            _ph("P5", 5.0, {"actions": []}), _ph("P10", 20.0, {"written_to_graph": True}), _ph("P9", 21.0, {"validation": []})]
    cs = [_llm("P8", 12.0, "m-x"), _llm("P9", 15.0, "m-x"), _tool("P10", "pytg:upsertVertex(AgentCase)", 17.0, caller="persist")]
    _write_run(tree["runs"], "r-done", {"HHF-003": _answer("HHF-003", sar=True)}, phases={"HHF-003": rows}, calls={"HHF-003": cs})
    v = common.case_view("r-done", "HHF-003")
    states = {s["code"]: s["state"] for s in common.pipeline_state(v)["stages"]}
    assert states["P6"] == states["P7"] == "skipped" and states["P8"] == states["P9"] == states["P10"] == "done"
    tl = common.trace_timeline(v)
    codes = [e["code"] for e in tl]
    assert codes.index("P5") < codes.index("P8") < codes.index("P10")
    p8 = next(e for e in tl if e["code"] == "P8")
    assert len(p8["llm"]) == 1 and p8["llm"][0]["model"] == "m-x" and p8["sentence"].startswith("SAR filed")
    assert next(e for e in tl if e["code"] == "P10")["start"] == 12.0


def test_call_lines_never_rewrite_the_model_and_compact_vectors():
    line = common.call_line(_llm("P4", 7.5, "claude-sonnet-4-5"), "HHF-009")
    assert "claude-sonnet-4-5" in line and "gemini" not in line and "[P4]" in line and line.startswith("HHF-009")
    assert "model not recorded" in common.call_line({"kind": "llm", "phase": "P2", "t": 1.0})
    vec = common.call_line(_tool("P1", "query:grounding_chunks", 1.8, params={"q": [0.01] * 1024, "k": 5}))
    assert "<1024-d vector>" in vec and "0.01, 0.01" not in vec and len(vec) < 260
    bad = common.call_line(_tool("P3", "query:ring_profile", 3.0, ok=False, error="timeout after 15 s"))
    assert "FAILED: timeout after 15 s" in bad
    assert len(common.compact_params({"s": "x" * 500}, limit=50)) == 50


def test_scorecard_rules_are_read_from_the_code_or_the_snapshot(monkeypatch):
    yaml = pytest.importorskip("yaml", reason="the policy file is YAML; the Railway image reads the snapshot instead")
    r = common.scorecard_rules()
    assert r["mode"] == "code", r["error"]
    eng = yaml.safe_load((common.ROOT / "policy" / "fraud_policy.yaml").read_text(encoding="utf-8"))["engine"]
    assert r["data"]["engine"]["fraud_band"] == eng["fraud_band"] and r["data"]["engine"]["max_abs_adjustment"] == eng["max_abs_adjustment"]
    floors = r["data"]["detector_floors"]
    assert floors and all(isinstance(d["p_floor"], float) and d["detector_id"] for d in floors)
    from agent.security import clamp_to_engine

    assert clamp_to_engine(1.0, 0.5) == pytest.approx(0.5 + r["data"]["llm_max_move"])
    monkeypatch.setattr(common, "optional_import", lambda name: (None, f"{name} not shipped"))
    fallback = common.scorecard_rules()
    assert fallback["mode"] in ("snapshot", "missing") and (fallback["data"] is not None or "not shipped" in fallback["error"])


def test_case_inspector_and_console_pipeline_render(tree, monkeypatch):
    """The Case file's S0-P10 strip renders every stage (recorded, skipped, S0) without an exception or a write; the Run
    console follows the running case, says the in-flight LLM call is inferred and prints the model calls.jsonl records."""
    pytest.importorskip("streamlit", reason="AppTest needs streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen at render")))
    rows = [_p0("HHF-003") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0, {"p_engine": 0.9, "adjustments": [["ring", 1.0]]}),
            _ph("P4", 4.0, {"fraud_probability": 0.9}), _ph("P5-voi", 4.1, {"ask": False}), _ph("P5", 5.0), _ph("P10", 20.0), _ph("P9", 21.0)]
    cs = [_tool("P0", "query:case_context", 0.3), _llm("P2", 1.5, "model-from-trace"), _llm("P8", 12.0, "model-from-trace")]
    _write_run(tree["runs"], "r-ui", {"HHF-003": _answer("HHF-003", sar=True), "HHF-004": None},
               phases={"HHF-003": rows, "HHF-004": [_p0("HHF-004") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0)]},
               calls={"HHF-003": cs, "HHF-004": [_tool("P3", "query:ring_profile", 2.9, ts="2026-10-31 11:21:30")]})
    at = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "2_case.py"), default_timeout=60)
    at.query_params["run"], at.query_params["case"] = "r-ui", "HHF-003"
    at.session_state["run_id"], at.session_state["case_id"] = "r-ui", "HHF-003"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    strip = next(p for p in at.pills if p.key == "case_stage")
    for code in ("S0", "P3", "P6", "P8", "all"):
        strip.set_value(code).run()
        assert not at.exception, (code, [e.value for e in at.exception])
    strip.set_value("P0").run()
    assert any("query:case_context" in df.value.to_string() for df in at.dataframe), "the P0 stage must list its calls"
    assert not tree["db"].exists()

    at = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "1_queue.py"), default_timeout=60)
    at.session_state["console_job"] = {"run": "r-ui", "cases": ["HHF-003", "HHF-004"], "pid": 0, "backend": "mock", "cmd": [], "log": "", "lock": ""}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = _page_text(at)
    assert "Pipeline · HHF\\-004" in text or "Pipeline · HHF-004" in text, text[:2000]
    console = next(str(c.value) for c in at.code if "(inferred)" in str(c.value))
    assert "HHF-004" in console and "[P4]" in console and "model-from-trace" in console and "gemini" not in console.lower()
    assert "LLM call in flight" in text and not tree["db"].exists()
    lines = console.splitlines()
    assert any(ln.startswith("HHF-003 t+   1.5s [P2]  LLM   model-from-trace") for ln in lines), lines  # a run_console line
    done_at = lines.index("HHF-003 answer.json · verdict fraud · p 0.90")
    assert done_at < next(i for i, ln in enumerate(lines) if ln.startswith("HHF-004 t+")) and lines[-1].endswith("(inferred)")
    status = next(str(m.value) for m in at.markdown if "LLM call in flight" in str(m.value))
    assert "model model-from-trace (from another case's calls.jsonl in this run)" in status and "gemini" not in status.lower(), status
    labels = {s["label"] for s in common.PIPELINE}
    assert len([c for c in at.caption if str(c.value).split(" · ")[0] in labels]) == 24  # the live S0-P10 strip and the replay's
    arch = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "7_architecture.py"), default_timeout=60)
    arch.session_state["run_id"] = "r-ui"
    arch.run()
    assert not arch.exception, [e.value for e in arch.exception]
    spec = arch.get("graphviz_chart")[0].proto.spec
    assert 'llm [label="LLM · model-from-trace"' in spec and "gemini" not in spec.lower()
    # TigerGraph is credited as the platform ("Built on" and its official logo), never as this app's brand
    assert any(str(c.value) == "Built on" for c in arch.caption)
    assert arch.get("image") and common.TIGERGRAPH_LOGO.is_file()
    assert "TigerGraph</text>" not in (common.UI_DIR / "assets" / "mark.svg").read_text(encoding="utf-8")


def test_resumed_case_reads_only_its_newest_attempt(tree):
    """agent.bench --resume appends a rerun to the same files with t restarting at 0: the strip and the timeline follow
    the newest attempt, and the console orders cases by their newest call and marks where the rerun starts."""
    a1 = [_p0("HHF-005") | {"t": 0.0, "at": "2026-10-31 11:28:37"}, _ph("P1", 1.8), _ph("P2", 40.0), _ph("P3", 60.0), _ph("P4", 120.0),
          _ph("P5-voi", 120.1, {"ask": False}), {"kind": "note", "t": 268.5, "text": "case failed", "error": "provider 503"}]
    a2 = [{"kind": "note", "t": 0.0, "text": "case memory excludes agent cases opened at or after this one", "n": 8},
          _p0("HHF-005") | {"t": 0.002, "at": "2026-10-31 11:35:40"}, _ph("P1", 1.8)]
    c1 = [_tool("P0", "query:case_context", 0.4, ts="2026-10-31 11:28:38"), _llm("P5", 268.4, "m-old") | {"ts": "2026-10-31 11:33:05"}]
    c2 = [_tool("P0", "query:case_context", 0.4, ts="2026-10-31 11:35:41"), _tool("P1", "query:similar_prior_cases", 1.5, ts="2026-10-31 11:35:42")]
    done = [_tool("P0", "query:case_context", 0.3, ts="2026-10-31 11:34:10")]
    _write_run(tree["runs"], "r-resume", {"HHF-003": _answer("HHF-003"), "HHF-005": None},
               phases={"HHF-003": [_p0("HHF-003") | {"t": 0.0}, _ph("P9", 9.0)], "HHF-005": a1 + a2}, calls={"HHF-003": done, "HHF-005": c1 + c2})
    v = common.case_view("r-resume", "HHF-005")
    assert v.state == "running" and common.trace_attempts(v) == 2
    ps = common.pipeline_state(v)
    states = {s["code"]: s["state"] for s in ps["stages"]}
    assert (ps["active"], ps["llm"], ps["newest"]["phase"]) == ("P2", True, "P1"), ps  # attempt 2 just finished P1
    assert states["P3"] == states["P4"] == states["P5"] == "pending" and ps["model"] == ""  # attempt 1's P5 row and m-old are ignored
    assert [e["code"] for e in common.trace_timeline(v)] == ["P0", "P1"]
    lines = common.run_console([common.case_view("r-resume", c) for c in ("HHF-005", "HHF-003")], limit=40)
    assert lines[0].startswith("HHF-003") and lines[-1].startswith("HHF-005") and "similar_prior_cases" in lines[-1], lines
    assert any("new attempt" in ln for ln in lines)


def test_after_the_last_decision_the_final_actions_decide_p8_or_p9(tree):
    """p8_sar drafts only when FILE_REPORT is in the final actions: without it the next call is the P9 closing and P8 has
    no row; with it P8 waits on the SAR model; actions not recorded keep the hedge."""
    base = [_p0("HHF-008") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0), _ph("P4", 4.0), _ph("P5-voi", 4.1, {"ask": True}),
            _ph("P5", 5.0, {"actions": [{"action": "STEP_UP_AUTH"}]}), _ph("P6", 6.0)]
    cases = {"HHF-008": None, "HHF-001": None, "HHF-002": None}
    phases = {"HHF-008": [*base, _ph("P7", 9.0, {"actions": [{"action": "CLOSE_NO_FRAUD", "route": "auto"}]})],
              "HHF-001": [*base, _ph("P7", 9.0, {"actions": [{"action": "BLOCK_CARD"}, {"action": "FILE_REPORT"}]})],
              "HHF-002": [*base, _ph("P7", 9.0)]}
    calls = {c: [_llm("P2", 1.5, "m-main")] for c in cases}
    calls["HHF-001"].append(_llm("P8", 0.5, "m-sar") | {"phase": "P8"})  # an earlier SAR row of this case names the SAR model
    _write_run(tree["runs"], "r-p7", cases, phases=phases, calls=calls)
    no_sar = common.pipeline_state(common.case_view("r-p7", "HHF-008"))
    assert (no_sar["active"], no_sar["llm"], no_sar["model"]) == ("P9", True, "m-main")
    assert {s["code"]: s["state"] for s in no_sar["stages"]}["P8"] == "skipped"
    sar = common.pipeline_state(common.case_view("r-p7", "HHF-001"))
    assert (sar["active"], sar["model"]) == ("P8", "m-sar") and "P8 rows" in sar["model_source"]
    unknown = common.pipeline_state(common.case_view("r-p7", "HHF-002"), run_sar_model="m-other")
    assert unknown["active"] == "P8" and unknown["model"] == "" and "different models" in unknown["model_source"]


def test_failed_case_stops_in_the_phase_its_error_names(tree):
    """GraphUnavailable is raised after P2's row is written: the strip stops in P2 (from the error), not the inferred P3."""
    rows = [_p0("HHF-001") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 25.0),
            {"kind": "note", "t": 25.1, "text": "case failed", "error": "GraphUnavailable('graph unavailable during P2 (query:shared_origin_scan)')"}]
    _write_run(tree["runs"], "r-fail", {"HHF-001": None}, phases={"HHF-001": rows}, calls={"HHF-001": [_tool("P2", "query:shared_origin_scan", 24.0, ok=False)]})
    ps = common.pipeline_state(common.case_view("r-fail", "HHF-001"))
    states = {s["code"]: s["state"] for s in ps["stages"]}
    assert (ps["stopped_at"], ps["stopped_basis"]) == ("P2", "error") and states["P2"] == "stopped" and states["P3"] == "pending"
    rows[-1]["error"] = "RuntimeError('provider 503')"  # an LLM failure names no phase: the next stage is inferred
    (tree["runs"] / "r-fail" / "HHF-001" / "phases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    ps = common.pipeline_state(common.case_view("r-fail", "HHF-001"))
    assert (ps["stopped_at"], ps["stopped_basis"]) == ("P3", "inferred")


def test_retry_note_older_than_the_newest_row_is_ignored(tree):
    rows = [_p0("HHF-002") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0), _ph("P4", 9.0), _ph("P5-voi", 9.1, {"ask": False}),
            {"kind": "note", "t": 10.0, "text": "P5: retrying structured output"}, _ph("P5", 20.0, {"actions": [{"action": "CLOSE_NO_FRAUD"}]})]
    _write_run(tree["runs"], "r-retry", {"HHF-002": None}, phases={"HHF-002": rows}, calls={"HHF-002": [_llm("P5", 19.9, "m")]})
    ps = common.pipeline_state(common.case_view("r-retry", "HHF-002"))
    assert ps["active"] == "P9" and "retry" not in ps["waiting"]


def test_detector_floors_follow_the_scan_and_policy_reads_are_fresh(tmp_path, monkeypatch):
    """Only the detectors hit_from_scan tries reach the scorecard, in its order; the policy file is read afresh on every
    call and named with its own time; the snapshot section is the fallback where the code is absent."""
    yaml = pytest.importorskip("yaml", reason="the policy file is YAML")
    from engine import config
    from ops import proof_snapshot

    data, errs = proof_snapshot.sec_scorecard()
    floors = data["detector_floors"]
    assert not errs and [d["detector_id"] for d in floors if d["in_scan"]] == ["D4_RECIPIENT_EMAIL_MULE", "D1_MOBILE_ANON_PROXY_WAVE",
                                                                             "D2_ESCALATING_C_DRAIN", "D3_MICRO_CHARGE_SKIMMING"]
    assert {d["detector_id"] for d in floors if not d["in_scan"]} == {"D5_INPERSON_IDENTITY_CLONE", "D6_NEW_MOBILE_CNP_BURST"}
    copy = tmp_path / "policy.yaml"
    copy.write_text((common.ROOT / "policy" / "fraud_policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config, "POLICY_YAML", copy)
    first = common.scorecard_rules()
    doc = yaml.safe_load(copy.read_text(encoding="utf-8"))
    doc["engine"]["fraud_band"] = 0.75
    copy.write_text(yaml.safe_dump(doc), encoding="utf-8")
    os.utime(copy, (time.time() + 5, time.time() + 5))
    second = common.scorecard_rules()
    assert first["data"]["engine"]["fraud_band"] != 0.75 and second["data"]["engine"]["fraud_band"] == 0.75
    assert second["as_of"] == common.as_of(copy) and str(copy) in second["source"]
    snap = tmp_path / "proof_snapshot.json"
    snap.write_text(json.dumps({"generated_at": "2026-10-31T00:00:00Z", "git_sha": "abc12345", "sections": {"scorecard": data}, "errors": {}}), encoding="utf-8")
    monkeypatch.setattr(common, "SNAPSHOT_PATH", snap)
    monkeypatch.setattr(common, "optional_import", lambda name: (None, f"{name} not shipped"))
    fallback = common.scorecard_rules()
    assert fallback["mode"] == "snapshot" and fallback["data"]["detector_floors"] == floors and fallback["git_sha"] == "abc12345"


def test_s0_says_not_covered_for_cases_outside_the_scan(tree):
    """The data-quality scan covers the Final Round cases only: an HHG case gets 'not covered', never 'no taint flag'."""
    pytest.importorskip("streamlit", reason="AppTest needs streamlit")
    from streamlit.testing.v1 import AppTest

    rows = [_p0("HHG-014") | {"t": 0.0}, _ph("P1", 1.0), _ph("P9", 2.0)]
    _write_run(tree["runs"], "r-hhg", {"HHG-014": _answer("HHG-014")}, phases={"HHG-014": rows}, calls={"HHG-014": [_tool("P0", "query:case_context", 0.3)]})
    at = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "2_case.py"), default_timeout=60)
    at.session_state["run_id"], at.session_state["case_id"] = "r-hhg", "HHG-014"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = _page_text(at)
    if common.proof("dq")["data"] is not None:
        assert "not covered by the data" in text and "no data\\-quality taint flag on this case" not in text and "no data-quality taint flag on this case" not in text


def test_after_a_sar_draft_the_closing_follows_unless_a_retry_is_noted(tree):
    """p8_sar notes 'SAR validation failed' before every retry: a draft with no newer note is followed by the P9 closing
    on the main model; with the note, a P8 retry on the SAR model."""
    rows = [_p0("HHF-001") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0), _ph("P4", 4.0), _ph("P5-voi", 4.1, {"ask": False}),
            _ph("P5", 5.0, {"actions": [{"action": "FILE_REPORT"}]})]
    cs = [_llm("P2", 1.5, "m-main"), _tool("P8", "ofac_screen", 5.1), _llm("P8", 15.0, "m-sar")]
    _write_run(tree["runs"], "r-sar", {"HHF-001": None}, phases={"HHF-001": rows}, calls={"HHF-001": cs})
    ps = common.pipeline_state(common.case_view("r-sar", "HHF-001"))
    assert (ps["active"], ps["model"]) == ("P9", "m-main") and {s["code"]: s["state"] for s in ps["stages"]}["P8"] == "done"
    rows.append({"kind": "note", "t": 15.01, "text": "SAR validation failed", "attempt": 1, "problems": ["amount mismatch"]})
    (tree["runs"] / "r-sar" / "HHF-001" / "phases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    ps = common.pipeline_state(common.case_view("r-sar", "HHF-001"))
    assert (ps["active"], ps["llm"], ps["model"]) == ("P8", True, "m-sar") and "SAR validator" in ps["waiting"]


def test_sar_checks_resolve_ids_against_the_dataset(monkeypatch, tmp_path):
    """The SAR tab and Approvals check ids with engine.validator.FactsResolver over data/ids.duckdb (what P8 falls back
    to), so the submitted SARs' device-profile subjects and CC-xxxx ids resolve; with no dataset the checks fall back to
    the answer's own lists and the page says so."""
    pytest.importorskip("duckdb", reason="the id checks open data/ids.duckdb read-only")
    if not common.SAR_IDS_DB.exists():
        pytest.skip("data/ids.duckdb is not in this checkout")
    common._SAR_RESOLVERS.clear()
    resolver, where = common.sar_resolver()
    assert resolver is not None and "data/ids.duckdb" in where, where
    unresolved = ("does not resolve", "does not exist in the dataset")
    answers = {c: json.loads((common.ROOT / "cases" / f"{c}.json").read_text(encoding="utf-8")) for c in ("HHF-002", "HHF-004", "HHF-006")}
    for cid, ans in answers.items():
        subj = ans["sar"]["subjects"]
        meta = {"customer_id": subj[0], "card_id": subj[1]}
        fails = [(label, detail) for label, ok, detail in common.sar_checks(ans, meta) if not ok]
        assert not [f for f in fails if any(u in f[1] for u in unresolved)], (cid, fails)
    monkeypatch.setattr(common, "SAR_IDS_DB", tmp_path / "missing_ids.duckdb")
    monkeypatch.setenv("ENGINE_FACTS_DB", str(tmp_path / "missing_engine.duckdb"))
    resolver, where = common.sar_resolver()
    assert resolver is None and "answer's own lists only" in where and "not here" in where
    ans = answers["HHF-004"]
    meta = {"customer_id": ans["sar"]["subjects"][0], "card_id": ans["sar"]["subjects"][1]}
    assert any("does not resolve" in detail for _, ok, detail in common.sar_checks(ans, meta) if not ok)  # the old false FAIL
    common._SAR_RESOLVERS.clear()


def test_follow_ups_attempts_failures_retries_and_model_window(tree):
    """A finished case whose rerun failed shows the attempt that wrote answer.json; a failed SAR draft stops in P8; model
    text echoed in an error never names the stage; the fallback narrative is named; a P8-retry note is a SAR-model call;
    an earlier invocation's rows never name the model when `since` is given."""
    ok = [_p0("HHF-012") | {"t": 0.0, "at": "2026-10-31 10:00:00"}, _ph("P1", 1.0), _ph("P9", 9.0, {"validation": []})]
    rerun = [_p0("HHF-012") | {"t": 0.0, "at": "2026-10-31 12:00:00"}, _ph("P1", 1.0), {"kind": "note", "t": 20.5, "text": "case failed", "error": "x"}]
    _write_run(tree["runs"], "r-f1", {"HHF-012": _answer("HHF-012")}, phases={"HHF-012": ok + rerun},
               calls={"HHF-012": [_tool("P0", "query:case_context", 0.3, ts="2026-10-31 10:00:01"), _tool("P0", "query:card_profile", 0.4, ts="2026-10-31 12:00:01")]})
    v = common.case_view("r-f1", "HHF-012")
    tl = common.trace_timeline(v)
    assert v.state == "done" and [e["code"] for e in tl] == ["P0", "P1", "P9"] and [c["name"] for c in tl[0]["calls"]] == ["query:case_context"]

    sar = [_p0("HHF-001") | {"t": 0.0}, _ph("P1", 1.0), _ph("P2", 2.0), _ph("P3", 3.0), _ph("P4", 4.0), _ph("P5-voi", 4.1, {"ask": False}),
           _ph("P5", 5.0, {"actions": [{"action": "FILE_REPORT"}]}),
           {"kind": "note", "t": 15.01, "text": "P8: retrying structured output"},
           {"kind": "note", "t": 30.0, "text": "case failed", "error": "RuntimeError('P8: structured output failed after 3 attempts')"}]
    _write_run(tree["runs"], "r-f2", {"HHF-001": None}, phases={"HHF-001": sar}, calls={"HHF-001": [_llm("P8", 15.0, "m-sar")]})
    ps = common.pipeline_state(common.case_view("r-f2", "HHF-001"))
    assert (ps["stopped_at"], ps["stopped_basis"]) == ("P8", "error"), ps

    echo = [*sar[:-2], {"kind": "note", "t": 30.0, "text": "case failed",
                        "error": "ValidationError(\"1 validation error for ActionChoice: input_value='{\\\"note\\\": \\\"during P6 we\\\"}'\")"}]
    _write_run(tree["runs"], "r-f3", {"HHF-001": None}, phases={"HHF-001": echo}, calls={"HHF-001": []})
    ps = common.pipeline_state(common.case_view("r-f3", "HHF-001"))
    assert ps["stopped_basis"] == "inferred" and ps["stopped_at"] != "P6"

    fb = [*sar[:-2], {"kind": "note", "t": 40.0, "text": "SAR validation failed", "attempt": 3},
          {"kind": "note", "t": 40.001, "text": "SAR: rag.sar.fallback_narrative used after three failed drafts"}]
    _write_run(tree["runs"], "r-f4", {"HHF-001": None}, phases={"HHF-001": fb}, calls={"HHF-001": [_llm("P8", 15.0, "m-sar") | {"phase": "P8-retry2"} | {"t": 39.9}]})
    ps = common.pipeline_state(common.case_view("r-f4", "HHF-001"))
    assert ps["active"] == "P9" and "fallback narrative" in ps["waiting"] and "passed" not in ps["waiting"]

    rt = [*sar[:-2], {"kind": "note", "t": 25.01, "text": "P8-retry1: retrying structured output"}]
    _write_run(tree["runs"], "r-f5", {"HHF-001": None}, phases={"HHF-001": rt},
               calls={"HHF-001": [_llm("P2", 1.5, "m-main"), _llm("P8", 25.0, "m-sar") | {"phase": "P8-retry1"}]})
    v = common.case_view("r-f5", "HHF-001")
    assert (common.pipeline_state(v)["active"], common.pipeline_state(v)["model"]) == ("P8", "m-sar")
    late = common.pipeline_state(v, since="2026-10-31 23:59:59")  # every own row predates this invocation
    assert late["model"] == "" and late["active"] == "P8"


def test_floors_table_lists_only_scanned_detectors():
    """How it works shows the detectors hit_from_scan tries (D4, D1, D2, D3), not D5/D6, and says why."""
    pytest.importorskip("yaml", reason="the scorecard is read from the policy file")
    pytest.importorskip("streamlit", reason="AppTest needs streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "7_architecture.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    floors = next(df.value for df in at.dataframe if "floor" in df.value.columns)
    names = list(floors["floor"])
    assert names.index("D4_RECIPIENT_EMAIL_MULE") < names.index("D1_MOBILE_ANON_PROXY_WAVE") and not any("D5_" in n or "D6_" in n for n in names)
    plain = [str(c.value).replace("\\", "") for c in at.caption]  # md() escapes the underscores
    assert any("not tried by hit_from_scan" in c and "D5_" in c and "D6_" in c for c in plain), plain

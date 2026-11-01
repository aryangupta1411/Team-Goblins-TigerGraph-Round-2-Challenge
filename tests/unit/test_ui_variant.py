"""Challenge 13 on the Data quality & schema page: ui/variant_jobs.py (uploads, the rehearsal job, the mapping decision)
and the CSV variant sub-view of ui/pages/6_schema.py. Nothing here runs the engine: subprocess.Popen is recorded."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import approvals_db, common, variant_jobs

BANK_B = common.ROOT / "data" / "variant_bank_b"


class _Popen:
    calls: list = []

    def __init__(self, cmd, **kw):
        _Popen.calls.append({"cmd": cmd, "env": kw.get("env") or {}, "cwd": kw.get("cwd")})
        self.pid = os.getpid()  # alive, so the lock written for it reads as held


@pytest.fixture
def env(tmp_path, monkeypatch):
    common._load_dotenv()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("DEPLOY_READONLY", "")
    monkeypatch.setenv("RUNS_NEED_APPROVER", "")
    monkeypatch.setenv("APPROVER_DIRECTORY", str(tmp_path / "principals.json"))
    monkeypatch.delenv("APPROVER_DIRECTORY_JSON", raising=False)
    _Popen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    d = tmp_path / "principals.json"
    for who, role in (("ana@bank", "analyst"), ("lead@bank", "team_lead"), ("mgr@bank", "fraud_manager")):
        approvals_db.add_principal(who, role, f"{role} passphrase 1", d)
    sessions = {r: approvals_db.authenticate(w, f"{r} passphrase 1", d) for w, r in (("ana@bank", "analyst"), ("lead@bank", "team_lead"),
                                                                                     ("mgr@bank", "fraud_manager"))}
    return {"runs": runs, "s": sessions}


def test_upload_rules_and_the_job_folder(env, monkeypatch):
    files = {"ledger.csv": b"ref,amount\n1,2\n", "alerts.csv": b"ticket,score\nX,1\n", "ground_truth.json": b'{"ledger.csv": {"ref": "TransactionID"}}'}
    res = variant_jobs.save_upload("bank-c", files)
    assert res["ok"], res
    folder = variant_jobs.input_dir("bank-c")
    assert sorted(p.name for p in folder.iterdir()) == ["alerts.csv", "ground_truth.json", "ledger.csv"]
    assert folder.is_relative_to(env["runs"] / common.UPLOADS_DIR)  # never under data/
    assert [j["id"] for j in variant_jobs.jobs()] == ["bank-c"] and variant_jobs.ground_truth(folder) == {"ledger.csv": {"ref": "TransactionID"}}
    assert "already exists" in variant_jobs.save_upload("bank-c", files)["error"]
    assert "plain .csv name" in variant_jobs.save_upload("d", {"notes.txt": b"x"})["error"]
    assert Path(next(iter({"../../etc/x.csv": 1}))).name == "x.csv"  # a path in an upload name is cut to its last part
    assert "at least one CSV" in variant_jobs.save_upload("e", {"ground_truth.json": b"{}"})["error"]
    assert "not a JSON object" in variant_jobs.save_upload("f", {"a.csv": b"x", "ground_truth.json": b"[1]"})["error"]
    assert "at most" in variant_jobs.save_upload("g", {f"f{i}.csv": b"x" for i in range(variant_jobs.MAX_FILES + 1)})["error"]
    assert "job id" in variant_jobs.save_upload("../up", {"a.csv": b"x"})["error"]
    monkeypatch.setattr(variant_jobs, "MAX_FILE_BYTES", 3)
    assert "over 3 bytes" in variant_jobs.save_upload("h", {"a.csv": b"abcd"})["error"]


def test_gates_read_only_and_approver_roles(env, monkeypatch):
    monkeypatch.setenv("RUNS_NEED_APPROVER", "1")
    for who in (None, env["s"]["analyst"], env["s"]["team_lead"]):
        assert "only a signed-in" in variant_jobs.save_upload("x", {"a.csv": b"x"}, session=who)["error"]
        assert "only a signed-in" in variant_jobs.launch_rehearsal("x", session=who)["error"]
    assert variant_jobs.save_upload("x", {"a.csv": b"x"}, session=env["s"]["fraud_manager"])["ok"]
    monkeypatch.setenv("DEPLOY_READONLY", "1")
    assert "read-only" in variant_jobs.save_upload("y", {"a.csv": b"x"}, session=env["s"]["fraud_manager"])["error"]
    assert "read-only" in variant_jobs.launch_rehearsal("x", session=env["s"]["fraud_manager"])["error"]
    assert "read-only" in variant_jobs.copy_checked_in(session=env["s"]["fraud_manager"])["error"]
    assert _Popen.calls == []


@pytest.mark.skipif(not BANK_B.is_dir(), reason="data/variant_bank_b is not in this checkout")
def test_rehearsal_job_command_state_and_one_at_a_time(env):
    before = sorted(p.name for p in BANK_B.iterdir())
    assert variant_jobs.copy_checked_in()["job"] == "bank-b"
    assert sorted(p.name for p in variant_jobs.input_dir("bank-b").glob("*.csv")) == sorted(p.name for p in BANK_B.glob("*.csv"))
    assert sorted(p.name for p in BANK_B.iterdir()) == before  # the checked-in folder is only read
    assert variant_jobs.rehearsal_state("bank-b")["state"] == "not run"
    res = variant_jobs.launch_rehearsal("bank-b")
    assert res["ok"], res
    cmd = _Popen.calls[-1]["cmd"]
    assert cmd[cmd.index("-m") + 1] == "ops.variant_rehearsal" and cmd[cmd.index("--out") + 1] == str(variant_jobs.output_dir("bank-b"))
    assert variant_jobs.rehearsal_state("bank-b")["state"] == "running"
    assert variant_jobs.save_upload("other", {"a.csv": b"x"})["ok"]
    assert "still running" in variant_jobs.launch_rehearsal("other")["error"]
    job = variant_jobs.job_dir("bank-b")
    (job / variant_jobs.EXIT).write_text("1")  # decisions differed or it failed: the report decides
    assert variant_jobs.rehearsal_state("bank-b")["state"] == "failed"
    (job / "out" / "rehearsal.json").write_text(json.dumps({"n_cases": 1, "n_same_decision": 0, "cases": {}}))
    st = variant_jobs.rehearsal_state("bank-b")
    assert st["state"] == "done" and st["report"]["n_cases"] == 1
    assert variant_jobs.running() is None and variant_jobs.launch_rehearsal("other")["ok"]


def test_mapping_decision_is_final_audited_and_by_role(env):
    variant_jobs.save_upload("bank-c", {"a.csv": b"x"})
    mapping = {"mappings": [{"source_file": "a.csv", "source_column": "x", "canonical_column": "amt", "transform": "identity"}]}
    with pytest.raises(PermissionError):
        variant_jobs.decide_mapping("bank-c", "approved", None, mapping)
    with pytest.raises(PermissionError):
        variant_jobs.decide_mapping("bank-c", "approved", env["s"]["analyst"], mapping)
    with pytest.raises(PermissionError):  # a role sent by the page is not a session
        variant_jobs.decide_mapping("bank-c", "approved", {"principal": "x", "role": "fraud_manager"}, mapping)
    rec = variant_jobs.decide_mapping("bank-c", "rejected", env["s"]["team_lead"], mapping, note="addr1 looks wrong")
    assert rec["status"] == "rejected" and rec["by"] == "lead@bank" and variant_jobs.mapping_decision("bank-c")["mapping_sha"] == variant_jobs.mapping_sha(mapping)
    with pytest.raises(PermissionError, match="already rejected"):
        variant_jobs.decide_mapping("bank-c", "approved", env["s"]["fraud_manager"], mapping)
    changed = {"mappings": [{**mapping["mappings"][0], "canonical_column": "addr1"}]}
    assert variant_jobs.decide_mapping("bank-c", "approved", env["s"]["fraud_manager"], changed)["status"] == "approved"
    outcomes = [r["outcome"] for r in approvals_db.audit_log_rows(common.approvals_db_path()) if str(r.get("action", "")).startswith("variant:")]
    assert outcomes.count("ok") == 2 and "denied_rbac" in outcomes and "denied_auth" in outcomes and "denied_immutable" in outcomes


@pytest.mark.skipif(not BANK_B.is_dir(), reason="data/variant_bank_b is not in this checkout")
def test_csv_variant_view_renders_mapping_review_and_report(env):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    def page():
        at = AppTest.from_file(str(common.ROOT / "ui" / "pages" / "6_schema.py"), default_timeout=90)
        at.session_state["data_view"], at.session_state["data_map_view"] = "map", "CSV variant"
        return at

    at = page()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.proto.body) for h in at.get("html"))
    assert "CSV variant columns onto the canonical tables" in html and "For review" in html and "Run on the variant" in html
    assert any(str(s.label) == "Attested samples for" for s in at.selectbox)
    assert not list((env["runs"]).glob("**/*"))  # viewing writes nothing
    variant_jobs.copy_checked_in()
    report = {"n_cases": 2, "n_same_decision": 2, "generated_at": "2026-11-01T00:00:00Z", "rehearsal_db": "x.duckdb",
              "row_comparison": {"n_translated": 5, "n_in_canonical": 5, "columns": {"card_id": {"equal": 4, "differ": 1, "examples": []}}},
              "accuracy": {"precision": 1.0, "recall": 1.0, "correct": 27, "wrong": [], "missed": []},
              "cases": {"HHF-001": {"variant": {"verdict": "fraud", "status": "escalated", "final_actions": ["BLOCK_CARD"]},
                                    "canonical": {"verdict": "fraud", "status": "escalated", "final_actions": ["BLOCK_CARD"]}, "same": True}}}
    job = variant_jobs.job_dir("bank-b")
    (job / "out").mkdir(exist_ok=True)
    (job / "out" / "rehearsal.json").write_text(json.dumps(report))
    (job / variant_jobs.EXIT).write_text("0")
    at = page()
    at.session_state["data_variant_src"] = "bank-b"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.proto.body) for h in at.get("html"))
    assert "Same decision on 2 of 2 cases" in html and "precision <b>100%</b>" in html and "Mapping decision" in html
    shutil.rmtree(job)

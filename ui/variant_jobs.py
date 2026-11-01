"""Challenge 13 jobs behind the Data quality & schema page's "CSV variant" view.

A job is a folder runs/_uploads/variant-<id>/ (the leading '_' keeps it out of every run listing; nothing is written under
data/): in/ holds the variant's CSVs as uploaded, copied from the checked-in Bank B or generated from the January files,
plus an optional ground_truth.json; out/ is where ops.variant_rehearsal.rehearse writes mapping.json, rehearsal.json and
its scratch facts DB. The rehearsal runs as a detached process with its own log and exit code, one at a time. A mapping
decision (accept / reject) is recorded per job against the mapping it was made on, final once taken, and logged in the
approval audit log (route 'gate'), like the C12 migration gate.

Every guard is here, server-side: refused on a read-only deployment, and where RUNS_NEED_APPROVER is set only a signed-in
role holding 'variant' (fraud manager, server admin) uploads or rehearses; a mapping decision needs 'approve_migration'.
No Streamlit import: the page calls these functions and renders what they return.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ui import approvals_db, common

PREFIX = "variant-"
CHECKED_IN = common.ROOT / "data" / "variant_bank_b"   # read only: copied into a job before it is rehearsed
ROUND2 = common.ROOT / "data" / "raw" / "round2"       # git-ignored: where a fresh test variant is generated from
GROUND_TRUTH = "ground_truth.json"
MAX_FILES = 12
MAX_FILE_BYTES = 50_000_000
MAX_TOTAL_BYTES = 150_000_000
LOCK, LOG, EXIT, DECISION, META = ".rehearse.lock", "rehearse.log", "rehearse.exit", "mapping_decision.json", "job.json"
_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,95}\.csv$", re.I)
_EXIT_WRAPPER = "import subprocess, sys; open(sys.argv[1], 'w').write(str(subprocess.call(sys.argv[2:])))"


# --------------------------------------------------------------------------- #
# Folders                                                                     #
# --------------------------------------------------------------------------- #
def job_dir(job_id: str) -> Path:
    if not _JOB_RE.match(str(job_id or "")):
        raise ValueError("a job id is 1-48 letters, digits, '.', '_' or '-', starting with a letter or digit")
    return common.runs_root() / common.UPLOADS_DIR / f"{PREFIX}{job_id}"


def input_dir(job_id: str) -> Path:
    return job_dir(job_id) / "in"


def output_dir(job_id: str) -> Path:
    return job_dir(job_id) / "out"


def jobs() -> list[dict]:
    """[{id, source, created, files}] of the variant jobs that hold at least one CSV, newest first."""
    root = common.runs_root() / common.UPLOADS_DIR
    try:
        dirs = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX) and any((p / "in").glob("*.csv"))]
    except OSError:
        return []
    out = []
    for p in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True):
        try:
            meta = json.loads((p / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        out.append({"id": p.name[len(PREFIX):], "source": str(meta.get("source") or "upload"), "created": str(meta.get("created") or ""),
                    "files": sorted(f.name for f in (p / "in").glob("*.csv"))})
    return out


def blocked(session: object) -> str:
    """'' when this caller may upload, generate or rehearse here, else why not."""
    if common.read_only():
        return "read-only deployment (DEPLOY_READONLY=1): no variant is uploaded or rehearsed here"
    return common.approver_gate(session, "variant")


def _new_job(job_id: str, source: str, files: list[str]) -> Path:
    job = job_dir(job_id)
    if job.exists():
        raise FileExistsError(f"runs/{common.UPLOADS_DIR}/{PREFIX}{job_id} already exists: choose another name")
    (job / "in").mkdir(parents=True)
    (job / META).write_text(json.dumps({"source": source, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files}, indent=1), encoding="utf-8")
    return job


# --------------------------------------------------------------------------- #
# Bringing a variant in                                                       #
# --------------------------------------------------------------------------- #
def save_upload(job_id: str, files: dict[str, bytes], session: object = None) -> dict:
    """Uploaded CSVs (several, or one flat file) and an optional ground_truth.json into a new job's in/. File names are
    kept as uploaded but must be plain names ending .csv (the mapper reads the folder, so the name is not trusted for
    anything else); at most MAX_FILES files, MAX_FILE_BYTES each and MAX_TOTAL_BYTES together. Returns {ok, error, job}."""
    why = blocked(session)
    if why:
        return {"ok": False, "error": why, "job": ""}
    try:
        job_dir(job_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "job": ""}
    csvs = {Path(n).name: b for n, b in files.items() if Path(n).name.lower() != GROUND_TRUTH}
    truth = next((b for n, b in files.items() if Path(n).name.lower() == GROUND_TRUTH), None)
    bad = [n for n in csvs if not _FILE_RE.match(n)]
    problems = ([f"not a plain .csv name: {', '.join(bad[:5])}"] if bad else []) + (["at least one CSV is needed"] if not csvs else [])
    if len(csvs) > MAX_FILES:
        problems.append(f"{len(csvs)} files; at most {MAX_FILES}")
    big = [n for n, b in csvs.items() if len(b) > MAX_FILE_BYTES]
    if big:
        problems.append(f"over {MAX_FILE_BYTES:,} bytes: {', '.join(big)}")
    if sum(len(b) for b in csvs.values()) > MAX_TOTAL_BYTES:
        problems.append(f"over {MAX_TOTAL_BYTES:,} bytes together")
    if truth is not None:
        try:
            if not isinstance(json.loads(truth.decode("utf-8")), dict):
                raise ValueError("not an object")
        except (UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{GROUND_TRUTH} is not a JSON object of {{file: {{column: canonical}}}}: {exc}")
    if problems:
        return {"ok": False, "error": "; ".join(problems), "job": ""}
    try:
        job = _new_job(job_id, "upload", sorted(csvs))
    except FileExistsError as exc:
        return {"ok": False, "error": str(exc), "job": ""}
    for name, data in csvs.items():
        (job / "in" / name).write_bytes(data)
    if truth is not None:
        (job / "in" / GROUND_TRUTH).write_bytes(truth)
    return {"ok": True, "error": "", "job": job_id}


def copy_checked_in(session: object = None) -> dict:
    """The checked-in Bank B variant (data/variant_bank_b, read only) as job 'bank-b', so its rehearsal writes under runs/."""
    why = blocked(session)
    if why:
        return {"ok": False, "error": why, "job": ""}
    if not CHECKED_IN.is_dir():
        return {"ok": False, "error": f"{common.rel(CHECKED_IN)} is not on this deployment", "job": ""}
    job = job_dir("bank-b")
    if job.exists() and any((job / "in").glob("*.csv")):
        return {"ok": True, "error": "", "job": "bank-b"}
    names = sorted(p.name for p in CHECKED_IN.glob("*.csv"))
    job = _new_job("bank-b", "checked-in", names) if not job.exists() else job
    (job / "in").mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copyfile(CHECKED_IN / n, job / "in" / n)
    return {"ok": True, "error": "", "job": "bank-b"}


def make_test_variant(job_id: str, flat: bool = False, session: object = None) -> dict:
    """A freshly reshaped variant of the January files (ops.variant_rehearsal.make_fresh_variant: new file and column
    names, other units), with its ground_truth.json, as a new job; `flat` puts transactions and device sessions in one file."""
    why = blocked(session)
    if why:
        return {"ok": False, "error": why, "job": ""}
    if not ROUND2.is_dir():
        return {"ok": False, "error": f"{common.rel(ROUND2)} (the January files) is not on this deployment", "job": ""}
    vr, err = common.optional_import("ops.variant_rehearsal")
    if vr is None:
        return {"ok": False, "error": f"ops.variant_rehearsal unavailable: {err}", "job": ""}
    try:
        job = _new_job(job_id, "generated (flat)" if flat else "generated", [])
    except (FileExistsError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "job": ""}
    try:
        src = vr.make_fresh_variant(job / "build", ROUND2)
        if flat:
            src = vr.flatten_variant(src, job / "build_flat")
        for p in Path(src).iterdir():
            if p.suffix.lower() == ".csv" or p.name == GROUND_TRUTH:
                shutil.copyfile(p, job / "in" / p.name)
    except Exception as exc:  # noqa: BLE001 - the reason is shown; the half-made job is removed
        shutil.rmtree(job, ignore_errors=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "job": ""}
    finally:
        for d in ("build", "build_flat"):
            shutil.rmtree(job / d, ignore_errors=True)
    meta = json.loads((job / META).read_text(encoding="utf-8"))
    (job / META).write_text(json.dumps({**meta, "files": sorted(p.name for p in (job / "in").glob("*.csv"))}, indent=1), encoding="utf-8")
    return {"ok": True, "error": "", "job": job_id}


# --------------------------------------------------------------------------- #
# The mapping and its decision                                                #
# --------------------------------------------------------------------------- #
def ground_truth(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / GROUND_TRUTH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mapping_sha(mapping: dict) -> str:
    """A short hash of what the mapping decides (file, column, target, transform), so a changed mapping needs a new decision."""
    rows = sorted((str(m.get("source_file")), str(m.get("source_column")), str(m.get("canonical_column")), str(m.get("transform")))
                  for m in mapping.get("mappings") or [] if isinstance(m, dict))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def mapping_decision(job_id: str) -> dict | None:
    try:
        data = json.loads((job_dir(job_id) / DECISION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def decide_mapping(job_id: str, decision: str, session: object, mapping: dict, note: str = "") -> dict:
    """Accept or reject a job's mapping as the signed-in principal; only a role holding 'approve_migration' may. Final for
    that mapping: a second decision on the same mapping hash is refused. Every attempt goes to the approval audit log
    (route 'gate', gate id 'variant:<job>'). Raises PermissionError or ValueError; returns the record."""
    gate = f"variant:{job_id}"
    actor, role = (getattr(session, "principal", ""), getattr(session, "role", "unauthenticated"))
    db = common.approvals_db_path()
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved or rejected, not {decision!r}")
    try:
        who = approvals_db.verify_session(session)
    except PermissionError as exc:
        approvals_db.audit_gate(gate, decision, actor, role, "denied_auth", str(exc), db)
        raise
    if who.role not in approvals_db.roles_with("approve_migration"):
        msg = f"{who.principal} ({who.role}) cannot decide a mapping: {' or '.join(approvals_db.roles_with('approve_migration'))} only"
        approvals_db.audit_gate(gate, decision, who.principal, who.role, "denied_rbac", msg, db)
        raise PermissionError(msg)
    sha = mapping_sha(mapping)
    prior = mapping_decision(job_id)
    if prior and prior.get("mapping_sha") == sha:
        msg = f"this mapping was already {prior.get('status')} by {prior.get('by')} at {prior.get('at')}"
        approvals_db.audit_gate(gate, decision, who.principal, who.role, "denied_immutable", msg, db)
        raise PermissionError(msg)
    rec = {"status": decision, "by": who.principal, "role": who.role, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "mapping_sha": sha,
           "n_mapped": len(mapping.get("mappings") or []), "n_unmapped": len(mapping.get("unmapped_columns") or []),
           "n_low_confidence": len(mapping.get("low_confidence") or []), "note": str(note or "")[:300]}
    (job_dir(job_id) / DECISION).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    approvals_db.audit_gate(gate, decision, who.principal, who.role, "ok", f"mapping {sha} on the Data quality & schema page", db)
    return rec


# --------------------------------------------------------------------------- #
# The rehearsal                                                               #
# --------------------------------------------------------------------------- #
def rehearsal_command(job_id: str) -> list[str]:
    return [sys.executable, "-m", "ops.variant_rehearsal", str(input_dir(job_id)), "--out", str(output_dir(job_id))]


def running() -> dict | None:
    """The rehearsal still running in any variant job (one at a time), else None."""
    for j in jobs():
        lock = common._read_lock(job_dir(j["id"]) / LOCK)
        if lock and not (job_dir(j["id"]) / EXIT).is_file():
            return {**lock, "job": j["id"]}
    return None


def launch_rehearsal(job_id: str, session: object = None) -> dict:
    """Start ops.variant_rehearsal on the job's in/ into its out/, detached, logging to rehearse.log and recording the exit
    code. Refused where blocked() says so, for a job without CSVs and while another rehearsal runs. Returns {ok, error, pid,
    cmd, log}."""
    out: dict = {"ok": False, "error": "", "pid": None, "cmd": [], "log": ""}
    why = blocked(session)
    if why:
        return {**out, "error": why}
    try:
        job = job_dir(job_id)
    except ValueError as exc:
        return {**out, "error": str(exc)}
    if not any((job / "in").glob("*.csv")):
        return {**out, "error": f"{PREFIX}{job_id} has no CSV to rehearse"}
    held = running()
    if held:
        return {**out, "error": f"the rehearsal of {held.get('job')} is still running (pid {held.get('pid')}); one at a time"}
    for name in (EXIT, "out/rehearsal.json", "out/mapping.json"):
        (job / name).unlink(missing_ok=True)
    (job / "out").mkdir(exist_ok=True)
    cmd = rehearsal_command(job_id)
    with open(job / LOG, "wb") as log:
        proc = subprocess.Popen([sys.executable, "-c", _EXIT_WRAPPER, str(job / EXIT), *cmd], cwd=str(common.ROOT), stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                start_new_session=True)
    (job / LOCK).write_text(json.dumps({"pid": proc.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    return {**out, "ok": True, "pid": proc.pid, "cmd": cmd, "log": common.rel(job / LOG)}


def rehearsal_state(job_id: str) -> dict:
    """{state: 'not run' | 'running' | 'done' | 'failed', report: rehearsal.json or None, log: last lines, started, exit}.
    'done' needs rehearsal.json: ops.variant_rehearsal exits 1 when decisions differ, which is a result, not a failure."""
    job = job_dir(job_id)
    lock = common._read_lock(job / LOCK)
    code: int | None = None
    try:
        code = int((job / EXIT).read_text().strip())
    except (OSError, ValueError):
        pass
    report: dict[str, Any] | None = None
    try:
        report = json.loads((job / "out" / "rehearsal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    try:
        with open(job / LOG, "rb") as fh:
            fh.seek(max(0, (job / LOG).stat().st_size - 16_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()[-30:]
    except OSError:
        tail = []
    if code is None and lock:
        state = "running"
    elif report is not None:
        state = "done"
    elif code is not None:
        state = "failed"
    else:
        state = "not run"
    started = ""
    try:
        started = str(json.loads((job / LOCK).read_text(encoding="utf-8")).get("started") or "")
    except (OSError, ValueError):
        pass
    return {"state": state, "report": report, "log": tail, "started": started, "exit": code}

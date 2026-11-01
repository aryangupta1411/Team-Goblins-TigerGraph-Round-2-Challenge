"""Railway parity: the UI renders from exactly what the Dockerfile copies into the image.

The test reads the Dockerfile's COPY lines, copies those sources (minus what .dockerignore always drops) into tmp_path,
and AppTest-renders ui/app.py and every page in a separate interpreter started with -I from that copy, with
DEPLOY_READONLY=1, RUN_MODE=mock, a tmp APPROVALS_DB and RUNS_DIR=<copy>/runs, as the image starts. The image holds
the whole app (agent, engine, MCP launcher), but of data/ only the checked-in files: the two DuckDB files a live run
reads, no raw CSVs, no work DBs, no .env, no approvals database and no approver directory.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
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
# .dockerignore's "never, even inside the paths let in" block, and the two DuckDB files it lets back in
_NEVER = ("*.env", ".env", ".env.*", "*.sqlite", "*.sqlite-journal", "*.duckdb", "*.wal", "__pycache__", "*.pyc", ".DS_Store",
          "principals.json")
_NEVER_DIRS = ("data/raw", "data/out", "data/out_delta", "data/backup", "data/corpus/raw")
_DUCKDB_KEPT = ("data/hhgoa_engine.duckdb", "data/ids.duckdb")
# generated or gitignored inputs: a clean clone may not have them, and the image then simply lacks them
_OPTIONAL_PREFIXES = ("runs/", "engine/out/")


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _dockerfile_copies() -> list[tuple[str, str]]:
    """(source, destination) for every source of every COPY instruction in the Dockerfile, in order, with ARG defaults
    (ARG NAME=value, as a build without --build-arg uses them) substituted."""
    out: list[tuple[str, str]] = []
    args_default: dict[str, str] = {}

    def sub(text: str) -> str:
        def one(m: re.Match) -> str:
            name = m.group(1) or m.group(2)
            assert name in args_default, f"Dockerfile COPY uses ${name} with no ARG default"
            return args_default[name]

        return _VAR.sub(one, text)

    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if parts and parts[0].upper() == "ARG" and len(parts) > 1 and "=" in parts[1]:
            name, _, value = parts[1].partition("=")
            args_default[name] = value.strip("\"'")
            continue
        if not parts or parts[0].upper() != "COPY":
            continue
        args = [sub(a) for a in parts[1:] if not a.startswith("--")]
        assert len(args) >= 2, f"unparsed COPY line: {line!r}"
        *srcs, dest = args
        out += [(s, dest) for s in srcs]
    return out


def _ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if rel in _DUCKDB_KEPT:
        return False
    return any(fnmatch.fnmatch(path.name, pat) for pat in _NEVER) or any(rel == d or rel.startswith(d + "/") for d in _NEVER_DIRS)


def _copy_one(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=lambda d, names: [n for n in names if _ignored(Path(d) / n)])
    elif not _ignored(src):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _build_image_tree(copy: Path) -> dict:
    """Copy the Dockerfile's file set into `copy` (WORKDIR /app). Returns {copied, missing_optional}."""
    copied, missing = [], []
    for src_s, dest_s in _dockerfile_copies():
        matches = sorted(ROOT.glob(src_s.rstrip("/"))) if any(ch in src_s for ch in "*?[") else [ROOT / src_s.rstrip("/")]
        matches = [m for m in matches if m.exists()]
        if not matches:
            assert src_s.startswith(_OPTIONAL_PREFIXES), f"the Dockerfile copies {src_s}, which does not exist"
            missing.append(src_s)
            continue
        dest_dir = dest_s.endswith("/") or dest_s in (".", "./") or len(matches) > 1
        for m in matches:
            if m.is_dir():
                target = copy / dest_s.rstrip("/") if dest_s not in (".", "./") else copy
            else:
                target = (copy / dest_s / m.name) if dest_dir else copy / dest_s
            _copy_one(m, target)
            copied.append(str(m.relative_to(ROOT)))
    return {"copied": copied, "missing_optional": missing}


RUNNER = r'''
import json, os, sys, subprocess
copy, pages = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, copy)          # -I keeps the cwd and the script folder off sys.path

_Popen = subprocess.Popen
class _NoBench(_Popen):
    def __init__(self, cmd, *a, **k):
        s = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "agent.bench" in s or "agent/bench" in s:
            raise RuntimeError("blocked agent.bench launch in the Railway smoke test: " + s)
        super().__init__(cmd, *a, **k)
subprocess.Popen = _NoBench

import importlib.util
leaks = {}
for name in ("engine", "etl", "graph", "policy", "data", "agent", "schema_engine", "rag", "ops", "ui"):  # repo packages
    spec = importlib.util.find_spec(name)
    origin = (spec.origin or str(list(spec.submodule_search_locations or []))) if spec else None
    if spec is not None and copy not in str(origin):
        leaks[name] = str(origin)

runs = os.path.join(copy, "runs")
runs_before = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
from streamlit.testing.v1 import AppTest
failures, rendered = {}, []
for page in pages:
    path = os.path.join(copy, page)
    if not os.path.isfile(path):
        failures[page] = ["page file missing from the image"]
        continue
    at = AppTest.from_file(path, default_timeout=180)
    at.run()
    if at.exception:
        failures[page] = [str(e.value) for e in at.exception]
    rendered.append(page)

from ui import common
proofs = {n: [common.proof(n)["mode"], common.proof(n)["error"]] for n in (*common._PROOF_FILES, *common.SNAPSHOT_SECTIONS)}
runs_after = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
print("RESULT " + json.dumps({
    "failures": failures, "rendered": rendered, "leaks": leaks, "proofs": proofs,
    "approvals_db_created": os.path.exists(os.environ["APPROVALS_DB"]),
    "runs_changed": runs_before != runs_after, "default_source": common.default_source(),
    "sources": [s.id for s in common.sources()],
}))
'''


def test_ui_renders_from_the_dockerfile_file_set(tmp_path):
    pytest.importorskip("streamlit", reason="the Railway smoke test renders with streamlit.testing (CI installs it)")
    copy = tmp_path / "app"
    copy.mkdir()
    built = _build_image_tree(copy)

    # the whole app, and of data/ only the checked-in files: no raw CSVs, no work DBs, no secrets, no local state
    for need in ("ui/app.py", "agent/bench.py", "agent/case_ids.py", "engine/facts_duckdb.py", "mcp/tg_mcp_launcher.py", *_DUCKDB_KEPT):
        assert (copy / need).is_file(), f"the image must hold {need}"
    assert sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*.duckdb")) == sorted(_DUCKDB_KEPT)
    assert not (copy / "data" / "raw").exists() and not list(copy.rglob(".env")) and not list(copy.rglob("*.sqlite"))
    assert not list(copy.rglob("principals.json")) and (copy / "cases").is_dir()

    scripts = tmp_path / "runner"  # outside the copy, so nothing in the copy shadows the runner's imports
    scripts.mkdir()
    script = scripts / "render_all.py"
    script.write_text(RUNNER, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "state" / "approvals.sqlite"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(tmp_path),
        "LANG": os.environ.get("LANG", "C.UTF-8"), "DEPLOY_READONLY": "1", "RUN_MODE": "mock",
        "APPROVALS_DB": str(db), "RUNS_DIR": str(copy / "runs"),
        "STREAMLIT_SERVER_HEADLESS": "true", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    }
    proc = subprocess.run([sys.executable, "-I", str(script), str(copy), json.dumps(PAGES)], cwd=str(copy), env=env,
                          capture_output=True, text=True, timeout=1200)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert proc.returncode == 0 and line, f"runner failed (rc {proc.returncode})\nstdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    res = json.loads(line[len("RESULT "):])

    assert res["leaks"] == {}, f"modules resolved outside the image copy: {res['leaks']}"
    assert res["failures"] == {}, json.dumps(res["failures"], indent=1)
    assert res["rendered"] == PAGES
    assert not res["approvals_db_created"], "a page created the approvals database on view"
    assert not res["runs_changed"], "a page created or removed a folder under runs/"
    for name, (mode, error) in res["proofs"].items():
        assert mode in ("file", "snapshot", "missing"), (name, mode)
        assert mode != "missing" or error, f"proof section {name} is missing without saying why"
    if not built["missing_optional"]:
        assert res["default_source"] in res["sources"]

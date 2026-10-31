"""Railway parity: the UI renders from exactly what the Dockerfile copies into the image.

The test reads the Dockerfile's COPY lines, copies those sources (minus what .dockerignore always drops) into tmp_path,
and AppTest-renders ui/app.py and every page in a separate interpreter started with -I from that copy, with
DEPLOY_READONLY=1, RUN_MODE=mock, a tmp APPROVALS_DB, RUNS_DIR=<copy>/runs, and duckdb and pydantic blocked
(neither is in requirements-ui.txt). No engine/ code, no data/ and no schema_engine/ beyond what the Dockerfile copies
exist in the copy, so a page that imports them must degrade (snapshot or 'not available in this deployment'), not crash.
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
# .dockerignore's "never, even inside the paths let in" block
_NEVER = ("*.env", ".env", ".env.*", "*.sqlite", "*.sqlite-journal", "*.duckdb", "__pycache__", "*.pyc", ".DS_Store")
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


def _ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _NEVER)


def _copy_one(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=lambda _d, names: [n for n in names if _ignored(n)])
    elif not _ignored(src.name):
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
sys.modules["duckdb"] = None      # not in requirements-ui.txt: `import duckdb` raises ImportError, as in the image
sys.modules["pydantic"] = None
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
    "sources": [s.id for s in common.sources()], "duckdb_blocked": sys.modules.get("duckdb", 0) is None,
}))
'''


def test_ui_renders_from_the_dockerfile_file_set(tmp_path):
    pytest.importorskip("streamlit", reason="the Railway smoke test renders with streamlit.testing (CI installs it)")
    copy = tmp_path / "app"
    copy.mkdir()
    built = _build_image_tree(copy)

    # the image holds no engine code, no data/ and no schema_engine beyond the attestation module
    assert not (copy / "data").exists(), "the image must not hold data/"
    assert not list((copy / "engine").rglob("*.py")), "the image must not hold engine/ code"
    if (copy / "schema_engine").exists():
        assert {p.name for p in (copy / "schema_engine").rglob("*.py")} <= {"__init__.py", "attestation.py"}
    assert not (copy / ".env").exists() and not list(copy.rglob("*.duckdb")) and not list(copy.rglob("*.sqlite"))
    assert (copy / "ui" / "app.py").is_file() and (copy / "cases").is_dir()
    assert not (copy / "ui" / "principals.json").exists()

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

    assert res["duckdb_blocked"]
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

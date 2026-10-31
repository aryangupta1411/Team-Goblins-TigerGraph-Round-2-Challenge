"""ops/proof_snapshot.py — measured proof sections for the read-only UI image.

The Railway image ships no data/, no DuckDB and no engine code, so the proof pages cannot recompute anything there. This
tool runs the real checks once, on a machine that has the data, and writes what they returned:

    uv run python -m ops.proof_snapshot                                   # -> engine/out/proof_snapshot.json
    uv run python -m ops.proof_snapshot --out /tmp/proof_snapshot.json

Sections (ui.common.SNAPSHOT_SECTIONS): attacks, intake_scans, drift, dq, gsql, variant, containment_plans, c3_built, facts,
scorecard.
Each runs in its own try/except. A failed section is left out and its error is recorded as errors[section] = 'Type:
message'; nothing is ever filled in by hand. A section that partly fails (one case of 18) keeps the measured part and
lists the failures in errors[section].

What it never does: run engine.backtest_detectors.run_backtest (engine/out/detector_backtest.json is only read), build
the variant fixture, call an LLM, touch TigerGraph or write anywhere but --out (tmp file + os.replace). DuckDB is opened
read-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "engine" / "out" / "proof_snapshot.json"
BACKTEST = ROOT / "engine" / "out" / "detector_backtest.json"
SCHEMAS_DIR = ROOT / "data" / "schemas"
VARIANT_DIR = ROOT / "data" / "variant_bank_b"
SECTIONS = ("attacks", "intake_scans", "drift", "dq", "gsql", "variant", "containment_plans", "c3_built", "facts", "scorecard")
FACT_TABLES = ("txc", "cc", "cp_round2")
MAX_BYTES = 5 * 1024 * 1024


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _plain(obj: Any) -> Any:
    """A dataclass, pydantic model or plain object as JSON-ready data (json.dumps(default=str) handles the rest)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (dict, list, tuple, str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return vars(obj)


def _rel(p: Path) -> str:
    """Repo-relative when possible (without resolving symlinks, so a staging copy still prints data/...)."""
    for a, b in ((p, ROOT), (p.resolve(), ROOT.resolve())):
        try:
            return str(a.relative_to(b))
        except ValueError:
            continue
    return str(p)


def _facts_db() -> Path:
    p = Path(os.environ.get("ENGINE_FACTS_DB") or ROOT / "data" / "hhgoa_engine.duckdb")
    return p if p.is_absolute() else ROOT / p


# --------------------------------------------------------------------------- #
# Header                                                                      #
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=True).stdout


def _git_root() -> Path | None:
    """The checkout this code runs from. A staging copy whose code dirs are symlinks has no .git of its own, so the
    repository the symlinked agent/ resolves into is tried next."""
    candidates = [ROOT, (ROOT / "agent").resolve().parent]
    for c in dict.fromkeys(candidates):
        try:
            return Path(_git(["rev-parse", "--show-toplevel"], c).strip())
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def header(argv: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    out: dict[str, Any] = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": None,
        "git_dirty": None,
        "git_root": None,
        "command": " ".join(["python", "-m", "ops.proof_snapshot", *argv]),
        "python": platform.python_version(),
        "run_mode": os.environ.get("RUN_MODE", ""),
    }
    root = _git_root()
    if root is None:
        errors["git"] = "git rev-parse failed: no git checkout found for this code"
        return out, errors
    out["git_root"] = str(root)
    try:
        out["git_sha"] = _git(["rev-parse", "HEAD"], root).strip()
        out["git_dirty"] = bool(_git(["status", "--porcelain"], root).strip())
    except (OSError, subprocess.SubprocessError) as exc:
        errors["git"] = _err(exc)
    return out, errors


# --------------------------------------------------------------------------- #
# Sections: each returns (data, partial_errors). Raising means the whole       #
# section failed and is omitted.                                              #
# --------------------------------------------------------------------------- #
def sec_attacks() -> tuple[Any, list[str]]:
    from agent.security import run_attack_ablation

    return run_attack_ablation(), []


def sec_intake_scans() -> tuple[Any, list[str]]:
    from agent.security import sanitize_trigger_text
    from ui.common import round2_pack_rows

    out: dict[str, Any] = {}
    errs: list[str] = []
    for row in sorted(round2_pack_rows(), key=lambda r: str(r.get("case_id") or "")):
        cid = str(row.get("case_id") or "")
        if not cid.startswith("HHF-"):
            continue
        try:
            san = sanitize_trigger_text(cid, str(row.get("trigger_text") or ""))
            out[cid] = {"case_id": cid, "codes": [f.code for f in san.findings], "attack_detected": bool(san.attack_detected), "clean_text": san.clean_text}
        except Exception as exc:  # noqa: BLE001 - one case failing must not hide the other 17
            errs.append(f"{cid}: {_err(exc)}")
    if not out and errs:
        raise RuntimeError("; ".join(errs))
    return out, errs


def sec_drift() -> tuple[Any, list[str]]:
    from schema_engine.drift import detect_round2_drift

    return _plain(detect_round2_drift()), []


def sec_dq() -> tuple[Any, list[str]]:
    from schema_engine.data_quality import run_data_quality_scan

    return _plain(run_data_quality_scan()), []


def _ddl_names(ast: Any) -> set[str]:
    names: set[str] = set()
    for v in ast.vertices.values():
        names.update({v.name, v.primary_id, *[a.name for a in v.attributes]})
    for e in ast.edges.values():
        names.update({e.name, *[a.name for a in e.attributes]})
        if e.reverse_edge:
            names.add(e.reverse_edge)
    return {n for n in names if n}


def sec_gsql() -> tuple[Any, list[str]]:
    from schema_engine.ai_analyst import analyze_gsql_schema
    from schema_engine.gsql_parser import parse_gsql_ddl

    files = sorted(SCHEMAS_DIR.glob("*.gsql"))
    if not files:
        raise FileNotFoundError(f"no *.gsql under {_rel(SCHEMAS_DIR)}")
    out: dict[str, Any] = {}
    errs: list[str] = []
    for p in files:
        try:
            analysis = _plain(analyze_gsql_schema(Path(p), use_llm=False))
            ast = parse_gsql_ddl(Path(p).read_text(encoding="utf-8"))
            names = _ddl_names(ast)
            per_mapping = []
            for m in analysis.get("mappings") or []:
                text = f"{m.get('mapped_to', '')} {m.get('filter_or_path', '')}"
                found = sorted({t for t in re.findall(r"[A-Za-z_]\w*", text) if t in names})
                per_mapping.append({"canonical_target": m.get("canonical_target"), "status": m.get("status"), "mapped_to": m.get("mapped_to"), "ddl_names_found": found})
            analysis["source_file"] = _rel(p)
            analysis["ddl"] = {"graph_name": ast.graph_name, "vertices": sorted(ast.vertices), "edges": sorted(ast.edges), "parser": "schema_engine.gsql_parser.parse_gsql_ddl"}
            analysis["ddl_names_by_mapping"] = per_mapping
            out[p.stem] = analysis
        except Exception as exc:  # noqa: BLE001 - one schema failing must not hide the others
            errs.append(f"{p.name}: {_err(exc)}")
    if not out and errs:
        raise RuntimeError("; ".join(errs))
    return out, errs


def sec_variant() -> tuple[Any, list[str]]:
    if not VARIANT_DIR.is_dir():
        raise FileNotFoundError(f"{_rel(VARIANT_DIR)} does not exist (the variant fixture is never built here)")
    from schema_engine.mapper import infer_variant_mapping

    data = _plain(infer_variant_mapping(VARIANT_DIR))
    if isinstance(data, dict):
        data = {**data, "variant_dir": _rel(VARIANT_DIR)}
    return data, []


def sec_containment_plans() -> tuple[Any, list[str]]:
    import engine.containment as containment

    bt = json.loads(BACKTEST.read_text(encoding="utf-8"))
    scan = bt.get("exam_scan")
    if not isinstance(scan, dict):
        raise ValueError(f"{_rel(BACKTEST)} has no exam_scan mapping")
    cases = [(str(det), str(cid)) for det, ids in scan.items() for cid in (ids or [])]
    out: dict[str, Any] = {}
    errs: list[str] = []
    for det, cid in cases:
        try:
            wave_id, params = containment.wave_for_case(cid)
            if not wave_id:
                errs.append(f"{cid}: wave_for_case found no wave (exam_scan lists it under {det})")
                continue
            as_of = str(params.get("as_of") or "")
            if not as_of:
                errs.append(f"{cid}: wave_for_case returned no opened_at")
                continue
            plan = containment.build_containment_plan(wave_id, as_of=as_of, r_email=params.get("r_email", ""))
            out[cid] = {"case_id": cid, "exam_scan_detector": det, "wave_for_case": {"wave_id": wave_id, **params}, **_plain(plan)}
        except Exception as exc:  # noqa: BLE001 - a case the engine cannot plan is recorded, not invented
            errs.append(f"{cid}: {_err(exc)}")
    if not out and errs:
        raise RuntimeError("; ".join(errs))
    return out, errs


def sec_c3_built() -> tuple[Any, list[str]]:
    import engine.detectors as detectors

    return hasattr(detectors, "compile_policy_rule_detector"), []


def sec_facts() -> tuple[Any, list[str]]:
    import duckdb

    db = _facts_db()
    if not db.exists():
        raise FileNotFoundError(f"{_rel(db)} does not exist")
    out: dict[str, Any] = {"db": _rel(db), "read_only": True}
    errs: list[str] = []
    con = duckdb.connect(str(db), read_only=True)
    try:
        for t in FACT_TABLES:
            sql = f"SELECT count(*) FROM {t}"
            try:
                out[t] = {"count": int(con.execute(sql).fetchone()[0]), "sql": sql}
            except Exception as exc:  # noqa: BLE001 - every duckdb.Error
                errs.append(f"{t}: {_err(exc)}")
    finally:
        con.close()
    if not any(t in out for t in FACT_TABLES):
        raise RuntimeError("; ".join(errs))
    return out, errs


def sec_scorecard() -> tuple[Any, list[str]]:
    """The scorecard's numbers as the code holds them: the policy file's 'engine' block, read afresh (bands, log-odds bumps
    and their clip, floors, the conflict clamp, the post-evidence moves), every module-level DetectorHit(detector_id=,
    name=, p_floor=) in engine/detectors.py (read with ast, never run; a missing p_floor is the dataclass default) marked
    in_scan and ordered as hit_from_scan tries them (only those reach the scorecard, the first match wins), and the LLM
    bound, agent.security.clamp_to_engine's max_move default. The UI reads the same function where the code exists."""
    import ast
    import inspect

    import yaml

    from agent.security import clamp_to_engine
    from engine import config

    errs: list[str] = []
    det_file = ROOT / "engine" / "detectors.py"
    tree = ast.parse(det_file.read_text(encoding="utf-8"))
    default = next((n.value.value for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "DetectorHit" for n in c.body
                    if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "p_floor" and isinstance(n.value, ast.Constant)), None)
    floors: list[dict[str, Any]] = []
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "DetectorHit"):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            row: dict[str, Any] = {"function": fn.name, "line": node.lineno}
            row.update({k: kw[k].value for k in ("detector_id", "name", "p_floor") if isinstance(kw.get(k), ast.Constant)})
            if "p_floor" not in kw:
                row.update(p_floor=default, p_floor_default=True)
            elif "p_floor" not in row:
                errs.append(f"{fn.name} (engine/detectors.py:{node.lineno}): p_floor is not a literal")
            floors.append(row)
    # the detectors the scorecard can apply are the ones hit_from_scan tries, in its order: the loop over a tuple whose
    # elements call detect_*; an order is claimed only when every element names a detector found above
    def _called(el: ast.AST) -> str:
        return next((c.func.id for c in ast.walk(el) if isinstance(c, ast.Call) and getattr(c.func, "id", "").startswith("detect_")), "")

    scan = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hit_from_scan"), None)
    loops = [n for n in ast.walk(scan) if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple)] if scan else []
    loop = next((n for n in loops if any(_called(el) for el in n.iter.elts)), None)
    order = [_called(el) for el in (loop.iter.elts if loop else [])]
    if order and all(order) and set(order) <= {r["function"] for r in floors}:
        for r in floors:
            r["in_scan"] = r["function"] in order
        floors.sort(key=lambda r: order.index(r["function"]) if r["in_scan"] else len(order))
    else:
        errs.append("engine/detectors.py: hit_from_scan's detector order not found; no order is claimed")
    policy = Path(config.POLICY_YAML).absolute()  # a relative ENGINE_POLICY_YAML means the process cwd, as the engine reads it
    out = {
        "policy_file": _rel(policy),
        "engine": yaml.safe_load(policy.read_text(encoding="utf-8"))["engine"],
        "detectors_file": _rel(det_file),
        "detector_floors": floors,
        "llm_max_move": inspect.signature(clamp_to_engine).parameters["max_move"].default,
        "calibration_file": _rel(Path(config.CALIBRATION_JSON)),
    }
    return out, errs


SECTION_FUNCS = {
    "attacks": sec_attacks,
    "intake_scans": sec_intake_scans,
    "drift": sec_drift,
    "dq": sec_dq,
    "gsql": sec_gsql,
    "variant": sec_variant,
    "containment_plans": sec_containment_plans,
    "c3_built": sec_c3_built,
    "facts": sec_facts,
    "scorecard": sec_scorecard,
}


def build(argv: list[str]) -> dict[str, Any]:
    head, errors = header(argv)
    sections: dict[str, Any] = {}
    timings: dict[str, float] = {}
    for name in SECTIONS:
        t0 = time.perf_counter()
        try:
            data, partial = SECTION_FUNCS[name]()
            sections[name] = data
            if partial:
                errors[name] = "partial: " + "; ".join(partial)
        except Exception as exc:  # noqa: BLE001 - every section stands alone
            errors[name] = _err(exc)
        timings[name] = round(time.perf_counter() - t0, 3)
    return {**head, "sections": sections, "errors": errors, "timings_s": timings}


def write_atomic(payload: dict[str, Any], out: Path) -> int:
    text = json.dumps(payload, indent=1, sort_keys=False, default=str, ensure_ascii=False)
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError(f"snapshot is {len(data):,} bytes, over the {MAX_BYTES:,}-byte limit; not written")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600; the image runs as a non-root user that must be able to read the copied file
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(data)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m ops.proof_snapshot", description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="where to write the snapshot (default engine/out/proof_snapshot.json)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out = out if out.is_absolute() else ROOT / out
    payload = build(argv)
    n = write_atomic(payload, out)
    print(f"wrote {_rel(out)} ({n:,} bytes) at {payload['generated_at']} git {str(payload['git_sha'] or 'unknown')[:8]}{' (uncommitted changes)' if payload['git_dirty'] else ''}")
    for name in SECTIONS:
        state = "ok" if name in payload["sections"] else "MISSING"
        note = payload["errors"].get(name, "")
        print(f"  {name:<18} {state:<8} {payload['timings_s'].get(name, 0):>7.2f} s  {note[:160]}")
    if "git" in payload["errors"]:
        print(f"  git: {payload['errors']['git']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

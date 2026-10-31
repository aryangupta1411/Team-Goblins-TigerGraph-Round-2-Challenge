"""Run console: start agent.bench for a set of Final Round cases and follow it from its run folder.

One form (backend, target run, cases) opens a confirm dialog that shows the exact command, the environment, the side
effects and the measured per-case timing, re-checks the run lock and only then calls common.launch_run, which starts
agent.bench detached, logs to runs/<run>/bench.log and writes runs/<run>/.bench.lock. Progress is read from the run
folder in a fragment that refreshes every 2 s. Nothing starts at render, live backends are disabled outside live mode,
and a read-only deployment shows only a disabled message.
"""
from __future__ import annotations

import os
import re
import shlex
import statistics
import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

md = common.md
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BACKENDS = ("gemini", "cli", "api", "mock")  # 'google' is an alias of gemini in common.LLM_BACKENDS
DIALOG_KEY = "console_dialog"   # the pending launch; kept so the dialog survives its own widget reruns
JOB_KEY = "console_job"         # the run this session launched
GOTO_KEY = "console_goto"       # (case, run) picked in the progress panel; switched to from the script body
LOG_LINES = 60
LOG_TAIL_BYTES = 64 * 1024

ctx = common.page_setup("console")

if common.read_only():
    st.info("Live runs are disabled on this deployment (DEPLOY_READONLY=1). Explain a case from its recorded trace instead.", icon=":material/lock:")
    common.page_link("case", "Open the case file", icon=common.PAGE_ICONS["case"], case=ctx.case_id)
    st.stop()

_pending_goto = st.session_state.pop(GOTO_KEY, None)
if isinstance(_pending_goto, tuple) and len(_pending_goto) == 2:
    common.goto("case", case=_pending_goto[0], run=_pending_goto[1])

common.page_header(
    "Run console",
    "Runs agent.bench detached, one run at a time, and reads every case's state from its run folder.",
    eyebrow="Operator · Stage 2 and Stage 3 live runs",
)


# --------------------------------------------------------------------------- #
# Availability: computed from the environment (names only) and the run folders #
# --------------------------------------------------------------------------- #
def _slug(text: object) -> str:
    """A widget-key-safe slug (common._slug is private)."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "x"


def _safe_id(value: object) -> bool:
    """A run or case id usable as one path segment (mirrors common._safe_id, which is private)."""
    s = str(value or "")
    return bool(s) and "/" not in s and "\\" not in s and s[:1] not in "._"


def _backend_rows(mode: str, mode_reason: str) -> dict[str, dict]:
    """backend -> {ok, reason}. A live backend is disabled outside live mode with the mode's reason (e.g. 'RUN_MODE=mock')."""
    out = {}
    for b in BACKENDS:
        if b == "mock":
            out[b] = {"ok": True, "reason": "MOCK: --mock --llm-backend mock, " + common.backend_status("mock")["reason"]}
        elif mode != "live":
            out[b] = {"ok": False, "reason": mode_reason}
        else:
            out[b] = common.backend_status(b)
    return out


def _protected_runs() -> set[str]:
    """Run folders the console never writes into: the default source and every run the submitted answers were promoted from."""
    promoted = {r for rs in common.promoted_from().values() for r in rs}
    return promoted | {common.default_source()} | set(common.SUBMITTED)


def _console_runs() -> list[str]:
    """Runs this console started (launch_run writes runs/<id>/bench.log; agent.bench run by hand does not), newest first,
    minus the protected ones. Only these may be continued."""
    protected = _protected_runs()
    root = common.runs_root()
    try:
        dirs = [p for p in root.iterdir() if p.is_dir() and (p / "bench.log").is_file()]
    except OSError:
        return []
    dirs = [p for p in dirs if p.name[:1] not in "._" and p.name not in protected]
    return [p.name for p in sorted(dirs, key=lambda p: (p / "bench.log").stat().st_mtime, reverse=True)]


def _lock_text(lock: dict) -> str:
    cases = lock.get("cases") or []
    return (f"run {lock.get('run_id')} · PID {lock.get('pid')} · started {lock.get('started') or 'unrecorded'} · backend {lock.get('backend') or 'unrecorded'}"
            f" · {len(cases)} case(s) · {lock.get('path')}")


mode, mode_reason = common.mode_status()
backends = _backend_rows(mode, mode_reason)
live_ready = [b for b in BACKENDS if b != "mock" and backends[b]["ok"]]  # live mode and the backend ready
key_status = {b: common.backend_status(b) for b in BACKENDS if b != "mock"}  # key / CLI presence, whatever the mode
bench_mod, bench_err = common.optional_import("agent.bench")
held = common.run_lock()

checks = [
    ("Live graph", mode == "live", "RUN_MODE=live and TG_HOST set" if mode == "live" else mode_reason),
    ("LLM backend key", any(r["ok"] for r in key_status.values()), "; ".join(f"{b}: {r['reason']}" for b, r in key_status.items())),
    ("agent/ importable", bench_mod is not None, "agent.bench imports" if bench_mod is not None else str(bench_err)),
    ("No live run in progress", held is None, "no live .bench.lock under runs/" if held is None else _lock_text(held)),
    ("Not read-only", True, "DEPLOY_READONLY is unset"),
]
with st.container(border=True, key="console-availability"):
    st.markdown("**Availability**")
    for name, ok, why in checks:
        st.markdown(f"{common.badge('ready' if ok else 'blocked', 'green' if ok else 'orange')} **{md(name)}** · {md(why)}")
    st.markdown("**Backends**")
    for b in BACKENDS:
        r = backends[b]
        st.markdown(f"{common.badge(b, 'green' if r['ok'] else 'gray')} {md('available' if r['ok'] else 'disabled')} · {md(r['reason'])}")
    if mode != "live":
        # The reason verbatim (st.code does not interpret markdown), so it reads exactly as the setting to change.
        st.caption("Why live backends are disabled")
        st.code(mode_reason, language=None, wrap_lines=True)

# A launch needs agent/ and no live lock; a live backend also needs the live mode and its key (shown above).
form_blockers = []
if bench_mod is None:
    form_blockers.append(f"agent.bench cannot be imported here: {bench_err}")
if held is not None:
    form_blockers.append(f"one run at a time: {_lock_text(held)}")


# --------------------------------------------------------------------------- #
# Run form and the confirm dialog                                             #
# --------------------------------------------------------------------------- #
pack = common.pack_for_source(ctx.source) if ctx.source is not None else []
if not pack:
    pack = common.final_round_pack()[0]
pack_ids = [r["case_id"] for r in pack]
trigger_of = {r["case_id"]: r.get("trigger_type", "") for r in pack}
pack_round = "round1" if pack_ids and all(c.startswith("HHG-") for c in pack_ids) else "final"


def _command(cases: list[str], backend: str, run_id: str, resume: bool) -> list[str]:
    """The argv common.launch_run builds, with the interpreter shown relative to the repo."""
    cmd = [common.rel(sys.executable), "-m", "agent.bench", "--run-id", run_id, "--cases", ",".join(cases), "--llm-backend", backend]
    if backend == "mock":
        cmd.append("--mock")
    if resume:
        cmd.append("--resume")
    return cmd


def _latency_source(round_: str) -> common.Source | None:
    """The newest complete live run of the same round."""
    live = [s for s in common.sources() if s.kind == "run" and s.mode == "live" and s.complete and s.round == round_]
    return max(live, key=lambda s: (s.first_started, s.id)) if live else None


def _latency_stats(src: common.Source) -> list[float]:
    out = []
    for cid in src.cases:
        v = common.case_view(src.id, cid)
        if v.ok:
            try:
                out.append(float(v.answer.get("latency_s")))
            except (TypeError, ValueError):
                pass
    return out


def _target_problem(run_id: str, continuing: bool) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        return "the run id must match ^[A-Za-z0-9._-]{1,64}$"
    if run_id[:1] in "._":
        return "the run id may not start with '.' or '_' (those folders are never runs)"
    if run_id in _protected_runs():
        return f"{run_id} is a recorded source (the default, promoted or submitted answers); the console never writes into it"
    if continuing:
        return "" if run_id in _console_runs() else f"runs/{run_id} was not started from this console, so it cannot be continued"
    if (common.runs_root() / run_id).exists():
        return f"runs/{run_id} already exists: choose a new run id, or pick it under 'Continue an existing run' if this console started it"
    return ""


def _close_dialog() -> None:
    st.session_state.pop(DIALOG_KEY, None)


@st.dialog("Start agent.bench", width="large", on_dismiss=_close_dialog)
def _confirm_launch(cases: list[str], backend: str, run_id: str, resume: bool, continuing: bool) -> None:
    live = backend != "mock"
    problems = []
    lock = common.run_lock()  # re-checked here, not taken from the render that opened the dialog
    if lock is not None:
        problems.append(f"one run at a time: {_lock_text(lock)}")
    status = _backend_rows(*common.mode_status())[backend]
    if not status["ok"]:
        problems.append(f"backend {backend} is disabled: {status['reason']}")
    target = _target_problem(run_id, continuing)
    if target:
        problems.append(target)
    for p in problems:
        st.error(md(p), icon=":material/block:")

    shown = ", ".join(cases[:8]) + (f" and {len(cases) - 8} more" if len(cases) > 8 else "")
    st.markdown(f"{common.badge('LIVE' if live else 'MOCK', 'green' if live else 'gray')} " + md(f"{len(cases)} case(s) into runs/{run_id}: {shown}"))
    st.caption("Command (no secrets)")
    st.code(shlex.join(_command(cases, backend, run_id, resume)), language=None, wrap_lines=True)
    st.caption("Environment set for the child process")
    st.code(f"RUN_MODE={'live' if live else 'mock'}\nLLM_BACKEND={backend}", language=None)

    effects = [
        "starts agent.bench detached in its own session; it keeps running after this page closes",
        f"writes runs/{run_id}/ (answer.json, phases.jsonl, calls.jsonl, sar.md per case, bench.log, .bench.lock)",
        "adds pending rows to the local approvals inbox for L1/L2 actions",
    ]
    if resume:
        effects.append(f"--resume: cases that already have an answer in runs/{run_id} are skipped")
    if live:
        effects += [
            "writes an AgentCase, its CaseEvents and an Approval per L1/L2 action to the shared FraudGraph for each case",
            f"spends LLM tokens on {backend} (a paid API call per phase)",
        ]
    else:
        effects.append("mock graph (DuckDB facts) and the mock model: nothing is written to TigerGraph and no LLM tokens are spent")
    st.markdown("**Side effects**")
    st.markdown("\n".join(f"- {md(x)}" for x in effects))

    if live:
        src = _latency_source(pack_round)
        lat = _latency_stats(src) if src is not None else []
        if lat:
            m1, m2 = st.columns(2)
            kw = {"source": f"runs/{src.id}/*/answer.json latency_s", "as_of": common.as_of(src.dir / "run_manifest.json"),
                  "how": f"over the {len(lat)} answered cases of {src.id}, the newest complete live run of this round"}
            common.measured("Per-case median", common.secs(statistics.median(lat)), container=m1, **kw)
            common.measured("Per-case max", common.secs(max(lat)), container=m2, **kw)
            st.caption(md(f"estimate at the median: about {statistics.median(lat) * len(cases) / 60:.0f} min for {len(cases)} case(s), run one after another"))
        else:
            st.caption("No complete live run of this round to measure per-case timing from")

    ack_text = "I understand this writes to the shared graph and spends LLM tokens" if live else "I understand this writes a run folder and the approvals inbox"
    ack = st.checkbox(ack_text, key="console_ack")
    if st.button("Confirm and start", type="primary", icon=":material/play_arrow:", disabled=bool(problems) or not ack, key="console_confirm"):
        try:
            res = common.launch_run(cases, backend, run_id, resume=resume)
        except OSError as exc:  # Popen itself failed (interpreter missing, folder not writable): shown, never swallowed
            res = {"ok": False, "error": f"agent.bench did not start: {type(exc).__name__}: {exc}"}
        if not res["ok"]:
            st.error(md(res["error"]), icon=":material/error:")
            return
        st.session_state[JOB_KEY] = {"run": run_id, "cases": list(cases), "backend": backend, "pid": res["pid"], "started": time.time(),
                                     "cmd": res["cmd"], "log": res["log"], "lock": res["lock"]}
        _close_dialog()
        st.rerun()


enabled = [b for b in BACKENDS if backends[b]["ok"]]
console_runs = _console_runs()
if "console_target" not in st.session_state:  # seeded once, so typed text survives reruns
    st.session_state["console_target"] = f"r2-{'live' if live_ready else 'mock'}-{time.strftime('%Y%m%d-%H%M')}"
if st.session_state.get("console_continue") not in ["", *console_runs]:
    st.session_state.pop("console_continue", None)

st.markdown("**Start a run**")
triggers = sorted({t for t in trigger_of.values() if t})
want = st.multiselect("Trigger filter", triggers, key="console_trigger", placeholder="All triggers")
listed = [c for c in pack_ids if not want or trigger_of.get(c) in want]
with st.form("console_form", border=True):
    f1, f2 = st.columns([1, 2])
    backend = f1.selectbox("Backend", enabled, key="console_backend", help="Disabled backends and their reasons are listed under Availability")
    target_text = f2.text_input("Target run id", key="console_target", help="A new folder runs/<id>/; letters, digits, '.', '_' and '-', at most 64")
    c1, c2 = st.columns([2, 1])
    continue_run = c1.selectbox("Continue an existing run", ["", *console_runs], key="console_continue",
                                format_func=lambda r: r or "No, start a new run",
                                help="Only runs this console started (they hold bench.log), never a recorded source")
    resume = c2.checkbox("Skip completed (--resume)", value=True, key="console_resume", help="Applies when continuing an existing run")
    cases = st.multiselect(f"Cases ({len(listed)} of {len(pack_ids)} in opened_at order)", listed, default=listed,
                           key=f"console_cases-{_slug('-'.join(want) or 'all')}")
    start = st.form_submit_button("Start run", type="primary", icon=":material/play_arrow:", disabled=bool(form_blockers) or not pack_ids)
for b in form_blockers:
    st.caption(md(b))
if not pack_ids:
    st.info("This source has no case pack on this deployment.")

if start:
    continuing = bool(continue_run)
    run_id = continue_run if continuing else str(target_text or "").strip()
    order = {c: i for i, c in enumerate(pack_ids)}
    chosen = sorted((c for c in cases if c in order), key=order.get)
    problem = ("choose at least one case" if not chosen else "" if backend in enabled else f"backend {backend} is disabled") or _target_problem(run_id, continuing)
    if problem:
        st.error(md(problem), icon=":material/block:")
    else:
        st.session_state.pop("console_ack", None)  # every launch is acknowledged afresh
        st.session_state[DIALOG_KEY] = {"cases": chosen, "backend": backend, "run_id": run_id, "resume": bool(resume and continuing), "continuing": continuing}
if st.session_state.get(DIALOG_KEY):
    _confirm_launch(**st.session_state[DIALOG_KEY])


# --------------------------------------------------------------------------- #
# Progress, read from the run folder                                          #
# --------------------------------------------------------------------------- #
_HOST_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)(?:[^\s/@]+@)?[^\s/:?#'\"]+")
_HOSTNAME = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:io|com|net|org|cloud|ai|dev|app|internal|local)\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_KV_SECRET = re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|authorization)[\w-]*)(\s*[:=]\s*)([\"']?)[^\s\"',;}]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_SHAPES = re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9._-]+)")
_LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|HOST|URL|USERNAME)$")


def _scrub(text: str) -> str:
    """Hosts, URLs, IPs, credentials and token-shaped strings out of a log line; literal values of *_KEY / *_TOKEN /
    *_SECRET / *_PASSWORD / *_HOST / *_URL environment variables are replaced by the variable's name."""
    for name, value in os.environ.items():
        if value and len(value) >= 6 and _SECRET_ENV.search(name.upper()):
            text = text.replace(value, f"<{name}>")
    text = _KEY_SHAPES.sub("<token>", text)
    text = _BEARER.sub(r"\1 <token>", text)
    text = _KV_SECRET.sub(r"\1\2\3<redacted>", text)
    text = _HOST_URL.sub(r"\1<host>", text)
    text = _HOSTNAME.sub("<host>", text)
    text = _IPV4.sub("<ip>", text)
    return _LONG_TOKEN.sub("<token>", text)


def _log_tail(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > LOG_TAIL_BYTES and lines:
        lines = lines[1:]  # the first line may be cut
    return "\n".join(_scrub(line) for line in lines[-LOG_LINES:])


def _newest_age(run_id: str) -> float | None:
    """Seconds since the newest write in runs/<run_id>/ (top-level files and the trace files of each case folder)."""
    d = common.runs_root() / run_id
    stamps = []
    try:
        for p in d.iterdir():
            if p.is_file():
                stamps.append(p.stat().st_mtime)
            elif p.is_dir():
                stamps += [f.stat().st_mtime for f in (p / n for n in (*common.TRACE_FILES, "sar.md")) if f.is_file()]
    except OSError:
        return None
    return max(0.0, time.time() - max(stamps)) if stamps else None


def _target() -> tuple[str | None, str]:
    """The run the console follows: the one this session launched, else the run holding a live lock, else none."""
    job = st.session_state.get(JOB_KEY)
    if isinstance(job, dict) and _safe_id(job.get("run")):
        return str(job["run"]), "launched from this session"
    lock = common.run_lock()
    if lock is not None:
        return str(lock.get("run_id")), "holds a live .bench.lock"
    return None, ""


def _active(run_id: str) -> bool:
    age = _newest_age(run_id)
    return common.run_lock(run_id) is not None or (age is not None and age < common.RUNNING_WINDOW_S)


def _run_cases(run_id: str) -> list[str]:
    """Every case of the run: this session's launch, the lock, the manifest and the case folders, in that order."""
    job = st.session_state.get(JOB_KEY) or {}
    lock = common.run_lock(run_id) or {}
    man = common.run_manifest(run_id)
    case_runs = man.get("case_runs") if isinstance(man.get("case_runs"), dict) else {}
    out: list[str] = []
    for group in ((job.get("cases") if job.get("run") == run_id else None) or [], lock.get("cases") or [], man.get("cases") or [],
                  list(case_runs), common.list_cases_in_run(run_id)):
        out += [str(c) for c in group if str(c) not in out]
    return out


def _elapsed_in_trace(view: common.CaseView) -> float | None:
    ts = []
    for r in (*view.phases, *view.calls):
        try:
            ts.append(float(r.get("t")))
        except (TypeError, ValueError):
            pass
    return max(ts) if ts else None


def _case_row(run_id: str, view: common.CaseView, active: bool) -> dict:
    """One case's state for the panel and the table. A verdict and a latency only for a parsed answer."""
    state = view.state
    label = "queued" if state == "not_run" and active else common.STATE_LABELS.get(state, state)
    row = {"case": view.case_id, "state": label, "verdict": "", "latency": "", "last phase": "", "error": view.error or ""}
    if view.last_phase:
        row["last phase"] = f"{view.last_phase} {common.PHASE_NAMES.get(view.last_phase, '')}".strip()
    if view.ok:
        case = view.answer.get("case") or {}
        row["verdict"] = str(case.get("verdict") or "")
        row["latency"] = common.secs(view.answer.get("latency_s"))
    return row


def _progress(run_id: str, why: str, active: bool) -> None:
    job = st.session_state.get(JOB_KEY) or {}
    lock = common.run_lock(run_id)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with st.container(border=True, key="console-progress"):
        st.markdown(f"**Progress of {md(run_id)}** · {md(why)} · " + (common.badge("active", "green") if active else common.badge("idle", "gray")))
        if lock is not None:
            st.markdown(md(f"PID {lock.get('pid')} alive · started {lock.get('started') or 'unrecorded'} · backend {lock.get('backend') or 'unrecorded'} · lock {lock.get('path')}"))
        elif job.get("run") == run_id:
            st.markdown(md(f"PID {job.get('pid')} from this session's launch · no live lock now: the process is not running"))
        else:
            st.markdown(md("no live lock: no process is running for this run"))
        views = [common.case_view(run_id, c) for c in _run_cases(run_id)]
        rows = [_case_row(run_id, v, active) for v in views]
        counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("done", "running", "queued")}
        bad = sum(1 for r in rows if r["state"] in ("failed", "unreadable", "incomplete"))
        cols = st.columns(4)
        kw = {"source": f"runs/{run_id}/<case>/answer.json, phases.jsonl, calls.jsonl", "as_of": now}
        common.measured("Done", f"{counts['done']} / {len(rows)}", container=cols[0], how="cases whose answer.json parses", **kw)
        common.measured("Running", counts["running"], container=cols[1], how="trace written within 120 s or the lock lists the case, no answer yet", **kw)
        common.measured("Queued", counts["queued"], container=cols[2], how="selected for the run, no folder yet, run active", **kw)
        common.measured("Failed or incomplete", bad, container=cols[3], how="'case failed' note, unreadable answer.json or a trace that stopped", **kw)
        for v, r in zip(views, rows):
            with st.container(horizontal=True, vertical_alignment="center", key=f"console-case-{_slug(v.case_id)}"):
                st.markdown(f"**{md(v.case_id)}**", width="content")
                st.markdown(common.badge(r["state"], common.STATE_COLORS.get(v.state, "gray")), width="content")
                if v.state == "running":
                    st.markdown(md(f"phase {r['last phase'] or 'none yet'} · elapsed {common.secs(_elapsed_in_trace(v))} in the trace · {len(v.calls)} calls"), width="content")
                elif v.ok:
                    st.markdown(f"{common.verdict_badge(r['verdict'])} " + md(r["latency"]), width="content")
                    if st.button("Open audit replay", key=f"console-open-{_slug(v.case_id)}", icon=":material/arrow_forward:", type="tertiary"):
                        st.session_state[GOTO_KEY] = (v.case_id, run_id)
                        st.rerun()
                elif v.state == "incomplete":
                    st.markdown(md(f"stopped at {r['last phase'] or 'no phase'}; newest write {common.secs(v.last_write_age_s)} ago; no answer"), width="content")
            if v.state in ("failed", "unreadable"):
                st.code(v.error, language=None, wrap_lines=True)
        log = common.runs_root() / run_id / "bench.log"
        with st.expander(f"bench.log (last {LOG_LINES} lines, hosts and tokens scrubbed)"):
            tail = _log_tail(log)
            if tail is None:
                st.caption(md(f"{common.rel(log)} not written: the run was not started from this console"))
            else:
                st.code(tail or "(empty)", language=None)
    _target_table(run_id, rows)


def _target_table(run_id: str, rows: list[dict]) -> None:
    st.markdown(f"**Target run {md(run_id)}**")
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch",
                     column_config={"error": st.column_config.TextColumn("error", help="verbatim, from phases.jsonl or the answer check")})
    else:
        st.caption("The run lists no cases yet")
    common.page_link("runs", "Open Runs & consistency", icon=common.PAGE_ICONS["runs"])


@st.fragment(run_every="2s")
def _progress_live(run_id: str, why: str) -> None:
    if not _active(run_id):
        st.rerun()  # the run went idle: redraw the page once without the timer
    _progress(run_id, why, True)


st.divider()
job = st.session_state.get(JOB_KEY)
if isinstance(job, dict):
    with st.container(border=True, key="console-launched"):
        st.success(md(f"Started {len(job.get('cases') or [])} case(s) into run {job.get('run')} with backend {job.get('backend')}: PID {job.get('pid')}"),
                   icon=":material/rocket_launch:")
        st.caption(md(f"log {job.get('log')} · lock {job.get('lock')}"))
        st.code(shlex.join(str(x) for x in job.get("cmd") or []), language=None, wrap_lines=True)

target, why = _target()
if target is None:
    st.caption("No run launched from this session and no live run in progress. Recorded runs are on Runs & consistency.")
    common.page_link("runs", "Open Runs & consistency", icon=common.PAGE_ICONS["runs"])
elif _active(target):
    _progress_live(target, why)
else:
    _progress(target, why, False)

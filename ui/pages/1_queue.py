"""Run console: can a run start now, and how far along is it?

The header says in one sentence whether a run can start, with the individual checks in a 'Why' popover. While a run is
active its progress leads, read from the run folder in a fragment that refreshes every 2 s: one strip of counts, the live
S0 + P0-P10 pipeline strip of the case in progress with its status line and the run's call console (what a running stage
waits on, an LLM call or not, is inferred from the newest phases.jsonl / calls.jsonl row and labelled so; the model is the
one calls.jsonl records), failures verbatim, one per-case table and bench.log folded. The start form (cases first, then
the backend and a New/Continue switch) is open when idle and folded while a run is active; it opens a confirm dialog that
shows the exact command, the environment, the side effects and the measured per-case timing, re-checks the run lock and
only then calls common.launch_run. Nothing starts at render, live backends are disabled outside live mode, and a
read-only deployment shows only a disabled message and the replay. 'Replay a recorded case' (ui/replay.py) plays any
recorded case back from its run folder.
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

from ui import common, design, replay, run_sources  # noqa: E402

md, badge, esc = common.md, common.badge, design.esc
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BACKENDS = ("gemini", "cli", "api", "mock")  # 'google' is an alias of gemini in common.LLM_BACKENDS
DIALOG_KEY = "console_dialog"   # the pending launch; kept so the dialog survives its own widget reruns
JOB_KEY = "console_job"         # the run this session launched
GOTO_KEY = "console_goto"       # (case, run) picked in the progress panel; switched to from the script body
LOG_LINES = 60
LOG_TAIL_BYTES = 64 * 1024
CONSOLE_LINES = 24  # calls.jsonl lines in the pipeline panel's console (newest last)

ctx = common.page_setup("console")

if common.read_only():
    st.info("Live runs are disabled on this deployment (DEPLOY_READONLY=1). Replay a recorded case below, or explain it from its case file.",
            icon=":material/lock:")
    common.page_link("case", "Open the case file", icon=common.PAGE_ICONS["case"], case=ctx.case_id)
    design.html_block(design.section("", "Replay a recorded case", "read from the run folder; nothing runs"))
    replay.render(ctx.source_id, ctx.case_id)
    st.stop()

_pending_goto = st.session_state.pop(GOTO_KEY, None)
if isinstance(_pending_goto, tuple) and len(_pending_goto) == 2:
    common.goto("case", case=_pending_goto[0], run=_pending_goto[1])


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


def _backend_rows(dest: dict) -> dict[str, dict]:
    """backend -> {ok, reason}. A live backend is disabled while this session's runs are mock (common.run_target), with
    the reason (e.g. 'RUN_MODE=mock', 'Mock is selected on the Run console')."""
    out = {}
    for b in BACKENDS:
        if b == "mock":
            out[b] = {"ok": True, "reason": "MOCK: --mock --llm-backend mock, " + common.backend_status("mock")["reason"]}
        elif not dest["live"]:
            out[b] = {"ok": False, "reason": dest["reason"]}
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


dest = common.run_target()  # where a run started here goes: the session's Live switch and workspace (run_sources.mode_panel)
backends = _backend_rows(dest)
live_ready = [b for b in BACKENDS if b != "mock" and backends[b]["ok"]]  # live mode and the backend ready
key_status = {b: common.backend_status(b) for b in BACKENDS if b != "mock"}  # key / CLI presence, whatever the mode
bench_mod, bench_err = common.optional_import("agent.bench")
held = common.run_lock()

checks = [
    ("Live graph", dest["live"], dest["reason"]),
    ("LLM backend key", any(r["ok"] for r in key_status.values()), "; ".join(f"{b}: {r['reason']}" for b, r in key_status.items())),
    ("agent/ importable", bench_mod is not None, "agent.bench imports" if bench_mod is not None else str(bench_err)),
    ("No live run in progress", held is None, "no live .bench.lock under runs/" if held is None else _lock_text(held)),
    ("Not read-only", True, "DEPLOY_READONLY is unset"),
]

# A launch needs agent/ and no live lock; a live backend also needs the live mode and its key (the 'Why' popover).
form_blockers = []
if bench_mod is None:
    form_blockers.append(f"agent.bench cannot be imported here: {bench_err}")
if held is not None:
    form_blockers.append(f"one run at a time: {_lock_text(held)}")


def _readiness_html() -> str:
    """One sentence: can a run start now? The checks behind it are in the 'Why' popover."""
    if held is not None:
        return (f'<p class="tc tc-lead"><span class="ochre">Not now</span>: run <b>{esc(held.get("run_id"))}</b> is in progress, and the console '
                "starts one run at a time.</p>")
    if bench_mod is None:
        return '<p class="tc tc-lead"><span class="red">No</span>: agent.bench cannot be imported here.</p>'
    if live_ready:
        return f'<p class="tc tc-lead"><span class="green">Yes</span>: a live run on <b>{esc(", ".join(live_ready))}</b>, or a mock run.</p>'
    why = f"live backends are off ({dest['reason']})" if not dest["live"] else "no live backend has its key or CLI"
    return f'<p class="tc tc-lead"><span class="green">Yes, a mock run</span>: {esc(why)}.</p>'


def _readiness() -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="small", key="rc-ready"):
        design.html_block(_readiness_html())
        with st.popover("Why", key="rc-why"):
            design.html_block('<table class="tc tc-table rc-why"><tbody>' + "".join(
                f'<tr><td class="rc-ok">{design.glyph("check" if ok else "cross", "ok" if ok else "bad")}</td><td><b>{esc(name)}</b>'
                f'<span class="sub">{esc(why)}</span></td></tr>' for name, ok, why in checks) + "</tbody></table>")
            st.markdown("**Backends**")
            for b in BACKENDS:
                r = backends[b]
                st.markdown(f"{common.badge(b, 'green' if r['ok'] else 'gray')} {md('available' if r['ok'] else 'disabled')} · {md(r['reason'])}")
            if not dest["live"]:
                # The reason verbatim (st.code does not interpret markdown), so it reads exactly as the setting to change.
                st.caption("Why live backends are disabled (switch Mode to Live below)")
                st.code(dest["reason"], language=None, wrap_lines=True)


# --------------------------------------------------------------------------- #
# Run form and the confirm dialog                                             #
# --------------------------------------------------------------------------- #
pack = common.pack_for_source(ctx.source) if ctx.source is not None else []
if not pack:
    pack = common.final_round_pack()[0]
pack_ids = [r["case_id"] for r in pack]
trigger_of = {r["case_id"]: r.get("trigger_type", "") for r in pack}
pack_round = "round1" if pack_ids and all(c.startswith("HHG-") for c in pack_ids) else "final"


def _interp(path: object) -> str:
    """The interpreter relative to the repo, as typed (.venv/bin/python), not resolved through the venv's symlink to an
    absolute path outside it; common.rel resolves first, so it is used only for a path outside the repo."""
    p = Path(str(path))
    try:
        return str(p.relative_to(common.ROOT))
    except ValueError:
        return common.rel(p)


def _shown(cmd: list) -> str:
    """A launched argv for display: the interpreter relative to the repo, the rest verbatim."""
    return shlex.join([_interp(cmd[0]), *(str(x) for x in cmd[1:])]) if cmd else ""


def _command(cases: list[str], backend: str, run_id: str, resume: bool, own_pack: bool = False) -> list[str]:
    """The argv common.launch_run builds, with the interpreter shown relative to the repo."""
    cmd = [_interp(sys.executable), "-m", "agent.bench", "--run-id", run_id, "--cases", ",".join(cases), "--llm-backend", backend]
    if backend == "mock":
        cmd.append("--mock")
    if resume:
        cmd.append("--resume")
    if own_pack:
        cmd += ["--case-pack", f"runs/{run_id}/case_pack.csv"]
    return cmd


def _run_pack(run_id: str) -> list[dict]:
    """The alerts a console run was started with (runs/<id>/case_pack.csv), [] for a run on a source's own pack."""
    path = common.runs_root() / run_id / "case_pack.csv"
    return common.validate_case_pack(path.read_bytes())["rows"] if run_id and path.is_file() else []


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
def _confirm_launch(cases: list[str], backend: str, run_id: str, resume: bool, continuing: bool, pack: list[dict] | None = None) -> None:
    live = backend != "mock"
    now = common.run_target()  # re-read here: the workspace check may have changed since the dialog opened
    own_pack = pack is not None or (continuing and bool(_run_pack(run_id)))
    problems = []
    lock = common.run_lock()  # re-checked here, not taken from the render that opened the dialog
    if lock is not None:
        problems.append(f"one run at a time: {_lock_text(lock)}")
    status = _backend_rows(now)[backend]
    if not status["ok"]:
        problems.append(f"backend {backend} is disabled: {status['reason']}")
    target = _target_problem(run_id, continuing)
    if target:
        problems.append(target)
    for p in problems:
        st.error(md(p), icon=":material/block:")

    shown = ", ".join(cases[:8]) + (f" and {len(cases) - 8} more" if len(cases) > 8 else "")
    where = f" on {common._hostname(now['host'])} ({'another workspace' if now['workspace'] == 'other' else 'the .env workspace'})" if live else ""
    st.markdown(f"{common.badge('LIVE' if live else 'MOCK', 'green' if live else 'gray')} " + md(f"{len(cases)} case(s) into runs/{run_id}{where}: {shown}"))
    if pack is not None:
        st.caption(md(f"Investigating {len(pack)} uploaded or typed alert(s), written to runs/{run_id}/case_pack.csv"))
    st.caption("Command (no secrets)")
    st.code(shlex.join(_command(cases, backend, run_id, resume, own_pack)), language=None, wrap_lines=True)
    st.caption("Environment set for the child process")
    extra = ""
    if live and now["workspace"] == "other":
        extra = f"\nTG_HOST={now['host']}\nTG_GRAPHNAME={now['graph']}\nTG_SECRET=*** (this session's, for this process only)"
    st.code(f"RUN_MODE={'live' if live else 'mock'}\nLLM_BACKEND={backend}{extra}", language=None)

    effects = [
        "starts agent.bench detached in its own session; it keeps running after this page closes",
        f"writes runs/{run_id}/ (answer.json, phases.jsonl, calls.jsonl, sar.md per case, bench.log, .bench.lock)",
        "adds pending rows to the local approvals inbox for L1/L2 actions",
    ]
    if resume:
        effects.append(f"--resume: cases that already have an answer in runs/{run_id} are skipped")
    if live:
        effects += [
            f"writes an AgentCase, its CaseEvents and an Approval per L1/L2 action to FraudGraph on {common._hostname(now['host'])} for each case"
            + (" (the shared live workspace)" if now["workspace"] != "other" else ""),
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
            res = common.launch_run(cases, backend, run_id, resume=resume, session=common.approver_session(), case_pack=pack, target=now)
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
CASES_KEY = f"console_cases-{_slug(pack_round)}"  # one key per pack, so the trigger filter never drops the selection
KINDS = ("New run", "Continue a run")


def _trigger_label(t: str, ids: list[str], trig: dict[str, str]) -> str:
    n = sum(1 for c in ids if trig.get(c) == t)
    return f"{t.replace('_', ' ').capitalize()} · {n}"


def _pick(key: str, ids: list[str]) -> None:
    st.session_state[key] = list(ids)


def _start_form() -> None:
    """What to investigate first (the source's case pack, uploaded or typed alerts), then the cases (trigger chips, quick
    picks, the selection), the New/Continue switch, the backend and the run id in st.form 'console_form'. Start only opens
    the confirm dialog."""
    if "console_target" not in st.session_state:  # seeded once, so typed text survives reruns
        st.session_state["console_target"] = f"r2-{'live' if live_ready else 'mock'}-{time.strftime('%Y%m%d-%H%M')}"
    if st.session_state.get("console_continue") not in console_runs:
        st.session_state.pop("console_continue", None)
    with st.container(border=True, key="rc-start"):
        src, custom = run_sources.case_rows(dest)
        ids = [r["case_id"] for r in custom] if custom is not None else pack_ids
        trig = {r["case_id"]: r.get("trigger_type", "") for r in custom} if custom is not None else trigger_of
        cases_key = f"console_cases-{src}" if custom is not None else CASES_KEY
        if cases_key not in st.session_state:
            st.session_state[cases_key] = list(ids)
        st.session_state[cases_key] = [c for c in st.session_state[cases_key] if c in trig]
        if custom is not None and ids and not st.session_state[cases_key] and st.session_state.get(f"{cases_key}-seen") != ids:
            st.session_state[cases_key] = list(ids)  # a new upload starts fully selected
        st.session_state[f"{cases_key}-seen"] = list(ids)
        triggers = sorted({t for t in trig.values() if t})
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small", key="rc-picks"):
            want = st.pills("Trigger", triggers, selection_mode="multi", key=f"console_trigger-{src}", format_func=lambda t: _trigger_label(t, ids, trig))
            listed = [c for c in ids if not want or trig.get(c) in want]
            st.button(f"Only the {len(listed)} shown" if want else f"All {len(listed)}", key="console_pick_all", on_click=_pick, args=(cases_key, listed), type="tertiary")
            st.button("Clear", key="console_pick_none", on_click=_pick, args=(cases_key, []), type="tertiary")
        picked = st.session_state[cases_key]
        options = [c for c in ids if c in listed or c in picked]  # a selected case stays listed whatever the filter
        st.multiselect(f"Cases · {len(picked)} of {len(ids)} selected, in opened_at order", options, key=cases_key, placeholder="Choose cases")
        kind = st.radio("Run", KINDS, horizontal=True, key="console_kind")
        continuing = kind == KINDS[1]
        with st.form("console_form", border=False, enter_to_submit=False):
            f1, f2 = st.columns([1, 2], vertical_alignment="bottom")
            backend = f1.selectbox("Backend", enabled, key="console_backend")
            off: dict[str, list[str]] = {}
            for b in BACKENDS:
                if not backends[b]["ok"]:
                    off.setdefault(backends[b]["reason"], []).append(b)
            if off:
                f2.caption(md(" · ".join(f"{', '.join(bs)} disabled: {reason}" for reason, bs in off.items())))
            resume = False
            if continuing:
                c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
                continue_run = c1.selectbox("Run to continue", console_runs, key="console_continue", placeholder="No run started from this console",
                                            help="Only runs this console started (they hold bench.log), never a recorded source")
                resume = c2.checkbox("Skip completed (--resume)", value=True, key="console_resume", help="Cases that already have an answer in the run are skipped")
                target_text = ""
            else:
                continue_run = None
                target_text = st.text_input("New run id", key="console_target", help="A new folder runs/<id>/; letters, digits, '.', '_' and '-', at most 64")
            start = st.form_submit_button("Start run", type="primary", icon=":material/play_arrow:", disabled=bool(form_blockers) or not ids)
        for b in form_blockers:
            st.caption(md(b))
        if not ids and custom is None:
            st.info("This source has no case pack on this deployment.")
    if start:
        run_id = str(continue_run or "") if continuing else str(target_text or "").strip()
        order = {c: i for i, c in enumerate(ids)}
        chosen = sorted((c for c in st.session_state.get(cases_key) or [] if c in order), key=order.get)
        run_pack = _run_pack(run_id) if continuing else []
        if run_pack:  # a run started on its own alerts continues on them: every case, the finished ones skipped by --resume
            chosen, custom = [r["case_id"] for r in run_pack], None
        problem = ("choose at least one case" if not chosen else "" if backend in enabled else f"backend {backend} is disabled") or _target_problem(run_id, continuing)
        if problem:
            st.error(md(problem), icon=":material/block:")
        else:
            st.session_state.pop("console_ack", None)  # every launch is acknowledged afresh
            st.session_state[DIALOG_KEY] = {"cases": chosen, "backend": backend, "run_id": run_id, "resume": bool(resume and continuing),
                                            "continuing": continuing, "pack": [r for r in custom if r["case_id"] in chosen] if custom is not None else None}


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


def _focus(views: list[common.CaseView]) -> common.CaseView | None:
    """The case the pipeline panel follows: the running one written last, else the case written last."""
    def age(v: common.CaseView) -> float:
        return v.last_write_age_s if v.last_write_age_s is not None else float("inf")

    running = [v for v in views if v.state == "running"]
    traced = [v for v in views if v.has_trace]
    return min(running or traced, key=age) if (running or traced) else None


def _run_models(run_id: str, views: list[common.CaseView]) -> tuple[str, str]:
    """(model, SAR model): the newest LLM row in the run's calls.jsonl files, for P8 SAR drafts and for every other call.
    While the run's .bench.lock is live only this invocation's rows count (ts at or after the lock's start), so a continued
    run does not borrow an earlier backend's model; with no live lock every row counts."""
    since = str((common.run_lock(run_id) or {}).get("started") or "")
    rows = sorted((c for x in views for c in x.calls if c.get("kind") == "llm" and c.get("model") and str(c.get("ts") or "") >= since),
                  key=lambda c: str(c.get("ts") or ""))
    sar = [c for c in rows if str(c.get("phase") or "").split("-")[0] == "P8"]
    main = [c for c in rows if str(c.get("phase") or "").split("-")[0] != "P8"]
    return (str(main[-1]["model"]) if main else ""), (str(sar[-1]["model"]) if sar else "")


def _pipeline_panel(run_id: str, views: list[common.CaseView]) -> None:
    """The live S0 + P0-P10 strip of the case in progress and the run's call console, from phases.jsonl and calls.jsonl
    only. What a running stage waits on (an LLM call or not) is inferred from the newest row and the phase machine's
    order, and labelled so; the model is the one calls.jsonl records for that kind of call, never assumed."""
    v = _focus(views)
    with st.container(key="console-pipeline"):
        if v is None:
            st.markdown("**Pipeline**")
            st.caption("No case of this run has written a trace yet")
            return
        run_model, run_sar_model = _run_models(run_id, views)
        since = str((common.run_lock(run_id) or {}).get("started") or "")
        ps = common.pipeline_state(v, run_model=run_model, run_sar_model=run_sar_model, since=since)
        pos = [x.case_id for x in views].index(v.case_id) + 1
        st.markdown(f"**Pipeline · {md(v.case_id)}** · case {pos} of {len(views)} · {common.state_badge(v.state)}")
        common.pipeline_strip(ps["stages"], key="console")
        at = ps["active"] or ps["stopped_at"]
        label = next((s["label"] for s in ps["stages"] if s["code"] == at), "")
        newest = ps["newest"] or {}
        inflight = ""
        if ps["active"]:
            what = f"{at} · {label}: waiting on {ps['waiting'] or 'the next step'}"
            if ps["llm"]:
                what += f" · model {ps['model']} (from {ps['model_source']})" if ps["model"] else f" · model {ps['model_source'] or 'not recorded yet'}"
            tag = badge("LLM call in flight · inferred", "blue") if ps["llm"] else badge("inferred", "gray")
            st.markdown(f"{tag} " + md(f"{what} · {common.secs(v.last_write_age_s)} since the newest write"))
            inflight = f"{v.case_id}  …  [{at}] waiting on {ps['waiting'] or 'the next step'}" + (f" ({ps['model']})" if ps["llm"] and ps["model"] else "") + "  (inferred)"
        elif ps["stopped_at"] and ps["stopped_basis"] == "error":
            st.markdown(badge("stopped", "orange") + " " + md(f"in {at} · {label}: the case error names it; newest write {common.secs(v.last_write_age_s)} ago"))
        elif ps["stopped_at"]:
            st.markdown(badge("stopped", "orange") + " " + badge("inferred", "gray") + " " + md(
                f"the trace's last row is {newest.get('kind') or '?'} [{newest.get('phase') or '?'}]; next in agent/phase_machine.py's order: "
                f"{at} · {label}; newest write {common.secs(v.last_write_age_s)} ago"))
        if newest:
            st.caption(md(f"Newest row: {newest.get('kind')} {newest.get('name') or ''} [{newest.get('phase')}] at t+{common.secs(newest.get('t'))}. "
                          "Rows are written when a step completes, so the running or stopped stage is inferred from that row and "
                          "agent/phase_machine.py's order, unless the case error names the phase."))
        if common.trace_attempts(v) > 1:
            st.caption(md(f"{v.case_id}'s folder holds {common.trace_attempts(v)} attempts (a rerun appends to the same files); the strip follows "
                          "the newest, or for a finished case the attempt that wrote answer.json"))
        # newest last and never scrolled out of view: the block grows with its (bounded) line count
        lines = common.run_console(views, limit=CONSOLE_LINES) + ([inflight] if inflight else [])
        st.code("\n".join(lines) or "(no calls recorded yet)", language=None)


def _progress(run_id: str, why: str, active: bool) -> None:
    """One strip of counts, the live pipeline (strip, status line, console), failures verbatim, one per-case table and
    bench.log folded, all read from runs/<run_id>/."""
    job = st.session_state.get(JOB_KEY) or {}
    lock = common.run_lock(run_id)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with st.container(key="rc-progress"):
        design.html_block(design.section("", f"Progress of {run_id}", why, design.tag("active" if active else "idle")))
        views = [common.answer_attempt_view(common.case_view(run_id, c)) for c in _run_cases(run_id)]  # one attempt per case
        rows = [_case_row(run_id, v, active) for v in views]
        counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("done", "running", "queued")}
        bad = sum(1 for r in rows if r["state"] in ("failed", "unreadable", "incomplete"))
        with st.container(border=True, key="strip-rc-counts"):
            cols = st.columns(4)
        kw = {"source": f"runs/{run_id}/<case>/answer.json, phases.jsonl, calls.jsonl", "as_of": now}
        common.measured("Done", f"{counts['done']} / {len(rows)}", container=cols[0], how="cases whose answer.json parses", **kw)
        common.measured("Running", counts["running"], container=cols[1], how="trace written within 120 s or the lock lists the case, no answer yet", **kw)
        common.measured("Queued", counts["queued"], container=cols[2], how="selected for the run, no folder yet, run active", **kw)
        common.measured("Failed or incomplete", bad, container=cols[3], how="'case failed' note, unreadable answer.json or a trace that stopped", **kw)
        if lock is not None:
            st.caption(md(f"PID {lock.get('pid')} alive · started {lock.get('started') or 'unrecorded'} · backend {lock.get('backend') or 'unrecorded'} · lock {lock.get('path')}"))
        elif job.get("run") == run_id:
            st.caption(md(f"PID {job.get('pid')} from this session's launch · no live lock now: the process is not running"))
        else:
            st.caption("No live lock: no process is running for this run")
        _pipeline_panel(run_id, views)
        for v in [v for v in views if v.error]:  # failures verbatim
            st.markdown(f"**{md(v.case_id)}** {common.state_badge(v.state)}")
            st.code(v.error, language=None, wrap_lines=True)
        table = [{"case": r["case"], "state": r["state"], "verdict": r["verdict"] or "—",
                  "latency": r["latency"] or (f"{common.secs(_elapsed_in_trace(v))} so far" if v.state == "running" else "—"),
                  "last phase": r["last phase"] or "—", "calls": len(v.calls)} for v, r in zip(views, rows)]
        if table:
            st.dataframe(table, hide_index=True, width="stretch",
                         column_config={"latency": st.column_config.TextColumn("latency", help="answer.json latency_s; for a running case, the newest t in its trace"),
                                        "calls": st.column_config.NumberColumn("calls", help="rows in calls.jsonl, this attempt")})
        else:
            st.caption("The run lists no cases yet")
        done_ids = [v.case_id for v in views if v.ok]
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small", key="rc-inspect"):
            if done_ids:
                pick = st.selectbox("Case", done_ids, key="console-inspect-case", width=180)
                if st.button("Inspect phases", key="console-inspect", icon=":material/arrow_forward:",
                             help=md(f"Switches the source to {run_id} and opens the case's Decision path in the Case file: the S0–P10 strip, "
                                     "and each stage's calls, model calls and payload")):
                    st.session_state[GOTO_KEY] = (pick, run_id)
                    st.rerun()
            common.page_link("runs", "Open Runs & consistency", icon=common.PAGE_ICONS["runs"])
        log = common.runs_root() / run_id / "bench.log"
        with st.expander(f"Command and bench.log (last {LOG_LINES} lines, hosts and tokens scrubbed)"):
            if job.get("run") == run_id and job.get("cmd"):
                st.code(_shown(job["cmd"]), language=None, wrap_lines=True)
            tail = _log_tail(log)
            if tail is None:
                st.caption(md(f"{common.rel(log)} not written: the run was not started from this console"))
            else:
                st.code(tail or "(empty)", language=None)


@st.fragment(run_every="2s")
def _progress_live(run_id: str, why: str) -> None:
    if not _active(run_id):
        st.rerun()  # the run went idle: redraw the page once without the timer
    _progress(run_id, why, True)


# --------------------------------------------------------------------------- #
# The page                                                                    #
# --------------------------------------------------------------------------- #
target, why = _target()
active = target is not None and _active(target)
common.page_header("Run console", "Runs agent.bench detached, one run at a time, and reads every case's state from its run folder.",
                   eyebrow=f"{common.ROUND_LABELS.get(pack_round, pack_round)} pack · {len(pack_ids)} cases")
_readiness()
if active:
    _progress_live(target, why)
    with st.expander("Start a run"):
        run_sources.mode_panel()
        _start_form()
else:
    design.html_block(design.section("", "Start a run", "mode and workspace, what to investigate, then the run"))
    run_sources.mode_panel()
    _start_form()
    if target is not None:
        _progress(target, why, False)
if st.session_state.get(DIALOG_KEY):
    _confirm_launch(**st.session_state[DIALOG_KEY])
if target is None:
    st.caption("No run launched from this session and no live run in progress; run-level numbers are on Runs & consistency.")
    common.page_link("runs", "Open Runs & consistency", icon=common.PAGE_ICONS["runs"])

run_sources.own_data_panel(dest, _scrub)

design.html_block(design.section("", "Replay a recorded case", "read from the run folder; nothing runs"))
replay.render(ctx.source_id, ctx.case_id)

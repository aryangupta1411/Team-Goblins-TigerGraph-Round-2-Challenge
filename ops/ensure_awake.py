"""Savanna auto-resume hygiene (PLAN §3.6): one shared retry decorator.

Savanna Free-tier workspaces auto-stop when idle and auto-start on the first
request, which is *dropped* (502 Bad Gateway / connection error) while the
workspace resumes (research/gap2 "Auto-resume drops the first request").
Every component that talks to TigerGraph wraps its calls with
``@ensure_awake()`` — the loader (graph/load_all.py), the agent's MCP session
(agent/mcp_client.py), keep_alive.py and the Streamlit UI (ui/common.py).

Backoff: 1, 2, 4, 8, 16, 32, 60, 60 ... seconds, until ``max_wait_s``
(default 240 s = 4 min, PLAN §3.6) is exhausted. Only transient errors are
retried; a GSQL compile error or a 401 is raised immediately.

Usage::

    from ops.ensure_awake import ensure_awake, warm_up, tg_connection

    conn = tg_connection()            # from .env: TG_HOST / TG_GRAPHNAME / TG_SECRET
    warm_up(conn)                     # blocks until getVer() answers (workspace awake)

    @ensure_awake()
    def load_chunk(path): ...

    @ensure_awake(max_wait_s=120)
    async def call_tool(session, name, params): ...
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger("ops.ensure_awake")

# Substrings (lower-cased) that identify a resuming / unreachable workspace.
TRANSIENT_MARKERS: tuple[str, ...] = (
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "gateway timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "temporarily unavailable",
    "max retries exceeded",
    "read timed out",
    "timed out",
    "workspace is resuming",
    "workspace is not ready",
    "server disconnected",
    "cannot connect to host",
)

# Errors that must never be retried even though they subclass OSError.
_NON_TRANSIENT_OS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def is_transient(exc: BaseException) -> bool:
    """True when ``exc`` looks like a resuming / unreachable Savanna workspace."""
    if isinstance(exc, _NON_TRANSIENT_OS):
        return False
    try:  # requests (pyTigerGraph sync client)
        import requests  # type: ignore

        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError):
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", 0) in (502, 503, 504):
                return True
    except ImportError:  # pragma: no cover
        pass
    try:  # aiohttp (pyTigerGraph async client used by tigergraph-mcp)
        import aiohttp  # type: ignore

        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
            return True
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status in (502, 503, 504):
            return True
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in TRANSIENT_MARKERS)


def backoff_delays(max_wait_s: float, first: float = 1.0, cap: float = 60.0) -> Iterator[float]:
    """Yield sleep lengths 1, 2, 4 ... capped at ``cap``, whose sum never exceeds ``max_wait_s``."""
    total = 0.0
    delay = first
    while total < max_wait_s:
        step = min(delay, cap, max_wait_s - total)
        if step <= 0:
            return
        yield step
        total += step
        delay = min(delay * 2, cap)


def ensure_awake(
    max_wait_s: float | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
):
    """Decorator: retry a sync or async callable while the workspace resumes.

    ``max_wait_s`` defaults to ``TG_RESUME_MAX_WAIT_S`` from the environment (240).
    ``on_retry(attempt, delay, exc)`` is called before each sleep (the UI shows a spinner).
    ``sleep`` is injectable for tests (sync path only; async uses ``asyncio.sleep``).
    """
    budget = float(os.getenv("TG_RESUME_MAX_WAIT_S", "240")) if max_wait_s is None else float(max_wait_s)

    def decorate(fn: Callable):
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                attempt = 0
                delays = backoff_delays(budget)
                while True:
                    try:
                        return await fn(*args, **kwargs)
                    except BaseException as exc:  # classified below; non-transient errors re-raise
                        if not is_transient(exc):
                            raise
                        delay = next(delays, None)
                        if delay is None:
                            log.error("%s: workspace still unreachable after %.0fs: %s", fn.__name__, budget, exc)
                            raise
                        attempt += 1
                        log.warning("%s: transient error (%s); retry %d in %.0fs", fn.__name__, exc, attempt, delay)
                        if on_retry:
                            on_retry(attempt, delay, exc)
                        await asyncio.sleep(delay)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            attempt = 0
            delays = backoff_delays(budget)
            while True:
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:  # classified below; non-transient errors re-raise
                    if not is_transient(exc):
                        raise
                    delay = next(delays, None)
                    if delay is None:
                        log.error("%s: workspace still unreachable after %.0fs: %s", fn.__name__, budget, exc)
                        raise
                    attempt += 1
                    log.warning("%s: transient error (%s); retry %d in %.0fs", fn.__name__, exc, attempt, delay)
                    if on_retry:
                        on_retry(attempt, delay, exc)
                    sleep(delay)

        return sync_wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Connection helpers shared by ops/, graph/ and ui/ (the agent uses the MCP).  #
# --------------------------------------------------------------------------- #
def tg_connection(graphname: str | None = None, timeout_ms: int | None = None):
    """Build a sync pyTigerGraph 2.0.4 connection from ``.env`` and fetch a token.

    Env: TG_HOST (https://<workspace>.i.tgcloud.io), TG_GRAPHNAME (FraudGraph),
    TG_SECRET (Savanna Database Secret), TG_QUERY_TIMEOUT_MS (120000).
    pyTigerGraph sets tgCloud=True and port 443 itself when the host contains "tgcloud".
    """
    from dotenv import load_dotenv
    from pyTigerGraph import TigerGraphConnection

    load_dotenv()
    host = os.environ["TG_HOST"]
    graph = graphname or os.getenv("TG_GRAPHNAME", "FraudGraph")
    secret = os.environ["TG_SECRET"]
    conn = TigerGraphConnection(host=host, graphname=graph, gsqlSecret=secret)
    ms = int(os.getenv("TG_QUERY_TIMEOUT_MS", "120000")) if timeout_ms is None else int(timeout_ms)
    # Connection-wide GSQL-TIMEOUT / RESPONSE-LIMIT headers (pyTigerGraph merges them into every request).
    conn.customizeHeader(timeout=ms, responseSize=64_000_000)
    _get_token(conn, secret)
    return conn


@ensure_awake()
def _get_token(conn, secret: str) -> None:
    conn.getToken(secret)


@ensure_awake()
def warm_up(conn) -> str:
    """Block until the workspace answers a cheap request; returns the product version string."""
    ver = conn.getVer()
    log.info("TigerGraph awake, version %s", ver)
    return ver


def run_query(conn, name: str, params: dict | None = None, timeout_ms: int | None = None) -> list:
    """``runInstalledQuery`` with retry and an explicit GSQL-TIMEOUT (ms)."""
    ms = int(os.getenv("TG_QUERY_TIMEOUT_MS", "120000")) if timeout_ms is None else int(timeout_ms)

    @ensure_awake()
    def _run():
        return conn.runInstalledQuery(name, params or {}, timeout=ms, usePost=True)

    return _run()

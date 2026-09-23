"""ops/ensure_awake.py: retry only on transient errors, bounded backoff, sync + async."""
from __future__ import annotations

import asyncio

import pytest

from ops.ensure_awake import backoff_delays, ensure_awake, is_transient


def test_backoff_is_geometric_capped_and_bounded():
    d = list(backoff_delays(240))
    assert d[:6] == [1, 2, 4, 8, 16, 32]
    assert max(d) <= 60
    assert abs(sum(d) - 240) < 1e-9


def test_transient_classification():
    assert is_transient(ConnectionError("Connection refused"))
    assert is_transient(RuntimeError("502 Bad Gateway"))
    assert is_transient(Exception("HTTPSConnectionPool: Max retries exceeded"))
    assert not is_transient(ValueError("GSQL syntax error"))
    assert not is_transient(FileNotFoundError("x.csv"))
    assert not is_transient(RuntimeError("401 Unauthorized"))


def test_sync_retries_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    @ensure_awake(max_wait_s=30, sleep=slept.append)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("502 Bad Gateway: workspace is resuming")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert slept == [1, 2]


def test_sync_gives_up_after_budget():
    slept: list[float] = []

    @ensure_awake(max_wait_s=3, sleep=slept.append)
    def always_down():
        raise ConnectionError("503 Service Unavailable")

    with pytest.raises(ConnectionError):
        always_down()
    assert sum(slept) <= 3


def test_non_transient_raises_immediately():
    slept: list[float] = []

    @ensure_awake(max_wait_s=30, sleep=slept.append)
    def bad():
        raise ValueError("bad params")

    with pytest.raises(ValueError):
        bad()
    assert slept == []


def test_async_retries(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    @ensure_awake(max_wait_s=30)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("read timed out")
        return 42

    assert asyncio.run(flaky()) == 42
    assert slept == [1]

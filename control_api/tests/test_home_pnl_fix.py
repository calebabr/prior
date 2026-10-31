"""Home P&L loading fix: cold cache, busy gate, stale-while-revalidate, warm-up, parallel reads, cache bounds.

Timings below use the in-memory FAKE store with artificial delays; they show the structure (sum vs max), not Atlas.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from control_api.routes import analytics as ra
from control_api.tests.test_analytics import CELLS, NOW, _Clock, _put, fill, settle  # noqa: F401


@pytest_asyncio.fixture
async def env(env):
    env.runner.config = {**env.runner.config, "cells": [list(c) for c in CELLS]}
    return env


def _count(store, monkeypatch, name="list_fills"):
    n = {"n": 0}
    orig = getattr(store, name)

    async def f(**kw):
        n["n"] += 1
        return await orig(**kw)
    monkeypatch.setattr(store, name, f)
    return n


async def _settle_tasks():
    for _ in range(5):
        await asyncio.sleep(0.02)


LEDGER = "/api/analytics/ledger?days=30"


@pytest.mark.asyncio
async def test_constants_and_bounds_hold():
    assert ra.CACHE_TTL_S == 300.0 and ra.SUMMARY_TTL_S == 60.0 and ra.REFRESH_AFTER_S == 60.0
    assert ra.ROWS_CACHE_KEYS == 2 and ra.CACHE_MAX_KEYS == 64
    assert ra.BUSY_WAIT_S == 8.0 and ra.STORE_TIMEOUT_S == 10.0


@pytest.mark.asyncio
async def test_cold_ledger_builds_once_then_serves_from_cache(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    reads = _count(env.store, monkeypatch)
    assert (await env.client.get(LEDGER, headers=env.auth)).status_code == 200
    assert reads["n"] == 1
    clock.t += 30
    assert (await env.client.get(LEDGER, headers=env.auth)).status_code == 200
    assert reads["n"] == 1                                            # warm: no big read


@pytest.mark.asyncio
async def test_stale_while_revalidate_serves_stale_and_refreshes_once(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    reads = _count(env.store, monkeypatch)
    first = (await env.client.get(LEDGER, headers=env.auth)).json()
    assert reads["n"] == 1
    await _put(env.store, [fill("b", ts=NOW - timedelta(hours=2), ticker="T-B")])
    clock.t += 120                                                    # past REFRESH_AFTER_S, inside CACHE_TTL_S
    rs = [await env.client.get(LEDGER, headers=env.auth) for _ in range(3)]
    assert [r.status_code for r in rs] == [200] * 3
    assert rs[0].json()["strategies"] == first["strategies"]          # stale copy served at once
    await _settle_tasks()
    assert reads["n"] == 2                                            # exactly ONE background refresh
    clock.t += 1
    after = (await env.client.get(LEDGER, headers=env.auth)).json()
    assert reads["n"] == 2                                            # the refreshed copy is fresh
    assert sum(s["fills"] for s in after["strategies"]) == sum(s["fills"] for s in first["strategies"]) + 1


@pytest.mark.asyncio
async def test_stale_beyond_ttl_blocks_and_rebuilds(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    reads = _count(env.store, monkeypatch)
    await env.client.get(LEDGER, headers=env.auth)
    clock.t += ra.CACHE_TTL_S + 1
    await env.client.get(LEDGER, headers=env.auth)
    assert reads["n"] == 2
    assert len(env.app.state.analytics["rows"]) <= ra.ROWS_CACHE_KEYS


@pytest.mark.asyncio
async def test_trades_and_summary_keep_their_60s_expiry(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    reads = _count(env.store, monkeypatch)
    await env.client.get("/api/analytics/trades", headers=env.auth)
    clock.t += 61
    await env.client.get("/api/analytics/trades", headers=env.auth)
    assert reads["n"] == 2                                            # no stale serving outside the ledger


@pytest.mark.asyncio
async def test_failed_refresh_only_logs_and_keeps_serving(env, monkeypatch, caplog):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    await env.client.get(LEDGER, headers=env.auth)
    clock.t += 120

    async def boom(**kw):
        raise RuntimeError("secret-detail")
    monkeypatch.setattr(env.store, "list_fills", boom)
    caplog.set_level("WARNING")
    r = await env.client.get(LEDGER, headers=env.auth)
    assert r.status_code == 200
    await _settle_tasks()
    assert "background build failed" in caplog.text and "secret-detail" not in caplog.text
    assert (await env.client.get(LEDGER, headers=env.auth)).status_code == 200       # still the stale copy


@pytest.mark.asyncio
async def test_busy_gate_gives_429_analytics_busy_with_retry_after(env, monkeypatch):
    monkeypatch.setattr(ra, "BUSY_WAIT_S", 0.05)
    release = asyncio.Event()
    orig = env.store.list_fills

    async def slow(**kw):
        await release.wait()
        return await orig(**kw)
    monkeypatch.setattr(env.store, "list_fills", slow)
    first = asyncio.create_task(env.client.get("/api/analytics/ledger?days=7", headers=env.auth))
    await asyncio.sleep(0.05)
    r = await env.client.get("/api/analytics/ledger?days=30", headers=env.auth)
    assert r.status_code == 429 and r.json()["detail"] == "analytics_busy" and int(r.headers["Retry-After"]) >= 1
    release.set()
    assert (await first).status_code == 200


@pytest.mark.asyncio
async def test_warm_prebuilds_default_key_and_never_raises(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    reads = _count(env.store, monkeypatch)
    await ra.warm(env.app)
    assert reads["n"] == 1
    assert (await env.client.get(LEDGER, headers=env.auth)).status_code == 200
    assert reads["n"] == 1                                            # Home found the warm copy
    await ra.warm(env.app)
    assert reads["n"] == 1                                            # min gap: not hammered
    clock.t += ra.WARM_MIN_GAP_S + 1

    async def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_fills", boom)
    await ra.warm(env.app)                                            # must not raise


@pytest.mark.asyncio
async def test_warm_includes_last_zone_and_survives_a_broken_app(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await env.client.get("/api/analytics/summary?tz=America/Chicago", headers=env.auth)
    clock.t += 100
    reads = _count(env.store, monkeypatch)
    await ra.warm(env.app)
    assert reads["n"] == 2                                            # UTC key + the last zone
    await ra.warm(object())                                           # nonsense app: logged, not raised


@pytest.mark.asyncio
async def test_runner_post_pass_hook_never_raises_into_the_loop(env):
    def bad():
        raise RuntimeError("x")
    env.runner.post_pass_hook = bad
    env.runner._fire_post_pass_hook()                                 # swallowed
    env.runner.post_pass_hook = None
    env.runner._fire_post_pass_hook()


@pytest.mark.asyncio
async def test_app_wires_the_warm_hook(env):
    assert callable(env.app.state.runner.post_pass_hook)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_ledger_extra_reads_run_in_parallel(env, monkeypatch):
    delay = 0.3
    for name in ("list_arb_opportunities", "list_account_snapshots", "list_cash_flows"):
        orig = getattr(env.store, name)

        def make(orig):
            async def slow(**kw):
                await asyncio.sleep(delay)
                return await orig(**kw)
            return slow
        monkeypatch.setattr(env.store, name, make(orig))
    await env.client.get(LEDGER, headers=env.auth)                    # warm the dataset first
    t0 = time.monotonic()
    r = await env.client.get(LEDGER, headers=env.auth)
    took = time.monotonic() - t0
    assert r.status_code == 200
    assert took < delay * 2, took                                     # ~max (0.3 s), not the sum (0.9 s)


@pytest.mark.asyncio
async def test_parallel_reads_keep_soft_and_hard_failure_rules(env, monkeypatch):
    await env.client.get(LEDGER, headers=env.auth)

    async def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_cash_flows", boom)
    r = await env.client.get(LEDGER, headers=env.auth)
    assert r.status_code == 200 and any("cash flows" in n for n in r.json()["notes"])      # soft: null + note
    monkeypatch.setattr(env.store, "list_arb_opportunities", boom)
    r = await env.client.get(LEDGER, headers=env.auth)
    assert r.status_code == 503 and r.json()["detail"] == "store_unavailable"             # hard read still 503


@pytest.mark.asyncio
async def test_parallel_helper_cancels_siblings_on_failure():
    state = {"cancelled": False}

    async def slow():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def bad():
        raise ValueError("x")
    with pytest.raises(ValueError):
        await ra._parallel(slow(), bad())
    assert state["cancelled"]
    assert await ra._parallel(asyncio.sleep(0, "a"), asyncio.sleep(0, "b")) == ["a", "b"]


@pytest.mark.asyncio
async def test_scorecard_still_works_with_parallel_reads(env):
    r = await env.client.get("/api/analytics/scorecard?days=7", headers=env.auth)
    assert r.status_code == 200 and "notes" in r.json()

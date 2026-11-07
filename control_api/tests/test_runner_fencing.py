"""R1 fencing in the runner, R4 schema check, S-25 telemetry pool and S-27 background cache refresh."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest

from control_api import version as ver
from control_api.db import MemoryStore
from control_api.runner import Runner
from control_api.tests.conftest import BrokenStore

pytestmark = pytest.mark.asyncio


class RecordingAlerter:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", priority="default"):
        self.sent.append((title, body, priority))
        return True


@pytest.fixture
def demo_keys(monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")


def _runner(settings, store, kalshi):
    r = Runner(settings, store, kalshi_factory=lambda m: kalshi)
    r.alerter = RecordingAlerter()
    r.last_monitor_utc = datetime.now(timezone.utc)
    return r


async def _pair(settings, kalshi, store=None):
    store = store or MemoryStore()
    await store.set_state(by="owner", desired="running")
    a, b = _runner(settings, store, kalshi), _runner(settings, store, kalshi)
    await a.rehydrate()
    await b.rehydrate()
    return store, a, b


async def test_standby_never_trades_and_status_says_so(settings, bot_paths, kalshi, demo_keys, monkeypatch):
    store, a, b = await _pair(settings, kalshi)
    assert a.role() == "leader" and b.role() == "standby"
    calls = []
    monkeypatch.setattr(b, "_run_strategy_sync", lambda m: (calls.append(m) or 0, ""))
    assert b.desired == "running"                               # mirrors the store ...
    assert await b.tick("t") == {"ran": False, "reason": "standby"}      # ... but never trades
    assert (await b.run_once("owner"))["reason"] == "standby"
    await b._pass("forced")                                     # even a direct pass sends nothing
    assert calls == [] and b.fence_blocked == 1
    rows = await store.list_journal(limit=10)
    assert any(r.get("event") == "fence_blocked" for r in rows)
    st = b.status()
    assert st["role"] == "standby" and st["lease_holder"] == a.instance_id
    assert st["api_version"] == ver.API_VERSION and st["schema_version"] == ver.SCHEMA_VERSION
    assert "min_app_version" in st and st["lease_generation"] is None
    assert a.status()["role"] == "leader" and a.status()["lease_generation"] == 1
    assert (await b.arb_scan())["reason"] in ("stopped", "standby")


async def test_lease_lost_midway_sends_no_orders(settings, bot_paths, kalshi, demo_keys, monkeypatch):
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    ran = []
    monkeypatch.setattr(r.ms, "main", lambda: ran.append(1) or 0)
    real = r._run_strategy_sync

    def lose_then_run(mode):
        store._lease["holder"] = "someone-else"                  # taken (e.g. after a long store outage) ...
        r.lease.last_renew_mono -= r.lease.ttl_s                 # ... and our local fence has lapsed
        return real(mode)
    monkeypatch.setattr(r, "_run_strategy_sync", lose_then_run)
    await r._pass("t")
    assert ran == [] and r.fence_blocked == 1


async def test_pass_renews_and_refuses_when_store_says_lost(settings, bot_paths, kalshi, demo_keys, monkeypatch):
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    calls = []
    monkeypatch.setattr(r, "_run_strategy_sync", lambda m: (calls.append(m) or 0, ""))
    await r._pass("ok")
    assert calls == ["demo"]
    store._lease["generation"] = 99                              # another instance took over
    await r._pass("again")
    assert calls == ["demo"] and r.role() == "standby"


async def test_arb_execution_is_fenced(settings, bot_paths, kalshi, real_strategy, monkeypatch):
    monkeypatch.setattr(settings, "prod_keys_present", True)
    monkeypatch.setattr(settings, "live_confirm_ok", True)
    r = _runner(settings, MemoryStore(), kalshi)
    await r.rehydrate()
    r.arb_desired, r.arb_mode = "running", "live"
    r.arming["arbitrage"] = True                 # S3 item 10: live arbitrage needs arming
    gen = r._stop_gen
    assert r._arb_live_ok(gen)
    r.lease.last_renew_mono -= r.lease.ttl_s
    assert not r._arb_live_ok(gen)
    assert r._arb_execute_sync(None, None, gen) == "skipped: stopped"


async def test_leader_adopts_a_stop_persisted_by_another_instance(settings, bot_paths, kalshi, demo_keys):
    store, a, b = await _pair(settings, kalshi)
    await b.stop(by="owner")                                    # the Stop request reached the standby
    assert sorted(kalshi.cancelled) == ["o1", "o3"]
    assert a.desired == "running"
    await a._lease_tick()                                       # within one renew interval
    assert a.desired == "stopped" and a.kill_switch()
    assert (await a.tick("t"))["ran"] is False


async def test_stop_on_leader_needs_no_lease_and_no_store(settings, bot_paths, kalshi, demo_keys):
    store, a, b = await _pair(settings, kalshi)
    for r in (a, b):
        r.store = r.lease.store = BrokenStore()
        res = await asyncio.wait_for(r.stop(by="owner"), 5)
        assert res["desired"] == "stopped" and r.kill_switch()
        if r._state_retry:
            r._state_retry.cancel()


async def test_store_flapping_nobody_trades(settings, bot_paths, kalshi, demo_keys, monkeypatch):
    store, a, b = await _pair(settings, kalshi)
    calls = []
    for r in (a, b):
        monkeypatch.setattr(r, "_run_strategy_sync", lambda m: (calls.append(m) or 0, ""))
        r.store = r.lease.store = BrokenStore()
    a.lease.last_renew_mono -= a.lease.ttl_s                    # renews kept failing past the TTL
    for r in (a, b):
        await r._lease_tick()
        assert (await r.tick("t"))["ran"] is False
        await r._pass("forced")
    assert calls == []


async def test_persisted_stop_honoured_by_a_new_leader(settings, bot_paths, kalshi, demo_keys):
    store, a, b = await _pair(settings, kalshi)
    await a.stop(by="owner")
    await a.shutdown()
    await b._lease_tick()
    assert b.role() == "leader" and b.desired == "stopped"
    assert (await b.tick("t"))["ran"] is False


async def test_new_leader_reloads_config_changed_while_standby(settings, bot_paths, kalshi, demo_keys):
    store, a, b = await _pair(settings, kalshi)
    cfg = await store.get_config()
    await store.save_config({**cfg, "run_budget": 33}, before=cfg, by="owner")
    await a.shutdown()
    await b._lease_tick()
    assert b.role() == "leader" and b.config["run_budget"] == 33


# ------------------------------------------------------------------ R4 schema version
async def test_newer_schema_means_standby_and_alert(settings, bot_paths, kalshi):
    store = MemoryStore()
    await store.set_schema_version(ver.SCHEMA_VERSION + 1)
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    assert r.role() == "standby" and (await r.tick("t"))["ran"] is False
    assert await store.get_lease() is None                      # never even tried for the lease
    assert any("schema" in t.lower() for t, _, _ in r.alerter.sent)
    assert await store.get_schema_version() == ver.SCHEMA_VERSION + 1     # never downgraded


@pytest.mark.parametrize("stored", [None, 0])
async def test_older_or_missing_schema_is_written(settings, bot_paths, kalshi, stored):
    store = MemoryStore()
    if stored is not None:
        await store.set_schema_version(stored)
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    assert await store.get_schema_version() == ver.SCHEMA_VERSION and r.role() == "leader"


async def test_runner_disabled_never_takes_the_lease(settings, bot_paths, kalshi):
    settings.runner_enabled = False
    store = MemoryStore()
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    assert r.role() == "standby" and await store.get_lease() is None


# ------------------------------------------------------------------ S-27 / S-25
async def test_startup_does_not_wait_for_the_cache_refresh(settings, bot_paths, kalshi, monkeypatch):
    r = _runner(settings, MemoryStore(), kalshi)
    gate = asyncio.Event()

    async def slow_refresh(*a, **k):
        await gate.wait()
    monkeypatch.setattr(r, "refresh_fill_quality", slow_refresh)
    monkeypatch.setattr(r, "refresh_queue_stats", slow_refresh)
    await asyncio.wait_for(r.rehydrate(), 2)                    # returns while the refresh is still blocked
    assert r.role() == "leader"
    gate.set()
    await asyncio.gather(*list(r._bg), return_exceptions=True)


async def test_telemetry_reads_use_the_dedicated_pool(settings, bot_paths, demo_keys):
    names = []

    class Fake:
        def get_fills(self, min_ts=None):
            names.append(threading.current_thread().name)
            return []

        def get_settlements(self, min_ts=None):
            return []
    r = _runner(settings, MemoryStore(), Fake())
    res = await r.sync_fills("demo", force=True)
    assert res["synced"] is True
    assert names and all(n.startswith("telemetry") for n in names)
    await r.shutdown()


async def test_short_telemetry_clients(monkeypatch):
    import control_api.runner as runmod
    assert runmod.TELEMETRY_CLIENT_TIMEOUT_S <= 10 and runmod.TELEMETRY_CLIENT_MAX_RETRIES <= 1
    pub = runmod.telemetry_public_factory("demo")
    assert pub.max_retries == runmod.TELEMETRY_CLIENT_MAX_RETRIES and pub.timeout == runmod.TELEMETRY_CLIENT_TIMEOUT_S
    for k in ("KALSHI_DEMO_API_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert runmod.telemetry_kalshi_factory("demo") is None


async def test_unconfirmed_renew_before_a_pass_sends_nothing(settings, bot_paths, kalshi, demo_keys, monkeypatch):
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    calls = []
    monkeypatch.setattr(r, "_run_strategy_sync", lambda m: (calls.append(m) or 0, ""))

    async def boom(*a, **k):
        raise RuntimeError("atlas timeout")
    monkeypatch.setattr(store, "renew_lease", boom)
    await r._pass("t")                     # local lease still young, but the store did not confirm it
    assert calls == [] and r.fence_blocked == 1

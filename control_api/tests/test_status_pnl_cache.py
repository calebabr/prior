"""/api/status rolling P&L cache (Runner.pnl_pair): identical values to the direct compute, TTL with a fake clock,
stale-while-revalidate, single flight, invalidation on a fills sync / label backfill and on a mode switch, null on
store errors, and the windows() maths off the event loop. No wall-clock time in any assertion."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as runner_mod
from control_api.services import pnl as pnlsvc
from control_api.tests.test_periods import _fill, _settle

pytestmark = pytest.mark.asyncio
UTC = timezone.utc
FROZEN = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(runner_mod, "utcnow", lambda: FROZEN)


@pytest.fixture
def clock(env):
    c = Clock()
    env.runner._pnl_clock = c
    return c


async def _seed(store, *, extra: int = 0):
    """Maker wins and losses, an arbitrage fill, a manual fill, a live fill, an unusable count and an old settlement."""
    fills = [_fill("M1", "KXDEMOB-1", ts=FROZEN - timedelta(days=2), price=0.4),
             _fill("M2", "KXDEMOB-2", ts=FROZEN - timedelta(days=12), price=0.7),
             _fill("A1", "KXDEMOB-3", ts=FROZEN - timedelta(days=1), price=0.3, strat="arb", fee=0.02),
             _fill("Y1", "KXDEMOB-4", ts=FROZEN - timedelta(days=1), price=0.5, src="you"),
             _fill("L1", "KXDEMOB-1", ts=FROZEN - timedelta(days=2), price=0.4, e="live"),
             {**_fill("BAD", "KXDEMOB-1", ts=FROZEN - timedelta(days=2)), "count_fp": "nan"},
             _fill("OLD", "KXDEMOB-9", ts=FROZEN - timedelta(days=50), price=0.4)]
    fills += [_fill(f"X{i}", f"KXDEMOB-X{i}", ts=FROZEN - timedelta(days=3), price=0.5) for i in range(extra)]
    await store.upsert_fills(fills)
    await store.upsert_settlements([_settle("KXDEMOB-1", "no", FROZEN - timedelta(days=1)),
                                    _settle("KXDEMOB-2", "yes", FROZEN - timedelta(days=10)),
                                    _settle("KXDEMOB-3", "no", FROZEN - timedelta(hours=5)),
                                    _settle("KXDEMOB-4", "yes", FROZEN - timedelta(hours=5)),
                                    _settle("KXDEMOB-9", "no", FROZEN - timedelta(days=40))]
                                   + [_settle(f"KXDEMOB-X{i}", "no", FROZEN - timedelta(days=2)) for i in range(extra)])


async def _direct(store, env="demo"):
    """Today's computation, written out: unprojected reads and windows() twice."""
    settlements = await store.list_settlements(since=FROZEN - timedelta(days=30), limit=100000)
    if not settlements:
        return None, None
    fills = await store.list_fills(since=FROZEN - timedelta(days=60), limit=100000)
    return (pnlsvc.windows(fills, settlements, FROZEN, taker=False, env=env),
            pnlsvc.windows(fills, settlements, FROZEN, taker=True, env=env))


async def _drain(runner):
    t = runner._pnl_pair_task
    if t is not None:
        await asyncio.gather(t, return_exceptions=True)


class CountingStore:
    """Wraps the MemoryStore and counts the P&L reads (optionally holding them on an event, or failing them)."""

    def __init__(self, inner) -> None:
        self.inner, self.reads, self.gate, self.fail = inner, 0, None, False

    async def list_settlements(self, **kw):
        self.reads += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("store down")
        return await self.inner.list_settlements(**kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


# ---------------------------------------------------------------- same values as the direct computation
async def test_projected_pure_split_equals_the_direct_compute(env, frozen):
    await _seed(env.store, extra=5)
    direct = await _direct(env.store)
    assert direct[0] is not None and direct[1] is not None
    assert await pnlsvc.compute_pair(env.store, FROZEN, env="demo") == direct
    fills, settlements = await pnlsvc.pair_rows(env.store, FROZEN)
    assert pnlsvc.pair_from_rows(fills, settlements, FROZEN, env="demo") == direct
    assert await pnlsvc.compute_pair(env.store, FROZEN, env="live") == await _direct(env.store, "live")


async def test_nothing_settled_is_none_none_without_reading_fills(env, frozen):
    await env.store.upsert_fills([_fill("M1", "KXDEMOB-1", ts=FROZEN - timedelta(days=2))])
    assert await pnlsvc.pair_rows(env.store, FROZEN) == ([], [])
    assert await pnlsvc.compute_pair(env.store, FROZEN, env="demo") == (None, None)
    assert await env.runner.pnl_pair() == (None, None)


async def test_cached_value_equals_the_direct_value_and_the_status_body(env, frozen, clock):
    await _seed(env.store)
    direct = await _direct(env.store)
    assert await env.runner.pnl_pair() == direct                       # cold: computed
    assert await env.runner.pnl_pair() == direct                       # warm: from the cache
    r = await env.client.get("/api/status", headers=env.auth)
    assert r.status_code == 200
    body = r.json()
    assert (body["pnl"], body["pnl_arb"]) == direct
    assert body["pnl_total"] == pnlsvc.combine_windows(*direct)


# ---------------------------------------------------------------- TTL, stale-while-revalidate, single flight
async def test_fresh_value_costs_no_store_read(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    await env.runner.pnl_pair()
    assert cs.reads == 1
    for _ in range(20):
        clock.t += runner_mod.PNL_PAIR_TTL_S / 25
        await env.runner.pnl_pair()
    assert cs.reads == 1


async def test_after_the_ttl_the_stale_value_is_served_at_once_and_refreshed_once(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    old = await env.runner.pnl_pair()
    await env.store.upsert_fills([_fill("N1", "KXDEMOB-5", ts=FROZEN - timedelta(days=1), price=0.2)])
    await env.store.upsert_settlements([_settle("KXDEMOB-5", "no", FROZEN - timedelta(hours=1))])
    clock.t += runner_mod.PNL_PAIR_TTL_S + 1
    cs.gate = asyncio.Event()                                          # the refresh's read is held
    got = await asyncio.gather(*[env.runner.pnl_pair() for _ in range(10)])
    assert all(g == old for g in got)                                   # nobody waited for the refresh
    await asyncio.sleep(0)
    assert cs.reads == 2                                                # ten stale polls, ONE refresh
    cs.gate.set()
    await _drain(env.runner)
    new = await env.runner.pnl_pair()
    assert new == await _direct(env.store) and new != old
    assert cs.reads == 2


async def test_cold_concurrent_polls_share_one_compute(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    cs.gate = asyncio.Event()
    polls = [asyncio.create_task(env.runner.pnl_pair()) for _ in range(8)]
    await asyncio.sleep(0)
    cs.gate.set()
    got = await asyncio.gather(*polls)
    assert cs.reads == 1 and all(g == got[0] for g in got) and got[0] == await _direct(env.store)


async def test_too_old_value_is_not_served_the_poll_waits_for_a_fresh_one(env, frozen, clock):
    await _seed(env.store)
    old = await env.runner.pnl_pair()
    await env.store.upsert_settlements([_settle("KXDEMOB-5", "no", FROZEN - timedelta(hours=1))])
    await env.store.upsert_fills([_fill("N1", "KXDEMOB-5", ts=FROZEN - timedelta(days=1), price=0.2)])
    clock.t += runner_mod.PNL_PAIR_MAX_STALE_S + 1
    new = await env.runner.pnl_pair()
    assert new != old and new == await _direct(env.store)


# ---------------------------------------------------------------- invalidation
async def test_a_fills_sync_marks_the_value_stale(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    await env.runner.pnl_pair()
    gen = env.runner._pnl_gen
    env.runner._fetch_fills_sync = lambda mode, since: ([], [])         # a sync that writes nothing new
    assert (await env.runner.sync_fills("demo", force=True))["synced"] is True
    assert env.runner._pnl_gen > gen
    await env.runner.pnl_pair()                                         # inside the TTL, but stale: refresh starts
    await _drain(env.runner)
    assert cs.reads == 2


async def test_a_mode_switch_never_serves_the_other_modes_value(env, frozen, clock):
    await _seed(env.store)
    demo = await env.runner.pnl_pair()
    env.runner.mode = "live"                                            # direct flag flip: the cache is keyed by mode
    live = await env.runner.pnl_pair()
    assert live == await _direct(env.store, "live") and live != demo
    env.runner.mode = "demo"
    assert await env.runner.pnl_pair() == demo


async def test_a_refresh_for_an_old_mode_is_not_cached(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    cs.gate = asyncio.Event()
    task = env.runner._pnl_pair_refresh("demo", env.runner._pnl_gen)
    env.runner.mode = "live"
    cs.gate.set()
    await asyncio.gather(task)
    assert env.runner._pnl_pair_cache is None


# ---------------------------------------------------------------- errors keep today's meaning
async def test_a_store_error_is_null_and_is_not_cached(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    cs.fail = True
    assert await env.runner.pnl_pair() == (None, None)
    assert env.runner._pnl_pair_cache is None
    cs.fail = False
    assert await env.runner.pnl_pair() == await _direct(env.store)


async def test_a_failed_refresh_drops_the_stale_value(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    good = await env.runner.pnl_pair()
    clock.t += runner_mod.PNL_PAIR_TTL_S + 1
    cs.fail = True
    assert await env.runner.pnl_pair() == good                          # stale served while the refresh runs
    await _drain(env.runner)
    assert env.runner._pnl_pair_cache is None
    assert await env.runner.pnl_pair() == (None, None)                  # the next poll waits and sees the error


async def test_a_hung_store_gives_null_after_the_wait_bound(env, frozen, clock, monkeypatch):
    monkeypatch.setattr(runner_mod, "PNL_PAIR_WAIT_S", 0.01)
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    cs.gate = asyncio.Event()                                           # never set: the read hangs
    assert await env.runner.pnl_pair() == (None, None)
    env.runner._pnl_pair_task.cancel()
    await _drain(env.runner)


# ---------------------------------------------------------------- off the event loop
async def test_the_windows_maths_runs_in_a_worker_thread(env, frozen, clock, monkeypatch):
    await _seed(env.store)
    seen = []
    real = pnlsvc.pair_from_rows

    def spy(*a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)
    monkeypatch.setattr(pnlsvc, "pair_from_rows", spy)
    assert await env.runner.pnl_pair() == await _direct(env.store)
    assert seen == [False]


async def test_shutdown_cancels_a_running_refresh(env, frozen, clock):
    await _seed(env.store)
    env.runner.store = cs = CountingStore(env.store)
    cs.gate = asyncio.Event()
    task = env.runner._pnl_pair_refresh("demo", env.runner._pnl_gen)
    await asyncio.sleep(0)
    await env.runner.shutdown()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()

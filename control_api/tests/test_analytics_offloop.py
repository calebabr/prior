"""Analytics CPU work runs off the event loop (load suite scenario `contention`: Stop during a 180-day rebuild).

No wall-clock assertions: a fake builder blocks on a threading.Event until the test releases it, and the test checks
that the loop (a concurrent coroutine, Stop) made progress WHILE the builder was still blocked. If the builder ran
inline on the loop, nothing else could run until it returned, and the "still building" checks would fail.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from control_api.routes import analytics as ra
from control_api.services import analytics as an
from control_api.services import ledger as ledgersvc
from control_api.services import returns as retsvc
from control_api.tests.test_analytics import env  # noqa: F401  (env fixture with synthetic cells)

pytestmark = pytest.mark.asyncio
GUARD_S = 10.0                       # deadlock guard only (never reached when the code is right)


class Blocker:
    """Wraps a real builder: records the thread it ran on, then blocks until release() (or the guard)."""

    def __init__(self, real=None, result=None):
        self.real, self.result = real, result
        self.started, self.released = threading.Event(), threading.Event()
        self.building, self.calls, self.threads, self.switch = False, 0, [], []

    def __call__(self, *a, **k):
        self.calls += 1
        self.threads.append(threading.current_thread())
        self.switch.append(sys.getswitchinterval())
        self.building = True
        self.started.set()
        try:
            self.released.wait(GUARD_S)
            return self.real(*a, **k) if self.real is not None else self.result
        finally:
            self.building = False

    async def wait_started(self):
        for _ in range(int(GUARD_S / 0.01)):
            if self.started.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the builder never started")

    def release(self):
        self.released.set()


@pytest.fixture
def blockers():
    made = []
    yield made
    for b in made:                   # never leave the one analytics worker blocked for the next test
        b.release()


def _new(blockers, **kw) -> Blocker:
    b = Blocker(**kw)
    blockers.append(b)
    return b


# ---------------------------------------------------------------- the helper
async def test_cpu_runs_in_the_analytics_worker_and_the_loop_stays_free(blockers):
    b = _new(blockers, result=42)
    job = asyncio.create_task(ra._cpu(b))
    await b.wait_started()
    ticks = 0
    for _ in range(5):               # the loop keeps running other coroutines while the builder is blocked
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 5 and b.building and not job.done()
    b.release()
    assert await job == 42
    assert b.threads[0] is not threading.main_thread() and b.threads[0].name.startswith("analytics")


async def test_switch_interval_lowered_during_the_build_and_restored_after(blockers):
    before = sys.getswitchinterval()
    b = _new(blockers, result=None)
    job = asyncio.create_task(ra._cpu(b))
    await b.wait_started()
    assert sys.getswitchinterval() == pytest.approx(min(before, ra.CPU_SWITCH_INTERVAL_S))
    b.release()
    await job
    for _ in range(100):             # restored by the worker's done-callback
        if ra._SWITCH.active == 0:
            break
        await asyncio.sleep(0.01)
    assert ra._SWITCH.active == 0 and sys.getswitchinterval() == pytest.approx(before)


async def test_switch_interval_restored_even_if_the_request_is_cancelled(blockers):
    before = sys.getswitchinterval()
    b = _new(blockers, result=None)
    job = asyncio.create_task(ra._cpu(b))
    await b.wait_started()
    job.cancel()                     # the client went away; the work itself still runs to the end
    await asyncio.gather(job, return_exceptions=True)
    assert ra._SWITCH.active == 1    # still lowered: the worker is still busy
    b.release()
    for _ in range(int(GUARD_S / 0.01)):
        if ra._SWITCH.active == 0:
            break
        await asyncio.sleep(0.01)
    assert ra._SWITCH.active == 0 and sys.getswitchinterval() == pytest.approx(before)


async def test_cpu_propagates_builder_errors():
    def boom():
        raise ValueError("bad")
    with pytest.raises(ValueError):
        await ra._cpu(boom)
    for _ in range(100):
        if ra._SWITCH.active == 0:
            break
        await asyncio.sleep(0.01)
    assert ra._SWITCH.active == 0


# ---------------------------------------------------------------- Stop while a heavy build is blocked
async def _stop_while_blocked(env, b: Blocker, path: str):
    env.runner.desired = "running"
    ana = asyncio.create_task(env.client.get(path, headers=env.auth))
    await b.wait_started()
    r = await asyncio.wait_for(env.client.post("/api/bot/stop", headers=env.auth), GUARD_S)
    still_building = b.building                       # Stop answered while the build was still blocked
    b.release()
    res = await asyncio.wait_for(ana, GUARD_S)
    assert r.status_code == 200 and env.runner.desired == "stopped" and env.runner.kill_path.exists()
    assert still_building, "Stop could only answer after the build finished (the build blocked the event loop)"
    assert res.status_code == 200, res.text
    assert b.threads[0].name.startswith("analytics")


async def test_stop_answers_while_a_returns_build_is_blocked(env, monkeypatch, blockers):
    b = _new(blockers, real=retsvc.build)
    monkeypatch.setattr(retsvc, "build", b)
    await _stop_while_blocked(env, b, "/api/analytics/returns?days=180&granularity=week")


async def test_stop_answers_while_a_ledger_build_is_blocked(env, monkeypatch, blockers):
    b = _new(blockers, real=ledgersvc.build)
    monkeypatch.setattr(ledgersvc, "build", b)
    await _stop_while_blocked(env, b, "/api/analytics/ledger?days=180")


async def test_stop_answers_while_a_summary_build_is_blocked(env, monkeypatch, blockers):
    b = _new(blockers, real=an.summary)
    monkeypatch.setattr(an, "summary", b)
    await _stop_while_blocked(env, b, "/api/analytics/summary?days=180")


async def test_stop_answers_while_a_scorecard_build_is_blocked(env, monkeypatch, blockers):
    b = _new(blockers, real=ledgersvc.scorecard)
    monkeypatch.setattr(ledgersvc, "scorecard", b)
    await _stop_while_blocked(env, b, "/api/analytics/scorecard?days=30")


# ---------------------------------------------------------------- single flight
@pytest.mark.parametrize("path,target", [
    ("/api/analytics/returns?days=90&granularity=week&tz=America/Chicago", (retsvc, "build")),
    ("/api/analytics/ledger?days=90&tz=America/Chicago", (ledgersvc, "build")),
    ("/api/analytics/scorecard?days=30", (ledgersvc, "scorecard")),
])
async def test_identical_requests_share_one_build_and_get_the_same_bytes(env, monkeypatch, blockers, path, target):
    mod, name = target
    b = _new(blockers, real=getattr(mod, name))
    monkeypatch.setattr(mod, name, b)
    first = asyncio.create_task(env.client.get(path, headers=env.auth))
    await b.wait_started()
    second = asyncio.create_task(env.client.get(path, headers=env.auth))
    for _ in range(20):              # let the second request reach the flight table
        await asyncio.sleep(0)
    b.release()
    r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), GUARD_S)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
    assert b.calls == 1, "a second identical request started a second build"
    assert not ra._state(type("R", (), {"app": env.app})()).get("flights")   # nothing left in flight


async def test_different_keys_do_not_share(env, monkeypatch, blockers):
    b = _new(blockers, real=retsvc.build)
    b.release()                      # no blocking: just count
    monkeypatch.setattr(retsvc, "build", b)
    for g in ("day", "week"):
        r = await env.client.get(f"/api/analytics/returns?days=30&granularity={g}", headers=env.auth)
        assert r.status_code == 200
    assert b.calls == 2


async def test_a_waiter_disconnecting_does_not_cancel_the_shared_build(env, monkeypatch, blockers):
    b = _new(blockers, real=ledgersvc.build)
    monkeypatch.setattr(ledgersvc, "build", b)
    path = "/api/analytics/ledger?days=30&tz=Europe/London"
    first = asyncio.create_task(env.client.get(path, headers=env.auth))
    await b.wait_started()
    second = asyncio.create_task(env.client.get(path, headers=env.auth))
    for _ in range(20):
        await asyncio.sleep(0)
    first.cancel()                   # the leader's client goes away
    await asyncio.gather(first, return_exceptions=True)
    b.release()
    r2 = await asyncio.wait_for(second, GUARD_S)
    assert r2.status_code == 200 and b.calls == 1


async def test_returns_cache_semantics_unchanged(env, monkeypatch):
    """Still cached per (env, days, tz, granularity) for SUMMARY_TTL_S: a repeat inside the TTL does not rebuild."""
    calls = []
    real = retsvc.build

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(retsvc, "build", counting)
    now = datetime.now(timezone.utc)
    await env.store.add_account_snapshot({"ts": now - timedelta(days=3), "cash": 100.0, "exposure": 0.0})
    a = await env.client.get("/api/analytics/returns?days=30", headers=env.auth)
    b = await env.client.get("/api/analytics/returns?days=30", headers=env.auth)
    assert a.status_code == b.status_code == 200 and a.content == b.content and len(calls) == 1

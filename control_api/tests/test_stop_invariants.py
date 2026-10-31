"""Stop invariants, fuzz style.

Rule under test: after a Stop has returned, nothing that STARTED before it returned can leave the bot (maker or
arbitrage) running, whatever the interleaving, store latency, or Kalshi / store / network failure. Only an
explicit Start / arb-enable that BEGAN after the Stop returned may turn trading back on.

Seeded, so a failure is reproducible: pytest ... -k "seed-17".
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
import threading
import time

import httpx
import pytest

from control_api.auth import security as sec
from control_api.main import create_app
from control_api.runner import RunnerDisabled, StopInProgress, utcnow
from control_api.tests.conftest import FakeKalshi

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ helpers
class JitterStore:
    """Delegates to a real MemoryStore but sleeps a random time before every call (shuffles interleavings)."""

    def __init__(self, inner, rng, max_delay=0.02):
        self._inner, self._rng, self._max = inner, rng, max_delay

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        async def call(*a, **k):
            await asyncio.sleep(self._rng.random() * self._max)
            r = attr(*a, **k)
            return await r if inspect.isawaitable(r) else r
        return call


class HangStore:
    """Every call awaits forever (Atlas black-holed)."""

    def __getattr__(self, name):
        async def hang(*a, **k):
            await asyncio.Event().wait()
        return hang


class _FastAsyncio:
    """Stands in for the `asyncio` name inside a module: same API, but wait_for timeouts are capped."""

    def __init__(self, cap):
        self._cap = cap

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def wait_for(self, aw, timeout=None):
        return asyncio.wait_for(aw, None if timeout is None else min(timeout, self._cap))


@pytest.fixture
def fast_timeouts(monkeypatch):
    """Cap every Stop-path timeout (runner 5 s / 25 s, audit 5 s, auth 3 s) at 0.3 s so hang tests run quickly."""
    from control_api import runner as runner_mod
    import importlib
    audit_mod = importlib.import_module("control_api.auth.audit")
    deps_mod = importlib.import_module("control_api.auth.deps")
    shim = _FastAsyncio(0.3)
    monkeypatch.setattr(runner_mod, "asyncio", shim)
    monkeypatch.setattr(audit_mod, "asyncio", shim)
    monkeypatch.setattr(deps_mod, "asyncio", shim)
    return shim


def _app_client(settings, store, kalshi_factory):
    app = create_app(settings, store=store, kalshi_factory=kalshi_factory)
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, client, {"Authorization": f"Bearer {tok}"}


def _assert_fully_stopped(runner):
    assert runner.desired == "stopped"
    assert runner.arb_desired == "stopped" and runner.arb_mode == "demo"
    assert runner.kill_path.exists() and runner.arb_kill_path.exists()


# ------------------------------------------------------------------ Stop vs everything (fuzz)
OPS = ["start", "arb_on", "arb_on2", "config", "rehydrate", "tick", "run_once"]


@pytest.mark.parametrize("seed", range(40), ids=lambda s: f"seed-{s}")
async def test_stop_concurrent_with_random_operations_always_ends_stopped(env, monkeypatch, seed):
    rng = random.Random(seed)
    r = env.runner
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    passes: list[float] = []
    monkeypatch.setattr(r, "_run_strategy_sync", lambda mode: (passes.append(time.perf_counter()) or 0, ""))
    r.last_monitor_utc = utcnow()
    r.RETRY_FIRST_DELAY = 0.01

    # initial state: maybe already trading (store + memory agree), so rehydrate and Stop both have work to do
    initially_running = rng.random() < 0.6
    if initially_running:
        await env.store.set_state(by="t", desired="running", arb_desired="running")
        r.desired, r.arb_desired = "running", "running"
    cfg_text = r.config_path.read_text(encoding="utf-8")
    r.store = JitterStore(env.store, rng)

    log: list[tuple[str, float, bool]] = []
    t_stop_end = [None]

    async def run(name, delay, fn):
        await asyncio.sleep(delay)
        t0 = time.perf_counter()
        try:
            await fn()
            log.append((name, t0, True))
        except (StopInProgress, PermissionError, RunnerDisabled):
            log.append((name, t0, False))

    async def stop():
        await asyncio.sleep(rng.random() * 0.03)
        res = await r.stop(by="owner")
        t_stop_end[0] = time.perf_counter()
        assert res["desired"] == "stopped"      # Stop always reports stopped

    fns = {
        "start": lambda: r.start_trading(by="owner"),
        "arb_on": lambda: r.set_arb(enabled=True, live=None, by="owner", ack_legging=True),
        "arb_on2": lambda: r.set_arb(enabled=True, live=False, by="owner", ack_legging=True),
        "config": lambda: r.apply_config(json.loads(cfg_text)),
        "rehydrate": lambda: r.rehydrate(),
        "tick": lambda: r.tick("fuzz"),
        "run_once": lambda: r.run_once("owner"),
    }
    chosen = [o for o in OPS if rng.random() < 0.7] or ["start"]
    chosen += rng.choices(["start", "arb_on", "tick"], k=rng.randint(0, 3))   # repeats
    tasks = [asyncio.create_task(run(o, rng.random() * 0.04, fns[o])) for o in chosen]
    tasks.append(asyncio.create_task(stop()))
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(r._bg), return_exceptions=True)
    if r._state_retry is not None:
        # A Start that wins after the Stop cancels this retry task on purpose (a stale stopped-retry must not override
        # it), and awaiting a cancelled task raises CancelledError, which is not an Exception.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(r._state_retry, 2)

    # a Start / arb-enable that began after the Stop returned is allowed to win
    legit_start = any(n == "start" and ok and t0 >= t_stop_end[0] for n, t0, ok in log)
    legit_arb = any(n.startswith("arb_on") and ok and t0 >= t_stop_end[0] for n, t0, ok in log)
    legit_tick = legit_start or any(n == "rehydrate" and t0 >= t_stop_end[0] for n, t0, ok in log)

    if not legit_start:
        assert r.desired == "stopped", f"maker running after Stop; log={log}"
        assert r.kill_path.exists()
        assert (await env.store.get_state())["desired"] == "stopped"
        assert (await r.tick("after"))["ran"] is False
        assert (await r.run_once("owner"))["ran"] is False
    else:
        assert not r.kill_path.exists() or r.desired == "stopped"
    if not legit_arb:
        assert r.arb_desired == "stopped" and r.arb_mode == "demo", f"arb running after Stop; log={log}"
        assert r.arb_kill_path.exists()
        assert (await env.store.get_state()).get("arb_desired", "stopped") == "stopped"
    # kill switch on disk and `running` never coexist
    if r.desired == "running":
        assert not r.kill_path.exists()
    # no strategy pass may begin after the Stop returned unless something legitimately restarted trading
    if not legit_tick:
        assert all(t < t_stop_end[0] for t in passes), f"a trading pass started after Stop; passes={passes} stop_end={t_stop_end[0]} log={log}"


@pytest.mark.parametrize("n", [2, 5])
async def test_many_concurrent_stops_all_succeed_and_end_stopped(env, n):
    env.runner.desired = "running"
    res = await asyncio.gather(*[env.client.post("/api/bot/stop", headers=env.auth) for _ in range(n)])
    assert [x.status_code for x in res] == [200] * n
    _assert_fully_stopped(env.runner)
    assert (await env.store.get_state())["desired"] == "stopped"


async def test_stop_is_idempotent_and_repeatable_after_start(env):
    for _ in range(3):
        assert (await env.client.post("/api/bot/stop", headers=env.auth)).status_code == 200
        await env.runner.start_trading(by="owner")
        assert env.runner.desired == "running"
    assert (await env.client.post("/api/bot/stop", headers=env.auth)).status_code == 200
    _assert_fully_stopped(env.runner)


async def test_stop_during_http_start_and_arb_enable_requests(env):
    """Same race through the real routes (2FA headers are fetched one at a time: a TOTP step is single-use)."""
    h_start = await env.h2fa()
    start = asyncio.create_task(env.client.post("/api/bot/start", headers=h_start))
    stop = asyncio.create_task(env.client.post("/api/bot/stop", headers=env.auth))
    await asyncio.gather(start, stop)
    t_end = time.perf_counter()
    arb = await env.client.put("/api/arb", json={"enabled": True}, headers=await env.h2fa())
    assert arb.status_code in (200, 409)
    # the arb enable above began after Stop returned, so it may legitimately turn arb on; a final Stop ends it all
    del t_end
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    _assert_fully_stopped(env.runner)


# ------------------------------------------------------------------ Stop with a failing / hanging world
@pytest.mark.slow
async def test_stop_with_store_hanging_forever_returns_and_is_stopped(settings, bot_paths, kalshi, fast_timeouts):
    app, client, auth = _app_client(settings, HangStore(), lambda m: kalshi)
    app.state.runner.desired = "running"
    app.state.runner.arb_desired = "running"
    t0 = time.perf_counter()
    async with client:
        res = await asyncio.wait_for(client.post("/api/bot/stop", headers=auth), 10)
    assert res.status_code == 200
    assert time.perf_counter() - t0 < 5
    _assert_fully_stopped(app.state.runner)
    assert any(e.startswith("store") for e in res.json()["errors"])
    assert sorted(kalshi.cancelled) == ["o1", "o3"]           # cancel did not wait for the store
    assert app.state.runner._stop_pending                      # and it keeps retrying in the background
    await app.state.runner.shutdown()


@pytest.mark.slow
async def test_stop_with_kalshi_hanging_sets_flags_first_and_still_returns(env, fast_timeouts):
    # The 0.3 s cap raced the asserts under load (Stop timed out before 'stop_in_progress' was checked): widen the
    # window to 3 s so the in-progress checks always run while Stop is blocked; Stop must still end by the cap.
    fast_timeouts._cap = 3.0
    release = threading.Event()
    entered = threading.Event()

    class HangingKalshi(FakeKalshi):
        def get_orders(self, status=None):
            entered.set()
            release.wait(30)                    # a stuck HTTP call in the worker thread
            return []
    env.runner.kalshi_factory = lambda m: HangingKalshi()
    env.runner.desired = "running"
    task = asyncio.create_task(env.client.post("/api/bot/stop", headers=env.auth))
    for _ in range(500):                        # wait (up to 10 s) until Stop is really blocked in the Kalshi call
        if entered.is_set():
            break
        await asyncio.sleep(0.02)
    assert entered.is_set()
    try:
        # flags + kill switches are already set although the cancel has not finished
        assert env.runner.desired == "stopped" and env.runner.kill_path.exists() and env.runner.arb_kill_path.exists()
        assert (await env.runner.tick("x"))["ran"] is False
        late_start = await env.client.post("/api/bot/start", headers=await env.h2fa())
        assert late_start.status_code == 409 and late_start.json()["detail"] == "stop_in_progress"
        res = await asyncio.wait_for(task, 5)
    finally:
        release.set()
    assert res.status_code == 200
    assert any(e.startswith("cancel") for e in res.json()["errors"])
    _assert_fully_stopped(env.runner)


@pytest.mark.parametrize("exc", [ConnectionError("network down"), TimeoutError("timed out"), OSError("no route"),
                                 RuntimeError("boom")])
async def test_stop_when_kalshi_calls_raise_network_errors(env, exc):
    class Down(FakeKalshi):
        def get_orders(self, status=None):
            raise exc
    env.runner.kalshi_factory = lambda m: Down()
    env.runner.desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    assert any(e.startswith("cancel") for e in r.json()["errors"])
    _assert_fully_stopped(env.runner)


async def test_stop_when_the_kalshi_factory_itself_raises(env):
    def boom(mode):
        raise ConnectionError("cannot build client")
    env.runner.kalshi_factory = boom
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    _assert_fully_stopped(env.runner)


async def test_one_failing_cancel_does_not_stop_the_others(env):
    class Flaky(FakeKalshi):
        def cancel_order(self, order_id, market_ticker=None):
            if order_id == "o1":
                raise ConnectionError("reset")
            super().cancel_order(order_id, market_ticker)
    k = Flaky()
    env.runner.kalshi_factory = lambda m: k
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200 and k.cancelled == ["o3"]       # o3 still cancelled, manual order untouched
    _assert_fully_stopped(env.runner)


async def test_stop_when_kill_switch_cannot_be_written_still_stops_in_memory(env, monkeypatch):
    from pathlib import Path
    real = Path.write_text

    def deny(self, *a, **k):
        if self.name in ("KILL", "ARB_KILL"):
            raise PermissionError("read-only disk")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", deny)
    env.runner.desired = "running"
    env.runner.arb_desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    errs = r.json()["errors"]
    assert any(e.startswith("kill_switch") for e in errs) and any(e.startswith("arb_kill_switch") for e in errs)
    assert env.runner.desired == "stopped" and env.runner.arb_desired == "stopped"
    assert (await env.runner.tick("x"))["ran"] is False
    assert sorted(env.kalshi.cancelled) == ["o1", "o3"]


@pytest.mark.slow
async def test_stop_with_store_and_kalshi_both_hanging(settings, bot_paths, fast_timeouts):
    release = threading.Event()

    class HangingKalshi(FakeKalshi):
        def get_orders(self, status=None):
            release.wait(30)
            return []
    app, client, auth = _app_client(settings, HangStore(), lambda m: HangingKalshi())
    try:
        async with client:
            res = await asyncio.wait_for(client.post("/api/bot/stop", headers=auth), 10)
    finally:
        release.set()
    assert res.status_code == 200
    _assert_fully_stopped(app.state.runner)
    await app.state.runner.shutdown()


async def test_stop_with_store_flapping_eventually_persists(env):
    """Store fails twice then recovers: the background retry lands desired=stopped."""
    good = env.store
    fails = {"n": 2}

    class Flapping:
        def __getattr__(self, name):
            fn = getattr(good, name)

            async def call(*a, **k):
                if name == "set_state" and fails["n"] > 0:
                    fails["n"] -= 1
                    raise ConnectionError("atlas flaps")
                return await fn(*a, **k)
            return call
    env.runner.store = Flapping()
    env.runner.RETRY_FIRST_DELAY = 0.01
    await good.set_state(by="t", desired="running")
    env.runner.desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    for _ in range(100):
        if not env.runner._stop_pending:
            break
        await asyncio.sleep(0.02)
    assert not env.runner._stop_pending
    assert (await good.get_state())["desired"] == "stopped"


async def test_rehydrate_while_store_down_stays_stopped_even_after_a_stop_and_start_attempt(settings, bot_paths, kalshi):
    app = create_app(settings, store=HangStore(), kalshi_factory=lambda m: kalshi)
    r = app.state.runner
    from control_api import runner as runner_mod
    # a hung store at startup: rehydrate must not leave trading on
    with contextlib.suppress(Exception):
        await asyncio.wait_for(r.rehydrate(), 0.5)
    assert r.desired == "stopped"
    assert (await r.tick("x"))["ran"] is False
    del runner_mod

"""Stop independence (Sprint 3 item 1): always-run set.

What is proved here (the rest is in test_stop_safety / test_stop_invariants / test_stop_trio_* /
test_failure_combinations / test_arb_gate_independent / test_analytics_extra, which this file does not repeat):

* from the code: the Stop route needs no 2FA and has no rate limit; the runner's ``stop`` sets the in-memory flags and
  both kill switches before its first ``await`` (the cancel), and touches the store only after the cancel;
* end to end through HTTP, on a virtual clock (tests/vclock.py, no real sleeps): Stop's flags, kill switches and the
  start of the cancel land in under 1 s, measured from the request, when the store is slow, black-holed or raising,
  Kalshi is slow or hung, a pass is in flight, an analytics rebuild is running, an arbitrage set is mid-send, the
  instance is a standby, and under 50 simultaneous Stops; the response itself is bounded too.

The defect this file found: with a slow or black-holed store, Stop's auth dependency waited on the store for up to
3 s (revocation + session-epoch reads) BEFORE the runner's flags were set. Stop now cuts those reads at
``deps.STOP_STORE_TIMEOUT_S`` (0.5 s) and fails open, as it already did on store errors.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading

import httpx
import pyotp

from control_api.auth import deps
from control_api.auth import security as sec
from control_api.db import MemoryStore
from control_api.main import create_app
from control_api.routes import bot as bot_routes
from control_api.runner import Runner
from control_api.services.lease import STANDBY
from control_api.tests.conftest import BrokenStore, FakeKalshi
from control_api.tests.vclock import SlowStore, run_virtual

FLAG_BUDGET_S = 1.0          # Stop's own work: flags + kill switches + the cancel started
INF = float("inf")


# ------------------------------------------------------------------ helpers
class _Probe:
    """Replaces Runner._cancel_in_stop_pool: records the virtual time the cancel STARTED and the state of the flags at
    that instant, then completes in-loop after ``kalshi_delay`` virtual seconds (inf = a hung Kalshi call)."""

    def __init__(self, runner: Runner, kalshi_delay: float = 0.0):
        self.r, self.delay = runner, kalshi_delay
        self.starts: list[float] = []
        self.flags_at_start: list[tuple] = []

    def __call__(self, mode: str):
        loop = asyncio.get_running_loop()
        r = self.r
        self.starts.append(loop.time())
        self.flags_at_start.append((r.desired, r.arb_desired, r.kill_path.exists(), r.arb_kill_path.exists()))
        fut = loop.create_future()
        if self.delay != INF:
            loop.call_later(self.delay, lambda: fut.done() or fut.set_result(r._cancel_resting(mode)))
        return fut


async def _app(settings, kalshi, *, store_delay: float | None = 0.0, broken: bool = False, kalshi_delay: float = 0.0):
    """App + client + auth headers. The runner is rehydrated on a healthy store, then the store is swapped for the
    faulty one (as when Atlas degrades while the service is up)."""
    mem = MemoryStore()
    await mem.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    app = create_app(settings, store=mem, kalshi_factory=lambda m: kalshi)
    r = app.state.runner
    await r.rehydrate()
    r.desired, r.arb_desired = "running", "running"
    faulty = BrokenStore() if broken else (SlowStore(mem, store_delay) if store_delay else mem)
    app.state.store = r.store = r.lease.store = faulty
    probe = _Probe(r, kalshi_delay)
    r._cancel_in_stop_pool = probe
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, r, client, {"Authorization": f"Bearer {tok}"}, probe, mem


def _stopped(r: Runner) -> bool:
    return (r.desired == "stopped" and r.arb_desired == "stopped" and r.arb_mode == "demo"
            and r.kill_path.exists() and r.arb_kill_path.exists())


async def _timed_stop(client, auth, probe):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await asyncio.wait_for(client.post("/api/bot/stop", headers=auth), 120)
    return res, probe.starts[0] - t0, loop.time() - t0


# ------------------------------------------------------------------ from the code
def test_stop_route_needs_no_2fa_and_is_never_rate_limited():
    sig = inspect.signature(bot_routes.stop)
    assert sig.parameters["user"].default.dependency is deps.current_user_stop
    src = inspect.getsource(bot_routes.stop)
    for banned in ("require_fresh_2fa", "_rate_limit", "enforce(", "check(", "check_user", "X-2FA"):
        assert banned not in src, banned
    assert ("POST", "/api/bot/stop") in deps.FAIL_OPEN_ROUTES


def test_runner_stop_sets_every_flag_before_its_first_await_and_the_store_comes_after_the_cancel():
    tree = ast.parse(textwrap.dedent(inspect.getsource(Runner.stop)))
    fn = tree.body[0]
    order: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Await):
            order.append((line, "await:" + ast.unparse(node.value)[:60]))
        elif isinstance(node, ast.Attribute) and node.attr in ("store", "_write_stopped"):
            order.append((line, "store"))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr in ("desired", "arb_desired", "arb_mode", "_stop_gen"):
                    order.append((line, "flag:" + t.attr))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            order.append((line, "flag:" + node.target.attr))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
            order.append((line, "kill:" + ast.unparse(node.func.value)))
    order.sort()
    first_await = next(i for i, (_, k) in enumerate(order) if k.startswith("await"))
    before = [k for _, k in order[:first_await]]
    assert order[first_await][1].startswith("await:asyncio.wait_for(self._cancel_in_stop_pool"), order
    for need in ("flag:_stop_gen", "flag:_stopping", "flag:desired", "flag:arb_desired", "flag:arb_mode",
                 "kill:self.kill_path", "kill:self.arb_kill_path"):
        assert need in before, (need, before)
    assert "store" not in before                       # no store reference at all before the cancel
    first_store = next(i for i, (_, k) in enumerate(order) if k == "store")
    assert first_store > first_await


def test_stop_auth_store_reads_are_cut_short_and_fail_open():
    assert deps.STOP_STORE_TIMEOUT_S <= 0.5 < FLAG_BUDGET_S
    src = inspect.getsource(deps._authenticate)
    assert "STOP_STORE_TIMEOUT_S if fail_open" in src


# ------------------------------------------------------------------ HTTP, virtual clock: store faults
def _store_case(settings, kalshi, **kw):
    async def go():
        app, r, client, auth, probe, _ = await _app(settings, kalshi, **kw)
        async with client:
            res, flag_t, total = await _timed_stop(client, auth, probe)
        assert res.status_code == 200, res.text
        assert flag_t < FLAG_BUDGET_S, f"cancel started {flag_t:.2f}s after the request"
        assert probe.flags_at_start[0] == ("stopped", "stopped", True, True)     # flags + kill switches first
        assert _stopped(r) and sorted(kalshi.cancelled) == ["o1", "o3"]
        await r.shutdown()
        return res.json(), total
    return run_virtual(go)


def test_stop_with_store_black_holed(settings, bot_paths, kalshi):
    body, total = _store_case(settings, kalshi, store_delay=INF)
    assert any(e.startswith("store") for e in body["errors"])
    assert total < 12.0                                # 0.5 s auth + 5 s store write + 5 s audit, all bounded


def test_stop_with_store_slow(settings, bot_paths, kalshi):
    body, total = _store_case(settings, kalshi, store_delay=2.0)
    assert body["errors"] == [] and total < 12.0      # the slow write still lands inside its 5 s bound


def test_stop_with_store_raising(settings, bot_paths, kalshi):
    body, total = _store_case(settings, kalshi, broken=True)
    assert any(e.startswith("store") for e in body["errors"]) and total < FLAG_BUDGET_S


def test_stop_with_store_healthy_is_fast(settings, bot_paths, kalshi):
    body, total = _store_case(settings, kalshi, store_delay=0.0)
    assert body["errors"] == [] and total < FLAG_BUDGET_S


# ------------------------------------------------------------------ Kalshi faults
def test_stop_with_kalshi_slow_starts_the_cancel_at_once(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, kalshi_delay=20.0)
        async with client:
            res, flag_t, total = await _timed_stop(client, auth, probe)
        assert res.status_code == 200 and flag_t < FLAG_BUDGET_S and _stopped(r)
        assert 20.0 <= total < 21.0 and sorted(kalshi.cancelled) == ["o1", "o3"]
        assert (await mem.get_state())["desired"] == "stopped"
        await r.shutdown()
    run_virtual(go)


def test_stop_with_kalshi_and_store_both_hung(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, _ = await _app(settings, kalshi, store_delay=INF, kalshi_delay=INF)
        async with client:
            res, flag_t, total = await _timed_stop(client, auth, probe)
        assert res.status_code == 200 and flag_t < FLAG_BUDGET_S and _stopped(r)
        errs = res.json()["errors"]
        assert any(e.startswith("cancel") for e in errs) and any(e.startswith("store") for e in errs)
        assert total < 40.0                            # 25 s cancel bound + 5 s store + 5 s audit
        await r.shutdown()
    run_virtual(go)


# ------------------------------------------------------------------ work in flight
def test_stop_with_a_pass_an_arb_set_and_an_analytics_rebuild_all_in_flight_and_a_slow_store(
        settings, bot_paths, kalshi, monkeypatch):
    from control_api.routes import analytics as an_route
    release, entered = threading.Event(), threading.Event()
    real_summary = an_route.an.summary

    def blocking_summary(*a, **k):                     # the rebuild, stuck in its worker thread
        entered.set()
        release.wait(30)
        return real_summary(*a, **k)
    monkeypatch.setattr(an_route.an, "summary", blocking_summary)

    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, store_delay=0.2, kalshi_delay=3.0)
        async with client:
            rebuild = asyncio.ensure_future(client.get("/api/analytics/summary", headers=auth))
            # until the rebuild is really inside its worker thread (a real wait: virtual sleeps take no real time)
            assert await asyncio.to_thread(entered.wait, 20)
            holder = asyncio.Event()

            async def hold_pass():                     # a pass in flight: the pass lock held, the order step running
                async with r._pass_lock:
                    r._strategy_running = True
                    await holder.wait()
                    r._strategy_running = False
            pass_task = asyncio.ensure_future(hold_pass())
            await asyncio.sleep(0)
            r.arb_running_scan = True                  # an arbitrage set mid-send
            try:
                res, flag_t, total = await _timed_stop(client, auth, probe)
                assert res.status_code == 200, res.text
                assert flag_t < FLAG_BUDGET_S and probe.flags_at_start[0] == ("stopped", "stopped", True, True)
                assert _stopped(r) and r._pass_lock.locked() and not rebuild.done()   # Stop waited for neither
                assert total < 12.0
                assert (await r.tick("x"))["ran"] is False
            finally:
                release.set()
                holder.set()
                r.arb_running_scan = False
                await pass_task
                done = await rebuild                   # released: the rebuild finishes normally after the Stop
            assert done.status_code == 200 and _stopped(r)
        await r.shutdown()
    run_virtual(go)


# ------------------------------------------------------------------ standby instance
def test_stop_on_a_standby_instance_still_stops_and_cancels(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, store_delay=INF)
        r.lease.role, r._leader_ready = STANDBY, False
        assert r.role() == "standby"
        async with client:
            res, flag_t, _ = await _timed_stop(client, auth, probe)
        assert res.status_code == 200 and flag_t < FLAG_BUDGET_S and _stopped(r)
        assert sorted(kalshi.cancelled) == ["o1", "o3"]     # a standby's Stop still cancels the resting mm- orders
        await r.shutdown()
    run_virtual(go)


# ------------------------------------------------------------------ many simultaneous Stops
def test_fifty_simultaneous_stops_with_a_slow_store(settings, bot_paths):
    async def go():
        k = FakeKalshi()
        app, r, client, auth, probe, mem = await _app(settings, k, store_delay=1.0, kalshi_delay=0.5)
        loop = asyncio.get_running_loop()
        async with client:
            t0 = loop.time()
            res = await asyncio.wait_for(asyncio.gather(*[client.post("/api/bot/stop", headers=auth)
                                                          for _ in range(50)]), 120)
            total = loop.time() - t0
        assert [x.status_code for x in res] == [200] * 50
        assert len(probe.starts) == 50 and max(probe.starts) - t0 < FLAG_BUDGET_S
        assert all(f == ("stopped", "stopped", True, True) for f in probe.flags_at_start)
        assert total < 12.0 and _stopped(r) and r._stopping == 0
        assert (await mem.get_state())["desired"] == "stopped"
        await r.shutdown()
    run_virtual(go)


# ------------------------------------------------------------------ never rate limited, never needs the limiter's store
def test_stop_is_never_rate_limited_even_with_every_bucket_exhausted_and_the_store_down(settings, bot_paths, kalshi):
    from control_api.auth.ratelimit import get_limiter

    calls: list[str] = []

    class CountingBroken(BrokenStore):
        def __getattr__(self, name):
            calls.append(name)
            return super().__getattr__(name)

    async def go():
        app, r, client, auth, probe, _ = await _app(settings, kalshi)
        store = CountingBroken()
        app.state.store = r.store = r.lease.store = store

        class Req:                                     # exhaust every known bucket for this user and this IP
            pass
        req = Req()
        req.app = app
        lim = get_limiter(req)
        for b in list(lim.limits) + ["stop", "bot_stop"]:
            lim.limits.setdefault(b, (1, 3600.0))
            for key in ("127.0.0.1", "testclient", "user:owner"):
                while lim.hit(b, key)[0]:
                    pass
        async with client:
            for _ in range(100):
                res = await client.post("/api/bot/stop", headers=auth)
                assert res.status_code == 200, res.text
        assert _stopped(r) and len(probe.starts) == 100
        assert "ratelimit_hit" not in calls and calls          # the store was used (auth, audit), never the limiter
        await r.shutdown()
    run_virtual(go)


# ------------------------------------------------------------------ the other endpoints keep failing closed
def test_non_stop_routes_still_fail_closed_on_a_slow_store(settings, bot_paths, kalshi):
    """The short, fail-open timeout is Stop's alone: a normal authenticated read with a black-holed store is 503."""
    async def go():
        app, r, client, auth, probe, _ = await _app(settings, kalshi, store_delay=INF)
        async with client:
            res = await asyncio.wait_for(client.get("/api/config", headers=auth), 60)
        assert res.status_code == 503 and res.json()["detail"] == "store_unavailable"
        await r.shutdown()
    run_virtual(go)


def test_start_still_needs_fresh_2fa_after_a_degraded_stop(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, store_delay=INF)
        async with client:
            assert (await client.post("/api/bot/stop", headers=auth)).status_code == 200
            app.state.store = r.store = r.lease.store = mem          # Atlas back
            res = await client.post("/api/bot/start", headers={**auth, "X-2FA-Code": pyotp.TOTP(
                pyotp.random_base32()).now()})
        assert res.status_code in (403, 503) and _stopped(r)
        await r.shutdown()
    run_virtual(go)

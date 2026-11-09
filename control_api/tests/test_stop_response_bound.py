"""Stop's HTTP response is bounded even with a hung store (Sprint 3 wave A, backend pass 2, item B).

Before: with a black-holed store the response took ~10.5 s (inline store write 5 s + audit 5 s), though the flags and
the cancel were already done. Now the store write and the audit run as tasks the response waits on only briefly
(runner.STOP_STORE_WAIT_S, bot.STOP_AUDIT_WAIT_S). Proved here on the virtual clock (tests/vclock.py, no real sleeps):

* hung / slow / raising / healthy store: response < 1.5 s, flags + kill switches + the cancel start < 1 s;
* nothing is skipped: the desired=stopped state still reaches the store afterwards (in-flight write, or the existing
  background retry once the store is back), and the bot.stop audit doc is still written (one retry);
* a healthy store behaves exactly as before: no error, nothing pending, the audit already written at the response;
* while the Stop's write is in flight, Start and arbitrage enable are refused (Stop wins);
* failures are logged by type only and never silently dropped.
"""
from __future__ import annotations

import asyncio
import logging

from control_api.auth import security as sec
from control_api.db import MemoryStore
from control_api.main import create_app
from control_api.routes import bot as bot_routes
from control_api import runner as runner_mod
from control_api.runner import StopInProgress
from control_api.tests.conftest import BrokenStore
from control_api.tests.vclock import SlowStore, run_virtual

INF = float("inf")
RESPONSE_BUDGET_S = 1.5
FLAG_BUDGET_S = 1.0


class _Probe:
    def __init__(self, r):
        self.r, self.starts, self.flags = r, [], []

    def __call__(self, mode):
        loop = asyncio.get_running_loop()
        self.starts.append(loop.time())
        self.flags.append((self.r.desired, self.r.arb_desired, self.r.kill_path.exists(), self.r.arb_kill_path.exists()))
        fut = loop.create_future()
        loop.call_soon(lambda: fut.done() or fut.set_result(self.r._cancel_resting(mode)))
        return fut


async def _app(settings, kalshi, store):
    import httpx
    mem = MemoryStore()
    await mem.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    app = create_app(settings, store=mem, kalshi_factory=lambda m: kalshi)
    r = app.state.runner
    await r.rehydrate()
    r.desired, r.arb_desired = "running", "running"
    faulty = store(mem)
    app.state.store = r.store = r.lease.store = faulty
    probe = _Probe(r)
    r._cancel_in_stop_pool = probe
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, r, client, {"Authorization": f"Bearer {tok}"}, probe, mem


def _restore(app, r, mem):
    app.state.store = r.store = r.lease.store = mem


async def _stop(client, auth):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await asyncio.wait_for(client.post("/api/bot/stop", headers=auth), 60)
    return res, t0, loop.time() - t0


async def _until(cond, seconds):
    for _ in range(int(seconds * 10)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


async def _stop_audits(mem):
    return [a for a in await mem.list_audit(limit=500) if a.get("action") == "bot.stop"]


def _stopped(r):
    return (r.desired == "stopped" and r.arb_desired == "stopped" and r.arb_mode == "demo"
            and r.kill_path.exists() and r.arb_kill_path.exists())


def test_budgets_add_up_under_the_target():
    from control_api.auth import deps
    assert deps.STOP_STORE_TIMEOUT_S + runner_mod.STOP_STORE_WAIT_S + bot_routes.STOP_AUDIT_WAIT_S < RESPONSE_BUDGET_S


def test_hung_store_response_is_prompt_and_state_and_audit_land_once_the_store_is_back(
        settings, bot_paths, kalshi, caplog):
    caplog.set_level(logging.WARNING)

    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: SlowStore(m, INF))
        async with client:
            res, t0, total = await _stop(client, auth)
            assert res.status_code == 200 and total < RESPONSE_BUDGET_S, total
            assert probe.starts[0] - t0 < FLAG_BUDGET_S and probe.flags[0] == ("stopped", "stopped", True, True)
            assert _stopped(r) and sorted(kalshi.cancelled) == ["o1", "o3"]
            assert res.json()["errors"] == ["store: slow (still writing in background)"]
            assert r._stop_pending and r._stop_writes == 1
            await asyncio.sleep(1.0)
            _restore(app, r, mem)                       # Atlas comes back
            # the in-flight write times out (5 s), the retry lands; the audit's retry lands too
            assert await _until(lambda: not r._stop_pending, 30)
            assert r._stop_writes == 0 and r._stop_persisted
            assert (await mem.get_state())["desired"] == "stopped"
            for _ in range(100):
                if await _stop_audits(mem):
                    break
                await asyncio.sleep(0.1)
            audits = await _stop_audits(mem)
            assert len(audits) == 1 and audits[0]["ok"] is False and audits[0]["user"] == "owner"
        await r.shutdown()
    run_virtual(go)
    assert "Stop not persisted yet: TimeoutError" in caplog.text


def test_slow_store_response_is_prompt_and_the_write_and_audit_land_behind_it(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: SlowStore(m, 2.0))
        async with client:
            res, t0, total = await _stop(client, auth)
            assert res.status_code == 200 and total < RESPONSE_BUDGET_S
            assert probe.starts[0] - t0 < FLAG_BUDGET_S and _stopped(r)
            assert (await mem.get_state() or {}).get("desired") != "stopped"     # not yet: still in flight
            assert await _until(lambda: not r._stop_pending, 5)
            assert r._stop_writes == 0 and r._stop_persisted and r._state_retry is None    # landed, no retry
            assert (await mem.get_state())["desired"] == "stopped"
            assert await _until(lambda: not app.state.stop_audit_tasks, 5)
            assert len(await _stop_audits(mem)) == 1
        await r.shutdown()
    run_virtual(go)


def test_healthy_store_behaves_as_before(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: m)
        async with client:
            res, t0, total = await _stop(client, auth)
            assert res.status_code == 200 and res.json()["errors"] == [] and total < FLAG_BUDGET_S
            # at the response: persisted, nothing pending, nothing in flight, the audit already written
            assert not r._stop_pending and r._stop_persisted and r._stop_writes == 0
            assert (await mem.get_state())["desired"] == "stopped"
            audits = await _stop_audits(mem)
            assert len(audits) == 1 and audits[0]["ok"] is True
            assert audits[0]["details"]["cancelled"] == 2
        await r.shutdown()
    run_virtual(go)


def test_raising_store_reports_the_error_and_hands_over_to_the_retry(settings, bot_paths, kalshi, caplog):
    caplog.set_level(logging.WARNING)

    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: BrokenStore())
        async with client:
            res, t0, total = await _stop(client, auth)
            assert res.status_code == 200 and total < RESPONSE_BUDGET_S and _stopped(r)
            errs = res.json()["errors"]
            assert len(errs) == 1 and errs[0].startswith("store: ") and errs[0].endswith("(retrying in background)")
            assert r._stop_pending and r._state_retry is not None and not r._state_retry.done()
            _restore(app, r, mem)
            assert await _until(lambda: not r._stop_pending, 30)
            assert (await mem.get_state())["desired"] == "stopped"
            assert await _until(lambda: not app.state.stop_audit_tasks, 10)
            assert len(await _stop_audits(mem)) == 1          # the audit's one retry landed on the recovered store
        await r.shutdown()
    run_virtual(go)
    assert "Stop not persisted yet:" in caplog.text


def test_audit_that_fails_twice_is_logged_and_never_breaks_the_stop(settings, bot_paths, kalshi, caplog):
    caplog.set_level(logging.WARNING)

    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: SlowStore(m, INF))
        async with client:
            res, _, total = await _stop(client, auth)
            assert res.status_code == 200 and total < RESPONSE_BUDGET_S and _stopped(r)
            assert await _until(lambda: not app.state.stop_audit_tasks, 30)
        await r.shutdown()
    run_virtual(go)
    assert "Stop audit not written after one retry" in caplog.text
    assert caplog.text.count("audit write failed for action=bot.stop") == 2


def test_start_and_arb_enable_are_refused_while_the_stop_write_is_in_flight(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: SlowStore(m, 3.0))
        async with client:
            res, _, _ = await _stop(client, auth)
            assert res.status_code == 200 and r._stop_writes == 1
            for call in (lambda: r.start_trading(by="owner"),
                         lambda: r.set_arb(enabled=True, live=None, by="owner", ack_legging=True)):
                try:
                    await call()
                    raise AssertionError("must be refused while Stop's write is in flight")
                except StopInProgress as e:
                    assert str(e) == "stop_in_progress"
            assert _stopped(r)
            assert await _until(lambda: r._stop_writes == 0, 10)
            assert (await mem.get_state())["desired"] == "stopped" and not r._stop_pending
        await r.shutdown()
    run_virtual(go)


def test_shutdown_during_an_in_flight_stop_write_leaves_no_counter_behind(settings, bot_paths, kalshi):
    async def go():
        app, r, client, auth, probe, mem = await _app(settings, kalshi, lambda m: SlowStore(m, INF))
        async with client:
            res, _, _ = await _stop(client, auth)
            assert res.status_code == 200 and r._stop_writes == 1
            await r.shutdown()
            assert await _until(lambda: r._stop_writes == 0, 1)
    run_virtual(go)

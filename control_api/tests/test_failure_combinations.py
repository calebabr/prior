"""Sprint 2A-1 failure combinations: Kalshi 503 / 429 / 409 during balance, positions, place and cancel reads and
writes, store errors on the arbitrage log write, and a Stop that lands mid-scan or mid-pass. Stop always wins and
no new order set is sent after it. Fakes only; no network."""
from __future__ import annotations

import asyncio
import threading

import pytest

from control_api import runner as rn
from control_api.tests.conftest import FakeKalshi
from control_api.tests.test_gate_integration import (  # noqa: F401  (demo_env is a fixture)
    DemoTrader, HttpError, arb_on, demo_env, last_opps)

pytestmark = pytest.mark.asyncio

PAUSED_409 = HttpError(409, '{"error": {"code": "trading_is_paused"}}')


# ============================================================ Kalshi errors around an arbitrage set
@pytest.mark.parametrize("status", [503, 429, 409])
async def test_balance_read_error_skips_the_set_and_places_nothing(demo_env, status):
    demo_env.trader.fail["get_balance"] = status
    await arb_on(demo_env)
    res = await demo_env.runner.arb_scan()
    assert res["scanned"] is True and demo_env.trader.orders == []
    assert (await last_opps(demo_env))[0]["executed"] == "skipped: balance HttpError"
    assert not demo_env.runner.arb_kill_switch()           # nothing was sent, so no legging risk


@pytest.mark.parametrize("status", [503, 429, 409])
async def test_positions_read_error_fails_closed(demo_env, status):
    demo_env.trader.fail["get_positions"] = status
    await arb_on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert (await last_opps(demo_env))[0]["executed"] == "skipped: gate positions unreadable"


@pytest.mark.parametrize("err", [503, 429, PAUSED_409])
async def test_place_error_on_the_first_leg_trips_the_kill_switch_and_stops_arb(demo_env, err, monkeypatch):
    """Stand-in scanner: a first-leg error raises, so the runner records ERROR and trips the kill switch.
    The real scanner (first-leg retry) skips the set instead, because nothing filled:
    a 5xx after its retries (order state unconfirmed here: this fake's get_orders cannot be called that way, so a
    normal-priority alert), a 4xx at once ("order rejected"). Neither trips the kill switch."""
    from control_api.services import arb as arbsvc
    sc = arbsvc.scanner()
    import kalshi_bot
    if not getattr(kalshi_bot, "IS_STANDIN", True):
        if hasattr(sc, "_sleep"):
            monkeypatch.setattr(sc, "_sleep", lambda s: None)          # no real retry spacing in tests
        demo_env.trader.fail["create_order"] = err
        await arb_on(demo_env)
        r = demo_env.runner
        await r.arb_scan()
        executed = (await last_opps(demo_env))[0]["executed"]
        assert executed.startswith("skipped: ")
        assert not r.arb_kill_switch() and r.arb_desired == "running"
        assert not any(a["priority"] == "urgent" for a in r.alerter.sent)
        if "order state unconfirmed" in executed:
            assert any(a["title"] == "Arbitrage order state unconfirmed" for a in r.alerter.sent)
        return
    demo_env.trader.fail["create_order"] = err
    await arb_on(demo_env)
    r = demo_env.runner
    await r.arb_scan()
    assert demo_env.trader.orders == []
    assert (await last_opps(demo_env))[0]["executed"] == "ERROR HttpError"
    assert r.arb_kill_switch() and r.arb_desired == "stopped"
    assert (await demo_env.store.get_state())["arb_desired"] == "stopped"
    assert any(a["priority"] == "urgent" for a in r.alerter.sent)
    n = len(demo_env.public.get_markets("KXDEMOA"))
    await r.arb_scan()                                      # the kill switch keeps it off
    assert demo_env.trader.orders == [] and n == 3


@pytest.mark.parametrize("nth", [2, 3])
async def test_place_error_mid_set_leaves_no_further_orders(demo_env, nth):
    demo_env.trader.fail_after["create_order"] = nth
    await arb_on(demo_env)
    await demo_env.runner.arb_scan()
    assert len(demo_env.trader.orders) == nth - 1
    assert demo_env.runner.arb_kill_switch()


async def test_arb_log_write_failure_does_not_lose_the_scan_or_the_alert(demo_env, monkeypatch):
    async def boom(doc):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(demo_env.store, "add_arb_opportunity", boom)
    await arb_on(demo_env)
    res = await demo_env.runner.arb_scan()
    assert res == {"scanned": True, "found": 1}
    assert len(demo_env.trader.orders) == 3                 # the set was placed...
    assert demo_env.runner.arb_recent[0]["executed"].startswith("FILLED")   # ...and shown from memory
    assert any(a["title"] == "Arbitrage executed" for a in demo_env.runner.alerter.sent)


async def test_arb_log_write_failure_still_counts_toward_the_day_cap_in_memory(demo_env, monkeypatch):
    async def boom(doc):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(demo_env.store, "add_arb_opportunity", boom)
    await arb_on(demo_env, arb_day_cap=7)
    for _ in range(3):
        await demo_env.runner.arb_scan()
    assert len(demo_env.trader.orders) == 6                 # the third set (9.36 > 7) is gated by memory alone


async def test_store_down_for_the_day_rebuild_fails_closed_for_executions(demo_env, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(demo_env.store, "arb_spend_since", boom)
    await arb_on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert demo_env.runner.arb_recent[0]["executed"] == "skipped: gate day spend unknown"


# ============================================================ Stop mid-scan
async def test_stop_mid_scan_after_the_first_set_sends_no_second_set(demo_env):
    demo_env.public.events = 2
    await arb_on(demo_env)
    r = demo_env.runner

    def stop_now(ticker):                                   # the synchronous first part of Stop()
        if not demo_env.trader.orders:
            r._stop_gen += 1
            r.arb_desired = "stopped"
    demo_env.trader.on_order = stop_now
    await r.arb_scan()
    assert len(demo_env.trader.orders) <= 3                 # at most the set that was already being sent
    docs = await last_opps(demo_env, 2)
    assert any(d["executed"] == "skipped: stopped" for d in docs)


async def test_stop_between_balance_read_and_send_blocks_the_set(demo_env):
    await arb_on(demo_env)
    r = demo_env.runner
    real = demo_env.trader.get_positions

    def positions_then_stop(**kw):
        out = real(**kw)
        r._stop_gen += 1
        r.arb_desired = "stopped"
        return out
    demo_env.trader.get_positions = positions_then_stop
    await r.arb_scan()
    assert demo_env.trader.orders == []
    assert (await last_opps(demo_env))[0]["executed"] == "skipped: stopped"


# ============================================================ Stop with Kalshi errors on cancel
class CancelFails(FakeKalshi):
    def __init__(self, err):
        super().__init__()
        self.err = err

    def cancel_order(self, order_id, market_ticker=None):
        raise self.err


@pytest.mark.parametrize("status", [503, 429, 409])
async def test_stop_wins_when_every_cancel_fails(env, status):
    env.kalshi = CancelFails(HttpError(status))
    env.runner.desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200
    body = r.json()
    assert body["desired"] == "stopped" and body["kill_switch"] is True and body["arb_kill_switch"] is True
    assert env.runner.desired == "stopped" and env.runner.kill_path.exists()
    assert (await env.store.get_state())["desired"] == "stopped"
    assert (await env.runner.tick("x"))["ran"] is False


@pytest.mark.parametrize("status", [503, 429, 409])
async def test_stop_wins_when_the_order_list_read_fails(env, status):
    env.kalshi.get_orders = lambda status_=None, **k: (_ for _ in ()).throw(HttpError(status))
    env.runner.desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200 and env.runner.kill_path.exists()
    assert any(e.startswith("cancel") for e in r.json()["errors"])


# ============================================================ Stop mid-pass, strategy failures
@pytest.fixture
def pass_env(env, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    env.runner.desired, env.runner.mode = "running", "demo"
    env.runner.last_monitor_utc = rn.utcnow()
    return env


async def _wait(cond, n=300):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_stop_mid_pass_wins_and_the_pass_cannot_restart_trading(pass_env, monkeypatch):
    env = pass_env
    r = env.runner
    r.RETRY_FIRST_DELAY = 0.01
    entered, release, calls = threading.Event(), threading.Event(), []

    def main(*a, **k):
        calls.append(1)
        entered.set()
        release.wait(5)
        return 0
    monkeypatch.setattr(r.ms, "main", main)
    task = asyncio.create_task(r._pass("t"))
    assert await _wait(entered.is_set)
    resp = await env.client.post("/api/bot/stop", headers=env.auth)        # lands while the pass is in flight
    assert resp.status_code == 200 and r.desired == "stopped" and r.kill_path.exists()
    env.kalshi.cancelled.clear()
    release.set()
    await task
    assert sorted(env.kalshi.cancelled) == ["o1", "o3"]                      # post-pass re-cancel of mm- only
    assert (await r.tick("x"))["ran"] is False and len(calls) == 1
    assert (await env.store.get_state())["desired"] == "stopped"


async def test_strategy_error_during_a_pass_is_recorded_and_stop_still_works(pass_env, monkeypatch):
    env = pass_env
    r = env.runner

    def main(*a, **k):
        raise HttpError(503)
    monkeypatch.setattr(r.ms, "main", main)
    await r._pass("t")
    assert r.last_error and "HttpError" in r.last_error and not r.running_pass
    assert any(a["title"] == "Trading pass error" for a in r.alerter.sent)
    assert (await env.client.post("/api/bot/stop", headers=env.auth)).status_code == 200
    assert r.desired == "stopped"


async def test_store_errors_after_a_pass_do_not_wedge_the_runner_or_block_stop(pass_env, monkeypatch):
    env = pass_env
    r = env.runner
    monkeypatch.setattr(r.ms, "main", lambda *a, **k: 0)

    async def boom(*a, **k):
        raise RuntimeError("atlas down")
    for name in ("add_heartbeat", "add_journal_rows", "add_account_snapshot"):
        monkeypatch.setattr(env.store, name, boom)
    await r._pass("t")
    assert r.running_pass is False
    resp = await env.client.post("/api/bot/stop", headers=env.auth)
    assert resp.status_code == 200 and r.kill_path.exists()


async def test_nonzero_exit_with_write_errors_alerts_once_and_is_a_failed_pass(pass_env, monkeypatch):
    env = pass_env
    r = env.runner

    def main(*a, **k):
        print("write_errors: cancel=1 place=0 paused=1\ntrading_paused")
        return 1
    monkeypatch.setattr(r.ms, "main", main)
    await r._pass("t")
    titles = [a["title"] for a in r.alerter.sent]
    assert titles.count("Trading paused by Kalshi") == 1 and sum(t.startswith("Order errors") for t in titles) == 1
    assert "Trading pass error" in titles                  # a non-zero exit is still a real failure
    assert DemoTrader  # keeps the shared helper import used

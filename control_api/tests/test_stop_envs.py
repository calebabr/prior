"""Stop fixes (owner-approved 2026-11-27): P2-H1 Stop cancels the bot's resting orders in every env with keys,
P3-A a Stop during a Start's checks wins, P3-B a failed cancel is reported.

Fakes follow Kalshi's shapes: GET /portfolio/orders?status=resting rows {order_id, client_order_id, ticker, side,
action, status: "resting", ...}; DELETE of an order that is already gone answers 404 (KalshiError.status). Synthetic
ids and tickers only. The two envs get separate fakes, so a cancel in the wrong env shows."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from control_api import runner as rn
from control_api.routes import bot as bot_routes
from control_api.runner import StopInProgress
from control_api.services import botfiles

botfiles.ensure_bot_path()
from kalshi_client import KalshiError  # noqa: E402

pytestmark = pytest.mark.asyncio
BAD_PREFIXES = ("store:", "kill_switch:", "arb_kill_switch:")


def order(oid, coid, ticker="KXDEMOB-26NOV27-B1"):
    return {"order_id": oid, "client_order_id": coid, "ticker": ticker, "side": "yes", "action": "buy",
            "type": "limit", "status": "resting", "yes_price_dollars": "0.9000", "remaining_count_fp": "1.00"}


class EnvKalshi:
    def __init__(self, env, orders=None, *, fail=(), gone=(), list_error=None, hang=None):
        self.env, self.cancelled, self.fail, self.gone = env, [], set(fail), set(gone)
        self.list_error, self.hang = list_error, hang
        self.orders = orders if orders is not None else [
            order(f"{env}-m1", "mm-0001"), order(f"{env}-a1", "arb-0001"), order(f"{env}-y1", "web-owner-1")]
        self.listed = 0

    def get_orders(self, status=None):
        assert status == "resting"
        self.listed += 1
        if self.hang is not None:
            self.hang.wait(10)
        if self.list_error:
            raise self.list_error
        return [dict(o) for o in self.orders]

    def cancel_order(self, order_id, market_ticker=None):
        if order_id in self.gone:
            raise KalshiError(404, {"error": {"code": "not_found"}}, "DELETE", f"/portfolio/events/orders/{order_id}")
        if order_id in self.fail:
            raise KalshiError(503, {"error": {"code": "service_unavailable"}}, "DELETE", "/portfolio/events/orders/x")
        self.cancelled.append(order_id)

    def __getattr__(self, name):
        if name in ("create_order", "amend_order", "batch_create_orders", "decrease_order"):
            raise AssertionError("Stop never creates or changes an order")
        raise AttributeError(name)


def _keys(monkeypatch, *envs):
    for e in envs:
        p = "KALSHI_PROD_" if e == "live" else "KALSHI_DEMO_"
        monkeypatch.setenv(p + "API_KEY_ID", "test-id")
        monkeypatch.setenv(p + "PRIVATE_KEY", "test-key")
    for e in {"demo", "live"} - set(envs):
        p = "KALSHI_PROD_" if e == "live" else "KALSHI_DEMO_"
        monkeypatch.delenv(p + "API_KEY_ID", raising=False)
        monkeypatch.delenv(p + "PRIVATE_KEY", raising=False)


def _wire(env, fakes):
    calls = []

    def factory(mode):
        calls.append(mode)
        return fakes.get(mode)
    env.runner.kalshi_factory = factory
    return calls


# ================================================================== P2-H1
@pytest.mark.parametrize("mode,other", [("demo", "live"), ("live", "demo")])
async def test_stop_cancels_bot_orders_in_both_envs_and_never_the_owners(env, monkeypatch, mode, other):
    _keys(monkeypatch, "demo", "live")
    fakes = {"demo": EnvKalshi("demo"), "live": EnvKalshi("live")}
    _wire(env, fakes)
    env.runner.mode = mode
    res = await env.runner.stop(by="t")
    assert res["errors"] == [] and res["cancelled"] == 4
    for e in ("demo", "live"):
        assert sorted(fakes[e].cancelled) == sorted([f"{e}-m1", f"{e}-a1"])      # never f"{e}-y1"


async def test_stop_skips_an_env_without_keys_silently(env, monkeypatch):
    _keys(monkeypatch, "demo")
    fakes = {"demo": EnvKalshi("demo"), "live": EnvKalshi("live")}
    calls = _wire(env, fakes)
    res = await env.runner.stop(by="t")
    assert res["errors"] == [] and calls == ["demo"] and fakes["live"].listed == 0


async def test_a_failing_other_env_is_reported_by_name(env, monkeypatch):
    _keys(monkeypatch, "demo", "live")
    fakes = {"demo": EnvKalshi("demo"), "live": EnvKalshi("live", list_error=RuntimeError("down"))}
    _wire(env, fakes)
    res = await env.runner.stop(by="t")
    assert res["errors"] == ["cancel (live): RuntimeError"]
    assert sorted(fakes["demo"].cancelled) == ["demo-a1", "demo-m1"] and res["desired"] == "stopped"
    assert not any(e.startswith(BAD_PREFIXES) for e in res["errors"])


@pytest.mark.slow
async def test_a_hung_other_env_never_delays_the_current_one(env, monkeypatch):
    _keys(monkeypatch, "demo", "live")
    gate = threading.Event()
    fakes = {"demo": EnvKalshi("demo"), "live": EnvKalshi("live", hang=gate)}
    _wire(env, fakes)
    monkeypatch.setattr(rn, "STOP_OTHER_ENV_WAIT_S", 0.5)
    r = env.runner
    try:
        t0 = time.monotonic()
        resp = await asyncio.wait_for(env.client.post("/api/bot/stop", headers=env.auth), 5)
        took = time.monotonic() - t0
        body = resp.json()
        assert resp.status_code == 200 and took < 2.0, took
        assert body["desired"] == "stopped" and body["kill_switch"] is True
        assert sorted(fakes["demo"].cancelled) == ["demo-a1", "demo-m1"]
        assert body["errors"] == ["cancel (live): timed out (still trying)"]
        # a second Stop while the live cancel still hangs: single-flight, the current env is cancelled again at once
        fakes["demo"].orders = [order("demo-m2", "mm-0002")]
        res2 = await asyncio.wait_for(r.stop(by="t2"), 3)
        assert "cancel (live): previous cancel still running" in res2["errors"]
        assert fakes["demo"].cancelled[-1] == "demo-m2"
    finally:
        gate.set()
    for _ in range(100):
        if sorted(fakes["live"].cancelled) == ["live-a1", "live-m1"]:
            break
        await asyncio.sleep(0.02)
    assert sorted(fakes["live"].cancelled) == ["live-a1", "live-m1"]        # the late cancel still completed


async def test_breaker_trip_is_a_full_stop_in_both_envs(env, monkeypatch):
    _keys(monkeypatch, "demo", "live")
    fakes = {"demo": EnvKalshi("demo"), "live": EnvKalshi("live")}
    _wire(env, fakes)
    await env.runner._breaker_trip({"reason": "drawdown", "limits": ["drawdown"], "env": "live", "utc": None})
    assert fakes["live"].cancelled and fakes["demo"].cancelled


@pytest.mark.slow
async def test_p4l1_a_hanging_other_env_at_takeover_never_delays_a_following_stop(env, monkeypatch):
    _keys(monkeypatch, "demo", "live")
    gate = threading.Event()
    threads: list[str] = []

    class Hanging(EnvKalshi):
        def get_orders(self, status=None):
            threads.append(threading.current_thread().name)
            return super().get_orders(status)
    fakes = {"demo": EnvKalshi("demo"), "live": Hanging("live", hang=gate)}
    _wire(env, fakes)
    r = env.runner
    r._pre_acquire_lease = {"holder": "another-instance"}               # a takeover of a stopped bot
    try:
        await r._become_leader(st={"desired": "stopped", "mode": "demo", "arb_desired": "stopped",
                                   "arb_mode": "demo"}, reload_config=False)
        for _ in range(100):
            if threads:
                break
            await asyncio.sleep(0.01)
        assert threads and threads[0].startswith("stop-other")           # never one of Stop's own workers
        fakes["demo"].orders = [order("demo-m9", "mm-0009")]
        t0 = time.monotonic()
        res = await asyncio.wait_for(r.stop(by="t"), 3)
        assert time.monotonic() - t0 < 1.0
        assert "demo-m9" in fakes["demo"].cancelled
        assert res["errors"] == ["cancel (live): previous cancel still running"]
        assert len(threads) == 1                                         # single-flight: no second live cancel
    finally:
        gate.set()


async def test_p4i2_a_mode_switch_mid_stop_still_reports_the_failure(env, monkeypatch):
    _keys(monkeypatch, "live")
    r = env.runner
    r.mode = "live"

    class SwitchWhileCancelling(EnvKalshi):
        def get_orders(self, status=None):
            r.mode = "demo"                                              # a switch lands during Stop's cancel
            return super().get_orders(status)
    fakes = {"live": SwitchWhileCancelling("live", fail={"live-m1"})}
    _wire(env, fakes)
    res = await r.stop(by="t")
    assert "cancel: 1 order(s) not cancelled" in res["errors"]


# ================================================================== P3-B
async def test_one_failed_cancel_is_reported_and_an_order_already_gone_is_not(env, monkeypatch):
    _keys(monkeypatch, "demo", "live")
    fakes = {"demo": EnvKalshi("demo", fail={"demo-m1"}, gone={"demo-a1"}),
             "live": EnvKalshi("live", fail={"live-a1"})}
    _wire(env, fakes)
    res = await env.runner.stop(by="t")
    assert res["errors"] == ["cancel: 1 order(s) not cancelled", "cancel (live): 1 order(s) not cancelled"]
    assert res["cancelled"] == 1 and fakes["live"].cancelled == ["live-m1"]
    assert not any(e.startswith(BAD_PREFIXES) for e in res["errors"])
    _keys(monkeypatch, "demo")
    fakes["demo"] = EnvKalshi("demo", gone={"demo-m1", "demo-a1"})
    assert (await env.runner.stop(by="t"))["errors"] == []                  # everything already gone: no error


# ================================================================== P3-A
async def test_a_stop_during_start_checks_wins(env):
    r = env.runner
    gen = r.stop_generation()
    await r.stop(by="phone")
    with pytest.raises(StopInProgress):
        await r.start_trading(by="t", stop_gen=gen)
    assert r.desired == "stopped" and r.kill_switch()
    assert (await env.store.get_state())["desired"] == "stopped"


async def test_route_a_stop_while_the_start_request_is_checked_wins(env, monkeypatch):
    real = bot_routes._start_body

    async def body_with_stop(request):
        await env.runner.stop(by="other-tab")          # lands after the route took its mark
        return await real(request)
    monkeypatch.setattr(bot_routes, "_start_body", body_with_stop)
    resp = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert resp.status_code == 409 and resp.json()["detail"] == "stopped_during_start"
    assert env.runner.desired == "stopped" and env.runner.kill_switch()


async def test_start_without_a_stop_still_works(env):
    resp = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert resp.status_code == 200 and env.runner.desired == "running"


async def test_a_stop_written_by_another_instance_during_the_request_wins(env):
    r = env.runner
    gen, t0 = r.stop_generation(), rn.utcnow()
    await env.store.set_state(by="other-instance", desired="stopped")      # the other instance's Stop write
    with pytest.raises(StopInProgress):
        await r.start_trading(by="t", stop_gen=gen, requested_utc=t0)
    assert (await env.store.get_state())["desired"] == "stopped"


async def test_arb_enable_and_switch_to_live_refuse_after_a_stop(env):
    r = env.runner
    gen = r.stop_generation()
    await r.stop(by="phone")
    with pytest.raises(StopInProgress):
        await r.set_arb(enabled=True, live=None, by="t", stop_gen=gen)
    assert r.arb_desired == "stopped"
    r.live_allowed = lambda: True
    with pytest.raises(StopInProgress):
        await r.set_mode("live", by="t", stop_gen=gen)
    assert r.mode == "demo"

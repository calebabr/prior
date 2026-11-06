"""Bracket arbitrage in the runner + /api/arb: log-only default, gates, Stop kills arb, legging kill switch."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio

LIVE_ENV = {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
            "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY"}


def _arm_live(env, monkeypatch):
    """Owner env with prod keys + confirm, snapshotted into Settings as at startup (S-19)."""
    for k, v in LIVE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(env.settings, "prod_keys_present", True)
    monkeypatch.setattr(env.settings, "live_confirm_ok", True)


class FakePublic:
    """One open 3-bracket event whose YES bids sum to 1.5: a fee-positive NO-arb."""

    def __init__(self):
        self.calls = 0

    def get_markets(self, series_ticker=None, status=None):
        self.calls += 1
        if series_ticker != "KXDEMOA":
            return []
        ev = "KXDEMOA-26OCT13"
        return [{"event_ticker": ev, "ticker": f"{ev}-B{i}"} for i in range(3)]

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook_fp": {"yes_dollars": [["0.50", "100"]], "no_dollars": []}}


class FakeTrader:
    def __init__(self, fill_ratio=1.0, cash_cents=100000, fail=False):
        self.fill_ratio, self.cash_cents, self.fail = fill_ratio, cash_cents, fail
        self.orders = []

    def get_balance(self):
        return {"balance": self.cash_cents}

    def get_positions(self, **kw):
        return {"market_positions": []}

    def create_order(self, *, ticker, side, count, price, client_order_id, **kw):
        if self.fail:
            raise RuntimeError("exchange error")
        self.orders.append((ticker, side, count, price, client_order_id))
        return {"order": {"fill_count_fp": str(float(count) * self.fill_ratio)}}

    def get_orders(self, status=None):
        return []


@pytest.fixture
def arb_env(env, real_strategy):
    env.public = FakePublic()
    env.runner.public_factory = lambda e: env.public
    env.runner.config = {**env.runner.config, "arb": {"arb_series": ["KXDEMOA"], "arb_min_profit": 0.01,
                                                      "arb_max_contracts": 2}}
    return env


async def _put(env, body):
    return await env.client.put("/api/arb", json=body, headers=await env.h2fa())


async def test_get_arb_defaults_log_only_off(arb_env):
    r = await arb_env.client.get("/api/arb", headers=arb_env.auth)
    assert r.status_code == 200
    b = r.json()
    assert b["settings"]["enabled"] is False and b["settings"]["live"] is False
    assert b["status"]["state"] == "stopped" and b["status"]["live_allowed"] is False
    assert set(b["schema"]) == {"arb_max_contracts", "arb_min_profit", "arb_series", "arb_poll_seconds",
                                "arb_scan_cap", "arb_day_cap", "arb_series_cap_frac", "arb_demo_execute",
                                "arb_complete_legs", "arb_set_loss_frac", "arb_scan_cap_frac", "arb_day_cap_frac",
                                "max_event_frac"}
    s = b["settings"]
    # Sprint 2B items 3, 4, 8: optional shares of equity, OFF (None) by default
    assert all(s[k] is None for k in ("arb_set_loss_frac", "arb_scan_cap_frac", "arb_day_cap_frac",
                                      "max_event_frac"))
    assert s["arb_scan_cap"] == 15 and s["arb_day_cap"] == 45 and s["arb_series_cap_frac"] == 0.35
    assert s["arb_demo_execute"] is False and b["status"]["demo_execute"] is False
    assert b["pnl"] is None and b["pnl_demo_execute"] is None and "expectation" in b


async def test_put_arb_requires_fresh_2fa(arb_env):
    r = await arb_env.client.put("/api/arb", json={"enabled": True}, headers=arb_env.auth)
    assert r.status_code == 403
    assert arb_env.runner.arb_desired == "stopped"


async def test_enable_is_log_only_and_scan_records_opportunity(arb_env):
    r = await _put(arb_env, {"enabled": True})
    assert r.status_code == 200 and r.json()["status"]["state"] == "log-only"
    assert (await arb_env.store.get_state())["arb_desired"] == "running"
    trader = FakeTrader()
    arb_env.runner.kalshi_factory = lambda m: trader
    res = await arb_env.runner.arb_scan()
    assert res == {"scanned": True, "found": 1}
    assert trader.orders == []                                   # log-only: nothing sent
    items = (await arb_env.client.get("/api/arb/opportunities", headers=arb_env.auth)).json()["items"]
    assert len(items) == 1 and items[0]["executed"] == "log-only" and items[0]["kind"] == "NO"
    assert items[0]["event"] == "KXDEMOA-26OCT13" and isinstance(items[0]["legs"], list)


async def test_scan_skipped_when_off(arb_env):
    assert (await arb_env.runner.arb_scan()) == {"scanned": False, "reason": "stopped"}
    assert arb_env.public.calls == 0


async def test_live_needs_confirm_and_env(arb_env):
    r = await _put(arb_env, {"enabled": True, "live": True})
    assert r.status_code == 400
    r = await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    assert r.status_code == 409 and r.json()["detail"] == "live_not_allowed"
    assert arb_env.runner.arb_mode == "demo"


async def test_live_executes_with_all_gates(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    r = await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    assert r.status_code == 200 and r.json()["status"]["state"] == "live"
    trader = FakeTrader()
    arb_env.runner.kalshi_factory = lambda m: trader
    await arb_env.runner.arb_scan()
    assert len(trader.orders) == 3 and all(o[4].startswith("arb-") for o in trader.orders)
    items = await arb_env.store.list_arb_opportunities(limit=5)
    assert items[0]["executed"].startswith("FILLED") and items[0]["mode"] == "live"
    pnl = (await arb_env.client.get("/api/arb", headers=arb_env.auth)).json()["pnl"]
    assert pnl and pnl["d30"] > 0


async def test_live_skips_when_cost_exceeds_cash_cap(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    trader = FakeTrader(cash_cents=100)                          # $1 cash
    arb_env.runner.kalshi_factory = lambda m: trader
    await arb_env.runner.arb_scan()
    assert trader.orders == []
    assert (await arb_env.store.list_arb_opportunities(limit=1))[0]["executed"].startswith("skipped: cost")


async def test_partial_fill_trips_kill_and_persists_arb_off(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    arb_env.runner.kalshi_factory = lambda m: FakeTrader(fill_ratio=0.5)
    await arb_env.runner.arb_scan()
    assert arb_env.runner.arb_kill_switch()
    assert arb_env.runner.arb_status()["kill_reason"] == "legging"
    assert arb_env.runner.arb_desired == "stopped"
    st = await arb_env.store.get_state()
    assert st["arb_desired"] == "stopped" and st["arb_mode"] == "demo"
    # re-enabling needs an explicit acknowledgement of the legging kill
    r = await _put(arb_env, {"enabled": True})
    assert r.status_code == 409 and r.json()["detail"] == "arb_legging_kill"
    r = await _put(arb_env, {"enabled": True, "ack_legging": True})
    assert r.status_code == 200 and not arb_env.runner.arb_kill_switch()


async def test_order_error_mid_set_trips_kill(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    arb_env.runner.kalshi_factory = lambda m: FakeTrader(fail=True)
    await arb_env.runner.arb_scan()
    assert arb_env.runner.arb_kill_switch() and arb_env.runner.arb_desired == "stopped"


async def test_stop_stops_arb_and_writes_arb_kill(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    r = await arb_env.client.post("/api/bot/stop", headers=arb_env.auth)
    assert r.status_code == 200 and r.json()["arb_kill_switch"] is True
    assert arb_env.runner.arb_desired == "stopped" and arb_env.runner.arb_mode == "demo"
    st = await arb_env.store.get_state()
    assert st["desired"] == "stopped" and st["arb_desired"] == "stopped" and st["arb_mode"] == "demo"
    assert arb_env.runner.arb_status()["kill_reason"] == "stop"
    assert (await arb_env.runner.arb_scan())["scanned"] is False
    # after Stop, enabling arb (2FA) clears a Stop kill switch without needing ack_legging
    r = await _put(arb_env, {"enabled": True})
    assert r.status_code == 200 and r.json()["status"]["state"] == "log-only"


async def test_stop_preserves_legging_record(arb_env):
    arb_env.runner.arb_kill_path.write_text(json.dumps({"reason": "partial fill / legging risk"}))
    await arb_env.client.post("/api/bot/stop", headers=arb_env.auth)
    assert arb_env.runner.arb_status()["kill_reason"] == "legging"


async def test_stop_mid_scan_blocks_execution(arb_env, monkeypatch):
    _arm_live(arb_env, monkeypatch)
    await _put(arb_env, {"enabled": True, "live": True, "confirm": "LIVE"})
    trader = FakeTrader()
    arb_env.runner.kalshi_factory = lambda m: trader
    runner = arb_env.runner
    real = arb_env.public.get_orderbook

    def book_then_stop(t, depth=None):
        runner._stop_gen += 1                                   # what Stop does first, synchronously
        runner.arb_desired = "stopped"
        return real(t)
    arb_env.public.get_orderbook = book_then_stop
    await runner.arb_scan()
    assert trader.orders == []


async def test_settings_validation_and_ceilings(arb_env):
    bad = {"arb_max_contracts": 10_000, "arb_poll_seconds": 2, "arb_series": ["KXDEMOA", "KXDEMOA"],
           "arb_min_profit": "x", "cells": []}
    r = await _put(arb_env, bad)
    assert r.status_code == 400
    msgs = " ".join(r.json()["detail"])
    for k in ("arb_max_contracts", "arb_poll_seconds", "arb_series", "arb_min_profit", "cells"):
        assert k in msgs
    r = await _put(arb_env, {"arb_poll_seconds": "15"})
    assert r.status_code == 400                                  # type errors are 400, never 422
    r = await _put(arb_env, {"arb_max_contracts": 3, "arb_poll_seconds": 20, "arb_series": ["KXDEMOB"]})
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["arb_max_contracts"] == 3 and s["arb_poll_seconds"] == 20 and s["arb_series"] == ["KXDEMOB"]
    stored = await arb_env.store.get_config()
    assert stored["arb"]["arb_max_contracts"] == 3
    on_disk = json.loads(arb_env.runner.config_path.read_text(encoding="utf-8"))
    assert on_disk["arb"]["arb_poll_seconds"] == 20 and "_README" in on_disk
    assert on_disk["cells"] == stored["cells"]


async def test_maker_config_put_keeps_arb_block(arb_env):
    await _put(arb_env, {"arb_max_contracts": 4})
    r = await arb_env.client.put("/api/config", json={"run_budget": 30}, headers=await arb_env.h2fa())
    assert r.status_code == 200
    assert (await arb_env.store.get_config())["arb"]["arb_max_contracts"] == 4


async def test_runner_disabled_refuses_arb(arb_env):
    arb_env.settings.runner_enabled = False
    try:
        r = await _put(arb_env, {"enabled": True})
        assert r.status_code == 409
        arb_env.runner.arb_desired = "running"
        assert (await arb_env.runner.arb_scan())["reason"] == "runner_disabled"
    finally:
        arb_env.settings.runner_enabled = True


async def test_status_has_arb_block(arb_env):
    b = (await arb_env.client.get("/api/status", headers=arb_env.auth)).json()
    assert b["arb"]["state"] == "stopped" and "last_scan_utc" in b["arb"]


async def test_external_arb_kill_switch_is_persisted_on_next_scan(arb_env):
    await _put(arb_env, {"enabled": True})
    arb_env.runner.arb_kill_path.write_text(json.dumps({"reason": "partial fill / legging risk"}))
    res = await arb_env.runner.arb_scan()
    assert res["scanned"] is False and arb_env.public.calls == 0
    assert arb_env.runner.arb_desired == "stopped"
    assert (await arb_env.store.get_state())["arb_desired"] == "stopped"

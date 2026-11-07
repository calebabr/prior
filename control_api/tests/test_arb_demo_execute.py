"""Arbitrage demo-execute (contract section 3) and the arb side of the account risk gate (section 2).

Every refusal is tested: live maker mode, live/prod keys or a production base URL on the client, stopped, shutting
down, not leader, the arb kill switch, and a Stop mid-scan. The gate: scan cap, day cap (rebuilt from the store
after a restart), series cap, unreadable positions, store down. No network: fake clients only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.tests.conftest import preflight_ready

from control_api import runner as rn
from control_api.services import arb as arbsvc

pytestmark = pytest.mark.asyncio

DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_URL = "https://external-api.kalshi.com/trade-api/v2"
SET_COST = 3.12          # 3 brackets x 2 contracts x ($0.50 + $0.02 fee per contract)


class TwoEventPublic:
    """Open 3-bracket events in KXDEMOA whose YES bids sum to 1.5: fee-positive NO-arbs ($3.12 a set)."""

    def __init__(self, events=1):
        self.events = events
        self.calls = 0

    def get_markets(self, series_ticker=None, status=None):
        self.calls += 1
        if series_ticker != "KXDEMOA":
            return []
        out = []
        for e in range(self.events):
            ev = f"KXDEMOA-26OCT2{e}"
            out += [{"event_ticker": ev, "ticker": f"{ev}-B{i}"} for i in range(3)]
        return out

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook_fp": {"yes_dollars": [["0.50", "100"]], "no_dollars": []}}


class DemoTrader:
    def __init__(self, base_url=DEMO_URL, fill_ratio=1.0, cash_cents=100000, positions=None, fail_positions=False):
        self.base_url = base_url
        self.fill_ratio, self.cash_cents = fill_ratio, cash_cents
        self.positions = positions or []
        self.fail_positions = fail_positions
        self.orders = []

    def get_balance(self):
        return {"balance": self.cash_cents}

    def get_positions(self, **kw):
        if self.fail_positions:
            raise RuntimeError("positions down")
        return {"market_positions": list(self.positions)}

    def create_order(self, *, ticker, side, count, price, client_order_id, **kw):
        self.orders.append((ticker, side, count, price, client_order_id))
        return {"order": {"fill_count_fp": str(float(count) * self.fill_ratio)}}

    def get_orders(self, status=None):
        return []


@pytest.fixture
def demo_env(env, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "demo-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "not-a-real-key")
    env.public = TwoEventPublic()
    env.runner.public_factory = lambda e: env.public
    env.trader = DemoTrader()
    env.factory_modes = []

    def factory(mode):
        env.factory_modes.append(mode)
        return env.trader
    env.runner.kalshi_factory = factory
    env.runner.config = {**env.runner.config, "arb": {"arb_series": ["KXDEMOA"], "arb_min_profit": 0.01,
                                                      "arb_max_contracts": 2}}
    return env


async def _put(env, body):
    return await env.client.put("/api/arb", json=body, headers=await env.h2fa())


async def _on(env, **extra):
    r = await _put(env, {"enabled": True, "arb_demo_execute": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def _last(env, n=1):
    return await env.store.list_arb_opportunities(limit=n)


async def test_default_is_log_only(demo_env):
    r = await _put(demo_env, {"enabled": True})
    assert r.status_code == 200 and r.json()["settings"]["arb_demo_execute"] is False
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == [] and demo_env.factory_modes == []
    assert (await _last(demo_env))[0]["executed"] == "log-only"


async def test_demo_execute_places_on_the_demo_exchange(demo_env):
    b = await _on(demo_env)
    assert b["status"]["demo_execute"] is True and b["status"]["state"] == "log-only"
    res = await demo_env.runner.arb_scan()
    assert res == {"scanned": True, "found": 1}
    assert set(demo_env.factory_modes) == {"demo"}                       # never the live factory
    assert len(demo_env.trader.orders) == 3 and all(o[4].startswith("arb-") for o in demo_env.trader.orders)
    doc = (await _last(demo_env))[0]
    assert doc["mode"] == "demo-execute" and doc["executed"].startswith("FILLED")
    view = (await demo_env.client.get("/api/arb", headers=demo_env.auth)).json()
    assert view["pnl"] is None                                           # never mixed into the live figure
    assert view["pnl_demo_execute"]["demo"] is True and view["pnl_demo_execute"]["sets"] == 1


async def test_turning_demo_execute_on_needs_fresh_2fa(demo_env):
    r = await demo_env.client.put("/api/arb", json={"arb_demo_execute": True}, headers=demo_env.auth)
    assert r.status_code == 403
    assert demo_env.runner.arb_settings()["arb_demo_execute"] is False


async def test_never_when_the_maker_is_live(demo_env):
    await _on(demo_env)
    demo_env.runner.mode = "live"
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == [] and demo_env.factory_modes == []
    assert (await _last(demo_env))[0]["mode"] == "log-only"
    assert demo_env.runner.arb_status()["demo_execute"] is False


async def test_never_with_a_production_base_url(demo_env):
    await _on(demo_env)
    demo_env.trader.base_url = PROD_URL
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert (await _last(demo_env))[0]["executed"] == "skipped: not the demo exchange"


async def test_never_without_a_base_url(demo_env):
    await _on(demo_env)
    demo_env.trader.base_url = None
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []


async def test_never_with_the_live_key_as_the_demo_key(demo_env, monkeypatch):
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "demo-id")              # same key id pasted in both fields
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY", "not-a-real-key")
    await _on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == [] and demo_env.factory_modes == []
    assert (await _last(demo_env))[0]["executed"] == "skipped: no demo keys"


async def test_never_without_demo_keys(demo_env, monkeypatch):
    monkeypatch.delenv("KALSHI_DEMO_API_KEY_ID")
    await _on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == [] and (await _last(demo_env))[0]["executed"] == "skipped: no demo keys"


@pytest.mark.parametrize("block", ["not_leader", "shutting_down", "kill_switch", "stopped", "stop_gen", "stopping",
                                   "arb_live_mode", "setting_off", "runner_disabled"])
async def test_execute_rechecks_every_refusal(demo_env, block):
    await _on(demo_env)
    r = demo_env.runner
    gen = r._stop_gen
    assert r._arb_demo_ok(gen)
    if block == "not_leader":
        r._leader_ready = False
    elif block == "shutting_down":
        r._shutting_down = True
    elif block == "kill_switch":
        r.arb_kill_path.write_text("stopped by test\n")
    elif block == "stopped":
        r.arb_desired = "stopped"
    elif block == "stop_gen":
        r._stop_gen += 1
    elif block == "stopping":
        r._stopping += 1
    elif block == "arb_live_mode":
        r.arb_mode = "live"
    elif block == "setting_off":
        r.config = {**r.config, "arb": {**r.config["arb"], "arb_demo_execute": False}}
    elif block == "runner_disabled":
        demo_env.settings.runner_enabled = False
    try:
        assert not r._arb_demo_ok(gen)
        op = arbsvc.scanner().best_opportunity("KXDEMOA-26OCT20", {
            f"KXDEMOA-26OCT20-B{i}": {"yes_dollars": [["0.50", "100"]]} for i in range(3)}, 2)
        budget = {"scan": 0.0, "day": {"date": "x", "amount": 0.0, "mark": None}}
        assert r._arb_execute_sync(arbsvc.scanner(), op, gen, "demo-execute", budget) == "skipped: stopped"
        assert demo_env.trader.orders == []
    finally:
        demo_env.settings.runner_enabled = True
        r._stopping = 0
        r._shutting_down = False


async def test_stop_mid_scan_blocks_demo_execution(demo_env):
    await _on(demo_env)
    runner = demo_env.runner
    real = demo_env.public.get_orderbook

    def book_then_stop(t, depth=None):
        runner._stop_gen += 1                                   # what Stop does first, synchronously
        runner.arb_desired = "stopped"
        return real(t)
    demo_env.public.get_orderbook = book_then_stop
    await runner.arb_scan()
    assert demo_env.trader.orders == []


async def test_partial_fill_trips_the_legging_kill_switch(demo_env):
    await _on(demo_env)
    demo_env.trader.fill_ratio = 0.5
    await demo_env.runner.arb_scan()
    r = demo_env.runner
    assert r.arb_kill_switch() and r.arb_status()["kill_reason"] == "legging" and r.arb_desired == "stopped"
    assert (await demo_env.store.get_state())["arb_desired"] == "stopped"
    assert any("legging" in a["title"] for a in r.alerter.sent)


# ---------------------------------------------------------------- the risk gate on executions
async def test_scan_cap_stops_the_second_set_in_one_scan(demo_env):
    demo_env.public.events = 2
    await _on(demo_env, arb_scan_cap=5)
    await demo_env.runner.arb_scan()
    docs = await _last(demo_env, 2)
    ex = sorted(d["executed"] for d in docs)
    assert ex[0].startswith("FILLED") and ex[1].startswith("skipped: gate scan cap")
    assert len(demo_env.trader.orders) == 3


async def test_day_cap_counts_across_scans(demo_env):
    await _on(demo_env, arb_day_cap=7)
    for _ in range(3):
        await demo_env.runner.arb_scan()
    docs = await _last(demo_env, 3)
    filled = [d for d in docs if d["executed"].startswith("FILLED")]
    gated = [d for d in docs if d["executed"].startswith("skipped: gate day cap")]
    assert len(filled) == 2 and len(gated) == 1                         # 2 x 3.12 <= 7 < 3 x 3.12


async def test_day_spend_is_rebuilt_from_the_store_after_a_restart(demo_env):
    now = datetime.now(timezone.utc)
    base = {"event": "KXDEMOA-OLD", "kind": "NO", "contracts": 2, "profit": 0.5, "legs": [], "sum_best": 1.5}
    await demo_env.store.add_arb_opportunity({**base, "cost": 44.0, "executed": "FILLED {}", "mode": "demo-execute",
                                              "ts": now - timedelta(seconds=2)})
    # neither of these counts: another kind (live) and yesterday
    await demo_env.store.add_arb_opportunity({**base, "cost": 44.0, "executed": "FILLED {}", "mode": "live",
                                              "ts": now - timedelta(seconds=1)})
    if now.hour >= 1:
        await demo_env.store.add_arb_opportunity({**base, "cost": 44.0, "executed": "FILLED {}",
                                                  "mode": "demo-execute", "ts": now - timedelta(days=1)})
    await _on(demo_env)                                                  # default day cap $45
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert (await _last(demo_env))[0]["executed"].startswith("skipped: gate day cap 44.00")


async def test_day_spend_unknown_fails_closed(demo_env, monkeypatch):
    await _on(demo_env)

    async def boom(*a, **kw):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(demo_env.store, "arb_spend_since", boom)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert demo_env.runner.arb_recent[0]["executed"] == "skipped: gate day spend unknown"


@pytest.mark.parametrize("bad", [float("nan"), -1.0, float("inf")])
async def test_unusable_day_spend_fails_closed(demo_env, monkeypatch, bad):
    await _on(demo_env)

    async def weird(*a, **kw):
        return bad
    monkeypatch.setattr(demo_env.store, "arb_spend_since", weird)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert demo_env.runner.arb_recent[0]["executed"] == "skipped: gate day spend unknown"


# ---------------------------------------------------------------- S2A1-1: skip-doc flood + restart
async def test_repeated_skips_are_stored_once_an_hour(demo_env):
    await _on(demo_env, arb_day_cap=1)                                   # every set is gated
    r = demo_env.runner
    for _ in range(20):
        await r.arb_scan()
    stored = [d for d in await _last(demo_env, 100) if d["executed"].startswith("skipped:")]
    assert len(stored) == 1 and stored[0]["executed"].startswith("skipped: gate day cap")
    assert len([d for d in r.arb_recent if d["executed"].startswith("skipped:")]) == 20   # in memory: all
    t = rn.utcnow()
    key = next(iter(r._arb_skip_seen))
    r._arb_skip_seen[key] = t - rn.ARB_SKIP_PERSIST_EVERY                 # an hour later: stored again
    await r.arb_scan()
    assert len([d for d in await _last(demo_env, 100) if d["executed"].startswith("skipped:")]) == 2


async def test_another_reason_or_event_is_stored(demo_env):
    r = demo_env.runner
    t = rn.utcnow()
    assert r._arb_skip_persist_ok("E1", "skipped: gate day cap 1.00 + 3.12 > 1.00", t)
    assert not r._arb_skip_persist_ok("E1", "skipped: gate day cap 2.00 + 3.12 > 1.00", t)   # same kind
    assert r._arb_skip_persist_ok("E1", "skipped: gate scan cap 0.00 + 3.12 > 1.00", t)
    assert r._arb_skip_persist_ok("E2", "skipped: gate day cap 1.00 + 3.12 > 1.00", t)
    assert r._arb_skip_persist_ok("E1", "FILLED {}", t) and r._arb_skip_persist_ok("E1", "FILLED {}", t)
    assert r._arb_skip_persist_ok("E1", "log-only", t)


async def test_skip_memory_is_bounded(demo_env, monkeypatch):
    monkeypatch.setattr(rn, "ARB_SKIP_SEEN_MAX", 50)
    r = demo_env.runner
    t = rn.utcnow()
    for i in range(500):
        assert r._arb_skip_persist_ok(f"E{i}", "skipped: gate day cap", t)
    assert len(r._arb_skip_seen) <= 50


async def test_restart_after_a_flood_still_sees_the_morning_spend(demo_env):
    """The cap was reached early; >5,000 skip docs came after. A restart must rebuild the full spend."""
    now = datetime.now(timezone.utc)
    base = {"event": "KXDEMOA-AM", "kind": "NO", "contracts": 2, "profit": 0.5, "legs": [], "sum_best": 1.5,
            "mode": "demo-execute"}
    for i in range(3):
        await demo_env.store.add_arb_opportunity({**base, "cost": 15.0, "executed": "FILLED {}",
                                                  "ts": now - timedelta(seconds=30 - i)})
    for i in range(5200):                                                 # the flood (e.g. from an older release)
        await demo_env.store.add_arb_opportunity({**base, "cost": 3.12, "event": f"KXDEMOA-F{i}",
                                                  "executed": "skipped: gate day cap 45.00 + 3.12 > 45.00",
                                                  "ts": now - timedelta(seconds=10)})
    r = demo_env.runner
    r._arb_day.clear()                                                    # what a restart leaves in memory
    await _on(demo_env)                                                   # default day cap $45
    await r.arb_scan()
    assert demo_env.trader.orders == []
    assert r.arb_recent[0]["executed"].startswith("skipped: gate day cap 45.00")


async def test_leadership_change_rebuilds_but_never_below_memory(demo_env):
    await _on(demo_env, arb_day_cap=7)
    r = demo_env.runner
    await r.arb_scan()                                                    # 3.12 spent, stored
    r._arb_day["demo-execute"]["amount"] = 6.0                            # memory knows more than the store
    r.lease.transitions += 1                                              # new leadership mark: rebuild
    r._ready_mark = (r.lease.generation, r.lease.transitions)
    st = await r._arb_day_state("demo-execute")
    assert st["amount"] == 6.0


async def test_series_cap_uses_maker_and_arb_positions_together(demo_env):
    # cash $100, KXDEMOA already holds $18: ceiling 0.5 x 118 = 59, series cap 0.35 x 59 = 20.65 < 18 + 3.12
    demo_env.trader.cash_cents = 10000
    demo_env.trader.positions = [{"ticker": "KXDEMOA-26OCT19-B1", "market_exposure_dollars": "18.00"}]
    await _on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert (await _last(demo_env))[0]["executed"].startswith("skipped: gate series cap KXDEMOA")


async def test_unreadable_positions_fail_closed(demo_env):
    demo_env.trader.fail_positions = True
    await _on(demo_env)
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == []
    assert (await _last(demo_env))[0]["executed"] == "skipped: gate positions unreadable"


async def test_live_arb_goes_through_the_same_gate(demo_env, monkeypatch, real_strategy):
    for k, v in {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
                 "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(demo_env.settings, "prod_keys_present", True)
    monkeypatch.setattr(demo_env.settings, "live_confirm_ok", True)
    demo_env.runner.arming["arbitrage"] = True     # S3 item 10: live arbitrage needs arming
    preflight_ready(monkeypatch)                 # S3 L-1: live re-runs the preflight
    r = await _put(demo_env, {"enabled": True, "live": True, "confirm": "LIVE", "arb_scan_cap": 2})
    assert r.status_code == 200 and r.json()["status"]["demo_execute"] is False
    demo_env.trader.base_url = PROD_URL
    await demo_env.runner.arb_scan()
    assert demo_env.trader.orders == [] and set(demo_env.factory_modes) == {"live"}
    doc = (await _last(demo_env))[0]
    assert doc["mode"] == "live" and doc["executed"].startswith("skipped: gate scan cap")

"""Numbers match Kalshi, own trades show (2026-11-27 sprint, B1-B4).

B1 GET /api/account/summary, B2 cost + legs on /api/account/positions rows, B3 the single-flight fills sync that
does not need a bot pass, B4 env=demo|live|all on /api/activity and /api/history.

Fakes follow Kalshi's documented response shapes: GET /portfolio/balance -> {balance (int cents), balance_dollars
(fixed-point dollar string), portfolio_value (int cents), updated_ts}; GET /portfolio/positions -> {market_positions:
[{ticker, position_fp, market_exposure_dollars, ...}], event_positions, cursor}; GET /markets/{t} -> {market: {...,
yes_bid_dollars, no_bid_dollars, mve_collection_ticker, mve_selected_legs: [{event_ticker, market_ticker, side}]}}.
All tickers, amounts and ids are synthetic."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as runner_mod
from control_api.routes import data as datar
from control_api.routes import ux

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc)
WRITES = ("create_order", "cancel_order", "amend_order", "batch_create_orders", "batch_cancel_orders",
          "decrease_order")


class SummaryKalshi:
    """GET-only fake account. Any write method raises (the account views and the sync never write)."""

    def __init__(self, *, balance=None, positions=None, pages=1, markets=None):
        self.balance = {"balance": 10000, "balance_dollars": "100.0000", "portfolio_value": 3700,
                        "updated_ts": 1700000000} if balance is None else balance
        self.positions = positions if positions is not None else [
            {"ticker": "KXDEMOB-26JAN01-B1", "position_fp": "10.00", "market_exposure_dollars": "30.0000",
             "total_traded_dollars": "30.0000", "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000"},
            {"ticker": "KXDEMOC-26JAN01-B2", "position_fp": "-5.00", "market_exposure_dollars": "10.0000",
             "total_traded_dollars": "10.0000", "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000"}]
        self.pages = pages
        self.markets = markets or {}
        self.calls: dict[str, int] = {}

    def _hit(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_balance(self):
        self._hit("get_balance")
        return dict(self.balance)

    def get_positions(self, **kw):
        self._hit("get_positions")
        page = int(kw.get("cursor") or 0)
        nxt = page + 1
        per = max(1, -(-len(self.positions) // self.pages))
        chunk = self.positions[page * per:(page + 1) * per]
        return {"market_positions": chunk, "event_positions": [],
                "cursor": str(nxt) if nxt < self.pages else ""}

    def get_orders(self, **kw):
        return []

    def get_market(self, t):
        self._hit("get_market")
        m = {"ticker": t, "title": f"Market {t}", "yes_bid_dollars": "0.3500", "no_bid_dollars": "0.6000",
             "last_price_dollars": "0.3600"}
        m.update(self.markets.get(t, {}))
        return m

    def __getattr__(self, name):
        if name in WRITES:
            raise AssertionError(f"{name}: no Kalshi writes allowed")
        raise AttributeError(name)


async def _summary(env, **params):
    return await env.client.get("/api/account/summary", headers=env.auth, params=params)


# ---------------------------------------------------------------- B1
async def test_summary_matches_kalshi_fields(env):
    k = SummaryKalshi()
    env.kalshi = k
    r = await _summary(env)
    assert r.status_code == 200
    body = r.json()
    assert body["env"] == "demo" and datetime.fromisoformat(body["as_of"]).tzinfo is not None
    assert body["cash"] == 100.0 and body["positions_value"] == 37.0 and body["positions_cost"] == 40.0
    assert body["total_value"] == 137.0 and body["total_at_cost"] == 140.0 and body["unrealized"] == -3.0
    assert (await _summary(env)).json() == body                               # cached 30 s
    assert k.calls["get_balance"] == 1


async def test_summary_cents_only_balance_and_zero_positions(env):
    """A balance with only the integer-cents fields; no positions: 0 is a real value, not null."""
    env.kalshi = SummaryKalshi(balance={"balance": 2550, "portfolio_value": 0, "updated_ts": 1}, positions=[])
    body = (await _summary(env)).json()
    assert body["cash"] == 25.5 and body["positions_cost"] == 0.0 and body["positions_value"] == 0.0
    assert body["total_value"] == 25.5 and body["unrealized"] == 0.0


@pytest.mark.parametrize("pv", [None, "nan", "inf", "", True])
async def test_summary_missing_portfolio_value_is_null_never_estimated(env, pv):
    bal = {"balance": 10000, "updated_ts": 1}
    if pv is not None:
        bal["portfolio_value"] = pv
    env.kalshi = SummaryKalshi(balance=bal)
    body = (await _summary(env)).json()
    assert body["positions_value"] is None and body["total_value"] is None and body["unrealized"] is None
    assert body["cash"] == 100.0 and body["positions_cost"] == 40.0 and body["total_at_cost"] == 140.0


async def test_summary_follows_positions_cursor(env):
    pos = [{"ticker": f"KXDEMOB-26JAN01-B{i}", "position_fp": "1.00", "market_exposure_dollars": "1.5000"}
           for i in range(6)]
    k = SummaryKalshi(positions=pos, pages=3)
    env.kalshi = k
    body = (await _summary(env)).json()
    assert k.calls["get_positions"] == 3 and body["positions_cost"] == 9.0


async def test_summary_truncated_or_unreadable_positions_null_cost(env, monkeypatch):
    pos = [{"ticker": f"KXDEMOB-26JAN01-B{i}", "position_fp": "1.00", "market_exposure_dollars": "1.0000"}
           for i in range(30)]
    env.kalshi = SummaryKalshi(positions=pos, pages=30)
    monkeypatch.setattr(ux.recon, "POSITION_PAGES", 20)               # more pages than we read: truncated
    body = (await _summary(env)).json()
    assert body["positions_cost"] is None and body["total_at_cost"] is None and body["unrealized"] is None
    assert body["positions_value"] == 37.0 and body["total_value"] == 137.0

    class Broken(SummaryKalshi):
        def get_positions(self, **kw):
            raise RuntimeError("positions down")
    env.kalshi = Broken()
    env.app.state.account_summary_cache = {}
    body = (await _summary(env)).json()
    assert body["positions_cost"] is None and body["cash"] == 100.0

    env.kalshi = SummaryKalshi(positions=[{"ticker": "KXDEMOB-X", "position_fp": "1", "market_exposure_dollars": "nan"}])
    env.app.state.account_summary_cache = {}
    assert (await _summary(env)).json()["positions_cost"] is None


async def test_summary_errors_and_rate_limit(env, monkeypatch):
    env.kalshi = None
    r = await _summary(env)
    assert r.status_code == 409 and r.json()["detail"] == "kalshi_not_configured"

    class Down(SummaryKalshi):
        def get_balance(self):
            raise RuntimeError("balance down")
    env.kalshi = Down()
    r = await _summary(env)
    assert r.status_code == 503 and r.json()["detail"] == "kalshi_unavailable"

    env.kalshi = SummaryKalshi()
    n = ux.RATE_LIMITS["account_summary"][0]
    codes = [(await _summary(env)).status_code for _ in range(n)]          # 2 already used above
    assert codes[-1] == 429 and set(codes) == {200, 429}


async def test_summary_needs_sign_in(env):
    assert (await env.client.get("/api/account/summary")).status_code == 401


async def test_summary_cache_is_per_mode(env):
    env.kalshi = SummaryKalshi()
    assert (await _summary(env)).json()["env"] == "demo"
    env.runner.mode = "live"
    env.kalshi = SummaryKalshi(balance={"balance": 500, "portfolio_value": 100, "updated_ts": 1})
    body = (await _summary(env)).json()
    assert body["env"] == "live" and body["cash"] == 5.0          # never the demo copy


async def test_summary_numbers_pure():
    assert ux.summary_numbers({"cash": None, "positions_cost": 1.0, "positions_value": 2.0}) == {
        "cash": None, "positions_cost": 1.0, "positions_value": 2.0, "total_value": None, "total_at_cost": None,
        "unrealized": 1.0}


# ---------------------------------------------------------------- B2
async def test_positions_cost_and_combo_legs(env):
    combo = "KXMVEDEMO-26JAN01-ABC123"
    legs = [{"event_ticker": f"KXDEMOE-26JAN01-{i}", "market_ticker": f"KXDEMOE-26JAN01-{i}-Y", "side": "yes"}
            for i in range(10)]
    k = SummaryKalshi(positions=[
        {"ticker": "KXDEMOB-26JAN01-B1", "position_fp": "10.00", "market_exposure_dollars": "8.0000"},
        {"ticker": combo, "position_fp": "4.00", "market_exposure_dollars": "1.0000"}],
        markets={combo: {"title": "yes A, yes B, yes C, yes D, ... (a very long combo title)",
                         "mve_collection_ticker": "KXMVEDEMO", "mve_selected_legs": legs}})
    env.kalshi = k
    await env.store.upsert_fills([          # 6 of the 10 contracts were the bot's, 4 the owner's
        {"fill_id": "b1", "order_id": "O1", "ticker": "KXDEMOB-26JAN01-B1", "side": "yes", "yes_price_dollars": "0.8",
         "count_fp": "6", "ts": NOW - timedelta(hours=3), "source": "bot", "strategy": "maker", "env": "demo"},
        {"fill_id": "y1", "order_id": "M1", "ticker": "KXDEMOB-26JAN01-B1", "side": "yes", "yes_price_dollars": "0.8",
         "count_fp": "4", "ts": NOW - timedelta(hours=2), "source": "you", "env": "demo"}])
    rows = (await env.client.get("/api/account/positions", headers=env.auth)).json()
    b1 = {r["source"]: r for r in rows if r["ticker"] == "KXDEMOB-26JAN01-B1"}
    assert b1["bot"]["cost"] == 4.8 and b1["you"]["cost"] == 3.2 and b1["bot"]["count"] == 6
    assert b1["bot"]["legs"] is None and b1["bot"]["title"] == "Market KXDEMOB-26JAN01-B1"
    assert b1["bot"]["value"] == 2.1                                     # value keeps its meaning: count x bid
    c = next(r for r in rows if r["ticker"] == combo)
    assert c["legs"] == 10 and c["title"] == "10-leg combo" and c["cost"] == 1.0 and c["source"] == "you"


async def test_positions_cost_null_when_kalshi_gives_none(env):
    env.kalshi = SummaryKalshi(positions=[{"ticker": "KXDEMOB-26JAN01-B1", "position_fp": "3.00"}])
    rows = (await env.client.get("/api/account/positions", headers=env.auth)).json()
    assert rows[0]["cost"] is None and rows[0]["price"] is None


async def test_combo_legs_shapes():
    assert ux.combo_legs({"mve_selected_legs": [{"market_ticker": "A"}, {"market_ticker": "B"}]}) == 2
    for m in ({}, {"mve_selected_legs": []}, {"mve_selected_legs": None}, {"mve_selected_legs": "x"}, None):
        assert ux.combo_legs(m) is None


# ---------------------------------------------------------------- B3
def _blocking_fetch(runner, gate: threading.Event, calls: list, result=([], [])):
    def fetch(mode, since):
        calls.append(mode)
        gate.wait(10)
        return result
    runner._fetch_fills_sync = fetch


async def _until(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


def _enable(env):
    env.runner.fills_on_demand = True
    env.runner.role = lambda: "leader"


async def test_on_demand_sync_is_single_flight_and_rate_limited(env):
    r = env.runner
    _enable(env)
    gate, calls = threading.Event(), []
    _blocking_fetch(r, gate, calls)
    assert r.request_fills_sync("read:x") is True
    assert [r.request_fills_sync("read:x") for _ in range(5)] == [False] * 5     # one at a time
    await _until(lambda: calls)
    assert r.fills_sync_busy() and r.status()["fills_sync"]["in_flight"] is True
    # the pass's own sync shares the lock: it does not run a second copy (no double alerts) and does not wait
    t0 = time.monotonic()
    assert (await r.sync_fills("demo", force=True))["reason"] == "in_flight"
    assert time.monotonic() - t0 < 0.5
    gate.set()
    await _until(lambda: not r.fills_sync_busy())
    assert calls == ["demo"] and r.last_fills_sync_utc is not None
    assert r.status()["fills_sync"]["trigger"] == "read:x"
    assert r.request_fills_sync("read:x") is False                    # 2-minute minimum interval
    r._fills_attempt_utc = runner_mod.utcnow() - runner_mod.FILLS_SYNC_ON_DEMAND_MIN - timedelta(seconds=1)
    assert r.request_fills_sync("read:x") is True
    await _until(lambda: len(calls) == 2)
    await _until(lambda: not r.fills_sync_busy())
    assert len(calls) == 2


async def test_on_demand_sync_backs_off_after_failure(env):
    r = env.runner
    _enable(env)

    def boom(mode, since):
        raise RuntimeError("kalshi down")
    r._fetch_fills_sync = boom
    assert r.request_fills_sync("read:x") is True
    await _until(lambda: r.fills_sync_error is not None and not r.fills_sync_busy())
    assert r.request_fills_sync("read:x") is False                    # same 2-minute back-off after a failure


async def test_on_demand_sync_off_unless_started_leader_and_not_shutting_down(env):
    r = env.runner
    calls: list = []
    r._fetch_fills_sync = lambda mode, since: calls.append(mode) or ([], [])
    assert r.fills_on_demand is False and r.request_fills_sync("read:x") is False   # loops not started (tests)
    r.fills_on_demand = True
    r.role = lambda: "standby"
    assert r.request_fills_sync("read:x") is False
    r.role = lambda: "leader"
    r._shutting_down = True
    try:
        assert r.request_fills_sync("read:x") is False
    finally:
        r._shutting_down = False
    assert calls == []


async def test_on_demand_sync_uses_current_mode_and_upserts(env):
    r = env.runner
    _enable(env)
    r.mode = "live"
    fill = {"fill_id": "f-live-1", "order_id": "M9", "ticker": "KXMVEDEMO-26JAN01-XYZ", "side": "yes",
            "action": "buy", "yes_price_dollars": "0.2000", "count_fp": "5.00", "is_taker": True,
            "created_time": (NOW - timedelta(minutes=5)).isoformat()}
    seen: list = []
    r._fetch_fills_sync = lambda mode, since: (seen.append(mode) or [dict(fill)], [])
    assert r.request_fills_sync("read:summary") is True
    await _until(lambda: not r.fills_sync_busy() and r.last_fills_sync_utc is not None)
    assert seen == ["live"]
    stored = await env.store.list_fills(limit=10)
    assert [f["fill_id"] for f in stored] == ["f-live-1"] and stored[0]["env"] == "live"


@pytest.mark.parametrize("path", ["/api/account/summary", "/api/account/positions", "/api/activity"])
async def test_reads_trigger_sync_without_waiting(env, path):
    r = env.runner
    _enable(env)
    env.kalshi = SummaryKalshi()
    gate, calls = threading.Event(), []
    _blocking_fetch(r, gate, calls)
    try:
        t0 = time.monotonic()
        resp = await env.client.get(path, headers=env.auth)
        took = time.monotonic() - t0
        assert resp.status_code == 200 and took < 2.0                      # the hung sync is not awaited
        await _until(lambda: calls)
        assert r.fills_sync_busy()
    finally:
        gate.set()
    await _until(lambda: not r.fills_sync_busy())


async def test_idle_timer_loop_syncs_while_stopped(env, monkeypatch):
    r = env.runner
    _enable(env)
    assert r.desired != "running"
    calls: list = []
    r._fetch_fills_sync = lambda mode, since: calls.append(mode) or ([], [])
    monkeypatch.setattr(runner_mod, "TIMER_SECONDS", 0.01)
    task = asyncio.create_task(r._timer_loop())
    try:
        await _until(lambda: calls)
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert calls == ["demo"]                                                # 2-minute minimum holds in the loop


@pytest.mark.slow
async def test_stop_stays_fast_while_sync_hangs(env):
    r = env.runner
    _enable(env)
    gate, calls = threading.Event(), []
    _blocking_fetch(r, gate, calls)
    try:
        await r.start_trading(by="t")
        assert r.request_fills_sync("read:x") is True
        await _until(lambda: calls)
        # also fill the telemetry pool's other worker with a hung read
        r._tel_executor.submit(gate.wait, 10)
        t0 = time.monotonic()
        resp = await asyncio.wait_for(env.client.post("/api/bot/stop", headers=env.auth), 3)
        took = time.monotonic() - t0
        assert resp.status_code == 200 and took < 1.0, took
        assert r.desired == "stopped" and r.fills_sync_busy()             # the sync is still hung
    finally:
        gate.set()
    await _until(lambda: not r.fills_sync_busy(), timeout=5)


@pytest.mark.slow
async def test_timed_out_fetch_thread_blocks_new_syncs_until_it_ends(env, monkeypatch):
    r = env.runner
    _enable(env)
    monkeypatch.setattr(runner_mod, "FILLS_SYNC_TIMEOUT_S", 0.2)
    gate, calls = threading.Event(), []
    _blocking_fetch(r, gate, calls)
    try:
        res = await r.sync_fills("demo", force=True)
        assert res["synced"] is False and r.fills_sync_error == "TimeoutError"
        assert not r._fills_lock.locked() and r.fills_sync_busy()           # thread still running
        assert (await r.sync_fills("demo", force=True))["reason"] == "in_flight"
    finally:
        gate.set()
    await _until(lambda: not r.fills_sync_busy())
    assert len(calls) == 1


# ---------------------------------------------------------------- B4
async def _seed_envs(store):
    hb_demo = NOW - timedelta(hours=6)
    hb_live = NOW - timedelta(hours=2)
    await store.add_heartbeat({"ts": hb_demo, "ok": True, "mode": "demo", "duration_s": 20.0})
    await store.add_heartbeat({"ts": hb_live, "ok": True, "mode": "live", "duration_s": 20.0})
    await store.add_journal_rows([
        {"utc": (hb_demo - timedelta(seconds=10)).isoformat(), "event": "quote", "ticker": "KXDEMOB-26JAN01-B1",
         "note": "demo pass"},
        {"utc": (hb_live - timedelta(seconds=10)).isoformat(), "event": "quote", "ticker": "KXDEMOB-26JAN01-B1",
         "note": "live pass"},
        {"utc": (NOW - timedelta(hours=4)).isoformat(), "event": "fence_blocked", "ticker": "",
         "note": "outside any pass"}])
    await store.upsert_fills([
        {"fill_id": "d1", "ticker": "KXDEMOC-26JAN01-B1", "side": "yes", "yes_price_dollars": "0.9", "count_fp": "1",
         "ts": NOW - timedelta(hours=5), "source": "bot", "strategy": "maker", "env": "demo"},
        {"fill_id": "old", "ticker": "KXDEMOD-26JAN01-B1", "side": "yes", "yes_price_dollars": "0.9",
         "count_fp": "1", "ts": NOW - timedelta(hours=5), "source": "bot", "strategy": "maker"},     # no env: demo
        {"fill_id": "l1", "ticker": "KXMVEDEMO-26JAN01-Q", "side": "yes", "yes_price_dollars": "0.2",
         "count_fp": "5", "ts": NOW - timedelta(hours=1), "source": "you", "env": "live"}])
    await store.upsert_settlements([
        {"ticker": "KXDEMOC-26JAN01-B1", "market_result": "yes", "ts": NOW - timedelta(minutes=50)},
        {"ticker": "KXMVEDEMO-26JAN01-Q", "market_result": "no", "ts": NOW - timedelta(minutes=40)},
        {"ticker": "KXDEMOZ-26JAN01-B1", "market_result": "no", "ts": NOW - timedelta(minutes=30)}])   # no fills
    await store.add_account_snapshot({"ts": NOW - timedelta(hours=3), "cash": 50.0, "exposure": 1.0, "env": "demo"})
    await store.add_account_snapshot({"ts": NOW - timedelta(hours=2), "cash": 90.0, "exposure": 2.0, "env": "live"})
    await store.add_account_snapshot({"ts": NOW - timedelta(hours=1), "cash": 40.0, "exposure": 0.0})   # no env


async def _activity(e, **params):
    r = await e.client.get("/api/activity", headers=e.auth, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_activity_env_filter(env):
    await _seed_envs(env.store)
    live = await _activity(env, env="live")
    assert live["env"] == "live"
    assert [f["fill_id"] for f in live["fills"]] == ["l1"]
    assert {i["note"]: i["env"] for i in live["items"]} == {"live pass": "live", "outside any pass": None}
    assert {s["ticker"]: s["envs"] for s in live["settlements"]} == {"KXMVEDEMO-26JAN01-Q": ["live"],
                                                                     "KXDEMOZ-26JAN01-B1": []}
    assert next(s for s in live["settlements"] if s["ticker"] == "KXMVEDEMO-26JAN01-Q")["source"] == "you"

    demo = await _activity(env, env="demo")
    assert sorted(f["fill_id"] for f in demo["fills"]) == ["d1", "old"]
    assert all(f["env"] == "demo" for f in demo["fills"])                    # a fill without env counts as demo
    assert {i["note"] for i in demo["items"]} == {"demo pass", "outside any pass"}
    assert {s["ticker"] for s in demo["settlements"]} == {"KXDEMOC-26JAN01-B1", "KXDEMOZ-26JAN01-B1"}

    every = await _activity(env, env="all")
    assert len(every["fills"]) == 3 and len(every["items"]) == 3 and len(every["settlements"]) == 3


async def test_activity_default_is_current_mode(env):
    await _seed_envs(env.store)
    assert (await _activity(env))["env"] == "demo"
    env.runner.mode = "live"
    body = await _activity(env)
    assert body["env"] == "live" and [f["fill_id"] for f in body["fills"]] == ["l1"]
    assert (await env.client.get("/api/activity", headers=env.auth, params={"env": "prod"})).status_code == 422


async def test_history_env_filter(env):
    await _seed_envs(env.store)

    async def hist(**p):
        r = await env.client.get("/api/history", headers=env.auth, params=p)
        assert r.status_code == 200
        return r.json()
    demo = await hist()
    assert demo["env"] == "demo" and [s["cash"] for s in demo["snapshots"]] == [50.0, 40.0]
    assert all(s["env"] == "demo" for s in demo["snapshots"])
    assert [s["cash"] for s in (await hist(env="live"))["snapshots"]] == [90.0]
    assert [s["cash"] for s in (await hist(env="all"))["snapshots"]] == [50.0, 90.0, 40.0]
    env.runner.mode = "live"
    assert [s["cash"] for s in (await hist())["snapshots"]] == [90.0]
    assert (await env.client.get("/api/history", headers=env.auth, params={"env": "x"})).status_code == 422


async def test_journal_env_rule():
    t = NOW
    w = datar._pass_windows([{"ts": t, "mode": "live", "duration_s": 30},
                             {"ts": t + timedelta(minutes=20), "mode": "demo"},       # no duration: 900 s window
                             {"ts": t, "mode": None}])
    assert datar.journal_env({"ts": t - timedelta(seconds=20)}, w) == "live"
    assert datar.journal_env({"ts": t - timedelta(seconds=60)}, w) is None            # inside no pass window
    assert datar.journal_env({"ts": t + timedelta(minutes=10)}, w) == "demo"
    both = datar._pass_windows([{"ts": t, "mode": "live", "duration_s": 30}, {"ts": t, "mode": "demo",
                                                                              "duration_s": 30}])
    assert datar.journal_env({"ts": t}, both) is None                                 # ambiguous: shown everywhere
    assert datar.journal_env({"ts": "not a time"}, w) is None

"""Independent adversarial tests (H3-B) for arbitrage tail-leg completion in the runner and the opposite-set gate.

Written from the requirement only. Drives `runner.arb_scan()` and `/api/arb` with fake public/trading clients.
No network. Checks: completion only in demo-execute with `arb_complete_legs` on (default off = old behavior), never
live; the completed set keeps profit >= 0; extra spend counts against the scan cap, day cap and total ceiling (also
after a restart); at most one attempt; mid-way failure -> PARTIAL + kill switch; Stop / not leader / shutting down /
kill switch at send time -> nothing sent; Stop latency unaffected; the opposite-set gate (blocked, same kind and other
events allowed, rebuilt after a restart, skipped sets never block, fails closed).
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as rn
from control_api.services import arb as arbsvc

pytestmark = pytest.mark.asyncio

DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_URL = "https://external-api.kalshi.com/trade-api/v2"
EV0 = "KXDEMOA-26OCT20"
SET_COST = 3.12          # 3 brackets x 2 NO at 0.50 + 0.04 fee per order


def bk(yes=(), no=()):
    return {"yes_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in yes],
            "no_dollars": [[f"{p:.4f}", f"{q:.2f}"] for p, q in no]}


NO_BOOK = bk(yes=[(0.50, 100)], no=[(0.30, 100)])          # the NO-side sum is over one dollar: a NO set
YES_BOOK = bk(yes=[(0.05, 100)], no=[(0.70, 100)])         # the YES-side sum is under one dollar: a YES set


class Public:
    """Events in KXDEMOA with 3 brackets each; `books[ticker]` is what the scanner reads."""

    def __init__(self, events=1, book=NO_BOOK):
        self.events = events
        self.books = {}
        for e in range(events):
            for i in range(3):
                self.books[f"KXDEMOA-26OCT2{e}-B{i}"] = dict(book)
        self.calls = 0

    def get_markets(self, series_ticker=None, status=None):
        self.calls += 1
        if series_ticker != "KXDEMOA":
            return []
        return [{"event_ticker": t.rsplit("-", 1)[0], "ticker": t} for t in self.books]

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook_fp": self.books[ticker]}


def _avail(book, side, limit):
    if side == "bid":
        return sum(float(q) for p, q in book.get("no_dollars") or [] if float(p) >= 1 - limit - 1e-9)
    return sum(float(q) for p, q in book.get("yes_dollars") or [] if float(p) >= limit - 1e-9)


class Trader:
    """Scripted fills per ticker for the FIRST order on that ticker in a scan; later orders fill from `books`
    (the exchange's current book, IOC). Records everything."""

    def __init__(self, books, base_url=DEMO_URL, cash_cents=100000):
        self.books = books
        self.base_url = base_url
        self.cash_cents = cash_cents
        self.first_fill: dict[str, float] = {}
        self.orders: list[dict] = []
        self.fills: list[float] = []
        self.book_reads: list[str] = []
        self.pre_reads: list[str] = []
        self.seen: set[str] = set()
        self.raise_on_completion = False
        self.book_hook = None
        self.positions: list = []

    def get_balance(self):
        """Starting cash minus what this fake has spent (as the exchange would report it)."""
        return {"balance": self.cash_cents - int(round(spent(self) * 100))}

    def get_positions(self, **kw):
        """Static positions plus what this fake has filled so far (as the exchange would report them)."""
        held: dict[str, float] = {}
        for o, f in zip(self.orders, self.fills):
            if f > 0:
                pb = float(o["price"]) if o["side"] == "bid" else 1 - float(o["price"])
                held[o["ticker"]] = held.get(o["ticker"], 0.0) + f * pb
        return {"market_positions": list(self.positions)
                + [{"ticker": t, "market_exposure_dollars": f"{x:.2f}"} for t, x in held.items()]}

    def get_orders(self, status=None, **kw):
        return []

    def cancel_order(self, order_id, market_ticker=None):
        return {}

    def get_orderbook(self, ticker, depth=None, **kw):
        # completion reads only: the overlaid scanner also re-reads the thinnest legs BEFORE the first order
        (self.book_reads if self.orders else self.pre_reads).append(ticker)
        if self.book_hook:
            self.book_hook(ticker)
        return {"orderbook_fp": self.books[ticker]}

    def create_order(self, *, ticker, side, count, price, client_order_id, **kw):
        n = float(count)
        first = ticker not in self.seen
        self.seen.add(ticker)
        self.orders.append({"ticker": ticker, "side": side, "count": count, "price": price,
                            "client_order_id": client_order_id, **kw})
        if not first and self.raise_on_completion:
            self.fills.append(0.0)
            raise RuntimeError("exchange error")
        if first and ticker in self.first_fill:
            f = min(n, self.first_fill[ticker])
        elif first:
            f = n
        else:
            f = min(n, _avail(self.books[ticker], side, float(price)))
        self.fills.append(f)
        return {"order": {"fill_count_fp": f"{f:.2f}"}}


def spent(trader):
    fee = arbsvc.scanner().taker_fee
    tot = 0.0
    for o, f in zip(trader.orders, trader.fills):
        if f > 0:
            pb = float(o["price"]) if o["side"] == "bid" else 1 - float(o["price"])
            tot += f * pb + fee(f, pb)
    return tot


def set_profit(trader, event, kind, n):
    per = {}
    for o, f in zip(trader.orders, trader.fills):
        if o["ticker"].startswith(event + "-"):
            per[o["ticker"]] = per.get(o["ticker"], 0) + f
    legs = [t for t in trader.books if t.startswith(event + "-")]
    sets = min(per.get(t, 0) for t in legs)
    fee = arbsvc.scanner().taker_fee
    cost = 0.0
    for o, f in zip(trader.orders, trader.fills):
        if f > 0 and o["ticker"].startswith(event + "-"):
            pb = float(o["price"]) if o["side"] == "bid" else 1 - float(o["price"])
            cost += f * pb + fee(f, pb)
    return (len(legs) - 1 if kind == "NO" else 1) * sets - cost, per


@pytest.fixture
def denv(env, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "demo-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "not-a-real-key")
    env.public = Public()
    env.runner.public_factory = lambda e: env.public
    env.trader = Trader({t: dict(b) for t, b in env.public.books.items()})
    env.factory_modes = []

    def factory(mode):
        env.factory_modes.append(mode)
        return env.trader
    env.runner.kalshi_factory = factory
    env.runner.config = {**env.runner.config, "arb": {"arb_series": ["KXDEMOA"], "arb_min_profit": 0.01,
                                                      "arb_max_contracts": 2}}
    return env


def _set_public(env, pub):
    env.public = pub
    env.runner.public_factory = lambda e: env.public
    env.trader.books.clear()
    env.trader.books.update({t: dict(b) for t, b in pub.books.items()})


async def _put(env, body, fresh=True):
    h = await env.h2fa() if fresh else env.auth
    return await env.client.put("/api/arb", json=body, headers=h)


async def _on(env, complete=True, **extra):
    body = {"enabled": True, "arb_demo_execute": True, **extra}
    if complete is not None:
        body["arb_complete_legs"] = complete
    r = await _put(env, body)
    assert r.status_code == 200, r.text
    return r.json()


async def _last(env, n=1):
    return await env.store.list_arb_opportunities(limit=n)


def _tail_missing(env, ticker=f"{EV0}-B2"):
    env.trader.first_fill[ticker] = 0.0


# ================================================================ the setting
async def test_setting_defaults_off_and_is_in_the_schema(denv):
    b = (await denv.client.get("/api/arb", headers=denv.auth)).json()
    assert b["settings"]["arb_complete_legs"] is False
    assert "arb_complete_legs" in b["schema"] and b["schema"]["arb_complete_legs"]["type"] == "boolean"
    assert arbsvc.defaults()["arb_complete_legs"] is False


async def test_setting_needs_fresh_2fa(denv):
    r = await _put(denv, {"arb_complete_legs": True}, fresh=False)
    assert r.status_code == 403
    assert denv.runner.arb_settings()["arb_complete_legs"] is False


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "yes", [True]])
async def test_setting_rejects_non_bool(denv, bad):
    r = await _put(denv, {"arb_complete_legs": bad})
    assert r.status_code == 400 and "arb_complete_legs" in " ".join(r.json()["detail"])
    assert denv.runner.arb_settings()["arb_complete_legs"] is False


async def test_setting_is_versioned_and_audited(denv):
    before = len(await denv.store.list_config_versions(limit=100))
    r = await _put(denv, {"arb_complete_legs": True})
    assert r.status_code == 200 and r.json()["settings"]["arb_complete_legs"] is True
    assert (await denv.store.get_config())["arb"]["arb_complete_legs"] is True
    assert len(await denv.store.list_config_versions(limit=100)) == before + 1
    assert any(a.get("action") == "arb.update" for a in await denv.store.list_audit(limit=20))
    # a maker config PUT keeps it
    r = await denv.client.put("/api/config", json={"run_budget": 30}, headers=await denv.h2fa())
    assert r.status_code == 200 and (await denv.store.get_config())["arb"]["arb_complete_legs"] is True


# ================================================================ default off: exactly today's behavior
@pytest.mark.parametrize("complete", [None, False])
async def test_off_partial_trips_kill_exactly_as_before(denv, complete):
    await _on(denv, complete=complete)
    _tail_missing(denv)
    await denv.runner.arb_scan()
    r = denv.runner
    assert len(denv.trader.orders) == 3 and denv.trader.book_reads == []
    doc = (await _last(denv))[0]
    assert doc["executed"].startswith("PARTIAL") and "completed" not in doc["executed"]
    assert r.arb_kill_switch() and r.arb_status()["kill_reason"] == "legging" and r.arb_desired == "stopped"
    assert any("legging" in a["title"] for a in r.alerter.sent)


async def test_off_full_fill_unchanged(denv):
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3 and denv.trader.book_reads == []
    assert (await _last(denv))[0]["executed"].startswith("FILLED") and not denv.runner.arb_kill_switch()


# ================================================================ completion happy path
async def test_partial_completed_keeps_profit_and_arb_running(denv):
    await _on(denv)
    _tail_missing(denv)
    res = await denv.runner.arb_scan()
    assert res["scanned"] is True
    doc = (await _last(denv))[0]
    assert doc["executed"].startswith("FILLED (completed)"), doc["executed"]
    assert doc["mode"] == "demo-execute"
    r = denv.runner
    assert not r.arb_kill_switch() and r.arb_desired == "running"
    assert len(denv.trader.orders) == 4 and denv.trader.orders[3]["ticker"] == f"{EV0}-B2"
    assert not denv.trader.orders[3].get("post_only")
    profit, per = set_profit(denv.trader, EV0, "NO", 2)
    assert profit >= -1e-9 and all(v == 2 for v in per.values())
    assert set(denv.factory_modes) == {"demo"}
    titles = [a["title"] for a in r.alerter.sent]
    assert any("executed" in t.lower() for t in titles) and not any("legging" in t for t in titles)


async def test_completed_set_counts_actual_spend_in_the_day_total(denv):
    """Tail re-read dearer (NO at 0.60): actual spend 2.08 + 1.24 = 3.32 > planned 3.12."""
    await _on(denv)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    assert (await _last(denv))[0]["executed"].startswith("FILLED (completed)")
    actual = spent(denv.trader)
    assert abs(actual - 3.32) < 1e-6
    assert denv.runner._arb_day["demo-execute"]["amount"] + 1e-6 >= actual


async def test_day_spend_after_restart_includes_the_completion_extra(denv):
    """After a restart the day spend is rebuilt from the store: it must still include the completion's extra
    spend (the stored doc's planned cost alone under-counts)."""
    await _on(denv)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    actual = spent(denv.trader)
    rebuilt = await denv.store.arb_spend_since(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0), "demo-execute")
    assert rebuilt + 1e-6 >= actual, (rebuilt, actual)


async def test_completed_set_pnl_is_not_overstated(denv):
    """The arb P&L sums the stored `profit` of FILLED sets: after a dearer completion (actual 0.68 vs planned 0.88)
    the stored profit must not exceed the real guaranteed profit, and the stored cost must cover the real spend."""
    await _on(denv)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    doc = (await _last(denv))[0]
    assert doc["executed"].startswith("FILLED (completed)")
    profit, _ = set_profit(denv.trader, EV0, "NO", 2)
    assert doc["profit"] <= profit + 1e-6, (doc["profit"], profit)
    assert doc["cost"] + 1e-6 >= spent(denv.trader), (doc["cost"], spent(denv.trader))
    pnl = (await denv.client.get("/api/arb", headers=denv.auth)).json()["pnl_demo_execute"]
    assert pnl is None or pnl["d30"] <= profit + 1e-6


# ================================================================ extra spend vs caps
async def test_completion_refused_when_actual_spend_would_pass_the_scan_cap(denv):
    await _on(denv, arb_scan_cap=3.2)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])     # tail costs 1.24
    await denv.runner.arb_scan()
    assert spent(denv.trader) <= 3.2 + 1e-9
    doc = (await _last(denv))[0]
    assert doc["executed"].startswith("PARTIAL") and denv.runner.arb_kill_switch()
    assert len(denv.trader.orders) == 3


async def test_completion_refused_when_actual_spend_would_pass_the_day_cap(denv):
    await _on(denv, arb_day_cap=3.2)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    assert spent(denv.trader) <= 3.2 + 1e-9
    assert (await _last(denv))[0]["executed"].startswith("PARTIAL") and denv.runner.arb_kill_switch()


async def test_completion_refused_when_actual_spend_would_pass_the_series_cap(denv):
    """One series, so the series cap (0.9 x ceiling) binds before the total ceiling: set 3.12 fits, 3.32 doesn't."""
    r = denv.runner
    try:
        frac = float(r.config.get("max_open_frac") or 0.5)
    except (TypeError, ValueError):
        frac = 0.5
    frac = min(frac, float(r.settings.max_open_frac))
    denv.trader.cash_cents = int(3.22 / 0.9 / frac * 100) + 1
    ceil = denv.trader.cash_cents / 100 * frac * 0.9
    assert SET_COST <= ceil < 3.32
    await _on(denv, arb_series_cap_frac=0.9)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await r.arb_scan()
    assert spent(denv.trader) <= ceil + 1e-9
    assert (await _last(denv))[0]["executed"].startswith("PARTIAL") and r.arb_kill_switch()


async def test_completion_refused_when_actual_spend_would_pass_the_total_ceiling(denv):
    """Other-series exposure X=2 so the TOTAL ceiling binds first: X + cost <= f x (cash + X) allows ~3.25."""
    r = denv.runner
    try:
        f = float(r.config.get("max_open_frac") or 0.5)
    except (TypeError, ValueError):
        f = 0.5
    f = min(f, float(r.settings.max_open_frac))
    x = 2.0
    denv.trader.cash_cents = int(round((3.25 + x * (1 - f)) / f * 100))
    cash = denv.trader.cash_cents / 100
    allow = f * (cash + x) - x
    assert SET_COST <= allow < 3.32 and 3.32 <= 0.9 * f * (cash + x) and 3.32 <= cash * f
    denv.trader.positions = [{"ticker": "KXDEMOB-26OCT19-B1", "market_exposure_dollars": f"{x:.2f}"}]
    await _on(denv, arb_series_cap_frac=0.9)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await r.arb_scan()
    assert spent(denv.trader) <= allow + 1e-9, (spent(denv.trader), allow)


async def test_completion_refused_when_actual_spend_would_pass_the_cash_cap(denv):
    """The runner's own pre-check (set cost <= cash x max_open_frac) must also hold for the completed spend."""
    r = denv.runner
    try:
        frac = float(r.config.get("max_open_frac") or 0.5)
    except (TypeError, ValueError):
        frac = 0.5
    frac = min(frac, float(r.settings.max_open_frac))
    # other-series exposure lifts the ceiling, so only the cash cap (cash x frac) is tight
    denv.trader.cash_cents = int(3.22 / frac * 100) + 1
    cap = denv.trader.cash_cents / 100 * frac
    denv.trader.positions = [{"ticker": "KXDEMOB-26OCT19-B1", "market_exposure_dollars": "40.00"}]
    assert SET_COST <= cap < 3.32
    await _on(denv, arb_series_cap_frac=0.9)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await r.arb_scan()
    assert spent(denv.trader) <= cap + 1e-9, (spent(denv.trader), cap)


async def test_completion_extra_counts_for_later_sets_in_the_same_scan(denv):
    """Event 0 completes (actual 3.32); event 1 (3.12) must then be gated by a 6.3 scan cap (3.32 + 3.12 > 6.3)."""
    _set_public(denv, Public(events=2))
    await _on(denv, arb_scan_cap=6.3)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    assert spent(denv.trader) <= 6.3 + 1e-9, spent(denv.trader)


async def test_completion_extra_counts_for_later_scans_in_the_day(denv):
    await _on(denv, arb_day_cap=6.3)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.40, 100)], no=[(0.30, 100)])
    await denv.runner.arb_scan()
    denv.trader.seen.clear()
    denv.trader.first_fill.clear()
    _set_public(denv, Public(events=2))
    # only event 1 is new (event 0's NO set is held; it is the same kind so it may repeat; either way <= cap)
    await denv.runner.arb_scan()
    assert spent(denv.trader) <= 6.3 + 1e-9, spent(denv.trader)


# ================================================================ refusals -> kill switch as before
async def test_unprofitable_completion_refused(denv):
    await _on(denv)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.04, 100)], no=[(0.30, 100)])     # completed set would lose 0.01
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3
    assert (await _last(denv))[0]["executed"].startswith("PARTIAL") and denv.runner.arb_kill_switch()


async def test_never_in_live_even_with_the_setting_on(denv, monkeypatch, real_strategy):
    for k, v in {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
                 "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(denv.settings, "prod_keys_present", True)
    monkeypatch.setattr(denv.settings, "live_confirm_ok", True)
    r = await _put(denv, {"enabled": True, "live": True, "confirm": "LIVE", "arb_complete_legs": True,
                          "arb_demo_execute": True})
    assert r.status_code == 200 and r.json()["status"]["state"] == "live"
    denv.trader.base_url = PROD_URL
    _tail_missing(denv)
    await denv.runner.arb_scan()
    assert set(denv.factory_modes) == {"live"}
    assert len(denv.trader.orders) == 3 and denv.trader.book_reads == []      # no completion attempt
    doc = (await _last(denv))[0]
    assert doc["mode"] == "live" and doc["executed"].startswith("PARTIAL") and denv.runner.arb_kill_switch()


async def test_never_when_the_maker_is_live(denv):
    await _on(denv)
    denv.runner.mode = "live"
    _tail_missing(denv)
    await denv.runner.arb_scan()
    assert denv.trader.orders == [] and denv.factory_modes == []


async def test_never_with_a_production_base_url(denv):
    await _on(denv)
    denv.trader.base_url = PROD_URL
    _tail_missing(denv)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_production_url_swapped_in_after_the_set_blocks_completion(denv):
    """The client's base URL changes between the set and the completion (re-asserted on the sending client)."""
    await _on(denv)
    _tail_missing(denv)

    def hook(t):
        denv.trader.base_url = PROD_URL
    denv.trader.book_hook = hook
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3
    assert (await _last(denv))[0]["executed"].startswith("PARTIAL")


@pytest.mark.parametrize("block", ["stop", "not_leader", "shutting_down", "kill_switch", "stopping", "setting_off",
                                   "maker_live", "arb_live_mode"])
async def test_state_change_at_send_time_blocks_completion(denv, block):
    """The change lands while the completion re-reads the book (i.e. after approve, right before sending)."""
    await _on(denv)
    _tail_missing(denv)
    r = denv.runner

    def hook(t):
        if block == "stop":
            r._stop_gen += 1
            r.arb_desired = "stopped"
        elif block == "not_leader":
            r._leader_ready = False
        elif block == "shutting_down":
            r._shutting_down = True
        elif block == "kill_switch":
            r.arb_kill_path.write_text("stopped by test\n")
        elif block == "stopping":
            r._stopping += 1
        elif block == "setting_off":
            r.config = {**r.config, "arb": {**r.config["arb"], "arb_complete_legs": False}}
        elif block == "maker_live":
            r.mode = "live"
        elif block == "arb_live_mode":
            r.arb_mode = "live"
    denv.trader.book_hook = hook
    try:
        await r.arb_scan()
        assert len(denv.trader.orders) == 3, block
        assert not (await _last(denv))[0]["executed"].startswith("FILLED"), block
    finally:
        r._stopping = 0
        r._shutting_down = False


async def test_completion_failure_midway_trips_kill_and_partial(denv):
    await _on(denv)
    _tail_missing(denv)
    denv.trader.raise_on_completion = True
    await denv.runner.arb_scan()
    doc = (await _last(denv))[0]
    assert doc["executed"].startswith("PARTIAL"), doc["executed"]
    assert denv.runner.arb_kill_switch() and denv.runner.arb_desired == "stopped"
    assert len(denv.trader.orders) == 4


async def test_completion_partial_fill_never_retried(denv):
    await _on(denv)
    _tail_missing(denv)
    denv.trader.books[f"{EV0}-B2"] = bk(yes=[(0.50, 1)], no=[(0.30, 1)])       # only 1 of 2 available
    await denv.runner.arb_scan()
    tail = [o for o in denv.trader.orders if o["ticker"] == f"{EV0}-B2"]
    assert len(tail) <= 2                                                     # the original + at most one more
    assert (await _last(denv))[0]["executed"].startswith("PARTIAL") and denv.runner.arb_kill_switch()
    # and nothing more on the next scans (arb is stopped by the kill switch)
    await denv.runner.arb_scan()
    assert len([o for o in denv.trader.orders if o["ticker"] == f"{EV0}-B2"]) == len(tail)


async def test_stop_latency_unaffected_by_a_hanging_completion(denv):
    """Stop answers fast while a completion is blocked inside the exchange call, and nothing is sent afterwards."""
    await _on(denv)
    _tail_missing(denv)
    entered, release = threading.Event(), threading.Event()

    def hook(t):
        entered.set()
        release.wait(10)
    denv.trader.book_hook = hook
    task = asyncio.create_task(denv.runner.arb_scan())
    for _ in range(500):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    t0 = time.perf_counter()
    resp = await denv.client.post("/api/bot/stop", headers=denv.auth)
    dt = time.perf_counter() - t0
    release.set()
    await asyncio.wait_for(task, 15)
    assert resp.status_code == 200 and dt < 1.0, dt
    assert len(denv.trader.orders) == 3                                       # no completion after Stop
    assert denv.runner.arb_desired == "stopped" and denv.runner.arb_kill_switch()


async def test_log_only_never_builds_a_trading_client(denv):
    r = await _put(denv, {"enabled": True, "arb_complete_legs": True})       # demo-execute off
    assert r.status_code == 200
    _tail_missing(denv)
    await denv.runner.arb_scan()
    assert denv.factory_modes == [] and denv.trader.orders == []
    assert (await _last(denv))[0]["executed"] == "log-only"


# ================================================================ seeded property: the scan's spend never passes its caps
@pytest.mark.parametrize("seed", range(25))
async def test_completion_spend_and_profit_property(denv, seed):
    rng = random.Random(seed)
    events = rng.randint(1, 3)
    _set_public(denv, Public(events=events))
    scan_cap = round(rng.uniform(3.2, 12), 2)
    day_cap = round(rng.uniform(3.2, 15), 2)
    await _on(denv, arb_scan_cap=scan_cap, arb_day_cap=day_cap)
    for t in list(denv.trader.books):
        if rng.random() < 0.3:
            denv.trader.first_fill[t] = float(rng.randint(0, 1))
        if rng.random() < 0.5:
            denv.trader.books[t] = bk(yes=[(round(rng.uniform(0.02, 0.6), 2), rng.randint(0, 4))],
                                      no=[(0.30, 100)])
    await denv.runner.arb_scan()
    total = spent(denv.trader)
    assert total <= min(scan_cap, day_cap) + 1e-6, (seed, total, scan_cap, day_cap)
    for d in await _last(denv, 20):
        if d["executed"].startswith("FILLED (completed)"):
            profit, per = set_profit(denv.trader, d["event"], d["kind"], d["contracts"])
            assert profit >= -1e-9 and all(abs(v - d["contracts"]) < 1e-9 for v in per.values()), (seed, d)


# ================================================================ opposite-set gate
def _doc(event, kind, executed, mode="demo-execute", ago=timedelta(seconds=5), cost=1.0):
    return {"event": event, "kind": kind, "contracts": 2, "profit": 0.1, "cost": cost, "legs": [],
            "sum_best": 1.0, "executed": executed, "mode": mode, "ts": datetime.now(timezone.utc) - ago}


async def test_opposite_set_blocked_after_restart(denv):
    """A YES set executed before a restart (store only) blocks a NO set on the same event."""
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "FILLED {}"))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []
    assert (await _last(denv))[0]["executed"].startswith("skipped")


async def test_opposite_set_blocked_within_the_process(denv):
    _set_public(denv, Public(book=YES_BOOK))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3 and (await _last(denv))[0]["kind"] == "YES"
    _set_public(denv, Public(book=NO_BOOK))
    denv.trader.seen.clear()
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3                                       # NO set refused
    d = (await _last(denv))[0]
    assert d["kind"] == "NO" and d["executed"].startswith("skipped")


async def test_completed_set_counts_as_held(denv):
    _set_public(denv, Public(book=YES_BOOK))
    await _on(denv)
    denv.trader.first_fill[f"{EV0}-B2"] = 0.0
    await denv.runner.arb_scan()
    assert (await _last(denv))[0]["executed"].startswith("FILLED (completed)")
    _set_public(denv, Public(book=NO_BOOK))
    denv.trader.seen.clear()
    n = len(denv.trader.orders)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == n


@pytest.mark.parametrize("executed", ["PARTIAL {}", "FILLED (completed) {}"])
async def test_partial_or_completed_stored_sets_block(denv, executed):
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", executed))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_same_kind_allowed(denv):
    await denv.store.add_arb_opportunity(_doc(EV0, "NO", "FILLED {}"))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3 and (await _last(denv))[0]["executed"].startswith("FILLED")


async def test_other_event_allowed(denv):
    await denv.store.add_arb_opportunity(_doc("KXDEMOA-26OCT99", "YES", "FILLED {}"))
    await denv.store.add_arb_opportunity(_doc("KXDEMOB-26OCT20", "YES", "FILLED {}"))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3


@pytest.mark.parametrize("executed", ["log-only", "skipped: gate day cap 45.00 + 3.12 > 45.00",
                                      "skipped: stopped", "skipped: not the demo exchange"])
async def test_skipped_and_log_only_sets_never_block(denv, executed):
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", executed))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3


async def test_skipped_set_in_process_never_blocks(denv):
    _set_public(denv, Public(book=YES_BOOK))
    await _on(denv, complete=False, arb_scan_cap=1)                        # the YES set is gated
    await denv.runner.arb_scan()
    assert denv.trader.orders == [] and (await _last(denv))[0]["executed"].startswith("skipped: gate")
    _set_public(denv, Public(book=NO_BOOK))
    await _on(denv, complete=False, arb_scan_cap=15)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3


async def test_live_set_does_not_block_demo_and_old_sets_expire(denv):
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "FILLED {}", mode="live"))
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "FILLED {}", ago=timedelta(days=3)))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert len(denv.trader.orders) == 3


async def test_set_from_late_yesterday_still_blocks(denv):
    now = datetime.now(timezone.utc)
    ago = now - (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=30))  # 23:30 yesterday
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "FILLED {}", ago=ago))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_error_set_with_possible_filled_legs_blocks(denv):
    """An ERROR set (order error mid-set) may hold filled legs, like a PARTIAL one."""
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "ERROR RuntimeError"))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_held_state_unreadable_fails_closed(denv, monkeypatch):
    await _on(denv, complete=False)

    async def boom(*a, **kw):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(denv.store, "list_arb_opportunities", boom)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_held_state_read_at_row_limit_fails_closed(denv, monkeypatch):
    monkeypatch.setattr(rn, "ARB_HELD_READ_MAX", 3)
    for i in range(6):
        await denv.store.add_arb_opportunity(_doc(f"KXDEMOB-OTHER{i}", "YES", "FILLED {}"))
    await _on(denv, complete=False)
    await denv.runner.arb_scan()
    assert denv.trader.orders == []


async def test_opposite_gate_applies_to_live(denv, monkeypatch, real_strategy):
    for k, v in {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
                 "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(denv.settings, "prod_keys_present", True)
    monkeypatch.setattr(denv.settings, "live_confirm_ok", True)
    await denv.store.add_arb_opportunity(_doc(EV0, "YES", "FILLED {}", mode="live"))
    r = await _put(denv, {"enabled": True, "live": True, "confirm": "LIVE"})
    assert r.status_code == 200
    denv.trader.base_url = PROD_URL
    await denv.runner.arb_scan()
    assert denv.trader.orders == []

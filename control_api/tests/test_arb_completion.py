"""Arbitrage leg completion (setting arb_complete_legs, default off; demo-execute only).

A partial demo set gets ONE attempt to buy its missing legs, through the runner's re-checks (stop, leader/fence,
kill switch, demo exchange, risk gate on the extra spend). Refused or failed -> the legging kill switch exactly as
before. Off -> byte-identical to the old behavior. Never in live mode. No network: fake clients only.
"""
from __future__ import annotations

import pytest

from control_api.tests.conftest import preflight_ready

from control_api import runner as rn
from control_api.services import arb as arbsvc
from control_api.tests.test_arb_demo_execute import DEMO_URL, PROD_URL, TwoEventPublic

pytestmark = pytest.mark.asyncio

BOOK = {"yes_dollars": [["0.50", "100"]], "no_dollars": []}


class Trader:
    """Demo trading client. fills[ticker]: fill ratios per order on that ticker (last repeats)."""

    def __init__(self, base_url=DEMO_URL, fills=None, cash_cents=100000):
        self.base_url = base_url
        self.fills = fills or {}
        self.cash_cents = cash_cents
        self.orders = []
        self.book_reads = []
        self.pre_reads: list[str] = []
        self.books = {}
        self.on_book = None

    def get_balance(self):
        return {"balance": self.cash_cents}

    def get_positions(self, **kw):
        return {"market_positions": []}

    def get_orderbook(self, ticker, depth=None):
        # completion reads only: the overlaid scanner also re-reads the thinnest legs BEFORE the first order
        (self.book_reads if self.orders else self.pre_reads).append(ticker)
        if self.on_book:
            self.on_book(ticker)
        return {"orderbook_fp": self.books.get(ticker, BOOK)}

    def create_order(self, *, ticker, side, count, price, client_order_id, **kw):
        k = sum(1 for o in self.orders if o[0] == ticker)
        self.orders.append((ticker, side, count, price, client_order_id))
        rs = self.fills.get(ticker, [1.0])
        return {"order": {"fill_count_fp": str(float(count) * rs[min(k, len(rs) - 1)])}}

    def get_orders(self, status=None):
        return []


TAIL = "KXDEMOA-26OCT20-B2"          # the last leg sent (legs go in ticker order at equal size)


@pytest.fixture
def cenv(env, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "demo-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "not-a-real-key")
    env.public = TwoEventPublic()
    env.runner.public_factory = lambda e: env.public
    env.trader = Trader(fills={TAIL: [0.0, 1.0]})                        # the tail misses, then completes
    env.runner.kalshi_factory = lambda mode: env.trader
    env.runner.config = {**env.runner.config, "arb": {"arb_series": ["KXDEMOA"], "arb_min_profit": 0.01,
                                                      "arb_max_contracts": 2}}
    return env


async def _put(env, body):
    return await env.client.put("/api/arb", json=body, headers=await env.h2fa())


async def _on(env, **extra):
    r = await _put(env, {"enabled": True, "arb_demo_execute": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def _last(env):
    return (await env.store.list_arb_opportunities(limit=1))[0]


def _legged(env):
    r = env.runner
    return r.arb_kill_switch() and r.arb_status()["kill_reason"] == "legging" and r.arb_desired == "stopped"


# ---------------------------------------------------------------- the setting
async def test_setting_defaults_off_and_needs_fresh_2fa(cenv):
    view = (await cenv.client.get("/api/arb", headers=cenv.auth)).json()
    assert view["settings"]["arb_complete_legs"] is False
    assert view["schema"]["arb_complete_legs"]["type"] == "boolean"
    r = await cenv.client.put("/api/arb", json={"arb_complete_legs": True}, headers=cenv.auth)
    assert r.status_code == 403 and cenv.runner.arb_settings()["arb_complete_legs"] is False
    r = await _put(cenv, {"arb_complete_legs": "yes"})
    assert r.status_code == 400
    r = await _put(cenv, {"arb_complete_legs": True})
    assert r.status_code == 200 and r.json()["settings"]["arb_complete_legs"] is True
    assert (await cenv.store.get_config())["arb"]["arb_complete_legs"] is True          # versioned with the block


# ---------------------------------------------------------------- off: exactly as before
async def test_off_a_partial_set_trips_the_kill_switch_as_before(cenv):
    await _on(cenv)
    await cenv.runner.arb_scan()
    doc = await _last(cenv)
    assert doc["executed"] == "PARTIAL {'KXDEMOA-26OCT20-B0': 2.0, 'KXDEMOA-26OCT20-B1': 2.0, " \
                              "'KXDEMOA-26OCT20-B2': 0.0}"
    assert _legged(cenv) and cenv.trader.book_reads == [] and len(cenv.trader.orders) == 3


async def test_off_the_scanner_is_called_without_the_new_argument(cenv, monkeypatch):
    await _on(cenv)
    seen = []
    sc = arbsvc.scanner()
    monkeypatch.setattr(sc, "execute", lambda client, op, **kw: seen.append(kw) or "FILLED {}")
    await cenv.runner.arb_scan()
    assert seen == [{}]


# ---------------------------------------------------------------- on: completed
async def test_on_the_missing_leg_is_completed(cenv):
    await _on(cenv, arb_complete_legs=True)
    r = cenv.runner
    res = await r.arb_scan()
    assert res == {"scanned": True, "found": 1}
    doc = await _last(cenv)
    assert doc["executed"].startswith("FILLED (completed) {") and doc["mode"] == "demo-execute"
    assert doc["profit"] >= 0
    assert not r.arb_kill_switch() and r.arb_desired == "running"                       # legging not tripped
    assert [o[0] for o in cenv.trader.orders] == [f"KXDEMOA-26OCT20-B{i}" for i in range(3)] + [TAIL]
    assert cenv.trader.book_reads == [TAIL]
    assert r._arb_day["demo-execute"]["amount"] == pytest.approx(3.12 + 1.04)          # extra reserved on top
    assert any(a["title"] == "Arbitrage executed" and "completed" in a["message"] for a in r.alerter.sent)
    assert not any("legging" in a["title"] for a in r.alerter.sent)


# ---------------------------------------------------------------- on: refused -> kill switch as before
async def test_on_unprofitable_completion_is_refused(cenv):
    await _on(cenv, arb_complete_legs=True)
    cenv.trader.books[TAIL] = {"yes_dollars": [["0.01", "100"]]}                         # NO now costs 0.99
    await cenv.runner.arb_scan()
    doc = await _last(cenv)
    assert doc["executed"].startswith("PARTIAL {") and "unprofitable" in doc["executed"]
    assert _legged(cenv) and len(cenv.trader.orders) == 3
    assert any("legging" in a["title"] for a in cenv.runner.alerter.sent)


async def test_on_over_the_scan_cap_is_refused(cenv):
    await _on(cenv, arb_complete_legs=True, arb_scan_cap=4)                            # 3.12 + 1.04 > 4
    await cenv.runner.arb_scan()
    doc = await _last(cenv)
    assert "refused: gate scan cap" in doc["executed"] and _legged(cenv) and len(cenv.trader.orders) == 3


@pytest.mark.parametrize("block", ["stop", "not_leader", "shutting_down", "setting_off", "maker_live",
                                   "stop_kill_file", "prod_url"])
async def test_on_a_block_during_completion_is_refused(cenv, block):
    await _on(cenv, arb_complete_legs=True)
    r = cenv.runner

    def flip(_t):
        if block == "stop":
            r._stop_gen += 1
            r.arb_desired = "stopped"
        elif block == "not_leader":
            r._leader_ready = False
        elif block == "shutting_down":
            r._shutting_down = True
        elif block == "setting_off":
            r.config = {**r.config, "arb": {**r.config["arb"], "arb_complete_legs": False}}
        elif block == "maker_live":
            r.mode = "live"
        elif block == "stop_kill_file":
            r.arb_kill_path.write_text("stopped by test\n")
        elif block == "prod_url":
            cenv.trader.base_url = PROD_URL
    cenv.trader.on_book = flip
    try:
        await r.arb_scan()
    finally:
        r._shutting_down = False
        r._leader_ready = True
        r.mode = "demo"
    assert len(cenv.trader.orders) == 3                                                 # nothing more sent
    doc = r.arb_recent[0]
    assert doc["executed"].startswith("PARTIAL {") and "refused" in doc["executed"]
    assert r.arb_kill_switch()


async def test_on_failure_mid_way_is_partial_and_not_retried(cenv):
    await _on(cenv, arb_complete_legs=True)
    cenv.trader.fills = {"KXDEMOA-26OCT20-B1": [0.0, 0.5]}                           # B1 + B2 missing; B1 short
    await cenv.runner.arb_scan()
    doc = await _last(cenv)
    assert doc["executed"].startswith("PARTIAL {") and "completion leg KXDEMOA-26OCT20-B1" in doc["executed"]
    assert [o[0] for o in cenv.trader.orders][-1] == "KXDEMOA-26OCT20-B1" and len(cenv.trader.orders) == 3
    assert _legged(cenv)
    await cenv.runner.arb_scan()                                                        # the kill switch holds
    assert len(cenv.trader.orders) == 3


async def test_gate_approves_once_only(cenv):
    await _on(cenv, arb_complete_legs=True)
    r = cenv.runner
    budget = {"scan": 0.0, "day": {"date": "x", "amount": 0.0, "mark": None}}
    g = rn.ArbCompletionGate(r, cenv.trader, "KXDEMOA-26OCT20", r._stop_gen, budget, 0.5)
    assert g.approve(1.0) is None and budget["scan"] == 1.0
    assert g.approve(1.0) == "already attempted" and budget["scan"] == 1.0


async def test_never_in_live_mode(cenv, monkeypatch, real_strategy):
    for k, v in {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
                 "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cenv.settings, "prod_keys_present", True)
    monkeypatch.setattr(cenv.settings, "live_confirm_ok", True)
    cenv.runner.arming["arbitrage"] = True     # S3 item 10: live arbitrage needs arming
    preflight_ready(monkeypatch)                 # S3 L-1: live re-runs the preflight
    r = await _put(cenv, {"enabled": True, "live": True, "confirm": "LIVE", "arb_complete_legs": True})
    assert r.status_code == 200
    cenv.trader.base_url = PROD_URL
    seen = []
    sc = arbsvc.scanner()
    real = sc.execute
    monkeypatch.setattr(sc, "execute", lambda client, op, **kw: seen.append(kw) or real(client, op, **kw))
    await cenv.runner.arb_scan()
    assert seen == [{}]                                                                 # no completion gate
    doc = await _last(cenv)
    assert doc["mode"] == "live" and doc["executed"].startswith("PARTIAL {")
    assert "completion" not in doc["executed"] and cenv.trader.book_reads == []


async def test_an_older_scanner_without_completion_is_called_as_before(cenv, monkeypatch):
    await _on(cenv, arb_complete_legs=True)
    seen = []

    def old_execute(client, op):
        seen.append(op.event)
        return "FILLED {}"
    monkeypatch.setattr(arbsvc.scanner(), "execute", old_execute)
    await cenv.runner.arb_scan()
    assert seen == ["KXDEMOA-26OCT20"]


# ---------------------------------------------------------------- security follow-ups
class Truncated(Trader):
    def __init__(self, *a, after=0, **kw):
        super().__init__(*a, **kw)
        self.after, self.reads = after, 0

    def get_positions(self, **kw):
        self.reads += 1
        return {"market_positions": [], "truncated": self.reads > self.after}


async def test_truncated_positions_skip_the_set(cenv):
    cenv.trader = Truncated(fills={TAIL: [1.0]})
    cenv.runner.kalshi_factory = lambda mode: cenv.trader
    await _on(cenv)
    await cenv.runner.arb_scan()
    assert (await _last(cenv))["executed"] == "skipped: gate positions truncated" and cenv.trader.orders == []


async def test_truncated_positions_refuse_completion(cenv):
    cenv.trader = Truncated(fills={TAIL: [0.0, 1.0]}, after=1)            # the set's read is whole, then truncated
    cenv.runner.kalshi_factory = lambda mode: cenv.trader
    await _on(cenv, arb_complete_legs=True)
    await cenv.runner.arb_scan()
    doc = await _last(cenv)
    assert "refused: gate positions truncated" in doc["executed"] and _legged(cenv) and len(cenv.trader.orders) == 3


async def test_an_abandoned_scan_starts_no_set_and_no_completion(cenv):
    await _on(cenv, arb_complete_legs=True)
    r = cenv.runner
    budget = {"scan": 0.0, "day": {"date": "x", "amount": 0.0, "mark": None}, "held": {}, "abandoned": True}
    op = arbsvc.scanner().best_opportunity("KXDEMOA-26OCT20", {
        f"KXDEMOA-26OCT20-B{i}": BOOK for i in range(3)}, 2)
    assert r._arb_execute_sync(arbsvc.scanner(), op, r._stop_gen, "demo-execute", budget) == "skipped: scan abandoned"
    g = rn.ArbCompletionGate(r, cenv.trader, "KXDEMOA-26OCT20", r._stop_gen, budget, 0.5)
    assert g.ok() is False and g.approve(1.0) == "stopped"
    assert cenv.trader.orders == []


async def test_the_scan_timeout_marks_the_scan_abandoned(cenv, monkeypatch):
    import threading
    await _on(cenv, arb_complete_legs=True)
    r = cenv.runner
    release = threading.Event()
    real = cenv.public.get_orderbook

    def slow(t, depth=None):
        release.wait(5)
        return real(t)
    cenv.public.get_orderbook = slow
    monkeypatch.setattr(rn, "ARB_SCAN_TIMEOUT_S", 0.05)
    res = await r.arb_scan()
    assert res["scanned"] is False
    release.set()
    r._arb_executor.submit(lambda: None).result(timeout=5)               # the abandoned worker has finished
    assert cenv.trader.orders == []                                      # it placed nothing after the timeout


def test_scanner_with_only_kwargs_is_not_completion_capable():
    class S:
        @staticmethod
        def execute(client, op, **kw):
            return "FILLED {}"
    assert rn._scanner_can_complete(S) is False
    assert rn._scanner_can_complete(object()) is False
    assert rn._scanner_can_complete(arbsvc.scanner()) is True

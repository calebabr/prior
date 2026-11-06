"""Sprint 2B part 2, runner side: contain and degrade after a partial arbitrage set (item 3), percentage arbitrage
caps (item 4), the per-event exposure limit (item 8) and the part-1 follow-ups (refund on skip, unconfirmed alert).

A fake scanner `execute` returns the scanner's result strings; no network, demo-execute only, arbitrary numbers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as rn
from control_api.services import arb as arbsvc
from control_api.services import arbcontain
from control_api.services import riskgate
from control_api.tests.test_arb_demo_execute import DEMO_URL

pytestmark = pytest.mark.asyncio

EV_A = "KXDEMOA-26NOV05"
EV_B = "KXDEMOB-26NOV05"
COST = 3.12                    # the stand-in's NO-arb on 3 brackets at 2 contracts (bids 0.50 x 3)


class TwoSeriesPublic:
    """One 3-bracket event per series in `series`, fee-positive NO-arbs."""

    def __init__(self, series=("KXDEMOA", "KXDEMOB")):
        self.series = series

    def get_markets(self, series_ticker=None, status=None):
        if series_ticker not in self.series:
            return []
        ev = f"{series_ticker}-26NOV05"
        return [{"event_ticker": ev, "ticker": f"{ev}-B{i}"} for i in range(3)]

    def get_orderbook(self, ticker, depth=None):
        return {"orderbook_fp": {"yes_dollars": [["0.50", "100"]], "no_dollars": []}}


class Trader:
    def __init__(self, cash_cents=100000, positions=None):
        self.base_url = DEMO_URL
        self.cash_cents = cash_cents
        self.positions = positions or []

    def get_balance(self):
        return {"balance": self.cash_cents}

    def get_positions(self, **kw):
        return {"market_positions": list(self.positions)}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def xenv(env, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "demo-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "not-a-real-key")
    env.runner.public_factory = lambda e: TwoSeriesPublic()
    env.trader = Trader()
    env.runner.kalshi_factory = lambda mode: env.trader
    env.runner.config = {**env.runner.config, "arb": {"arb_series": ["KXDEMOA", "KXDEMOB"], "arb_min_profit": 0.01,
                                                      "arb_max_contracts": 2}}
    # 2026-11-05 15:00 UTC = 10:00 New York (EST): the next market day starts 2026-11-06 05:00 UTC
    env.clock = Clock(datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(rn, "utcnow", env.clock)
    env.results = {}          # event -> result string (default FILLED)
    env.calls = []

    def execute(client, op, complete=None, reread=True, *, loss_cap=None):
        env.calls.append({"event": op.event, "loss_cap": loss_cap})
        out = env.results.get(op.event, "FILLED {}")
        return out(op) if callable(out) else out
    monkeypatch.setattr(arbsvc.scanner(), "execute", execute)
    return env


async def _on(env, **extra):
    r = await env.client.put("/api/arb", json={"enabled": True, "arb_demo_execute": True, **extra},
                             headers=await env.h2fa())
    assert r.status_code == 200, r.text
    return r.json()


async def _docs(env, event):
    return [d for d in await env.store.list_arb_opportunities(limit=100) if d["event"] == event]


def contained(loss, how="flattened", event=EV_A):
    return f"PARTIAL {{'{event}-B0': 2.0, '{event}-B1': 0.0}} contained: {how} loss {loss:.4f} event {event}"


def _legged(env):
    r = env.runner
    return r.arb_kill_switch() and r.arb_status()["kill_reason"] == "legging" and r.arb_desired == "stopped"


# ---------------------------------------------------------------- item 3: contained partial pauses one series
async def test_contained_partial_pauses_only_its_series_and_resumes_next_market_day(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)              # equity $1000 -> loss cap $10
    r = xenv.runner
    xenv.results[EV_A] = contained(0.52)
    await r.arb_scan()
    assert not r.arb_kill_switch() and r.arb_desired == "running"            # no global kill
    a = (await _docs(xenv, EV_A))[0]
    assert a["executed"].startswith("PARTIAL") and a["contain"]["loss"] == pytest.approx(0.52)
    assert a["contain"]["series"] == "KXDEMOA" and a["contain"]["paused_until_date"] == "2026-11-06"
    assert a["contain"]["global_kill"] is None and a["legs"]                 # legs recorded with the partial
    assert a["contain"]["fills"] == [[f"{EV_A}-B0", 2.0], [f"{EV_A}-B1", 0.0]]   # pairs: no dotted Mongo keys
    assert (await _docs(xenv, EV_B))[0]["executed"] == "FILLED {}"
    al = [x for x in r.alerter.sent if "contained" in x["title"]]
    assert al and al[0]["priority"] == "urgent" and "paused until 2026-11-06" in al[0]["message"]
    assert EV_A in al[0]["message"] and "$0.52" in al[0]["message"]
    st = r.arb_status()
    assert [p["series"] for p in st["paused_series"]] == ["KXDEMOA"]
    assert st["paused_series"][0]["until_date"] == "2026-11-06"
    assert st["contained_24h"]["demo-execute"]["count"] == 1

    # same day, later: KXDEMOA is skipped, KXDEMOB keeps trading
    xenv.clock.t += timedelta(hours=10)                  # 01:00 UTC 11/6 = 20:00 New York 11/5
    xenv.results[EV_A] = "FILLED {}"
    xenv.calls.clear()
    await r.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_B]
    assert (await _docs(xenv, EV_A))[0]["executed"].startswith("skipped: series paused until 2026-11-06")

    # the next market day (00:00 New York = 05:00 UTC): auto-resume at the first scan
    xenv.clock.t = datetime(2026, 11, 6, 5, 0, 1, tzinfo=timezone.utc)
    xenv.calls.clear()
    await r.arb_scan()
    assert sorted(c["event"] for c in xenv.calls) == [EV_A, EV_B]
    assert r.arb_status()["paused_series"] == []


async def test_loss_cap_is_share_of_equity_and_not_passed_when_off(xenv):
    await _on(xenv)
    await xenv.runner.arb_scan()
    assert all(c["loss_cap"] is None for c in xenv.calls)
    xenv.clock.t += timedelta(minutes=1)
    xenv.calls.clear()
    await _on(xenv, arb_set_loss_frac=0.02)
    await xenv.runner.arb_scan()
    assert [c["loss_cap"] for c in xenv.calls] == [pytest.approx(20.0)] * 2


async def test_loss_cap_uses_the_lower_of_snapshot_and_fresh_equity(xenv):
    await _on(xenv, arb_set_loss_frac=0.02)
    xenv.runner._note_equity({"ts": xenv.clock.t, "cash": 300.0, "exposure": 100.0, "env": "demo"})
    await xenv.runner.arb_scan()
    assert [c["loss_cap"] for c in xenv.calls] == [pytest.approx(8.0)] * 2
    xenv.runner._note_equity({"ts": xenv.clock.t, "cash": 300.0, "exposure": 100.0, "env": "live"})
    assert xenv.runner._arb_equity_of("demo-execute", 1000, 0) == 400.0


# ---------------------------------------------------------------- the global kill switch still trips
async def test_uncontained_partial_still_trips_the_global_kill(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = f"PARTIAL {{'{EV_A}-B0': 2.0, '{EV_A}-B1': 0.0}}"       # the fake writes no kill file
    await xenv.runner.arb_scan()
    assert _legged(xenv)                                  # the runner wrote it (defensive) and stopped arbitrage
    assert any("kill switch tripped" in a["title"] and a["priority"] == "urgent" for a in xenv.runner.alerter.sent)


@pytest.mark.parametrize("text", ["completion: no book", "unconfirmed: get_orders down"])
async def test_uncontained_variants_trip(xenv, text):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = f"PARTIAL {{'{EV_A}-B0': 2.0}} {text}"
    await xenv.runner.arb_scan()
    assert _legged(xenv)


async def test_two_contained_partials_in_24h_across_series_trip(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.10)
    xenv.results[EV_B] = contained(0.10, event=EV_B)
    await xenv.runner.arb_scan()
    assert _legged(xenv)
    b = (await _docs(xenv, EV_B))[0]
    assert b["contain"]["global_kill"] == "two contained partials in 24 h"
    assert "two contained" in xenv.runner.arb_kill_path.read_text(encoding="utf-8")


async def test_second_contained_after_a_restart_still_trips(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.10)
    await xenv.runner.arb_scan()
    assert not xenv.runner.arb_kill_switch()
    xenv.runner._arb_contain.clear()                      # a restart: memory gone, the store keeps the log
    xenv.clock.t += timedelta(hours=20)                   # past the New York midnight, inside 24 h
    xenv.results = {EV_A: "FILLED {}", EV_B: contained(0.10, event=EV_B)}
    await xenv.runner.arb_scan()
    assert _legged(xenv)


async def test_contained_loss_over_the_cap_trips(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)              # cap $10
    xenv.results[EV_A] = contained(10.5)
    await xenv.runner.arb_scan()
    assert _legged(xenv)


async def test_contained_without_a_loss_cap_trips(xenv):
    await _on(xenv)                                      # share OFF: no cap passed, so "contained" is not believed
    xenv.results[EV_A] = contained(0.0, how="completed")
    await xenv.runner.arb_scan()
    assert _legged(xenv)


async def test_negative_loss_is_a_gain_and_does_not_trip(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(-0.25, how="completed")
    await xenv.runner.arb_scan()
    assert not xenv.runner.arb_kill_switch()
    assert xenv.runner.arb_status()["contained_24h"]["demo-execute"]["loss"] == 0.0


def test_cumulative_loss_rule(monkeypatch):
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    recs = [arbcontain.record(EV_A, 6.0, now - timedelta(hours=3), "a")]
    monkeypatch.setattr(arbcontain, "MAX_CONTAINED_IN_WINDOW", 5)    # isolate the loss rule from the count rule
    assert arbcontain.must_trip(recs, 3.0, 10.0, now) is None
    assert arbcontain.must_trip(recs, 5.0, 10.0, now) == "contained losses over 24 h exceed the loss cap"
    old = [arbcontain.record(EV_A, 6.0, now - timedelta(hours=25), "b")]
    assert arbcontain.must_trip(old, 5.0, 10.0, now) is None        # outside the 24 h window


def test_pause_until_is_next_new_york_midnight():
    # EDT (UTC-4) and EST (UTC-5)
    assert arbcontain.pause_until(datetime(2026, 7, 1, 3, 59, tzinfo=timezone.utc)) == \
        datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)
    assert arbcontain.pause_until(datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- pause survives a restart
async def test_pause_survives_a_restart(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()
    xenv.runner._arb_contain.clear()                      # memory lost
    xenv.clock.t += timedelta(hours=1)
    xenv.results[EV_A] = "FILLED {}"
    xenv.calls.clear()
    await xenv.runner.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_B]
    assert [p["series"] for p in xenv.runner.arb_status()["paused_series"]] == ["KXDEMOA"]


async def test_partial_history_unreadable_refuses_sets(xenv, monkeypatch):
    await _on(xenv)
    xenv.runner._arb_contain.clear()

    async def boom(**kw):
        raise RuntimeError("store down")
    real = xenv.store.list_arb_opportunities
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", boom)
    budget = {"scan": 0.0, "day": {"date": "x", "amount": 0.0, "mark": None}, "held": {},
              "contain": await xenv.runner._arb_contain_state("demo-execute")}
    assert budget["contain"] is None
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", real)
    sc = arbsvc.scanner()
    op = sc.Opportunity(event=EV_A, kind="NO", contracts=2, profit=0.5, cost=COST,
                        legs=[(f"{EV_A}-B0", "no", 0.5, 2)], sum_best=1.5)
    out = xenv.runner._arb_execute_sync(sc, op, xenv.runner._stop_gen, "demo-execute", budget)
    assert out == "skipped: gate partial history unknown" and xenv.calls == []


# ---------------------------------------------------------------- follow-ups: refund, non-tripping skips, alert
@pytest.mark.parametrize("skip", [
    "skipped: depth fell KXDEMOA-26NOV05-B1 1 < 4",
    "skipped: depth re-read failed KXDEMOA-26NOV05-B1 (TimeoutError)",
    "skipped: first leg KXDEMOA-26NOV05-B2 filled 0 of 2",
    "skipped: order rejected KalshiError 400 on KXDEMOA-26NOV05-B2",
    "skipped: transient error KalshiError 503 on KXDEMOA-26NOV05-B2 after 3 tries (no order)",
])
async def test_skip_refunds_the_reservation_and_never_trips(xenv, skip):
    await _on(xenv)
    xenv.results = {EV_A: skip, EV_B: skip}
    await xenv.runner.arb_scan()
    assert not xenv.runner.arb_kill_switch() and xenv.runner.arb_desired == "running"
    assert xenv.runner._arb_day["demo-execute"]["amount"] == 0.0
    assert not any(a["priority"] == "urgent" for a in xenv.runner.alerter.sent)


async def test_refund_frees_room_for_the_next_set_in_the_same_scan(xenv):
    await _on(xenv, arb_scan_cap=4)                      # room for one $3.12 set per scan
    xenv.results[EV_A] = "skipped: depth fell KXDEMOA-26NOV05-B1 1 < 4"
    await xenv.runner.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_A, EV_B]           # B is not refused by the scan cap
    assert (await _docs(xenv, EV_B))[0]["executed"] == "FILLED {}"
    assert xenv.runner._arb_day["demo-execute"]["amount"] == pytest.approx(COST)


async def test_unconfirmed_skip_alerts_normal_priority_and_keeps_the_reservation(xenv):
    await _on(xenv)
    xenv.results[EV_A] = ("skipped: transient error KalshiError 503 on KXDEMOA-26NOV05-B2 after 3 tries "
                          "(order state unconfirmed: TypeError)")
    await xenv.runner.arb_scan()
    r = xenv.runner
    al = [a for a in r.alerter.sent if a["title"] == "Arbitrage order state unconfirmed"]
    assert len(al) == 1 and al[0]["priority"] == "default" and EV_A in al[0]["message"]
    assert not r.arb_kill_switch()
    assert r._arb_day["demo-execute"]["amount"] == pytest.approx(2 * COST)     # A kept, B filled
    assert "NO" in r._arb_held["demo-execute"]["events"][EV_A]


# ---------------------------------------------------------------- item 4: percentage caps
def test_effective_caps_off_is_exactly_the_dollar_caps():
    cfg = arbsvc.defaults()
    caps = arbsvc.effective_caps(cfg, 1000.0)
    assert {k: caps[k] for k in ("arb_scan_cap", "arb_day_cap", "arb_series_cap_frac")} == arbsvc.gate_caps(cfg)
    assert caps["max_event_frac"] is None


def test_effective_caps_take_the_smaller_of_share_and_dollars():
    cfg = {**arbsvc.defaults(), "arb_scan_cap": 15.0, "arb_day_cap": 45.0, "arb_scan_cap_frac": 0.01,
           "arb_day_cap_frac": 0.5}
    caps = arbsvc.effective_caps(cfg, 200.0)
    assert caps["arb_scan_cap"] == pytest.approx(2.0) and caps["arb_day_cap"] == 45.0
    unknown = arbsvc.effective_caps(cfg, None)
    assert unknown["arb_scan_cap"] == 15.0 and unknown["arb_day_cap"] == 45.0    # equity unknown: dollars
    assert arbsvc.set_loss_cap(cfg, 200.0) is None                                # loss share off
    assert arbsvc.set_loss_cap({**cfg, "arb_set_loss_frac": 0.05}, 200.0) == pytest.approx(10.0)
    assert arbsvc.set_loss_cap({**cfg, "arb_set_loss_frac": 0.05}, float("nan")) is None


async def test_scan_cap_share_refuses_a_set_over_it(xenv):
    await _on(xenv, arb_scan_cap_frac=0.003)             # $1000 x 0.003 = $3 < $3.12
    await xenv.runner.arb_scan()
    assert xenv.calls == []
    assert (await _docs(xenv, EV_A))[0]["executed"].startswith("skipped: gate scan cap")


async def test_day_cap_share_refuses_a_set_over_it(xenv):
    await _on(xenv, arb_day_cap_frac=0.005)              # $5: room for one set only
    await xenv.runner.arb_scan()
    assert len(xenv.calls) == 1


def test_frac_settings_validation():
    class S:
        max_arb_contracts = 50
    d = arbsvc.defaults()
    out = arbsvc.validate(d, {"arb_set_loss_frac": 0.03, "arb_scan_cap_frac": 0.1, "arb_day_cap_frac": 1,
                              "max_event_frac": 0.2}, S())
    assert out["arb_set_loss_frac"] == 0.03 and out["arb_day_cap_frac"] == 1.0
    assert arbsvc.validate(out, {"arb_set_loss_frac": None}, S())["arb_set_loss_frac"] is None
    for bad in ({"arb_set_loss_frac": 0}, {"arb_set_loss_frac": 0.6}, {"arb_scan_cap_frac": 1.01},
                {"arb_day_cap_frac": -0.1}, {"max_event_frac": True}, {"max_event_frac": "0.2"},
                {"arb_scan_cap_frac": float("nan")}, {"arb_day_cap_frac": float("inf")}):
        with pytest.raises(ValueError):
            arbsvc.validate(d, bad, S())
    c = arbsvc.ceilings(S())
    assert c["arb_set_loss_frac"] == 0.5 and c["arb_scan_cap_frac"] == 1.0 and c["max_event_frac"] == 1.0
    assert arbsvc.frac_of({"arb_scan_cap_frac": 5}, "arb_scan_cap_frac") is None   # a bad stored value is off


async def test_frac_settings_need_fresh_2fa_and_are_versioned(xenv):
    r = await xenv.client.put("/api/arb", json={"arb_set_loss_frac": 0.03}, headers=xenv.auth)
    assert r.status_code == 403 and xenv.runner.arb_settings()["arb_set_loss_frac"] is None
    r = await xenv.client.put("/api/arb", json={"arb_set_loss_frac": 2}, headers=await xenv.h2fa())
    assert r.status_code == 400
    r = await xenv.client.put("/api/arb", json={"arb_set_loss_frac": 0.03}, headers=await xenv.h2fa())
    assert r.status_code == 200 and r.json()["settings"]["arb_set_loss_frac"] == 0.03
    assert (await xenv.store.get_config())["arb"]["arb_set_loss_frac"] == 0.03
    view = (await xenv.client.get("/api/arb", headers=xenv.auth)).json()
    assert "paused_series" in view["status"] and "contained_24h" in view["status"]


# ---------------------------------------------------------------- item 8: per-event limit
def test_event_exposure_and_gate():
    pos = [{"ticker": f"{EV_A}-B0", "market_exposure_dollars": 4.0},
           {"ticker": f"{EV_A}-B1", "market_exposure_dollars": -1.0},
           {"ticker": "KXDEMOA-26NOV050-B1", "market_exposure_dollars": 9.0},        # another event, same prefix
           {"ticker": f"{EV_B}-B0", "market_exposure_dollars": 7.0}]
    assert riskgate.exposure_of_event(pos, EV_A) == 5.0
    with pytest.raises(ValueError):
        riskgate.exposure_of_event(pos, "")
    by = riskgate.exposure_by_series(pos)
    caps = {"arb_scan_cap": 100, "arb_day_cap": 100, "arb_series_cap_frac": 0.9, "max_event_frac": 0.01}
    # equity = 1000 + 21 = 1021 -> event cap 10.21
    assert riskgate.check_arb(5.0, "KXDEMOA", by, 1000, 0.5, 0, 0, caps, event_exposure=5.0) == (True, "ok")
    ok, why = riskgate.check_arb(5.3, "KXDEMOA", by, 1000, 0.5, 0, 0, caps, event_exposure=5.0)
    assert not ok and why.startswith("event cap")
    assert riskgate.check_arb(5.0, "KXDEMOA", by, 1000, 0.5, 0, 0, caps) == (False, "bad input")   # fail closed
    off = {**caps, "max_event_frac": None}
    assert riskgate.check_arb(50.0, "KXDEMOA", by, 1000, 0.5, 0, 0, off) == (True, "ok")


async def test_event_limit_refuses_a_set_on_a_loaded_event(xenv):
    xenv.trader.positions = [{"ticker": f"{EV_A}-B0", "market_exposure_dollars": 8.0}]
    await _on(xenv, max_event_frac=0.01)                 # equity 1008 -> $10.08 per event
    await xenv.runner.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_B]
    assert (await _docs(xenv, EV_A))[0]["executed"].startswith("skipped: gate event cap")


# ---------------------------------------------------------------- parsing
def test_local_parse_of_the_contract():
    p = arbcontain.local_parse(contained(0.52))
    assert p["kind"] == "contained" and p["how"] == "flattened" and p["loss"] == pytest.approx(0.52)
    assert p["fills"] == {f"{EV_A}-B0": 2.0, f"{EV_A}-B1": 0.0}
    s = f"PARTIAL {{'{EV_A}-B0': 2.0}} contained: flattened loss 0.5200 sold {{'{EV_A}-B0': 2.0}} event {EV_A}"
    assert arbcontain.local_parse(s)["loss"] == pytest.approx(0.52)
    assert arbcontain.local_parse(contained(-0.1, how="completed"))["loss"] == pytest.approx(-0.1)
    assert arbcontain.local_parse("PARTIAL {'x': 1.0}")["kind"] == "partial"
    assert arbcontain.local_parse("PARTIAL {'x': 1.0} contained: maybe loss 1")["kind"] == "partial"
    assert arbcontain.local_parse("FILLED (completed) {}")["kind"] == "completed"
    assert arbcontain.local_parse("ERROR KalshiError")["kind"] == "error"
    assert arbcontain.local_parse("skipped: x")["kind"] == "skipped"


def test_scanner_parser_can_never_downgrade_an_uncontained_result():
    class Sc:
        @staticmethod
        def parse_result(s):
            return {"kind": "contained", "fills": {}, "loss": 0.0, "event": EV_A}
    assert arbcontain.parse("PARTIAL {'x': 1.0}", Sc)["kind"] == "partial"
    assert arbcontain.parse("ERROR X", Sc)["kind"] == "error"

    class Bad:
        @staticmethod
        def parse_result(s):
            raise RuntimeError("bug")
    assert arbcontain.parse(contained(0.5), Bad)["kind"] == "contained"


# ---------------------------------------------------------------- Stop is unaffected
async def test_stop_works_while_a_series_is_paused_and_the_store_is_down(xenv, monkeypatch):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()

    async def boom(*a, **kw):
        raise RuntimeError("store down")
    monkeypatch.setattr(xenv.store, "set_state", boom)
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", boom)
    res = await xenv.runner.stop("owner")
    assert res["desired"] == "stopped" and res["arb_kill_switch"] is True
    assert xenv.runner.arb_desired == "stopped" and xenv.runner.arb_state() == "stopped"

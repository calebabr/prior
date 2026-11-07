"""The per-series limit in the Shadow estimate sizes bids to the room left, like the market cap (services/shadow.py).

A series may hold, rest and be quoted up to limit x (max_open_frac x capital) in all. Each simulated bid is sized to the
smaller of its market's room and the series' room (limit x ceiling minus the series' held cost, unseen bids and what
this pass already took), in 0.01-contract steps; room for under one contract means no quote (stored `capped` with
cap_reason "series_cap", never scored). Synthetic episodes only (KXDEMO* tickers); the limits are TEST values passed
in (the real ones are a private data file, absent in the public repo).
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest

from control_api.services import shadow as sh
from control_api.tests import shadow_verifier as V
from control_api.tests.test_shadow_market_cap import (
    CAPITAL, FRAC, T1, T2, T3, FixedStrategy, HundredthStrategy, Sim, _held_ep, _md)
from control_api.tests.test_shadow_reliability import NOW, BudgetStrategy, SimMD, book, trade

T4 = "KXDEMOA-26OCT19-T4"
SLIM = 0.2                              # test value: series cap 15.0 dollars (ceiling 75)
SCAP = SLIM * FRAC * CAPITAL
MLIM = 0.12                             # test value: market cap 9.0 dollars


@pytest.fixture(autouse=True)
def _no_private_files(tmp_path, monkeypatch):
    monkeypatch.setenv(sh.FEE_SCHEDULE_ENV, str(tmp_path / "absent_fee_schedule.json"))
    monkeypatch.setenv(sh.SHADOW_LIMITS_ENV, str(tmp_path / "absent_shadow_limits.json"))


def _cyc(md, st, *, held=(), series_limit=SLIM, market_limit=None, series=("KXDEMOA",), run_budget=40.0,
         max_order=15.0, now=NOW):
    return sh.run_cycle(md, st, sh.CycleInput(
        series=list(series), run_budget=run_budget, max_order=max_order, now=now, held=list(held), capital=CAPITAL,
        max_open_frac=FRAC, fee_schedule=None, series_share_limit=series_limit, market_share_limit=market_limit))


def _by(res):
    return {u["ticker"]: u for u in res.upserts}


# ================================================================ sizing to the series room
def test_a_bid_near_the_series_cap_is_sized_down_in_hundredths():
    # held 10 in the series (another market): room 5.0 -> 10 contracts of the 60 wanted
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 60)]), held=[_held_ep(T2, 10.0)])
    (ep,) = res.upserts
    assert ep["count"] == 10.0 and ep["capped"] is False and ep["cap_reason"] is None
    # a price that does not divide the room is floored to 0.01, never rounded up (5.0 / 0.7 = 7.142 -> 7.14)
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.7, 40)]), held=[_held_ep(T2, 10.0)])
    assert res.upserts[0]["count"] == pytest.approx(7.14) and res.upserts[0]["cost"] <= 5.0 + 1e-9


def test_room_for_under_one_contract_is_no_quote_and_exactly_one_is_a_quote():
    mk = lambda: _md(("KXDEMOA", [T1, T2]))                               # noqa: E731
    st = lambda: FixedStrategy([(T1, "yes", "bid", 0.5, 60)])             # noqa: E731
    res = _cyc(mk(), st(), held=[_held_ep(T2, 14.5)])                     # room 0.5 = exactly one contract
    assert res.upserts[0]["count"] == pytest.approx(1.0) and res.upserts[0]["capped"] is False
    res = _cyc(mk(), st(), held=[_held_ep(T2, 14.51)])                    # room 0.49: under one contract
    (ep,) = res.upserts
    assert ep["capped"] is True and ep["cap_reason"] == "series_cap" and ep["pnl"] is None
    assert ep["trades_read"] is True and ep["fill"] == {"lo": 0.0, "mid": 0.0, "hi": 0.0}
    assert res.pass_doc["series_capped"] == ["KXDEMOA"] and res.pass_doc["capped_new"] == 1


def test_a_series_at_its_cap_gets_no_new_bid_and_other_series_are_unaffected():
    st = FixedStrategy([(T1, "yes", "bid", 0.5, 12), (T3, "yes", "bid", 0.5, 12)])
    res = _cyc(_md(("KXDEMOA", [T1, T2]), ("KXDEMOB", [T3])), st, held=[_held_ep(T2, SCAP)],
               series=("KXDEMOA", "KXDEMOB"))
    by = _by(res)
    assert by[T1]["capped"] is True and by[T1]["cap_reason"] == "series_cap"
    assert by[T3]["capped"] is False and by[T3]["count"] == 12.0
    assert res.pass_doc["series_capped"] == ["KXDEMOA"]


def test_the_series_room_is_shared_across_its_markets_in_one_pass():
    st = FixedStrategy([(T1, "yes", "bid", 0.5, 20), (T2, "yes", "bid", 0.5, 20), (T4, "yes", "bid", 0.5, 20)])
    res = _cyc(_md(("KXDEMOA", [T1, T2, T4])), st)
    by = _by(res)
    assert by[T1]["count"] == 20.0                                        # 10.0 of the 15.0
    assert by[T2]["count"] == pytest.approx(10.0)                         # the 5.0 left
    assert by[T4]["capped"] is True and by[T4]["cap_reason"] == "series_cap"
    live = [u for u in res.upserts if not u["capped"]]
    assert sum(u["cost"] for u in live) <= SCAP + sh.CAP_TOL
    # with the budget flow (a real per-order sizing strategy) the markets still add up to the room
    res = _cyc(_md(("KXDEMOA", [T1, T2, T4])), HundredthStrategy({T1: 0.95, T2: 0.95, T4: 0.95}), run_budget=60.0)
    assert sum(u["cost"] for u in res.upserts if not u["capped"]) <= SCAP + sh.CAP_TOL


def test_the_smaller_of_market_room_and_series_room_binds():
    # market cap 9.0, series cap 15.0, 8.0 held elsewhere in the series: series room 7.0 < market room 9.0
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 40)]),
               held=[_held_ep(T2, 8.0)], market_limit=MLIM)
    assert res.upserts[0]["count"] == pytest.approx(14.0) and res.pass_doc["market_capped"] == []
    # nothing held: market room 9.0 < series room 15.0
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 60)]), market_limit=MLIM)
    assert res.upserts[0]["count"] == 18.0 and res.pass_doc["market_capped"] == [T1]
    # series room gone (0.3) but market room whole: refused as the SERIES cap, the market is not blamed
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 60)]),
               held=[_held_ep(T2, 14.7)], market_limit=MLIM)
    (ep,) = res.upserts
    assert ep["capped"] is True and ep["cap_reason"] == "series_cap" and res.pass_doc["market_capped"] == []
    # market room gone, series room whole: refused as the MARKET cap
    res = _cyc(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 60)]),
               held=[_held_ep(T1, 9.0)], market_limit=MLIM)
    assert res.upserts[0]["cap_reason"] == "market_cap"


def test_plenty_of_room_is_the_plain_answer():
    for strat in (lambda: HundredthStrategy({T1: 0.6, T2: 0.7, T4: 0.8}), lambda: BudgetStrategy({T1: 0.6, T2: 0.7})):
        a = _cyc(_md(("KXDEMOA", [T1, T2, T4])), strat(), series_limit=0.9, run_budget=30.0)   # cap 67.5: nothing binds
        b = _cyc(_md(("KXDEMOA", [T1, T2, T4])), strat(), series_limit=None, run_budget=30.0)
        key = lambda r: sorted((u["ticker"], u["count"], u["capped"], u["cap_reason"]) for u in r.upserts)  # noqa: E731
        assert key(a) == key(b) and a.pass_doc["series_capped"] == b.pass_doc["series_capped"] == []
    # the strategy is asked the plain way (one call, the whole series) when the limit does not bind
    st = BudgetStrategy({T1: 0.6})
    _cyc(_md(("KXDEMOA", [T1, T2])), st, series_limit=0.9, run_budget=30.0)
    assert len([c for c in st.calls if c["budget"] >= 30.0 - 1e-9]) >= 1


def test_the_series_limit_never_loosens_the_other_limits():
    res = _cyc(_md(("KXDEMOA", [T1])), BudgetStrategy({T1: 0.5}), run_budget=3.0)
    assert res.upserts[0]["cost"] <= 3.0 + 1e-9


# ================================================================ stability over passes
def test_continuing_bids_are_not_double_counted_against_the_series_room():
    sim = Sim(_md(("KXDEMOA", [T1, T2])), FixedStrategy([(T1, "yes", "bid", 0.5, 20), (T2, "yes", "bid", 0.5, 20)]),
              series=["KXDEMOA"], market_limit=None, series_limit=SLIM)
    sim.cycle(NOW)
    ids = sorted(e["ep_id"] for e in sim.live())
    assert sum(e["cost"] for e in sim.live()) == pytest.approx(SCAP)
    for k in range(1, 4):                                                 # the resting bids fill the room exactly
        res = sim.cycle(NOW + timedelta(minutes=10 * k))
        assert res.pass_doc["new_eps"] == 0 and res.pass_doc["ended_eps"] == 0
        assert sorted(e["ep_id"] for e in sim.live()) == ids


def test_filled_bids_use_up_the_room_over_passes():
    sim = Sim(SimMD({"KXDEMOA": [T1]}), BudgetStrategy({}), series=["KXDEMOA"], series_limit=SLIM, market_limit=None)
    peaks = []
    for k in range(12):
        now = NOW + timedelta(minutes=10 * k)
        p = round(0.90 - 0.01 * k, 2)
        sim.st.prices[T1] = p
        sim.md.books[T1] = book(p, 5)
        sim.md.trade_rows.setdefault(T1, []).append(trade(now + timedelta(minutes=5), p - 0.01, 1000))
        sim.cycle(now)
        peaks.append(sim.held() + sim.resting())
    assert max(peaks) <= SCAP + sh.CAP_TOL + 1e-6
    assert any(e.get("cap_reason") == "series_cap" for e in sim.eps.values() if e.get("capped"))
    assert any(p["series_capped"] == ["KXDEMOA"] for p in sim.passes)


# ================================================================ property: Shadow and the verifier agree
@pytest.mark.parametrize("seed", range(10))
def test_property_held_plus_resting_per_series_never_exceeds_the_cap(seed):
    rng = random.Random(seed)
    tickers = [T1, T2, T3]
    slim = rng.choice([0.08, 0.15, 0.3])
    mlim = rng.choice([None, None, 0.05])
    sim = Sim(SimMD({"KXDEMOA": [T1, T2], "KXDEMOB": [T3]}), BudgetStrategy({}), series=["KXDEMOA", "KXDEMOB"],
              market_limit=mlim, series_limit=slim, run_budget=rng.choice([20.0, 40.0]), max_order=15.0)
    ceiling = FRAC * CAPITAL
    snaps = []
    for k in range(25):
        now = NOW + timedelta(minutes=10 * k)
        for t in tickers:
            p = round(rng.uniform(0.55, 0.9), 2)
            sim.st.prices[t] = p
            sim.md.books[t] = book(p, rng.randint(1, 20))
            if rng.random() < 0.5:
                sim.md.trade_rows.setdefault(t, []).append(
                    trade(now + timedelta(minutes=rng.randint(1, 9)), p - 0.01, rng.randint(1, 400)))
        sim.cycle(now)
        snaps.append((now, {i: dict(e) for i, e in sim.eps.items()}))
        for s, ts in (("KXDEMOA", (T1, T2)), ("KXDEMOB", (T3,))):                   # Shadow's own books
            tot = sum(sim.held(t) + sim.resting(t) for t in ts)
            assert tot <= slim * ceiling + sh.CAP_TOL + 1e-6, (seed, k, s)
        bad = [v for v in V.cap_violations(sim.eps.values(), capital=CAPITAL, max_open_frac=FRAC,
                                           run_budget=sim.run_budget, series=["KXDEMOA", "KXDEMOB"], series_cap=slim)
               if "> cap" in v or v.startswith("capped")]
        assert bad == [], (seed, k, bad[:3])
    # the verifier's hindsight check (fills valued at their final state) agrees
    assert V.series_funding_violations(snaps, sim.eps, ceiling=ceiling, series_cap=slim) == [], seed
    assert V.series_funding_violations(snaps, sim.eps, ceiling=ceiling, series_cap=None) == []


def test_the_verifier_catches_a_series_over_its_cap():
    start = NOW
    over = {"ep_id": "x", "series": "KXDEMOA", "ticker": T1, "price": 0.5, "count": 40.0, "cost": 20.0, "end_ts": None,
            "capped": False, "result": None, "fill": None}
    assert V.series_funding_violations([(start, {"x": over})], {"x": over}, ceiling=75.0, series_cap=SLIM)
    assert V.cap_violations([over], capital=CAPITAL, max_open_frac=FRAC, run_budget=40.0, series=["KXDEMOA"],
                            series_cap=SLIM)
    ok = {**over, "count": 30.0, "cost": 15.0}
    assert V.series_funding_violations([(start, {"x": ok})], {"x": ok}, ceiling=75.0, series_cap=SLIM) == []

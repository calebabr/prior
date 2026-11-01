"""Ledger reconciliation, model pair_time_v2: YES / NO pairs are booked when they form (Kalshi credits $1 per pair
then), not at settlement. Pure functions over hand-built rows; no I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.services import ledger as lg
from control_api.tests.test_ui1a_backend import fill, rows_of, settle

UTC = timezone.utc
D0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TK = "KXDEMOB-26OCT22-B70"


def rec_of(fills, setts, snaps, **kw):
    return lg.reconciliation(rows_of(fills, setts), snaps, [], since=D0, now=D0 + timedelta(days=5), **kw)


def test_pair_formed_in_window_on_an_open_market_reconciles():
    fills = [fill("y1", D0 + timedelta(hours=1), ticker=TK, side="yes", price=0.70, count=4, taker=True, fee=0.05),
             fill("n1", D0 + timedelta(hours=2), ticker=TK, side="no", price=0.20, count=4)]
    # cash: -2.80 -0.05 -0.80 +4.00 (the pair credit), nothing left in exposure, market still open
    snaps = [{"ts": D0, "cash": 100.0, "exposure": 0.0},
             {"ts": D0 + timedelta(days=1), "cash": 100.35, "exposure": 0.0}]
    rec = rec_of(fills, [], snaps)
    assert rec["model"] == "pair_time_v2" and rec["equity_change"] == 0.35
    assert rec["pair_timing"] == 0.40 and rec["pairs_formed_in_window"] == 4.0 and rec["open_pair_markets"] == 1
    assert rec["residual"] == 0.0 and rec["flag"] is False
    assert rec["fee_timing"] == -0.05 and rec["realized_pnl"] == 0.0
    old = rec["equity_change"] - (rec["net_flows"] + rec["realized_pnl"] + rec["fee_timing"])
    assert old == pytest.approx(0.40)                       # the settlement-time formula left the pair gain unexplained
    assert any("both YES and NO" in n for n in rec["notes"])


def test_pair_formed_before_the_window_is_not_counted_again_when_it_settles():
    fills = [fill("y1", D0 - timedelta(days=1), ticker=TK, side="yes", price=0.70, count=6),
             fill("n1", D0 - timedelta(days=1, minutes=-5), ticker=TK, side="no", price=0.20, count=4)]
    # start: cash 100 -4.2 -0.8 +4 = 99.0, exposure 1.4 (2 un-paired YES); end: YES wins, 2 contracts pay $2
    snaps = [{"ts": D0, "cash": 99.0, "exposure": 1.4}, {"ts": D0 + timedelta(days=2), "cash": 101.0, "exposure": 0.0}]
    rec = rec_of(fills, [settle(TK, "yes", D0 + timedelta(days=1))], snaps)
    assert rec["equity_change"] == 0.6 and rec["realized_pnl"] == 1.0         # fill_pnl books a gain of 1.8 for the yes fill and a loss of 0.8 for the no fill
    assert rec["pair_timing"] == -0.4 and rec["pairs_before_window_settled_in_window"] == 4.0
    assert rec["pair_pnl_before_window_settled_in_window"] == 0.4 and rec["pairs_formed_in_window"] == 0.0
    assert rec["residual"] == 0.0 and rec["flag"] is False


def test_pair_inside_window_that_also_settles_inside_is_neutral():
    fills = [fill("y1", D0 + timedelta(hours=1), ticker=TK, side="yes", price=0.70, count=4),
             fill("n1", D0 + timedelta(hours=2), ticker=TK, side="no", price=0.20, count=4)]
    snaps = [{"ts": D0, "cash": 100.0, "exposure": 0.0}, {"ts": D0 + timedelta(days=2), "cash": 100.4, "exposure": 0.0}]
    rec = rec_of(fills, [settle(TK, "no", D0 + timedelta(days=1))], snaps)
    assert rec["pair_timing"] == 0.0 and rec["residual"] == 0.0 and rec["open_pair_markets"] == 0


def test_no_pairs_and_fees_on_an_open_fill():
    fills = [fill("a1", D0 + timedelta(hours=1), ticker=TK, side="yes", price=0.50, count=4, taker=True, fee=0.07)]
    snaps = [{"ts": D0, "cash": 100.0, "exposure": 0.0}, {"ts": D0 + timedelta(days=1), "cash": 97.93, "exposure": 2.0}]
    rec = rec_of(fills, [], snaps)
    assert rec["pair_timing"] == 0.0 and rec["pairs_formed_in_window"] == 0.0 and rec["open_pair_markets"] == 0
    assert rec["fee_timing"] == -0.07 and rec["residual"] == 0.0
    assert not any("both YES and NO" in n for n in rec["notes"])


def test_pair_timing_keys_exist_when_not_computable():
    rec = lg.reconciliation([], [], [], since=D0, now=D0)
    assert rec["pair_timing"] is None and rec["model"] == "pair_time_v2" and rec["tolerance"] == 0.10


def test_netting_uses_average_cost_not_fifo():
    t = D0
    rs = rows_of([fill("y1", t, ticker=TK, side="yes", price=0.50, count=2, taker=True),
                  fill("y2", t + timedelta(minutes=1), ticker=TK, side="yes", price=0.70, count=2, taker=True),
                  fill("n1", t + timedelta(minutes=2), ticker=TK, side="no", price=0.20, count=2, taker=True)], [])
    net = lg.netting(rs)
    assert net["pairs"] == 2.0 and net["cost_basis"] == "average" and net["verified"] is False
    assert net["pair_cost"] == pytest.approx(2 * 0.60 + 2 * 0.20)               # FIFO would give 1.00 + 0.40
    assert net["pair_gain"] == pytest.approx(0.40)
    ev = lg.pair_events(rs)[TK]
    assert len(ev) == 1 and ev[0][1] == 2.0 and ev[0][2] == pytest.approx(0.40)

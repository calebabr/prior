"""B5 (2026-11-27): the loss breaker ignores the owner's own trades, plus the security fixes M-1, L-1 and L-2.

Fakes follow Kalshi's documented shapes. GET /portfolio/positions (settlement_status=all, count_filter=total_traded)
-> {market_positions: [{ticker, exchange_index, total_traded_dollars, position_fp, market_exposure_dollars,
realized_pnl_dollars (before fees), fees_paid_dollars, last_updated_ts, netting_enabled}], event_positions, cursor}.
GET /portfolio/fills rows: V2 shape with book_side; a NO buy arrives as action "sell", book_side "ask",
outcome_side "no". A multi-leg combo (KXMVE... ticker) is one market position like any other. All tickers, ids and
amounts are synthetic."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as rn
from control_api.routes import ux
from control_api.services import breaker as bk

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)          # 10:00 New York
ON = {**bk.DEFAULTS, "demo_enabled": True}
COMBO = "KXMVEDEMO-26NOV10-C0FFEE"


def pos(ticker, *, realized="0.0000", fees="0.0000", exposure="0.0000", position="0.00", updated=T0 - timedelta(hours=1)):
    return {"ticker": ticker, "exchange_index": 0, "total_traded_dollars": "1.0000", "position_fp": position,
            "market_exposure_dollars": exposure, "realized_pnl_dollars": realized, "fees_paid_dollars": fees,
            "last_updated_ts": updated.isoformat(), "netting_enabled": False}


def fill(ticker, *, source="you", by="order", env="live", fid=None, **kw):
    f = {"fill_id": fid or f"f-{ticker}-{source}-{by}", "ticker": ticker, "action": "buy", "book_side": "bid",
         "outcome_side": "yes", "side": "yes", "yes_price_dollars": "0.4000", "count_fp": "5.00", "fee_cost": "0.07",
         "is_taker": True, "env": env, "source": source, "source_by": by, "strategy": None if source == "you" else "maker"}
    f.update(kw)
    return f


NO_BUY = dict(action="sell", book_side="ask", outcome_side="no", side="no", no_price_dollars="0.3000")


def evid(positions, fills, *, reading=T0, synced=T0 + timedelta(minutes=1), complete=True, env="live"):
    return bk.manual_evidence(positions, fills, env=env, reading_utc=reading, synced_utc=synced, complete=complete)


def step(state, env, account, ts, ev):
    prev = (state.get("envs") or {}).get(env) or {}
    upd = bk.manual_update(prev.get("manual"), ev, ts, carry=bk._num(prev.get("last_m")))
    return bk.evaluate(ON, state, env, account, ts, manual=upd)


# ================================================================== rule 1: proof required
def test_only_markets_proven_by_the_order_client_id_are_excluded():
    fills = [fill("KXA-1"),                                                # proven by the order's client id
             fill("KXA-2", by="default"),                                  # unknown order id
             fill("KXA-3", by="no_order_id"),
             fill("KXA-4", source="bot", by="client_order_id"),            # the bot's
             fill("KXA-5"), fill("KXA-5", source="bot", by="journal", fid="b5"),   # both: counts in full
             {**fill("KXA-6"), "source": None},                            # unlabelled (legacy): bot view
             fill(COMBO, **NO_BUY)]                                        # a NO buy in the V2 shape, a combo
    positions = [pos(t, realized="-2.0000") for t in ("KXA-1", "KXA-2", "KXA-3", "KXA-4", "KXA-5", "KXA-6", COMBO)]
    positions.append(pos("KXA-7", realized="-1.0000"))                     # no stored fill at all: unsynced
    ev = evid(positions, fills)
    assert ev["proven"] == {"KXA-1", COMBO}
    assert ev["bot"] == {"KXA-4", "KXA-5", "KXA-6"}
    assert ev["unproven"] == 3                                             # KXA-2, KXA-3, KXA-7
    assert ev["late"] == set() and ev["pending"] == set()


def test_a_market_changed_after_the_last_fills_sync_is_pending_not_proven():
    p = [pos("KXA-1", updated=T0 + timedelta(minutes=2))]
    ev = evid(p, [fill("KXA-1")], reading=T0 + timedelta(minutes=5), synced=T0)
    assert ev["proven"] == set() and ev["pending"] == {"KXA-1"} and ev["unproven"] == 1
    ev = evid(p, [fill("KXA-1")], reading=T0 + timedelta(minutes=5), synced=None)     # never synced (restart)
    assert ev["pending"] == {"KXA-1"}


def test_demo_fills_never_prove_a_live_market():
    ev = evid([pos("KXA-1")], [fill("KXA-1", env="demo")])
    assert ev["proven"] == set() and ev["unproven"] == 1


def test_unusable_inputs_are_no_evidence():
    assert bk.manual_evidence(None, [], env="live", reading_utc=T0, synced_utc=T0, complete=True) is None
    assert evid([{"no": "ticker"}], []) is None
    ev = evid([pos("KXA-1", realized="nan")], [fill("KXA-1")])
    assert ev["unproven"] == 1 and ev["unusable"] == {"KXA-1"}


# ================================================================== the ledger: anchor, once only, archive
def _anchored(values_at_anchor):
    ev = evid([pos(t, realized=v) for t, v in values_at_anchor.items()], [fill(t) for t in values_at_anchor])
    return bk.manual_update(None, ev, T0, carry=None)


def test_first_application_anchors_without_a_jump_and_counts_only_later_events():
    a = _anchored({"KXA-1": "-3.0000"})                       # a loss before the anchor stays counted (as before)
    assert a["m_new"] == 0.0 and a["separable"] is False and a["ledger"]["base"] == {"KXA-1": -3.0}
    t1 = T0 + timedelta(hours=1)
    ev = evid([pos("KXA-1", realized="-5.0000", fees="0.0700", updated=T0 + timedelta(minutes=30)),
               pos(COMBO, realized="-4.0000", fees="0.1000", updated=T0 + timedelta(minutes=40))],
              [fill("KXA-1"), fill(COMBO, **NO_BUY)], reading=t1, synced=t1)
    u = bk.manual_update(a["ledger"], ev, t1, carry=0.0)
    assert u["separable"] is True and u["m_new"] == pytest.approx(-2.07 - 4.10)
    again = bk.manual_update(u["ledger"], ev, t1 + timedelta(minutes=10), carry=u["m_new"])   # same data: no double
    assert again["m_new"] == pytest.approx(u["m_new"])


def test_a_market_kalshi_archives_keeps_its_contribution():
    a = _anchored({})
    t1 = T0 + timedelta(hours=1)
    ev = evid([pos(COMBO, realized="-4.0000")], [fill(COMBO)], reading=t1, synced=t1)
    u = bk.manual_update(a["ledger"], ev, t1, carry=0.0)
    assert u["m_new"] == -4.0
    gone = evid([], [fill(COMBO)], reading=t1 + timedelta(hours=1), synced=t1 + timedelta(hours=1))
    u2 = bk.manual_update(u["ledger"], gone, t1 + timedelta(hours=1), carry=-4.0)
    assert u2["m_new"] == -4.0 and u2["ledger"]["archived"] == -4.0 and u2["ledger"]["seen"] == {}
    partial = evid([], [fill(COMBO)], complete=False, reading=t1 + timedelta(hours=1), synced=t1 + timedelta(hours=1))
    u3 = bk.manual_update(u["ledger"], partial, t1 + timedelta(hours=1), carry=-4.0)
    assert u3["separable"] is False and u3["ledger"]["seen"] == {COMBO: -4.0}           # unknown: kept, not archived


def test_a_bot_fill_on_a_manual_market_makes_it_count_in_full():
    a = _anchored({})
    t1 = T0 + timedelta(hours=1)
    u = bk.manual_update(a["ledger"], evid([pos("KXA-1", realized="-4.0000")], [fill("KXA-1")], reading=t1,
                                           synced=t1), t1, carry=0.0)
    both = [fill("KXA-1"), fill("KXA-1", source="bot", by="journal", fid="b1")]
    u2 = bk.manual_update(u["ledger"], evid([pos("KXA-1", realized="-4.0000")], both, reading=t1, synced=t1), t1,
                          carry=-4.0)
    assert u2["m_new"] == 0.0


# ================================================================== rule 2: both directions, timing
def _base_state(account=100.0):
    """A live env already anchored (M = 0) at T0 with a high-water mark of `account`."""
    st = bk.empty_state()
    res = step(st, "live", account, T0, evid([], []))
    return res["state"]


def test_a_manual_loss_never_trips_and_a_manual_win_never_raises_the_mark():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    # the owner lost $30 on a combo: account 70, but the bot did nothing
    ev = evid([pos(COMBO, realized="-29.9000", fees="0.1000", updated=T0 + timedelta(minutes=10))],
              [fill(COMBO, **NO_BUY)], reading=t1, synced=t1)
    res = step(st, "live", 70.0, t1, ev)
    assert res["trip"] is None
    d = res["state"]["envs"]["live"]
    assert d["last_equity"] == pytest.approx(100.0) and d["last_m"] == pytest.approx(-30.0)
    assert d["hwm"] == pytest.approx(100.0)
    # then a $50 manual win elsewhere: account 120, the mark stays at 100
    t2 = t1 + timedelta(hours=1)
    ev2 = evid([pos(COMBO, realized="-29.9000", fees="0.1000"), pos("KXA-9", realized="50.0000",
                                                                    updated=t1 + timedelta(minutes=5))],
               [fill(COMBO, **NO_BUY), fill("KXA-9")], reading=t2, synced=t2)
    res2 = step(res["state"], "live", 120.0, t2, ev2)
    d2 = res2["state"]["envs"]["live"]
    assert d2["hwm"] == pytest.approx(100.0) and d2["last_equity"] == pytest.approx(100.0)
    assert bk.env_view(ON, d2, "live")["manual_excluded"] == pytest.approx(20.0)


def test_the_same_loss_by_the_bot_still_trips():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    ev = evid([pos("KXA-1", realized="-30.0000")], [fill("KXA-1", source="bot", by="journal")], reading=t1,
              synced=t1)
    assert step(st, "live", 70.0, t1, ev)["trip"]["reason"] == "drawdown"


def test_an_unproven_manual_loss_counts_in_full_and_trips():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    ev = evid([pos(COMBO, realized="-30.0000")], [fill(COMBO, by="default")], reading=t1, synced=t1)
    res = step(st, "live", 70.0, t1, ev)
    assert res["trip"] and res["state"]["envs"]["live"]["manual_unproven"] == 1


def test_a_win_updated_after_the_reading_is_left_out_and_a_late_loss_counts_in_full():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    # the win shows in positions (read later) but maybe not in the balance read at t1: never raises the mark
    late_win = evid([pos("KXA-9", realized="25.0000", updated=t1 + timedelta(seconds=30))], [fill("KXA-9")],
                    reading=t1, synced=t1 + timedelta(minutes=1))
    res = step(st, "live", 125.0, t1, late_win)
    assert res["state"]["envs"]["live"]["hwm"] == pytest.approx(100.0)
    # a late manual loss: counted in full (the conservative side), so it can trip
    late_loss = evid([pos("KXA-8", realized="-30.0000", updated=t1 + timedelta(seconds=30))], [fill("KXA-8")],
                     reading=t1, synced=t1 + timedelta(minutes=1))
    assert step(st, "live", 70.0, t1, late_loss)["trip"] is not None


def test_no_evidence_uses_the_stored_m_and_never_jumps():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    ev = evid([pos(COMBO, realized="-30.0000")], [fill(COMBO)], reading=t1, synced=t1)
    st = step(st, "live", 70.0, t1, ev)["state"]
    res = step(st, "live", 70.0, t1 + timedelta(minutes=10), None)            # Kalshi read failed
    d = res["state"]["envs"]["live"]
    assert res["trip"] is None and d["last_m"] == pytest.approx(-30.0) and d["last_equity"] == pytest.approx(100.0)


# ================================================================== rule 4: limit size
def test_manual_losses_tighten_the_limit_and_wins_never_loosen_it():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    loss = evid([pos(COMBO, realized="-40.0000")], [fill(COMBO)], reading=t1, synced=t1)
    # bot-only equity 89 (11 below the mark): under 15% of 100, but over 15% of (100 - 40) = 9 -> trips
    res = step(st, "live", 49.0, t1, loss)
    assert res["trip"]["reason"] == "drawdown" and res["trip"]["drawdown_limit"] == pytest.approx(9.0)
    assert res["trip"]["manual_excluded"] == pytest.approx(-40.0) and res["trip"]["account_equity"] == 49.0
    # bot-only 93: drawdown 7 < 9, day loss 7 >= 10% of (100 - 40) = 6
    assert step(st, "live", 53.0, t1, loss)["trip"]["limits"] == ["day_loss"]
    assert step(st, "live", 55.0, t1, loss)["trip"] is None                         # 5 below: under both

    win = evid([pos("KXA-9", realized="40.0000")], [fill("KXA-9")], reading=t1, synced=t1)
    res2 = step(st, "live", 132.0, t1, win)                               # bot-only 92: limit stays 15 (of 100)
    assert res2["trip"] is None
    assert step(st, "live", 124.0, t1, win)["trip"]["drawdown_limit"] == pytest.approx(15.0)


# ================================================================== rule 3/5: state, restarts, acknowledge
def test_state_round_trips_and_a_corrupt_ledger_reanchors_without_a_jump():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    st = step(st, "live", 70.0, t1, evid([pos(COMBO, realized="-30.0000")], [fill(COMBO)], reading=t1,
                                          synced=t1))["state"]
    back, warns = bk.normalize_state(bk.encode_state(st))
    assert not warns and back["envs"]["live"]["manual"] == st["envs"]["live"]["manual"]
    enc = bk.encode_state(st)
    enc["envs"]["live"]["manual"] = {"base": "garbage"}
    back, _ = bk.normalize_state(enc)
    assert back["envs"]["live"]["manual"] is None
    t2 = t1 + timedelta(hours=1)
    res = step(back, "live", 70.0, t2, evid([pos(COMBO, realized="-30.0000")], [fill(COMBO)], reading=t2,
                                             synced=t2))
    d = res["state"]["envs"]["live"]
    assert d["last_m"] == pytest.approx(-30.0) and d["manual"]["archived"] == pytest.approx(-30.0)
    assert res["trip"] is None                                            # carried, no false jump
    enc["envs"]["live"]["last_m"] = "x"                                    # M unreadable: marks re-based
    back, warns = bk.normalize_state(enc)
    assert "live" not in back["envs"] and warns


def test_acknowledge_rebases_on_the_adjusted_equity():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    st = step(st, "live", 40.0, t1, evid([pos(COMBO, realized="-30.0000")], [fill(COMBO)], reading=t1,
                                          synced=t1))["state"]
    assert st["tripped"]                                                  # bot-only 70: a real drawdown
    ack = bk.acknowledge(st)["envs"]["live"]
    assert ack["hwm"] == pytest.approx(70.0) and ack["hwm_m"] == pytest.approx(-30.0)
    assert ack["manual"] == st["envs"]["live"]["manual"]


def test_older_callers_are_unchanged():
    res = bk.evaluate(ON, bk.empty_state(), "live", 100.0, T0)
    assert "last_m" not in res["state"]["envs"]["live"]


def test_views_and_alert_show_the_excluded_amount():
    st = _base_state(100.0)
    t1 = T0 + timedelta(hours=1)
    res = step(st, "live", 52.0, t1, evid([pos(COMBO, realized="-40.0000")], [fill(COMBO)], reading=t1, synced=t1))
    v = bk.status_view(ON, res["state"], readable=True, warning=None, mode="live")["envs"]["live"]
    assert v["account_equity"] == 52.0 and v["manual_excluded"] == -40.0 and v["manual_unproven"] == 0
    assert v["last_equity"] == 92.0
    assert "your own trades left out: -$40.00" in bk.alert(res["trip"])["body"]
    assert bk.audit_details(res["trip"])["manual_excluded"] == -40.0
    empty = bk.env_view(ON, None, "live")
    assert empty["account_equity"] is None and empty["manual_excluded"] is None and empty["manual_unproven"] is None


# ================================================================== runner glue
def _kalshi_read(positions, *, account=None, complete=True, t=None):
    def read(env, with_balance):
        return {"t": t or rn.utcnow(), "positions": [dict(p) for p in positions], "complete": complete,
                "account": account if with_balance else None}
    return read


async def _prime(r, store, now, fills):
    await store.upsert_fills(fills)
    r._pnl_gen += 1
    r._fills_synced["live"] = now + timedelta(minutes=1)
    r.cloud.setdefault(bk.CLOUD_SETTINGS, {"live_enabled": True, "demo_enabled": False,
                                           "drawdown_frac": 0.15, "day_loss_frac": 0.10})


@pytest.mark.asyncio
async def test_runner_excludes_a_proven_manual_loss_and_audits_nothing(env):
    r = env.runner
    now = rn.utcnow()
    fl = [fill(COMBO, fid="m1", ts=now - timedelta(hours=2), **NO_BUY)]
    await _prime(r, env.store, now, fl)
    r._read_breaker_kalshi = _kalshi_read([])
    assert await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker") is None
    r._read_breaker_kalshi = _kalshi_read([pos(COMBO, realized="-30.0000", updated=now - timedelta(minutes=20))])
    assert await r._breaker_observe("live", 70.0, now, source="maker") is None
    assert not r.breaker_tripped()
    view = r.breaker_status(full=True)["envs"]["live"]
    assert view["manual_excluded"] == -30.0 and view["account_equity"] == 70.0 and view["last_equity"] == 100.0


@pytest.mark.asyncio
async def test_runner_fresh_read_clears_a_trip_only_when_it_separates(env):
    r = env.runner
    now = rn.utcnow()
    await _prime(r, env.store, now, [fill(COMBO, fid="m1", ts=now - timedelta(hours=2))])
    r._read_breaker_kalshi = _kalshi_read([])
    await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker")
    calls = []

    def read(env_, with_balance):
        calls.append(with_balance)
        if not with_balance:                       # the first read: the loss is late (after the reading)
            return {"t": rn.utcnow(), "complete": True, "account": None,
                    "positions": [pos(COMBO, realized="-30.0000", updated=now + timedelta(seconds=30))]}
        return {"t": now + timedelta(minutes=1), "complete": True, "account": 70.0,
                "positions": [pos(COMBO, realized="-30.0000", updated=now + timedelta(seconds=30))]}
    r._read_breaker_kalshi = read
    assert await r._breaker_observe("live", 70.0, now, source="maker") is None
    assert calls == [False, True] and not r.breaker_tripped()


@pytest.mark.asyncio
async def test_runner_trips_when_the_fresh_read_fails(env):
    r = env.runner
    now = rn.utcnow()
    await _prime(r, env.store, now, [fill(COMBO, fid="m1", ts=now - timedelta(hours=2))])
    r._read_breaker_kalshi = _kalshi_read([])
    await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker")

    def read(env_, with_balance):
        if with_balance:
            raise RuntimeError("kalshi down")
        return {"t": rn.utcnow(), "complete": True, "account": None,
                "positions": [pos(COMBO, realized="-30.0000", updated=now + timedelta(seconds=30))]}
    r._read_breaker_kalshi = read
    trip = await r._breaker_observe("live", 70.0, now, source="maker")
    assert trip and r.breaker_tripped() and trip["manual_excluded"] == 0.0


@pytest.mark.asyncio
async def test_runner_evidence_failure_falls_back_to_the_old_behaviour(env):
    r = env.runner
    now = rn.utcnow()
    await _prime(r, env.store, now, [])

    def boom(env_, with_balance):
        raise RuntimeError("down")
    r._read_breaker_kalshi = boom
    await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker")
    trip = await r._breaker_observe("live", 80.0, now, source="maker")
    assert trip and trip["reason"] == "drawdown"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_stop_is_fast_while_the_evidence_read_hangs(env, monkeypatch):
    r = env.runner
    now = rn.utcnow()
    await _prime(r, env.store, now, [])
    gate = threading.Event()

    def hang(env_, with_balance):
        gate.wait(10)
        return {"t": rn.utcnow(), "positions": [], "complete": True, "account": None}
    r._read_breaker_kalshi = hang
    monkeypatch.setattr(rn.Runner, "BREAKER_EVIDENCE_TIMEOUT_S", 3.0)
    try:
        await r.start_trading(by="t")
        obs = asyncio.create_task(r._breaker_observe("live", 100.0, now, source="maker"))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        resp = await asyncio.wait_for(env.client.post("/api/bot/stop", headers=env.auth), 3)
        assert resp.status_code == 200 and time.monotonic() - t0 < 1.0
        assert not obs.done()                                            # the evidence read is still waiting
    finally:
        gate.set()
    await asyncio.wait_for(obs, 5)


# ================================================================== security pass 2: P2-M1, P2-L1
class LedgerKalshi:
    """GET-only fake: the ledger read (settlement_status=all, count_filter=total_traded) lists a settled bot market;
    the open-positions read (count_filter=position) lists only what is still open."""

    def __init__(self, ledger, open_rows, *, cash_cents=8000, truncated=False):
        self.ledger, self.open_rows, self.cash, self.truncated = ledger, open_rows, cash_cents, truncated
        self.reads: list[dict] = []

    def get_balance(self):
        return {"balance": self.cash, "balance_dollars": f"{self.cash / 100:.4f}", "portfolio_value": 0,
                "updated_ts": 1}

    def get_positions(self, **kw):
        self.reads.append(dict(kw))
        if kw.get("count_filter") == "position":
            body = {"market_positions": [dict(p) for p in self.open_rows], "event_positions": [], "cursor": ""}
            if self.truncated:
                body["truncated"] = True
            return body
        return {"market_positions": [dict(p) for p in self.ledger], "event_positions": [], "cursor": ""}

    def __getattr__(self, name):
        if name in ("create_order", "cancel_order", "amend_order", "batch_create_orders", "batch_cancel_orders"):
            raise AssertionError("no Kalshi writes")
        raise AttributeError(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_exposure", ["20.0000", "0.0000"])     # Kalshi keeps or drops it: both must trip
async def test_p2m1_a_settled_bot_loss_still_listed_still_trips(env, settled_exposure):
    r = env.runner
    now = rn.utcnow()
    bot_t = "KXDEMOB-26NOV10-B1"
    await _prime(r, env.store, now, [fill(bot_t, source="bot", by="journal", fid="b1", ts=now - timedelta(hours=3))])
    k = LedgerKalshi([], [])
    r.kalshi_factory = lambda m: k
    assert await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker") is None
    # the bot's $20 position settled at a loss: the snapshot reads 80 (20% below the mark, limit 15%)
    k.ledger = [pos(bot_t, realized="-20.0000", exposure=settled_exposure, position="0.00",
                    updated=now - timedelta(minutes=10))]
    trip = await r._breaker_observe("live", 80.0, now, source="maker")
    assert trip and trip["reason"] == "drawdown" and r.breaker_tripped()
    assert trip["account_equity"] == pytest.approx(80.0)                    # the fresh read: cash 80 + open 0
    assert {"count_filter": "position"}.items() <= k.reads[-1].items()      # open cost from its own read


@pytest.mark.asyncio
async def test_p2m1_fresh_account_counts_open_positions_only_and_a_truncated_read_keeps_the_trip(env):
    r = env.runner
    now = rn.utcnow()
    await _prime(r, env.store, now, [])
    k = LedgerKalshi([], [pos("KXDEMOC-1", exposure="5.0000", position="10.00")], cash_cents=7500)
    r.kalshi_factory = lambda m: k
    out = r._read_breaker_kalshi("live", True)
    assert out["account"] == pytest.approx(80.0)
    k.truncated = True
    assert r._read_breaker_kalshi("live", True)["account"] is None
    assert await r._breaker_observe("live", 100.0, now - timedelta(minutes=30), source="maker") is None
    assert await r._breaker_observe("live", 80.0, now, source="maker")             # no fresh account: trip kept


def test_p2l1_a_malformed_order_lookup_is_not_proof(env):
    r = env.runner
    replies = {"O1": {"order_id": "O1", "client_order_id": "x"},                     # no `order` envelope
               "O2": {"order": {"order_id": "O9", "client_order_id": "x"}},          # another order's id
               "O3": {"order": {"order_id": "O3", "client_order_id": None}},         # no client id
               "O4": {"order": {"order_id": "O3"}},
               "O5": {"order": {"order_id": "O5", "client_order_id": "web-123"}}}    # well formed: manual

    class K:
        def request(self, method, path, **kw):
            assert method == "GET"
            return replies[path.rsplit("/", 1)[1]]
    r.kalshi_factory = lambda m: K()
    assert r._lookup_orders_sync("live", list(replies)) == [{"order_id": "O5", "client_order_id": "web-123"}]


# ================================================================== security M-1, L-1, L-2
@pytest.mark.asyncio
async def test_m1_each_env_has_its_own_sync_watermark(env):
    r = env.runner
    seen = []
    r._fetch_fills_sync = lambda mode, since: seen.append((mode, since)) or ([], [])
    await r.sync_fills("demo", force=True)
    await r.sync_fills("live", force=True)
    (_, d_since), (_, l_since) = seen
    assert rn.utcnow() - l_since > timedelta(days=rn.FILLS_FIRST_DAYS - 1)     # live: full first window
    assert r.fills_synced_utc("demo") and r.fills_synced_utc("live")
    await r.sync_fills("demo", force=True)
    assert rn.utcnow() - seen[-1][1] < timedelta(days=3)                       # demo: from its own watermark
    assert (await r.sync_fills("live"))["reason"] == "not_due"


@pytest.mark.asyncio
async def test_m1_errors_are_per_env(env):
    r = env.runner

    def boom(mode, since):
        raise RuntimeError("x")
    r._fetch_fills_sync = boom
    await r.sync_fills("live", force=True)
    r.mode = "demo"
    assert r.status()["fills_sync"]["error"] is None
    r.mode = "live"
    assert r.status()["fills_sync"]["error"] == "RuntimeError"


def test_l1_bounded_fills_stops_on_a_repeated_cursor(monkeypatch):
    from control_api.services import botfiles
    botfiles.ensure_bot_path()
    from kalshi_client import KalshiClient
    c = KalshiClient("https://demo.example.invalid/trade-api/v2")
    calls = []

    def req(method, path, params=None, auth=False, **kw):
        calls.append(path)
        assert method == "GET"
        return {"fills": [{"fill_id": f"f{len(calls)}"}], "cursor": "same"}
    monkeypatch.setattr(c, "request", req)
    with pytest.raises(RuntimeError, match="fills_cursor_repeated"):
        rn._bounded_fills(c, 0)
    assert len(calls) == 2

    n = {"i": 0}

    def endless(method, path, params=None, auth=False, **kw):
        n["i"] += 1
        return {"fills": [{"fill_id": f"g{n['i']}"}], "cursor": f"c{n['i']}"}
    monkeypatch.setattr(c, "request", endless)
    with pytest.raises(RuntimeError, match="fills_page_limit"):
        rn._bounded_fills(c, 0)
    assert n["i"] == rn.FILLS_MAX_PAGES

    pages = iter([{"fills": [{"fill_id": "a"}, {"fill_id": "b"}], "cursor": "n1"}, {"fills": [{"fill_id": "b"}],
                                                                                     "cursor": ""},
                  {"fills": [], "cursor": ""}])
    monkeypatch.setattr(c, "request", lambda *a, **k: next(pages))
    assert [f["fill_id"] for f in rn._bounded_fills(c, 0)] == ["a", "b"]


@pytest.mark.asyncio
async def test_l1_a_stuck_fetch_is_reported_once(env, monkeypatch):
    r = env.runner
    gate = threading.Event()
    r._fetch_fills_sync = lambda mode, since: gate.wait(10) and ([], [])
    monkeypatch.setattr(rn, "FILLS_SYNC_TIMEOUT_S", 0.1)
    try:
        await r.sync_fills("demo", force=True)
        assert r.fills_sync_busy()
        r._fills_fetch_started -= rn.FILLS_STUCK_AFTER_S + 1
        r.status()
        r.status()
        assert (await r.sync_fills("demo", force=True))["reason"] == "in_flight"
        await asyncio.sleep(0.05)
        assert r.status()["fills_sync"]["error"] == "stuck"
        assert len([a for a in r.alerter.sent if a["title"] == "Fills sync stuck"]) == 1
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_l2_portfolio_value_with_no_positions_is_not_shown(env):
    from control_api.tests.test_live_numbers import SummaryKalshi
    env.kalshi = SummaryKalshi(balance={"balance": 10000, "portfolio_value": 10000, "updated_ts": 1}, positions=[])
    body = (await env.client.get("/api/account/summary", headers=env.auth)).json()
    assert body["positions_value"] is None and body["total_value"] is None and body["unrealized"] is None
    assert body["cash"] == 100.0 and body["positions_cost"] == 0.0
    assert ux.summary_numbers({"cash": 1.0, "positions_cost": 0.0, "positions_value": 0.0})["total_value"] == 1.0

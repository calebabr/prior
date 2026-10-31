"""Sprint UI-1a backend: local-date buckets, per-strategy win rate, arbitrage counters and grouped sets, the Honest
Ledger and its reconciliation, the functional scorecard, cash-flow guard rails, the shadow_capital setting and the
Today's P&L state. Clock-dependent assertions use fixed dates; API tests use relative data but never assert a
date bucket.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from control_api.routes import analytics as ra
from control_api.services import analytics as an
from control_api.services import arbsets
from control_api.services import ledger as lg
from control_api.services import pnl as pnlsvc
from control_api.tests.fake_cells import TEST_CELLS as CELLS

UTC = timezone.utc
CHI, _ = pnlsvc.resolve_tz("America/Chicago")


def z(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def fill(fid, ts, ticker="KXDEMOB-26OCT10-B70", side="no", price=0.40, count=10, taker=False, fee=0.0, **extra):
    return {"fill_id": fid, "order_id": f"o-{fid}", "ticker": ticker, "side": side, "count": count,
            "yes_price_dollars": f"{(1 - price) if side == 'no' else price:.4f}",
            "no_price_dollars": f"{price if side == 'no' else (1 - price):.4f}", "fee_cost": f"{fee:.4f}",
            "is_taker": taker, "env": "demo", "created_time": z(ts), **extra}


def settle(ticker, result, ts):
    return {"ticker": ticker, "market_result": result, "settled_time": z(ts)}


def rows_of(fills, settlements):
    return an.trade_rows(fills, settlements, [], cells=CELLS, series_list=["KXDEMOB", "KXDEMOA"], env="demo")


def curve(settle_times, tz, now):
    """pnl_curve dates for one winning $1-per-contract maker fill per settlement time."""
    fills = [fill(f"f{i}", t - timedelta(hours=12), ticker=f"KXDEMOB-26OCT10-B{i}", price=0.5, count=2)
             for i, t in enumerate(settle_times)]
    setts = [settle(f"KXDEMOB-26OCT10-B{i}", "no", t) for i, t in enumerate(settle_times)]
    s = an.summary(rows_of(fills, setts), [], [], days=7, env="demo", now=now, cells=CELLS, tz=tz)
    return s, {p["date"]: p["pnl"] for p in s["pnl_curve"]}


# ================================================================ 1. local dates
def test_curve_buckets_by_local_midnight_and_labels_the_zone():
    now = datetime(2026, 10, 28, 3, 34, tzinfo=UTC)                  # Oct 27, 10:34 PM in Chicago (CDT, UTC-5)
    settled_at = now - timedelta(minutes=30)                         # 03:04Z = 22:04 local Oct 27
    s, by = curve([settled_at], "America/Chicago", now)
    assert s["tz_used"] == "America/Chicago"
    assert s["pnl_curve"][-1]["date"] == "2026-10-27"                # the old UTC bucketing said Oct 28
    assert by["2026-10-27"] == 1.0
    s_utc, by_utc = curve([settled_at], None, now)
    assert s_utc["tz_used"] == "UTC" and s_utc["pnl_curve"][-1]["date"] == "2026-10-28"
    assert by_utc["2026-10-28"] == 1.0


def test_settlement_just_before_and_after_local_midnight_land_on_different_days():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    before = datetime(2026, 10, 28, 4, 59, 59, tzinfo=UTC)           # 23:59:59 Oct 27 local
    after = datetime(2026, 10, 28, 5, 0, 0, tzinfo=UTC)              # 00:00:00 Oct 28 local
    _, by = curve([before, after], "America/Chicago", now)
    assert by["2026-10-27"] == 1.0 and by["2026-10-28"] == 1.0
    _, by_utc = curve([before, after], "UTC", now)
    assert by_utc["2026-10-28"] == 2.0 and "2026-10-27" not in by_utc or by_utc.get("2026-10-27", 0) == 0


def test_daylight_time_change_keeps_each_day_whole():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    # US fall-back is Nov 1 2026 07:00 UTC: Nov 1 local is 25 hours, [05:00Z Nov 1, 06:00Z Nov 2)
    a = datetime(2026, 11, 1, 4, 59, tzinfo=UTC)       # Oct 31 local (CDT)
    b = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)        # Nov 1 local, first instant
    c = datetime(2026, 11, 2, 5, 59, tzinfo=UTC)       # Nov 1 local, last hour of the 25 h day (CST)
    d = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)        # Nov 2 local, first instant
    _, by = curve([a, b, c, d], "America/Chicago", now)
    assert by["2026-10-31"] == 1.0 and by["2026-11-01"] == 2.0 and by["2026-11-02"] == 1.0
    # spring forward (Mar 8 2026 08:00 UTC): Mar 8 local is 23 hours, [06:00Z, 05:00Z next day)
    now2 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    e = datetime(2026, 3, 9, 4, 59, tzinfo=UTC)        # Mar 8 local (CDT)
    f = datetime(2026, 3, 9, 5, 0, tzinfo=UTC)         # Mar 9 local
    _, by2 = curve([e, f], "America/Chicago", now2)
    assert by2["2026-03-08"] == 1.0 and by2["2026-03-09"] == 1.0


@pytest.mark.parametrize("bad", [None, "", "Mars/Base", "../../etc/hosts", "America/Chicago;DROP", "x" * 200,
                                 "UTC\x00", "America/Chi cago", 5])
def test_invalid_tz_falls_back_to_utc(bad):
    now = datetime(2026, 10, 28, 3, 34, tzinfo=UTC)
    s, _ = curve([now - timedelta(hours=2)], bad, now)
    assert s["tz_used"] == "UTC" and s["pnl_curve"][-1]["date"] == "2026-10-28"


@pytest.mark.asyncio
async def test_summary_route_echoes_tz_keys_the_cache_by_zone_and_bounds_it(env, monkeypatch):
    c, h = env.client, env.auth
    r = await c.get("/api/analytics/summary?tz=America/Chicago", headers=h)
    assert r.status_code == 200 and r.json()["tz_used"] == "America/Chicago"
    assert (await c.get("/api/analytics/summary?tz=Nope/Nope", headers=h)).json()["tz_used"] == "UTC"
    assert (await c.get("/api/analytics/summary", headers=h)).json()["tz_used"] == "UTC"
    cache = env.app.state.analytics_cache
    zones = {k[3] for k in cache}
    assert zones == {"America/Chicago", "UTC"}                       # the bad name was never a key
    assert all(isinstance(k, tuple) and len(k) == 4 for k in cache)
    monkeypatch.setattr(ra, "CACHE_MAX_KEYS", 3)
    for tz in ("Asia/Tokyo", "Europe/Paris", "Australia/Sydney", "America/Denver", "Asia/Kolkata"):
        assert (await c.get(f"/api/analytics/summary?tz={tz}", headers=h)).status_code == 200
    assert len(cache) <= 3                                           # bounded: the oldest entries are dropped
    assert "Asia/Kolkata" in {k[3] for k in cache}                  # the newest survives
    assert len(env.app.state.analytics["rows"]) <= ra.ROWS_CACHE_KEYS


@pytest.mark.asyncio
async def test_trade_rows_are_shared_across_zones(env):
    c, h = env.client, env.auth
    await c.get("/api/analytics/summary?tz=Asia/Tokyo", headers=h)
    st = env.app.state.analytics
    assert list(st["rows"]) == [("demo", 30, None)]                  # no zone in the rows key


# ================================================================ 2. win rate per strategy
def _arb_fixture(now, *, settle_all=True):
    """Four maker fills (3 win, 1 lose) and one 3-leg NO arbitrage set (2 legs win, 1 loses; the SET wins)."""
    t = now - timedelta(days=2)
    fills, setts = [], []
    for i, res in enumerate(["no", "no", "no", "yes"]):              # maker buys NO at 0.40
        tk = f"KXDEMOB-26OCT10-M{i}"
        fills.append(fill(f"m{i}", t + timedelta(minutes=i), ticker=tk, price=0.40, count=5))
        setts.append(settle(tk, res, now - timedelta(days=1)))
    legs, executed = [], {}
    for j, res in enumerate(["no", "no", "yes"]):                    # NO set: buy NO at 0.40 on each bracket
        tk = f"KXDEMOA-26OCT10-B{j}"
        fills.append(fill(f"a{j}", t + timedelta(hours=1), ticker=tk, price=0.40, count=2, taker=True))
        if settle_all:
            setts.append(settle(tk, res, now - timedelta(days=1)))
        legs.append([tk, "ask", 0.60, 2.0])
        executed[tk] = 2.0
    doc = {"event": "KXDEMOA-26OCT10", "kind": "NO", "contracts": 2.0, "profit": 1.6, "cost": 2.4, "legs": legs,
           "executed": f"FILLED {executed}", "mode": "demo-execute", "sig": "s1", "seen_count": 1,
           "ts": t + timedelta(hours=1, seconds=5)}
    return fills, setts, doc


def test_win_rate_is_per_strategy_and_never_pooled():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    fills, setts, doc = _arb_fixture(now)
    s = an.summary(rows_of(fills, setts), [], [doc], days=7, env="demo", now=now, cells=CELLS)
    w = s["win_rate_by_strategy"]
    assert w["maker"]["unit"] == "fill" and w["maker"]["n"] == 4 and w["maker"]["wins"] == 3
    assert w["maker"]["ci_low"] is not None and w["maker"]["method"] == "wilson95"
    assert w["arb"]["unit"] == "set" and w["arb"]["n"] == 1 and w["arb"]["wins"] == 1 and w["arb"]["win_rate"] == 1.0
    assert s["totals"]["win_rate"] == pytest.approx(5 / 7, abs=1e-3)   # the old pooled figure (legs) is kept ...
    assert "totals.win_rate" in s["deprecated"]                        # ... and marked deprecated
    assert w["arb"]["win_rate"] != s["totals"]["win_rate"] != w["maker"]["win_rate"]


def test_win_rate_arb_counts_a_set_only_when_all_its_legs_settled():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    fills, setts, doc = _arb_fixture(now, settle_all=False)
    s = an.summary(rows_of(fills, setts), [], [doc], days=7, env="demo", now=now, cells=CELLS)
    assert s["win_rate_by_strategy"]["arb"] == {"unit": "set", "n": 0, "wins": 0, "losses": 0, "win_rate": None,
                                                "ci_low": None, "ci_high": None, "method": "wilson95"}
    assert s["arb_sets"][0]["status"] == "FILLED" and s["arb_sets"][0]["realized_pnl"] is None


# ================================================================ 3/4. arbitrage counters and sets
def test_counters_are_distinct_sets_by_gate_outcome_not_sightings_of_the_scan_counter():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)         # 07:00 Chicago; local midnight = 05:00Z
    today = now - timedelta(hours=3)
    yesterday = now - timedelta(days=1)

    def d(sig, executed, ts, seen=1, mode="log-only", event="E1"):
        return {"event": event, "kind": "NO", "sig": sig, "executed": executed, "ts": ts, "seen_count": seen,
                "mode": mode, "legs": [], "profit": 1.0, "cost": 5.0, "contracts": 1.0}
    docs = [d("a", "log-only", today, seen=9), d("b", "log-only", today, seen=4, event="E2"),
            d("c", "skipped: gate total exposure 75.12 + 24.00 > ceiling 79.00", today, mode="demo-execute"),
            d("e", "skipped: gate total exposure 60.00 + 30.00 > ceiling 79.00", yesterday, mode="demo-execute"),
            d("f", "skipped: gate series cap KXDEMOB 5.00 + 9.00 > 7.00", yesterday, mode="demo-execute"),
            d("g", "FILLED {'T': 1.0}", yesterday, mode="demo-execute"),
            d("h", "PARTIAL {'T': 0.0}", yesterday, mode="demo-execute"),
            d("i", "skipped: gate total exposure 1 + 2 > ceiling 3", today, mode="live")]   # a live doc, demo env
    zone, name = pnlsvc.resolve_tz("America/Chicago")
    out = arbsets.summary(docs, [], env="demo", now=now, zone=zone, tzname=name, since=now - timedelta(days=7))
    assert out["today"]["since"] == "2026-10-30T05:00:00+00:00"
    assert out["today"]["sets"] == 3 and out["today"]["sightings"] == 9 + 4 + 1 and out["today"]["events"] == 2
    p = out["period"]
    assert p["sets"] == 7 and p["by_outcome"] == {"filled": 1, "partial": 1, "error": 0, "skipped": 3, "log_only": 2}
    assert p["skipped_by_rule"] == {"total_exposure_ceiling": 2, "series_cap": 1}   # numbers do not split a rule
    assert arbsets.skip_rule("skipped: cost 12.00 > cap 10.00") == "set_cost_cap"
    assert arbsets.skip_rule("skipped: gate opposite set held on event") == "opposite_set"
    live = arbsets.counters(docs, env="live", since=None)
    assert live["by_outcome"]["skipped"] == 1 and live["by_outcome"]["log_only"] == 2   # log-only is env-agnostic


@pytest.mark.asyncio
async def test_arb_page_summary_comes_from_the_stored_log_with_since_restart_labelled(env):
    now = datetime.now(UTC)
    for i, ex in enumerate(["log-only", "log-only", "skipped: gate day cap 40.00 + 9.00 > 45.00"]):
        await env.store.add_arb_opportunity({"event": f"EV{i}", "kind": "NO", "contracts": 1, "profit": 1.0,
                                             "cost": 3.0, "legs": [], "executed": ex, "mode": "demo-execute"
                                             if ex.startswith("skipped") else "log-only", "ts": now})
    env.runner.arb_scans, env.runner.arb_found = 1384, 571
    r = await env.client.get("/api/arb?tz=America/Chicago", headers=env.auth)
    assert r.status_code == 200
    a = r.json()["arb_summary"]
    assert a["tz"] == "America/Chicago" and a["period"]["sets"] == 3
    assert a["period"]["skipped_by_rule"] == {"day_cap": 1}
    sr = a["since_restart"]
    assert sr["scans"] == 1384 and sr["sightings"] == 571 and "restart" in sr["label"] and sr["since_utc"]
    env.runner.arb_scans = 0                                         # a restart resets ONLY the raw counters
    again = (await env.client.get("/api/arb", headers=env.auth)).json()["arb_summary"]
    assert again["period"]["sets"] == 3 and again["since_restart"]["scans"] == 0


def test_grouped_sets_status_and_realized_pnl():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    fills, setts, doc = _arb_fixture(now, settle_all=False)
    rows = rows_of(fills, setts)
    [row] = arbsets.build_sets([doc], rows, env="demo")
    assert row["event"] == "KXDEMOA-26OCT10" and row["kind"] == "NO" and row["mode"] == "demo-execute"
    assert row["status"] == "FILLED" and row["complete"] is True and row["realized_pnl"] is None
    assert row["guaranteed_profit"] == 1.6 and len(row["legs"]) == 3
    leg = row["legs"][0]
    assert {"ticker", "side", "price", "contracts", "filled", "settled", "result", "pnl"} <= set(leg)
    assert leg["side"] == "no" and leg["price"] == 0.4 and leg["filled"] == 2.0 and leg["settled"] is False
    # one leg settles -> OPEN; all settle -> SETTLED with the set's realized P&L
    one = rows_of(fills, [settle("KXDEMOA-26OCT10-B0", "no", now)])
    assert arbsets.build_sets([doc], one, env="demo")[0]["status"] == "OPEN"
    fills2, setts2, _ = _arb_fixture(now)
    [done] = arbsets.build_sets([doc], rows_of(fills2, setts2), env="demo")
    assert done["status"] == "SETTLED" and done["realized_pnl"] == pytest.approx(1.6)
    assert [lg_["result"] for lg_ in done["legs"]] == ["win", "win", "loss"] and done["settled_utc"]


def test_partial_set_and_unmatched_fills():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    _fills, _setts, doc = _arb_fixture(now)
    part = {**doc, "executed": "PARTIAL {'KXDEMOA-26OCT10-B0': 2.0, 'KXDEMOA-26OCT10-B1': 0.0}"}
    [row] = arbsets.build_sets([part], [], env="demo")
    assert row["status"] == "PARTIAL" and row["complete"] is False and row["outcome"] == "partial"
    assert row["legs"][0]["filled"] == 2.0 and row["legs"][1]["filled"] == 0.0
    assert any("no stored fill matched" in n for n in row["notes"])          # said, not invented
    assert arbsets.parse_filled("FILLED {'A': 1.0} planned profit 2") == {"A": 1.0}
    assert arbsets.parse_filled("FILLED {__import__('os').system('x'): 1}") == {}   # literals only
    assert arbsets.parse_filled("ERROR TypeError") == {}
    other = fill("zz", now, ticker="KXDEMOA-26OCT10-B0", side="no", taker=True)      # an hour away: not this set
    other["created_time"] = z(now - timedelta(days=3))
    assert arbsets.build_sets([doc], rows_of([other], []), env="demo")[0]["legs"][0]["settled"] is None


# ================================================================ 5. ledger + reconciliation
def _recon_inputs(end_cash_delta=0.0):
    d0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    f = fill("m1", d0 + timedelta(days=2), ticker="KXDEMOB-26OCT22-B70", price=0.40, count=10, fee=0.10)
    rows = rows_of([f], [settle("KXDEMOB-26OCT22-B70", "no", d0 + timedelta(days=3))])
    snaps = [{"ts": d0, "cash": 100.0, "exposure": 0.0},
             {"ts": d0 + timedelta(days=4), "cash": 100.0 + 50.0 + 6.0 - 0.10 + end_cash_delta, "exposure": 0.0}]
    flows = [{"ts": d0 + timedelta(days=1), "kind": "deposit", "amount": 50.0}]
    return d0, rows, snaps, flows


def test_reconciliation_closes_within_tolerance_and_flags_a_gap():
    d0, rows, snaps, flows = _recon_inputs()
    now = d0 + timedelta(days=5)
    rec = lg.reconciliation(rows, snaps, flows, since=d0, now=now)
    assert rec["tolerance"] == 0.10 and rec["residual"] == 0.0 and rec["flag"] is False
    assert rec["net_flows"] == 50.0 and rec["realized_pnl"] == 5.9 and rec["fee_timing"] == 0.0
    assert rec["equity_change"] == 55.9 and rec["expected_equity_change"] == 55.9 and rec["start_exact"] is True
    d0, rows, snaps, flows = _recon_inputs(end_cash_delta=-0.5)
    bad = lg.reconciliation(rows, snaps, flows, since=d0, now=now)
    assert bad["residual"] == -0.5 and bad["flag"] is True
    d0, rows, snaps, flows = _recon_inputs(end_cash_delta=-0.05)
    assert lg.reconciliation(rows, snaps, flows, since=d0, now=now)["flag"] is False   # inside the tolerance


def test_reconciliation_without_data_is_null_with_notes_never_invented():
    d0, rows, snaps, flows = _recon_inputs()
    rec = lg.reconciliation(rows, snaps[:1], flows, since=d0, now=d0 + timedelta(days=5))
    assert rec["residual"] is None and rec["flag"] is None and rec["equity_change"] is None
    assert rec["tolerance"] == 0.10 and rec["notes"]
    none = lg.reconciliation([], [], [], since=d0, now=d0)
    assert none["residual"] is None and none["flag"] is None and none["notes"]
    late = lg.reconciliation(rows, snaps, flows, since=d0 - timedelta(days=9), now=d0 + timedelta(days=5))
    assert late["start_exact"] is False and any("first stored snapshot" in n for n in late["notes"])


def test_ledger_rows_are_by_local_fill_day_and_fills_net_to_settlements():
    d0, rows, snaps, flows = _recon_inputs()
    open_f = fill("o1", datetime(2026, 10, 28, 3, 30, tzinfo=UTC), ticker="KXDEMOB-26OCT29-B70", price=0.5, count=4)
    allr = rows + rows_of([open_f], [])
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    out = lg.build(allr, [], zone=CHI, tzname="America/Chicago", since=d0, until=None, snapshots=snaps, flows=flows,
                   now=now)
    m = next(s for s in out["strategies"] if s["strategy"] == "maker")
    assert (m["fills"], m["settled_fills"], m["wins"], m["losses"], m["open_fills"]) == (2, 1, 1, 0, 1)
    assert m["cost"] == 6.0 and m["settled_cost"] == 4.0 and m["open_cost"] == 2.0 and m["realized_pnl"] == 5.9
    assert out["total"]["fills"] == 2 and out["tz_used"] == "America/Chicago"
    days = {(r["date"], r["strategy"]): r for r in out["daily"]}
    assert ("2026-10-27", "maker") in days                           # 03:30Z is Oct 27 in Chicago
    assert days[("2026-10-27", "maker")]["open_fills"] == 1
    assert out["daily_by_settlement"][0]["realized_pnl"] == 5.9
    assert out["reconciliation"]["tolerance"] == 0.10
    # the same ledger in UTC puts the open fill on Oct 28
    utc_out = lg.build(allr, [], zone=pnlsvc.resolve_tz("UTC")[0], tzname="UTC", since=d0, until=None,
                       snapshots=snaps, flows=flows, now=now)
    assert ("2026-10-28", "maker") in {(r["date"], r["strategy"]) for r in utc_out["daily"]}


def test_ledger_pair_netting_is_informational_and_fifo():
    t = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
    rs = rows_of([fill("y1", t, ticker="KXDEMOB-26OCT29-B1", side="yes", price=0.60, count=4, taker=True),
                  fill("n1", t + timedelta(minutes=1), ticker="KXDEMOB-26OCT29-B1", side="no", price=0.30,
                       count=1, taker=True),
                  fill("n2", t + timedelta(minutes=2), ticker="KXDEMOB-26OCT29-B1", side="no", price=0.35,
                       count=2, taker=True)], [])
    net = lg.netting([r for r in rs])
    assert net["pairs"] == 3.0 and net["tickers"] == 1 and net["verified"] is False
    assert net["pair_cost"] == pytest.approx(3 * 0.60 + 0.30 + 2 * 0.35)
    assert net["pair_gain"] == pytest.approx(3 - net["pair_cost"], abs=0.01)


@pytest.mark.asyncio
async def test_ledger_route_is_read_only_signed_in_no_2fa(env):
    now = datetime.now(UTC)
    store = env.store
    await store.upsert_fills([{**fill("L1", now - timedelta(days=3), ticker="KXDEMOB-26OCT10-B70"),
                               "ts": now - timedelta(days=3)}])
    await store.upsert_settlements([{"ticker": "KXDEMOB-26OCT10-B70", "market_result": "no",
                                     "ts": now - timedelta(days=2)}])
    await store.add_account_snapshot({"ts": now - timedelta(days=5), "cash": 100.0, "exposure": 0.0, "headroom": 50})
    await store.add_account_snapshot({"ts": now - timedelta(days=1), "cash": 106.0, "exposure": 0.0, "headroom": 50})
    assert (await env.client.get("/api/analytics/ledger")).status_code in (401, 403)
    before = (len(await store.list_fills()), len(await store.list_cash_flows()))
    r = await env.client.get("/api/analytics/ledger?days=30&tz=America/Chicago", headers=env.auth)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["tz_used"] == "America/Chicago" and b["total"]["fills"] == 1 and b["total"]["realized_pnl"] == 6.0
    assert b["reconciliation"]["tolerance"] == 0.10 and b["reconciliation"]["residual"] == 0.0
    assert b["reconciliation"]["flag"] is False and "netting" in b and "daily" in b
    assert (len(await store.list_fills()), len(await store.list_cash_flows())) == before
    assert (await env.client.get("/api/analytics/ledger?days=9", headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/analytics/ledger?tz=Bad/Zone", headers=env.auth)).json()["tz_used"] == "UTC"


# ================================================================ 6. scorecard
def test_scorecard_missing_metrics_are_null_with_notes():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    rec = lg.reconciliation([], [], [], since=now - timedelta(days=7), now=now)
    sc = lg.scorecard(env="demo", rows=[], sets=[], arb_counts={}, heartbeats=[], journal=[], alerts=[],
                      audit_rows=[], reconciliation_obj=rec, since=now - timedelta(days=7), until=None, now=now,
                      label="last 7 days")
    assert sc["passes"]["ok_pct"] is None and sc["churn"]["cancels_per_quote"] is None
    assert sc["fill_to_settlement"]["rate"] is None and sc["return"]["roi"] is None
    assert sc["order_errors"]["value"] is None and sc["cap_bindings"]["maker"] is None
    assert sc["reconciliation"]["flag"] is None and sc["return"]["label"] == "demo, indicative only"
    assert sc["return"]["sample_settled_fills"] == 0 and len(sc["notes"]) >= 4


def test_scorecard_counts_from_stored_data():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    old_open = fill("o", now - timedelta(days=4), ticker="KXDEMOB-26OCT10-B70", price=0.5, count=2,
                    close_time=z(now - timedelta(days=3)))
    won = fill("w", now - timedelta(days=4), ticker="KXDEMOB-26OCT10-B71", price=0.5, count=2,
               close_time=z(now - timedelta(days=3)))
    rows = rows_of([old_open, won], [settle("KXDEMOB-26OCT10-B71", "no", now - timedelta(days=2))])
    hbs = [{"ts": now, "ok": True, "duration_s": 5.0}] * 3 + [{"ts": now, "ok": False, "duration_s": 9.0}]
    jr = [{"event": "quote"}] * 10 + [{"event": "cancel"}] * 4
    alerts = [{"type": "error", "ts": now}, {"type": "fill", "ts": now}, {"type": "fill", "ts": now}]
    sets = [{"outcome": "partial", "status": "PARTIAL", "realized_pnl": None, "guaranteed_profit": 1.0},
            {"outcome": "filled", "status": "SETTLED", "realized_pnl": 0.5, "guaranteed_profit": 0.6}]
    counts = {"sets": 9, "by_outcome": {"skipped": 3, "log_only": 4}, "skipped_by_rule": {"day_cap": 3}}
    rec = lg.reconciliation([], [], [], since=now, now=now)
    sc = lg.scorecard(env="demo", rows=rows, sets=sets, arb_counts=counts, heartbeats=hbs, journal=jr, alerts=alerts,
                      audit_rows=[{"action": "bot.stop", "ok": True}, {"action": "bot.start", "ok": True}],
                      reconciliation_obj=rec, since=now - timedelta(days=7), until=None, now=now, label="x")
    assert sc["passes"] == {"n": 4, "ok": 3, "ok_pct": 75.0, "errors": 1, "median_duration_s": 5.0,
                            "max_duration_s": 9.0}
    assert sc["churn"] == {"quotes": 10, "cancels": 4, "cancels_per_quote": 0.4}
    assert sc["open_past_expected_settlement"]["count"] == 1
    assert sc["fill_to_settlement"]["due_fills"] == 2 and sc["fill_to_settlement"]["rate"] == 0.5
    assert sc["alerts_by_type"] == {"error": 1, "fill": 2} and sc["order_errors"]["error_alerts"] == 1
    a = sc["arbitrage_per_set"]
    assert (a["legged"], a["complete"], a["skipped_by_gate"], a["log_only"], a["set_wins"]) == (1, 1, 3, 4, 1)
    assert sc["stop_start_log"] == {"stops": 1, "stop_failures": 0, "starts": 1}


@pytest.mark.asyncio
async def test_scorecard_route(env):
    now = datetime.now(UTC)
    for i, ok in enumerate([True, True, True, False]):
        await env.store.add_heartbeat({"ts": now - timedelta(minutes=10 * i), "ok": ok, "duration_s": 4.0})
    assert (await env.client.get("/api/analytics/scorecard")).status_code in (401, 403)
    r = await env.client.get("/api/analytics/scorecard?days=7", headers=env.auth)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["passes"]["ok_pct"] == 75.0 and b["return"]["label"] == "demo, indicative only"
    assert b["churn"]["cancels_per_quote"] is None and b["notes"]
    assert (await env.client.get("/api/analytics/scorecard?period=nope", headers=env.auth)).status_code == 422


# ================================================================ 7. cash-flow guard rails
NOW = datetime.now(UTC)


@pytest.mark.asyncio
async def test_cash_flow_requires_a_date_and_rejects_the_future_with_422(env):
    c = env.client
    r = await c.post("/api/cash-flows", json={"kind": "deposit", "amount": 98.2}, headers=await env.h2fa())
    assert r.status_code == 422 and any("ts: required" in m for m in r.json()["detail"])
    fut = (NOW + timedelta(days=2)).isoformat()
    r = await c.post("/api/cash-flows", json={"kind": "deposit", "amount": 5, "ts": fut}, headers=await env.h2fa())
    assert r.status_code == 422 and any("future" in m for m in r.json()["detail"])
    r = await c.post("/api/cash-flows", json={"kind": "deposit", "amount": -3, "ts": "2026-10-17"},
                     headers=await env.h2fa())
    assert r.status_code == 422 and any("amount" in m for m in r.json()["detail"])
    assert await env.store.list_cash_flows() == []
    assert (await c.post("/api/cash-flows", json={"kind": "deposit", "amount": 5, "ts": "2026-10-17"},
                         headers=env.auth)).status_code == 403          # 2FA is still required


@pytest.mark.asyncio
async def test_cash_flow_duplicate_needs_confirmation_and_warnings_come_back(env):
    c = env.client
    body = {"kind": "deposit", "amount": 98.2, "ts": "2026-10-17"}
    ok = await c.post("/api/cash-flows", json=body, headers=await env.h2fa())
    assert ok.status_code == 200 and ok.json()["created"] and isinstance(ok.json()["warnings"], list)
    dup = await c.post("/api/cash-flows", json=body, headers=await env.h2fa())
    assert dup.status_code == 409 and "duplicate" in dup.json()["detail"][0]
    assert len(await env.store.list_cash_flows()) == 1
    other_day = await c.post("/api/cash-flows", json={**body, "ts": "2026-10-19"}, headers=await env.h2fa())
    assert other_day.status_code == 200                                    # a different date is not a duplicate
    other_kind = await c.post("/api/cash-flows", json={**body, "kind": "withdrawal"}, headers=await env.h2fa())
    assert other_kind.status_code == 200
    forced = await c.post("/api/cash-flows", json={**body, "confirm_duplicate": True}, headers=await env.h2fa())
    assert forced.status_code == 200 and len(await env.store.list_cash_flows()) == 4
    acts = [(a["action"], a["ok"], (a["details"] or {}).get("error")) for a in await env.store.list_audit(limit=50)
            if a["action"] == "cash_flow.create"]
    assert ("cash_flow.create", False, "duplicate") in acts


@pytest.mark.asyncio
async def test_cash_flow_inside_the_current_period_warns_and_no_balance_jump_warns(env):
    c = env.client
    await c.post("/api/periods", json={"name": "Two"}, headers=await env.h2fa())
    since = datetime.now(UTC) - timedelta(hours=1)
    await env.store.add_account_snapshot({"ts": since - timedelta(hours=3), "cash": 100.0, "exposure": 0.0})
    await env.store.add_account_snapshot({"ts": since + timedelta(hours=2), "cash": 100.0, "exposure": 0.0})
    r = await c.post("/api/cash-flows", json={"kind": "deposit", "amount": 50, "ts": datetime.now(UTC).isoformat()},
                     headers=await env.h2fa())
    assert r.status_code == 200
    codes = {w["code"] for w in r.json()["warnings"]}
    assert "date_in_current_period" in codes and "no_balance_jump" in codes
    assert all(w["message"] for w in r.json()["warnings"])


# ================================================================ 8. shadow_capital
@pytest.mark.asyncio
async def test_shadow_capital_bounds_fresh_2fa_atomic_and_audited(env):
    c = env.client
    for bad in (49.99, 1000.01, 0, -1, True, "300", 1e12):
        r = await c.put("/api/cloud", json={"shadow_capital": bad, "keepalive_minutes": 9}, headers=await env.h2fa())
        assert r.status_code == 400, (bad, r.text)
    cloud = await env.store.get_cloud_settings() or {}
    assert cloud.get("shadow_capital") in (None, 150) and cloud.get("keepalive_minutes") in (None, 10)   # all or nothing
    assert env.runner.cloud.get("keepalive_minutes") != 9
    no2fa = await c.put("/api/cloud", json={"shadow_capital": 300}, headers=env.auth)
    assert no2fa.status_code == 403
    assert (await env.store.get_cloud_settings() or {}).get("shadow_capital") in (None, 150)
    for good in (50, 1000, 300.5):
        r = await c.put("/api/cloud", json={"shadow_capital": good}, headers=await env.h2fa())
        assert r.status_code == 200 and r.json()["settings"]["shadow_capital"] == good
    from control_api.services import shadow as sh
    assert sh.capital_of(env.runner.cloud) == 300.5                      # the shadow service reads the setting
    assert (await env.store.get_cloud_settings())["shadow_capital"] == 300.5
    rows = [a for a in await env.store.list_audit(limit=100) if a["action"] == "cloud.update" and a["ok"]]
    changes = [a["details"].get("changed", {}).get("shadow_capital") for a in rows]
    assert {"from": 1000, "to": 300.5} in changes and {"from": None, "to": 50} in changes or \
        {"from": 150, "to": 50} in changes
    assert not any(json.dumps(a["details"]).find("password") >= 0 for a in rows)


@pytest.mark.asyncio
async def test_shadow_capital_is_in_the_settings_response_and_status_keeps_ceiling_full(env):
    r = await env.client.get("/api/config", headers=env.auth)
    f = r.json()["shadow_capital"]
    assert (f["value"], f["default"], f["min"], f["max"]) == (150, 150, 50, 1000)
    assert f["needs_fresh_2fa"] is True and f["write_via"] == "PUT /api/cloud" and f["help"]
    await env.client.put("/api/cloud", json={"shadow_capital": 400}, headers=await env.h2fa())
    assert (await env.client.get("/api/config", headers=env.auth)).json()["shadow_capital"]["value"] == 400
    st = (await env.client.get("/api/shadow/summary", headers=env.auth)).json()
    assert "ceiling_full" in json.dumps(st) or "status" in st


# ================================================================ 9. today's P&L state
@pytest.mark.asyncio
async def test_pnl_today_state_none_yet_then_settled(env):
    r = await env.client.get("/api/status?tz=America/Chicago", headers=env.auth)
    b = r.json()
    assert b["pnl_today_state"] == "none_yet" and b["pnl_today_tz"] == "America/Chicago"
    assert b["pnl_today"]["total"] is None and b["pnl_today"] is not None
    now = datetime.now(UTC)
    f = fill("T1", now - timedelta(hours=2), ticker="KXDEMOB-26OCT10-B70", price=0.4, count=5)
    await env.store.upsert_fills([{**f, "ts": now - timedelta(hours=2)}])
    await env.store.upsert_settlements([{"ticker": "KXDEMOB-26OCT10-B70", "market_result": "no", "ts": now}])
    env.runner._pnl_today.clear()
    b2 = (await env.client.get("/api/status?tz=UTC", headers=env.auth)).json()
    assert b2["pnl_today_state"] == "settled" and b2["pnl_today"]["total"] == 3.0
    bad = (await env.client.get("/api/status?tz=Not/AZone", headers=env.auth)).json()
    assert bad["pnl_today_tz"] == "UTC"


@pytest.mark.asyncio
async def test_pnl_today_state_is_unavailable_when_the_store_read_fails(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_settlements", boom)
    b = (await env.client.get("/api/status", headers=env.auth)).json()
    assert b["pnl_today_state"] == "unavailable"


# ================================================================ Stop is untouched
@pytest.mark.asyncio
async def test_stop_still_works_with_the_new_reads_broken(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("atlas down")
    for name in ("list_arb_opportunities", "list_account_snapshots", "list_cash_flows", "list_heartbeats",
                 "list_alerts", "list_settlements", "list_fills"):
        monkeypatch.setattr(env.store, name, boom)
    env.runner.desired = "running"
    r = await env.client.post("/api/bot/stop", headers=env.auth)            # no 2FA header
    assert r.status_code == 200 and env.runner.desired == "stopped"
    assert (await env.client.get("/api/analytics/ledger", headers=env.auth)).status_code == 503
    assert (await env.client.get("/api/arb", headers=env.auth)).status_code == 200   # the page still answers


# ---- settlement-date attribution per strategy (Home rows must add up to the settlement-date headline)
def _settle_case():
    since = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    until = since + timedelta(days=7)
    now = until + timedelta(days=1)
    spec = [  # (id, ticker suffix, filled at, settled at, taker)
        ("early_m", "B71", since - timedelta(days=3), since + timedelta(days=1), False),   # filled before, settled in
        ("in_m", "B72", since + timedelta(days=1), since + timedelta(days=2), False),
        ("early_a", "B73", since - timedelta(days=2), since + timedelta(days=3), True),    # arb, filled before
        ("in_a", "B74", since + timedelta(days=2), since + timedelta(days=4), True),
        ("at_since", "B75", since - timedelta(days=4), since, False),                      # boundary: included
        ("at_until", "B76", since + timedelta(days=3), until, False),                      # boundary: excluded
        ("after_now", "B77", since + timedelta(days=3), now + timedelta(hours=1), False),  # not settled yet
    ]
    fills = [fill(i, f, ticker=f"KXDEMOB-26OCT22-{t}", price=0.4, count=10, taker=tk) for i, t, f, _, tk in
             [(a, b, c, d, e) for a, b, c, d, e in spec]]
    setts = [settle(f"KXDEMOB-26OCT22-{t}", "no", s) for _, t, _, s, _ in spec]
    allr = rows_of(fills, setts)
    in_window = [r for r in allr if r["_ts"] >= since and r["_ts"] < until]
    return since, until, now, allr, in_window


def test_settled_by_strategy_counts_rows_filled_before_the_window_and_matches_the_sum():
    since, until, now, allr, in_window = _settle_case()
    out = lg.build(in_window, [], zone=CHI, tzname="America/Chicago", since=since, until=until, snapshots=[],
                   flows=[], now=now, all_rows=allr)
    by_fill = {s["strategy"]: s for s in out["strategies"]}
    sb = {s["strategy"]: s for s in out["settled_by_strategy"]}
    expect = [r for r in allr if r["_settled"] is not None and since <= r["_settled"] <= now and r["_settled"] < until]
    assert {r["fill_id"] for r in expect} >= {"early_m", "early_a", "at_since"} and "at_until" not in {
        r["fill_id"] for r in expect} and "after_now" not in {r["fill_id"] for r in expect}
    m_exp = round(sum(r["_pnl"] for r in expect if r["fill_id"] in ("early_m", "in_m", "at_since")), 2)
    a_exp = round(sum(r["_pnl"] for r in expect if r["fill_id"] in ("early_a", "in_a")), 2)
    assert sb["maker"]["realized_pnl"] == m_exp and sb["maker"]["settled_fills"] == 3
    assert sb["arb"]["realized_pnl"] == a_exp and sb["arb"]["settled_fills"] == 2
    assert sb["total"]["realized_pnl"] == round(sum(r["_pnl"] for r in expect), 2)
    assert sb["total"]["settled_fills"] == len(expect) == 5
    # the fill-date table leaves out the fills made before the window, so it differs; that is the old bug
    assert by_fill["arb"]["settled_fills"] == 1 and by_fill["arb"]["realized_pnl"] != sb["arb"]["realized_pnl"]
    assert out["settled_attribution"].startswith("settlement date") and out["attribution"].startswith("fill date")
    assert out["daily_by_settlement"] and round(sum(d["realized_pnl"] for d in out["daily_by_settlement"]), 2) == \
        sb["total"]["realized_pnl"]


def test_settled_by_strategy_has_maker_and_arb_when_nothing_settled_and_no_until_bound():
    since, _, now, allr, _ = _settle_case()
    empty = lg.build([], [], zone=CHI, tzname="America/Chicago", since=since, until=None, snapshots=[], flows=[],
                     now=since)
    sb = {s["strategy"]: s for s in empty["settled_by_strategy"]}
    assert sb["maker"]["realized_pnl"] == 0.0 and sb["arb"]["settled_fills"] == 0 and sb["total"]["realized_pnl"] == 0.0
    open_ended = lg.build(allr, [], zone=CHI, tzname="America/Chicago", since=since, until=None, snapshots=[],
                          flows=[], now=now)
    ids = open_ended["settled_by_strategy"][-1]["settled_fills"]
    assert ids == 6   # at_until now counts (no upper bound); after_now is still in the future

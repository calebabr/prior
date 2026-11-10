"""Analytics API (docs/ANALYTICS_CONTRACT.md): math, cell matching, filters, pagination, env, auth, validation."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from control_api.db import MemoryStore
from control_api.db.seed import seed
from control_api.services import analytics as an
from control_api.services import botfiles

NOW = datetime.now(timezone.utc).replace(microsecond=0)
from control_api.tests.fake_cells import TEST_CELLS as CELLS  # noqa: E402  (synthetic, not a strategy)


@pytest_asyncio.fixture
async def env(env):
    """API tests read the synthetic CELLS (not the installed strategy), so they pass with the stand-in and overlay."""
    env.runner.config = {**env.runner.config, "cells": [list(c) for c in CELLS]}
    return env

SUMMARY_KEYS = {"env", "days", "generated_utc", "truncated", "totals", "pnl_curve", "by_cell", "by_series",
                "by_band", "by_ttc", "fill_quality", "arb", "expectations", "calibration", "fill_rate",
                "tz_used", "win_rate_by_strategy", "deprecated", "arb_summary", "arb_sets", "arb_sets_total"}
TOTAL_KEYS = {"settled_fills", "contracts", "cost", "pnl", "roi", "win_rate", "open_fills", "max_drawdown"}
GROUP_KEYS = {"key", "n", "wins", "contracts", "cost", "pnl", "roi", "avg_price", "win_rate", "win_ci_low",
              "win_ci_high", "edge", "edge_ci_low", "edge_ci_high", "cluster_ci_low", "cluster_ci_high",
              "edge_cluster_ci_low", "edge_cluster_ci_high", "roi_ci_low", "roi_ci_high"}
TRADE_KEYS = {"fill_id", "ts", "ticker", "series", "side", "kind", "price", "count", "fee", "cost", "band",
              "ttc_hours", "ttc_band", "cell", "status", "result", "settled_utc", "pnl", "fill_quality",
              "source", "strategy"}      # UX sprint Y1 (additive)


def _z(d):
    return d.isoformat().replace("+00:00", "Z")


def fill(fid, *, ts, ticker="KXDEMOB-26OCT10-B70", side="no", price=0.35, count=10, taker=False, env="demo",
         fee=0.0, **extra):
    return {"fill_id": fid, "order_id": f"o-{fid}", "ticker": ticker, "side": side, "count": count,
            "yes_price_dollars": f"{(1 - price) if side == 'no' else price:.4f}",
            "no_price_dollars": f"{price if side == 'no' else (1 - price):.4f}", "fee_cost": f"{fee:.4f}",
            "is_taker": taker, "env": env, "created_time": _z(ts), **extra}


def settle(ticker, result, ts, close=None):
    d = {"ticker": ticker, "market_result": result, "settled_time": _z(ts)}
    if close is not None:
        d["close_time"] = _z(close)
    return d


# ---------------------------------------------------------------- pure math
def test_wilson_known_values():
    lo, hi = an.wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    assert an.wilson(0, 0) == (None, None)
    lo, hi = an.wilson(10, 10)
    assert hi == pytest.approx(1.0) and 0.69 < lo < 0.73
    lo, hi = an.wilson(0, 5)
    assert lo == pytest.approx(0.0) and 0 < hi < 0.5


def test_drawdown_starts_at_zero_peak_and_never_positive():
    assert an.drawdown([1, 3, 2, 5, 1]) == [0, 0, -1, 0, -4]
    assert an.drawdown([-2, -1, -3]) == [-2, -1, -3]          # losing from the start is a drawdown
    assert an.drawdown([]) == []


def test_bands():
    bands = an.price_bands(CELLS)                     # derived from the cells: [lo, hi) each, the top one inclusive
    assert bands == [(30, 33, "0.30-0.33"), (34, 37, "0.34-0.37"), (38, 44, "0.38-0.44"), (45, 49, "0.45-0.49")]
    assert [an.price_band(p, bands) for p in (0.34, 0.37, 0.374, 0.38, 0.44, 0.45, 0.49, 0.29, 1.0, None)] == \
        ["0.34-0.37", "0.34-0.37", "0.34-0.37", "0.38-0.44", "0.38-0.44", "0.45-0.49", "0.45-0.49",
         "other", "other", "other"]
    assert an.price_band(0.35) == "other" and an.price_bands([]) == [] and an.price_bands(None) == []
    assert an.band_labels(CELLS + [[0.34, 0.38, 0, 2, "yes"]]) == ("0.30-0.33", "0.34-0.37", "0.38-0.44", "0.45-0.49")
    assert an.price_bands([[0.4, 0.5, 0, 1, "no"], [0.6, 0.7, 0, 1, "no"]]) == \
        [(40, 49, "0.40-0.49"), (60, 70, "0.60-0.70")]                # a gap between ranges stays "other"
    tb = an.ttc_bands(CELLS)                          # hour edges come from the cells too (0, 3, 5, 11)
    assert [b[2] for b in tb] == ["0-3h", "3-5h", "5-11h", ">11h"]
    assert [an.ttc_band(h, tb) for h in (-1, 0, 2.99, 3, 4.9, 5, 10.99, 11, 500, None)] == \
        ["0-3h", "0-3h", "0-3h", "3-5h", "3-5h", "5-11h", "5-11h", ">11h", ">11h", None]
    assert an.ttc_labels([]) == (">0h",) and an.ttc_band(7) == ">0h"
    assert an.ttc_labels([[0.1, 0.2, 1, 4, "no"], [0.1, 0.2, 8, 16, "yes"]]) == ("0-1h", "1-4h", "4-8h", "8-16h", ">16h")


def test_cell_id_and_matching_mirror_the_strategy():
    cells = an.parse_cells(CELLS + [["bad"], [0.1, 0.2, 0, 1, "maybe"]])
    assert len(cells) == 4
    assert an.cell_id(cells[0]) == "0.34-0.38|5-11h|no"
    assert an.cell_id([0.4, 0.55, 0.5, 2, "yes"]) == "0.40-0.55|0.5-2h|yes"
    assert an.match_cell(0.35, 8, "no", cells) == cells[0]
    assert an.match_cell(0.38, 8, "no", cells) == cells[0]          # strategy: price < hi + 1e-9 -> first match
    assert an.match_cell(0.43, 8, "no", cells) == cells[1]
    assert an.match_cell(0.49, 1, "yes", cells) == cells[2]
    assert an.match_cell(0.35, 12, "no", cells) is None              # hours upper edge is exclusive
    assert an.match_cell(0.35, 8, "yes", cells) is None
    assert an.match_cell(None, 8, "no", cells) is None


def test_cells_are_only_read():
    cells = [list(c) for c in CELLS]
    before = json.dumps(cells)
    an.trade_rows([fill("a", ts=NOW - timedelta(hours=20))], [], [], cells=cells, series_list=[], env="demo")
    an.summary([], [], [], days=30, env="demo", now=NOW, cells=cells)
    assert json.dumps(cells) == before


def test_pnl_cost_cell_and_ttc_on_a_settled_fill():
    t = NOW - timedelta(days=2)
    close = t + timedelta(hours=8)
    rows = an.trade_rows([fill("w", ts=t, price=0.35, count=10, fee=0.05)],
                         [settle("KXDEMOB-26OCT10-B70", "no", close + timedelta(hours=5), close=close)], [],
                         cells=CELLS, series_list=["KXDEMOB"], env="demo")
    r = an.public_trade(rows[0])
    assert set(r) == TRADE_KEYS
    assert r["pnl"] == pytest.approx(10 * (1 - 0.35) - 0.05, abs=0.01) and r["result"] == "win"
    assert r["cost"] == 3.5 and r["ttc_hours"] == 8.0 and r["ttc_band"] == "5-11h"
    assert r["cell"] == "0.34-0.38|5-11h|no" and r["series"] == "KXDEMOB" and r["band"] == "0.34-0.37"
    assert r["status"] == "settled" and r["kind"] == "maker"


def test_settlement_time_fallback_and_journal_cell():
    t = NOW - timedelta(days=2)
    # no close time known: ttc from the settlement time (20 h -> no cell by price/ttc) ...
    rows = an.trade_rows([fill("x", ts=t)], [settle("KXDEMOB-26OCT10-B70", "yes", t + timedelta(hours=20))], [],
                         cells=CELLS, series_list=[], env="demo")
    assert rows[0]["ttc_hours"] == 20.0 and rows[0]["cell"] is None and rows[0]["result"] == "loss"
    # ... unless the journal quote row of that order names a config cell
    jr = [{"event": "quote", "note": "live order_id=o-x", "cell": json.dumps(CELLS[0])},
          {"event": "quote", "note": "live order_id=o-y", "cell": json.dumps([0.5, 0.6, 1, 2, "no"])}]
    rows = an.trade_rows([fill("x", ts=t), fill("y", ts=t)],
                         [settle("KXDEMOB-26OCT10-B70", "yes", t + timedelta(hours=20))], [],
                         cells=CELLS, series_list=[], env="demo", journal=jr)
    by = {r["fill_id"]: r for r in rows}
    assert by["x"]["cell"] == "0.34-0.38|5-11h|no" and by["y"]["cell"] is None    # not in config -> ignored


def test_open_fill_and_fill_quality_labels():
    t = NOW - timedelta(hours=3)
    f1 = fill("f1", ts=t, ticker="T-1")
    f2 = fill("f2", ts=t, ticker="T-2", fill_quality={"trade_through": True})
    f3 = fill("f3", ts=t, ticker="T-3", fill_quality={"trade_through": False})
    eps = [{"order_id": "e1", "ts": t - timedelta(minutes=5), "fill_ids": ["f1"], "front_fill": True}]
    by = {r["fill_id"]: r for r in an.trade_rows([f1, f2, f3], [], eps, cells=CELLS, series_list=[], env="demo")}
    assert by["f1"]["fill_quality"] == "front" and by["f2"]["fill_quality"] == "trade_through"
    assert by["f3"]["fill_quality"] is None
    assert by["f1"]["status"] == "open" and by["f1"]["pnl"] is None and by["f1"]["result"] is None
    assert by["f1"]["ttc_hours"] is None and by["f1"]["cell"] is None


def test_summary_groups_curve_and_drawdown():
    d1, d2, d3 = NOW - timedelta(days=5), NOW - timedelta(days=4), NOW - timedelta(days=3)
    fills = [fill("a", ts=d1 - timedelta(hours=8), ticker="KXDEMOB-A", price=0.35, count=10),   # +6.50
             fill("b", ts=d2 - timedelta(hours=8), ticker="KXDEMOB-B", price=0.35, count=10),   # -3.50
             fill("c", ts=d3 - timedelta(hours=8), ticker="KXDEMOA-C", price=0.43, count=10),  # +5.70
             fill("t", ts=d3 - timedelta(hours=8), ticker="KXDEMOA-D", price=0.60, count=5, taker=True),  # +2
             fill("o", ts=NOW - timedelta(hours=1), ticker="KXDEMOA-E")]                         # open
    st = [settle("KXDEMOB-A", "no", d1, d1), settle("KXDEMOB-B", "yes", d2, d2),
          settle("KXDEMOA-C", "no", d3, d3), settle("KXDEMOA-D", "no", d3, d3)]
    rows = an.trade_rows(fills, st, [], cells=CELLS, series_list=["KXDEMOB", "KXDEMOA"], env="demo")
    s = an.summary(rows, [], [], days=7, env="demo", now=NOW, cells=CELLS)
    assert set(s) == SUMMARY_KEYS and set(s["totals"]) == TOTAL_KEYS
    tot = s["totals"]
    assert tot["settled_fills"] == 4 and tot["open_fills"] == 1
    assert tot["pnl"] == pytest.approx(6.5 - 3.5 + 5.7 + 2.0, abs=0.01)
    assert tot["win_rate"] == 0.75 and tot["roi"] == pytest.approx(round(tot["pnl"] / tot["cost"], 4))
    assert tot["max_drawdown"] == pytest.approx(-3.5, abs=0.01)
    curve = s["pnl_curve"]
    assert len(curve) == 8 and curve[-1]["cum_pnl"] == pytest.approx(tot["pnl"], abs=0.01)
    assert all(p["drawdown"] <= 0 for p in curve) and {"maker_pnl", "taker_pnl"} <= set(curve[0])
    day3 = next(p for p in curve if p["date"] == d3.date().isoformat())
    assert day3["taker_pnl"] == 2.0 and day3["maker_pnl"] == 5.7
    # by_* groups are maker only
    assert sum(g["n"] for g in s["by_series"]) == 3
    ny = next(g for g in s["by_series"] if g["key"] == "KXDEMOB")
    assert set(ny) == GROUP_KEYS and ny["n"] == 2 and ny["wins"] == 1 and ny["pnl"] == 3.0
    assert ny["avg_price"] == 0.35 and ny["win_rate"] == 0.5
    lo, hi = an.wilson(1, 2)
    assert ny["win_ci_low"] == round(lo, 4) and ny["edge"] == round(0.5 - 0.35, 4)
    assert ny["edge_ci_high"] == round(hi - 0.35, 4)
    assert [g["key"] for g in s["by_band"]] == ["0.34-0.37", "0.38-0.44"]
    assert [g["key"] for g in s["by_ttc"]] == ["5-11h"]
    cells = {g["cell"]: g for g in s["by_cell"]}
    assert cells["0.34-0.38|5-11h|no"]["n"] == 2 and cells["0.34-0.38|5-11h|no"]["in_config"] is True
    assert cells["0.38-0.45|5-11h|no"]["n"] == 1


def test_summary_windows_by_settlement_time():
    old = NOW - timedelta(days=10)
    rows = an.trade_rows([fill("a", ts=old - timedelta(hours=8), ticker="T-A")], [settle("T-A", "no", old, old)],
                         [], cells=CELLS, series_list=[], env="demo")
    assert an.summary(rows, [], [], days=7, env="demo", now=NOW, cells=CELLS)["totals"]["settled_fills"] == 0
    assert an.summary(rows, [], [], days=30, env="demo", now=NOW, cells=CELLS)["totals"]["settled_fills"] == 1


def test_empty_summary_shape():
    s = an.summary([], [], [], days=30, env="demo", now=NOW, cells=CELLS)
    assert s["totals"] == {"settled_fills": 0, "contracts": 0.0, "cost": 0.0, "pnl": 0.0, "roi": None,
                           "win_rate": None, "open_fills": 0, "max_drawdown": 0.0}
    assert s["pnl_curve"] == [] and s["by_cell"] == [] and s["fill_quality"] == []
    assert s["arb"] == {"opportunities": 0, "executed": 0, "pnl": None, "daily": []}
    assert an.group_stats("x", [])["win_rate"] is None


def test_fill_quality_weekly_and_arb():
    t = NOW - timedelta(days=1)
    f1 = fill("f1", ts=t, ticker="T-1", fill_quality={"trade_through": True})
    f2 = fill("f2", ts=t, ticker="T-2")
    eps = [{"order_id": "e1", "ts": t - timedelta(minutes=5), "env": "demo", "fill_ids": ["f2"], "front_fill": True,
            "queue_ahead_at_join": 10.0, "outcome": "filled"},
           {"order_id": "e2", "ts": t - timedelta(minutes=50), "env": "demo", "fill_ids": [], "front_fill": None,
            "queue_ahead_at_join": 30.0, "outcome": "repriced"},
           {"order_id": "e3", "ts": t, "env": "live", "queue_ahead_at_join": 999.0, "outcome": "repriced"}]
    rows = an.trade_rows([f1, f2], [], eps, cells=CELLS, series_list=[], env="demo")
    wk = an.fill_quality_weekly(rows, eps, lo=NOW - timedelta(days=7), now=NOW, env="demo")
    w = next(x for x in wk if x["week"] == an.iso_week(t))
    assert w == {"week": an.iso_week(t), "fills": 2, "front": 1, "trade_through": 1, "front_rate": 1.0,
                 "avg_queue_ahead": 20.0, "requotes": 1}
    arbs = [{"ts": t, "executed": "log-only", "mode": "demo"},
            {"ts": t, "executed": "FILLED {}", "profit": 0.5, "mode": "demo"},
            {"ts": t, "executed": "PARTIAL {}", "profit": 9, "mode": "demo"},
            {"ts": t, "executed": "FILLED {}", "profit": 7, "mode": "live"},
            {"ts": NOW - timedelta(days=40), "executed": "FILLED {}", "profit": 7}]
    a = an.arb_summary(arbs, lo=NOW - timedelta(days=30), env="demo")
    assert a["opportunities"] == 3 and a["executed"] == 2 and a["pnl"] == 0.5
    assert a["daily"] == [{"date": t.date().isoformat(), "opportunities": 3, "executed": 2}]
    assert an.arb_summary(arbs, lo=NOW - timedelta(days=30), env="all")["pnl"] == 7.5


def test_filters():
    t = NOW - timedelta(days=1)
    rows = an.trade_rows([fill("a", ts=t, ticker="KXDEMOB-1"), fill("b", ts=t, ticker="KXDEMOA-1", side="yes",
                                                                    price=0.48, taker=True)],
                         [settle("KXDEMOB-1", "no", t + timedelta(hours=8), t + timedelta(hours=8))], [],
                         cells=CELLS, series_list=["KXDEMOB", "KXDEMOA"], env="demo")
    since = NOW - timedelta(days=7)
    ids = lambda **k: [r["fill_id"] for r in an.filter_trades(rows, since=since, **k)]  # noqa: E731
    assert ids() == ["b", "a"] or ids() == ["a", "b"]
    assert ids(series="KXDEMOB") == ["a"] and ids(side="yes") == ["b"] and ids(kind="taker") == ["b"]
    assert ids(status="open") == ["b"] and ids(result="win") == ["a"] and ids(result="loss") == []
    assert ids(band="0.45-0.49") == ["b"] and ids(cell="0.34-0.38|5-11h|no") == ["a"] and ids(cell="none") == ["b"]
    assert an.filter_trades(rows, since=NOW) == []


def test_env_filter_in_rows():
    t = NOW - timedelta(days=1)
    fills = [fill("d", ts=t, env="demo"), fill("l", ts=t, env="live"), fill("n", ts=t, env=None)]
    get = lambda e: sorted(r["fill_id"] for r in an.trade_rows(fills, [], [], cells=CELLS, series_list=[], env=e))  # noqa: E731
    assert get("demo") == ["d", "n"] and get("live") == ["l", "n"] and get("all") == ["d", "l", "n"]


# ---------------------------------------------------------------- API
async def _put(store, fills, settlements=(), episodes=(), arbs=()):
    await store.upsert_fills(fills)
    if settlements:
        await store.upsert_settlements(list(settlements))
    if episodes:
        await store.upsert_quote_episodes(list(episodes))
    for a in arbs:
        await store.add_arb_opportunity(a)


@pytest.mark.asyncio
async def test_auth_required(env):
    for path in ("/api/analytics/summary", "/api/analytics/trades"):
        assert (await env.client.get(path)).status_code == 401
        assert (await env.client.get(path, headers={"Authorization": "Bearer nope"})).status_code == 401


@pytest.mark.asyncio
async def test_no_2fa_needed_and_empty_shapes(env):
    r = await env.client.get("/api/analytics/summary", headers=env.auth)
    assert r.status_code == 200
    s = r.json()
    assert set(s) == SUMMARY_KEYS and s["env"] == "demo" and s["days"] == 30 and s["truncated"] is False
    assert s["totals"]["roi"] is None and s["pnl_curve"] == []
    r = await env.client.get("/api/analytics/trades", headers=env.auth)
    assert r.json() == {"total": 0, "offset": 0, "limit": 50, "trades": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("path,query", [
    ("summary", "days=14"), ("summary", "days=abc"), ("summary", "env=prod"), ("trades", "limit=0"),
    ("trades", "limit=201"), ("trades", "offset=-1"), ("trades", "side=maybe"), ("trades", "kind=x"),
    ("trades", "status=closed"), ("trades", "band=0.5-0.6"), ("trades", "result=draw"),
    ("trades", "series=NOTASERIES"), ("trades", "series=kxdemob"), ("trades", "cell=0.10-0.20|0-1h|no"),
    ("trades", "days=365")])
async def test_validation_422(env, path, query):
    r = await env.client.get(f"/api/analytics/{path}?{query}", headers=env.auth)
    assert r.status_code == 422, (query, r.text)


@pytest.mark.asyncio
async def test_trades_api_filters_pagination_and_env(env):
    t = NOW - timedelta(days=1)
    fills = [fill(f"f{i:02d}", ts=t - timedelta(minutes=i), ticker=f"KXDEMOB-26OCT12-B{i}") for i in range(25)]
    fills += [fill("live1", ts=t, ticker="KXDEMOB-26OCT12-L", env="live"),
              fill("tk", ts=t, ticker="KXDEMOA-26OCT12-T", taker=True)]
    sts = [settle(f"KXDEMOB-26OCT12-B{i}", "no" if i % 5 else "yes", t + timedelta(hours=8), t + timedelta(hours=8))
           for i in range(25)]
    await _put(env.store, fills, sts)
    get = lambda q: env.client.get(f"/api/analytics/trades?{q}", headers=env.auth)  # noqa: E731
    body = (await get("limit=10&offset=0")).json()
    assert body["total"] == 26 and len(body["trades"]) == 10 and body["limit"] == 10
    assert body["trades"][0]["ts"] >= body["trades"][-1]["ts"]                     # newest first
    assert set(body["trades"][0]) == TRADE_KEYS
    page3 = (await get("limit=10&offset=20")).json()
    assert len(page3["trades"]) == 6 and page3["offset"] == 20
    ids = {x["fill_id"] for p in (0, 10, 20) for x in (await get(f"limit=10&offset={p}")).json()["trades"]}
    assert len(ids) == 26 and "live1" not in ids                                       # default env = bot mode (demo)
    assert (await get("env=live")).json()["total"] == 1
    assert (await get("env=all")).json()["total"] == 27
    assert (await get("kind=taker")).json()["total"] == 1
    assert (await get("result=loss")).json()["total"] == 5
    assert (await get("status=open")).json()["total"] == 1
    assert (await get("series=KXDEMOA")).json()["total"] == 1
    assert (await get("cell=0.34-0.38%7C5-11h%7Cno")).json()["total"] == 25
    assert (await get("offset=500")).json()["trades"] == []


@pytest.mark.asyncio
async def test_summary_api_with_seed(settings, bot_paths, monkeypatch):
    import httpx
    from control_api.auth import security as sec
    from control_api.main import create_app
    store = MemoryStore()
    await seed(store)
    app = create_app(settings, store=store, kalshi_factory=lambda m: None, cronjob_factory=lambda k: None)
    await app.state.runner.rehydrate()
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    await store.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/analytics/summary?days=90", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["totals"]["settled_fills"] > 200 and s["totals"]["max_drawdown"] < 0
        assert len(s["pnl_curve"]) == 91 and s["by_cell"] and all(g["in_config"] for g in s["by_cell"] if g["cell"])
        assert s["fill_quality"] and s["arb"]["executed"] > 0
        assert any(g["win_ci_low"] is not None for g in s["by_series"])
        r = await c.get("/api/analytics/trades?days=90&limit=200", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and len(r.json()["trades"]) == 200


@pytest.mark.asyncio
async def test_store_down_is_503(env):
    class Down:
        def __getattr__(self, name):
            async def boom(*a, **k):
                raise RuntimeError("atlas down")
            return boom
    real = env.app.state.store
    env.app.state.store = Down()
    try:
        r = await env.client.get("/api/analytics/summary", headers=env.auth)
    finally:
        env.app.state.store = real
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_reads_are_bounded(env, monkeypatch):
    calls = {}
    store = env.store
    for name in ("list_fills", "list_settlements", "list_quote_episodes", "list_journal", "list_arb_opportunities"):
        orig = getattr(store, name)

        def wrap(orig=orig, name=name):
            async def f(**kw):
                calls[name] = kw
                return await orig(**kw)
            return f
        monkeypatch.setattr(store, name, wrap())
    monkeypatch.setattr(an, "MAX_FILLS", 3)
    await _put(store, [fill(f"x{i}", ts=NOW - timedelta(hours=i + 1), ticker=f"T-{i}") for i in range(5)])
    s = (await env.client.get("/api/analytics/summary?days=180", headers=env.auth)).json()
    assert s["truncated"] is True and s["totals"]["open_fills"] == 3
    for name, kw in calls.items():
        assert kw["limit"] <= 20_001 and kw["since"] >= NOW - timedelta(days=188), name
    assert calls["list_fills"]["fields"] == an.FILL_FIELDS
    assert calls["list_journal"]["events"] == ["quote"]


@pytest.mark.asyncio
async def test_no_secret_or_user_data_in_responses(env):
    t = NOW - timedelta(days=1)
    await _put(env.store, [fill("s1", ts=t, ticker="T-1", api_key="nope")])
    for p in ("summary", "trades"):
        text = (await env.client.get(f"/api/analytics/{p}", headers=env.auth)).text.lower()
        assert "api_key" not in text and "owner" not in text and "password" not in text


def test_series_list_from_strategy_defaults():
    ms = botfiles.strategy()
    assert "KXDEMOB" in ms.DEFAULTS["series"]


# ---------------------------------------------------------------- summary cache + per-user rate limit
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _until(pred, steps: int = 500) -> None:
    """Let background refresh tasks run until pred() holds (no wall-clock assertion; fails only if it never does)."""
    for _ in range(steps):
        if pred():
            return
        await asyncio.sleep(0.01)
    assert pred()


def ra_state(env) -> dict:
    from control_api.routes import analytics as route
    return route._state(type("R", (), {"app": env.app})())


def _count_reads(store, monkeypatch):
    n = {"fills": 0}
    orig = store.list_fills

    async def f(**kw):
        n["fills"] += 1
        return await orig(**kw)
    monkeypatch.setattr(store, "list_fills", f)
    return n


@pytest.mark.asyncio
async def test_summary_cache_hit_expiry_and_keys(env, monkeypatch):
    from control_api.routes import analytics as route
    clock = _Clock()
    monkeypatch.setattr(route, "_clock", clock)
    reads = _count_reads(env.store, monkeypatch)
    get = lambda q="": env.client.get(f"/api/analytics/summary?{q}", headers=env.auth)  # noqa: E731
    a = (await get()).json()
    await _put(env.store, [fill("late", ts=NOW - timedelta(hours=1), ticker="T-LATE")])
    clock.t += 30
    b = (await get()).json()
    assert reads["fills"] == 1 and b == a                         # cache hit: no store read, same generated_utc
    assert b["totals"]["open_fills"] == 0 and "truncated" in b
    await get("days=7")
    await get("env=all")
    assert reads["fills"] == 3                                    # (env, days) are separate keys
    clock.t += 31                                 # 61 s after the first read: stale-while-refresh
    c = (await get()).json()
    assert c == a                                                 # the last good copy, served at once
    await _until(lambda: reads["fills"] == 4)                     # ONE background refresh ...
    await _until(lambda: not ra_state(env)["gate"].locked())      # ... that has finished and stored its copy
    d = (await get()).json()
    assert reads["fills"] == 4 and d["totals"]["open_fills"] == 1 and d["generated_utc"] >= a["generated_utc"]


@pytest.mark.asyncio
async def test_summary_errors_are_not_cached(env, monkeypatch):
    from control_api.routes import analytics as route
    monkeypatch.setattr(route, "_clock", _Clock())
    real = env.store.list_fills

    async def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_fills", boom)
    assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 503
    monkeypatch.setattr(env.store, "list_fills", real)
    assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_429_per_user_and_stop_still_works(env, monkeypatch):
    from control_api.auth import security as sec
    from control_api.routes import analytics as route
    monkeypatch.setitem(route.RATE_LIMITS, "analytics_summary", (3, 60.0))
    monkeypatch.setitem(route.RATE_LIMITS, "analytics_trades", (2, 60.0))
    for _ in range(3):
        assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 200
    r = await env.client.get("/api/analytics/summary", headers=env.auth)
    assert r.status_code == 429 and r.json()["detail"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
    for _ in range(2):
        assert (await env.client.get("/api/analytics/trades", headers=env.auth)).status_code == 200
    assert (await env.client.get("/api/analytics/trades", headers=env.auth)).status_code == 429
    # per user, not per IP: another user from the same IP is not limited
    other = {"Authorization": f"Bearer {sec.create_token(env.settings, 'second', sec.TYP_ACCESS, 3600)[0]}"}
    await env.store.save_user({"username": "second", "password_hash": "x", "failed_logins": 0})
    assert (await env.client.get("/api/analytics/trades", headers=other)).status_code == 200
    # Stop and the other GET routes are unaffected
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200, r.text
    assert (await env.client.get("/api/history", headers=env.auth)).status_code == 200


@pytest.mark.asyncio
async def test_unauthenticated_requests_do_not_consume_the_limit(env, monkeypatch):
    from control_api.routes import analytics as route
    monkeypatch.setitem(route.RATE_LIMITS, "analytics_trades", (1, 60.0))
    for _ in range(5):
        assert (await env.client.get("/api/analytics/trades")).status_code == 401
    assert (await env.client.get("/api/analytics/trades", headers=env.auth)).status_code == 200


# ---------------------------------------------------------------- S-28 / S-30: single flight, rows cache, projections
def _count_calls(store, monkeypatch, names=("list_fills", "list_settlements", "list_quote_episodes",
                                            "list_journal", "list_arb_opportunities")):
    calls = {n: [] for n in names}
    for n in names:
        orig = getattr(store, n)

        def wrap(orig=orig, n=n):
            async def f(**kw):
                calls[n].append(kw)
                return await orig(**kw)
            return f
        monkeypatch.setattr(store, n, wrap())
    return calls


def _slow_rows(monkeypatch, secs, counter):
    import time as _t
    orig = an.trade_rows

    def slow(*a, **k):
        counter["active"] += 1
        counter["max"] = max(counter["max"], counter["active"])
        counter["builds"] += 1
        try:
            _t.sleep(secs)
            return orig(*a, **k)
        finally:
            counter["active"] -= 1
    monkeypatch.setattr(an, "trade_rows", slow)


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_one_build(env, monkeypatch):
    import asyncio
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    calls = _count_calls(env.store, monkeypatch)
    ctr = {"active": 0, "max": 0, "builds": 0}
    _slow_rows(monkeypatch, 0.3, ctr)
    reqs = [env.client.get("/api/analytics/summary", headers=env.auth) for _ in range(6)]
    reqs += [env.client.get(f"/api/analytics/trades?offset={i}", headers=env.auth) for i in range(2)]
    rs = await asyncio.gather(*reqs)
    assert [r.status_code for r in rs] == [200] * 8
    assert ctr["builds"] == 1 and len(calls["list_fills"]) == 1        # one build serves summary AND trades
    assert len({r.json()["generated_utc"] for r in rs[:6]}) == 1


@pytest.mark.slow
@pytest.mark.asyncio
async def test_at_most_one_build_at_a_time_and_busy_429(env, monkeypatch):
    import asyncio
    from control_api.routes import analytics as route
    ctr = {"active": 0, "max": 0, "builds": 0}
    _slow_rows(monkeypatch, 0.25, ctr)
    rs = await asyncio.gather(*[env.client.get(f"/api/analytics/summary?days={d}&env={e}", headers=env.auth)
                                for d in (7, 30) for e in ("demo", "live")])
    assert [r.status_code for r in rs] == [200] * 4 and ctr["max"] == 1 and ctr["builds"] == 4
    route.clear_cache(env.app)
    monkeypatch.setattr(route, "BUSY_WAIT_S", 0.05)
    rs = await asyncio.gather(*[env.client.get(f"/api/analytics/summary?days={d}", headers=env.auth)
                                for d in (7, 30, 90)])
    codes = sorted(r.status_code for r in rs)
    assert codes[0] == 200 and 429 in codes and ctr["max"] == 1
    busy = next(r for r in rs if r.status_code == 429)
    assert busy.json()["detail"] == "analytics_busy" and int(busy.headers["Retry-After"]) >= 1


@pytest.mark.slow
@pytest.mark.asyncio
async def test_stop_fast_during_concurrent_analytics_load(env, monkeypatch):
    import asyncio
    import time as _t
    ctr = {"active": 0, "max": 0, "builds": 0}
    _slow_rows(monkeypatch, 0.4, ctr)
    env.runner.desired = "running"
    load = [asyncio.create_task(env.client.get(f"/api/analytics/{p}?days={d}&env={e}", headers=env.auth))
            for p in ("summary", "trades") for d in (7, 30) for e in ("demo", "live")]
    await asyncio.sleep(0.1)
    t0 = _t.monotonic()
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    took = _t.monotonic() - t0
    assert r.status_code == 200 and env.runner.desired == "stopped" and env.runner.kill_path.exists()
    assert took < 1.0, f"Stop took {took:.2f}s under analytics load"
    rs = await asyncio.gather(*load)
    assert all(x.status_code in (200, 429) for x in rs) and ctr["max"] == 1


@pytest.mark.asyncio
async def test_trades_paging_and_filters_hit_the_rows_cache(env, monkeypatch):
    await _put(env.store, [fill(f"f{i}", ts=NOW - timedelta(hours=i + 1), ticker=f"KXDEMOB-X{i}") for i in range(12)])
    calls = _count_calls(env.store, monkeypatch)
    get = lambda q: env.client.get(f"/api/analytics/trades?{q}", headers=env.auth)  # noqa: E731
    assert (await get("limit=5")).json()["total"] == 12
    assert len((await get("limit=5&offset=5")).json()["trades"]) == 5
    assert (await get("side=yes")).json()["total"] == 0
    assert (await get("series=KXDEMOB&status=open")).json()["total"] == 12
    assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 200
    assert all(len(v) == 1 for v in calls.values()), {k: len(v) for k, v in calls.items()}
    assert calls["list_settlements"][0]["fields"] == an.SETTLEMENT_FIELDS
    assert calls["list_journal"][0]["fields"] == an.JOURNAL_FIELDS
    assert calls["list_journal"][0]["events"] == ["quote"]


@pytest.mark.asyncio
async def test_build_errors_propagate_to_waiters_and_are_not_cached(env, monkeypatch):
    import asyncio
    real = env.store.list_fills

    async def slow_boom(**kw):
        await asyncio.sleep(0.1)
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_fills", slow_boom)
    rs = await asyncio.gather(*[env.client.get("/api/analytics/summary", headers=env.auth) for _ in range(3)])
    assert [r.status_code for r in rs] == [503] * 3 and "down" not in rs[0].text
    monkeypatch.setattr(env.store, "list_fills", real)
    assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 200


@pytest.mark.asyncio
async def test_journal_map_is_incremental(env, monkeypatch):
    from control_api.routes import analytics as route
    clock = _Clock()
    monkeypatch.setattr(route, "_clock", clock)
    t = NOW - timedelta(days=2)

    def qrow(at, ticker, oid):
        return {"utc": _z(at), "event": "quote", "ticker": ticker, "side": "no", "price": "0.35", "count": "1",
                "cost": "0.35", "cell": json.dumps(CELLS[0]), "note": f"live order_id={oid}"}
    await env.store.add_journal_rows([qrow(t, "T-1", "o-j1")])
    await _put(env.store, [fill("j1", ts=t + timedelta(minutes=5), ticker="T-1"),
                           fill("j2", ts=t + timedelta(minutes=6), ticker="T-2")],
               [settle("T-1", "no", t + timedelta(hours=20)), settle("T-2", "no", t + timedelta(hours=20))])
    calls = _count_calls(env.store, monkeypatch, names=("list_journal",))
    by = {x["fill_id"]: x for x in (await env.client.get("/api/analytics/trades", headers=env.auth)).json()["trades"]}
    assert by["j1"]["cell"] == "0.34-0.38|5-11h|no" and by["j2"]["cell"] is None
    await env.store.add_journal_rows([qrow(t + timedelta(minutes=1), "T-2", "o-j2")])
    clock.t += 61                                                       # summary/rows caches stale
    await env.client.get("/api/analytics/trades", headers=env.auth)     # stale copy served, one refresh started
    await _until(lambda: len(calls["list_journal"]) == 2)
    await _until(lambda: not ra_state(env)["inflight"] and not ra_state(env)["gate"].locked())
    by = {x["fill_id"]: x for x in (await env.client.get("/api/analytics/trades", headers=env.auth)).json()["trades"]}
    assert by["j2"]["cell"] == "0.34-0.38|5-11h|no" and by["j1"]["cell"] == "0.34-0.38|5-11h|no"
    first, second = calls["list_journal"][0]["since"], calls["list_journal"][1]["since"]
    assert second > first                                              # incremental: newest seen - overlap
    assert abs((second - (t - route.JOURNAL_OVERLAP)).total_seconds()) < 1


@pytest.mark.asyncio
async def test_store_projection_parity_memory_and_mongo():
    from control_api.db.mongo import MongoStore
    from control_api.tests.test_store_arb import _Client
    s = MemoryStore()
    await s.upsert_settlements([{"ticker": "A", "market_result": "no", "settled_time": _z(NOW), "revenue": 100}])
    await s.add_journal_rows([{"utc": _z(NOW), "event": "quote", "ticker": "A", "note": "n", "cell": "[]"}])
    assert await s.list_settlements(fields=("ticker", "market_result")) == [{"ticker": "A", "market_result": "no"}]
    assert set((await s.list_settlements())[0]) >= {"ticker", "market_result", "revenue", "ts"}   # default: all
    assert await s.list_journal(fields=("event", "note")) == [{"event": "quote", "note": "n"}]
    assert "row_hash" in (await s.list_journal())[0]
    client = _Client()
    ms = MongoStore("", client=client)
    await ms.list_settlements(fields=("ticker", "market_result"))
    await ms.list_journal(events=["quote"], fields=("event", "note"))
    await ms.list_settlements()
    finds = [c for c in client.calls if c[1] == "find"]
    assert finds[0][3] == {"_id": 0, "ticker": 1, "market_result": 1}
    assert finds[1][2] == {"event": {"$in": ["quote"]}} and finds[1][3] == {"_id": 0, "event": 1, "note": 1}
    assert finds[2][3] == {"_id": 0}


# ---------------------------------------------------------------- sprint extension: 1 verdict, 2 clustered CI,
# 3 calibration, 4 fill rate, 7 CSV
def test_cluster_ratio_ci_known_values():
    # clusters (wins, fills): A 2/2, B 1/2, C 0/1 -> p = 0.6, V = 3/2 * (0.8^2 + 0.2^2 + 0.6^2) / 5^2 = 0.0624
    p, lo, hi = an.cluster_ratio_ci([(2, 2), (1, 2), (0, 1)])
    assert p == pytest.approx(0.6) and lo == pytest.approx(0.6 - an.Z95 * 0.0624 ** 0.5, abs=1e-9)
    assert hi == pytest.approx(0.6 + an.Z95 * 0.0624 ** 0.5, abs=1e-9)
    assert an.cluster_ratio_ci([(2, 2), (1, 2), (0, 1)], clip=(0, 1))[2] == 1.0
    # ROI as a ratio of sums (pnl, cost): R = 2/40, V = 3/2 * (0.5^2 + 2.5^2 + 2^2) / 40^2
    r, lo, hi = an.cluster_ratio_ci([(1, 10), (-2, 10), (3, 20)])
    se = (1.5 * 10.5 / 1600) ** 0.5
    assert r == pytest.approx(0.05) and lo == pytest.approx(0.05 - an.Z95 * se) and hi == pytest.approx(0.05 + an.Z95 * se)
    assert an.cluster_ratio_ci([(1, 1)]) == (1.0, None, None)                # < 2 clusters -> no CI
    assert an.cluster_ratio_ci([]) == (None, None, None)


def _rows_for(results_by_ticker, price=0.4):
    """[('T1', ['win','win']), ...] -> minimal settled rows."""
    out = []
    for t, res in results_by_ticker:
        for i, x in enumerate(res):
            out.append({"ticker": t, "fill_id": f"{t}-{i}", "result": x, "price": price, "count": 1.0,
                        "_cost": price, "_pnl": (1 - price) if x == "win" else -price})
    return out


def test_group_cluster_ci_widens_and_handles_all_wins():
    rows = _rows_for([("A", ["win", "win"]), ("B", ["win", "loss"]), ("C", ["loss"])])
    g = an.group_stats("k", rows)
    wlo, whi = an.wilson(3, 5)
    assert g["cluster_ci_low"] <= round(wlo, 4) and g["cluster_ci_high"] >= round(whi, 4)
    assert g["cluster_ci_low"] == round(min(0.6 - an.Z95 * 0.0624 ** 0.5, wlo), 4)
    # edge = win - price per fill, clustered: same residuals as the win rate (price is constant)
    assert g["edge_cluster_ci_low"] == pytest.approx(round(0.6 - an.Z95 * 0.0624 ** 0.5 - 0.4, 4), abs=1e-4)
    # every market won: the cluster variance is 0 -> Wilson on the number of markets, never a point
    allwin = an.group_stats("k", _rows_for([(f"T{i}", ["win", "win"]) for i in range(25)], price=0.48))
    lo, hi = an.wilson(25, 25)
    assert allwin["cluster_ci_low"] == round(lo, 4) and allwin["cluster_ci_high"] == 1.0
    assert allwin["cluster_ci_low"] < 1.0
    one = an.group_stats("k", _rows_for([("A", ["win", "loss"])]))
    assert one["cluster_ci_low"] is None and one["roi_ci_low"] is None and one["edge_cluster_ci_high"] is None


def test_verdict_rules():
    g = {"win_rate": 0.45, "cluster_ci_low": 0.42, "cluster_ci_high": 0.47, "roi": 0.01, "roi_ci_low": -0.01,
         "roi_ci_high": 0.03}
    assert an.verdict(g, 19, 0.46, 0.01)[0] == "too_early"
    assert "19 markets" in an.verdict(g, 19, 0.46, 0.01)[1]
    assert an.verdict(g, 20, 0.46, 0.01)[0] == "on_track"
    assert an.verdict(g, 20, 0.48, 0.01)[0] == "below"
    assert an.verdict(g, 20, 0.40, 0.01)[0] == "above"
    assert an.verdict(g, 20, None, 0.04)[0] == "below" and "ROI" in an.verdict(g, 20, None, 0.04)[1]
    assert an.verdict(g, 20, None, -0.02)[0] == "above"
    assert an.verdict(g, 20, None, 0.015)[0] == "on_track"
    assert an.verdict(g, 20, None, None)[0] == "on_track"
    assert an.verdict({**g, "cluster_ci_low": None}, 20, 0.46, None)[0] == "too_early"
    for v in ("below", "above", "on_track"):
        reason = an.verdict(g, 25, {"below": 0.48, "above": 0.9, "on_track": 0.46}[v], None)[1]
        assert reason.endswith(".") and "research expectation" in reason


def _config_cell_ids() -> set[str]:
    cfg = json.loads((Path(__file__).resolve().parents[2] / "kalshi_bot" / "maker_config.json").read_text())
    return {an.cell_id(c) for c in an.parse_cells(cfg["cells"])}


def _fake_exp() -> dict:
    from control_api.tests.conftest import fake_expectations_doc
    return fake_expectations_doc()


def test_expectations_load_from_env_path_and_fallbacks(tmp_path, monkeypatch):
    fake = _fake_exp()
    exp = an.load_expectations()                                        # conftest: CELL_EXPECTATIONS_PATH = fixture
    assert exp["loaded"] and exp["source"] == fake["source"] and exp["default"]["roi"] == fake["default"]["roi"]
    assert set(exp["cells"]) >= _config_cell_ids()                      # installed cells + the synthetic test cells
    cid = sorted(fake["cells"])[0]
    assert an.expectation_for(cid, exp) == (fake["cells"][cid]["win_rate"], fake["cells"][cid]["roi"])
    dflt = (None, fake["default"]["roi"])
    assert an.expectation_for("nope", exp) == dflt and an.expectation_for(None, exp) == dflt
    monkeypatch.setenv("CELL_EXPECTATIONS_PATH", str(tmp_path / "none.json"))
    missing = an.load_expectations()
    assert missing["loaded"] is False and an.expectation_for(cid, missing) == (None, None)   # no number in code
    (tmp_path / "bad.json").write_text("{not json")
    monkeypatch.setenv("CELL_EXPECTATIONS_PATH", str(tmp_path / "bad.json"))
    assert an.load_expectations()["loaded"] is False
    monkeypatch.setenv("CELL_EXPECTATIONS_PATH", "  ")
    assert an.expectations_path() == an.EXPECTATIONS_PATH                # blank -> repo default
    monkeypatch.delenv("CELL_EXPECTATIONS_PATH")
    assert an.expectations_path() == an.EXPECTATIONS_PATH


def test_repo_expectations_file_ids_match_config_cells():
    """Ids only (no numbers). The file is private (git-ignored here, written by the overlay); skipped without it."""
    if not an.EXPECTATIONS_PATH.exists():
        pytest.skip("cell_expectations.json not present (private repo / secret file)")
    exp = an.load_expectations(an.EXPECTATIONS_PATH)
    assert exp["loaded"] and set(exp["cells"]) == _config_cell_ids()
    assert all(set(v) >= {"win_rate", "roi"} for v in exp["cells"].values())


def test_expectations_never_logged(caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        exp = an.load_expectations()
    text = caplog.text
    assert exp["loaded"] and exp["source"] not in text and "win_rate" not in text


def test_summary_by_cell_verdict_fields():
    t = NOW - timedelta(days=3)
    fills, sts = [], []
    for i in range(24):                                     # 24 markets in 0.34-0.38|5-11h|no, 1 loss
        tk = f"KXDEMOB-V{i}"
        fills.append(fill(f"v{i}", ts=t, ticker=tk, price=0.35))
        sts.append(settle(tk, "yes" if i == 0 else "no", t + timedelta(hours=8), t + timedelta(hours=8)))
    fills.append(fill("w", ts=t, ticker="KXDEMOB-W", price=0.43))
    sts.append(settle("KXDEMOB-W", "no", t + timedelta(hours=8), t + timedelta(hours=8)))
    rows = an.trade_rows(fills, sts, [], cells=CELLS, series_list=["KXDEMOB"], env="demo")
    s = an.summary(rows, [], [], days=7, env="demo", now=NOW, cells=CELLS, expectations=an.load_expectations())
    assert s["expectations"]["loaded"] is True
    c = {g["key"]: g for g in s["by_cell"]}
    a = c["0.34-0.38|5-11h|no"]
    want = _fake_exp()["cells"]["0.34-0.38|5-11h|no"]
    assert a["markets"] == 24 and a["expected_win_rate"] == want["win_rate"] and a["expected_roi"] == want["roi"]
    # 23/24 won vs a fake expectation far below -> above (the rule is applied to whatever file is loaded)
    assert a["verdict"] == "above" and a["verdict_reason"]
    b = c["0.38-0.45|5-11h|no"]
    assert b["markets"] == 1 and b["verdict"] == "too_early"


def test_calibration_buckets():
    t = NOW - timedelta(days=2)
    fills = [fill("a", ts=t, ticker="T-A", price=0.34), fill("b", ts=t, ticker="T-B", price=0.35),
             fill("c", ts=t, ticker="T-C", price=0.49), fill("d", ts=t, ticker="T-D", price=0.48),
             fill("e", ts=t, ticker="T-E", price=0.29), fill("f", ts=t, ticker="T-F", price=0.36, taker=True)]
    sts = [settle(x, "no" if x != "T-B" else "yes", t + timedelta(hours=8), t + timedelta(hours=8))
           for x in ("T-A", "T-B", "T-C", "T-D", "T-E", "T-F")]
    rows = an.trade_rows(fills, sts, [], cells=CELLS, series_list=[], env="demo")
    cal = an.summary(rows, [], [], days=7, env="demo", now=NOW, cells=CELLS)["calibration"]
    # 2-cent buckets over the cells' price span (synthetic cells: 0.30 to 0.49, the top price inclusive)
    assert [b["bucket"] for b in cal] == ["0.30-0.31", "0.32-0.33", "0.34-0.35", "0.36-0.37", "0.38-0.39",
                                          "0.40-0.41", "0.42-0.43", "0.44-0.45", "0.46-0.47", "0.48-0.49"]
    assert cal[2]["lo"] == 0.34 and cal[2]["hi"] == 0.36 and cal[-1]["hi"] == 0.5
    assert cal[2]["n"] == 2 and cal[2]["wins"] == 1 and cal[2]["markets"] == 2 and cal[2]["win_rate"] == 0.5
    assert cal[2]["avg_price"] == 0.345 and cal[2]["ci_low"] is not None
    assert cal[-1]["n"] == 2 and cal[-1]["wins"] == 2                  # 0.48 and 0.49 (top price inclusive)
    assert cal[3] == {"bucket": "0.36-0.37", "lo": 0.36, "hi": 0.38, "n": 0, "markets": 0, "wins": 0,
                      "win_rate": None, "avg_price": None, "ci_low": None, "ci_high": None}   # taker excluded
    assert sum(b["n"] for b in cal) == 4                               # 0.29 excluded
    assert an.calibration([], []) == [] and an.calibration([], None) == []      # no cells -> no buckets


def _ep(oid, *, joined, outcome, count=5.0, filled=0.0, first=None, closed=None, status="closed", env="demo",
        price=0.35, side="no", close=None, series="KXDEMOB"):
    return {"order_id": oid, "ts": joined, "env": env, "status": status, "outcome": outcome, "count": count,
            "filled_count": filled, "first_fill_utc": first, "closed_utc": closed, "price": price, "side": side,
            "series": series, "ticker": f"{series}-{oid}", "close_time": close}


def test_fill_rate_from_episodes():
    j = NOW - timedelta(days=1)
    eps = [_ep("f", joined=j, outcome="filled", filled=5, first=j + timedelta(minutes=10), closed=j + timedelta(minutes=10),
               close=j + timedelta(hours=8)),
           _ep("p", joined=j, outcome="expired", filled=2, first=j + timedelta(minutes=30), closed=j + timedelta(minutes=90),
               close=j + timedelta(hours=8)),
           _ep("x", joined=j, outcome="expired", closed=j + timedelta(minutes=60), close=j + timedelta(hours=30)),
           _ep("c", joined=j, outcome="cancelled", closed=j + timedelta(minutes=20), series="KXDEMOA"),
           _ep("r", joined=j, outcome="repriced", closed=j + timedelta(minutes=5)),
           _ep("open", joined=j, outcome=None, status="open"),                          # still resting: excluded
           _ep("old", joined=NOW - timedelta(days=40), outcome="expired", closed=NOW - timedelta(days=40)),
           _ep("live", joined=j, outcome="expired", closed=j + timedelta(minutes=5), env="live")]
    fr = an.fill_rate(eps, lo=NOW - timedelta(days=7), env="demo", cells=CELLS, jcells={"c": "0.38-0.45|5-11h|no"},
                      series_list=["KXDEMOB", "KXDEMOA"])
    assert fr["totals"] == {"quotes": 5, "filled": 2, "partial": 1, "unfilled": 3, "fill_rate": 0.4,
                            "median_rest_minutes": 20.0, "median_minutes_to_fill": 20.0}
    cells = {x["key"]: x for x in fr["by_cell"]}
    assert cells["0.34-0.38|5-11h|no"]["quotes"] == 2                   # f, p: from price + close time at join
    assert cells["0.38-0.45|5-11h|no"]["quotes"] == 1                   # c: from the journal quote row
    assert cells["none"]["quotes"] == 2                                 # x (30 h: no cell), r (no close time)
    assert {x["key"]: x["quotes"] for x in fr["by_series"]} == {"KXDEMOA": 1, "KXDEMOB": 4}
    empty = an.fill_rate([], lo=NOW, env="demo", cells=CELLS, jcells={}, series_list=[])
    assert empty == {"totals": {"quotes": 0, "filled": 0, "partial": 0, "unfilled": 0, "fill_rate": None,
                                "median_rest_minutes": None, "median_minutes_to_fill": None},
                     "by_cell": [], "by_series": []}


def test_csv_safe_and_columns():
    assert an.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and an.csv_safe("+1") == "'+1"
    assert an.csv_safe("-cmd") == "'-cmd" and an.csv_safe("@SUM") == "'@SUM" and an.csv_safe("\tx") == "'\tx"
    assert an.csv_safe("KXDEMOB-1") == "KXDEMOB-1" and an.csv_safe(-0.35) == -0.35 and an.csv_safe(None) is None
    assert an.TRADE_COLUMNS == tuple(TRADE_KEYS_ORDER)
    text = an.trades_csv([{**{k: None for k in an.TRADE_COLUMNS}, "fill_id": "=1+1", "ticker": "@x", "pnl": -0.5}])
    lines = text.split("\r\n")
    assert lines[0] == ",".join(an.TRADE_COLUMNS)
    assert lines[1].startswith("'=1+1,,'@x,") and lines[1].endswith(",-0.5,,,")   # pnl, fill_quality, source, strategy


TRADE_KEYS_ORDER = ["fill_id", "ts", "ticker", "series", "side", "kind", "price", "count", "fee", "cost", "band",
                    "ttc_hours", "ttc_band", "cell", "status", "result", "settled_utc", "pnl", "fill_quality",
                    "source", "strategy"]


@pytest.mark.asyncio
async def test_trades_csv_endpoint(env, monkeypatch):
    import csv as _csv
    import io as _io
    t = NOW - timedelta(days=1)
    await _put(env.store, [fill(f"c{i}", ts=t - timedelta(minutes=i), ticker=f"KXDEMOB-C{i}") for i in range(4)]
               + [fill("tk", ts=t, ticker="KXDEMOA-T", taker=True)],
               [settle("KXDEMOB-C0", "yes", t + timedelta(hours=8), t + timedelta(hours=8))])
    r = await env.client.get("/api/analytics/trades.csv?days=7", headers=env.auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in r.headers["content-type"] and r.headers["x-truncated"] == "false"
    assert r.headers["content-disposition"] == \
        f'attachment; filename="trades-{datetime.now(timezone.utc):%Y-%m-%d}.csv"'
    rows = list(_csv.reader(_io.StringIO(r.text)))
    assert rows[0] == TRADE_KEYS_ORDER and len(rows) == 6
    by = {x[0]: dict(zip(rows[0], x)) for x in rows[1:]}
    assert by["c0"]["result"] == "loss" and float(by["c0"]["pnl"]) < 0 and by["c1"]["pnl"] == ""
    assert [x[0] for x in rows[1:]][:2] == ["c0", "tk"] or rows[1][0] in ("c0", "tk")     # newest first
    r = await env.client.get("/api/analytics/trades.csv?days=7&kind=taker", headers=env.auth)
    assert [x[0] for x in list(_csv.reader(_io.StringIO(r.text)))[1:]] == ["tk"]
    from control_api.services import analytics as svc
    monkeypatch.setattr(svc, "CSV_MAX_ROWS", 2)
    r = await env.client.get("/api/analytics/trades.csv?days=7", headers=env.auth)
    assert r.headers["x-truncated"] == "true" and len(list(_csv.reader(_io.StringIO(r.text)))) == 3
    assert (await env.client.get("/api/analytics/trades.csv")).status_code == 401
    assert (await env.client.get("/api/analytics/trades.csv?side=maybe", headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/analytics/trades.csv?days=14", headers=env.auth)).status_code == 422


@pytest.mark.asyncio
async def test_trades_csv_shares_the_trades_rate_limit(env, monkeypatch):
    from control_api.routes import analytics as route
    monkeypatch.setitem(route.RATE_LIMITS, "analytics_trades", (2, 60.0))
    assert (await env.client.get("/api/analytics/trades", headers=env.auth)).status_code == 200
    assert (await env.client.get("/api/analytics/trades.csv", headers=env.auth)).status_code == 200
    r = await env.client.get("/api/analytics/trades.csv", headers=env.auth)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_seed_exercises_extension_fields(settings, bot_paths):
    import httpx
    from control_api.auth import security as sec
    from control_api.main import create_app
    store = MemoryStore()
    await seed(store)
    app = create_app(settings, store=store, kalshi_factory=lambda m: None, cronjob_factory=lambda k: None)
    await app.state.runner.rehydrate()
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    await store.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        s = (await c.get("/api/analytics/summary?days=90", headers={"Authorization": f"Bearer {tok}"})).json()
    verdicts = {g["verdict"] for g in s["by_cell"] if g["cell"]}
    assert verdicts - {"too_early"} and all(g["markets"] >= 20 for g in s["by_cell"] if g["verdict"] != "too_early")
    assert s["expectations"]["loaded"] and sum(b["n"] for b in s["calibration"]) > 100
    fr = s["fill_rate"]["totals"]
    assert fr["quotes"] > 100 and fr["unfilled"] > 0 and fr["partial"] > 0 and 0 < fr["fill_rate"] < 1
    assert any(x["key"] != "none" for x in s["fill_rate"]["by_cell"])


# ---------------------------------------------------------------- lead decision: clustered CIs never narrower than
# the per-fill Wilson range; edge from the floored win CI; ROI widened by the Wilson-implied ROI
def test_all_won_group_ranges_are_not_degenerate():
    rows = _rows_for([(f"T{i}", ["win", "win", "win"]) for i in range(22)], price=0.48)
    g = an.group_stats("k", rows)
    mlo, _ = an.wilson(22, 22)                                     # effective n = markets (66 fills, 22 markets)
    flo, fhi = an.wilson(66, 66)
    assert g["cluster_ci_high"] == 1.0 and g["cluster_ci_low"] == round(min(mlo, flo), 4) < 1.0
    assert g["cluster_ci_low"] <= round(flo, 4)
    assert g["edge_cluster_ci_low"] == round(min(mlo, flo) - 0.48, 4)
    assert g["edge_cluster_ci_high"] == round(1.0 - 0.48, 4)
    assert g["roi_ci_low"] == round((min(mlo, flo) - 0.48) / 0.48, 4) < g["roi"]   # ROI range not a point
    assert g["roi_ci_high"] >= g["roi"]
    v, reason = an.verdict(g, 22, 0.999, 0.01)
    assert v == "on_track" and "consistent" in reason               # not a spurious "above"


def test_all_lost_group_ranges():
    rows = _rows_for([(f"T{i}", ["loss", "loss"]) for i in range(21)], price=0.35)
    g = an.group_stats("k", rows)
    _, mhi = an.wilson(0, 21)
    _, fhi = an.wilson(0, 42)
    assert g["cluster_ci_low"] == 0.0 and g["cluster_ci_high"] == round(max(mhi, fhi), 4) > 0
    assert g["edge_cluster_ci_low"] == round(-0.35, 4) and g["edge_cluster_ci_high"] == round(max(mhi, fhi) - 0.35, 4)
    assert g["roi_ci_low"] <= -1.0 + 1e-9 and g["roi_ci_high"] >= round((max(mhi, fhi) - 0.35) / 0.35, 4)
    assert an.verdict(g, 21, 0.4, 0.01)[0] == "below"


def test_single_cluster_has_no_clustered_ranges():
    g = an.group_stats("k", _rows_for([("ONLY", ["win", "loss", "win", "win"])]))
    assert g["n"] == 4 and g["win_ci_low"] is not None                  # per-fill Wilson still shown
    for k in ("cluster_ci_low", "cluster_ci_high", "edge_cluster_ci_low", "edge_cluster_ci_high",
              "roi_ci_low", "roi_ci_high"):
        assert g[k] is None, k
    cal = an.calibration(_rows_for([("ONLY", ["win", "win"])], price=0.4), CELLS)
    assert cal[5]["bucket"] == "0.40-0.41" and cal[5]["n"] == 2 and cal[5]["ci_low"] is None


def test_mixed_group_cluster_ci_contains_wilson_and_fees_lower_roi():
    rows = _rows_for([(f"T{i}", ["win"] * 3 + (["loss"] if i % 4 == 0 else [])) for i in range(30)], price=0.4)
    g = an.group_stats("k", rows)
    wlo, whi = an.wilson(g["wins"], g["n"])
    assert g["cluster_ci_low"] <= round(wlo, 4) and g["cluster_ci_high"] >= round(whi, 4)
    assert g["cluster_ci_low"] <= g["win_rate"] <= g["cluster_ci_high"]
    assert g["roi_ci_low"] <= g["roi"] <= g["roi_ci_high"]
    feed = [dict(r, fee=0.01) for r in rows]
    assert an.group_stats("k", feed)["roi_ci_low"] < g["roi_ci_low"]

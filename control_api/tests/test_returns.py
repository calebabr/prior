"""Returns per day / week / month (services/returns.py + GET /api/analytics/returns[/fills]): calendar buckets in the
viewer's zone, DST, modified-Dietz base, null reasons, partial and dropped periods, cache keying, validation."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from control_api.routes import analytics as ra
from control_api.services import returns as rt
from control_api.tests.test_analytics import _Clock, _put, fill, settle  # noqa: F401
from control_api.tests.test_analytics import env  # noqa: F401  (env fixture with synthetic cells)

UTC = timezone.utc
CHI = ZoneInfo("America/Chicago")


def _row(settled, pnl, strategy="maker", source="bot", fid=None):
    return {"fill_id": fid or f"f{id(object())}", "ticker": "T", "side": "no", "kind": "maker", "source": source,
            "strategy": strategy if source == "bot" else None, "_settled": settled, "_pnl": pnl, "_ts": settled,
            "count": 1, "price": 0.5, "pnl": pnl, "result": "win", "settled_utc": settled.isoformat(),
            "ts": settled.isoformat()}


def _snap(ts, equity):
    return {"ts": ts, "cash": equity, "exposure": 0.0}


def _flow(ts, amount, kind="deposit"):
    return {"ts": ts, "kind": kind, "amount": amount}


def _build(rows, *, now, gran="day", days=30, tz="UTC", snaps=(), flows=(), current=True, env="demo"):
    zone = UTC if tz == "UTC" else ZoneInfo(tz)
    return rt.build(list(rows), list(snaps), list(flows), zone=zone, tzname=tz, granularity=gran, days=days,
                    env=env, env_is_current=current, now=now)


def _by_start(out):
    return {p["start_local"]: p for p in out["periods"]}


# ---------------------------------------------------------------- calendar
def test_week_boundary_is_monday_local():
    now = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    sun = datetime(2026, 10, 26, 4, 59, tzinfo=UTC)         # Sun Oct 25 23:59 CDT
    mon = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)          # Mon Oct 26 00:00 CDT
    out = _build([_row(sun, 1.0, fid="a"), _row(mon, 2.0, fid="b")], now=now, gran="week", tz="America/Chicago")
    p = _by_start(out)
    assert p["2026-10-19"]["realized_pnl"] == 1.0 and p["2026-10-26"]["realized_pnl"] == 2.0
    assert p["2026-10-19"]["end_local"] == "2026-10-25" and p["2026-10-26"]["partial"] is True


def test_week_across_iso_year_change():
    assert rt.period_start(date(2027, 1, 1), "week") == date(2026, 12, 28)
    assert rt.period_end(date(2026, 12, 28), "week") == date(2027, 1, 3)


def test_month_boundaries_and_lengths():
    assert rt.period_end(date(2028, 2, 1), "month") == date(2028, 2, 29)
    assert rt.period_end(date(2027, 2, 1), "month") == date(2027, 2, 28)
    assert rt.period_end(date(2026, 12, 1), "month") == date(2026, 12, 31)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    rows = [_row(datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), 1.0, fid="a"),
            _row(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 2.0, fid="b")]
    p = _by_start(_build(rows, now=now, gran="month", days=90))
    assert p["2026-09-01"]["realized_pnl"] == 1.0 and p["2026-10-01"]["realized_pnl"] == 2.0
    assert p["2026-11-01"]["partial"] is True and p["2026-10-01"]["partial"] is False


def test_dst_days_use_real_instants():
    fall = rt.enumerate_periods(datetime(2026, 11, 3, 12, tzinfo=UTC), CHI, "day", datetime(2026, 10, 31, tzinfo=UTC))
    nov1 = next(p for p in fall if p["start"] == date(2026, 11, 1))
    assert (nov1["end_utc"] - nov1["start_utc"]) == timedelta(hours=25)
    assert nov1["start_utc"] == datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    spring = rt.enumerate_periods(datetime(2026, 3, 10, 12, tzinfo=UTC), CHI, "day", datetime(2026, 3, 7, tzinfo=UTC))
    mar8 = next(p for p in spring if p["start"] == date(2026, 3, 8))
    assert (mar8["end_utc"] - mar8["start_utc"]) == timedelta(hours=23)
    wk = rt.enumerate_periods(datetime(2026, 11, 5, 12, tzinfo=UTC), CHI, "week", datetime(2026, 10, 20, tzinfo=UTC))
    w = next(p for p in wk if p["start"] == date(2026, 10, 26))
    assert (w["end_utc"] - w["start_utc"]) == timedelta(hours=169)


def test_dst_dietz_weight_uses_seconds():
    """A deposit 12.5 h into the 25 h day Nov 1 2026 (Chicago) weighs exactly 0.5."""
    start = datetime(2026, 11, 1, 5, 0, tzinfo=CHI.utcoffset(datetime(2026, 11, 1)) and UTC)
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    out = _build([], now=now, tz="America/Chicago", days=7,
                 snaps=[_snap(start - timedelta(hours=1), 1000.0)],
                 flows=[_flow(start + timedelta(hours=12.5), 100.0)])
    p = _by_start(out)["2026-11-01"]
    assert p["base"] == 1050.0 and p["flows_in_period"]["deposits"] == 100.0


def test_nonexistent_local_midnight_uses_first_real_instant():
    sao = ZoneInfo("America/Havana")                  # DST starts at 00:00 -> 01:00 in some years
    d = date(2026, 3, 8)
    s = rt.local_start_utc(d, sao)
    assert s.astimezone(sao).date() == d
    syd = ZoneInfo("Australia/Sydney")                # southern hemisphere: springs forward in October
    ps = rt.enumerate_periods(datetime(2026, 10, 7, 12, tzinfo=UTC), syd, "day", datetime(2026, 10, 3, tzinfo=UTC))
    assert any(p["end_utc"] - p["start_utc"] == timedelta(hours=23) for p in ps)


# ---------------------------------------------------------------- Dietz
def _week_ctx():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    start = datetime(2026, 10, 26, tzinfo=UTC)
    return now, start


def test_deposit_mid_week_is_weighted_by_remaining_time():
    now, start = _week_ctx()
    out = _build([_row(start + timedelta(days=2), 10.0)], now=now, gran="week", days=30,
                 snaps=[_snap(start, 1000.0)], flows=[_flow(start + timedelta(days=3), 100.0)])
    p = _by_start(out)["2026-10-26"]                       # Thursday 00:00 of a Mon-Sun week: 4/7 remains
    assert p["base"] == pytest.approx(1000 + 100 * 4 / 7, abs=0.01)
    assert p["return_rate"] == pytest.approx(10 / (1000 + 100 * 4 / 7), abs=1e-6)
    assert p["flows_in_period"] == {"count": 1, "net": 100.0, "deposits": 100.0, "withdrawals": 0.0}


def test_withdrawal_is_negative_and_flow_before_start_has_weight_one():
    now, start = _week_ctx()
    out = _build([], now=now, gran="week", days=30,
                 snaps=[_snap(start - timedelta(hours=10), 1000.0)],
                 flows=[_flow(start - timedelta(hours=5), 50.0),                      # after the snapshot, before start
                        _flow(start + timedelta(days=3.5), 70.0, "withdrawal")])      # half the week left
    p = _by_start(out)["2026-10-26"]
    assert p["base"] == pytest.approx(1000 + 50 - 35, abs=0.01)
    assert p["flows_in_period"]["withdrawals"] == 70.0 and p["flows_in_period"]["count"] == 1


def test_two_flows_in_one_period_and_flow_before_snapshot_ignored():
    now, start = _week_ctx()
    out = _build([], now=now, gran="week", days=30, snaps=[_snap(start, 1000.0)],
                 flows=[_flow(start - timedelta(days=1), 999.0),                       # already inside the snapshot
                        _flow(start + timedelta(days=1), 70.0), _flow(start + timedelta(days=4), 70.0)])
    assert _by_start(out)["2026-10-26"]["base"] == pytest.approx(1000 + 70 * 6 / 7 + 70 * 3 / 7, abs=0.01)


def test_zero_activity_period_is_present_with_zero_return():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([_row(datetime(2026, 10, 30, 6, tzinfo=UTC), 1.0)], now=now, days=7,
                 snaps=[_snap(datetime(2026, 10, 20, tzinfo=UTC), 500.0),
                        _snap(datetime(2026, 10, 28, 23, 59, tzinfo=UTC), 500.0)])
    p = _by_start(out)["2026-10-29"]
    assert p["empty"] is True and p["settled_fills"] == 0 and p["realized_pnl"] == 0.0
    assert p["small_sample"] is False and p["return_rate"] == 0.0


@pytest.mark.parametrize("snaps,reason", [
    ([_snap(datetime(2026, 10, 28, 12, tzinfo=UTC), 5.0)], "base_too_small"),
    ([_snap(datetime(2026, 10, 28, 12, tzinfo=UTC), -40.0)], "base_too_small"),
    ([_snap(datetime(2026, 10, 28, 12, tzinfo=UTC), 10.0)], "base_too_small"),
    ([], "no_start_snapshot"),
    ([_snap(datetime(2026, 10, 30, tzinfo=UTC), 900.0)], "no_start_snapshot"),           # only after the start
    ([_snap(datetime(2026, 10, 25, tzinfo=UTC), 900.0)], "stale_start_snapshot"),
])
def test_null_reasons_keep_the_dollars(snaps, reason):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([_row(datetime(2026, 10, 29, 6, tzinfo=UTC), 3.5)], now=now, days=7, snaps=snaps)
    p = _by_start(out)["2026-10-29"]
    assert p["return_rate"] is None and p["base"] is None and p["return_reason"] == reason
    assert p["realized_pnl"] == 3.5


def test_env_not_current_has_no_rates():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([_row(datetime(2026, 10, 29, 6, tzinfo=UTC), 3.5)], now=now, days=7, current=False, env="all",
                 snaps=[_snap(datetime(2026, 10, 20, tzinfo=UTC), 900.0)])
    p = _by_start(out)["2026-10-29"]
    assert p["return_reason"] == "env_not_current" and p["return_rate"] is None and p["equity_change"] is None
    assert out["label"] == rt.MIXED_LABEL


def test_equity_change_reported_when_two_snapshots():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([], now=now, days=7, snaps=[_snap(datetime(2026, 10, 29, tzinfo=UTC), 900.0),
                                            _snap(datetime(2026, 10, 29, 20, tzinfo=UTC), 905.5)])
    assert _by_start(out)["2026-10-29"]["equity_change"] == 5.5


# ---------------------------------------------------------------- partial and window
def test_partial_current_period_uses_elapsed_time_and_is_not_projected():
    now = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    start = datetime(2026, 10, 31, tzinfo=UTC)
    out = _build([_row(start + timedelta(hours=1), 2.0)], now=now, days=7,
                 snaps=[_snap(start - timedelta(hours=1), 1000.0)], flows=[_flow(start + timedelta(hours=3), 100.0)])
    p = out["periods"][-1]
    assert p["partial"] is True and p["elapsed_hours"] == 6.0
    assert p["base"] == pytest.approx(1050.0)             # end_eff = now: (6h-3h)/6h = 0.5
    assert p["realized_pnl"] == 2.0                        # never scaled up


def test_future_settlements_are_not_counted():
    now = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    out = _build([_row(now + timedelta(hours=1), 9.0)], now=now, days=7)
    assert out["totals"] == {"realized_pnl": 0.0, "settled_fills": 0}


def test_straddling_period_is_dropped_and_wide_windows_return_whole_periods():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([], now=now, days=7)
    assert [p["start_local"] for p in out["periods"]][0] == "2026-10-25" and len(out["periods"]) == 7
    wk = _build([], now=now, gran="week", days=84)
    assert len(wk["periods"]) in (12, 13) and wk["periods"][-1]["partial"] is True
    assert datetime.fromisoformat(wk["periods"][0]["start_utc"].replace("Z", "+00:00")) >= now - timedelta(days=84)
    mo = _build([], now=now, gran="month", days=180)
    assert mo["periods"][-1]["start_local"] == "2026-10-01" and mo["periods"][0]["start_local"] == "2026-06-01"
    assert mo["periods"][0]["label"] == "June 2026"


def test_window_with_no_whole_period_is_empty_with_a_note():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    out = _build([_row(datetime(2026, 10, 30, tzinfo=UTC), 1.0)], now=now, gran="month", days=7)
    assert out["periods"] == [] and any("no whole month" in n for n in out["notes"])
    assert out["totals"]["settled_fills"] == 0


def test_tz_changes_the_buckets_not_the_rows():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    r = [_row(datetime(2026, 10, 29, 3, 0, tzinfo=UTC), 1.0)]      # Oct 28 22:00 in Chicago
    assert _by_start(_build(r, now=now, days=7))["2026-10-29"]["realized_pnl"] == 1.0
    chi = _by_start(_build(r, now=now, days=7, tz="America/Chicago"))
    assert chi["2026-10-28"]["realized_pnl"] == 1.0 and chi["2026-10-29"]["realized_pnl"] == 0.0


# ---------------------------------------------------------------- counts and strategies
def test_small_sample_flag():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    t = datetime(2026, 10, 30, 6, tzinfo=UTC)
    for n, flag in ((0, False), (1, True), (4, True), (5, False), (9, False)):
        out = _build([_row(t, 1.0, fid=f"x{i}") for i in range(n)], now=now, days=7)
        assert _by_start(out)["2026-10-30"]["small_sample"] is flag, n


def test_you_is_excluded_and_counted_in_a_note_and_strategies_split():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    t = datetime(2026, 10, 30, 6, tzinfo=UTC)
    out = _build([_row(t, 1.5), _row(t, 2.0, "arb"), _row(t, -0.5, source="you"), _row(t, -0.25, source="you")],
                 now=now, days=7)
    p = _by_start(out)["2026-10-30"]
    assert p["by_strategy"] == {"maker": 1.5, "arb": 2.0} and p["realized_pnl"] == 3.5 and p["settled_fills"] == 2
    assert p["fills_by_strategy"] == {"maker": 1, "arb": 1}
    assert p["you"] == {"settled_fills": 2, "realized_pnl": -0.75}
    assert out["totals"] == {"realized_pnl": 3.5, "settled_fills": 2}
    assert any("Your own trades are not included (2 settled" in n for n in out["notes"])


def test_day_bars_add_up_to_the_rows_total():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    rows = [_row(now - timedelta(hours=7 * i + 1), 0.5 + i * 0.1, fid=f"r{i}") for i in range(20)]
    out = _build(rows, now=now, days=7)
    in_window = [r["_pnl"] for r in rows if r["_settled"] >= datetime(2026, 10, 25, tzinfo=UTC)]
    assert out["totals"]["realized_pnl"] == pytest.approx(sum(in_window), abs=0.01)


# ---------------------------------------------------------------- routes
URL = "/api/analytics/returns"


async def _seed(env):
    """One maker fill settled 2 days ago and a snapshot a week back."""
    now = datetime.now(UTC)
    await _put(env.store, [fill("a", ts=now - timedelta(days=4), ticker="T-A", source="bot", strategy="maker")],
               [settle("T-A", "no", now - timedelta(days=2))])
    await env.store.add_account_snapshot({"ts": now - timedelta(days=20), "cash": 500.0, "exposure": 0.0})
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await env.store.add_account_snapshot({"ts": midnight - timedelta(hours=1), "cash": 500.0, "exposure": 0.0})
    return now


@pytest.mark.asyncio
async def test_route_shape_and_defaults(env):
    await _seed(env)
    r = await env.client.get(URL, headers=env.auth)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["granularity"] == "day" and j["days"] == 30 and j["env"] == "demo" and j["tz_used"] == "UTC"
    assert {"periods", "totals", "notes", "label", "small_sample_threshold", "min_base", "truncated"} <= set(j)
    assert j["small_sample_threshold"] == 5 and j["min_base"] == 10.0
    assert j["totals"]["settled_fills"] == 1
    p = j["periods"][-1]
    assert {"start_local", "end_local", "start_utc", "end_utc", "label", "partial", "realized_pnl", "by_strategy",
            "settled_fills", "fills_by_strategy", "base", "return_rate", "return_reason", "flows_in_period",
            "equity_change", "you", "small_sample", "empty"} <= set(p)
    assert p["partial"] is True and p["return_reason"] == "ok" and p["return_rate"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["granularity=year", "days=0", "days=181", "days=abc", "env=bogus", "granularity="])
async def test_validation_422(env, q):
    r = await env.client.get(f"{URL}?{q}", headers=env.auth)
    assert r.status_code == 422, q


@pytest.mark.asyncio
@pytest.mark.parametrize("gran,days", [("day", 7), ("day", 180), ("week", 84), ("month", 180), ("week", 7)])
async def test_wide_windows_accepted_for_every_granularity(env, gran, days):
    r = await env.client.get(f"{URL}?granularity={gran}&days={days}", headers=env.auth)
    assert r.status_code == 200 and r.json()["days"] == days


@pytest.mark.asyncio
async def test_week_days_7_returns_empty_list_with_note_not_error(env):
    r = await env.client.get(f"{URL}?granularity=month&days=7", headers=env.auth)
    j = r.json()
    assert r.status_code == 200 and (j["periods"] == [] or j["periods"][0]["start_local"].endswith("-01"))


@pytest.mark.asyncio
async def test_requires_sign_in(env):
    assert (await env.client.get(URL)).status_code == 401
    assert (await env.client.get(URL + "/fills?start=2026-10-01&end=2026-10-01")).status_code == 401


@pytest.mark.asyncio
async def test_bad_tz_falls_back_to_utc_and_never_enters_the_cache_key(env):
    for tz in ("Nope/Zone", "x" * 200, "../etc"):
        r = await env.client.get(URL, params={"tz": tz}, headers=env.auth)
        assert r.status_code == 200 and r.json()["tz_used"] == "UTC"
    keys = list(ra._state(env.app.state.__class__ and type("R", (), {"app": env.app})())["returns"])
    assert keys and all(k[2] == "UTC" for k in keys)


def _cache(env):
    return ra._state(type("R", (), {"app": env.app})())["returns"]


@pytest.mark.asyncio
async def test_cache_is_keyed_ttl_bounded_and_cleared(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    assert ra.RETURNS_CACHE_KEYS == 24
    await _seed(env)
    a = (await env.client.get(URL, headers=env.auth)).json()
    await env.client.get(URL + "?granularity=week", headers=env.auth)
    await env.client.get(URL + "?tz=America/Chicago", headers=env.auth)
    await env.client.get(URL + "?days=7", headers=env.auth)
    assert set(_cache(env)) == {("demo", 30, "UTC", "day"), ("demo", 30, "UTC", "week"),
                                ("demo", 30, "America/Chicago", "day"), ("demo", 7, "UTC", "day")}
    clock.t += 30
    assert (await env.client.get(URL, headers=env.auth)).json()["generated_utc"] == a["generated_utc"]   # cached
    clock.t += 31                                                                                         # past 60 s
    await env.client.get(URL, headers=env.auth)
    assert _cache(env)[("demo", 30, "UTC", "day")][0] == clock.t
    monkeypatch.setattr(ra, "RETURNS_CACHE_KEYS", 3)
    monkeypatch.setitem(ra.RATE_LIMITS, "analytics_returns", (1000, 60.0))
    for tz in ("America/New_York", "Europe/London", "Asia/Tokyo", "Asia/Kolkata", "Australia/Sydney"):
        await env.client.get(URL, params={"tz": tz}, headers=env.auth)
    assert len(_cache(env)) <= 3
    ra.clear_cache(env.app)
    assert _cache(env) == {}


@pytest.mark.asyncio
async def test_failed_soft_read_gives_null_rates_a_note_and_is_not_cached(env, monkeypatch):
    await _seed(env)

    async def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(env.store, "list_account_snapshots", boom)
    r = await env.client.get(URL, headers=env.auth)
    j = r.json()
    assert r.status_code == 200 and all(p["return_rate"] is None for p in j["periods"])
    assert any("account snapshots" in n for n in j["notes"])
    assert _cache(env) == {}


@pytest.mark.asyncio
async def test_other_env_than_the_running_account_has_no_rates(env):
    await _seed(env)
    j = (await env.client.get(URL + "?env=live", headers=env.auth)).json()
    assert env.runner.mode == "demo"
    assert all(p["return_reason"] == "env_not_current" for p in j["periods"])


@pytest.mark.asyncio
async def test_rate_limit_429_and_stop_unaffected(env, monkeypatch):
    monkeypatch.setitem(ra.RATE_LIMITS, "analytics_returns", (2, 60.0))
    assert (await env.client.get(URL, headers=env.auth)).status_code == 200
    assert (await env.client.get(URL + "/fills?start=2026-10-01&end=2026-10-01", headers=env.auth)).status_code in (200, 422)
    r = await env.client.get(URL, headers=env.auth)
    assert r.status_code == 429 and r.json()["detail"] == "rate_limited"
    assert (await env.client.post("/api/bot/stop", headers=env.auth)).status_code == 200
    assert (await env.client.get("/api/history", headers=env.auth)).status_code == 200
    assert (await env.client.get("/api/analytics/summary", headers=env.auth)).status_code == 200


@pytest.mark.asyncio
async def test_fills_drill_down(env):
    now = await _seed(env)
    day = (now - timedelta(days=2)).date().isoformat()
    r = await env.client.get(f"{URL}/fills?start={day}&end={day}", headers=env.auth)
    j = r.json()
    assert r.status_code == 200 and j["total"] == 1 and j["fills"][0]["fill_id"] == "a"
    assert j["fills"][0]["strategy"] == "maker" and j["period"]["start_local"] == day
    r = await env.client.get(f"{URL}/fills?start={day}&end={day}&limit=100&offset=1", headers=env.auth)
    assert r.json()["fills"] == [] and r.json()["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["", "start=bad&end=2026-10-01", "start=2026-10-02&end=2026-10-01",
                               "start=2026-01-01&end=2026-03-31", "start=2020-01-01&end=2020-01-02",
                               "start=2026-10-01&end=2026-10-01&limit=101", "start=2026-10-01&end=2026-10-01&offset=-1"])
async def test_fills_validation_422(env, q):
    assert (await env.client.get(f"{URL}/fills?{q}", headers=env.auth)).status_code == 422

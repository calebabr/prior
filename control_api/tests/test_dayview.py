"""GET /api/analytics/day (Day view): local-day boundaries in a non-UTC zone, settles_later, empty day, validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.services import returns as retsvc
from control_api.services import pnl as pnlsvc
from control_api.tests.test_analytics import _ep, _put, _z, fill, settle  # noqa: F401
from control_api.tests.test_analytics import env  # noqa: F401  (env fixture with synthetic cells)

UTC = timezone.utc
URL = "/api/analytics/day"
TZ = "Pacific/Kiritimati"                                   # UTC+14: the local day is far from the UTC day


def _today():
    zone, _ = pnlsvc.resolve_tz(TZ)
    now = datetime.now(UTC)
    day = now.astimezone(zone).date()
    return zone, now, day, retsvc.local_start_utc(day, zone)


async def _get(env, day, **extra):
    r = await env.client.get(URL, params={"date": day.isoformat(), "tz": TZ, **extra}, headers=env.auth)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_requires_auth(env):
    assert (await env.client.get(URL, params={"date": "2026-01-01"})).status_code == 401


@pytest.mark.asyncio
async def test_empty_day_has_the_full_shape(env):
    _z_, _n, day, _lo = _today()
    j = await _get(env, day)
    assert j["date"] == day.isoformat() and j["tz_used"] == TZ and j["env"] == "demo"
    assert j["summary"] == {"filled": {"count": 0, "cost": 0.0}, "settled": {"count": 0, "wins": 0, "losses": 0},
                            "realized": 0.0, "settles_later": 0}
    assert j["filled"] == [] and j["settled"] == []
    assert [a["key"] for a in j["analytics"]] == ["fill_rate", "maker_share", "win_rate"]
    assert all(a["today"] is None for a in j["analytics"])


@pytest.mark.asyncio
async def test_day_boundaries_use_the_viewer_zone_and_settles_later(env):
    zone, now, day, lo = _today()
    mid = lo + (now - lo) / 4                                # a fill early in today's local day
    before = lo - timedelta(minutes=30)                      # yesterday, local
    fills = [fill("a", ts=mid, ticker="KXDEMOB-26X-A", source="bot", strategy="maker"),
             fill("b", ts=before, ticker="KXDEMOB-26X-B", source="bot", strategy="maker"),
             fill("c", ts=mid, ticker="KXDEMOB-26X-C", source="bot", strategy="arb", side="no",
                  close_time=_z(now + timedelta(hours=5))),
             fill("y", ts=mid, ticker="KXDEMOB-26X-Y", source="you")]
    # a settles today (loss: NO side, market resolved yes); b filled yesterday but settles today (win)
    setts = [settle("KXDEMOB-26X-A", "yes", mid + (now - mid) / 2), settle("KXDEMOB-26X-B", "no", mid)]
    await _put(env.store, fills, setts)
    j = await _get(env, day)
    assert {f["fill_id"] for f in j["filled"]} == {"a", "c"}                  # b is yesterday, y is not the bot
    assert {s["fill_id"] for s in j["settled"]} == {"a", "b"}
    s = j["summary"]
    assert s["filled"]["count"] == 2 and s["settled"] == {"count": 2, "wins": 1, "losses": 1}
    assert s["settles_later"] == 1
    c = next(f for f in j["filled"] if f["fill_id"] == "c")
    assert c["status"] == "open" and c["settles_at"] is not None and c["strategy"] == "arb"
    assert next(f for f in j["filled"] if f["fill_id"] == "a")["status"] == "lost"
    assert s["realized"] == round(sum(x["pnl"] for x in j["settled"]), 2)
    # the previous local day holds b's fill only
    prev = await _get(env, day - timedelta(days=1))
    assert [f["fill_id"] for f in prev["filled"]] == ["b"]
    # strategy filter
    only = await _get(env, day, strategy="arb")
    assert [f["fill_id"] for f in only["filled"]] == ["c"] and only["settled"] == []


@pytest.mark.asyncio
async def test_analytics_rows_and_fill_rate(env):
    _z_, now, day, lo = _today()
    mid = lo + (now - lo) / 4
    await _put(env.store, [fill("a", ts=mid, ticker="KXDEMOB-26X-A", source="bot", strategy="maker")], (),
               [_ep("e1", joined=_z(mid), outcome="filled", filled=5.0), _ep("e2", joined=_z(mid), outcome="expired")])
    j = await _get(env, day)
    fr = next(a for a in j["analytics"] if a["key"] == "fill_rate")
    assert fr["today"] == 0.5 and fr["avg7"] is None
    assert next(a for a in j["analytics"] if a["key"] == "maker_share")["today"] == 1.0


@pytest.mark.asyncio
async def test_validation(env):
    _z_, _n, day, _lo = _today()
    for params in ({"date": "nope"}, {"date": (day + timedelta(days=3)).isoformat()},
                   {"date": "2001-01-01"}, {"date": day.isoformat(), "strategy": "zzz"},
                   {"date": day.isoformat(), "env": "zzz"}):
        r = await env.client.get(URL, params={"tz": TZ, **params}, headers=env.auth)
        assert r.status_code == 422, params

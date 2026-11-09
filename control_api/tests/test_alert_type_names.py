"""S3 item 8: `weekly` and `arb_legging` are stored, filtered, grouped and delivered under their own names."""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from control_api.db import MemoryStore
from control_api.services import alerts as al

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)


async def _drain(runner):
    for _ in range(40):
        if not runner._bg:
            return
        await asyncio.sleep(0.01)


async def test_legging_alert_stored_under_its_own_type(env):
    a = al.legging_alert({"kind": "partial", "fills": {"A-1": 1}, "event": "E-1"}, None, None)
    await env.runner.alerter.send_alert(a)
    await _drain(env.runner)
    got = await env.store.list_alerts(type="arb_legging", limit=10)
    assert len(got) == 1 and got[0]["priority"] == "urgent"
    assert await env.store.list_alerts(type="breaker", limit=10) == []


async def test_weekly_summary_stored_under_its_own_type(env):
    st = MemoryStore()
    sun = T0 + timedelta(hours=1, minutes=30)
    assert await al.maybe_send_weekly(env.runner.alerter, st, sun, env.runner.cloud) is True
    await _drain(env.runner)
    got = await env.store.list_alerts(type="weekly", limit=10)
    assert len(got) == 1 and got[0]["title"] == "Weekly Summary"
    assert await env.store.list_alerts(type="daily", limit=10) == []


async def test_filter_route_accepts_new_types_and_still_lists_old_rows(env):
    for t in ("breaker", "daily", "weekly", "arb_legging"):
        await env.store.add_alert({"type": t, "title": f"t-{t}", "body": "b", "priority": "default", "channels": []})
    for t in ("weekly", "arb_legging", "breaker", "daily"):
        r = await env.client.get("/api/alerts", params={"type": t}, headers=env.auth)
        assert r.status_code == 200 and [x["type"] for x in r.json()] == [t]
    assert {x["type"] for x in await env.store.list_alerts(limit=10)} == {"breaker", "daily", "weekly", "arb_legging"}


async def test_arb_legging_urgent_never_grouped_and_critical():
    g = al.AlertGrouper()
    for m in range(4):
        d = g.check("arb_legging", "Arbitrage legging risk: x", "default", now=T0 + timedelta(seconds=m))
        assert d["send"] and d["count"] == 1
    assert al.is_critical("arb_legging")
    assert al.should_push({"mode": "problems", "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"},
                           "tz": "UTC"}, "arb_legging", "urgent", T0)


async def test_weekly_is_not_critical_and_respects_quiet_hours():
    s = {"mode": "problems", "daily_summary_time": "08:00", "tz": "UTC",
         "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"}}
    assert not al.is_critical("weekly") and not al.should_push(s, "weekly", "default", T0)


async def test_ntfy_carries_priority_and_tags_for_the_legging_type():
    seen = []
    al_ = al.Alerter(types.SimpleNamespace(ntfy_topic="t", ntfy_server="http://x"),
                     transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)))
    hooked = []
    al_.on_alert = lambda *a: hooked.append(a)
    await al_.send_alert(al.legging_alert({"kind": "partial", "fills": {}, "event": "E"}, None, None))
    assert seen[0].headers["Priority"] == "urgent" and seen[0].headers["Tags"] == "rotating_light"
    assert hooked[0][0] == "arb_legging"

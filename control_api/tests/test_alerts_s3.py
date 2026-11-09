"""Sprint 2B part 3: urgent legging alert (item 5), alert grouping and the weekly summary (item 18).
Arbitrary tickers and numbers only (public repo)."""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from control_api.db import MemoryStore
from control_api.services import alerts as al


T0 = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)      # a Sunday, 17:00 in Chicago (CST), 23:00 UTC


class Capture:
    """An Alerter whose deliveries are recorded (no network)."""

    def __init__(self, grouper=None):
        self.requests: list[httpx.Request] = []

        def handler(req):
            self.requests.append(req)
            return httpx.Response(200)
        self.alerter = al.Alerter(types.SimpleNamespace(ntfy_topic="t", ntfy_server="http://x"),
                                  transport=httpx.MockTransport(handler), grouper=grouper)
        self.hooked: list[tuple] = []
        self.alerter.on_alert = lambda *a: self.hooked.append(a)


# ---------------------------------------------------------------- item 5: legging alert
def test_legging_alert_contained_fields():
    a = al.legging_alert({"kind": "contained", "fills": {"KXT-1-A": 3.0, "KXT-1-B": 0.0}, "loss": 0.42,
                          "event": "KXT-1"}, "2026-11-09", 1.0)
    assert a["type"] == "arb_legging" and a["priority"] == "urgent"
    assert a["title"].startswith("Arbitrage legging risk") and al.classify(a["title"], "urgent") == "arb_legging"
    b = a["body"]
    assert "KXT-1 " in b and "KXT-1-A (3)" in b and "Not filled: KXT-1-B" in b
    assert "$0.42" in b and "$1.00" in b and "until 2026-11-09" in b
    assert "did not trip" in b and "Next:" in b


@pytest.mark.parametrize("parsed,cap", [
    ({"kind": "partial", "fills": {"A-1": 1}, "loss": None, "event": "E-1"}, 1.0),
    ({"kind": "error", "fills": {}, "loss": None, "event": "E-1"}, 1.0),
    ({"kind": "contained", "fills": {"A-1": 1}, "loss": 2.5, "event": "E-1"}, 1.0),       # over the cap
    ({"kind": "contained", "fills": {"A-1": 1}, "loss": 0.1, "event": "E-1"}, None),       # no cap
    ({"kind": "contained", "fills": {"A-1": 1}, "loss": float("nan"), "event": "E-1"}, 1.0),
    ({"kind": "weird"}, 1.0), ({}, 1.0),
])
def test_legging_alert_uncontained_trips_kill_switch(parsed, cap):
    a = al.legging_alert(parsed, "2026-11-09", cap)
    assert a["type"] == "arb_legging" and a["priority"] == "urgent"
    assert "kill switch tripped" in a["title"] and "paused until" not in a["body"]
    assert "stopped everywhere" in a["body"] and "Next:" in a["body"]


def test_legging_alert_has_no_secrets_and_never_raises():
    a = al.legging_alert({"kind": "partial", "fills": "garbage", "loss": "x", "event": "E" * 500}, None, "bad")
    assert "Which legs filled is unknown" in a["body"] and "Loss so far: unknown" in a["body"]
    assert len(a["body"]) < 2001 and al.legging_alert(None, None, None)["priority"] == "urgent"


@pytest.mark.asyncio
async def test_legging_alert_sends_urgent_through_the_alerter():
    c = Capture(al.AlertGrouper())
    a = al.legging_alert({"kind": "partial", "fills": {"A-1": 1}, "event": "E-1"}, None, 1.0)
    assert await c.alerter.send_alert(a) is True and await c.alerter.send_alert(a) is True
    assert len(c.requests) == 2 and c.requests[0].headers["Priority"] == "urgent"     # never grouped


# ---------------------------------------------------------------- item 18a: grouping
def test_grouper_window_and_counts():
    g = al.AlertGrouper(window_s=1800)
    d = g.check("error", "Trading pass error", "high", now=T0)
    assert d["send"] and d["count"] == 1
    for m in (1, 5, 29):
        assert g.check("error", "Trading pass error", "high", now=T0 + timedelta(minutes=m))["send"] is False
    d = g.check("error", "Trading pass error", "high", now=T0 + timedelta(minutes=30))
    assert d["send"] and d["count"] == 4                          # 3 held back + this one
    assert d["first"] == T0 + timedelta(minutes=1) and d["last"] == T0 + timedelta(minutes=30)
    # a new window started at minute 30
    assert g.check("error", "Trading pass error", "high", now=T0 + timedelta(minutes=45))["send"] is False
    d = g.check("error", "Trading pass error", "high", now=T0 + timedelta(minutes=61))
    assert d["send"] and d["count"] == 2


def test_grouper_keys_and_types_are_separate():
    g = al.AlertGrouper()
    assert g.check("error", "A", "high", now=T0)["send"]
    assert g.check("error", "B", "high", now=T0)["send"]                      # other title
    assert g.check("system", "A", "default", now=T0)["send"]                  # other type
    assert not g.check("error", "A", "high", now=T0 + timedelta(minutes=1))["send"]
    assert g.check("error", "Order errors: 1", "high", key="orders", now=T0)["send"]
    assert not g.check("error", "Order errors: 7", "high", key="orders", now=T0 + timedelta(minutes=1))["send"]


@pytest.mark.parametrize("kind,title,prio", [
    ("error", "Anything", "urgent"), ("breaker", "Arbitrage legging risk: x", "high"), ("arb_legging", "Arbitrage legging risk: x", "default"), ("stop", "Bot stopped", "default"),
    ("system", "Arbitrage stopped: Kill Switch", "default")])
def test_urgent_and_kill_switch_never_grouped(kind, title, prio):
    g = al.AlertGrouper()
    for m in range(4):
        d = g.check(kind, title, prio, now=T0 + timedelta(seconds=m))
        assert d["send"] and d["count"] == 1
    assert g.due(T0 + timedelta(hours=2)) == []


@pytest.mark.asyncio
async def test_alerter_groups_and_appends_the_count():
    c = Capture(al.AlertGrouper())
    assert await c.alerter.send("Trading pass error", "boom", priority="high") is True
    for _ in range(3):
        assert await c.alerter.send("Trading pass error", "boom", priority="high") is True      # held back
    assert len(c.requests) == 1 and len(c.hooked) == 1
    # force the window closed, then the next repeat carries the count
    c.alerter.grouper._g[("error", "Trading pass error")]["start"] -= timedelta(hours=1)
    await c.alerter.send("Trading pass error", "boom", priority="high")
    assert len(c.requests) == 2
    assert b"Repeated 4 times" in c.requests[1].content and b"boom" in c.requests[1].content


@pytest.mark.asyncio
async def test_alerter_without_grouper_is_unchanged_and_urgent_always_sends():
    c = Capture()
    for _ in range(3):
        await c.alerter.send("Trading pass error", "boom", priority="high")
    assert len(c.requests) == 3
    c2 = Capture(al.AlertGrouper())
    for _ in range(3):
        await c2.alerter.send("Arbitrage stopped: kill switch", "r", priority="urgent")
    assert len(c2.requests) == 3


@pytest.mark.asyncio
async def test_flush_groups_reports_a_quiet_tail_once():
    c = Capture(al.AlertGrouper())
    for _ in range(3):
        await c.alerter.send("Self-ping failed", "down", priority="high")
    assert len(c.requests) == 1 and await c.alerter.flush_groups() == 0           # window still open
    c.alerter.grouper._g[("error", "Self-ping failed")]["start"] -= timedelta(hours=1)
    assert await c.alerter.flush_groups() == 1 and await c.alerter.flush_groups() == 0
    assert len(c.requests) == 2 and b"Repeated 2 times" in c.requests[1].content
    assert c.requests[1].headers["Title"] == "Self-ping failed"


def test_grouper_bounds_memory():
    g = al.AlertGrouper(max_keys=5)
    for i in range(50):
        g.check("system", f"t{i}", "default", now=T0)
    assert len(g._g) <= 5


# ---------------------------------------------------------------- item 18b: weekly summary
def _fill(fid, ticker, strategy, ts, *, side="yes", price="0.90", count="10", env="demo"):
    return {"fill_id": fid, "ticker": ticker, "side": side, f"{side}_price_dollars": price, "count_fp": count,
            "fee_cost": "0", "source": "bot", "strategy": strategy, "ts": ts, "env": env, "is_taker": strategy == "arb"}


async def _seed(mode="demo"):
    st = MemoryStore()
    await st.set_state(by="t", mode=mode)
    def d(days):
        return T0 - timedelta(days=days)
    await st.upsert_fills([
        _fill("m1", "KXA-1", "maker", d(2)), _fill("m2", "KXB-1", "maker", d(3)),
        _fill("m3", "KXC-1", "maker", d(1)),                                  # still open
        _fill("a1", "KXD-1", "arb", d(2)),
        _fill("old", "KXE-1", "maker", d(20)),                                # outside 7 days, settled long ago
        _fill("live", "KXF-1", "maker", d(1), env="live"),                    # other env: ignored
    ])
    await st.upsert_settlements([
        {"ticker": "KXA-1", "market_result": "yes", "ts": d(1)},              # +1.00 on 10 @ 0.90
        {"ticker": "KXB-1", "market_result": "no", "ts": d(2)},               # -9.00
        {"ticker": "KXD-1", "market_result": "yes", "ts": d(1)},              # arb +1.00
        {"ticker": "KXE-1", "market_result": "yes", "ts": d(15)},
    ])
    for ex, mode_ in (("FILLED {'a': 1}", "demo"), ("FILLED {'a': 1}", "demo"), ("PARTIAL {'a': 1}", "demo"),
                      ("skipped: no depth", "demo"), ("FILLED {'z': 1}", "live")):
        await st.add_arb_opportunity({"event": "KXD-1", "kind": "NO", "executed": ex, "mode": mode_, "ts": d(1),
                                      "last_seen": d(1), "sig": ex + mode_ + str(id(ex)), "legs": [], "cost": 1.0,
                                      "profit": 0.1, "contracts": 1})
    await st.add_alert({"type": "stop", "title": "Bot stopped", "ts": d(1)})
    await st.add_alert({"type": "error", "title": "Trading pass error", "ts": d(2)})
    await st.add_alert({"type": "error", "title": "Trading pass error", "ts": d(3)})
    await st.add_alert({"type": "error", "title": "old", "ts": d(30)})
    await st.add_account_snapshot({"ts": d(1), "cash": 90.5, "exposure": 9.5, "env": mode})
    return st


@pytest.mark.asyncio
async def test_weekly_summary_text_demo():
    st = await _seed("demo")
    rep = await al.weekly_summary(st, T0, "America/Chicago")
    t = rep["text"]
    assert rep["demo"] is True and "demo mode (demo, indicative only)" in t
    assert "Bot · Maker: settled P&L -$8.00 (demo, indicative only), 3 fills, 2 settled." in t
    assert "Bot · Arb: settled P&L +$1.00 (demo, indicative only), 1 fills, 1 settled." in t
    assert "Arbitrage sets: 2 filled, 1 partial, 1 skipped." in t           # the live-mode doc is not counted
    assert "Stops: 1. Errors: 2." in t
    assert "Cash $90.50, exposure $9.50." in t
    assert "Open positions: 1 maker, 0 arb markets." in t
    assert rep["strategies"]["maker"]["pnl"] == -8.0 and rep["arb"]["filled"] == 2


@pytest.mark.asyncio
async def test_weekly_summary_live_has_no_demo_label_and_survives_a_failing_store():
    st = await _seed("live")
    rep = await al.weekly_summary(st, T0, "UTC")
    assert rep["demo"] is False and "indicative" not in rep["text"]
    assert "Arbitrage sets: 1 filled, 0 partial, 0 skipped." in rep["text"]

    class Broken(MemoryStore):
        async def list_fills(self, **k):
            raise RuntimeError("down")

        async def list_alerts(self, **k):
            raise NotImplementedError
    rep = await al.weekly_summary(Broken(), T0, None)
    assert "settled P&L n/a" in rep["text"] and "Stops: n/a. Errors: n/a." in rep["text"]


@pytest.mark.asyncio
async def test_maybe_send_weekly_once_per_iso_week_and_survives_restart():
    st = await _seed("demo")
    c = Capture()
    cloud = {"alerts": {"tz": "America/Chicago"}, "alerts_state": {"daily_sent": "2026-11-08"}}
    sat = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)
    assert await al.maybe_send_weekly(c.alerter, st, sat, cloud) is False                    # a Saturday
    assert await al.maybe_send_weekly(c.alerter, st, T0, cloud) is False                     # Sunday 17:00 local
    assert c.hooked == []
    sun = T0 + timedelta(hours=1, minutes=30)                                                # Sunday 18:30 local
    assert await al.maybe_send_weekly(c.alerter, st, sun, cloud) is True
    assert len(c.hooked) == 1
    kind, title, body, prio, ch = c.hooked[0]
    assert (kind, title, prio, ch) == ("weekly", "Weekly Summary", "default", [])
    assert "Arbitrage sets:" in body and "demo, indicative only" in body
    assert cloud["alerts_state"] == {"daily_sent": "2026-11-08", "weekly_sent": "2026-W45"}   # daily key kept
    assert await al.maybe_send_weekly(c.alerter, st, sun + timedelta(hours=2), cloud) is False
    # a restart: the runner reloads cloud settings from the store, and the guard still holds
    stored = await st.get_cloud_settings()
    assert stored["alerts_state"]["weekly_sent"] == "2026-W45"
    reloaded = {"alerts": cloud["alerts"], "alerts_state": stored["alerts_state"]}
    assert await al.maybe_send_weekly(c.alerter, st, sun + timedelta(hours=3), reloaded) is False
    assert len(c.hooked) == 1
    # next week sends again
    assert await al.maybe_send_weekly(c.alerter, st, sun + timedelta(days=7), reloaded) is True
    assert reloaded["alerts_state"]["weekly_sent"] == "2026-W46" and len(c.hooked) == 2


@pytest.mark.asyncio
async def test_maybe_send_weekly_never_raises_and_uses_the_local_zone():
    st = MemoryStore()
    c = Capture()

    class Boom:
        def notify(self, *a, **k):
            raise RuntimeError("hook down")
    # 00:30 UTC Monday is still Sunday 18:30 in Chicago, but already Monday in UTC
    sun = T0 + timedelta(hours=1, minutes=30)
    assert await al.maybe_send_weekly(Boom(), st, sun, {"alerts": {"tz": "America/Chicago"}}) is False
    assert await al.maybe_send_weekly(c.alerter, st, sun, {"alerts": {"tz": "UTC"}}) is False
    assert c.hooked == []
    assert await al.maybe_send_weekly(c.alerter, st, sun, None) is True       # default zone is Chicago
    assert len(c.hooked) == 1

"""UX sprint Y1 (fill source/strategy) and items 37/38/45 (alert history, alert settings, push hook)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from control_api.db import MemoryStore
from control_api.db import schemas as S
from control_api.services import alerts as al
from control_api.services import analytics as an
from control_api.services import pnl as pnlsvc
from control_api.services import strategies as strat

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def fill(fid, oid, *, ticker="KXDEMOB-26OCT15-B80", side="no", price="0.40", count="10", taker=False, ago_h=30,
         **kw):
    return {"fill_id": fid, "order_id": oid, "ticker": ticker, "side": side, f"{side}_price_dollars": price,
            "count_fp": count, "is_taker": taker, "fee_cost": "0", "created_time": _iso(NOW - timedelta(hours=ago_h)),
            **kw}


# ---------------------------------------------------------------- registry + classification
async def test_registry_lists_strategies_then_you_and_prefixes_decide_orders():
    reg = strat.registry()
    assert [r["id"] for r in reg] == ["maker", "arb", "you"]
    assert reg[0] == {"id": "maker", "source": "bot", "name": "Bot · Maker", "color": "var(--accent)",
                      "prefix": "mm-"}
    assert reg[2]["name"] == "You" and reg[2]["source"] == "you"
    assert strat.order_label({"client_order_id": "mm-1"}) == {"source": "bot", "strategy": "maker"}
    assert strat.order_label({"client_order_id": "arb-1"}) == {"source": "bot", "strategy": "arb"}
    assert strat.order_label({"client_order_id": "my-own"}) == {"source": "you", "strategy": None}
    assert strat.order_label({}) == {"source": "you", "strategy": None}


async def test_classify_precedence_and_defaults():
    idx = strat.OrderIndex({"J1": None})
    idx.add_orders([{"order_id": "O1", "client_order_id": "mm-x"}, {"order_id": "O2", "client_order_id": "hand"}])
    idx.add_episodes([{"order_id": "E1"}])
    idx.add_arb_docs([{"executed": "FILLED {'KXDEMOB-26OCT15-B80': 5.0}", "ts": NOW}])
    assert idx.classify(fill("a", "O1"))["strategy"] == "maker"
    assert idx.classify(fill("b", "O2")) == {"source": "you", "strategy": None, "source_by": "order"}
    assert idx.classify(fill("c", "J1")) == {"source": "bot", "strategy": "maker", "source_by": "journal"}
    assert idx.classify(fill("d", "E1"))["source_by"] == "episode"
    arb = fill("e", "X", taker=True, ago_h=0.05)
    assert idx.classify(arb) == {"source": "bot", "strategy": "arb", "source_by": "arb_log"}
    assert idx.classify(fill("f", "X", taker=True, ago_h=5))["source"] == "you"        # outside the arb window
    assert idx.classify(fill("g", "UNKNOWN")) == {"source": "you", "strategy": None, "source_by": "default"}
    assert idx.classify(fill("h", ""))["source_by"] == "no_order_id"                    # legacy view kept
    assert idx.classify(fill("i", "Z", client_order_id="arb-9"))["strategy"] == "arb"


async def test_merge_never_weakens_a_stored_label():
    strong = {"source": "bot", "strategy": "maker", "source_by": "order"}
    assert strat.merge_label(strong, {"source": "you", "strategy": None, "source_by": "default"}) is None
    weak = {"source": "you", "strategy": None, "source_by": "default"}
    assert strat.merge_label(weak, strong) == strong
    assert strat.merge_label({}, weak) == weak
    assert strat.merge_label(strong, dict(strong)) is None


async def test_legacy_fills_without_source_count_as_bot():
    assert strat.fill_label({"is_taker": False}) == ("bot", "maker")
    assert strat.fill_label({"is_taker": True}) == ("bot", "arb")
    assert strat.fill_label({"source": "you", "strategy": "maker"}) == ("you", None)


# ---------------------------------------------------------------- manual trades excluded from bot statistics
def _settled_world():
    t0 = NOW - timedelta(days=1)
    fills = [dict(fill("b1", "O1", ticker="KXDEMOB-A"), source="bot", strategy="maker", ts=t0),
             dict(fill("y1", "O9", ticker="FED-25DEC", side="yes", price="0.40"), source="you", strategy=None, ts=t0)]
    setts = [{"ticker": "KXDEMOB-A", "market_result": "no", "ts": NOW - timedelta(hours=2)},
             {"ticker": "FED-25DEC", "market_result": "no", "ts": NOW - timedelta(hours=2)}]
    return fills, setts


async def test_pnl_windows_and_split_exclude_manual_trades():
    fills, setts = _settled_world()
    w = pnlsvc.windows(fills, setts, NOW)
    assert w == {"d7": 6.0, "d30": 6.0, "settled_fills": 1}           # the manual -4.00 is not in the bot number
    split = pnlsvc.realised_by_source(fills, setts, since=NOW - timedelta(days=1))
    assert split["maker"] == 6.0 and split["you"] == -4.0 and split["arb"] is None
    assert split["bot"] == 6.0 and split["total"] == 2.0 and split["settled_fills"] == 2


async def test_analytics_summary_excludes_manual_and_my_trades_shows_them():
    fills, setts = _settled_world()
    rows = an.trade_rows(fills, setts, [], cells=[], series_list=["KXDEMOB"], env="all")
    assert {r["source"] for r in rows} == {"bot", "you"}
    summ = an.summary(rows, [], [], days=30, env="all", now=NOW, cells=[])
    assert summ["totals"]["settled_fills"] == 1 and summ["totals"]["pnl"] == 6.0
    mine = an.my_trades(rows, days=30, env="all", now=NOW)
    assert mine["totals"]["pnl"] == -4.0 and mine["bot"]["pnl"] == 6.0
    assert [t["fill_id"] for t in mine["trades"]] == ["y1"] and mine["trades"][0]["source"] == "you"
    assert an.filter_trades(rows, since=NOW - timedelta(days=30), source="you")[0]["fill_id"] == "y1"


# ---------------------------------------------------------------- runner: sync labels + backfill
class SourcesKalshi:
    def __init__(self, fills, orders=None):
        self.fills, self.orders, self.lookups = fills, orders or {}, []

    def get_fills(self, **kw):
        return [dict(f) for f in self.fills]

    def get_settlements(self, **kw):
        return []

    def get_orders(self, status=None):
        return []

    def request(self, method, path, **kw):
        assert method == "GET" and path.startswith("/portfolio/orders/")
        oid = path.rsplit("/", 1)[1]
        self.lookups.append(oid)
        if oid not in self.orders:
            raise RuntimeError("404")
        return {"order": {"order_id": oid, "client_order_id": self.orders[oid]}}


async def _labels(store):
    return {f["fill_id"]: (f.get("source"), f.get("strategy"), f.get("source_by"))
            for f in await store.list_fills(limit=100)}


async def test_sync_labels_from_journal_lookup_and_default(env):
    r = env.runner
    r._index_journal([{"event": "quote", "note": "live order_id=J1"}])
    k = SourcesKalshi([fill("f1", "J1"), fill("f2", "L1"), fill("f3", "M1")], orders={"L1": "mm-abc", "M1": "x-hand"})
    r.kalshi_factory = lambda m: k
    assert (await r.sync_fills("demo", force=True))["synced"] is True
    lab = await _labels(env.store)
    assert lab["f1"] == ("bot", "maker", "journal")
    assert lab["f2"] == ("bot", "maker", "order")              # looked up: mm- prefix
    assert lab["f3"] == ("you", None, "order")                 # looked up: manual prefix
    assert sorted(k.lookups) == ["L1", "M1"]                   # only ids no local evidence explained
    # a later sync without evidence never weakens what is stored, and doesn't look the same ids up again
    k.orders = {}
    r._maker_oids.clear()
    k.lookups.clear()
    await r.sync_fills("demo", force=True)
    assert (await _labels(env.store))["f1"] == ("bot", "maker", "journal")
    assert k.lookups == []


async def test_unknown_order_defaults_to_you_and_is_excluded_from_status_pnl(env):
    r = env.runner
    k = SourcesKalshi([fill("f9", "NOPE", ticker="KXDEMOB-Z")])
    k.get_settlements = lambda **kw: [{"ticker": "KXDEMOB-Z", "market_result": "no",
                                       "settled_time": _iso(NOW - timedelta(hours=1))}]
    r.kalshi_factory = lambda m: k
    await r.sync_fills("demo", force=True)
    assert (await _labels(env.store))["f9"][0] == "you"
    body = (await env.client.get("/api/status", headers=env.auth)).json()
    assert body["pnl"] is None                                 # no bot fill settled
    if NOW.date() == (NOW - timedelta(hours=1)).date():
        assert body["pnl_today"]["you"] == 6.0 and body["pnl_today"]["maker"] is None


async def test_backfill_labels_old_fills_from_the_store_journal(env):
    r = env.runner
    old = NOW - timedelta(days=60)
    await env.store.upsert_fills([dict(fill("old1", "OJ"), ts=old), dict(fill("old2", "ZZ"), ts=old),
                                  dict(fill("ok", "J"), ts=old, source="bot", strategy="maker", source_by="order")])
    await env.store.add_journal_rows([{"utc": _iso(old - timedelta(minutes=5)), "event": "quote",
                                       "note": "live order_id=OJ"}])
    r.kalshi_factory = lambda m: SourcesKalshi([])
    res = await r.backfill_sources()
    assert res["checked"] == 2 and res["labelled"] == 2 and res["you"] == 1
    lab = await _labels(env.store)
    assert lab["old1"] == ("bot", "maker", "journal") and lab["old2"][0] == "you"
    assert lab["ok"] == ("bot", "maker", "order")
    assert (await r.backfill_sources())["checked"] == 1        # only the 'default' guess is retried


# ---------------------------------------------------------------- alert settings + quiet hours
async def test_alert_settings_validation_and_changes():
    cur = al.merged_settings(None)
    assert cur == al.DEFAULT_ALERT_SETTINGS
    new = al.validate_settings(cur, {"mode": "every_fill", "quiet_hours": {"enabled": True, "start": "23:00"}})
    assert new["mode"] == "every_fill" and new["quiet_hours"] == {"enabled": True, "start": "23:00", "end": "07:00"}
    assert al.changes(cur, new) == [{"key": "mode", "from": "problems", "to": "every_fill"},
                                    {"key": "quiet_hours.enabled", "from": False, "to": True},
                                    {"key": "quiet_hours.start", "from": "22:00", "to": "23:00"}]
    for bad in ({"mode": "all"}, {"daily_summary_time": "25:00"}, {"daily_summary_time": 8},
                {"quiet_hours": {"enabled": "yes"}}, {"quiet_hours": {"start": "7"}}, {"tz": "Mars/Base"},
                {"cells": []}, {"quiet_hours": {"x": 1}}, "nope"):
        with pytest.raises(ValueError):
            al.validate_settings(cur, bad)
    assert al.validate_settings(cur, {"daily_summary_time": None})["daily_summary_time"] is None


async def test_quiet_hours_and_push_filter():
    s = al.validate_settings(al.merged_settings(None), {"tz": "UTC", "mode": "problems",
                                                       "quiet_hours": {"enabled": True, "start": "22:00",
                                                                       "end": "07:00"}})
    night = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)
    day = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert al.in_quiet_hours(s, night) and al.in_quiet_hours(s, night.replace(hour=6, minute=59))
    assert not al.in_quiet_hours(s, day) and not al.in_quiet_hours(s, night.replace(hour=7, minute=0))
    assert al.should_push(s, "stop", now=night) and al.should_push(s, "breaker", now=night)
    assert al.should_push(s, "system", "urgent", now=night)            # critical bypasses quiet hours
    assert not al.should_push(s, "error", now=night) and al.should_push(s, "error", now=day)
    assert not al.should_push(s, "fill", now=day)                       # "problems" mode
    assert al.should_push({**s, "mode": "every_fill"}, "fill", now=day)
    assert al.should_push(s, "test", now=night)


async def test_classify_existing_ntfy_titles():
    assert al.classify("Bot stopped") == "stop" and al.classify("Bot stopped: invalid config", "high") == "stop"
    assert al.classify("Arbitrage legging risk: arb kill switch tripped", "urgent") == "arb_legging"
    assert al.classify("Trading pass error", "high") == "error" and al.classify("No heartbeat") == "error"
    assert al.classify("Arbitrage executed") == "fill" and al.classify("LIVE trading enabled", "urgent") == "system"


# ---------------------------------------------------------------- history hook + everyday alerts
async def _drain(runner):
    for _ in range(20):
        if not runner._bg:
            return
        await asyncio.sleep(0.01)


async def test_every_alert_lands_in_history_without_secrets(env, monkeypatch):
    monkeypatch.setenv("TICK_TOKEN", "super-secret-token-value")
    await env.runner.alerter.send("Trading pass error", "boom token=super-secret-token-value", priority="high")
    await env.runner.stop(by="owner")
    await _drain(env.runner)
    items = await env.store.list_alerts(limit=10)
    types = [i["type"] for i in items]
    assert "error" in types and "stop" in types
    assert all("super-secret" not in (i["title"] + i["body"]) for i in items)
    assert all(len(i["body"]) <= S.ALERT_BODY_MAX for i in items)


async def test_fill_alerts_only_in_every_fill_mode(env):
    r = env.runner
    r._index_journal([{"event": "quote", "note": "live order_id=J1"}])
    k = SourcesKalshi([fill("f1", "J1")])
    r.kalshi_factory = lambda m: k
    await env.store.upsert_fills([fill("seed", "J0")])         # not a first bulk import
    await r.sync_fills("demo", force=True)
    await _drain(r)
    assert await env.store.list_alerts(type="fill") == []
    r.cloud["alerts"] = {"mode": "every_fill"}
    k.fills.append(fill("f2", "J1", ticker="KXDEMOA-1"))
    await r.sync_fills("demo", force=True)
    await _drain(r)
    got = await env.store.list_alerts(type="fill")
    assert len(got) == 1 and got[0]["title"] == "New Fill" and "KXDEMOA-1" in got[0]["body"]


async def test_daily_summary_once_per_local_day(env):
    r = env.runner
    r.cloud["alerts"] = {"daily_summary_time": "08:00", "tz": "UTC"}
    at = datetime(2026, 10, 15, 8, 5, tzinfo=timezone.utc)
    assert await r.check_daily_summary(at.replace(hour=7)) is False
    assert await r.check_daily_summary(at) is True
    assert await r.check_daily_summary(at + timedelta(hours=3)) is False
    assert await r.check_daily_summary(at + timedelta(days=1)) is True
    await _drain(r)
    daily = await env.store.list_alerts(type="daily")
    assert len(daily) == 2 and daily[0]["body"].startswith("Last 24 h:")
    assert (await env.store.get_cloud_settings())["alerts_state"]["daily_sent"] == "2026-10-16"
    r.cloud["alerts"] = {"daily_summary_time": None}
    assert await r.check_daily_summary(at + timedelta(days=2)) is False


# ---------------------------------------------------------------- store contract (memory) + mongo indexes
async def test_store_alerts_newest_first_type_filter_and_paging():
    st = MemoryStore()
    for i in range(5):
        await st.add_alert({"type": "fill" if i % 2 else "error", "title": f"t{i}", "body": "x" * 500,
                            "ts": NOW + timedelta(minutes=i)})
    await st.add_alert({"type": "weird", "title": "y", "ts": NOW + timedelta(minutes=10)})
    all_ = await st.list_alerts(limit=10)
    assert [a["title"] for a in all_[:5]] == ["y", "t4", "t3", "t2", "t1"]
    assert all_[0]["type"] == "system" and len(all_[1]["body"]) == S.ALERT_BODY_MAX and all_[1]["id"]
    assert [a["title"] for a in await st.list_alerts(type="fill")] == ["t3", "t1"]
    assert [a["title"] for a in await st.list_alerts(before=NOW + timedelta(minutes=2))] == ["t1", "t0"]


async def test_store_bulk_set_fill_fields():
    st = MemoryStore()
    await st.upsert_fills([fill("a", "1"), fill("b", "2")])
    assert await st.set_fills_fields({"a": {"source": "you", "ts": "nope"}, "zz": {"source": "bot"}}) == 1
    a = (await st.list_fills(fill_ids=["a"]))[0]
    assert a["source"] == "you" and isinstance(a["ts"], datetime)


async def test_mongo_indexes_for_the_new_collections():
    from control_api.db.mongo import MongoStore
    rec = []

    class Coll:
        def __init__(self, name):
            self.name = name

        async def create_index(self, keys, **kw):
            rec.append((self.name, keys, kw))

    class DB:
        def __getitem__(self, n):
            return Coll(n)

    class Client:
        def __getitem__(self, n):
            return DB()
    await MongoStore("", client=Client()).ensure_indexes()
    alerts = {kw["name"]: (k, kw) for n, k, kw in rec if n == S.ALERT_HISTORY}
    assert alerts["ts_ttl"][1]["expireAfterSeconds"] == 90 * 86400 and "type_ts" in alerts
    push = {kw["name"]: kw for n, k, kw in rec if n == S.PUSH_SUBSCRIPTIONS}
    assert push["sub_id_unique"]["unique"] is True

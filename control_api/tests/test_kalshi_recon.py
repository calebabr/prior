"""Daily Kalshi reconciliation (services/kalshi_recon.py, store methods, /api/recon routes). Fakes + MemoryStore only:
no network, no real Kalshi client, no writes to Kalshi."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.db import MemoryStore
from control_api.db import schemas as S
from control_api.services import kalshi_recon as kr

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)          # recon day = 2026-11-06 (UTC)
DAY = "2026-11-06"
T_IN = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def fill(fid, ticker="KXTEST-26NOV06-B1", count="10", price="0.90", ts=T_IN, side="yes", action="buy"):
    return {"fill_id": fid, "ticker": ticker, "side": side, "action": action, f"{side}_price_dollars": price,
            "count_fp": count, "created_time": ts.isoformat(), "fee_cost": "0.00"}


def settlement(ticker, result="yes", revenue=1000, ts=T_IN):
    return {"ticker": ticker, "market_result": result, "revenue": revenue, "settled_time": ts.isoformat()}


class FakeClient:
    """Read-only Kalshi stand-in. Anything that is not one of the four reads fails loudly."""
    READS = {"get_balance", "get_positions", "get_fills", "get_settlements"}

    def __init__(self, *, cash_cents=10000, positions=None, fills=None, settlements=None, fail=None):
        self.cash_cents, self.fail = cash_cents, fail
        self.positions = [{"ticker": "KXTEST-26NOV06-B1", "position_fp": "10", "market_exposure_dollars": "9.00"}] \
            if positions is None else positions
        self.fills = [fill("f1")] if fills is None else fills
        self.settlements = [] if settlements is None else settlements
        self.calls = []
        # secrets that must never reach a report
        self.private_key, self.api_key_id = "PRIVATE-KEY-MATERIAL", "KEY-ID-123"

    def _hit(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("boom PRIVATE-KEY-MATERIAL")

    def get_balance(self):
        self._hit("get_balance")
        return {"balance": self.cash_cents}

    def get_positions(self, **kw):
        self._hit("get_positions")
        return {"market_positions": list(self.positions)}

    def get_fills(self, **kw):
        self._hit("get_fills")
        return list(self.fills)

    def get_settlements(self, **kw):
        self._hit("get_settlements")
        return list(self.settlements)

    def __getattr__(self, name):                       # create_order, cancel_order, request, ...
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"recon must not call {name}")


async def seeded(*, kalshi=None, store_fills=None, store_settle=None, snap_cash=100.0, snap_expo=9.0, env="demo",
                 snap_age=timedelta(minutes=5)):
    store = MemoryStore()
    await store.upsert_fills([{**f, "env": env} for f in (store_fills if store_fills is not None else [fill("f1")])])
    if store_settle:
        await store.upsert_settlements(store_settle)
    if snap_cash is not None:
        await store.add_account_snapshot({"ts": NOW - snap_age, "cash": snap_cash, "exposure": snap_expo, "env": env})
    return store, kalshi or FakeClient()


async def run(store, client, mode="demo", **kw):
    return await kr.run_daily_recon(store, lambda m: client, mode, NOW, **kw)


def kinds(rep):
    return sorted(d["kind"] for d in rep["differences"])


# ---------------------------------------------------------------- clean + shape
async def test_clean_day_is_ok_with_totals_and_only_reads():
    store, client = await seeded()
    rep = await run(store, client)
    assert rep["ok"] is True and rep["differences"] == [] and rep["persisted"] is True
    assert rep["day"] == DAY and rep["mode"] == "demo" and rep["checked_at"] == NOW.isoformat()
    t = rep["totals"]
    assert t["fills"]["store"] == t["fills"]["kalshi"] == {"count": 1, "contracts": 10.0, "cost": 9.0}
    assert t["account"]["cash"] == {"store": 100.0, "kalshi": 100.0}
    assert t["positions"] == {"store": 1, "kalshi": 1, "complete": True}
    assert t["differences"]["n"] == 0
    assert set(client.calls) <= FakeClient.READS


async def test_day_boundaries_and_other_env_are_excluded():
    edge = [fill("old", ts=datetime(2026, 11, 5, 23, 59, tzinfo=timezone.utc)),
            fill("new", ts=datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)), fill("f1")]
    store, client = await seeded(kalshi=FakeClient(fills=edge, positions=[
        {"ticker": "KXTEST-26NOV06-B1", "position_fp": "30", "market_exposure_dollars": "9.00"}]),
        store_fills=edge)
    await store.upsert_fills([{**fill("other"), "env": "live"}])          # a live fill must not count for demo
    rep = await run(store, client)
    assert [d["kind"] for d in rep["differences"] if d["kind"].startswith("fill")] == []
    assert rep["totals"]["fills"]["store"]["count"] == 1


async def test_local_day_uses_the_given_timezone():
    la_now = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)             # 20:00 on 11-06 in Los Angeles
    day, start, end = kr.day_window(la_now, "America/Los_Angeles")
    assert day == "2026-11-05" and start == datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)   # PST (UTC-8)
    assert end - start == timedelta(hours=24)
    _, s, e = kr.day_window(NOW, "America/Los_Angeles", day="2026-11-01")                        # DST ends: 25 h day
    assert e - s == timedelta(hours=25)
    assert kr.day_window(NOW, "no/such-zone")[0] == DAY                  # unknown zone reads as UTC


# ---------------------------------------------------------------- fills
async def test_fill_missing_in_store():
    store, client = await seeded(kalshi=FakeClient(fills=[fill("f1"), fill("f2", count="5")]))
    rep = await run(store, client)
    assert rep["ok"] is False
    d = [x for x in rep["differences"] if x["kind"] == "fill_missing_in_store"]
    assert len(d) == 1 and d[0]["id"] == "f2" and d[0]["store_value"] is None and d[0]["severity"] == "error"
    assert d[0]["kalshi_value"]["contracts"] == 5.0


async def test_fill_missing_at_kalshi():
    store, client = await seeded(store_fills=[fill("f1"), fill("f9")])
    rep = await run(store, client)
    d = [x for x in rep["differences"] if x["kind"] == "fill_missing_at_kalshi"]
    assert rep["ok"] is False and len(d) == 1 and d[0]["id"] == "f9" and d[0]["kalshi_value"] is None
    assert rep["totals"]["fills"]["store"]["count"] == 2 and rep["totals"]["fills"]["kalshi"]["count"] == 1


async def test_fill_count_and_cost_mismatch():
    store, client = await seeded(store_fills=[fill("f1", count="8", price="0.85")])
    rep = await run(store, client)
    by = {d["kind"]: d for d in rep["differences"]}
    assert by["fill_count_mismatch"]["delta"] == 2.0 and by["fill_count_mismatch"]["id"] == "f1"
    assert by["fill_cost_mismatch"]["store_value"] == 6.8 and by["fill_cost_mismatch"]["kalshi_value"] == 9.0


# ---------------------------------------------------------------- settlements
async def test_settlement_differences():
    t_a, t_b, t_c = "KXTEST-26NOV06-B1", "KXTEST-26NOV06-B2", "KXTEST-26NOV06-B3"
    kal = FakeClient(settlements=[settlement(t_a, revenue=1000), settlement(t_b)])
    store, kal = await seeded(kalshi=kal, store_settle=[settlement(t_a, revenue=900), settlement(t_c)])
    rep = await run(store, kal)
    by = {(d["kind"], d["id"]): d for d in rep["differences"] if d["kind"].startswith("settlement")}
    assert ("settlement_missing_in_store", t_b) in by and by[("settlement_missing_in_store", t_b)]["severity"] == "error"
    assert ("settlement_missing_at_kalshi", t_c) in by
    rev = by[("settlement_revenue_mismatch", t_a)]
    assert rev["store_value"] == 9.0 and rev["kalshi_value"] == 10.0 and rev["delta"] == 1.0
    assert rep["totals"]["settlements"]["kalshi"]["count"] == 2


async def test_settlement_result_mismatch_is_an_error():
    t = "KXTEST-26NOV06-B1"
    store, kal = await seeded(kalshi=FakeClient(settlements=[settlement(t, "no")]),
                              store_settle=[settlement(t, "yes")])
    rep = await run(store, kal)
    d = [x for x in rep["differences"] if x["kind"] == "settlement_result_mismatch"]
    assert d and d[0]["severity"] == "error" and (d[0]["store_value"], d[0]["kalshi_value"]) == ("yes", "no")


# ---------------------------------------------------------------- cash / exposure
async def test_cash_within_tolerance_is_ok_and_beyond_is_flagged():
    store, client = await seeded(snap_cash=100.05)
    assert (await run(store, client))["ok"] is True                      # $0.05 < $0.10
    store, client = await seeded(snap_cash=100.30)
    rep = await run(store, client)
    d = [x for x in rep["differences"] if x["kind"] == "cash_delta"]
    assert rep["ok"] is False and d[0]["delta"] == -0.3 and d[0]["severity"] == "warn"
    assert kr.recon_alert(rep)["priority"] == "default"


async def test_tolerance_is_configurable():
    store, client = await seeded(snap_cash=100.30)
    assert (await run(store, client, tolerance=0.50))["ok"] is True


async def test_big_cash_delta_is_an_error_and_alert_is_urgent():
    store, client = await seeded(snap_cash=101.00)                       # 10 x tolerance
    rep = await run(store, client)
    d = [x for x in rep["differences"] if x["kind"] == "cash_delta"][0]
    assert d["severity"] == "error"
    a = kr.recon_alert(rep)
    assert a["priority"] == "urgent" and "$1.00" in a["message"] and len(a["message"]) <= S.ALERT_BODY_MAX


async def test_exposure_delta():
    store, client = await seeded(snap_expo=12.0)
    rep = await run(store, client)
    d = [x for x in rep["differences"] if x["kind"] == "exposure_delta"]
    assert d and d[0]["store_value"] == 12.0 and d[0]["kalshi_value"] == 9.0


async def test_stale_or_missing_snapshot_is_info_and_does_not_fail_the_run():
    store, client = await seeded(snap_age=timedelta(hours=3), snap_cash=250.0)
    rep = await run(store, client)
    assert rep["ok"] is True and kinds(rep) == ["snapshot_stale"] and rep["differences"][0]["severity"] == "info"
    store, client = await seeded(snap_cash=None)
    rep = await run(store, client)
    assert rep["ok"] is True and kinds(rep) == ["snapshot_missing"]


async def test_snapshot_of_the_other_env_is_not_used():
    store, client = await seeded(env="live", snap_cash=1.0)              # only a live snapshot exists
    rep = await run(store, client, mode="demo")
    assert "cash_delta" not in kinds(rep) and "snapshot_missing" in kinds(rep)


# ---------------------------------------------------------------- positions
async def test_position_differences():
    pos = [{"ticker": "KXTEST-26NOV06-B1", "position_fp": "10", "market_exposure_dollars": "9.00"},
           {"ticker": "KXTEST-26NOV06-B7", "position_fp": "-4", "market_exposure_dollars": "0.00"}]
    store, client = await seeded(kalshi=FakeClient(positions=pos), store_fills=[fill("f1"), fill("f3", ticker="KXT-9")],
                                 snap_cash=None)
    await store.upsert_fills([{**fill("f4", ticker="KXTEST-26NOV06-B7", count="3", side="no", price="0.80",
                                      ts=NOW - timedelta(days=3)), "env": "demo"}])
    rep = await run(store, client)
    by = {d["id"]: d for d in rep["differences"] if d["kind"].startswith("position")}
    assert by["KXT-9"]["kind"] == "position_missing_at_kalshi"
    assert by["KXTEST-26NOV06-B7"]["kind"] == "position_mismatch"         # store -3 (NO), Kalshi -4
    assert by["KXTEST-26NOV06-B7"]["store_value"] == -3.0 and by["KXTEST-26NOV06-B7"]["kalshi_value"] == -4.0
    assert rep["ok"] is False


async def test_position_missing_in_store_and_settled_markets_are_not_open():
    pos = [{"ticker": "KXTEST-26NOV06-B1", "position_fp": "10", "market_exposure_dollars": "9.00"},
           {"ticker": "KXMANUAL-1", "position_fp": "2", "market_exposure_dollars": "1.00"}]
    store, client = await seeded(kalshi=FakeClient(positions=pos), snap_cash=None, snap_expo=None)
    rep = await run(store, client)
    assert [(d["kind"], d["id"]) for d in rep["differences"]
            if d["kind"].startswith("position")] == [("position_missing_in_store", "KXMANUAL-1")]
    # once the market has a stored settlement the stored fills are no longer an open position
    store2, client2 = await seeded(kalshi=FakeClient(positions=[]), snap_cash=None,
                                   store_settle=[settlement("KXTEST-26NOV06-B1", ts=T_IN - timedelta(days=2))])
    assert (await run(store2, client2))["totals"]["positions"]["store"] == 0


async def test_truncated_positions_never_prove_a_position_missing():
    class Trunc(FakeClient):
        def get_positions(self, **kw):
            return {"market_positions": [], "truncated": True}
    store, _ = await seeded()
    rep = await run(store, Trunc())
    assert kinds(rep) == ["positions_truncated"] and rep["totals"]["positions"]["complete"] is False


# ---------------------------------------------------------------- unreachable / store trouble
@pytest.mark.parametrize("fail", ["get_balance", "get_positions", "get_fills", "get_settlements"])
async def test_unreachable_kalshi_is_a_finding_not_a_crash(fail):
    store, client = await seeded(kalshi=FakeClient(fail=fail))
    rep = await run(store, client)
    assert rep["ok"] is False and kinds(rep) == ["unreachable"]
    assert rep["differences"][0]["detail"] == "RuntimeError" and rep["persisted"] is True
    a = kr.recon_alert(rep)
    assert a["priority"] == "default" and "could not reach Kalshi" in a["title"]


async def test_factory_error_and_no_keys():
    store, _ = await seeded()

    def boom(mode):
        raise ConnectionError("down")
    rep = await kr.run_daily_recon(store, boom, "demo", NOW)
    assert kinds(rep) == ["unreachable"] and rep["differences"][0]["detail"] == "ConnectionError"
    rep = await kr.run_daily_recon(store, lambda m: None, "demo", NOW)
    assert kinds(rep) == ["unreachable"] and rep["differences"][0]["detail"] == "not_configured"


async def test_store_failure_is_reported_not_raised():
    class Broken(MemoryStore):
        async def list_fills(self, **kw):
            raise RuntimeError("atlas down")
    rep = await kr.run_daily_recon(Broken(), lambda m: FakeClient(), "demo", NOW)
    assert rep["ok"] is False and kinds(rep) == ["store_unavailable"]


async def test_an_older_store_without_report_support_still_runs():
    class Old(MemoryStore):
        async def save_recon_report(self, doc):
            return None
    store = Old()
    rep = await kr.run_daily_recon(store, lambda m: FakeClient(fills=[]), "demo", NOW)
    assert rep["persisted"] is False and rep["ok"] is False                 # Kalshi cash vs no snapshot: info only; positions differ


# ---------------------------------------------------------------- persistence
async def test_rerun_replaces_the_stored_report():
    store, client = await seeded(snap_cash=105.0)
    first = await run(store, client)
    assert first["ok"] is False
    await store.add_account_snapshot({"ts": NOW - timedelta(minutes=1), "cash": 100.0, "exposure": 9.0, "env": "demo"})
    second = await run(store, client)
    assert second["ok"] is True
    docs = await store.list_recon_reports()
    assert len(docs) == 1 and docs[0]["ok"] is True and docs[0]["differences"] == []
    assert (await store.latest_recon_report("demo"))["ok"] is True


async def test_history_is_newest_day_first_and_modes_are_separate():
    store, client = await seeded()
    for d in ("2026-11-04", "2026-11-06", "2026-11-05"):
        await run(store, client, day=d)
    live_store_client = FakeClient()
    await kr.run_daily_recon(store, lambda m: live_store_client, "live", NOW, day="2026-11-06")
    docs = await store.list_recon_reports()
    assert [(d["day"], d["mode"]) for d in docs] == [("2026-11-06", "live"), ("2026-11-06", "demo"),
                                                      ("2026-11-05", "demo"), ("2026-11-04", "demo")]
    assert [d["day"] for d in await store.list_recon_reports(mode="demo", since_day="2026-11-05")] == \
        ["2026-11-06", "2026-11-05"]
    assert (await store.latest_recon_report("demo"))["day"] == "2026-11-06"
    assert await store.list_recon_reports(limit=0) == []
    # re-checking an OLD day does not move it ahead of a newer day
    await run(store, client, day="2026-11-04")
    assert (await store.list_recon_reports(mode="demo"))[0]["day"] == "2026-11-06"


async def test_store_defaults_for_older_fakes_and_validation():
    from control_api.db.store import Store
    assert await Store.save_recon_report(object(), {"mode": "demo", "day": DAY}) is None
    assert await Store.latest_recon_report(object()) is None and await Store.list_recon_reports(object()) == []
    s = MemoryStore()
    with pytest.raises(ValueError):
        await s.save_recon_report({"mode": "paper", "day": DAY})
    with pytest.raises(ValueError):
        await s.save_recon_report({"mode": "demo", "day": "yesterday"})
    with pytest.raises(ValueError):
        await s.save_recon_report({"mode": "demo", "day": DAY, "api_key": "x"})
    big = {"mode": "demo", "day": DAY, "ok": False, "checked_at": NOW.isoformat(),
           "differences": [{"kind": "k"}] * (S.RECON_DIFFS_MAX + 7)}
    doc = await s.save_recon_report(big)
    assert len(doc["differences"]) == S.RECON_DIFFS_MAX and doc["differences_truncated"] == 7
    assert doc["ts"] == NOW


async def test_mongo_indexes_ttl_and_upsert_shape_offline():
    from control_api.db.mongo import MongoStore
    rec = []

    class Cur:
        def sort(self, spec): rec.append(("sort", spec)); return self
        def limit(self, n): rec.append(("limit", n)); return self
        async def to_list(self): return []

    class Coll:
        def __init__(self, name): self.name = name
        async def create_index(self, keys, **kw): rec.append(("index", self.name, keys, kw))
        async def replace_one(self, q, doc, **kw): rec.append(("replace", self.name, q, kw))
        async def find_one(self, q, proj, **kw): rec.append(("find_one", self.name, q, kw)); return None
        def find(self, q, proj): rec.append(("find", self.name, q)); return Cur()

    class Db:
        def __getitem__(self, n): return Coll(n)

    class Client:
        def __getitem__(self, n): return Db()

    ms = MongoStore("", client=Client())
    await ms.ensure_indexes()
    idx = {kw["name"]: (k, kw) for t, n, k, kw in (r for r in rec if r[0] == "index") if n == S.RECON_REPORTS}
    assert idx["mode_day_unique"][1]["unique"] and idx["mode_day_unique"][0] == [("mode", 1), ("day", 1)]
    assert idx["ts_ttl"][1]["expireAfterSeconds"] == 90 * 24 * 3600
    assert idx["day_ts_desc"][0] == [("day", -1), ("ts", -1)]
    assert S.RECON_REPORTS in S.COLLECTIONS
    await ms.save_recon_report({"mode": "demo", "day": DAY, "ok": True})
    await ms.latest_recon_report("live")
    await ms.list_recon_reports(since_day="2026-11-01", mode="demo", limit=5)
    rep = [r for r in rec if r[0] == "replace"][0]
    assert rep[2] == {"mode": "demo", "day": DAY} and rep[3] == {"upsert": True}
    assert [r for r in rec if r[0] == "find_one"][0][2] == {"mode": "live"}
    assert [r for r in rec if r[0] == "find"][0][2] == {"mode": "demo", "day": {"$gte": "2026-11-01"}}


# ---------------------------------------------------------------- no secrets, alert
async def test_report_holds_no_secrets_even_when_kalshi_errors_with_one():
    store, client = await seeded(kalshi=FakeClient(fail="get_fills"))
    text = kr.dumps(await run(store, client))
    ok_store, ok_client = await seeded()
    text += kr.dumps(await run(ok_store, ok_client))
    for stored in await store.list_recon_reports():
        text += kr.dumps(stored)
    for needle in ("PRIVATE-KEY-MATERIAL", "KEY-ID-123", "boom", "BEGIN"):
        assert needle not in text


async def test_alert_is_none_when_clean_and_stays_short():
    store, client = await seeded()
    assert kr.recon_alert(await run(store, client)) is None
    assert kr.recon_alert(None) is None and kr.recon_alert({"ok": True}) is None
    store, client = await seeded(kalshi=FakeClient(fills=[fill(f"x{i}") for i in range(40)]))
    a = kr.recon_alert(await run(store, client))
    assert a["kind"] == "recon" and a["priority"] == "default" and len(a["message"]) <= S.ALERT_BODY_MAX
    assert "fill_missing_in_store" in a["message"]


# ---------------------------------------------------------------- routes
async def test_routes_need_a_session_and_return_reports(env):
    r = await env.client.get("/api/recon/latest")
    assert r.status_code == 401
    assert (await env.client.get("/api/recon/history")).status_code == 401
    empty = await env.client.get("/api/recon/latest", headers=env.auth)
    assert empty.status_code == 200 and empty.json() == {"report": None}
    today = datetime.now(timezone.utc)
    client = FakeClient(fills=[])
    await env.store.upsert_fills([{**fill("f1"), "env": "demo"}])
    for back in (1, 2, 3):
        await kr.run_daily_recon(env.store, lambda m: client, "demo", today - timedelta(days=back - 1))
    r = await env.client.get("/api/recon/latest", headers=env.auth)
    body = r.json()["report"]
    assert body["mode"] == "demo" and "ts" not in body and isinstance(body["differences"], list)
    h = (await env.client.get("/api/recon/history?days=30", headers=env.auth)).json()
    assert h["days"] == 30 and len(h["reports"]) == 3
    assert [x["day"] for x in h["reports"]] == sorted((x["day"] for x in h["reports"]), reverse=True)
    one = (await env.client.get("/api/recon/history?days=1", headers=env.auth)).json()
    assert len(one["reports"]) <= 2


async def test_route_validation(env):
    assert (await env.client.get("/api/recon/history?days=0", headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/recon/history?days=91", headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/recon/latest?mode=paper", headers=env.auth)).status_code == 422


async def test_route_rate_limit(env):
    codes = [(await env.client.get("/api/recon/latest", headers=env.auth)).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and 429 in codes[30:]

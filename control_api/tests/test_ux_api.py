"""UX API additions: /status fields, since=, /since, strategies, changelog,
config versions + undo, toast fields, alert history/settings, push routes, account positions, my-trades + notes."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from control_api.services import webpush as w

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc)
ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-secret-123"


def _iso(dt):
    return dt.isoformat()


# ---------------------------------------------------------------- /status
async def test_status_has_new_fields_and_keeps_old_ones(env):
    for mins, ok in ((100, True), (90, True), (50, False), (40, True)):     # the third pass came 40 min late
        await env.store.add_heartbeat({"ts": NOW - timedelta(minutes=mins), "ok": ok,
                                       "error": None if ok else "boom", "mode": "demo"})
    body = (await env.client.get("/api/status", headers=env.auth)).json()
    for old in ("running_pass", "last_run_utc", "next_due_utc", "kill_switch", "desired", "mode", "pnl", "account"):
        assert old in body
    assert set(body["pnl_today"]) >= {"maker", "arb", "you", "total"}
    rp = body["recent_passes"]
    assert len(rp) == 4 and set(rp[0]) == {"ts", "ok", "late", "errors"}
    assert [p["ok"] for p in rp] == [True, True, False, True] and rp[2]["errors"] == 1
    assert rp[2]["late"] is True and rp[1]["late"] is False
    assert body["next_run_at"] is None                              # stopped: nothing scheduled
    assert "last_run_at" in body


async def test_status_recent_passes_caps_at_12_newest_last(env):
    for i in range(20):
        await env.store.add_heartbeat({"ts": NOW - timedelta(minutes=200 - i * 10), "ok": True})
    rp = (await env.client.get("/api/status", headers=env.auth)).json()["recent_passes"]
    assert len(rp) == 12 and rp[0]["ts"] < rp[-1]["ts"]


async def test_status_next_run_at_when_running(env):
    await env.runner.start_trading(by="t")
    body = (await env.client.get("/api/status", headers=env.auth)).json()
    assert body["next_run_at"] == body["next_due_utc"] and body["next_run_at"] is not None


# ---------------------------------------------------------------- since= and /since
async def _seed_activity(store):
    await store.add_journal_rows([
        {"utc": _iso(NOW - timedelta(hours=5)), "event": "quote", "ticker": "KXDEMOB-A", "note": "live order_id=O1"},
        {"utc": _iso(NOW - timedelta(hours=1)), "event": "cancel", "ticker": "KXDEMOB-A", "note": "{}"}])
    await store.upsert_fills([
        {"fill_id": "b1", "order_id": "O1", "ticker": "KXDEMOB-A", "side": "no", "no_price_dollars": "0.4",
         "count_fp": "10", "ts": NOW - timedelta(hours=4), "source": "bot", "strategy": "maker", "env": "demo"},
        {"fill_id": "y1", "order_id": "M1", "ticker": "FED-1", "side": "yes", "yes_price_dollars": "0.4",
         "count_fp": "5", "ts": NOW - timedelta(hours=2), "source": "you", "env": "demo"}])
    await store.upsert_settlements([
        {"ticker": "KXDEMOB-A", "market_result": "no", "ts": NOW - timedelta(minutes=30)},
        {"ticker": "FED-1", "market_result": "no", "ts": NOW - timedelta(minutes=20)}])


async def test_activity_since_and_sources(env):
    await _seed_activity(env.store)
    body = (await env.client.get("/api/activity", headers=env.auth)).json()
    assert len(body["items"]) == 2 and all(i["source"] == "bot" and i["strategy"] == "maker" for i in body["items"])
    assert {f["fill_id"]: f["source"] for f in body["fills"]} == {"b1": "bot", "y1": "you"}
    setts = {s["ticker"]: s for s in body["settlements"]}
    assert setts["FED-1"]["source"] == "you" and setts["KXDEMOB-A"]["sources"] == ["bot"]
    since = _iso(NOW - timedelta(hours=3))
    body = (await env.client.get("/api/activity", params={"since": since}, headers=env.auth)).json()
    assert [i["event"] for i in body["items"]] == ["cancel"] and [f["fill_id"] for f in body["fills"]] == ["y1"]
    body = (await env.client.get("/api/activity", params={"source": "you"}, headers=env.auth)).json()
    assert [f["fill_id"] for f in body["fills"]] == ["y1"]
    assert (await env.client.get("/api/activity", params={"source": "x"}, headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/activity", params={"since": "nope"}, headers=env.auth)).status_code == 422


async def test_history_since(env):
    for h in (10, 5, 1):
        await env.store.add_account_snapshot({"ts": NOW - timedelta(hours=h), "cash": h, "exposure": 0})
    body = (await env.client.get("/api/history", params={"since": _iso(NOW - timedelta(hours=6))},
                                 headers=env.auth)).json()
    assert [s["cash"] for s in body["snapshots"]] == [5, 1]


async def test_since_card(env):
    await _seed_activity(env.store)
    body = (await env.client.get("/api/since", params={"ts": _iso(NOW - timedelta(hours=3))},
                                 headers=env.auth)).json()
    assert body["fills"] == 1 and body["settled"] == 2
    assert body["pnl_change"] == 6.0                                    # bot only; manual trade -2.00 separate
    assert body["by_source"]["you"] == {"fills": 1, "pnl": -2.0} and body["pnl_total"] == 4.0
    old = (await env.client.get("/api/since", params={"ts": "2020-01-01T00:00:00Z"}, headers=env.auth)).json()
    assert datetime.fromisoformat(old["since"]) > NOW - timedelta(days=36)        # clamped
    assert (await env.client.get("/api/since", headers=env.auth)).status_code == 422


# ---------------------------------------------------------------- strategies + changelog
async def test_strategies_and_changelog(env):
    s = (await env.client.get("/api/strategies", headers=env.auth)).json()
    assert [x["id"] for x in s] == ["maker", "arb", "you"] and s[1]["color"] == "var(--chart2)"
    cl = (await env.client.get("/api/changelog", headers=env.auth)).json()
    assert isinstance(cl, list) and cl and set(cl[0]) == {"version", "date", "items"}
    assert all(isinstance(i, str) for i in cl[0]["items"])


async def test_changelog_loader_tolerates_bad_files(tmp_path):
    from control_api.routes import ux
    p = tmp_path / "c.json"
    p.write_text('[{"version": "1", "date": "2026-10-15", "items": ["a", 3]}, {"bad": 1}, "x"]', encoding="utf-8")
    assert ux.load_changelog(p) == [{"version": "1", "date": "2026-10-15", "items": ["a"]}]
    p.write_text("{not json", encoding="utf-8")
    assert ux.load_changelog(p) == []
    assert ux.load_changelog(tmp_path / "missing.json") == []


# ---------------------------------------------------------------- toast fields, config versions + undo
async def test_stop_returns_toast_fields_and_keeps_old_ones(env):
    body = (await env.client.post("/api/bot/stop", headers=env.auth)).json()
    assert body["orders_cancelled"] == body["cancelled"] == 2
    assert datetime.fromisoformat(body["stopped_at"]) and body["desired"] == "stopped"


async def test_config_put_changes_versions_and_undo(env):
    cells_before = (await env.client.get("/api/config", headers=env.auth)).json()["config"]["cells"]
    old = env.runner.config["run_budget"]
    r = await env.client.put("/api/config", json={"run_budget": 50}, headers=await env.h2fa())
    assert r.status_code == 200
    assert r.json()["changes"] == [{"key": "run_budget", "from": old, "to": 50}]
    v = (await env.client.get("/api/config/versions", headers=env.auth)).json()
    assert v["versions"][0]["changes"] == [{"key": "run_budget", "from": old, "to": 50}]
    assert v["undo"]["version"] == v["versions"][0]["version"] and "after" not in v["versions"][0]
    # undo needs fresh 2FA
    assert (await env.client.post("/api/config/undo", json={}, headers=env.auth)).status_code == 403
    stale = await env.client.post("/api/config/undo", json={"version": 1}, headers=await env.h2fa())
    assert stale.status_code == 409 and stale.json()["detail"] == "stale_version"
    u = await env.client.post("/api/config/undo", json={"version": v["undo"]["version"]}, headers=await env.h2fa())
    assert u.status_code == 200, u.text
    assert u.json()["changes"] == [{"key": "run_budget", "from": 50, "to": old}]
    assert env.runner.config["run_budget"] == old
    assert (await env.client.get("/api/config", headers=env.auth)).json()["config"]["cells"] == cells_before
    audit = [a["action"] for a in await env.store.list_audit()]
    assert "config.undo" in audit


async def test_undo_with_nothing_to_undo(env):
    r = await env.client.post("/api/config/undo", headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "nothing_to_undo"       # only the seed version
    assert (await env.client.post("/api/config/undo", json={"x": 1}, headers=await env.h2fa())).status_code == 422


async def test_blind_undo_refuses_a_monitor_change(env):
    cfg = dict(env.runner.config)
    paused = dict(cfg, series=cfg["series"][:1])
    await env.store.save_config(paused, before=cfg, by="monitor")
    r = await env.client.post("/api/config/undo", headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "last_change_not_yours"
    v = (await env.client.get("/api/config/versions", headers=env.auth)).json()["undo"]
    assert v["by"] == "monitor"
    ok = await env.client.post("/api/config/undo", json={"version": v["version"]}, headers=await env.h2fa())
    assert ok.status_code == 200 and env.runner.config["series"] == cfg["series"]


async def test_undo_never_writes_cells_even_if_a_version_changed_them(env):
    cfg = dict(env.runner.config)
    tampered = dict(cfg, cells=[[0.5, 0.6, 0, 1, "yes"]], run_budget=cfg["run_budget"])
    await env.store.save_config(cfg, before=tampered, by="owner")       # a stored version "changing" cells
    r = await env.client.post("/api/config/undo", headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "nothing_to_undo"     # cells are never undone
    assert env.runner.config["cells"] == cfg["cells"]


# ---------------------------------------------------------------- alerts
async def test_alert_history_route(env):
    for i, t in enumerate(["fill", "error", "stop"]):
        await env.store.add_alert({"type": t, "title": f"T{i}", "body": "b", "ts": NOW - timedelta(minutes=10 - i)})
    items = (await env.client.get("/api/alerts", headers=env.auth)).json()
    assert [i["title"] for i in items] == ["T2", "T1", "T0"]
    assert set(items[0]) == {"id", "ts", "type", "title", "body"}
    assert [i["type"] for i in (await env.client.get("/api/alerts", params={"type": "error"},
                                                     headers=env.auth)).json()] == ["error"]
    page = (await env.client.get("/api/alerts", params={"before": items[1]["ts"], "limit": 5},
                                 headers=env.auth)).json()
    assert [i["title"] for i in page] == ["T0"]
    assert (await env.client.get("/api/alerts", params={"type": "x"}, headers=env.auth)).status_code == 422
    assert (await env.client.get("/api/alerts")).status_code == 401


async def test_alert_settings_get_put(env):
    assert (await env.client.get("/api/alerts/settings", headers=env.auth)).json()["mode"] == "problems"
    body = {"mode": "every_fill", "quiet_hours": {"enabled": True}}
    assert (await env.client.put("/api/alerts/settings", json=body, headers=env.auth)).status_code == 403
    r = await env.client.put("/api/alerts/settings", json=body, headers=await env.h2fa())
    assert r.status_code == 200 and r.json()["mode"] == "every_fill"
    assert {"key": "mode", "from": "problems", "to": "every_fill"} in r.json()["changes"]
    assert (await env.store.get_cloud_settings())["alerts"]["quiet_hours"]["enabled"] is True
    assert env.runner.alert_settings()["mode"] == "every_fill"
    bad = await env.client.put("/api/alerts/settings", json={"mode": "loud"}, headers=await env.h2fa())
    assert bad.status_code == 400 and isinstance(bad.json()["detail"], list)


# ---------------------------------------------------------------- push routes
def _enable_push(env, status=201):
    key = ec.generate_private_key(ec.SECP256R1())
    env.settings.vapid_private_key = w.b64u(key.private_numbers().private_value.to_bytes(32, "big"))
    env.settings.vapid_subject = "mailto:owner@example.com"
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(status)
    env.runner.push = w.PushService(env.settings, env.store, transport=httpx.MockTransport(handler))
    return key, sent


def _sub():
    ua = ec.generate_private_key(ec.SECP256R1())
    return {"endpoint": ENDPOINT, "expirationTime": None,
            "keys": {"p256dh": w.b64u(w.public_bytes(ua)), "auth": w.b64u(b"0123456789abcdef")}}


async def test_push_not_configured(env):
    pk = (await env.client.get("/api/push/public-key", headers=env.auth)).json()
    assert pk == {"public_key": None, "enabled": False}
    assert (await env.client.post("/api/push/subscribe", json=_sub(), headers=env.auth)).status_code == 409
    assert (await env.client.post("/api/push/test", headers=env.auth)).status_code == 409


async def test_push_subscribe_test_unsubscribe_and_no_leaks(env, caplog):
    caplog.set_level(logging.DEBUG)
    key, sent = _enable_push(env)
    pk = (await env.client.get("/api/push/public-key", headers=env.auth)).json()
    assert pk["enabled"] and w.b64u_dec(pk["public_key"]) == w.public_bytes(key)
    sub = _sub()
    r = await env.client.post("/api/push/subscribe", json=sub, headers=env.auth)
    assert r.status_code == 200 and r.json() == {"subscribed": True}
    assert ENDPOINT not in r.text and sub["keys"]["auth"] not in r.text
    t = await env.client.post("/api/push/test", headers=env.auth)
    assert t.json() == {"sent": 1, "failed": 0, "removed": 0} and str(sent[0].url) == ENDPOINT
    assert (await env.store.list_alerts(type="test"))[0]["title"] == "Test Notification"
    d = await env.client.request("DELETE", "/api/push/subscribe", json={"endpoint": ENDPOINT}, headers=env.auth)
    assert d.json() == {"subscribed": False} and await env.store.list_push_subscriptions() == []
    # the endpoint and keys never reach logs, the audit log or the in-app log buffer
    blob = caplog.text + str(await env.store.list_audit()) + "\n".join(env.runner.logs.tail(2000))
    assert "device-secret-123" not in blob and sub["keys"]["p256dh"] not in blob


async def test_push_subscribe_validation(env):
    _enable_push(env)
    bad = _sub()
    bad["endpoint"] = "https://attacker.example/x"
    r = await env.client.post("/api/push/subscribe", json=bad, headers=env.auth)
    assert r.status_code == 400 and "attacker" not in r.text
    assert (await env.client.post("/api/push/subscribe", json={"endpoint": ENDPOINT},
                                  headers=env.auth)).status_code == 400
    assert (await env.client.post("/api/push/subscribe", json=_sub())).status_code == 401


async def test_push_test_is_rate_limited(env):
    _enable_push(env)
    codes = [(await env.client.post("/api/push/test", headers=env.auth)).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429


async def test_alert_goes_out_as_push_and_is_logged(env):
    _, sent = _enable_push(env)
    await env.client.post("/api/push/subscribe", json=_sub(), headers=env.auth)
    await env.client.post("/api/bot/stop", headers=env.auth)
    for _ in range(50):
        if not env.runner._bg:
            break
        import asyncio
        await asyncio.sleep(0.01)
    stop = (await env.store.list_alerts(type="stop"))[0]
    assert stop["channels"] == ["push"] and len(sent) == 1


# ---------------------------------------------------------------- account positions
class AccountKalshi:
    def __init__(self):
        self.calls = 0

    def get_positions(self, **kw):
        self.calls += 1
        return {"market_positions": [
            {"ticker": "KXDEMOB-A", "position_fp": "-10", "market_exposure_dollars": "4.00"},
            {"ticker": "FED-1", "position": 4, "market_exposure": 160},
            {"ticker": "ZERO", "position": 0}]}

    def get_orders(self, status=None):
        return [{"ticker": "KXDEMOB-A", "side": "no", "action": "buy", "remaining_count_fp": "5",
                 "no_price_dollars": "0.41", "client_order_id": "mm-1"},
                {"ticker": "NBA-X", "side": "yes", "action": "buy", "remaining_count": 2, "yes_price": 55,
                 "client_order_id": "phone-1"}]

    def get_market(self, t):
        return {"title": f"Title {t}", "yes_bid_dollars": "0.38", "no_bid_dollars": "0.42"}

    def cancel_order(self, *a, **k):
        raise AssertionError("account view must never cancel")


async def test_account_positions_rows_and_sources(env):
    k = AccountKalshi()
    env.kalshi = k
    await env.store.upsert_fills([
        {"fill_id": "b", "order_id": "O", "ticker": "KXDEMOB-A", "side": "no", "no_price_dollars": "0.4",
         "count_fp": "10", "ts": NOW - timedelta(hours=3), "source": "bot", "strategy": "maker", "env": "demo"}])
    rows = (await env.client.get("/api/account/positions", headers=env.auth)).json()
    pos = [r for r in rows if r["kind"] == "position"]
    ords = [r for r in rows if r["kind"] == "order"]
    ny = next(r for r in pos if r["ticker"] == "KXDEMOB-A")
    assert ny == {"kind": "position", "ticker": "KXDEMOB-A", "title": "Title KXDEMOB-A", "side": "no", "count": 10,
                  "price": 0.4, "cost": 4.0, "value": 4.2, "payout_if_wins": 10.0, "source": "bot",
                  "strategy": "maker", "legs": None}
    fed = next(r for r in pos if r["ticker"] == "FED-1")
    assert fed["source"] == "you" and fed["side"] == "yes" and fed["price"] == 0.4 and fed["value"] == 1.52
    assert {o["ticker"]: (o["source"], o["strategy"]) for o in ords} == {"KXDEMOB-A": ("bot", "maker"),
                                                                          "NBA-X": ("you", None)}
    assert next(o for o in ords if o["ticker"] == "NBA-X")["price"] == 0.55
    for r in rows:
        assert {"ticker", "title", "side", "count", "price", "value", "payout_if_wins", "source", "strategy"} <= set(r)
    await env.client.get("/api/account/positions", headers=env.auth)
    assert k.calls == 1                                                  # cached 30 s


async def test_account_positions_without_keys_or_on_errors(env):
    env.kalshi = None
    assert (await env.client.get("/api/account/positions", headers=env.auth)).status_code == 409

    class Down(AccountKalshi):
        def get_positions(self, **kw):
            raise RuntimeError("kalshi down")
    env.kalshi = Down()
    env.app.state.account_cache = {}
    assert (await env.client.get("/api/account/positions", headers=env.auth)).status_code == 503


# ---------------------------------------------------------------- my trades + notes
async def test_my_trades_and_notes(env):
    await env.store.upsert_fills([
        {"fill_id": "y1", "order_id": "M1", "ticker": "FED-1", "side": "yes", "yes_price_dollars": "0.4",
         "count_fp": "5", "ts": NOW - timedelta(days=2), "source": "you", "env": "demo"},
        {"fill_id": "b1", "order_id": "O1", "ticker": "KXDEMOB-A", "side": "no", "no_price_dollars": "0.4",
         "count_fp": "10", "ts": NOW - timedelta(days=2), "source": "bot", "strategy": "maker", "env": "demo"}])
    await env.store.upsert_settlements([{"ticker": "FED-1", "market_result": "yes", "ts": NOW - timedelta(days=1)},
                                        {"ticker": "KXDEMOB-A", "market_result": "no",
                                         "ts": NOW - timedelta(days=1)}])
    m = (await env.client.get("/api/analytics/my-trades", headers=env.auth)).json()
    assert m["totals"]["pnl"] == 3.0 and m["bot"]["pnl"] == 6.0 and [t["fill_id"] for t in m["trades"]] == ["y1"]
    summ = (await env.client.get("/api/analytics/summary", headers=env.auth)).json()
    assert summ["totals"]["pnl"] == 6.0                                 # bot only
    tr = (await env.client.get("/api/analytics/trades", headers=env.auth)).json()
    assert [t["fill_id"] for t in tr["trades"]] == ["b1"]                # bot-only by default
    tr = (await env.client.get("/api/analytics/trades", params={"source": "all"}, headers=env.auth)).json()
    assert {t["fill_id"] for t in tr["trades"]} == {"b1", "y1"}
    # notes
    assert (await env.client.get("/api/trades/y1/note", headers=env.auth)).json() == {"fill_id": "y1", "note": ""}
    r = await env.client.put("/api/trades/y1/note", json={"note": "  Fed\nhunch ‮ "}, headers=env.auth)
    assert r.status_code == 200 and r.json()["note"] == "Fed hunch"
    assert (await env.client.get("/api/trades/y1/note", headers=env.auth)).json()["note"] == "Fed hunch"
    m = (await env.client.get("/api/analytics/my-trades", headers=env.auth)).json()
    assert m["trades"][0]["note"] == "Fed hunch"                         # cached rows patched
    assert (await env.client.put("/api/trades/y1/note", json={"note": "x" * 201},
                                 headers=env.auth)).status_code == 400
    assert (await env.client.put("/api/trades/nope/note", json={"note": "a"}, headers=env.auth)).status_code == 404
    assert (await env.client.put("/api/trades/bad%20id/note", json={"note": "a"},
                                 headers=env.auth)).status_code == 400
    assert (await env.client.put("/api/trades/y1/note", json={"note": "a", "x": 1},
                                 headers=env.auth)).status_code == 400
    assert (await env.client.put("/api/trades/y1/note", json={"note": ""}, headers=env.auth)).json()["note"] == ""

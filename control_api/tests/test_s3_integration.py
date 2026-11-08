"""Sprint 2B part 3 integrator wiring: legging alerts, grouping, daily/weekly merge (items 5, 18), the daily recon
schedule (item 14) and the release `trading_changes` flag (follow-up a)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from control_api import runner as rn
from control_api.db import schemas
from control_api.runner import Runner
from control_api.services import alerts as alertsvc

pytestmark = pytest.mark.asyncio
UTC = timezone.utc


def _at(h, m=0):
    return datetime(2026, 11, 4, h, m, tzinfo=UTC)          # a Wednesday


def _set_alerts(r, **kw):
    r.cloud["alerts"] = {"tz": "UTC", **kw}


# ------------------------------------------------------------------ item 5: legging alert wiring
OP = SimpleNamespace(event="KXDEMOA-26NOV04", kind="YES", cost=1.0, profit=0.02)


async def test_contained_partial_sends_the_urgent_contained_legging_alert(env):
    r = env.runner
    note = {"type": "contained", "trip": None, "contain": {"fills": [["KXDEMOA-B1", 2], ["KXDEMOA-B2", 0]],
                                                           "loss": 0.12, "loss_cap": 0.5,
                                                           "paused_until_date": "2026-11-05"}}
    await r._send_legging_alert(OP, "PARTIAL contained: flattened", note)
    a = r.alerter.sent[-1]
    assert a["priority"] == "urgent" and a["title"] == "Arbitrage legging risk: partial set contained"
    assert "2026-11-05" in a["message"] and "KXDEMOA-B1 (2)" in a["message"]
    assert alertsvc.classify(a["title"], a["priority"]) == "breaker"


async def test_contained_partial_that_tripped_says_kill_switch_with_the_reason(env):
    r = env.runner
    note = {"type": "contained", "trip": "second contained partial in 24 h",
            "contain": {"fills": [["KXDEMOA-B1", 1]], "loss": 0.1, "loss_cap": 0.5, "paused_until_date": "x"}}
    await r._send_legging_alert(OP, "PARTIAL contained: flattened", note)
    a = r.alerter.sent[-1]
    assert a["title"].endswith("arb kill switch tripped") and a["priority"] == "urgent"
    assert "second contained partial in 24 h" in a["message"]


@pytest.mark.parametrize("executed", ["PARTIAL {'KXDEMOA-B1': 2}", "ERROR RuntimeError"])
async def test_uncontained_partial_or_error_sends_the_kill_switch_alert(env, executed):
    r = env.runner
    await r._send_legging_alert(OP, executed, None)
    a = r.alerter.sent[-1]
    assert a["title"] == "Arbitrage legging risk: arb kill switch tripped" and a["priority"] == "urgent"
    assert OP.event in a["message"]


async def test_legging_alert_never_raises_and_falls_back_to_a_plain_urgent_alert(env, monkeypatch):
    r = env.runner
    monkeypatch.setattr(alertsvc, "legging_alert", lambda *a: (_ for _ in ()).throw(RuntimeError("bug")))
    await r._send_legging_alert(OP, "PARTIAL {}", None)
    assert r.alerter.sent[-1]["title"].startswith("Arbitrage legging risk") and \
        r.alerter.sent[-1]["priority"] == "urgent"


async def test_legging_alerts_are_never_grouped(env):
    r = env.runner
    for _ in range(3):
        await r._send_legging_alert(OP, "PARTIAL {}", None)
    assert sum(1 for a in r.alerter.sent if a["title"].startswith("Arbitrage legging risk")) == 3


# ------------------------------------------------------------------ item 18: grouping, daily + weekly
async def test_runner_enables_the_alert_grouper(env):
    assert isinstance(env.runner.alerter.grouper, alertsvc.AlertGrouper)


async def test_repeats_are_grouped_but_runner_throttled_alerts_are_not(env):
    r = env.runner
    for _ in range(3):
        await r.alerter.send("Monitor failed", "x")
    assert sum(1 for a in r.alerter.sent if a["title"] == "Monitor failed") == 1
    out = "write_errors: cancel=1 place=0 paused=0\n"
    t0 = rn.utcnow()
    await r._handle_write_report(out, "demo", now=t0)
    await r._handle_write_report(out, "demo", now=t0 + rn.ORDER_ERROR_ALERT_EVERY)
    assert sum(1 for a in r.alerter.sent if a["title"].startswith("Order errors")) == 2


async def test_daily_summary_merges_alerts_state_and_keeps_weekly_sent(env):
    r = env.runner
    _set_alerts(r, daily_summary_time="08:00")
    r.cloud["alerts_state"] = {"weekly_sent": "2026-W45", "recon_day": "2026-11-04"}
    assert await r.check_daily_summary(_at(9)) is True
    stored = (await env.store.get_cloud_settings())["alerts_state"]
    assert stored == {"weekly_sent": "2026-W45", "recon_day": "2026-11-04", "daily_sent": "2026-11-04"}
    assert r.cloud["alerts_state"]["weekly_sent"] == "2026-W45"


async def test_alert_schedules_flush_daily_and_weekly(env, monkeypatch):
    r = env.runner
    calls = []

    async def weekly(alerter, store, now, cloud):
        calls.append(("weekly", now))
        return False

    async def flush(now=None):
        calls.append(("flush", now))
        return 0
    monkeypatch.setattr(alertsvc, "maybe_send_weekly", weekly)
    monkeypatch.setattr(r.alerter, "flush_groups", flush)
    await r.check_alert_schedules(_at(9))
    assert [c[0] for c in calls] == ["flush", "weekly"] and calls[1][1] == _at(9)


async def test_weekly_after_daily_on_sunday_sends_once(env):
    r = env.runner
    _set_alerts(r, daily_summary_time="08:00")
    sunday = datetime(2026, 11, 8, 19, 0, tzinfo=UTC)
    await r.check_alert_schedules(sunday)
    await r.check_alert_schedules(sunday + timedelta(minutes=5))
    st = (await env.store.get_cloud_settings())["alerts_state"]
    assert st["weekly_sent"] == "2026-W45" and st["daily_sent"] == "2026-11-08"
    await asyncio.sleep(0)                               # let the history writes (background) land
    for t in list(r._bg):
        await t
    hist = await env.store.list_alerts(limit=50)
    assert sum(1 for h in hist if h["title"] == "Weekly Summary") == 1


async def test_flush_runs_once_per_pass(env, demo_keys, monkeypatch):
    r = env.runner
    r.desired = "running"
    r.last_monitor_utc = rn.utcnow()
    n = []

    async def flush(now=None):
        n.append(1)
        return 0
    monkeypatch.setattr(r.alerter, "flush_groups", flush)
    monkeypatch.setattr(r.ms, "main", lambda *a, **k: 0)
    await r._pass("t")
    assert n == [1]


async def test_new_alert_types_are_valid_history_types():
    assert {"weekly", "arb_legging"} <= set(schemas.ALERT_TYPES)
    assert schemas.prepare_alert({"type": "arb_legging", "title": "t"})["type"] == "arb_legging"
    assert schemas.prepare_alert({"type": "weekly", "title": "t"})["type"] == "weekly"


# ------------------------------------------------------------------ item 14: daily recon schedule
@pytest.fixture
def demo_keys(monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")


@pytest.fixture
def fake_recon(monkeypatch):
    calls = []
    state = {"report": {"ok": True, "day": "2026-11-03", "differences": []}, "raise": None}

    async def run(store, factory, mode, now, *, tz=None, run_blocking=None, **kw):
        calls.append({"mode": mode, "now": now, "tz": tz, "run_blocking": run_blocking, "factory": factory})
        if state["raise"]:
            raise state["raise"]
        return state["report"]
    monkeypatch.setattr(rn.recon, "run_daily_recon", run)
    return SimpleNamespace(calls=calls, state=state)


async def test_recon_due_once_a_day_after_the_summary_time(env):
    r = env.runner
    _set_alerts(r, daily_summary_time="08:00")
    assert r._recon_due(_at(7, 59)) is None
    assert r._recon_due(_at(8, 0)) == "2026-11-04"
    r._recon_day = "2026-11-04"
    assert r._recon_due(_at(23)) is None
    assert r._recon_due(_at(8) + timedelta(days=1)) == "2026-11-05"


async def test_recon_default_time_without_a_summary_time(env):
    r = env.runner
    _set_alerts(r, daily_summary_time=None)
    assert r._recon_due(_at(5, 59)) is None and r._recon_due(_at(6)) == "2026-11-04"


async def test_recon_runs_after_an_ok_pass_on_the_telemetry_pool(env, demo_keys, fake_recon, monkeypatch):
    r = env.runner
    _set_alerts(r, daily_summary_time="00:00")
    r.desired = "running"
    r.last_monitor_utc = rn.utcnow()
    monkeypatch.setattr(r.ms, "main", lambda *a, **k: 0)
    await r._pass("t")
    assert r._recon_task is not None
    await r._recon_task
    assert len(fake_recon.calls) == 1
    c = fake_recon.calls[0]
    assert c["mode"] == "demo" and c["tz"] == "UTC"
    assert c["run_blocking"] == r._run_tel and c["factory"] == r._tel_kalshi
    assert (await env.store.get_cloud_settings())["alerts_state"]["recon_day"] == r._recon_day
    await r._pass("t2")                                  # same day: not again
    assert len(fake_recon.calls) == 1


async def test_recon_not_scheduled_after_a_failed_pass(env, demo_keys, fake_recon, monkeypatch):
    r = env.runner
    _set_alerts(r, daily_summary_time="00:00")
    r.desired = "running"
    r.last_monitor_utc = rn.utcnow()
    monkeypatch.setattr(r.ms, "main", lambda *a, **k: 1)
    await r._pass("t")
    assert r._recon_task is None and fake_recon.calls == []


async def test_recon_difference_is_alerted(env, fake_recon, monkeypatch):
    r = env.runner
    fake_recon.state["report"] = {"ok": False}
    monkeypatch.setattr(rn.recon, "recon_alert", lambda rep: {"kind": "recon", "title": "Reconciliation difference",
                                                              "message": "1 difference", "priority": "default"})
    await r.run_recon("demo", "2026-11-04", _at(9))
    assert r.alerter.sent[-1]["title"] == "Reconciliation difference"


async def test_recon_and_store_failures_never_raise(env, fake_recon, monkeypatch):
    r = env.runner
    fake_recon.state["raise"] = RuntimeError("kalshi down")

    async def boom(*a, **k):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(env.store, "save_cloud_settings", boom)
    assert await r.run_recon("demo", "2026-11-04", _at(9)) is None
    assert r.cloud["alerts_state"]["recon_day"] == "2026-11-04"      # in memory: no repeat today


async def test_recon_guard_survives_a_restart_and_keeps_other_alert_state(env, fake_recon):
    r = env.runner
    r.cloud["alerts_state"] = {"daily_sent": "2026-11-04", "weekly_sent": "2026-W45"}
    await r.run_recon("demo", "2026-11-04", _at(9))
    st = (await env.store.get_cloud_settings())["alerts_state"]
    assert st == {"daily_sent": "2026-11-04", "weekly_sent": "2026-W45", "recon_day": "2026-11-04"}
    fresh = Runner(env.settings, env.store, kalshi_factory=lambda m: env.kalshi)
    await fresh.rehydrate()
    assert fresh._recon_day == "2026-11-04"


# ------------------------------------------------------------------ release trading_changes flag
TOKEN = "release-token-for-tests-0123456789"
BODY = {"version": "2026.11.09-1", "commit": "abc1234def", "summary": "feat: something", "ci": "success"}


@pytest.fixture
def rel_env(env, monkeypatch):
    monkeypatch.setattr(env.settings, "release_token", TOKEN)
    return env


async def _rel_audits(e, action):
    return [a for a in await e.store.list_audit(limit=100) if a["action"] == action]


async def test_release_trading_changes_defaults_to_false(rel_env):
    r = await rel_env.client.post("/api/release/pending", json=BODY, headers={"X-Release-Token": TOKEN})
    assert r.status_code == 200
    assert (await _rel_audits(rel_env, "release.register"))[0]["details"]["trading_changes"] is False


async def test_release_trading_changes_true_is_in_register_and_approve_audits(rel_env):
    r = await rel_env.client.post("/api/release/pending", json={**BODY, "trading_changes": True},
                                  headers={"X-Release-Token": TOKEN})
    rid = r.json()["id"]
    assert (await _rel_audits(rel_env, "release.register"))[0]["details"]["trading_changes"] is True
    pend = (await rel_env.client.get("/api/release/pending", headers=rel_env.auth)).json()["release"]
    assert pend["trading_changes"] is True
    ap = await rel_env.client.post(f"/api/release/pending/{rid}/approve", headers=await rel_env.h2fa())
    assert ap.status_code == 200, ap.text
    assert (await _rel_audits(rel_env, "release.approve"))[0]["details"]["trading_changes"] is True


@pytest.mark.parametrize("bad", ["true", 1, None])
async def test_release_trading_changes_must_be_a_boolean(rel_env, bad):
    r = await rel_env.client.post("/api/release/pending", json={**BODY, "trading_changes": bad},
                                  headers={"X-Release-Token": TOKEN})
    assert r.status_code == 422


async def test_periods_marker_reads_the_flag(rel_env):
    from control_api.services import markers
    await rel_env.client.post("/api/release/pending", json={**BODY, "trading_changes": True},
                              headers={"X-Release-Token": TOKEN})
    row = (await _rel_audits(rel_env, "release.register"))[0]
    m = markers._marker(row)
    assert m and m["changes_trading"] is True

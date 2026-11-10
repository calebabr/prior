"""Sprint 3 L1: the loss / drawdown circuit breaker (services/breaker.py, the runner glue, /api/breaker, Start and
arbitrage acknowledgement, the preflight check).

Fakes only, no network, arbitrary equity numbers (not a live profile). Times are fixed datetimes passed in (or the
runner clock patched), so nothing depends on the wall clock and every test is parallel-safe."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from control_api import runner as rn
from control_api.db import schemas
from control_api.runner import Runner
from control_api.services import alerts as alertsvc
from control_api.services import botfiles
from control_api.services import breaker as bk
from control_api.services import preflight as pf
from control_api.tests.conftest import BrokenStore
from control_api.tests.test_arb_contain import EV_A, EV_B, _on, xenv  # noqa: F401
from control_api.tests.vclock import SlowStore

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)          # 10:00 New York (EST)
ON = {**bk.DEFAULTS, "demo_enabled": True}                       # both modes on, default limits


def _ev(settings, state, env, eq, ts):
    return bk.evaluate(settings, state, env, eq, ts)


def _seq(settings, env, readings):
    """Feed (equity, ts) readings in order; return the last result."""
    st, res = bk.empty_state(), None
    for eq, ts in readings:
        res = _ev(settings, st, env, eq, ts)
        st = res["state"]
    return res


# ================================================================== pure: the two limits
def test_drawdown_trips_exactly_at_the_limit_and_not_below():
    s = {**ON, "day_loss_frac": 0.25}               # keep the day-loss limit out of the way
    hit = _seq(s, "live", [(100.0, T0), (85.0, T0 + timedelta(minutes=10))])
    assert hit["trip"] and hit["trip"]["reason"] == "drawdown" and hit["trip"]["limits"] == ["drawdown"]
    assert hit["trip"]["hwm"] == 100.0 and hit["trip"]["equity"] == 85.0 and hit["persist"] is True
    miss = _seq(s, "live", [(100.0, T0), (85.01, T0 + timedelta(minutes=10))])
    assert miss["trip"] is None and miss["state"]["tripped"] is None


def test_drawdown_uses_the_running_high_water_mark_across_days():
    s = {**ON, "day_loss_frac": 0.25}
    readings = [(100.0, T0), (120.0, T0 + timedelta(days=1)), (110.0, T0 + timedelta(days=2)),
                (102.0, T0 + timedelta(days=3))]
    res = _seq(s, "live", readings)
    assert res["trip"]["reason"] == "drawdown" and res["trip"]["hwm"] == 120.0      # 120 x 0.85 = 102


def test_day_loss_trips_exactly_at_the_limit_and_not_below():
    s = {**ON, "drawdown_frac": 0.30}
    hit = _seq(s, "live", [(100.0, T0), (90.0, T0 + timedelta(hours=1))])
    assert hit["trip"]["reason"] == "day_loss" and hit["trip"]["day_start_equity"] == 100.0
    assert hit["trip"]["day_loss"] == 10.0
    miss = _seq(s, "live", [(100.0, T0), (90.01, T0 + timedelta(hours=1))])
    assert miss["trip"] is None


def test_both_limits_at_once_are_both_reported():
    res = _seq(ON, "live", [(100.0, T0), (80.0, T0 + timedelta(hours=1))])
    assert res["trip"]["limits"] == ["drawdown", "day_loss"] and res["trip"]["reason"] == "drawdown"


def test_new_york_midnight_starts_a_new_day_from_the_recent_previous_reading():
    s = {**ON, "drawdown_frac": 0.30}
    late = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)      # 23:00 New York on 11/9
    after = datetime(2026, 11, 10, 5, 30, tzinfo=timezone.utc)    # 00:30 New York on 11/10
    res = _seq(s, "live", [(100.0, late - timedelta(hours=2)), (100.0, late), (95.0, after)])
    env = res["state"]["envs"]["live"]
    assert env["day"] == "2026-11-10" and env["day_start_equity"] == 100.0 and res["trip"] is None
    res2 = _ev(s, res["state"], "live", 90.0, after + timedelta(hours=1))
    assert res2["trip"]["reason"] == "day_loss"                     # the overnight drop counts toward the new day


def test_a_stale_previous_reading_does_not_seed_the_new_day():
    s = {**ON, "drawdown_frac": 0.30}
    res = _seq(s, "live", [(100.0, T0), (91.0, T0 + timedelta(days=1))])   # a day later: seeded from 91
    assert res["trip"] is None and res["state"]["envs"]["live"]["day_start_equity"] == 91.0


def test_the_day_boundary_is_new_york_not_utc():
    assert bk.market_day(datetime(2026, 11, 10, 4, 59, tzinfo=timezone.utc)) == "2026-11-09"
    assert bk.market_day(datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)) == "2026-11-10"


# ================================================================== pure: modes, bad data, settings
def test_demo_is_off_by_default_and_live_is_on():
    s = bk.settings_of(None)
    assert s["live_enabled"] is True and s["demo_enabled"] is False
    assert s["drawdown_frac"] == 0.15 and s["day_loss_frac"] == 0.10
    demo = _seq(s, "demo", [(100.0, T0), (50.0, T0 + timedelta(hours=1))])
    assert demo["trip"] is None and demo["state"]["envs"]["demo"]["hwm"] == 100.0   # still tracked
    live = _seq(s, "live", [(100.0, T0), (50.0, T0 + timedelta(hours=1))])
    assert live["trip"] is not None


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -5.0, 0, "100", True, [100]])
def test_bad_equity_skips_with_a_warning_and_never_trips(bad):
    st = _seq(ON, "live", [(100.0, T0)])["state"]
    res = _ev(ON, st, "live", bad, T0 + timedelta(minutes=5))
    assert res["trip"] is None and res["warning"] and res["state"] is st and res["persist"] is False


def test_bad_time_or_env_skips_with_a_warning():
    st = bk.empty_state()
    assert _ev(ON, st, "live", 100.0, "not a time")["warning"]
    assert _ev(ON, st, "paper", 100.0, T0)["warning"]


def test_nothing_is_evaluated_while_tripped():
    res = _seq(ON, "live", [(100.0, T0), (50.0, T0 + timedelta(hours=1))])
    again = _ev(ON, res["state"], "live", 10.0, T0 + timedelta(hours=2))
    assert again["trip"] is None and again["state"] is res["state"]


def test_stored_settings_are_not_trusted():
    raw = {"live_enabled": "no", "demo_enabled": 1, "drawdown_frac": 0.9, "day_loss_frac": True}
    assert {k: bk.settings_of(raw)[k] for k in bk.FIELDS} == bk.DEFAULTS
    ok = bk.settings_of({"live_enabled": False, "drawdown_frac": 0.2})
    assert ok["live_enabled"] is False and ok["drawdown_frac"] == 0.2


@pytest.mark.parametrize("body, msg", [
    ({"drawdown_frac": True}, "drawdown_frac: must be a number"),
    ({"drawdown_frac": "0.1"}, "drawdown_frac: must be a number"),
    ({"drawdown_frac": 0.019}, "drawdown_frac: must be between"),
    ({"drawdown_frac": 0.31}, "drawdown_frac: must be between"),
    ({"day_loss_frac": 0.009}, "day_loss_frac: must be between"),
    ({"day_loss_frac": 0.26}, "day_loss_frac: must be between"),
    ({"live_enabled": 0}, "live_enabled: must be true/false"),
    ({"demo_enabled": "true"}, "demo_enabled: must be true/false"),
    ({"tripped": None}, "tripped: unknown field"),
])
def test_settings_validation_is_strict(body, msg):
    with pytest.raises(ValueError) as e:
        bk.validate_update(bk.settings_of(None), body)
    assert any(m.startswith(msg) for m in e.value.args[0]), e.value.args[0]


def test_settings_ceilings_are_inclusive_and_loosening_is_flagged():
    cur = bk.settings_of(None)
    new, loose = bk.validate_update(cur, {"drawdown_frac": 0.30, "day_loss_frac": 0.01})
    assert new["drawdown_frac"] == 0.30 and new["day_loss_frac"] == 0.01 and loose == ["drawdown_frac"]
    new, loose = bk.validate_update(cur, {"live_enabled": False, "demo_enabled": True, "drawdown_frac": 0.02})
    assert loose == ["live_enabled"]
    with pytest.raises(ValueError):
        bk.validate_update(cur, [1, 2])


def test_stored_state_is_normalized_and_never_raises():
    st, w = bk.normalize_state({"tripped": "garbage", "envs": {"live": {"hwm": "x"}, "demo": 5}})
    assert st["tripped"]["reason"] == "unknown" and st["envs"] == {} and len(w) == 2
    st, w = bk.normalize_state("junk")
    assert st["tripped"] and w
    st, w = bk.normalize_state(None)
    assert st == bk.empty_state() and w == []
    good = _seq(ON, "live", [(100.0, T0), (50.0, T0 + timedelta(hours=1))])["state"]
    back, w = bk.normalize_state(json.loads(json.dumps(bk.encode_state(good))))
    assert w == [] and back["tripped"]["reason"] == "drawdown" and back["envs"]["live"]["hwm"] == 100.0


def test_acknowledge_rebases_to_the_last_reading():
    st = _seq(ON, "live", [(100.0, T0), (80.0, T0 + timedelta(hours=1))])["state"]
    ack = bk.acknowledge(st)
    assert ack["tripped"] is None and ack["envs"]["live"]["hwm"] == 80.0
    assert ack["envs"]["live"]["day_start_equity"] == 80.0
    assert _ev(ON, ack, "live", 79.0, T0 + timedelta(hours=2))["trip"] is None


def test_alert_is_urgent_breaker_type_with_the_numbers_and_the_restart_rule():
    trip = _seq(ON, "live", [(100.0, T0), (80.0, T0 + timedelta(hours=1))])["trip"]
    a = bk.alert(trip)
    assert a["type"] == "breaker" and a["priority"] == "urgent"
    assert alertsvc.classify(a["title"], a["priority"]) == "breaker"
    assert alertsvc.never_grouped("breaker", a["title"], a["priority"])
    assert "drawdown" in a["title"] and "day loss" in a["title"] and "(live)" in a["title"]
    assert "$80.00" in a["body"] and "$100.00" in a["body"] and "15.0%" in a["body"] and "2FA" in a["body"]


# ================================================================== runner glue
async def _trip(env, eq_hi=100.0, eq_lo=80.0, t=T0):
    r = env.runner
    assert await r._breaker_observe("live", eq_hi, t, source="maker") is None
    return await r._breaker_observe("live", eq_lo, t + timedelta(minutes=10), source="maker")


def _breaker_alerts(env):
    return [a for a in env.runner.alerter.sent if a["title"].startswith(bk.ALERT_TITLE)]


async def _audits(env, action):
    return [a for a in await env.store.list_audit(limit=500) if a["action"] == action]


@pytest.mark.asyncio
async def test_trip_runs_the_full_stop_and_alerts_and_audits_once(env):
    env.runner.desired = "running"
    env.runner.arb_desired = "running"
    trip = await _trip(env)
    r = env.runner
    assert trip and trip["reason"] == "drawdown" and r.breaker_tripped()
    assert r.desired == "stopped" and r.arb_desired == "stopped"
    assert r.kill_switch() and r.arb_kill_switch()
    assert sorted(env.kalshi.cancelled) == ["o1", "o3"]                       # resting mm- orders only
    assert (await env.store.get_state())["desired"] == "stopped"
    stored = (await env.store.get_cloud_settings())[bk.CLOUD_STATE]
    assert stored["tripped"]["reason"] == "drawdown"
    assert len(_breaker_alerts(env)) == 1 and _breaker_alerts(env)[0]["priority"] == "urgent"
    assert len(await _audits(env, "breaker.trip")) == 1
    # later readings (maker pass or arbitrage) never repeat the Stop, the alert or the audit row
    for i in range(3):
        assert await r._breaker_observe("live", 50.0, T0 + timedelta(hours=1 + i), source="maker") is None
    assert len(_breaker_alerts(env)) == 1 and len(await _audits(env, "breaker.trip")) == 1


@pytest.mark.asyncio
async def test_pass_equity_comes_from_the_journal_account_row(env):
    r = env.runner
    rows = [{"utc": T0.isoformat(), "event": "account", "ticker": "", "side": "", "price": "", "count": "",
             "cost": "", "cell": "", "note": json.dumps({"cash": 70.0, "exposure": 30.0, "headroom": 5.0})}]
    botfiles.write_journal_rows(r.journal_path, rows, schemas.JOURNAL_FIELDS)
    r._tail = None
    await r._sync_journal("live")
    assert r._pass_equity == ("live", 100.0, T0)
    await r._breaker_after_pass("live", r._pass_equity)
    assert r.breaker_state["envs"]["live"]["hwm"] == 100.0 and not r.breaker_tripped()
    await r._breaker_after_pass("live", ("live", 84.0, T0 + timedelta(minutes=10)))
    assert r.breaker_tripped()


@pytest.mark.asyncio
async def test_missing_exposure_is_bad_data_not_zero(env):
    assert bk.snapshot_equity({"cash": 50.0, "exposure": None}) is None
    assert bk.snapshot_equity({"cash": 50.0, "exposure": 10.0}) == 60.0
    r = env.runner
    await r._breaker_observe("live", 100.0, T0, source="maker")
    await r._breaker_after_pass("live", ("live", None, T0 + timedelta(minutes=10)))
    assert not r.breaker_tripped() and "unusable" in r.breaker_status()["warning"]
    await r._breaker_after_pass("live", None)                    # no account row at all this pass
    assert not r.breaker_tripped() and "no account equity" in r.breaker_status()["warning"]
    body = (await env.client.get("/api/status", headers=env.auth)).json()
    assert body["breaker"]["tripped"] is False and body["breaker"]["warning"]


@pytest.mark.asyncio
async def test_unreadable_state_skips_with_a_warning_and_never_trips(env, monkeypatch):
    r = env.runner
    r._breaker_loaded = False

    async def boom():
        raise RuntimeError("atlas down")
    monkeypatch.setattr(env.store, "get_cloud_settings", boom)
    assert await r._breaker_observe("live", 100.0, T0, source="maker") is None
    assert await r._breaker_observe("live", 10.0, T0 + timedelta(minutes=1), source="maker") is None
    assert not r.breaker_tripped() and "unreadable" in r.breaker_warning
    assert r.breaker_status()["readable"] is False
    assert r.desired != "stopped" or not _breaker_alerts(env)


@pytest.mark.asyncio
async def test_a_failed_trip_write_keeps_the_trip_and_retries(env, monkeypatch):
    r = env.runner
    r.BREAKER_RETRY_FIRST_S = 0.01
    real = env.store.save_cloud_settings
    fails = {"n": 2}

    async def flaky(doc, *, by):
        if bk.CLOUD_STATE in doc and fails["n"] > 0:
            fails["n"] -= 1
            raise RuntimeError("atlas down")
        return await real(doc, by=by)
    monkeypatch.setattr(env.store, "save_cloud_settings", flaky)
    await r._breaker_observe("live", 100.0, T0, source="maker")       # this write fails (marks unsaved)
    await r._breaker_observe("live", 80.0, T0 + timedelta(minutes=5), source="maker")   # trip write fails too
    assert r.breaker_tripped() and r._breaker_unsaved
    await r._breaker_retry
    assert (await env.store.get_cloud_settings())[bk.CLOUD_STATE]["tripped"]["reason"] == "drawdown"


async def _successor(env):
    await env.runner.lease.release(handover=None)
    new = Runner(env.settings, env.store, kalshi_factory=env.runner.kalshi_factory)
    new.alerter = env.runner.alerter
    await new.rehydrate()
    assert new.role() == "leader"
    return new


@pytest.mark.asyncio
async def test_trip_and_marks_survive_a_restart_and_keep_the_bot_stopped(env):
    await _trip(env)
    await env.store.set_state(by="test", desired="running", arb_desired="running")   # a lost Stop write, say
    new = await _successor(env)
    assert new.breaker_tripped() and new.breaker_state["envs"]["live"]["hwm"] == 100.0
    assert new.desired == "stopped" and new.arb_desired == "stopped"
    assert await new._breaker_observe("live", 10.0, T0 + timedelta(hours=1), source="maker") is None
    assert len(_breaker_alerts(env)) == 1                       # no second alert after the restart


@pytest.mark.asyncio
async def test_high_water_mark_survives_a_restart_without_a_trip(env):
    r = env.runner
    await r._breaker_observe("live", 120.0, T0, source="maker")
    await r._breaker_observe("live", 110.0, T0 + timedelta(minutes=10), source="maker")
    new = await _successor(env)
    assert new.breaker_state["envs"]["live"]["hwm"] == 120.0
    assert await new._breaker_observe("live", 102.0, T0 + timedelta(minutes=20), source="maker")


# ================================================================== Start / arbitrage enable while tripped
@pytest.mark.asyncio
async def test_start_while_tripped_needs_fresh_2fa_and_the_ack(env):
    await _trip(env)
    r = await env.client.post("/api/bot/start", headers=env.auth, json={"ack_breaker": True})
    assert r.status_code == 403 and r.json()["detail"] == "2fa_required"
    r = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "breaker_tripped"
    r = await env.client.post("/api/bot/start", headers=await env.h2fa(), json={"ack_breaker": False})
    assert r.status_code == 409 and env.runner.desired == "stopped" and env.runner.breaker_tripped()
    r = await env.client.post("/api/bot/start", headers=await env.h2fa(), json={"ack_breaker": "yes"})
    assert r.status_code == 400
    r = await env.client.post("/api/bot/start", headers=await env.h2fa(), json={"ack_breaker": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"desired": "running", "breaker_cleared": True}
    assert not env.runner.breaker_tripped() and env.runner.desired == "running"
    stored = (await env.store.get_cloud_settings())[bk.CLOUD_STATE]
    assert stored["tripped"] is None and stored["envs"]["live"]["hwm"] == 80.0     # re-based
    assert len(await _audits(env, "breaker.ack")) == 1


@pytest.mark.asyncio
async def test_start_without_a_trip_is_unchanged_and_ignores_the_ack(env):
    r = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert r.status_code == 200 and r.json() == {"desired": "running"}
    r = await env.client.post("/api/bot/start", headers=await env.h2fa(), json={"ack_breaker": True})
    assert r.status_code == 200 and r.json() == {"desired": "running"}
    assert await _audits(env, "breaker.ack") == []


@pytest.mark.asyncio
async def test_start_reads_a_trip_written_by_another_instance(env):
    other = Runner(env.settings, env.store, kalshi_factory=env.runner.kalshi_factory)
    await other.load_cloud()
    await other._breaker_observe("live", 100.0, T0, source="maker")
    await other._breaker_observe("live", 80.0, T0 + timedelta(minutes=1), source="maker")
    assert other.breaker_tripped() and not env.runner.breaker_tripped()       # this instance has a stale copy
    r = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "breaker_tripped"


@pytest.mark.asyncio
async def test_start_refuses_when_the_breaker_state_cannot_be_read(env, monkeypatch):
    async def boom():
        raise RuntimeError("atlas down")
    monkeypatch.setattr(env.store, "get_cloud_settings", boom)
    r = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert r.status_code == 503 and env.runner.desired == "stopped"


@pytest.mark.asyncio
async def test_arbitrage_enable_while_tripped_needs_the_ack(env):
    await _trip(env)
    r = await env.client.put("/api/arb", json={"enabled": True}, headers=await env.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "breaker_tripped"
    r = await env.client.put("/api/arb", json={"enabled": True, "ack_breaker": 1}, headers=await env.h2fa())
    assert r.status_code == 400
    r = await env.client.put("/api/arb", json={"enabled": True, "ack_breaker": True}, headers=env.auth)
    assert r.status_code == 403
    # the trip's Stop wrote an arbitrage Stop kill file (not a legging kill): no ack_legging needed
    r = await env.client.put("/api/arb", json={"enabled": True, "ack_breaker": True}, headers=await env.h2fa())
    assert r.status_code == 200, r.text
    assert env.runner.arb_desired == "running" and not env.runner.breaker_tripped()
    assert len(await _audits(env, "breaker.ack")) == 1


# ================================================================== arbitrage execute path
@pytest.mark.asyncio
async def test_arbitrage_execute_feeds_the_breaker_and_a_trip_stops_everything(xenv):
    put = await xenv.client.put("/api/breaker", json={"demo_enabled": True}, headers=await xenv.h2fa())
    assert put.status_code == 200, put.text
    await _on(xenv)
    await xenv.runner.arb_scan()                                   # two sets FILLED at $1000 equity: the mark
    r = xenv.runner
    assert r.breaker_state["envs"]["demo"]["hwm"] == 1000.0 and not r.breaker_tripped()
    xenv.trader.cash_cents = 84000                                 # 16% below the mark
    xenv.clock.t += timedelta(minutes=5)
    await r.arb_scan()
    assert r.breaker_tripped() and r.breaker_state["tripped"]["source"] == "arbitrage"
    assert r.arb_desired == "stopped" and r.desired == "stopped" and r.arb_kill_switch()
    assert len(_breaker_alerts(xenv)) == 1 and len(await _audits(xenv, "breaker.trip")) == 1
    xenv.calls.clear()
    await r.arb_scan()
    assert xenv.calls == []                                        # nothing sent while tripped


@pytest.mark.asyncio
async def test_a_contained_partial_loss_counts_toward_the_reading(xenv):
    from control_api.tests.test_arb_contain import contained
    await xenv.client.put("/api/breaker", json={"demo_enabled": True, "day_loss_frac": 0.01,
                                                "drawdown_frac": 0.30}, headers=await xenv.h2fa())
    await _on(xenv)
    await xenv.runner.arb_scan()                                   # mark and day start $1000
    xenv.results[EV_A] = contained(12.0)
    xenv.clock.t += timedelta(minutes=5)
    await xenv.runner.arb_scan()
    trip = xenv.runner.breaker_state["tripped"]
    assert trip and trip["reason"] == "day_loss" and trip["equity"] == pytest.approx(988.0)


# ================================================================== Stop stays independent and fast
@pytest.mark.asyncio
async def test_stop_never_touches_the_breaker_or_the_store_for_it(env, monkeypatch):
    await _trip(env)
    r = env.runner

    def forbidden(*a, **k):
        raise AssertionError("breaker code on the Stop path")
    for name in ("_breaker_observe", "_breaker_save", "_breaker_gate", "_breaker_apply_cloud", "breaker_settings",
                 "_breaker_trip"):
        monkeypatch.setattr(r, name, forbidden)
    spy = SlowStore(env.store, 0)
    r.store = spy
    res = await r.stop(by="owner")
    assert res["desired"] == "stopped"
    assert not {"get_cloud_settings", "save_cloud_settings"} & set(spy.calls)


@pytest.mark.asyncio
async def test_stop_while_tripped_is_fast_with_the_store_down(env):
    await _trip(env)
    env.app.state.store = env.runner.store = BrokenStore()
    t = time.monotonic()
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200 and r.json()["desired"] == "stopped"
    assert time.monotonic() - t < 1.0


# ================================================================== settings API
@pytest.mark.asyncio
async def test_get_breaker_shape(env):
    await env.runner._breaker_observe("live", 100.0, T0, source="maker")
    await env.runner._breaker_observe("live", 95.0, T0 + timedelta(minutes=10), source="maker")
    r = await env.client.get("/api/breaker", headers=env.auth)
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"settings", "schema", "ceilings", "defaults", "state"}
    assert {k: b["settings"][k] for k in bk.FIELDS} == bk.DEFAULTS
    assert b["ceilings"] == {"drawdown_frac": {"min": 0.02, "max": 0.30}, "day_loss_frac": {"min": 0.01, "max": 0.25}}
    st = b["state"]
    assert set(st) == {"readable", "tripped", "trip", "warning", "env", "envs"}
    live = st["envs"]["live"]
    assert live["hwm"] == 100.0 and live["drawdown"] == 5.0 and live["drawdown_share"] == 0.05
    assert live["day_start_equity"] == 100.0 and live["day_loss"] == 5.0 and live["enabled"] is True
    assert st["envs"]["demo"]["enabled"] is False and st["envs"]["demo"]["hwm"] is None
    assert (await env.client.get("/api/breaker")).status_code == 401


@pytest.mark.asyncio
async def test_put_breaker_needs_fresh_2fa_validates_and_audits(env):
    r = await env.client.put("/api/breaker", json={"drawdown_frac": 0.2}, headers=env.auth)
    assert r.status_code == 403
    r = await env.client.put("/api/breaker", json={"drawdown_frac": 0.5}, headers=await env.h2fa())
    assert r.status_code == 400 and r.json()["detail"] == ["drawdown_frac: must be between 0.02 and 0.3"]
    r = await env.client.put("/api/breaker", json={"drawdown_frac": "0.2"}, headers=await env.h2fa())
    assert r.status_code == 400
    r = await env.client.put("/api/breaker", content=b"not json",
                             headers={**(await env.h2fa()), "Content-Type": "application/json"})
    assert r.status_code == 400
    r = await env.client.put("/api/breaker", json={"drawdown_frac": 0.2, "live_enabled": False},
                             headers=await env.h2fa())
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["drawdown_frac"] == 0.2 and s["live_enabled"] is False and s["by"] == "owner" and s["updated"]
    stored = (await env.store.get_cloud_settings())[bk.CLOUD_SETTINGS]
    assert stored["drawdown_frac"] == 0.2 and stored["live_enabled"] is False
    rows = [a for a in await _audits(env, "breaker.set") if a["ok"]]
    assert len(rows) == 1 and sorted(rows[0]["details"]["loosened"]) == ["drawdown_frac", "live_enabled"]
    assert rows[0]["details"]["from"]["drawdown_frac"] == 0.15


@pytest.mark.asyncio
async def test_breaker_is_not_writable_through_the_cloud_api(env):
    h = await env.h2fa()
    for key, val in ((bk.CLOUD_SETTINGS, {"live_enabled": False}), (bk.CLOUD_STATE, {"tripped": None})):
        r = await env.client.put("/api/cloud", json={key: val}, headers=h)
        assert r.status_code in (400, 422)
        h = await env.h2fa()
    r = await env.client.get("/api/cloud", headers=env.auth)
    assert bk.CLOUD_SETTINGS not in r.json()["settings"] and bk.CLOUD_STATE not in r.json()["settings"]


# ================================================================== preflight
@pytest.mark.asyncio
async def test_preflight_breaker_check(env):
    r = env.runner
    assert pf.check_breaker(r)[0] == pf.PASS
    r.cloud[bk.CLOUD_SETTINGS] = {"live_enabled": False}
    assert pf.check_breaker(r)[0] == pf.FAIL
    r.cloud[bk.CLOUD_SETTINGS] = None
    await _trip(env)
    status, detail = pf.check_breaker(r)
    assert status == pf.WARN and "tripped" in detail
    r._breaker_loaded = False
    assert pf.check_breaker(r)[0] == pf.FAIL


@pytest.mark.asyncio
async def test_preflight_breaker_check_is_in_the_live_run(env):
    out = await pf.run_preflight("live", runner=env.runner, store=env.store, settings=env.settings)
    ids = [c["id"] for c in out["checks"]]
    assert "breaker" in ids
    env.runner.cloud[bk.CLOUD_SETTINGS] = {"live_enabled": False}
    out = await pf.run_preflight("demo", runner=env.runner, store=env.store, settings=env.settings)
    c = {c["id"]: c for c in out["checks"]}["breaker"]
    assert c["status"] == pf.WARN and c["detail"].startswith("live only")     # demo: shown, never blocks


def test_runner_module_has_no_breaker_on_stop():
    """Static guard: the Stop method's source never mentions the breaker."""
    import inspect
    src = inspect.getsource(rn.Runner.stop) + inspect.getsource(rn.Runner._persist_stop) \
        + inspect.getsource(rn.Runner._write_stopped) + inspect.getsource(rn.Runner._cancel_resting)
    assert "breaker" not in src

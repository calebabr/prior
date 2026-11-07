"""Sprint 2B part 3, item 10: per-strategy LIVE arming (services/arming.py, routes/arming.py, runner gates)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from control_api.db import MemoryStore
from control_api.runner import Runner
from control_api.services import arming as armsvc
from control_api.services import preflight as pf

pytestmark = pytest.mark.asyncio
_real_run_preflight = pf.run_preflight
LIVE_ENV = {"KALSHI_PROD_API_KEY_ID": "prod-id", "KALSHI_PROD_PRIVATE_KEY": "not-a-real-key",
            "KALSHI_LIVE_CONFIRM": "I_UNDERSTAND_REAL_MONEY",
            "KALSHI_DEMO_API_KEY_ID": "kid", "KALSHI_DEMO_PRIVATE_KEY": "pk"}


def _ready(ready=True, failed=()):
    async def fake(target, **kw):
        assert target == "live"                    # arming always checks the live target
        checks = [{"id": "keys", "label": "Keys", "status": "pass", "detail": ""}]
        checks += [{"id": f, "label": f.capitalize(), "status": "fail", "detail": f"{f} is bad"} for f in failed]
        return {"ready": ready and not failed, "target": target, "ts": "x", "checks": checks}
    return fake


@pytest.fixture
def live_ok(env, monkeypatch, real_strategy):
    for k, v in LIVE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(env.settings, "prod_keys_present", True)
    monkeypatch.setattr(env.settings, "live_confirm_ok", True)
    monkeypatch.setattr(pf, "run_preflight", _ready())
    return env


async def _put(env, body, two_fa=True):
    h = await env.h2fa() if two_fa else env.auth
    return await env.client.put("/api/arming", json=body, headers=h)


async def _audits(env, action="arming.set"):
    return [a for a in await env.store.list_audit(limit=100) if a.get("action") == action]


# ------------------------------------------------------------------ GET
async def test_get_defaults_not_armed(env):
    r = await env.client.get("/api/arming", headers=env.auth)
    assert r.status_code == 200
    assert r.json() == {"maker": {"armed": False, "mode": "demo"}, "arbitrage": {"armed": False, "mode": "demo"},
                        "live_allowed": False, "updated": None}


async def test_get_needs_a_session(env):
    assert (await env.client.get("/api/arming")).status_code == 401


# ------------------------------------------------------------------ PUT arm
async def test_arm_needs_fresh_2fa(live_ok):
    r = await _put(live_ok, {"strategy": "maker", "armed": True}, two_fa=False)
    assert r.status_code == 403 and r.json()["detail"] == "2fa_required"
    assert live_ok.runner.armed("maker") is False


async def test_arm_refused_when_live_not_allowed(env, monkeypatch):
    monkeypatch.setattr(pf, "run_preflight", _ready())
    r = await _put(env, {"strategy": "maker", "armed": True})
    assert r.status_code == 409
    assert env.runner.armed("maker") is False
    assert (await _audits(env))[0]["ok"] is False


async def test_arm_refused_with_the_failing_preflight_checks(live_ok, monkeypatch):
    monkeypatch.setattr(pf, "run_preflight", _ready(failed=("clock", "release")))
    r = await _put(live_ok, {"strategy": "arbitrage", "armed": True})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert [d["id"] for d in detail] == ["clock", "release"]
    assert all(d["loc"][0] == "preflight" and d["msg"] for d in detail)
    assert live_ok.runner.armed("arbitrage") is False
    a = (await _audits(live_ok))[0]
    assert a["ok"] is False and a["details"]["failed"] == ["clock", "release"]


async def test_arm_with_the_real_preflight_not_ready_is_refused(live_ok, monkeypatch):
    monkeypatch.setattr(pf, "run_preflight", _real_run_preflight)    # real checks: no release, no snapshot here
    r = await _put(live_ok, {"strategy": "maker", "armed": True})
    assert r.status_code == 409 and live_ok.runner.armed("maker") is False


async def test_arm_maker_persists_and_survives_a_restart(live_ok):
    r = await _put(live_ok, {"strategy": "maker", "armed": True})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["maker"]["armed"] is True and b["arbitrage"]["armed"] is False and b["live_allowed"] is True
    assert b["updated"]
    stored = (await live_ok.store.get_cloud_settings())[armsvc.CLOUD_KEY]
    assert stored["maker"] is True and stored["arbitrage"] is False and stored["by"] == "owner"
    a = (await _audits(live_ok))[0]
    assert a["ok"] is True and a["details"] == {"strategy": "maker", "armed": True}
    fresh = Runner(live_ok.settings, live_ok.store, kalshi_factory=lambda m: live_ok.kalshi)
    await fresh.rehydrate()
    assert fresh.armed("maker") is True and fresh.armed("arbitrage") is False


async def test_arming_is_allowed_while_the_mode_is_demo(live_ok):
    """Lead decision 2026-11-07: arm in demo (preflight target=live), then switch to live. Demo is unaffected."""
    assert live_ok.runner.mode == "demo"
    r = await _put(live_ok, {"strategy": "maker", "armed": True})
    assert r.status_code == 200, r.text
    assert r.json()["maker"] == {"armed": True, "mode": "demo"}
    assert live_ok.runner._skip_reason() in (None, "stopped")     # demo behaviour unchanged


async def test_arming_in_demo_with_the_real_preflight_snapshot_warning(live_ok):
    """The real check: in demo mode a fresh demo snapshot gives the live-target snapshot check a warn, not a fail."""
    from datetime import datetime, timezone
    await live_ok.store.add_account_snapshot({"ts": datetime.now(timezone.utc), "cash": 1.0, "exposure": 0.0,
                                              "env": "demo"})
    out = await _real_run_preflight("live", runner=live_ok.runner, store=live_ok.store, settings=live_ok.settings,
                                    app_state=live_ok.app.state)
    snap = next(c for c in out["checks"] if c["id"] == "snapshot")
    assert snap["status"] == "warn" and snap["detail"].startswith(pf.LIVE_SNAPSHOT_PENDING)


async def test_arm_strategies_separately(live_ok):
    assert (await _put(live_ok, {"strategy": "arbitrage", "armed": True})).status_code == 200
    assert live_ok.runner.armed("arbitrage") and not live_ok.runner.armed("maker")
    assert (await _put(live_ok, {"strategy": "maker", "armed": True})).status_code == 200
    stored = (await live_ok.store.get_cloud_settings())[armsvc.CLOUD_KEY]
    assert stored["maker"] is True and stored["arbitrage"] is True


async def test_arm_with_a_failing_store_is_not_armed(live_ok, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(live_ok.store, "save_cloud_settings", boom)
    r = await _put(live_ok, {"strategy": "maker", "armed": True})
    assert r.status_code == 503 and live_ok.runner.armed("maker") is False


@pytest.mark.parametrize("body", [{"strategy": "arb", "armed": True}, {"strategy": "maker", "armed": "true"},
                                  {"strategy": "maker"}, {"strategy": "maker", "armed": False, "x": 1}])
async def test_bad_bodies_are_422(live_ok, body):
    r = await _put(live_ok, body)
    assert r.status_code == 422


# ------------------------------------------------------------------ PUT disarm
async def test_disarm_needs_no_2fa_and_cancels_live_maker_orders(live_ok):
    await _put(live_ok, {"strategy": "maker", "armed": True})
    live_ok.runner.mode = "live"
    r = await _put(live_ok, {"strategy": "maker", "armed": False}, two_fa=False)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["maker"]["armed"] is False and b["result"]["cancelled"] == 2 and b["result"]["persisted"] is True
    assert sorted(live_ok.kalshi.cancelled) == ["o1", "o3"]          # mm- orders only
    assert (await live_ok.store.get_cloud_settings())[armsvc.CLOUD_KEY]["maker"] is False
    assert any(a["ok"] and a["details"]["armed"] is False for a in await _audits(live_ok))


async def test_disarm_in_demo_cancels_nothing(live_ok):
    await _put(live_ok, {"strategy": "maker", "armed": True})
    r = await _put(live_ok, {"strategy": "maker", "armed": False}, two_fa=False)
    assert r.status_code == 200 and r.json()["result"]["cancelled"] == 0 and live_ok.kalshi.cancelled == []


async def test_disarm_arbitrage_cancels_nothing(live_ok):
    await _put(live_ok, {"strategy": "arbitrage", "armed": True})
    live_ok.runner.mode = "live"
    r = await _put(live_ok, {"strategy": "arbitrage", "armed": False}, two_fa=False)
    assert r.json()["arbitrage"]["armed"] is False and live_ok.kalshi.cancelled == []


async def test_disarm_with_a_failing_store_stays_disarmed(live_ok, monkeypatch):
    await _put(live_ok, {"strategy": "maker", "armed": True})

    async def boom(*a, **k):
        raise RuntimeError("atlas down")
    monkeypatch.setattr(live_ok.store, "save_cloud_settings", boom)
    r = await _put(live_ok, {"strategy": "maker", "armed": False}, two_fa=False)
    assert r.status_code == 200 and r.json()["result"]["persisted"] is False
    assert live_ok.runner.armed("maker") is False
    live_ok.runner._apply_arming({"maker": True, "arbitrage": False})     # a stale store read: disarm still wins
    assert live_ok.runner.armed("maker") is False


async def test_disarm_cancel_is_single_flight(live_ok, monkeypatch):
    """Repeated disarms never hold more than one of Stop's cancel workers."""
    r = live_ok.runner
    r.arming = {**r.arming, "maker": True}
    r.mode = "live"
    gate, calls = threading.Event(), []

    def slow_cancel(mode):
        calls.append(mode)
        gate.wait(5)
        return 0
    monkeypatch.setattr(r, "_cancel_resting", slow_cancel)
    first = asyncio.ensure_future(r.set_armed("maker", False, by="a"))
    await asyncio.sleep(0.05)
    second = await r.set_armed("maker", False, by="b")
    assert "cancel: already running" in second["errors"]
    gate.set()
    await first
    assert calls == ["live"]


# ------------------------------------------------------------------ runner gates
async def test_live_maker_pass_needs_arming_demo_does_not(live_ok):
    r = live_ok.runner
    r.desired = "running"
    assert r._skip_reason() is None                    # demo: unaffected by arming
    r.mode = "live"
    assert r._skip_reason() == "not_armed"
    r.arming = {**r.arming, "maker": True}
    assert r._skip_reason() is None


async def test_pass_started_then_disarmed_sends_nothing(live_ok, monkeypatch):
    r = live_ok.runner
    r.desired, r.mode = "running", "live"
    ran = []
    monkeypatch.setattr(r.ms, "main", lambda *a, **k: ran.append(1) or 0)
    await r._pass("t")                                 # not armed: skipped before the order step
    assert ran == []
    assert await live_ok.store.last_heartbeat() is None


async def test_start_in_live_needs_the_maker_armed(live_ok):
    live_ok.runner.mode = "live"
    r = await live_ok.client.post("/api/bot/start", headers=await live_ok.h2fa())
    assert r.status_code == 409 and r.json()["detail"] == "maker_not_armed"
    live_ok.runner.arming = {**live_ok.runner.arming, "maker": True}
    r = await live_ok.client.post("/api/bot/start", headers=await live_ok.h2fa())
    assert r.status_code == 200, r.text


async def test_arb_live_needs_arbitrage_armed(live_ok):
    r = live_ok.runner
    with pytest.raises(PermissionError, match="arbitrage_not_armed"):
        await r.set_arb(enabled=True, live=True, by="owner")
    r.arming = {**r.arming, "arbitrage": True}
    await r.set_arb(enabled=True, live=True, by="owner")
    assert r.arb_exec_kind() == "live" and r.arb_state() == "live"
    r.arming = {**r.arming, "arbitrage": False}        # disarmed: the next scan is log-only, no set is sent
    assert r.arb_exec_kind() is None and r.arb_state() == "log-only"
    assert r._arb_live_ok(r._stop_gen) is False


async def test_leader_adopts_a_disarm_from_the_store_never_an_arm(live_ok):
    r = live_ok.runner
    r.arming = {**r.arming, "maker": True}
    await live_ok.store.save_cloud_settings({armsvc.CLOUD_KEY: {"maker": False, "arbitrage": True,
                                                                "by": "other"}}, by="other")
    await r._sync_arming_from_store()
    assert r.armed("maker") is False and r.armed("arbitrage") is False


async def test_stop_never_touches_arming(live_ok):
    await _put(live_ok, {"strategy": "maker", "armed": True})
    res = await live_ok.client.post("/api/bot/stop", headers=live_ok.auth)
    assert res.status_code == 200 and res.json()["desired"] == "stopped"
    assert live_ok.runner.armed("maker") is True


async def test_status_reports_arming(live_ok):
    st = live_ok.runner.status()
    assert st["arming"] == {"maker": False, "arbitrage": False}


def test_normalize_fails_closed():
    assert armsvc.normalize({"maker": "true", "arbitrage": 1}) == armsvc.default()
    assert armsvc.normalize(None) == armsvc.default()
    assert armsvc.normalize({"maker": True})["maker"] is True
    with pytest.raises(ValueError):
        armsvc.check_strategy("arb")



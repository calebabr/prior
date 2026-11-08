import pytest

pytestmark = pytest.mark.asyncio


async def test_mode_needs_2fa(env):
    r = await env.client.put("/api/mode", headers=env.auth, json={"mode": "demo"})
    assert r.status_code == 403


async def test_live_without_confirm_is_400(env):
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "live"})
    assert r.status_code == 400 and env.runner.mode == "demo"


async def test_live_wrong_confirm_400(env):
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "live", "confirm": "live"})
    assert r.status_code == 400


async def test_live_with_confirm_but_env_not_allowed_is_409(env, real_strategy):
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "live", "confirm": "LIVE"})
    assert r.status_code == 409 and r.json()["detail"] == "live_not_allowed"
    assert env.runner.mode == "demo"


async def test_live_allowed_when_env_set(env, monkeypatch, real_strategy):
    from control_api import runner as rn
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "x")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY", "y")
    monkeypatch.setenv("KALSHI_LIVE_CONFIRM", rn.LIVE_CONFIRM)
    monkeypatch.setattr(env.settings, "prod_keys_present", True)   # startup snapshot (S-19)
    monkeypatch.setattr(env.settings, "live_confirm_ok", True)
    from control_api.tests.conftest import preflight_ready
    preflight_ready(monkeypatch)                    # S3 L-1: live mode re-runs the go-live preflight
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "live", "confirm": "LIVE"})
    assert r.status_code == 200 and env.runner.mode == "live"
    assert (await env.store.get_state())["mode"] == "live"


async def test_demo_mode_ok_and_persisted(env):
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "demo"})
    assert r.status_code == 200 and (await env.store.get_state())["mode"] == "demo"


async def test_invalid_mode_422(env):
    r = await env.client.put("/api/mode", headers=await env.h2fa(), json={"mode": "paper"})
    assert r.status_code == 422


async def test_start_persists_running_and_is_audited(env):
    r = await env.client.post("/api/bot/start", headers=await env.h2fa())
    assert r.status_code == 200
    assert (await env.store.get_state())["desired"] == "running"
    assert any(a.get("action") == "bot.start" for a in await env.store.list_audit())


async def test_start_reused_code_rejected(env):
    h = await env.h2fa()
    assert (await env.client.post("/api/bot/start", headers=h)).status_code == 200
    assert (await env.client.post("/api/bot/start", headers=h)).status_code == 403


async def test_start_wrong_code_rejected(env):
    h = {**env.auth, "X-2FA-Code": "123456"}
    assert (await env.client.post("/api/bot/start", headers=h)).status_code == 403
    assert env.runner.desired == "stopped"

"""`record_decisions`: an optional on/off strategy setting (default off, observation only), read and written through
the normal maker settings API (fresh 2FA, validate_config + API checks, store version, atomic file write, audit)."""
from __future__ import annotations

import pytest

from control_api.services import botfiles
from control_api.services import handover as hosvc

pytestmark = pytest.mark.asyncio


async def _put(env, body):
    return await env.client.put("/api/config", headers=await env.h2fa(), json=body)


async def test_get_config_shows_the_default_off_and_the_schema(env):
    r = await env.client.get("/api/config", headers=env.auth)
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["record_decisions"] is False
    spec = body["schema"]["record_decisions"]
    assert spec["type"] == "boolean" and spec["default"] is False and spec["editable"] is True
    assert "record_decisions" not in botfiles.read_config_file(env.runner.config_path)   # not stored implicitly


async def test_put_turns_it_on_and_off_with_version_file_and_audit(env):
    r = await _put(env, {"record_decisions": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["record_decisions"] is True
    assert body["changes"] == [{"key": "record_decisions", "from": False, "to": True}]
    assert botfiles.read_config_file(env.runner.config_path)["record_decisions"] is True
    assert env.runner.config["record_decisions"] is True
    assert (await env.store.get_config())["record_decisions"] is True
    audits = [a for a in await env.store.list_audit(limit=20) if a.get("action") == "config.update"]
    assert audits and audits[0].get("ok", True) is not False
    r = await _put(env, {"record_decisions": False})
    assert r.status_code == 200 and r.json()["config"]["record_decisions"] is False
    assert (await env.client.get("/api/config", headers=env.auth)).json()["config"]["record_decisions"] is False


async def test_put_needs_fresh_2fa(env):
    r = await env.client.put("/api/config", headers=env.auth, json={"record_decisions": True})
    assert r.status_code == 403
    assert "record_decisions" not in botfiles.read_config_file(env.runner.config_path)


@pytest.mark.parametrize("bad", ["true", 1, 0, "yes", [True], {"on": True}])
async def test_non_boolean_is_400_and_nothing_written(env, bad):
    before = botfiles.read_config_file(env.runner.config_path)
    nver = len(await env.store.list_config_versions())
    r = await _put(env, {"record_decisions": bad})
    assert r.status_code == 400, r.text
    assert "record_decisions" in str(r.json())
    assert botfiles.read_config_file(env.runner.config_path) == before
    assert len(await env.store.list_config_versions()) == nver


async def test_null_is_a_no_op(env):
    r = await _put(env, {"record_decisions": None, "run_budget": 10})
    assert r.status_code == 200 and r.json()["config"]["record_decisions"] is False


async def test_undo_restores_the_previous_value(env):
    assert (await _put(env, {"record_decisions": True})).status_code == 200
    r = await env.client.post("/api/config/undo", headers=await env.h2fa(), json={})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["record_decisions"] is False


def test_stored_config_with_a_bad_value_is_rejected(settings):
    cfg = botfiles.public_config(botfiles.strategy().DEFAULTS)
    botfiles.validate_stored(dict(cfg, record_decisions=True), settings)
    with pytest.raises(ValueError, match="record_decisions"):
        botfiles.validate_stored(dict(cfg, record_decisions="true"), settings)


def test_standin_accepts_it_as_a_plain_optional_boolean():
    ms = botfiles.strategy()
    cfg = dict(ms.DEFAULTS)
    ms.validate_config(cfg)
    ms.validate_config(dict(cfg, record_decisions=True))
    ms.validate_config(dict(cfg, record_decisions=False))
    with pytest.raises(ValueError, match="record_decisions"):
        ms.validate_config(dict(cfg, record_decisions="on"))


def test_an_unset_key_does_not_change_the_handover_config_hash():
    cfg = botfiles.public_config(botfiles.strategy().DEFAULTS)
    assert hosvc.config_hash(botfiles.public_config(cfg)) == hosvc.config_hash(dict(cfg))
    assert "record_decisions" not in botfiles.public_config(cfg)          # config_view adds it for display only

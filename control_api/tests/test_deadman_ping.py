"""Dead-man ping (Sprint 3 wave A, infra spec in docs/MONITORING.md): HEALTHCHECKS_PING_URL, off by default, pings the
plain URL when healthy and URL/fail when the bot should be trading but is stalled. The URL is a secret: never logged,
returned or audited, and a ping failure never affects the tick (trading) or Stop."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest

from control_api.services import keepalive as ka
from control_api.services import logbuffer
from control_api.settings import Settings

pytestmark = pytest.mark.asyncio
URL = "https://hc-ping.com/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


def _transport(seen: list, status: int = 200, exc: Exception | None = None):
    def handler(req):
        seen.append(str(req.url))
        if exc is not None:
            raise exc
        return httpx.Response(status, text="OK")
    return httpx.MockTransport(handler)


async def test_setting_is_off_by_default_and_read_from_env_only():
    assert Settings().healthchecks_ping_url is None
    assert Settings.from_env({}).healthchecks_ping_url is None
    assert Settings.from_env({"HEALTHCHECKS_PING_URL": "  "}).healthchecks_ping_url is None
    assert Settings.from_env({"HEALTHCHECKS_PING_URL": URL + "/"}).healthchecks_ping_url == URL


async def test_non_https_url_is_ignored_without_logging_it(caplog):
    caplog.set_level(logging.WARNING)
    bad = "http://hc-ping.com/secret-uuid-value"
    assert Settings.from_env({"HEALTHCHECKS_PING_URL": bad}).healthchecks_ping_url is None
    assert "secret-uuid-value" not in caplog.text and "HEALTHCHECKS_PING_URL ignored" in caplog.text


async def test_settings_repr_never_shows_the_url():
    s = Settings(healthchecks_ping_url=URL)
    assert URL not in repr(s) and "hc-ping" not in repr(s)


async def test_off_by_default_makes_no_call(env):
    seen: list = []
    env.settings.healthchecks_ping_url = None
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) is None
    assert seen == []


async def test_healthy_running_bot_pings_the_success_url(env):
    seen: list = []
    env.settings.healthchecks_ping_url = URL
    env.runner.desired = "running"
    env.runner.last_run_utc = ka.utcnow() - timedelta(minutes=3)
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) == "ok"
    assert seen == [URL]


async def test_stalled_running_bot_pings_fail(env):
    seen: list = []
    env.settings.healthchecks_ping_url = URL
    env.runner.desired = "running"
    env.runner.cloud["silent_alert_minutes"] = 30
    env.runner.last_run_utc = ka.utcnow() - timedelta(minutes=31)
    env.runner._silence_ref = ka.utcnow() - timedelta(minutes=45)
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) == "fail"
    assert seen == [URL + "/fail"]


async def test_fresh_start_gets_the_watchdog_window_before_fail(env):
    seen: list = []
    env.settings.healthchecks_ping_url = URL
    env.runner.desired = "running"
    env.runner.last_run_utc = ka.utcnow() - timedelta(hours=5)       # an old pass, but the bot was just started
    env.runner._silence_ref = ka.utcnow() - timedelta(minutes=2)
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) == "ok"
    env.runner.last_run_utc = env.runner._silence_ref = None          # running, watchdog not looked yet
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) == "ok"
    assert seen == [URL, URL]


async def test_stopped_bot_pings_ok_so_stop_never_raises_an_alarm(env):
    seen: list = []
    env.settings.healthchecks_ping_url = URL
    env.runner.desired = "stopped"
    env.runner.last_run_utc = ka.utcnow() - timedelta(days=2)
    assert await ka.deadman_ping(env.runner, transport=_transport(seen)) == "ok"
    assert seen == [URL]


@pytest.mark.parametrize("exc,status", [(httpx.ConnectError("boom " + URL), 200),
                                        (httpx.ReadTimeout("slow " + URL), 200),
                                        (None, 500)])
async def test_ping_failure_is_ignored_and_never_logs_the_url(env, caplog, exc, status):
    caplog.set_level(logging.DEBUG)
    seen: list = []
    env.settings.healthchecks_ping_url = URL
    assert await ka.deadman_ping(env.runner, transport=_transport(seen, status, exc)) == "error"
    assert seen and URL not in caplog.text and "0b1c2d3e" not in caplog.text


async def test_ping_uses_a_short_timeout():
    assert ka.DEADMAN_TIMEOUT_S <= 3.0


async def test_keepalive_loop_pings_after_the_tick_and_a_failure_never_stops_the_tick(env, monkeypatch):
    order: list[str] = []
    sleeps: list = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    async def fake_ping(runner, transport=None):
        order.append("self-ping")

    async def fake_tick(reason):
        order.append("tick")
        return {"ran": False}

    async def broken_deadman(runner, transport=None):
        order.append("deadman")
        raise RuntimeError("never escapes " + URL)

    monkeypatch.setattr(ka.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ka, "ping_once", fake_ping)
    monkeypatch.setattr(ka, "deadman_ping", broken_deadman)
    monkeypatch.setattr(env.runner, "tick", fake_tick)
    with pytest.raises(asyncio.CancelledError):
        await ka.keepalive_loop(env.runner)
    assert order == ["self-ping", "tick", "deadman", "self-ping", "tick", "deadman"]


async def test_url_is_never_returned_by_the_api_or_kept_in_the_log_buffer(env, caplog):
    env.settings.healthchecks_ping_url = URL
    for path in ("/api/status", "/api/cloud", "/api/health", "/api/logs"):
        r = await env.client.get(path, headers=env.auth)
        assert URL not in r.text and "0b1c2d3e" not in r.text, path
    assert logbuffer.redact(f"GET {URL}/fail") == "GET https://hc-ping.com/[REDACTED]"
    audits = await env.store.list_audit(limit=200) if hasattr(env.store, "list_audit") else []
    assert all(URL not in str(a) for a in audits)

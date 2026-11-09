"""GET /api/preflight (services/preflight.py): read-only go-live checklist. Fakes only; no network."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from control_api import version as ver
from control_api.routes import analytics as anr
from control_api.services import preflight as pf

FAKE_KEY_ID = "FAKEKEYID-preflight-0000-not-real"
FAKE_PEM = "FAKEPEMBODY-preflight-should-never-leak"
FAKE_DEMO_KEY_ID = "FAKEDEMOKEYID-preflight-1111"
FAKE_DEMO_PEM = "FAKEDEMOPEM-preflight-should-never-leak"
SECRET_IN_ERROR = "kalshi-error-body-with-SECRETVALUE"
RUNNING = "9.9.9-test"

pytestmark = pytest.mark.asyncio
COMMIT = "abcdef0123456789abcdef0123456789abcdef01"


class OkKalshi:
    """Read-only fake: balance answers; any order call is recorded (and must never happen)."""

    def __init__(self, skew=0.4, fail=None, delay=0.0):
        self.clock_skew_seconds = None
        self._skew, self.fail, self.delay = skew, fail, delay
        self.writes = []

    def get_balance(self):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.clock_skew_seconds = self._skew
        return {"balance": 123456}

    def place_order(self, *a, **k):
        self.writes.append(("place", a, k))

    def cancel_order(self, *a, **k):
        self.writes.append(("cancel", a, k))

    def get_orders(self, *a, **k):
        return []


def _ok_recon(now=None):
    now = now or datetime.now(timezone.utc)
    return {"ok": True, "day": (now - timedelta(days=1)).date().isoformat(), "mode": "live",
            "checked_at": (now - timedelta(hours=2)).isoformat(), "differences": [], "totals": {}}


@pytest_asyncio.fixture
async def ready_env(env, monkeypatch, real_strategy):
    """Everything set up so a target=live preflight passes every check."""
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY", FAKE_PEM)
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", FAKE_DEMO_KEY_ID)
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", FAKE_DEMO_PEM)
    env.settings.prod_keys_present = True
    env.settings.live_confirm_ok = True
    env.settings.ntfy_topic = "test-topic"
    env.runner.mode = "live"
    env.runner.desired = "running"
    env.kalshi = OkKalshi()
    monkeypatch.setattr(ver, "API_VERSION", RUNNING)
    await env.store.add_audit({"ts": datetime.now(timezone.utc), "user": "owner", "action": "release.approve",
                               "ok": True, "details": {"id": "r1", "version": RUNNING, "commit": COMMIT}})
    await env.store.add_account_snapshot({"ts": datetime.now(timezone.utc) - timedelta(minutes=3), "cash": 50.0,
                                          "exposure": 10.0, "headroom": 5.0, "env": "live"})

    async def latest(mode=None):
        return _ok_recon()
    monkeypatch.setattr(env.store, "latest_recon_report", latest, raising=False)
    return env


async def _get(env, target="live"):
    return await env.client.get("/api/preflight", params={"target": target}, headers=env.auth)


def _by_id(body):
    return {c["id"]: c for c in body["checks"]}


async def test_all_pass_is_ready(ready_env):
    r = await _get(ready_env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ready", "target", "ts", "checks"}
    assert body["target"] == "live"
    checks = _by_id(body)
    assert list(checks) == list(pf.ORDER)
    bad = {k: v for k, v in checks.items() if v["status"] != "pass"}
    assert not bad, bad
    assert body["ready"] is True
    for c in body["checks"]:
        assert set(c) == {"id", "label", "status", "detail"}
        assert c["label"] and isinstance(c["detail"], str)
    assert ready_env.kalshi.writes == []                      # read-only: no order placed or cancelled


def _set(obj, attr, value):
    return lambda env, mp: mp.setattr(obj(env), attr, value)


async def _stale_snapshot(env, mp):
    env.store._snapshots.clear()
    await env.store.add_account_snapshot({"ts": datetime.now(timezone.utc) - timedelta(minutes=30), "cash": 1.0,
                                          "exposure": 0.0, "env": "live"})


async def _wrong_env_snapshot(env, mp):
    await env.store.add_account_snapshot({"ts": datetime.now(timezone.utc), "cash": 1.0, "exposure": 0.0,
                                          "env": "demo"})


def _bad_recon(env, mp):
    async def latest(mode=None):
        return {**_ok_recon(), "ok": False, "differences": [{"kind": "cash", "severity": "error", "delta": 4.0}]}
    mp.setattr(env.store, "latest_recon_report", latest, raising=False)


def _paused(env, mp):
    mp.setattr(env.runner, "arb_contain_status",
               lambda: {"paused_series": [{"series": "KXDEMOA", "kind": "live"}], "contained_24h": {}})


def _no_probe(env, mp):
    def boom(d):
        raise PermissionError("read-only")
    mp.setattr(pf, "_probe_dir", boom)


def _ping_false(env, mp):
    async def ping():
        return False
    mp.setattr(env.store, "ping", ping)


def _kill(env, mp):
    env.runner.kill_path.write_text("x")


def _arb_kill(env, mp):
    env.runner.arb_kill_path.write_text("x")


FAILURES = {
    "keys": lambda env, mp: mp.delenv("KALSHI_PROD_PRIVATE_KEY"),
    "mode": lambda env, mp: setattr(env.settings, "runner_enabled", False),
    "live_gate": lambda env, mp: setattr(env.settings, "live_confirm_ok", False),
    "strategy": lambda env, mp: mp.setattr(pf.botfiles, "is_standin", lambda: True),
    "config": lambda env, mp: env.runner.config.__setitem__("poll_minutes", 99),
    "caps": lambda env, mp: env.runner.config.__setitem__("market_open_frac", 1.5),
    "clock": lambda env, mp: setattr(env, "kalshi", OkKalshi(skew=30.0)),
    "store": _ping_false,
    "snapshot": _stale_snapshot,
    "snapshot_env": _wrong_env_snapshot,
    "kalshi": lambda env, mp: setattr(env, "kalshi", OkKalshi(fail=RuntimeError(SECRET_IN_ERROR))),
    "kill_switch": _kill,
    "kill_switch_arb": _arb_kill,
    "stop_path": _no_probe,
    "arb_paused": _paused,
    "alerts": lambda env, mp: setattr(env.settings, "ntfy_topic", None),
    "release": lambda env, mp: mp.setattr(ver, "API_VERSION", "1.0.0-other"),
    "reconciliation": _bad_recon,
}


@pytest.mark.parametrize("case", sorted(FAILURES))
async def test_each_failing_check_flips_ready(ready_env, monkeypatch, case):
    res = FAILURES[case](ready_env, monkeypatch)
    if hasattr(res, "__await__"):
        await res
    r = await _get(ready_env)
    assert r.status_code == 200, r.text
    body = r.json()
    cid = {"snapshot_env": "snapshot", "kill_switch_arb": "kill_switch"}.get(case, case)
    assert _by_id(body)[cid]["status"] == "fail", _by_id(body)[cid]
    assert body["ready"] is False


async def test_standin_with_target_live_fails(env, standin, monkeypatch):
    r = await _get(env, "live")
    assert r.status_code == 200
    body = r.json()
    assert _by_id(body)["strategy"]["status"] == "fail"
    assert "stand-in" in _by_id(body)["strategy"]["detail"]
    assert body["ready"] is False


async def test_demo_target_downgrades_live_only_checks(ready_env, monkeypatch):
    monkeypatch.setattr(pf.botfiles, "is_standin", lambda: True)
    ready_env.settings.live_confirm_ok = False
    monkeypatch.setattr(ver, "API_VERSION", "0.0.0-dev")
    ready_env.runner.mode = "demo"
    await ready_env.store.add_account_snapshot({"ts": datetime.now(timezone.utc), "cash": 1.0, "exposure": 0.0,
                                                "env": "demo"})
    body = (await _get(ready_env, "demo")).json()
    checks = _by_id(body)
    for cid in ("strategy", "live_gate", "release"):
        assert checks[cid]["status"] == "warn", checks[cid]
        assert checks[cid]["detail"].startswith("live only: ")
    assert body["target"] == "demo" and body["ready"] is True, checks


async def test_demo_target_still_fails_non_live_checks(ready_env, monkeypatch):
    ready_env.runner.mode = "demo"
    monkeypatch.delenv("KALSHI_DEMO_API_KEY_ID")
    body = (await _get(ready_env, "demo")).json()
    assert _by_id(body)["keys"]["status"] == "fail"
    assert body["ready"] is False


@pytest.mark.parametrize("params", [{"target": "prod"}, {"target": ""}, {}])
async def test_unknown_or_missing_target_is_422(env, params):
    r = await env.client.get("/api/preflight", params=params, headers=env.auth)
    assert r.status_code == 422


async def test_requires_login(env):
    r = await env.client.get("/api/preflight", params={"target": "live"})
    assert r.status_code == 401


async def test_secrets_never_in_response(ready_env, monkeypatch):
    ready_env.kalshi = OkKalshi(fail=RuntimeError(SECRET_IN_ERROR))
    ready_env.settings.ntfy_topic = "private-ntfy-topic-name"
    for target in ("live", "demo"):
        text = (await _get(ready_env, target)).text
        for secret in (FAKE_KEY_ID, FAKE_PEM, FAKE_DEMO_KEY_ID, FAKE_DEMO_PEM, SECRET_IN_ERROR,
                       "private-ntfy-topic-name", "123456"):
            assert secret not in text, secret
    audits = json.dumps(await ready_env.store.list_audit(limit=50), default=str)
    for secret in (FAKE_KEY_ID, FAKE_PEM, SECRET_IN_ERROR):
        assert secret not in audits


async def test_live_run_is_audited_demo_is_not(ready_env):
    await _get(ready_env, "live")
    await _get(ready_env, "demo")
    rows = [a for a in await ready_env.store.list_audit(limit=50) if a["action"].startswith("preflight")]
    assert len(rows) == 1
    assert rows[0]["action"] == "preflight.live" and rows[0]["ok"] is True
    assert rows[0]["details"]["target"] == "live" and rows[0]["details"]["failed"] == []


async def test_failed_live_run_audit_lists_failed_ids(ready_env):
    ready_env.settings.ntfy_topic = None
    await _get(ready_env, "live")
    row = [a for a in await ready_env.store.list_audit(limit=50) if a["action"] == "preflight.live"][0]
    assert row["ok"] is False and row["details"]["failed"] == ["alerts"]


async def test_rate_limited(ready_env):
    codes = [(await _get(ready_env, "demo")).status_code for _ in range(pf_limit() + 1)]
    assert codes[-1] == 429 and all(c == 200 for c in codes[:-1])


def pf_limit():
    from control_api.routes import preflight as route
    return route.LIMIT[0]


async def test_check_that_raises_fails_closed(ready_env, monkeypatch):
    def boom():
        raise OSError("disk")
    monkeypatch.setattr(ready_env.runner, "kill_switch", boom)
    body = (await _get(ready_env)).json()
    ks = _by_id(body)["kill_switch"]
    assert ks["status"] == "fail" and "could not run" in ks["detail"]
    assert body["ready"] is False


async def test_store_down_fails_closed(ready_env, monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("atlas down")
    for name in ("ping", "list_account_snapshots", "list_push_subscriptions", "list_audit", "server_time",
                 "latest_recon_report"):
        monkeypatch.setattr(ready_env.store, name, down, raising=False)
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store,
                                 settings=ready_env.settings, app_state=ready_env.app.state)
    checks = _by_id(out)
    assert checks["store"]["status"] == "fail"
    assert checks["snapshot"]["status"] == "fail"
    assert checks["release"]["status"] == "fail"
    assert checks["reconciliation"]["status"] == "warn"
    assert checks["alerts"]["status"] == "pass"            # ntfy alone is enough
    assert out["ready"] is False


async def test_kalshi_timeout_fails(ready_env):
    ready_env.kalshi = OkKalshi(delay=0.3)
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store,
                                 settings=ready_env.settings, app_state=ready_env.app.state, kalshi_timeout=0.05)
    assert _by_id(out)["kalshi"]["status"] == "fail"
    assert "no answer" in _by_id(out)["kalshi"]["detail"]


async def test_no_kalshi_client_fails(ready_env, monkeypatch):
    monkeypatch.setattr(ready_env.runner, "kalshi_factory", lambda mode: None)
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store,
                                 settings=ready_env.settings, app_state=None)
    assert _by_id(out)["kalshi"]["status"] == "fail"


async def test_clock_falls_back_to_store_and_fails_without_any_reference(ready_env, monkeypatch):
    ready_env.kalshi = OkKalshi(skew=None)

    async def store_now():
        return datetime.now(timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr(ready_env.store, "server_time", store_now)
    # S3 item 9: live no longer accepts the database clock instead of Kalshi's time (fails closed)
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
    assert _by_id(out)["clock"]["status"] == "fail" and "Kalshi" in _by_id(out)["clock"]["detail"]
    # demo may still fall back to the database clock
    ready_env.runner.mode = "demo"
    out = await pf.run_preflight("demo", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
    assert _by_id(out)["clock"]["status"] == "pass" and "database" in _by_id(out)["clock"]["detail"]

    async def none():
        return None
    monkeypatch.setattr(ready_env.store, "server_time", none)
    out = await pf.run_preflight("demo", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
    assert _by_id(out)["clock"]["status"] == "warn"


# ---------------------------------------------------------------- S3 item 9: Kalshi time source
class TimeKalshi:
    """Only the public exchange-status call answers (Date header skew). Anything else is recorded and must not happen."""

    def __init__(self, skew=0.4, fail=None, delay=0.0):
        self.clock_skew_seconds = None
        self._skew, self.fail, self.delay = skew, fail, delay
        self.calls = []

    def get_exchange_status(self):
        self.calls.append("status")
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.clock_skew_seconds = self._skew
        return {"exchange_active": True}

    def __getattr__(self, name):
        if name.startswith(("place", "cancel", "get_balance", "get_orders", "get_positions", "amend")):
            def rec(*a, **k):
                self.calls.append(name)
            return rec
        raise AttributeError(name)


class _R:
    def __init__(self, client):
        self.client = client
        self.kalshi_factory = lambda mode: client


async def test_time_probe_reads_skew_and_makes_no_auth_or_order_calls():
    c = TimeKalshi(skew=1.5)
    assert await pf._kalshi_time_probe(_R(c), "live") == 1.5
    assert c.calls == ["status"]


async def test_time_probe_unreadable_and_timeout_give_none():
    assert await pf._kalshi_time_probe(_R(TimeKalshi(fail=RuntimeError("x"))), "live") is None
    assert await pf._kalshi_time_probe(_R(TimeKalshi(skew=None)), "live") is None
    assert await pf._kalshi_time_probe(_R(TimeKalshi(delay=0.5)), "live", timeout=0.05) is None
    assert await pf._kalshi_time_probe(_R(None), "live") is None


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def test_check_clock_live_fail_closed_demo_warns():
    assert pf.check_clock(0.4, None, NOW, "live")[0] == "pass"
    assert pf.check_clock(30.0, None, NOW, "live")[0] == "fail"
    assert pf.check_clock(30.0, None, NOW, "demo")[0] == "warn"
    assert pf.check_clock(None, NOW, NOW, "live")[0] == "fail"       # database clock is no substitute for live
    assert pf.check_clock(None, NOW, NOW, "demo")[0] == "pass"
    assert pf.check_clock(None, None, NOW, "demo")[0] == "warn"
    assert pf.check_clock(None, None, NOW, "live")[0] == "fail"


async def test_run_preflight_uses_the_time_probe_for_the_clock(ready_env):
    ready_env.kalshi = OkKalshi(skew=0.2)
    # time probe only: patch it, leaving the authenticated probe on OkKalshi
    async def probe(runner, target, timeout=3.0):
        return 40.0
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        mp.setattr(pf, "_kalshi_time_probe", probe)
        out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
        assert _by_id(out)["clock"]["status"] == "fail" and "+40" in _by_id(out)["clock"]["detail"].replace("-40", "+40")
        out = await pf.run_preflight("demo", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
        assert _by_id(out)["clock"]["status"] == "warn"
    finally:
        mp.undo()


async def test_alerts_pass_with_push_only(ready_env, monkeypatch):
    ready_env.settings.ntfy_topic = None
    ready_env.settings.vapid_private_key = "x" * 10

    async def subs():
        return [{"sub_id": "a"}]
    monkeypatch.setattr(ready_env.store, "list_push_subscriptions", subs)
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store, settings=ready_env.settings)
    assert _by_id(out)["alerts"]["status"] == "pass"


async def test_reconciliation_not_checked_is_warn(ready_env, monkeypatch):
    async def none(mode=None):
        return None
    monkeypatch.setattr(ready_env.store, "latest_recon_report", none, raising=False)
    anr.clear_cache(ready_env.app)
    body = (await _get(ready_env)).json()
    rc = _by_id(body)["reconciliation"]
    assert rc["status"] == "warn" and "not checked" in rc["detail"]
    assert body["ready"] is True


async def test_old_recon_report_falls_back_to_ledger_cache(ready_env, monkeypatch):
    async def old(mode=None):
        return _ok_recon(datetime.now(timezone.utc) - timedelta(days=5))
    monkeypatch.setattr(ready_env.store, "latest_recon_report", old, raising=False)
    now = datetime.now(timezone.utc)
    ready_env.store._snapshots.clear()
    for mins in (60 * 24 * 8, 2):          # one before the 7-day window, one now; same equity -> residual 0
        await ready_env.store.add_account_snapshot({"ts": now - timedelta(minutes=mins), "cash": 40.0,
                                                    "exposure": 20.0, "env": "live"})
    st = anr._state(type("R", (), {"app": ready_env.app})())
    st["rows"][("live", anr.WARM_DAYS, None)] = (anr._clock(), [])
    out = await pf.run_preflight("live", runner=ready_env.runner, store=ready_env.store,
                                 settings=ready_env.settings, app_state=ready_env.app.state)
    rc = _by_id(out)["reconciliation"]
    assert rc["status"] == "pass" and "$+0.00" in rc["detail"], rc


async def test_unit_release_match_rules():
    assert pf._version_match("1.2.3", "1.2.3", None)
    assert pf._version_match("abcdef0", "x", "abcdef0123")
    assert not pf._version_match("0.0.0-dev", "0.0.0-dev", None)
    assert not pf._version_match("abc", "x", "abcdef0")          # short prefixes never match
    assert pf.check_release(None, "1.2.3")[0] == "fail"


async def test_unit_unknown_target_raises():
    with pytest.raises(ValueError):
        await pf.run_preflight("prod", runner=None, store=None, settings=None)


# ------------------------------------------------------------------ lead decision 2026-11-07: arming while in demo
async def test_live_target_in_demo_mode_warns_on_a_fresh_demo_snapshot(ready_env):
    ready_env.runner.mode = "demo"
    ready_env.store._snapshots.clear()
    await ready_env.store.add_account_snapshot({"ts": datetime.now(timezone.utc), "cash": 1.0, "exposure": 0.0,
                                                "env": "demo"})
    body = (await _get(ready_env, "live")).json()
    snap = _by_id(body)["snapshot"]
    assert snap["status"] == "warn", snap
    assert snap["detail"].startswith(pf.LIVE_SNAPSHOT_PENDING)
    assert pf.LIVE_SNAPSHOT_PENDING == "live-tagged snapshot appears after the first live pass; verify then"
    assert body["ready"] is True, [c for c in body["checks"] if c["status"] == "fail"]


@pytest.mark.parametrize("mins,tag", [(30, "demo"), (21, "live")])     # untagged: unit test (the store tags)
async def test_live_target_in_demo_mode_still_needs_a_fresh_tagged_snapshot(ready_env, mins, tag):
    ready_env.runner.mode = "demo"
    ready_env.store._snapshots.clear()
    await ready_env.store.add_account_snapshot({"ts": datetime.now(timezone.utc) - timedelta(minutes=mins),
                                                "cash": 1.0, "exposure": 0.0, "env": tag})
    body = (await _get(ready_env, "live")).json()
    assert _by_id(body)["snapshot"]["status"] == "fail" and body["ready"] is False


async def test_live_target_in_demo_mode_with_no_snapshot_fails(ready_env):
    ready_env.runner.mode = "demo"
    ready_env.store._snapshots.clear()
    body = (await _get(ready_env, "live")).json()
    assert _by_id(body)["snapshot"]["status"] == "fail" and body["ready"] is False


@pytest.mark.parametrize("mins,tag", [(30, "live"), (2, "demo")])
async def test_live_target_in_live_mode_fails_on_stale_or_demo_snapshot(ready_env, mins, tag):
    ready_env.store._snapshots.clear()
    await ready_env.store.add_account_snapshot({"ts": datetime.now(timezone.utc) - timedelta(minutes=mins),
                                                "cash": 1.0, "exposure": 0.0, "env": tag})
    body = (await _get(ready_env, "live")).json()
    assert ready_env.runner.mode == "live"
    assert _by_id(body)["snapshot"]["status"] == "fail" and body["ready"] is False


def test_unit_check_snapshot_rules():
    now = datetime.now(timezone.utc)
    fresh = {"ts": now - timedelta(minutes=1)}
    assert pf.check_snapshot({**fresh, "env": "demo"}, "demo", "live", now)[0] == "warn"
    assert pf.check_snapshot({**fresh, "env": "live"}, "live", "live", now)[0] == "pass"
    assert pf.check_snapshot({**fresh, "env": "demo"}, "live", "live", now)[0] == "fail"
    assert pf.check_snapshot({**fresh, "env": "demo"}, "demo", "demo", now)[0] == "pass"
    assert pf.check_snapshot({**fresh, "env": "live"}, "demo", "demo", now)[0] == "fail"
    assert pf.check_snapshot({**fresh}, "demo", "live", now)[0] == "fail"

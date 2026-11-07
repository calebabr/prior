"""R2/R3: graceful shutdown + signed handover (A2). A valid token keeps LIVE across a planned deploy; every other
case drops to demo (S-21). Stop during handover always ends stopped."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from control_api.db import MemoryStore
from control_api.runner import Runner
from control_api.services import handover as ho

KEY = "k" * 40


class RecordingAlerter:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", priority="default"):
        self.sent.append((title, body, priority))
        return True


# ------------------------------------------------------------------ token unit tests
def _tok(**over):
    now = over.pop("now", datetime.now(timezone.utc))
    t = ho.make_token(KEY.encode(), from_instance="old", mode="live", desired="running", config_hash="h",
                      stop_seq="s1", now=now)
    t.update(over)
    return t


def _verify(t, key=KEY.encode(), **kw):
    args = {"config_hash": "h", "stop_seq": "s1", "now": datetime.now(timezone.utc), "max_age_s": 300}
    args.update(kw)
    return ho.verify(key, t, **args)


def test_valid_token_verifies():
    assert _verify(_tok()) is None


@pytest.mark.parametrize("change,reason", [
    ({"mode": "demo"}, "bad_signature"),            # any field change breaks the signature
    ({"desired": "stopped"}, "bad_signature"),
    ({"config_hash": "other"}, "bad_signature"),
    ({"sig": "0" * 64}, "bad_signature"),
    ({"sig": "short"}, "bad_signature"),
    ({"sig": None}, "bad_signature"),
])
def test_tampered_token_refused(change, reason):
    assert _verify(_tok(**change)) == reason


def test_refusal_reasons():
    assert _verify(_tok(), key=None) == "no_key"
    assert _verify(None) == "no_token"
    assert _verify(_tok(), key=b"x" * 40) == "bad_signature"          # forged with another key
    assert _verify(_tok(now=datetime.now(timezone.utc) - timedelta(seconds=301))) == "expired"
    assert _verify(_tok(now=datetime.now(timezone.utc) + timedelta(seconds=120))) == "issued_in_future"
    assert _verify(_tok(), config_hash="changed") == "config_changed"
    assert _verify(_tok(), stop_seq="s2") == "state_changed"
    assert _verify(_tok(), stop_seq=None) == "state_changed"
    t = ho.make_token(KEY.encode(), from_instance="old", mode="demo", desired="running", config_hash="h",
                      stop_seq="s1", now=datetime.now(timezone.utc))
    assert _verify(t) == "not_live_running"


def test_short_or_missing_key_disables_handover():
    class S:
        handover_hmac_key = "short"
    assert ho.key_bytes(S()) is None
    S.handover_hmac_key = None
    assert ho.key_bytes(S()) is None
    S.handover_hmac_key = KEY
    assert ho.key_bytes(S()) == KEY.encode()


def test_config_hash_is_canonical():
    assert ho.config_hash({"a": 1, "b": [1, 2]}) == ho.config_hash({"b": [1, 2], "a": 1})
    assert ho.config_hash({"a": 1}) != ho.config_hash({"a": 2})


# ------------------------------------------------------------------ runner-level handover
@pytest.fixture
def live_env(settings, bot_paths, kalshi, monkeypatch, real_strategy):
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "fake-id")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY", "fake-key")
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    monkeypatch.setattr(settings, "prod_keys_present", True)
    monkeypatch.setattr(settings, "live_confirm_ok", True)
    monkeypatch.setattr(settings, "handover_hmac_key", KEY)
    return settings


def _runner(settings, store, kalshi):
    r = Runner(settings, store, kalshi_factory=lambda m: kalshi)
    r.alerter = RecordingAlerter()
    return r


async def _old_leader_live(settings, store, kalshi):
    """A leader running LIVE (owner re-armed LIVE after its own restart)."""
    await store.set_state(by="owner", desired="running", mode="demo")
    old = _runner(settings, store, kalshi)
    await old.rehydrate()
    assert old.role() == "leader"
    await store.set_state(by="owner", mode="live")
    old.mode = "live"
    return old


@pytest.mark.asyncio
async def test_planned_handover_in_live_keeps_live(live_env, kalshi):
    store = MemoryStore()
    old = await _old_leader_live(live_env, store, kalshi)
    await old.shutdown()
    lease = await store.get_lease()
    tok = lease["handover"]
    assert tok and tok["mode"] == "live" and KEY not in str(tok)          # signed, key never stored
    new = _runner(live_env, store, kalshi)
    await new.rehydrate()
    assert new.role() == "leader" and new.lease.generation == 2
    assert new.mode == "live" and new.desired == "running"
    assert new.handover_result["kept_live"] is True
    assert (await store.get_state())["mode"] == "live"
    assert any("LIVE continued" in t for t, _, _ in new.alerter.sent)
    # replay: the same token a second time (e.g. a third instance) drops to demo
    await new.shutdown()                          # writes a NEW token; restore the old one to replay it
    lease_doc = store._lease
    lease_doc["handover"] = tok
    third = _runner(live_env, store, kalshi)
    await third.rehydrate()
    assert third.mode == "demo" and third.handover_result["reason"] == "replayed"


@pytest.mark.asyncio
async def test_planned_handover_in_demo_continues_running(settings, bot_paths, kalshi, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    old = _runner(settings, store, kalshi)
    await old.rehydrate()
    await old.shutdown()
    assert (await store.get_lease())["handover"] is None                  # no token in demo
    new = _runner(settings, store, kalshi)
    await new.rehydrate()
    assert new.role() == "leader" and new.desired == "running" and new.mode == "demo"


@pytest.mark.parametrize("case", ["no_key", "short_key", "expired", "config_changed", "stop_since",
                                  "bad_signature", "state_write_since"])
@pytest.mark.asyncio
async def test_invalid_handover_drops_to_demo(live_env, kalshi, monkeypatch, case):
    store = MemoryStore()
    old = await _old_leader_live(live_env, store, kalshi)
    if case == "no_key":
        monkeypatch.setattr(live_env, "handover_hmac_key", None)
    if case == "short_key":
        monkeypatch.setattr(live_env, "handover_hmac_key", "x" * 31)
    await old.shutdown()
    if case in ("no_key", "short_key"):
        assert (await store.get_lease())["handover"] is None
        monkeypatch.setattr(live_env, "handover_hmac_key", KEY)          # even if the new side has the key
    if case == "expired":
        tok = store._lease["handover"]
        store._lease["handover"] = ho.make_token(KEY.encode(), **{k: tok[k] for k in (
            "from_instance", "mode", "desired", "config_hash", "stop_seq")},
            now=datetime.now(timezone.utc) - timedelta(seconds=400))
    if case == "config_changed":
        cfg = await store.get_config()
        await store.save_config({**cfg, "run_budget": 7}, before=cfg, by="owner")
    if case == "bad_signature":
        store._lease["handover"]["config_hash"] = "0" * 64              # attacker with Atlas write access
    if case == "stop_since":
        await old.stop(by="owner")                                       # Stop after the handover was written
        await store.set_state(by="attacker", desired="running", mode="live")
    if case == "state_write_since":
        await store.set_state(by="attacker", mode="live")               # any state write since the token
    new = _runner(live_env, store, kalshi)
    await new.rehydrate()
    assert new.mode == "demo", case
    assert (await store.get_state())["mode"] == "demo"
    if case == "stop_since":
        assert new.desired == "stopped"          # the old process's kill file is shared here: Stop wins


@pytest.mark.asyncio
async def test_stop_during_handover_ends_stopped(live_env, kalshi, monkeypatch):
    """Stop lands while the old leader builds its handover: no token, and the new leader starts stopped."""
    store = MemoryStore()
    old = await _old_leader_live(live_env, store, kalshi)
    real_get = store.get_state

    async def get_state_then_stop():
        st = await real_get()
        await old.stop(by="owner")                                       # Stop arrives mid-handover
        return st
    monkeypatch.setattr(store, "get_state", get_state_then_stop)
    await old.shutdown()
    monkeypatch.setattr(store, "get_state", real_get)
    assert (await store.get_lease())["handover"] is None
    new = _runner(live_env, store, kalshi)
    await new.rehydrate()
    assert new.desired == "stopped" and new.mode == "demo"
    assert (await store.get_state())["desired"] == "stopped"


@pytest.mark.asyncio
async def test_persisted_stop_survives_many_restarts_and_handovers(settings, bot_paths, kalshi):
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    await r.stop(by="owner")
    for _ in range(4):
        await r.shutdown()
        r = _runner(settings, store, kalshi)
        await r.rehydrate()
        assert r.role() == "leader" and r.desired == "stopped"
        assert (await r.tick("x"))["ran"] is False
    assert (await store.get_state())["desired"] == "stopped"


# ------------------------------------------------------------------ R2: SIGTERM mid-pass
@pytest.mark.asyncio
async def test_sigterm_mid_pass_finishes_order_step_then_releases(settings, bot_paths, kalshi, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    r.last_monitor_utc = datetime.now(timezone.utc)
    in_step, release = threading.Event(), threading.Event()
    done = []

    def slow_step(mode):
        in_step.set()
        release.wait(5)
        done.append(mode)
        return 0, ""
    monkeypatch.setattr(r, "_run_strategy_sync", slow_step)
    assert (await r.tick("t"))["ran"] is True
    await asyncio.get_running_loop().run_in_executor(None, in_step.wait, 5)
    sd = asyncio.create_task(r.shutdown())
    await asyncio.sleep(0.05)
    assert not sd.done()                                   # waits for the order step, never cuts it
    assert (await store.get_lease())["expires_at"] > datetime.now(timezone.utc)   # still held mid-step
    release.set()
    await asyncio.wait_for(sd, 10)
    assert done == ["demo"]
    lease = await store.get_lease()
    assert lease["expires_at"] <= datetime.now(timezone.utc)                     # released
    assert (await store.last_heartbeat()) is not None                             # pass persisted
    assert (await r.tick("t"))["ran"] is False


@pytest.mark.asyncio
async def test_overrunning_pass_keeps_the_lease(settings, bot_paths, kalshi, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = _runner(settings, store, kalshi)
    await r.rehydrate()
    r.last_monitor_utc = datetime.now(timezone.utc)
    in_step, release = threading.Event(), threading.Event()

    def stuck(mode):
        in_step.set()
        release.wait(5)
        return 0, ""
    monkeypatch.setattr(r, "_run_strategy_sync", stuck)
    await r.tick("t")
    await asyncio.get_running_loop().run_in_executor(None, in_step.wait, 5)
    r.pass_budget_s = 0.1
    try:
        await asyncio.wait_for(r.shutdown(), 5)
        lease = await store.get_lease()
        assert lease["expires_at"] > datetime.now(timezone.utc)        # NOT released: expires after the TTL
        assert any("overran" in t for t, _, _ in r.alerter.sent)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_shutdown_budget_inside_render_delay(settings):
    """Item 11: the default window is Render's 30 s (free plan); uvicorn's drain and a margin come out of it."""
    from control_api import runner as rn
    r = Runner(settings, MemoryStore())
    assert settings.shutdown_delay_s == rn.RENDER_SHUTDOWN_WINDOW_S == 30
    assert r.shutdown_budget_s() + rn.UVICORN_GRACEFUL_S + rn.SHUTDOWN_MARGIN_S <= 30
    assert r.shutdown_budget_s() == 22
    assert r.shutdown_order_wait_s() + rn.SHUTDOWN_STORE_TIMEOUT_S + rn.SHUTDOWN_RESERVE_S <= r.shutdown_budget_s()
    assert r.pass_budget_s == 25 and r.lease.ttl_s == 60 and r.lease.renew_s == 20


@pytest.mark.asyncio
async def test_shutdown_never_raises_with_a_broken_store(settings, bot_paths, kalshi):
    store = MemoryStore()
    r = _runner(settings, store, kalshi)
    await r.rehydrate()

    class Broken:
        def __getattr__(self, n):
            async def f(*a, **k):
                raise RuntimeError("down")
            return f
    r.store = r.lease.store = Broken()
    await asyncio.wait_for(r.shutdown(), 10)

"""Sprint 2B part 3, item 11 (option b): the graceful shutdown/handover fits Render's default 30 s window.

Every test runs on a virtual-time loop (tests/vclock.py): no real sleeps, slow or hung stores and a hung Kalshi
cost no wall time. Elapsed time is measured on the loop's virtual clock."""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

import pytest

from control_api import runner as rn
from control_api.db import MemoryStore
from control_api.runner import Runner
from control_api.tests.vclock import SlowStore, run_virtual

KEY = "k" * 40
WINDOW = rn.RENDER_SHUTDOWN_WINDOW_S
# what the runner may use after uvicorn's drain, before SIGKILL
RUNNER_LIMIT = WINDOW - rn.UVICORN_GRACEFUL_S


class RecordingAlerter:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", priority="default", **_):
        self.sent.append((title, body, priority))
        return True

    def notify(self, *a, **k):
        pass


@pytest.fixture
def demo_keys(monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")


def _runner(settings, store, kalshi):
    r = Runner(settings, store, kalshi_factory=lambda m: kalshi)
    r.alerter = RecordingAlerter()
    return r


def _append_journal(path, n=1):
    fields = ("utc", "event", "ticker", "side", "price", "count", "cost", "cell", "note")
    exists = path.exists() and path.stat().st_size > 0
    buf = io.StringIO()
    w = csv.writer(buf)
    if not exists:
        w.writerow(fields)
    for i in range(n):
        w.writerow([datetime.now(timezone.utc).isoformat(), "quote", f"KXDEMOA-{i}", "yes", "0.42", "1", "0.42",
                    "c", f"shutdown-flush-{i}"])
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


async def _leader(settings, kalshi, mem, desired="running"):
    await mem.set_state(by="owner", desired=desired)
    r = _runner(settings, mem, kalshi)
    await r.rehydrate()
    assert r.role() == "leader"
    return r


async def _timed_shutdown(r):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await r.shutdown()
    return loop.time() - t0


def _released(lease):
    return lease["expires_at"] <= datetime.now(timezone.utc)


# ------------------------------------------------------------------ the budget arithmetic
def test_shutdown_budget_fits_render_default_window(settings):
    r = Runner(settings, MemoryStore())
    assert settings.shutdown_delay_s == WINDOW == 30.0
    assert r.shutdown_budget_s() == 22.0
    assert r.shutdown_budget_s() + rn.UVICORN_GRACEFUL_S + rn.SHUTDOWN_MARGIN_S <= WINDOW
    assert r.shutdown_order_wait_s() == 12.0
    assert rn.SHUTDOWN_RESERVE_S == 4 * rn.SHUTDOWN_STORE_TIMEOUT_S


def test_shutdown_budget_scales_with_a_paid_plan_window(settings, monkeypatch):
    monkeypatch.setattr(settings, "shutdown_delay_s", 120.0)
    r = Runner(settings, MemoryStore())
    assert r.shutdown_budget_s() == 112.0
    assert r.shutdown_order_wait_s() == r.pass_budget_s == 25.0       # the full pass budget again


def test_shutdown_window_env_default_and_clamp():
    from control_api.settings import release_timing_from
    assert release_timing_from(lambda n: None)["shutdown_delay_s"] == 30.0
    assert release_timing_from(lambda n: "1")["shutdown_delay_s"] == 10.0
    assert release_timing_from(lambda n: "120")["shutdown_delay_s"] == 120.0


def test_render_start_command_drains_requests_within_the_runner_constant():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    text = (root / "render.yaml").read_text(encoding="utf-8")
    start = next(line for line in text.splitlines() if line.strip().startswith("startCommand:"))
    assert f"--timeout-graceful-shutdown {int(rn.UVICORN_GRACEFUL_S)}" in start


# ------------------------------------------------------------------ demo handover under a slow store
@pytest.mark.parametrize("delay", [0.0, 0.5, 1.9])
def test_shutdown_handover_flushes_journal_and_releases_lease_inside_window_with_slow_store(
        settings, bot_paths, kalshi, demo_keys, delay):
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        _append_journal(r.journal_path, 3)
        r.store = r.lease.store = SlowStore(mem, delay)
        took = await _timed_shutdown(r)
        assert took <= r.shutdown_budget_s() <= RUNNER_LIMIT - rn.SHUTDOWN_MARGIN_S
        rows = await mem.list_journal(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=100)
        assert sum(1 for x in rows if str(x.get("note", "")).startswith("shutdown-flush-")) == 3
        assert _released(await mem.get_lease())
    run_virtual(go)


def test_shutdown_waits_for_a_running_order_step_then_releases(settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        r.store = r.lease.store = SlowStore(mem, 1.0)
        loop = asyncio.get_running_loop()
        r._strategy_running = True                    # an order step that ends 9 s into the shutdown
        loop.call_later(9.0, setattr, r, "_strategy_running", False)
        _append_journal(r.journal_path)
        took = await _timed_shutdown(r)
        assert 8.9 <= took <= r.shutdown_budget_s()
        assert _released(await mem.get_lease())
        assert not any("overran" in t for t, _, _ in r.alerter.sent)
    run_virtual(go)


def test_shutdown_waits_for_an_executing_arbitrage_set_then_releases(settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        loop = asyncio.get_running_loop()
        r.arb_running_scan = True                     # an arbitrage set in flight: never cut by the shutdown
        loop.call_later(5.0, setattr, r, "arb_running_scan", False)
        took = await _timed_shutdown(r)
        assert 4.9 <= took <= r.shutdown_budget_s()
        assert _released(await mem.get_lease())
    run_virtual(go)


def test_shutdown_with_a_hung_order_step_keeps_the_lease_and_ends_inside_window(
        settings, bot_paths, kalshi, demo_keys):
    """Slow Kalshi: the order step never ends. The shutdown still ends in time and the lease is NOT released."""
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        r._strategy_running = True                    # never cleared (a hung Kalshi call in the worker thread)
        took = await _timed_shutdown(r)
        assert took <= r.shutdown_budget_s()
        assert r.shutdown_order_wait_s() <= took
        assert not _released(await mem.get_lease())   # expires after the TTL: no other instance trades beside it
        assert any("overran" in t for t, _, _ in r.alerter.sent)
    run_virtual(go)


def test_shutdown_with_a_hung_store_never_raises_and_ends_inside_window(settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        _append_journal(r.journal_path)
        r.store = r.lease.store = SlowStore(mem, float("inf"))
        took = await _timed_shutdown(r)
        assert took <= r.shutdown_budget_s()
        assert r._leader_ready is False
    run_virtual(go)


def test_shutdown_remote_stop_with_hung_kalshi_cancel_still_releases(settings, bot_paths, kalshi, demo_keys,
                                                                      monkeypatch):
    """A Stop written to the store by another instance during shutdown: the cancel against a hung Kalshi is cut at
    the reserve, the lease is still released in time (the successor's takeover cancel covers what is left)."""
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        await mem.set_state(by="other-instance", desired="stopped")
        hung = []

        def never(mode):
            fut = asyncio.get_running_loop().create_future()
            hung.append(mode)
            return fut
        monkeypatch.setattr(r, "_cancel_in_stop_pool", never)
        r.store = r.lease.store = SlowStore(mem, 0.5)
        took = await _timed_shutdown(r)
        assert hung == ["demo"] and r.desired == "stopped"
        assert took <= r.shutdown_budget_s()
        lease = await mem.get_lease()
        assert _released(lease) and lease.get("handover") is None
    run_virtual(go)


def test_shutdown_never_slows_or_blocks_stop(settings, bot_paths, kalshi, demo_keys):
    """Stop during a shutdown that waits on a hung order step returns at once and ends stopped."""
    async def go():
        mem = MemoryStore()
        r = await _leader(settings, kalshi, mem)
        r._strategy_running = True
        sd = asyncio.ensure_future(r.shutdown())
        await asyncio.sleep(1.0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        res = await r.stop(by="owner")
        assert loop.time() - t0 < 1.0 and res["desired"] == "stopped" and r.kill_switch()
        await sd
        assert (await mem.get_state())["desired"] == "stopped"
    run_virtual(go)


# ------------------------------------------------------------------ LIVE: signed handover under a slow store
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


@pytest.mark.parametrize("delay", [0.0, 1.5])
def test_live_signed_handover_completes_inside_window_with_slow_store(live_env, kalshi, delay):
    async def go():
        mem = MemoryStore()
        old = await _leader(live_env, kalshi, mem)
        await mem.set_state(by="owner", mode="live")
        old.mode = "live"
        old.store = old.lease.store = SlowStore(mem, delay)
        took = await _timed_shutdown(old)
        assert took <= old.shutdown_budget_s()
        lease = await mem.get_lease()
        assert _released(lease) and lease["handover"] and lease["handover"]["mode"] == "live"
        assert KEY not in str(lease["handover"])
        new = _runner(live_env, mem, kalshi)
        await new.rehydrate()
        assert new.mode == "live" and new.handover_result["kept_live"] is True
    run_virtual(go)


def test_live_handover_with_hung_store_drops_to_demo_never_keeps_live(live_env, kalshi):
    """A store that hangs at shutdown: no token can be written in time, so the successor drops LIVE to demo
    (S-21) once the lease expires. The shutdown still ends inside the window."""
    async def go():
        mem = MemoryStore()
        old = await _leader(live_env, kalshi, mem)
        await mem.set_state(by="owner", mode="live")
        old.mode = "live"
        old.store = old.lease.store = SlowStore(mem, float("inf"))
        took = await _timed_shutdown(old)
        assert took <= old.shutdown_budget_s()
        assert (await mem.get_lease()).get("handover") is None
    run_virtual(go)


# ------------------------------------------------------------------ real clock, real worker thread (slow set)
@pytest.mark.slow
@pytest.mark.asyncio
async def test_shutdown_real_clock_order_step_thread_and_slow_store(settings, bot_paths, kalshi, demo_keys,
                                                                    monkeypatch):
    """End to end on the real clock: a real order step in its worker thread (1.5 s) and a store that answers every
    call after 0.3 s. The step finishes, the journal is flushed and the lease released, all inside the budget."""
    import threading
    import time
    mem = MemoryStore()
    r = await _leader(settings, kalshi, mem)
    r.last_monitor_utc = datetime.now(timezone.utc)
    in_step = threading.Event()

    def step(mode):
        in_step.set()
        time.sleep(1.5)
        _append_journal(r.journal_path)
        return 0, ""
    monkeypatch.setattr(r, "_run_strategy_sync", step)
    assert (await r.tick("t"))["ran"] is True
    await asyncio.get_running_loop().run_in_executor(None, in_step.wait, 5)
    r.store = r.lease.store = SlowStore(mem, 0.3)
    t0 = time.monotonic()
    await r.shutdown()
    took = time.monotonic() - t0
    assert 1.0 <= took <= r.shutdown_budget_s()
    assert _released(await mem.get_lease())
    rows = await mem.list_journal(since=datetime(2000, 1, 1, tzinfo=timezone.utc), limit=100)
    assert any(str(x.get("note", "")).startswith("shutdown-flush-") for x in rows)

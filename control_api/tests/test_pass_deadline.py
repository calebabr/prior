"""Whole-pass time limit in the runner: the order-step thread gets a thread-local deadline (start + pass_budget_s)
through the strategy's optional set_pass_deadline/clear_pass_deadline, a timed-out pass is recorded as pass_timeout
(one failed pass, alerted only from the second in a row), and Stop and the arbitrage pool never see the deadline."""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from control_api import runner as runmod
from control_api.db import MemoryStore
from control_api.runner import Runner

pytestmark = pytest.mark.asyncio

EXIT = 3


class Alerter:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", priority="default", **kw):
        self.sent.append((title, body))
        return True


@pytest.fixture
def demo_keys(monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", "pk")


@pytest.fixture
def gate():
    g = threading.Event()
    yield g
    g.set()


class Hooks:
    """A strategy's deadline hooks backed by a thread-local, like kalshi_client's."""

    def __init__(self):
        self.local = threading.local()
        self.calls = []

    def set(self, ts):
        self.calls.append(("set", threading.current_thread().name, ts))
        self.local.ts = ts

    def clear(self):
        self.calls.append(("clear", threading.current_thread().name, None))
        self.local.ts = None

    def seen(self):
        return getattr(self.local, "ts", None)


@pytest.fixture
def hooks(monkeypatch, bot_paths):
    from control_api.services import botfiles
    ms = botfiles.strategy()
    h = Hooks()
    monkeypatch.setattr(ms, "set_pass_deadline", h.set, raising=False)
    monkeypatch.setattr(ms, "clear_pass_deadline", h.clear, raising=False)
    monkeypatch.setattr(ms, "PASS_DEADLINE_EXIT", EXIT, raising=False)
    return ms, h


async def _runner(settings, kalshi) -> Runner:
    store = MemoryStore()
    await store.set_state(by="owner", desired="running")
    r = Runner(settings, store, kalshi_factory=lambda m: kalshi)
    r.alerter = Alerter()
    r.last_monitor_utc = datetime.now(timezone.utc)
    await r.rehydrate()
    r.alerter.sent.clear()                                             # the restart notice is not under test
    return r


async def test_deadline_set_and_cleared_around_the_order_step(settings, kalshi, demo_keys, hooks, monkeypatch):
    ms, h = hooks
    seen = {}

    def main():
        seen["ts"], seen["thread"] = h.seen(), threading.current_thread().name
        return 0
    monkeypatch.setattr(ms, "main", main)
    r = await _runner(settings, kalshi)
    t0 = time.monotonic()
    await r._pass("t")
    assert r.last_error is None and r.pass_timeouts_in_row == 0
    assert [c[0] for c in h.calls] == ["set", "clear"]
    assert h.calls[0][1] == h.calls[1][1] == seen["thread"]           # the order-step thread, set and cleared there
    assert t0 < seen["ts"] <= time.monotonic() + r.pass_budget_s       # step start + pass_budget_s
    assert abs(seen["ts"] - (t0 + r.pass_budget_s)) < 5
    assert await asyncio.to_thread(h.seen) is None                     # a reused default-pool thread: no leak


async def test_deadline_cleared_when_main_raises(settings, kalshi, demo_keys, hooks, monkeypatch):
    ms, h = hooks

    def main():
        raise RuntimeError("boom")
    monkeypatch.setattr(ms, "main", main)
    r = await _runner(settings, kalshi)
    await r._pass("t")
    assert [c[0] for c in h.calls] == ["set", "clear"] and r.last_error.startswith("RuntimeError")


async def test_strategy_without_deadline_hooks_runs_as_before(settings, kalshi, demo_keys, bot_paths, monkeypatch):
    from control_api.services import botfiles
    ms = botfiles.strategy()
    for name in ("set_pass_deadline", "clear_pass_deadline", "PASS_DEADLINE_EXIT"):
        monkeypatch.delattr(ms, name, raising=False)
    ran = []
    monkeypatch.setattr(ms, "main", lambda: ran.append(1) or 0)
    r = await _runner(settings, kalshi)
    await r._pass("t")
    assert ran == [1] and r.last_error is None
    monkeypatch.setattr(ms, "main", lambda: EXIT)                      # an unknown nonzero code stays a plain failure
    r.last_pass_started_utc = None
    await r._pass("t2")
    assert r.last_error == f"RuntimeError: maker_strategy exited {EXIT}" and r.pass_timeouts_in_row == 0


async def test_only_set_hook_present_is_ignored(settings, kalshi, demo_keys, bot_paths, monkeypatch):
    from control_api.services import botfiles
    ms = botfiles.strategy()
    calls = []
    monkeypatch.setattr(ms, "set_pass_deadline", lambda ts: calls.append(ts), raising=False)
    monkeypatch.delattr(ms, "clear_pass_deadline", raising=False)
    monkeypatch.setattr(ms, "main", lambda: 0)
    r = await _runner(settings, kalshi)
    await r._pass("t")
    assert calls == [] and r.last_error is None                       # never set without a way to clear it


async def test_timeout_recorded_and_alert_only_from_the_second_in_a_row(settings, kalshi, demo_keys, hooks,
                                                                        monkeypatch):
    ms, h = hooks
    result = {"rc": EXIT}
    monkeypatch.setattr(ms, "main", lambda: print("pass_deadline: stopped") or result["rc"])
    r = await _runner(settings, kalshi)

    await r._pass("one")
    assert r.last_error.startswith("pass_timeout: ") and r.pass_timeouts_in_row == 1
    assert r.alerter.sent == []                                        # a lone timeout: no alert
    hb = await r.store.last_heartbeat()                                # one failed pass
    assert hb["ok"] is False and hb["error"].startswith("pass_timeout: ")

    await r._pass("two")
    assert r.pass_timeouts_in_row == 2
    assert [t for t, _ in r.alerter.sent] == ["Trading pass error"]
    assert r.alerter.sent[0][1].startswith("pass_timeout: ") and "2 passes in a row" in r.alerter.sent[0][1]

    result["rc"] = 0
    await r._pass("ok")
    assert r.pass_timeouts_in_row == 0 and r.last_error is None
    result["rc"] = EXIT
    await r._pass("three")
    assert r.pass_timeouts_in_row == 1                                 # the count restarted: no new pass alert
    assert [t for t, _ in r.alerter.sent].count("Trading pass error") == 1


async def test_other_failure_resets_the_timeout_count_and_still_alerts(settings, kalshi, demo_keys, hooks,
                                                                       monkeypatch):
    ms, h = hooks
    monkeypatch.setattr(ms, "main", lambda: EXIT)
    r = await _runner(settings, kalshi)
    await r._pass("one")

    def bad():
        raise ValueError("x")
    monkeypatch.setattr(ms, "main", bad)
    await r._pass("two")
    assert r.pass_timeouts_in_row == 0 and r.last_error.startswith("ValueError")
    assert [t for t, _ in r.alerter.sent] == ["Trading pass error"]   # a non-timeout failure alerts at once


async def test_deadline_error_escaping_main_is_a_pass_timeout(settings, kalshi, demo_keys, hooks, monkeypatch):
    ms, h = hooks

    class PassDeadlineExceeded(Exception):
        pass_deadline = True

    def main():
        raise PassDeadlineExceeded("GET /x: pass deadline reached")
    monkeypatch.setattr(ms, "main", main)
    r = await _runner(settings, kalshi)
    await r._pass("t")
    assert r.last_error.startswith("pass_timeout: ") and r.pass_timeouts_in_row == 1
    assert [c[0] for c in h.calls] == ["set", "clear"]


async def test_stop_and_arb_pool_unaffected_while_the_deadline_thread_is_blocked(settings, kalshi, demo_keys, hooks,
                                                                                 gate, monkeypatch):
    ms, h = hooks
    monkeypatch.setattr(runmod, "CANCEL_TIMEOUT_S", 2.0)
    inside = threading.Event()

    def main():
        inside.set()
        gate.wait(10)                                                  # a hung order step holding its deadline
        return 0
    monkeypatch.setattr(ms, "main", main)
    cancel_saw = []
    real_cancel = kalshi.cancel_order

    def cancel(order_id, market_ticker=None):
        cancel_saw.append((threading.current_thread().name, h.seen()))
        return real_cancel(order_id, market_ticker)
    monkeypatch.setattr(kalshi, "cancel_order", cancel)
    r = await _runner(settings, kalshi)
    task = asyncio.ensure_future(r._pass("t"))
    for _ in range(300):
        if inside.is_set():
            break
        await asyncio.sleep(0.01)
    assert inside.is_set() and h.calls[0][0] == "set"
    arb_saw = await asyncio.wrap_future(r._arb_executor.submit(h.seen))
    assert arb_saw is None                                             # the arbitrage pool has no deadline
    t0 = time.monotonic()
    res = await r.stop(by="owner")
    assert time.monotonic() - t0 < 1.0                                 # Stop did not wait on the blocked step
    assert res["errors"] == [] and res["cancelled"] == 2
    assert cancel_saw and all(ts is None for _, ts in cancel_saw)      # Stop's cancels ran with no deadline
    assert all(name != h.calls[0][1] for name, _ in cancel_saw)
    gate.set()
    await asyncio.wait_for(task, 10)
    assert h.calls[-1][0] == "clear"
    await r.shutdown()

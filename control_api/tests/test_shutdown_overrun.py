"""L-2 (SECURITY_REVIEW_2B): a step still running when the shutdown wait ends fails safe.

The shutdown waits at most shutdown_order_wait_s (12 s in Render's 30 s window) and never cuts the step. After that:
the lease is kept (no second leader), an URGENT alert goes out ("check positions" when an arbitrage set was being
sent), and a marker is saved so the next leader trips the arbitrage legging kill switch. Virtual clock: no real
sleeps."""
from __future__ import annotations

import pytest

from control_api.db import MemoryStore
from control_api.services import arbguard
from control_api.tests.test_shutdown_window import (RecordingAlerter, _leader, _released, _runner,  # noqa: F401
                                                    _timed_shutdown, demo_keys)
from control_api.tests.vclock import SlowStore, run_virtual

EV = "KXDEMOA-26NOV05"


async def _successor(settings, kalshi, mem, old):
    await mem.release_lease(old.instance_id, old.lease.generation)   # the TTL lapsed after SIGKILL
    new = _runner(settings, mem, kalshi)
    await new.rehydrate()
    assert new.role() == "leader"
    return new


def test_overrun_mid_arbitrage_set_alerts_urgent_and_the_successor_trips_the_legging_kill(
        settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        old = await _leader(settings, kalshi, mem)
        await mem.set_state(by="owner", arb_desired="running")
        old.arb_running_scan = True                       # a hung set in its worker thread, never cleared
        old._arb_set_inflight = {"kind": "demo-execute", "event": EV}
        took = await _timed_shutdown(old)
        assert took <= old.shutdown_budget_s()
        assert not _released(await mem.get_lease())       # kept: no other instance trades beside the set
        al = [a for a in old.alerter.sent if "overran" in a[0]]
        assert al and al[0][2] == "urgent" and "Check positions" in al[0][1] and EV in al[0][1]
        marker = (await mem.get_cloud_settings())[arbguard.CLOUD_OVERRUN]
        assert marker["arb_set"]["event"] == EV and marker["instance"] == old.instance_id
        old.arb_kill_path.unlink(missing_ok=True)         # the old disk is gone
        new = await _successor(settings, kalshi, mem, old)
        assert new.arb_kill_switch() and new.arb_status()["kill_reason"] == "legging"
        assert new.arb_desired == "stopped"
        cloud = await mem.get_cloud_settings()
        assert cloud[arbguard.CLOUD_OVERRUN] is None and cloud[arbguard.CLOUD_KILL]["event"] == EV
        assert any(a[2] == "urgent" and "kill switch" in a[0] for a in new.alerter.sent)
        with pytest.raises(PermissionError, match="arb_legging_kill"):
            await new.set_arb(enabled=True, live=None, by="owner")
    run_virtual(go)


def test_overrun_of_a_maker_step_alerts_urgent_and_never_trips_arbitrage(settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        old = await _leader(settings, kalshi, mem)
        old._strategy_running = True
        await _timed_shutdown(old)
        al = [a for a in old.alerter.sent if "overran" in a[0]]
        assert al and al[0][2] == "urgent" and "maker order step" in al[0][1] and "Check positions" not in al[0][1]
        assert (await mem.get_cloud_settings())[arbguard.CLOUD_OVERRUN]["arb_set"] is None
        new = await _successor(settings, kalshi, mem, old)
        assert not new.arb_kill_switch()
    run_virtual(go)


def test_overrun_with_a_hung_store_still_ends_inside_the_window(settings, bot_paths, kalshi, demo_keys):
    async def go():
        mem = MemoryStore()
        old = await _leader(settings, kalshi, mem)
        old.arb_running_scan = True
        old._arb_set_inflight = {"kind": "live", "event": EV}
        old.store = old.lease.store = SlowStore(mem, float("inf"))
        took = await _timed_shutdown(old)
        assert took <= old.shutdown_budget_s()
        assert any(a[2] == "urgent" for a in old.alerter.sent)    # the alert does not need the store
    run_virtual(go)


def test_scan_without_a_set_in_flight_does_not_trip_the_successor(settings, bot_paths, kalshi, demo_keys):
    """The scan was still reading books (no set being sent): nothing can be part-filled, so no kill switch."""
    async def go():
        mem = MemoryStore()
        old = await _leader(settings, kalshi, mem)
        old.arb_running_scan = True
        await _timed_shutdown(old)
        new = await _successor(settings, kalshi, mem, old)
        assert not new.arb_kill_switch()
    run_virtual(go)


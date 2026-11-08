"""Sprint 3 pre-work: the arbitrage series pause and the legging kill switch survive a restart and a leader change
(services/arbguard), and a non-finite fill count never reads as a full fill.

Demo-execute only, fake scanner results, no network. The pause rule itself is unchanged: a contained partial pauses
its series until the next 00:00 America/New_York, computed from the record's own time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.runner import Runner
from control_api.services import arbcontain, arbguard, arbsets
from control_api.services import pnl as pnlsvc
from control_api.tests.test_arb_contain import EV_A, EV_B, _legged, _on, contained, xenv  # noqa: F401



async def _no_log(**kw):
    return []                                  # the arbitrage log row is gone (write failed or pushed out)


async def _successor(env):
    """A new process on the same store, after the old one released the lease (a deploy or a restart)."""
    await env.runner.lease.release(handover=None)
    new = Runner(env.settings, env.store, kalshi_factory=env.runner.kalshi_factory)
    new.public_factory = env.runner.public_factory
    new.alerter = env.runner.alerter
    await new.rehydrate()
    assert new.role() == "leader"
    new.config = env.runner.config
    return new


# ---------------------------------------------------------------- series pause
@pytest.mark.asyncio
async def test_pause_is_persisted_and_survives_a_restart_without_the_log(xenv, monkeypatch):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()
    stored = (await xenv.store.get_cloud_settings())[arbguard.CLOUD_CONTAIN]
    assert [r["event"] for r in stored["demo-execute"]] == [EV_A]
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", _no_log)
    xenv.runner._arb_contain.clear()                      # a restart: memory gone, and now the log too
    xenv.clock.t += timedelta(hours=1)
    xenv.results[EV_A] = "FILLED {}"
    xenv.calls.clear()
    await xenv.runner.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_B]      # still paused from the persisted record
    assert [p["series"] for p in xenv.runner.arb_status()["paused_series"]] == ["KXDEMOA"]


@pytest.mark.asyncio
async def test_persisted_pause_expires_at_the_same_new_york_midnight(xenv, monkeypatch):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()                          # 2026-11-05 15:00 UTC: paused until 11-06 05:00 UTC
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", _no_log)
    xenv.results[EV_A] = "FILLED {}"
    xenv.runner._arb_contain.clear()
    xenv.clock.t = datetime(2026, 11, 6, 4, 59, tzinfo=timezone.utc)
    xenv.calls.clear()
    await xenv.runner.arb_scan()
    assert EV_A not in [c["event"] for c in xenv.calls]   # one minute before: still paused (never shortened)
    xenv.runner._arb_contain.clear()
    xenv.clock.t = datetime(2026, 11, 6, 5, 0, 30, tzinfo=timezone.utc)
    xenv.calls.clear()
    await xenv.runner.arb_scan()
    assert EV_A in [c["event"] for c in xenv.calls]       # the boundary: trades again (never extended)


@pytest.mark.asyncio
async def test_pause_survives_a_leader_change(xenv, monkeypatch):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", _no_log)
    new = await _successor(xenv)
    new.arb_desired = "running"                           # the owner turned arbitrage back on (demo-execute)
    xenv.results[EV_A] = "FILLED {}"
    xenv.calls.clear()
    await new.arb_scan()
    assert [c["event"] for c in xenv.calls] == [EV_B]
    assert [p["series"] for p in new.arb_status()["paused_series"]] == ["KXDEMOA"]


@pytest.mark.asyncio
async def test_two_contained_across_a_restart_trip_from_the_persisted_record(xenv, monkeypatch):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.10)
    await xenv.runner.arb_scan()
    monkeypatch.setattr(xenv.store, "list_arb_opportunities", _no_log)
    xenv.runner._arb_contain.clear()
    xenv.clock.t += timedelta(hours=20)
    xenv.results = {EV_A: "FILLED {}", EV_B: contained(0.10, event=EV_B)}
    await xenv.runner.arb_scan()
    assert _legged(xenv)                                  # the 24 h count still sees the first one


@pytest.mark.asyncio
async def test_guard_unreadable_refuses_sets(xenv, monkeypatch):
    await _on(xenv)
    xenv.runner._arb_contain.clear()

    async def boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(xenv.store, "get_cloud_settings", boom)
    assert await xenv.runner._arb_contain_state("demo-execute") is None   # fail closed: every set refused


@pytest.mark.asyncio
async def test_guard_fields_are_not_in_the_cloud_api(xenv):
    await _on(xenv, arb_set_loss_frac=0.01)
    xenv.results[EV_A] = contained(0.20)
    await xenv.runner.arb_scan()
    r = await xenv.client.get("/api/cloud", headers=xenv.auth)
    assert r.status_code == 200
    assert arbguard.CLOUD_CONTAIN not in r.json()["settings"]
    r = await xenv.client.put("/api/cloud", json={arbguard.CLOUD_KILL: {"reason": "x"}}, headers=await xenv.h2fa())
    assert r.status_code in (400, 422)                    # never writable through the settings API


@pytest.mark.asyncio
async def test_set_in_flight_marker_is_set_only_while_execute_runs(xenv):
    await _on(xenv)
    seen = []
    xenv.results[EV_A] = lambda op: (seen.append(dict(xenv.runner._arb_set_inflight)), "FILLED {}")[1]
    await xenv.runner.arb_scan()
    assert seen == [{"kind": "demo-execute", "event": EV_A}]
    assert xenv.runner._arb_set_inflight is None


# ---------------------------------------------------------------- legging kill switch
@pytest.mark.asyncio
async def test_legging_kill_survives_a_restart_and_needs_the_acknowledgement(xenv):
    await _on(xenv)
    xenv.results[EV_A] = "PARTIAL {'X': 2.0}"            # uncontained: the scanner's kill file
    xenv.runner._arb_write_kill("legging risk: partial set", EV_A)
    xenv.results[EV_A] = "FILLED {}"
    await xenv.runner.arb_scan()
    assert _legged(xenv)
    assert "legging" in (await xenv.store.get_cloud_settings())[arbguard.CLOUD_KILL]["reason"]
    xenv.runner.arb_kill_path.unlink()                    # Render's disk is ephemeral
    new = await _successor(xenv)
    assert new.arb_kill_switch() and new.arb_status()["kill_reason"] == "legging"
    assert new.arb_desired == "stopped"
    with pytest.raises(PermissionError, match="arb_legging_kill"):
        await new.set_arb(enabled=True, live=None, by="owner")
    await new.set_arb(enabled=True, live=None, by="owner", ack_legging=True)
    assert not new.arb_kill_switch()
    assert (await xenv.store.get_cloud_settings())[arbguard.CLOUD_KILL] is None
    again = await _successor(type("E", (), {"runner": new, "settings": xenv.settings, "store": xenv.store})())
    assert not again.arb_kill_switch()                    # the acknowledgement is persisted too


@pytest.mark.asyncio
async def test_stop_kill_is_not_persisted_as_legging(xenv):
    await _on(xenv)
    await xenv.runner.stop("owner")
    assert ((await xenv.store.get_cloud_settings()) or {}).get(arbguard.CLOUD_KILL) is None


# ---------------------------------------------------------------- pure helpers
def test_records_round_trip_and_drop_bad_rows():
    now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
    recs = [arbcontain.record(EV_A, 0.5, now - timedelta(hours=1), "a", "flattened"),
            arbcontain.record(EV_B, 0.1, now - timedelta(hours=50), "old")]
    enc = arbguard.encode_records(recs, now)
    assert [r["id"] for r in enc] == ["a"]                # older than 48 h: pruned
    raw = {"demo-execute": enc + [{"id": "x"}, "junk", {"id": "y", "ts": "nope", "event": EV_A}]}
    dec = arbguard.decode_records(raw, "demo-execute", now)
    assert [(r["id"], r["series"], r["loss"]) for r in dec] == [("a", "KXDEMOA", 0.5)]
    assert arbcontain.paused(dec, now)["KXDEMOA"]["until_date"] == "2026-11-06"
    assert arbguard.decode_records(raw, "live", now) == [] and arbguard.decode_records(None, "live", now) == []
    neg = {"live": [{"id": "n", "ts": now.isoformat(), "event": EV_A, "loss": float("-inf")}]}
    assert arbguard.decode_records(neg, "live", now)[0]["loss"] == 0.0


def test_overrun_kill_only_for_another_instance_mid_set():
    now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
    mid = arbguard.overrun_doc("old", now, maker_step=False, arb_set={"kind": "live", "event": EV_A})
    assert arbguard.overrun_kill(mid, "new")["event"] == EV_A
    assert arbguard.overrun_kill(mid, "old") is None
    maker = arbguard.overrun_doc("old", now, maker_step=True, arb_set=None)
    assert arbguard.overrun_kill(maker, "new") is None
    assert arbguard.overrun_kill("junk", "new") is None
    assert "legging" in arbguard.normalize_kill({"reason": "x"})["reason"]
    assert arbguard.kill_from_file("stopped by owner") is None


# ---------------------------------------------------------------- fill counts fail closed
@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "Infinity", float("inf"), float("nan"), "1e300", -1, True,
                                 "abc"])
def test_unusable_fill_count_is_unknown_never_a_full_fill(bad):
    f = {"fill_id": "f1", "count_fp": bad, "count": 5}
    assert pnlsvc.fill_count_checked(f) is None
    assert pnlsvc.fill_count(f) == 0.0


def test_usable_fill_counts_unchanged():
    assert pnlsvc.fill_count({"count_fp": "2.00"}) == 2.0
    assert pnlsvc.fill_count({"count": 3}) == 3.0
    assert pnlsvc.fill_count({}) == 0.0
    assert pnlsvc.fill_count({"count_fp": None, "count": "4"}) == 4.0
    assert pnlsvc._f("inf") is None and pnlsvc._f("0.5") == 0.5


def test_executed_string_with_inf_is_not_a_fill():
    assert arbsets.parse_filled("PARTIAL {'A': inf}") == {}          # not a literal: unreadable
    huge = "1" + "0" * 400
    assert arbsets.parse_filled(f"PARTIAL {{'A': 1e999, 'B': 2.0, 'C': -1.0, 'D': {huge}}}") == {"B": 2.0}


def test_scanner_parsed_fills_drop_non_finite_counts():
    class Sc:
        @staticmethod
        def parse_result(s):
            return {"kind": "partial", "fills": {"A": float("inf"), "B": 1.0, "C": float("nan"), "D": "x"}}
    assert arbcontain.parse("PARTIAL {'B': 1.0}", Sc)["fills"] == {"B": 1.0}


def test_queue_episode_with_inf_fill_is_not_closed_as_filled():
    from control_api.services import queuepos
    now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
    ep = {"order_id": "o1", "count": 2, "ts": now - timedelta(minutes=5), "ticker": "KXDEMOA-X", "side": "yes",
          "price": 0.5}
    data = {"fills": [{"order_id": "o1", "fill_id": "f1", "count_fp": "inf", "ts": now}], "resting": []}
    queuepos.apply_account([ep], data, now=now, new_ids=set(), stop_ids=set(), last_stop_utc=None)
    assert ep.get("filled_count", 0) == 0.0
    assert ep.get("outcome") != "filled"


@pytest.mark.asyncio
async def test_a_persisted_kill_alone_still_needs_the_acknowledgement(xenv):
    await xenv.store.save_cloud_settings({arbguard.CLOUD_KILL: {"reason": "legging risk: x", "event": EV_A}},
                                         by="test")
    await xenv.runner.load_cloud()
    assert not xenv.runner.arb_kill_switch()              # e.g. the file could not be written back
    with pytest.raises(PermissionError, match="arb_legging_kill"):
        await xenv.runner.set_arb(enabled=True, live=None, by="owner")
    await xenv.runner.set_arb(enabled=True, live=None, by="owner", ack_legging=True)
    assert (await xenv.store.get_cloud_settings())[arbguard.CLOUD_KILL] is None

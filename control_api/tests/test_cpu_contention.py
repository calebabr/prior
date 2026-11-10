"""Stop is never starved by Analytics (load suite scenario `contention`): MemoryStore projects before copying, heavy
builds step aside while a Stop is in flight (control_api/cpuyield.py), and big in-memory reads yield to the loop.

No wall-clock assertions: threads block on Events, and the tests check ORDER (what ran while something else was
still parked), never how long anything took. GUARD_S is only a deadlock guard.
"""
from __future__ import annotations

import asyncio
import copy
import threading
from datetime import datetime, timedelta, timezone

import pytest

from control_api import cpuyield
from control_api.db import MemoryStore
from control_api.db import store as store_mod
from control_api.routes import analytics as ra
from control_api.services import analytics as an
from control_api.tests.test_analytics import NOW, _Clock, _put, _z, env, fill  # noqa: F401
from control_api.tests.test_analytics import _until

pytestmark = pytest.mark.asyncio
GUARD_S = 10.0


@pytest.fixture(autouse=True)
def _no_leaked_urgent():
    yield
    while cpuyield.in_flight():                  # a failed test must not leave builds pausing for the next one
        cpuyield.urgent().__exit__(None, None, None)


# ---------------------------------------------------------------- MemoryStore: project before copy
def _old_clean(doc: dict) -> dict:
    d = copy.deepcopy(doc)
    d.pop("\x00seq", None)
    d.pop("_id", None)
    return d


def _old_project(docs, fields):
    if fields is None:
        return docs
    keep = {str(f) for f in fields}
    return [{k: v for k, v in d.items() if k in keep} for d in docs]


async def _seeded(n: int = 40) -> MemoryStore:
    s = MemoryStore()
    t0 = NOW - timedelta(days=3)
    await s.upsert_fills([fill(f"f{i}", ts=t0 + timedelta(minutes=i), ticker=f"T-{i % 7}",
                               fill_quality={"trade_through": i % 2 == 0, "tags": [i, {"k": [i]}]},
                               user_note=None if i % 3 else f"n{i}") for i in range(n)])
    await s.upsert_settlements([{"ticker": f"T-{i}", "market_result": "no", "settled_time": _z(t0 + timedelta(hours=i)),
                                 "close_time": _z(t0 + timedelta(hours=i - 1)), "extra": {"a": [1, 2]}}
                                for i in range(7)])
    await s.add_journal_rows([{"utc": _z(t0 + timedelta(minutes=i)), "event": "quote" if i % 2 else "cancel",
                               "ticker": f"T-{i}", "note": f"order_id=o{i}", "cell": "[]"} for i in range(20)])
    await s.upsert_quote_episodes([{"order_id": f"o{i}", "ts": t0 + timedelta(minutes=i), "env": "demo",
                                    "status": "filled" if i % 2 else "open", "fill_ids": [f"f{i}"],
                                    "ticker": f"T-{i}"} for i in range(10)])
    for i in range(5):
        await s.add_heartbeat({"ts": t0 + timedelta(minutes=i), "ok": True, "detail": {"n": [i]}})
        await s.add_account_snapshot({"ts": t0 + timedelta(minutes=i), "balance": 100 + i, "env": "demo"})
    return s


def _ref(s: MemoryStore, coll, since=None, until=None, fields=None, limit=10_000, pred=None, key=None):
    """The pre-change semantics: deep-copy every matching doc, THEN project."""
    lo = since and store_mod.S.norm_dt(since)
    hi = until and store_mod.S.norm_dt(until)
    docs = [d for d in coll if (lo is None or d["ts"] >= lo) and (hi is None or d["ts"] < hi)
            and (pred is None or pred(d))]
    out = [_old_clean(d) for d in s._newest_seq(docs)[:limit]]
    return _old_project(out, fields) if key is None else key(out)


@pytest.mark.parametrize("fields", [None, ("fill_id", "ts", "fill_quality"), list(an.FILL_FIELDS), ("_id", "nope")])
async def test_memorystore_projected_fills_equal_deepcopy_then_project(fields):
    s = await _seeded()
    since, until = NOW - timedelta(days=3, minutes=-5), NOW - timedelta(days=3, minutes=-30)
    for kw in ({}, {"since": since}, {"since": since, "until": until}, {"fill_ids": ["f3", "f9", "zz"]}):
        got = await s.list_fills(limit=500, fields=fields, **kw)
        ids = kw.get("fill_ids")
        want = _ref(s, s._fills.values(), kw.get("since"), kw.get("until"), fields, 500,
                    None if ids is None else (lambda d: d["fill_id"] in set(ids)))
        assert got == want and [list(d) for d in got] == [list(d) for d in want]       # same keys, same order


async def test_memorystore_other_projected_reads_equal_the_old_semantics():
    s = await _seeded()
    for fields in (None, ("ticker", "market_result", "ts")):
        assert await s.list_settlements(limit=500, fields=fields) == _ref(s, s._settlements.values(), fields=fields)
    jf = ("event", "ts", "note")
    assert await s.list_journal(limit=500, events=["quote"], fields=jf) == _ref(
        s, s._journal.values(), fields=jf, pred=lambda d: d.get("event") in {"quote"})
    assert await s.list_journal(limit=3) == _ref(s, s._journal.values(), limit=3)
    ef = ("order_id", "fill_ids", "ts")
    assert await s.list_quote_episodes(env="demo", status="filled", fields=ef) == _ref(
        s, s._episodes.values(), fields=ef, pred=lambda d: d.get("env") == "demo" and d.get("status") == "filled")
    assert await s.list_heartbeats() == _ref(s, s._heartbeats)
    assert await s.list_account_snapshots(env="demo") == _ref(
        s, s._snapshots, pred=lambda d: store_mod.S.snapshot_env(d) == "demo",
        key=lambda out: [store_mod.S.with_snapshot_env(d) for d in out])


async def test_memorystore_results_are_private_copies():
    s = await _seeded()
    a = await s.list_fills(limit=500, fields=("fill_id", "fill_quality"))
    a[0]["fill_quality"]["tags"][1]["k"].append("mutated")
    a[0]["fill_quality"]["new"] = 1
    b = await s.list_fills(limit=500, fields=("fill_id", "fill_quality"))
    assert "mutated" not in b[0]["fill_quality"]["tags"][1]["k"] and "new" not in b[0]["fill_quality"]


async def test_memorystore_big_read_is_identical_and_yields_to_the_loop(monkeypatch):
    monkeypatch.setattr(store_mod, "COPY_CHUNK", 10)
    s = await _seeded(45)
    want = _ref(s, s._fills.values(), fields=an.FILL_FIELDS)
    read = asyncio.create_task(s.list_fills(limit=500, fields=an.FILL_FIELDS))
    turns = 0
    while not read.done():                        # another coroutine (a Stop) gets turns DURING the read
        turns += 1
        await asyncio.sleep(0)
    assert await read == want and turns >= 4      # 45 docs in chunks of 10: at least 4 checkpoints


# ---------------------------------------------------------------- cpuyield
def test_checkpoint_never_blocks_a_thread_that_is_not_a_cooperative_build():
    with cpuyield.urgent():
        assert cpuyield.in_flight() == 1
        cpuyield.checkpoint()                     # the loop / a plain thread: returns at once, never parks
    assert cpuyield.in_flight() == 0


def test_urgent_marker_never_goes_negative_and_never_raises():
    u = cpuyield.urgent()
    u.__exit__(None, None, None)
    assert cpuyield.in_flight() == 0


def _parking_build(log: list, entered: threading.Event, rows: int = 3):
    def build():
        for i in range(rows):
            cpuyield.checkpoint()
            log.append(i)
            if i == 0:
                entered.set()
        return "done"
    return build


async def test_a_cooperative_build_parks_while_a_stop_is_in_flight_and_resumes_after():
    log, entered = [], threading.Event()
    gate = threading.Event()

    def build():
        cpuyield.checkpoint()
        log.append("before")
        entered.set()
        gate.wait(GUARD_S)                        # the test marks a Stop urgent while the build sits here
        cpuyield.checkpoint()                     # -> parks until the Stop is done
        log.append("after")
        return 7

    u = cpuyield.urgent()
    job = asyncio.create_task(ra._cpu(build))
    await asyncio.to_thread(entered.wait, GUARD_S)
    u.__enter__()
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0.01)                 # give the worker every chance to run past the checkpoint
    assert log == ["before"] and not job.done()  # parked: it did not compute while the Stop was in flight
    u.__exit__(None, None, None)
    assert await asyncio.wait_for(job, GUARD_S) == 7 and log == ["before", "after"]


async def test_the_pause_is_bounded_per_build(monkeypatch):
    monkeypatch.setattr(cpuyield, "MAX_PAUSE_PER_BUILD_S", 0.0)
    log, entered = [], threading.Event()
    with cpuyield.urgent():                       # a marker that never clears cannot stall a build forever
        assert await asyncio.wait_for(ra._cpu(_parking_build(log, entered)), GUARD_S) == "done"
    assert log == [0, 1, 2]


async def test_results_are_identical_with_and_without_a_stop_in_flight(env):
    t = NOW - timedelta(days=2)
    await _put(env.store, [fill(f"x{i}", ts=t + timedelta(minutes=i), ticker=f"T-{i % 3}") for i in range(30)])
    rows = await env.store.list_fills(limit=500, fields=an.FILL_FIELDS)
    a = await ra._cpu(an.trade_rows, rows, [], [], cells=[], series_list=[], env="demo", jcells={})
    with cpuyield.urgent():
        cpuyield.MAX_PAUSE_PER_BUILD_S, old = 0.0, cpuyield.MAX_PAUSE_PER_BUILD_S
        try:
            b = await ra._cpu(an.trade_rows, rows, [], [], cells=[], series_list=[], env="demo", jcells={})
        finally:
            cpuyield.MAX_PAUSE_PER_BUILD_S = old
    assert a == b


async def test_the_stop_route_is_marked_urgent_and_the_marker_is_cleared(env, monkeypatch):
    seen = []
    real = env.runner.stop

    async def spy(**kw):
        seen.append(cpuyield.in_flight())
        return await real(**kw)
    monkeypatch.setattr(env.runner, "stop", spy)
    r = await env.client.post("/api/bot/stop", headers=env.auth)
    assert r.status_code == 200 and seen == [1] and cpuyield.in_flight() == 0
    await env.client.get("/api/status", headers=env.auth)
    assert cpuyield.in_flight() == 0              # other routes are never marked


async def test_stop_answers_while_a_real_summary_build_is_parked(env, monkeypatch):
    """A long (parked) build does not block a concurrent Stop: the build waits at a checkpoint, Stop returns 200."""
    entered, gate = threading.Event(), threading.Event()
    real = an.trade_rows

    def slow_rows(*a, **k):
        entered.set()
        gate.wait(GUARD_S)
        cpuyield.checkpoint()
        return real(*a, **k)
    monkeypatch.setattr(an, "trade_rows", slow_rows)
    job = asyncio.create_task(env.client.get("/api/analytics/summary?days=180", headers=env.auth))
    await asyncio.to_thread(entered.wait, GUARD_S)
    stop = asyncio.create_task(env.client.post("/api/bot/stop", headers=env.auth))
    await _until(lambda: cpuyield.in_flight() > 0 or stop.done())
    gate.set()
    r = await asyncio.wait_for(stop, GUARD_S)
    assert r.status_code == 200 and env.runner.desired == "stopped"
    assert (await asyncio.wait_for(job, GUARD_S)).status_code == 200


# ---------------------------------------------------------------- stale-while-refresh
async def test_stale_summary_is_served_while_its_refresh_is_still_blocked(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    await _put(env.store, [fill("a", ts=NOW - timedelta(days=1), ticker="T-A")])
    first = (await env.client.get("/api/analytics/summary", headers=env.auth)).json()
    release = asyncio.Event()
    started = asyncio.Event()
    real = env.store.list_fills

    async def hung(**kw):
        started.set()
        await release.wait()
        return await real(**kw)
    monkeypatch.setattr(env.store, "list_fills", hung)
    clock.t += 61
    r = await asyncio.wait_for(env.client.get("/api/analytics/summary", headers=env.auth), GUARD_S)
    assert r.status_code == 200 and r.json() == first                      # the last good copy, not a wait
    await asyncio.wait_for(started.wait(), GUARD_S)                        # its refresh is running (and hung)
    r2 = await asyncio.wait_for(env.client.get("/api/analytics/summary", headers=env.auth), GUARD_S)
    assert r2.json() == first                                              # still served while refreshing
    release.set()
    await _until(lambda: not ra._state(type("R", (), {"app": env.app})())["gate"].locked())


async def test_stale_returns_body_is_served_and_refreshed_once(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    url = "/api/analytics/returns?days=30"
    n = {"calls": 0}
    real = ra.retsvc.build

    def counting(*a, **k):
        n["calls"] += 1
        return real(*a, **k)
    monkeypatch.setattr(ra.retsvc, "build", counting)
    first = (await env.client.get(url, headers=env.auth)).json()
    assert n["calls"] == 1
    clock.t += 61
    rs = [(await env.client.get(url, headers=env.auth)).json() for _ in range(3)]
    assert all(x == first for x in rs)                                     # stale copy, at once, every time
    await _until(lambda: n["calls"] == 2)
    st = ra._state(type("R", (), {"app": env.app})())
    await _until(lambda: st["body_refresh"] is None or st["body_refresh"].done())
    assert n["calls"] == 2                                                 # ONE refresh for three stale requests
    clock.t += 1
    await env.client.get(url, headers=env.auth)
    assert n["calls"] == 2                                                 # the refreshed copy is fresh


async def test_returns_past_the_stale_bound_is_computed_before_answering(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ra, "_clock", clock)
    url = "/api/analytics/returns?days=30"
    a = (await env.client.get(url, headers=env.auth)).json()
    clock.t += ra.CACHE_TTL_S + 1
    monkeypatch.setattr(ra, "_now", lambda: datetime.now(timezone.utc) + timedelta(seconds=1))
    b = (await env.client.get(url, headers=env.auth)).json()
    assert b["generated_utc"] != a["generated_utc"]

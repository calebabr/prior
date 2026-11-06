"""Sprint 2B part 1 data items: snapshot env tag (12), durable journal sync (13), ops monitor (19)."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from control_api.db import MemoryStore
from control_api.db import schemas as S
from control_api.services import botfiles, journalsync, ledger
from control_api.services import ops_monitor as om

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- item 12
async def test_snapshot_env_tag_and_backfill():
    st = MemoryStore()
    await st.add_account_snapshot({"ts": NOW - timedelta(hours=3), "cash": 1.0, "exposure": 2.0, "headroom": 0.5})  # old shape
    await st.add_account_snapshot({"ts": NOW - timedelta(hours=2), "cash": 1.0, "exposure": 2.0, "env": "live"})
    await st.add_account_snapshot({"ts": NOW - timedelta(hours=1), "cash": 1.0, "exposure": 2.0, "env": "demo"})
    allrows = await st.list_account_snapshots()
    assert [r["env"] for r in allrows] == ["demo", "live", "demo"]          # old row reads as demo; newest first
    assert len(await st.list_account_snapshots(env="demo")) == 2
    assert [r["env"] for r in await st.list_account_snapshots(env="live")] == ["live"]
    assert "env" not in st._snapshots[0]                                      # stored row untouched (read-path rule)


def test_snapshot_env_helpers_and_query():
    assert S.snapshot_env({}) == "demo" and S.snapshot_env({"env": "weird"}) == "demo"
    assert S.snapshot_env({"env": "live"}) == "live"
    assert S.snapshot_env_query(None) == {} and S.snapshot_env_query("live") == {"env": "live"}
    assert {"env": {"$exists": False}} in S.snapshot_env_query("demo")["$or"]


def _snaps():
    return [{"ts": NOW - timedelta(days=2), "cash": 100.0, "exposure": 0.0},
            {"ts": NOW - timedelta(days=1), "cash": 90.0, "exposure": 20.0, "env": "demo"},
            {"ts": NOW - timedelta(hours=1), "cash": 5000.0, "exposure": 0.0, "env": "live"}]


def test_ledger_env_filter_default_unchanged():
    assert len(S.filter_snapshots(_snaps(), None)) == 3 and len(S.filter_snapshots(_snaps(), "demo")) == 2
    kw = {"since": NOW - timedelta(days=3), "now": NOW}
    base = ledger.reconciliation([], _snaps(), [], **kw)
    assert base == ledger.reconciliation([], _snaps(), [], snapshot_env=None, **kw)
    demo = ledger.reconciliation([], _snaps(), [], snapshot_env="demo", **kw)
    assert demo["equity_end"] == 110.0 and base["equity_end"] == 5000.0


# ---------------------------------------------------------------- item 13
FIELDS = S.JOURNAL_FIELDS


def _write(path, rows, header=True):
    with open(path, "w" if header else "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if header:
            w.writerow(FIELDS)
        for r in rows:
            w.writerow([r.get(k, "") for k in FIELDS])


def _row(i):
    return {"utc": (NOW + timedelta(seconds=i)).isoformat(), "event": "quote", "ticker": f"KXT-{i}", "side": "yes",
            "price": "0.5", "count": "1", "cost": "0.5", "cell": "c", "note": str(i)}


async def test_store_failure_keeps_rows_and_retry_has_no_duplicates(tmp_path):
    p, st = tmp_path / "journal.csv", MemoryStore()
    _write(p, [_row(i) for i in range(5)])
    dt = journalsync.DurableTail(p)
    fail = {"on": True}

    async def push(rows):
        if fail["on"]:
            raise RuntimeError("atlas down")
        return await st.add_journal_rows(rows)

    with pytest.raises(RuntimeError):
        await dt.sync(push)
    assert await st.list_journal(limit=100) == []                    # nothing stored yet ...
    fail["on"] = False
    rows, n = await dt.sync(push)                                    # ... and the SAME rows are read again
    assert len(rows) == 5 and n == 5
    rows, n = await dt.sync(push)
    assert rows == [] and n == 0                                     # committed: not re-sent
    assert len(await st.list_journal(limit=100)) == 5


async def test_kill_between_write_and_sync_then_restart_replay(tmp_path):
    """Rows written locally, process dies before sync; at restart the local file is replayed (idempotently)
    before it is rehydrated from the store: no loss, no duplicates, also when replay runs twice."""
    p, st = tmp_path / "journal.csv", MemoryStore()
    _write(p, [_row(i) for i in range(3)])
    await st.add_journal_rows(botfiles.JournalTail(p).read_new()[:2])          # 2 of 3 synced before the kill
    _write(p, [_row(3), _row(4)], header=False)                                # written after the last sync
    assert await journalsync.replay_local(p, st) == 3                          # rows 2,3,4 were missing
    assert await journalsync.replay_local(p, st) == 0                          # replay is idempotent
    rows = await st.list_journal(limit=100)
    assert sorted(r["note"] for r in rows) == ["0", "1", "2", "3", "4"]
    botfiles.write_journal_rows(p, list(reversed(rows)), FIELDS)               # the runner's rehydrate keeps all
    assert len(botfiles.JournalTail(p).read_new()) == 5


async def test_replay_never_raises(tmp_path):
    class Boom(MemoryStore):
        async def add_journal_rows(self, rows):
            raise RuntimeError("x")
    p = tmp_path / "journal.csv"
    _write(p, [_row(1)])
    assert await journalsync.replay_local(p, Boom()) == 0
    assert await journalsync.replay_local(tmp_path / "missing.csv", MemoryStore()) == 0


async def test_runner_sync_journal_survives_store_failure(env):
    p = env.paths / "journal.csv"
    _write(p, [_row(1), _row(2)])
    env.runner._tail = botfiles.JournalTail(p)
    real = env.store.add_journal_rows
    calls = {"n": 0}

    async def flaky(rows):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("atlas down")
        return await real(rows)

    env.store.add_journal_rows = flaky
    await env.runner._sync_journal("demo")
    await env.runner._sync_journal("demo")
    assert len(await env.store.list_journal(limit=100)) == 2


async def test_runner_tags_snapshot_env(env):
    p = env.paths / "journal.csv"
    note = '{"cash": 10.0, "exposure": 2.0, "headroom": 1.0}'
    _write(p, [{"utc": NOW.isoformat(), "event": "account", "note": note}])
    env.runner._tail = botfiles.JournalTail(p)
    await env.runner._sync_journal("demo")
    snap = (await env.store.list_account_snapshots())[0]
    assert snap["env"] == "demo" and snap["cash"] == 10.0


# ---------------------------------------------------------------- item 19
class FakeStore(MemoryStore):
    def __init__(self, stats=None, idx=None, st=None):
        super().__init__()
        self._stats, self._idx, self._st = stats, idx, st

    async def storage_stats(self):
        return self._stats

    async def ttl_index_report(self):
        return self._idx

    async def server_time(self):
        return self._st


@pytest.fixture(autouse=True)
def _reset():
    om.reset()
    yield
    om.reset()


MB = 1048576


async def test_ops_report_healthy_and_unavailable():
    ok = FakeStore({"used_bytes": 100 * MB, "limit_bytes": 512 * MB}, {"heartbeats": True, "journal_rows": True},
                   NOW + timedelta(seconds=1))
    r = await om.ops_report(ok, now=lambda: NOW, use_cache=False)
    assert r["storage"]["used_mb"] == 100.0 and not r["warnings"] and r["clock"]["skew_s"] == 1.0
    r = await om.ops_report(MemoryStore(), now=lambda: NOW, use_cache=False)      # MemoryStore: nothing available
    assert r["storage"]["available"] is False and r["ttl_indexes"]["ok"] is None and r["clock"]["skew_s"] is None
    assert r["warnings"] == []


async def test_ops_thresholds_and_reference_clock():
    bad = FakeStore({"used_bytes": 420 * MB, "limit_bytes": 512 * MB}, {"heartbeats": True, "journal_rows": False},
                    NOW + timedelta(seconds=2))

    async def ref():
        return NOW - timedelta(seconds=9)                                          # external reference wins over store
    r = await om.ops_report(bad, ref_time=ref, now=lambda: NOW, use_cache=False)
    assert r["storage"]["warn"] and r["ttl_indexes"]["missing"] == ["journal_rows"]
    assert r["clock"]["skew_s"] == -9.0 and r["clock"]["warn"] and r["clock"]["source"] == "reference"
    assert len(r["warnings"]) == 3


async def test_ops_source_error_is_soft():
    class Bad(FakeStore):
        async def storage_stats(self):
            raise RuntimeError("x")
    r = await om.ops_report(Bad(), now=lambda: NOW, use_cache=False)
    assert r["storage"]["available"] is False


async def test_alert_throttled_and_never_raises():
    sent = []

    class A:
        def notify(self, kind, title, msg, priority="default"):
            sent.append((kind, title))
    r = await om.ops_report(FakeStore({"used_bytes": 500 * MB, "limit_bytes": 512 * MB}, None, None),
                            now=lambda: NOW, use_cache=False)
    assert om.maybe_alert(A(), r, clock=lambda: 0.0) == 1
    assert om.maybe_alert(A(), r, clock=lambda: 60.0) == 0                         # throttled
    assert om.maybe_alert(A(), r, clock=lambda: om.ALERT_EVERY_S + 1) == 1
    assert om.maybe_alert(None, r, clock=lambda: 1e9) == 0                         # broken alerter: no raise
    assert sent[0] == ("system", "Store storage high")


def test_parse_http_date():
    assert om.parse_http_date("Thu, 05 Nov 2026 12:00:00 GMT") == NOW
    assert om.parse_http_date("garbage") is None and om.parse_http_date(None) is None


async def test_health_route_has_ops(env):
    r = await env.client.get("/api/health", headers=env.auth)
    assert r.status_code == 200
    assert set(r.json()["ops"]) == {"storage", "ttl_indexes", "clock", "warnings"}
    assert (await env.client.get("/api/health")).status_code in (401, 403)         # auth unchanged


async def test_mongo_snapshot_env_query_and_backfill():
    from control_api.db.mongo import MongoStore

    class Cur:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, *_):
            return self

        def limit(self, *_):
            return self

        async def to_list(self):
            return self.rows

    seen = {}

    class Coll:
        def find(self, q, proj=None):
            seen["q"] = q
            return Cur([{"ts": NOW, "cash": 1.0, "exposure": 1.0}])

    class Db:
        def __getitem__(self, n):
            return Coll()

    class Client:
        def __getitem__(self, n):
            return Db()

    ms = MongoStore("", client=Client())
    rows = await ms.list_account_snapshots(env="demo")
    assert rows[0]["env"] == "demo" and "$or" in seen["q"]
    await ms.list_account_snapshots(env="live")
    assert seen["q"] == {"env": "live"}

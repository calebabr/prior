"""Release-process store methods (leader lease, handover replay guard, schema version, pending releases)
on MemoryStore AND MongoStore (fake collection, no Atlas), plus the schema_v1 compatibility fixtures.

The fake collection interleaves with `await asyncio.sleep(0)` before every operation, so concurrent
coroutines really do race between the separate Mongo calls an acquire makes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from control_api.db import MemoryStore, schemas as S
from control_api.db.mongo import MongoStore

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "schema_v1"


# ------------------------------------------------------------------ fake pymongo collection
def _ok(doc, flt):
    for k, v in flt.items():
        x = doc.get(k)
        if isinstance(v, dict) and v and all(str(o).startswith("$") for o in v):
            for op, arg in v.items():
                if op == "$ne" and x == arg:
                    return False
                if op == "$lte" and not (x is not None and x <= arg):
                    return False
                if op == "$lt" and not (x is not None and x < arg):
                    return False
                if op == "$gt" and not (x is not None and x > arg):
                    return False
                if op == "$gte" and not (x is not None and x >= arg):
                    return False
                if op == "$in" and x not in arg:
                    return False
        elif x != v:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=None):
        keys = [(key, direction)] if isinstance(key, str) else list(key)
        for k, dr in reversed(keys):
            self.docs.sort(key=lambda d: (d.get(k) is not None, d.get(k)), reverse=dr == -1)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, *a):
        return self.docs


class Coll:
    def __init__(self):
        self.docs: list[dict] = []
        self.unique: list[tuple[str, ...]] = []
        self.indexes: dict[str, dict] = {}

    async def create_index(self, keys, **kw):
        name = kw.get("name") or str(keys)
        self.indexes[name] = {"keys": [k for k, _ in keys], **kw}
        if kw.get("unique"):
            self.unique.append(tuple(k for k, _ in keys))
        return name

    def _hits(self, flt, sort=None):
        hits = [d for d in self.docs if _ok(d, flt)]
        for key, direction in reversed(sort or []):
            hits.sort(key=lambda d: d.get(key), reverse=direction == -1)
        return hits

    @staticmethod
    def _proj(doc, proj):
        doc = dict(doc)
        if proj and proj.get("_id") == 0:
            doc.pop("_id", None)
        return doc

    def find(self, flt=None, proj=None):
        docs = [self._proj(d, proj) for d in self._hits(flt or {})]
        keep = {k for k, v in (proj or {}).items() if v == 1}
        if keep:
            docs = [{k: v for k, v in d.items() if k in keep} for d in docs]
        return Cursor(docs)

    async def find_one(self, flt, proj=None, sort=None):
        await asyncio.sleep(0)
        hits = self._hits(flt, sort)
        return self._proj(hits[0], proj) if hits else None

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        doc = dict(doc)
        doc.setdefault("_id", f"oid{len(self.docs)}-{id(doc)}")      # like the driver's ObjectId
        for key in [("_id",), *self.unique]:
            if any(all(d.get(k) == doc.get(k) for k in key) for d in self.docs):
                raise DuplicateKeyError("E11000 duplicate key")
        self.docs.append(doc)

    def _apply(self, d, upd):
        d.update(upd.get("$set", {}))
        for k, v in upd.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v

    async def find_one_and_update(self, flt, upd, projection=None, upsert=False, return_document=None, sort=None):
        await asyncio.sleep(0)
        hits = self._hits(flt, sort)
        if not hits:
            if not upsert:
                return None
            d = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            self._apply(d, upd)
            self.docs.append(d)
            return self._proj(d, projection) if return_document == ReturnDocument.AFTER else None
        d = hits[0]
        before = dict(d)
        self._apply(d, upd)
        return self._proj(d if return_document == ReturnDocument.AFTER else before, projection)

    async def update_one(self, flt, upd, upsert=False):
        await asyncio.sleep(0)
        hits = self._hits(flt)
        if hits:
            self._apply(hits[0], upd)
        elif upsert:
            d = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            self._apply(d, upd)
            self.docs.append(d)


UNIQUE = {S.HANDOVER_NONCES: [("nonce",)], S.RELEASES: [("id",)]}


class Client:
    def __init__(self):
        self.colls: dict[str, Coll] = {}

        class DB:
            def __getitem__(db, name):
                if name not in self.colls:
                    c = self.colls[name] = Coll()
                    c.unique.extend(UNIQUE.get(name, ()))     # what ensure_indexes() creates in production
                return self.colls[name]
        self._db = DB()

    def __getitem__(self, name):
        return self._db


@pytest.fixture(params=["memory", "mongo"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    return MongoStore("", client=Client())


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ------------------------------------------------------------------ lease
async def test_acquire_creates_then_other_is_refused(store):
    a = await store.acquire_lease("A", "v1", 30, now=T0)
    assert a == {"acquired": True, "generation": 1, "holder": "A", "expires_at": at(30)}
    b = await store.acquire_lease("B", "v2", 30, now=at(5))
    assert b == {"acquired": False, "generation": 1, "holder": "A", "expires_at": at(30)}
    lease = await store.get_lease()
    assert lease == {"holder": "A", "version": "v1", "generation": 1, "expires_at": at(30), "handover": None}
    assert "_id" not in lease


async def test_same_holder_acquire_is_a_renew_with_same_generation(store):
    await store.acquire_lease("A", "v1", 30, now=T0)
    r = await store.acquire_lease("A", "v1b", 30, now=at(10))
    assert r["acquired"] and r["generation"] == 1 and r["expires_at"] == at(40)
    assert (await store.get_lease())["version"] == "v1b"
    # even after expiry, the same holder keeps its generation (nobody else held it in between)
    r = await store.acquire_lease("A", "v1b", 30, now=at(500))
    assert r["acquired"] and r["generation"] == 1


async def test_expired_lease_is_taken_with_higher_generation_and_old_holder_is_fenced(store):
    await store.acquire_lease("A", "v1", 30, now=T0)
    assert not (await store.acquire_lease("B", "v2", 30, now=at(29.9)))["acquired"]
    b = await store.acquire_lease("B", "v2", 30, now=at(30))          # expiry instant is free
    assert b["acquired"] and b["generation"] == 2 and b["holder"] == "B"
    assert await store.renew_lease("A", 1, 30, now=at(31)) is False   # stale holder cannot renew
    assert await store.renew_lease("B", 1, 30, now=at(31)) is False   # stale generation cannot renew
    assert await store.renew_lease("B", 2, 30, now=at(31)) is True
    assert (await store.get_lease())["expires_at"] == at(61)
    c = await store.acquire_lease("A", "v1", 30, now=at(100))         # A comes back: higher again
    assert c["acquired"] and c["generation"] == 3


async def test_renew_without_lease_and_bad_ttl(store):
    assert await store.renew_lease("A", 1, 30) is False
    assert await store.get_lease() is None
    with pytest.raises(ValueError):
        await store.acquire_lease("A", "v", 0)
    with pytest.raises(ValueError):
        await store.renew_lease("A", 1, -5)


async def test_release_expires_now_stores_handover_and_lets_the_standby_in(store):
    await store.acquire_lease("A", "v1", 30, now=T0)
    assert await store.release_lease("B", 1, now=at(1)) is False
    assert await store.release_lease("A", 9, now=at(1)) is False
    ho = {"nonce": "n1", "mode": "live", "exp": T0}
    assert await store.release_lease("A", 1, handover=ho, now=at(2)) is True
    lease = await store.get_lease()
    assert lease["expires_at"] == at(2) and lease["handover"]["nonce"] == "n1" and lease["holder"] == "A"
    lease["handover"]["nonce"] = "tampered"                              # copies, not references
    assert (await store.get_lease())["handover"]["nonce"] == "n1"
    b = await store.acquire_lease("B", "v2", 30, now=at(2))             # immediately takeable
    assert b["acquired"] and b["generation"] == 2
    assert (await store.get_lease())["handover"]["nonce"] == "n1"       # the new leader can still read it
    assert await store.renew_lease("A", 1, 30, now=at(3)) is False
    # releasing again with no handover clears it
    assert await store.release_lease("B", 2, now=at(4)) is True
    assert (await store.get_lease())["handover"] is None


async def test_concurrent_acquire_exactly_one_wins(store):
    results = await asyncio.gather(*[store.acquire_lease(f"i{n}", "v", 30, now=T0) for n in range(25)])
    winners = [r for r in results if r["acquired"]]
    assert len(winners) == 1 and winners[0]["generation"] == 1
    assert {r["holder"] for r in results} == {winners[0]["holder"]}      # losers all see the winner
    assert (await store.get_lease())["holder"] == winners[0]["holder"]


async def test_concurrent_takeover_of_expired_lease_one_winner_generation_plus_one(store):
    await store.acquire_lease("old", "v", 10, now=T0)
    results = await asyncio.gather(*[store.acquire_lease(f"i{n}", "v", 30, now=at(60)) for n in range(25)])
    winners = [r for r in results if r["acquired"]]
    assert len(winners) == 1 and winners[0]["generation"] == 2
    assert (await store.get_lease())["generation"] == 2


async def test_concurrent_acquires_by_one_holder_never_bump_generation(store):
    await store.acquire_lease("A", "v", 10, now=T0)
    results = await asyncio.gather(*[store.acquire_lease("A", "v", 10, now=at(n)) for n in range(1, 20)])
    assert all(r["acquired"] and r["generation"] == 1 for r in results)


async def test_generation_is_monotonic_across_many_handoffs(store):
    gens, t = [], 0
    for n in range(12):
        r = await store.acquire_lease("AB"[n % 2], "v", 5, now=at(t))
        assert r["acquired"]
        gens.append(r["generation"])
        t += 10                                                         # always expired by the next round
    assert gens == list(range(1, 13))


async def test_clock_going_backwards_never_gives_two_leaders(store):
    await store.acquire_lease("A", "v", 30, now=at(100))
    assert not (await store.acquire_lease("B", "v", 30, now=at(0)))["acquired"]   # clock jumped back
    assert (await store.get_lease())["holder"] == "A"


# ------------------------------------------------------------------ handover nonces
async def test_consume_handover_is_single_use(store):
    assert await store.consume_handover("abc", now=T0) is True
    assert await store.consume_handover("abc", now=at(1)) is False
    assert await store.consume_handover("other", now=at(1)) is True
    assert await store.consume_handover("", now=at(1)) is False


async def test_concurrent_replay_of_one_nonce_succeeds_once(store):
    results = await asyncio.gather(*[store.consume_handover("same", now=T0) for _ in range(20)])
    assert results.count(True) == 1


async def test_memory_nonce_expires_like_the_ttl_index():
    st = MemoryStore()
    assert await st.consume_handover("n", now=T0)
    assert not await st.consume_handover("n", now=at(S.HANDOVER_NONCE_TTL_SECONDS - 1))
    assert await st.consume_handover("n", now=at(S.HANDOVER_NONCE_TTL_SECONDS + 1))


# ------------------------------------------------------------------ schema version
async def test_schema_version_roundtrip(store):
    assert await store.get_schema_version() is None
    await store.set_schema_version(1)
    assert await store.get_schema_version() == 1
    await store.set_schema_version(2)
    assert await store.get_schema_version() == 2


# ------------------------------------------------------------------ pending releases
RELEASE = {"version": "1.2.0", "commit": "abc1234", "summary": "notes", "ci": {"ok": True}}


async def test_pending_release_lifecycle(store):
    doc = await store.save_pending_release(RELEASE)
    assert doc["status"] == "pending" and len(doc["id"]) == 32 and "_id" not in doc
    assert doc["expires_at"] - doc["created_at"] == timedelta(minutes=30)
    assert doc["version"] == "1.2.0" and doc["ci"] == {"ok": True}
    assert await store.get_pending_release(doc["id"]) == doc
    assert await store.get_pending_release("nope") is None
    assert (await store.latest_pending_release())["id"] == doc["id"]
    done = await store.resolve_pending_release(doc["id"], "approved", "owner")
    assert done["status"] == "approved" and done["by"] == "owner" and done["at"] is not None
    assert await store.resolve_pending_release(doc["id"], "rejected", "owner") is None    # only from pending
    assert await store.latest_pending_release() is None
    assert (await store.get_pending_release(doc["id"]))["status"] == "approved"


async def test_pending_release_expires_and_latest_is_newest(store):
    first = await store.save_pending_release(RELEASE)
    await asyncio.sleep(0.005)
    second = await store.save_pending_release({**RELEASE, "version": "1.3.0"})
    assert (await store.latest_pending_release())["id"] == second["id"]
    late = second["expires_at"] + timedelta(seconds=1)
    assert await store.latest_pending_release(now=late) is None
    assert await store.resolve_pending_release(first["id"], "approved", "o", now=late) is None
    assert (await store.get_pending_release(first["id"]))["status"] == "pending"


async def test_resolve_validates_decision_and_rejects_secrets(store):
    doc = await store.save_pending_release(RELEASE)
    with pytest.raises(ValueError):
        await store.resolve_pending_release(doc["id"], "maybe", "o")
    with pytest.raises(ValueError):
        await store.save_pending_release({**RELEASE, "api_key": "x"})
    # caller-supplied id/status are ignored
    forged = await store.save_pending_release({**RELEASE, "id": "forged", "status": "approved"})
    assert forged["id"] != "forged" and forged["status"] == "pending"


async def test_concurrent_resolve_one_decision_wins(store):
    doc = await store.save_pending_release(RELEASE)
    res = await asyncio.gather(*[store.resolve_pending_release(doc["id"], d, "o")
                                 for d in ("approved", "rejected") * 5])
    assert sum(r is not None for r in res) == 1


# ------------------------------------------------------------------ indexes (Mongo)
async def test_ensure_indexes_adds_release_indexes_and_is_idempotent():
    client = Client()
    st = MongoStore("", client=client)
    await st.ensure_indexes()
    await st.ensure_indexes()
    nonces = client.colls[S.HANDOVER_NONCES].indexes
    assert nonces["nonce_unique"]["unique"] and nonces["ts_ttl"]["expireAfterSeconds"] == 3600
    rel = client.colls[S.RELEASES].indexes
    assert rel["id_unique"]["unique"] and rel["created_ttl"]["expireAfterSeconds"] == 30 * 24 * 3600
    assert {S.RUNNER_LEASE, S.HANDOVER_NONCES, S.META, S.RELEASES} <= set(S.COLLECTIONS)


async def test_mongo_lease_uses_the_single_leader_doc_and_meta_schema_doc():
    client = Client()
    st = MongoStore("", client=client)
    await st.acquire_lease("A", "v", 30, now=T0)
    await st.set_schema_version(1)
    assert [d["_id"] for d in client.colls[S.RUNNER_LEASE].docs] == ["leader"]
    assert [d["_id"] for d in client.colls[S.META].docs] == ["schema"]


# ------------------------------------------------------------------ S-26 projection support
async def test_memory_fill_projection_and_fill_ids_filter():
    store = MemoryStore()
    now = datetime.now(timezone.utc)
    await store.upsert_fills([{"fill_id": f"f{i}", "ts": now - timedelta(minutes=i), "ticker": "T", "count": i,
                               "fill_quality": {"trade_through": True}, "is_taker": False, "env": "demo"}
                              for i in range(4)])
    out = await store.list_fills(limit=10, fields=("fill_id", "fill_quality"), fill_ids=["f1", "f3"])
    assert [sorted(d) for d in out] == [["fill_id", "fill_quality"]] * 2
    assert [d["fill_id"] for d in out] == ["f1", "f3"]


async def test_store_projection_args():
    seen = {}

    class C(Coll):
        def find(self, q, proj=None):
            seen["proj"] = proj

            class Cur:
                def sort(s, *a, **k): return s
                def limit(s, *a): return s
                async def to_list(s): return []
            return Cur()

    class Cl(Client):
        def __init__(self):
            super().__init__()
            self.colls[S.FILLS] = C()

    st = MongoStore("", client=Cl())
    await st.list_fills(limit=5, fields=("fill_id", "fill_quality"))
    assert seen["proj"] == {"_id": 0, "fill_id": 1, "fill_quality": 1}


# ------------------------------------------------------------------ schema_v1 compatibility fixtures
def _dec(o):
    if isinstance(o, dict):
        if set(o) == {"$date"}:
            return datetime.fromisoformat(o["$date"])
        return {k: _dec(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_dec(v) for v in o]
    return o


def _fixture(name: str) -> list[dict]:
    return _dec(json.loads((FIX / f"{name}.json").read_text(encoding="utf-8")))


def _no_naive(o):
    if isinstance(o, datetime):
        assert o.tzinfo is not None
    elif isinstance(o, dict):
        assert "_id" not in o
        [_no_naive(v) for v in o.values()]
    elif isinstance(o, list):
        [_no_naive(v) for v in o]


async def test_fixture_files_exist_for_every_pre_release_collection():
    new = {S.RUNNER_LEASE, S.HANDOVER_NONCES, S.META, S.RELEASES, S.SHADOW_EPISODES, S.SHADOW_PASSES,
           S.PERIODS, S.CASH_FLOWS, S.RECON_REPORTS}
    for name in set(S.COLLECTIONS) - new:
        assert (FIX / f"{name}.json").is_file(), name
    text = "".join(p.read_text(encoding="utf-8") for p in FIX.glob("*.json"))
    assert "BEGIN" not in text and "mongodb+srv" not in text            # no secrets in fixtures


async def _read_all(st):
    return {
        "state": [await st.get_state()],
        "cloud": [await st.get_cloud_settings()],
        "config_versions": await st.list_config_versions(10),
        "heartbeats": await st.list_heartbeats(limit=10),
        "journal_rows": await st.list_journal(limit=10),
        "account_snapshots": await st.list_account_snapshots(limit=10),
        "fills": await st.list_fills(limit=10),
        "monitor": await st.list_monitor_summaries(limit=10),
        "audit": await st.list_audit(10),
        "user": [await st.get_user("owner")],
        "settlements": await st.list_settlements(limit=10),
        "arb": await st.list_arb_opportunities(limit=10),
        "episodes": await st.list_quote_episodes(limit=10),
        "alerts": await st.list_alerts(limit=10),
        "push": await st.list_push_subscriptions(),
    }


def _check_read(out):
    for key, docs in out.items():
        assert docs and all(docs), key
        _no_naive(docs)
    assert out["state"][0]["desired"] in S.DESIRED_VALUES and set(out["state"][0]) == set(S.STATE_FIELDS)
    assert out["cloud"][0]["keepalive_minutes"] and out["user"][0]["username"] == "owner"
    assert [d["version"] for d in out["config_versions"]] == sorted((d["version"] for d in out["config_versions"]),
                                                                    reverse=True)
    assert all(len(d["row_hash"]) == 40 for d in out["journal_rows"])


async def test_new_code_reads_schema_v1_docs_in_mongo_store():
    client = Client()
    st = MongoStore("", client=client)
    raw = {S.CONFIG_VERSIONS: "config_versions", S.HEARTBEATS: "heartbeats", S.JOURNAL_ROWS: "journal_rows",
           S.ACCOUNT_SNAPSHOTS: "account_snapshots", S.FILLS: "fills", S.MONITOR_SUMMARIES: "monitor_summaries",
           S.AUDIT_LOG: "audit_log", S.USERS: "users", S.SETTLEMENTS: "settlements",
           S.ARB_OPPORTUNITIES: "arb_opportunities", S.QUOTE_EPISODES: "quote_episodes",
           S.ALERT_HISTORY: "alert_history", S.PUSH_SUBSCRIPTIONS: "push_subscriptions",
           S.REVOKED_TOKENS: "revoked_tokens"}
    for coll in raw:
        client.colls[coll] = c = Coll()
        c.docs = [{"_id": f"oid{n}", **d} for n, d in enumerate(_fixture(coll))]
    client.colls[S.STATE] = s = Coll()
    s.docs = [{"_id": S.STATE_ID, **_fixture(S.STATE)[0]}]
    client.colls[S.CLOUD_SETTINGS] = c = Coll()
    c.docs = [{"_id": S.CLOUD_ID, **_fixture(S.CLOUD_SETTINGS)[0]}]

    out = await _read_all(st)
    # find_one(sort=...) is not exercised above; lease/schema/release collections are simply absent on old data
    assert await st.get_lease() is None and await st.get_schema_version() is None
    assert await st.latest_pending_release() is None
    assert await st.is_revoked("fixture-jti") is True
    _check_read(out)


async def test_new_code_reads_schema_v1_docs_in_memory_store():
    st = MemoryStore()
    state = _fixture(S.STATE)[0]
    await st.set_state(by=state["updated_by"], desired=state["desired"], mode=state["mode"])
    cloud = _fixture(S.CLOUD_SETTINGS)[0]
    await st.save_cloud_settings({k: v for k, v in cloud.items() if k not in ("updated_utc", "updated_by")}, by="fx")
    for v in _fixture(S.CONFIG_VERSIONS)[::-1]:
        await st.save_config(v["after"], before=v["before"], by=v["by"])
    for d in _fixture(S.HEARTBEATS):
        await st.add_heartbeat(d)
    assert await st.add_journal_rows(_fixture(S.JOURNAL_ROWS)) == 2
    for d in _fixture(S.ACCOUNT_SNAPSHOTS):
        await st.add_account_snapshot(d)
    await st.upsert_fills(_fixture(S.FILLS))
    for d in _fixture(S.MONITOR_SUMMARIES):
        await st.add_monitor_summary(d)
    for d in _fixture(S.AUDIT_LOG):
        await st.add_audit(d)
    await st.save_user(_fixture(S.USERS)[0])
    await st.upsert_settlements(_fixture(S.SETTLEMENTS))
    for d in _fixture(S.ARB_OPPORTUNITIES):
        await st.add_arb_opportunity(d)
    await st.upsert_quote_episodes(_fixture(S.QUOTE_EPISODES))
    for d in _fixture(S.ALERT_HISTORY):
        await st.add_alert(d)
    await st.save_push_subscription(_fixture(S.PUSH_SUBSCRIPTIONS)[0])
    out = await _read_all(st)
    _check_read(out)
    assert await st.get_lease() is None and await st.get_schema_version() is None


async def test_old_data_does_not_block_the_lease_or_schema_marker():
    st = MemoryStore()
    await st.set_state(by="old", desired="stopped", mode="demo")
    assert await st.get_schema_version() is None              # "missing" means written by an older release
    await st.set_schema_version(S.SCHEMA_VERSION)
    assert (await st.acquire_lease("A", "v", 30))["acquired"]
    assert (await st.get_state())["desired"] == "stopped"

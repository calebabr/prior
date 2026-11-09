"""Store-backed rate limits (Sprint 3 item 2): buckets survive a restart, are shared between instances on one store,
expire by window (fake clock), fail open to the in-memory counter when the store is down, keep the 429 contract, and
never touch Stop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

from control_api.auth import ratelimit as rl
from control_api.auth import security as sec
from control_api.db import MemoryStore
from control_api.db import schemas as S
from control_api.db.store import Store, ratelimit_expires_at
from control_api.main import create_app
from control_api.routes import deploy_status as ds
from control_api.routes import ux as ux_route
from control_api.tests.conftest import BrokenStore

pytestmark = pytest.mark.asyncio

SINCE = "/api/since?ts=2026-01-01T00:00:00Z"
DEPLOY = "/api/deploy/status"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _instance(settings, store, kalshi, clock: Clock | None = None):
    """One API instance (its own in-memory limiter) on a shared store."""
    app = create_app(settings, store=store, kalshi_factory=lambda m: kalshi)
    if clock is not None:
        app.state.auth_shared_ratelimiter = rl.SharedCounter(clock=clock)
    tok = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, client, {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def shared(settings, bot_paths, kalshi, monkeypatch):
    store = MemoryStore()
    await store.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    monkeypatch.setitem(ux_route.RATE_LIMITS, "since", (3, 60.0))
    monkeypatch.setattr(ds, "LIMIT", (3, 300.0))
    clients = []

    def make(clock=None, st=None):
        app, c, auth = _instance(settings, st or store, kalshi, clock)
        clients.append(c)
        return app, c, auth
    yield store, make
    for c in clients:
        await c.aclose()


# ------------------------------------------------------------------ the store contract
async def test_memory_store_counts_returns_previous_and_expires_by_window():
    s = MemoryStore()
    assert await s.ratelimit_hit("k", 10, 60.0) == (1, 0)
    assert await s.ratelimit_hit("k", 10, 60.0) == (2, 0)
    assert await s.ratelimit_hit("other", 10, 60.0) == (1, 0)          # keys are independent
    assert await s.ratelimit_hit("k", 11, 60.0) == (1, 2)               # next window sees the previous count
    assert await s.ratelimit_hit("k", 13, 60.0) == (1, 0)               # two windows on: window 11/12 expired
    exp = s._ratelimits["k:10"][1]
    assert exp == ratelimit_expires_at(10, 60.0) == datetime.fromtimestamp(12 * 60, tz=timezone.utc)


async def test_memory_store_validates_and_bounds_memory(monkeypatch):
    s = MemoryStore()
    for bad in [("", 1, 60.0), ("x" * 65, 1, 60.0), ("k", -1, 60.0), ("k", True, 60.0), ("k", 1, 0.0)]:
        with pytest.raises(ValueError):
            await s.ratelimit_hit(*bad)
    monkeypatch.setattr(S, "RATE_LIMIT_MEMORY_MAX", 5)
    for i in range(5):
        await s.ratelimit_hit(f"k{i}", 1, 60.0)
    await s.ratelimit_hit("late", 9, 60.0)                               # the old windows are swept, not kept
    assert set(s._ratelimits) == {"late:9"}


async def test_base_store_without_counters_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        await Store.ratelimit_hit(object(), "k", 1, 60.0)


async def test_mongo_ttl_index_and_atomic_upsert_shape_offline():
    from control_api.db.mongo import MongoStore
    rec = []

    class Coll:
        def __init__(self, name):
            self.name = name

        async def create_index(self, keys, **kw):
            rec.append(("index", self.name, keys, kw))

        async def find_one_and_update(self, q, upd, **kw):
            rec.append(("fau", self.name, q, upd, kw))
            return {"n": 4}

        async def find_one(self, q, proj=None, **kw):
            rec.append(("find_one", self.name, q, proj))
            return {"n": 7}

    class Db:
        def __getitem__(self, n):
            return Coll(n)

    class Client:
        def __getitem__(self, n):
            return Db()

    ms = MongoStore("", client=Client())
    await ms.ensure_indexes()
    idx = [r for r in rec if r[0] == "index" and r[1] == S.RATE_LIMITS]
    assert idx == [("index", S.RATE_LIMITS, [("expires_at", 1)], {"expireAfterSeconds": 0, "name": "expires_ttl"})]
    assert await ms.ratelimit_hit("abc", 5, 30.0) == (4, 7)
    fau = [r for r in rec if r[0] == "fau"][0]
    assert fau[1] == S.RATE_LIMITS and fau[2] == {"_id": "abc:5"}
    assert fau[3]["$inc"] == {"n": 1}
    assert fau[3]["$setOnInsert"] == {"expires_at": ratelimit_expires_at(5, 30.0)}
    assert fau[4]["upsert"] is True
    assert [r for r in rec if r[0] == "find_one"][0][2] == {"_id": "abc:4"}
    assert S.RATE_LIMITS in S.COLLECTIONS


# ------------------------------------------------------------------ the shared layer on a fake clock
async def test_shared_counter_limit_retry_after_and_ttl_expiry_on_a_fake_clock():
    clock, store = Clock(), MemoryStore()
    sc = rl.SharedCounter(clock=clock)
    clock.t = 600.0 * 1000                                    # start of a 60 s window
    for _ in range(3):
        assert await sc.hit(store, "b", "k", 3, 60.0) == (True, 0)
    ok, retry = await sc.hit(store, "b", "k", 3, 60.0)
    assert ok is False and 1 <= retry <= 120
    clock.t += 30                                             # half a window later: the previous count still weighs
    clock.t += 60                                             # next window, 30 s in: 4 * 0.5 + 1 = 3 -> allowed
    assert (await sc.hit(store, "b", "k", 3, 60.0))[0] is True
    assert (await sc.hit(store, "b", "k", 3, 60.0))[0] is False   # 2 + 2 = 4 > 3
    clock.t += 180                                            # TTL: two windows on, every old doc is ignored
    for _ in range(3):
        assert (await sc.hit(store, "b", "k", 3, 60.0))[0] is True


@pytest.mark.parametrize("cur,prev,elapsed", [(4, 0, 10.0), (3, 2, 50.0), (10, 10, 1.0), (2, 9, 5.0)])
async def test_retry_after_is_bounded_and_points_to_a_time_that_fits(cur, prev, elapsed):
    w, limit = 60.0, 3
    retry = rl.shared_retry_after(cur, prev, elapsed, w, limit)
    assert 1 <= retry <= 2 * w
    t = elapsed + retry                                        # one more attempt at that moment
    if t < w:
        est = rl.sliding_estimate(cur + 1, prev, t, w)
    else:
        est = rl.sliding_estimate(1, cur, t - w, w)
    assert est <= limit + 1e-9 or retry == 2 * w


async def test_keys_are_hashed_no_ip_or_username_in_the_store(shared):
    store, make = shared
    _, c, auth = make()
    assert (await c.get(SINCE, headers=auth)).status_code == 200
    assert (await c.get(DEPLOY, headers={"X-Deploy-Status-Key": "x"})).status_code in (401, 403)
    keys = " ".join(store._ratelimits)
    assert keys and "owner" not in keys and "127.0.0.1" not in keys and "user:" not in keys


# ------------------------------------------------------------------ restart and two instances
async def test_per_user_bucket_survives_a_restart(shared):
    store, make = shared
    _, a, auth = make()
    assert [(await a.get(SINCE, headers=auth)).status_code for _ in range(3)] == [200] * 3
    _, b, auth = make()                                        # restart: a fresh process, fresh in-memory limiter
    r = await b.get(SINCE, headers=auth)
    assert r.status_code == 429 and r.json()["detail"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


async def test_per_ip_bucket_survives_a_restart(shared):
    store, make = shared
    _, a, _ = make()
    codes = [(await a.get(DEPLOY, headers={"X-Deploy-Status-Key": "wrong"})).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    _, b, _ = make()
    assert (await b.get(DEPLOY, headers={"X-Deploy-Status-Key": "wrong"})).status_code == 429


async def test_bucket_is_shared_between_two_live_instances(shared):
    store, make = shared
    _, a, auth = make()
    _, b, _ = make()
    codes = [(await (a if i % 2 == 0 else b).get(SINCE, headers=auth)).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]                      # 2 + 1 on one instance would each be under 3 alone
    assert (await a.get(SINCE, headers=auth)).status_code == 429


async def test_login_bucket_is_shared_and_still_429(shared, monkeypatch):
    store, make = shared

    async def fast_false(*a, **k):                             # skip argon2: this test is about counting
        return False
    monkeypatch.setattr(sec, "verify_password_async", fast_false)
    apps = [make(), make()]
    for app, _, _ in apps:
        app.state.auth_ratelimiter = rl.SlidingWindowLimiter({**rl.DEFAULT_LIMITS, "login": (3, 300.0)})
    body = {"username": "owner", "password": "wrong"}
    codes = [(await apps[i % 2][1].post("/api/auth/login", json=body)).status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]
    u = await store.get_user("owner")
    assert not u.get("locked_until")                          # wrong passwords still never lock the account (S-2)


async def test_window_expiry_reopens_the_bucket_on_a_fake_clock(shared):
    store, make = shared
    clock = Clock()
    _, a, auth = make(clock)
    assert [(await a.get(SINCE, headers=auth)).status_code for _ in range(3)] == [200] * 3
    clock.t += 180                                             # 3 windows later (the TTL removed the docs)
    _, b, auth = make(clock)                                   # restarted instance, same store and clock
    assert (await b.get(SINCE, headers=auth)).status_code == 200


# ------------------------------------------------------------------ store down / slow: fail open to memory, never 5xx
class _RateLimitDown(MemoryStore):
    def __init__(self, hang: bool = False):
        super().__init__()
        self.hang, self.calls = hang, 0

    async def ratelimit_hit(self, *a, **k):
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        raise RuntimeError("secret-uri mongodb://u:p@h down")


@pytest.mark.parametrize("hang", [False, True])
async def test_store_down_or_hung_falls_back_to_the_in_memory_counter(settings, bot_paths, kalshi, monkeypatch,
                                                                       caplog, hang):
    monkeypatch.setitem(ux_route.RATE_LIMITS, "since", (3, 60.0))
    store = _RateLimitDown(hang)
    await store.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    app, c, auth = _instance(settings, store, kalshi)
    app.state.auth_shared_ratelimiter = rl.SharedCounter(timeout_s=0.05)
    async with c:
        codes = [(await c.get(SINCE, headers=auth)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]                       # the in-memory limit still holds; never 5xx
    assert store.calls == 1                                    # then the shared layer is skipped for the cooldown
    assert "mongodb://" not in caplog.text and "u:p@" not in caplog.text


async def test_store_down_cooldown_ends_and_the_shared_layer_returns():
    clock = Clock()
    sc = rl.SharedCounter(clock=clock, cooldown_s=30.0)
    down = _RateLimitDown()
    assert await sc.hit(down, "b", "k", 3, 60.0) == (None, 0)
    assert not sc.available() and await sc.hit(MemoryStore(), "b", "k", 3, 60.0) == (None, 0)
    clock.t += 31
    assert sc.available() and await sc.hit(MemoryStore(), "b", "k", 3, 60.0) == (True, 0)


async def test_a_store_without_the_counter_method_fails_open(settings, bot_paths, kalshi, monkeypatch):
    monkeypatch.setitem(ux_route.RATE_LIMITS, "since", (2, 60.0))

    class Old(MemoryStore):
        ratelimit_hit = Store.ratelimit_hit                     # an older store: NotImplementedError
    store = Old()
    await store.save_user({"username": "owner", "password_hash": "x", "failed_logins": 0})
    _, c, auth = _instance(settings, store, kalshi)
    async with c:
        assert [(await c.get(SINCE, headers=auth)).status_code for _ in range(3)] == [200, 200, 429]


async def test_login_with_the_store_down_stays_closed(settings, bot_paths, kalshi):
    """Auth buckets: the limiter falls back to memory, and login itself still fails closed without the store."""
    app, c, _ = _instance(settings, BrokenStore(), kalshi)
    app.state.auth_ratelimiter = rl.SlidingWindowLimiter({**rl.DEFAULT_LIMITS, "login": (2, 300.0)})
    async with c:
        codes = [(await c.post("/api/auth/login", json={"username": "owner", "password": "pw"})).status_code
                 for _ in range(3)]
    assert codes == [503, 503, 429]


async def test_locally_refused_attempts_never_reach_the_store(shared):
    store, make = shared
    calls = []
    real = store.ratelimit_hit

    async def counting(*a, **k):
        calls.append(a)
        return await real(*a, **k)
    store.ratelimit_hit = counting
    _, c, auth = make()
    codes = [(await c.get(SINCE, headers=auth)).status_code for _ in range(10)]
    assert codes == [200] * 3 + [429] * 7 and len(calls) == 3   # a flood is refused in memory, no store writes


# ------------------------------------------------------------------ Stop
async def test_stop_never_counts_against_or_waits_for_the_limiter(shared):
    store, make = shared
    calls = []

    async def counting(*a, **k):
        calls.append(a)
        await asyncio.Event().wait()                           # a hung counter store would hang anything using it
    store.ratelimit_hit = counting
    app, c, auth = make()
    for _ in range(30):
        r = await asyncio.wait_for(c.post("/api/bot/stop", headers=auth), 5)
        assert r.status_code == 200
    assert calls == [] and app.state.runner.desired == "stopped"

"""Read-only analytics: GET /api/analytics/summary and GET /api/analytics/trades (docs/ANALYTICS_CONTRACT.md).

Signed-in session only (no fresh 2FA, like /history).

Load guards (Atlas M0 + the 512 MB instance; SECURITY_REVIEW S-28 / S-30):
* ONE build per process at a time (the gate). A build reads the store once and produces BOTH the summary and the
  trade rows for one (env, days); concurrent requests for the same key share it (single flight); a request for
  another key waits up to BUSY_WAIT_S for the gate, then gets 429 `analytics_busy` + Retry-After.
* Builds run on a dedicated one-thread executor, never the default pool that Stop's cancel uses (S-25 family).
* Results are cached: the summary SUMMARY_TTL_S (60 s) per (env, days, period, tz) (small; `generated_utc` shows its age) and
  the trade rows per (env, days, period) (at most ROWS_CACHE_KEYS keys; the largest objects, independent of the
  viewer's time zone), so /trades paging and filtering never re-reads Atlas. The rows live CACHE_TTL_S (300 s);
  /trades and /summary treat them as expired after 60 s (unchanged), the LEDGER (Home) uses stale-while-revalidate:
  a copy older than REFRESH_AFTER_S but younger than CACHE_TTL_S is served at once and refreshed once in the
  background (never more than one refresh at a time, through the same gate). After each bot pass the runner's
  post-pass hook calls warm(), which pre-builds the default key so Home usually finds a copy. A failed refresh or
  warm-up only logs. Both caches are bounded
  (CACHE_MAX_KEYS summaries; the oldest entry is dropped first). `tz` is validated (pnl.resolve_tz: IANA names
  only, anything else means UTC), so the key space is the tz database, never raw client text. Only successful
  builds are cached.
* Store reads are projected to the fields used. Journal `quote` rows are folded into an order_id -> cell map kept
  across requests and refreshed incrementally (only rows newer than the last read), so the journal cap is not hit
  on every request (S-30).
* Both routes are rate limited PER USER with the auth sliding-window limiter (own buckets, so login/2FA/step-up
  limits and Stop are unaffected): 429 `rate_limited` + Retry-After.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..auth.deps import current_user
from ..auth.ratelimit import get_limiter
from ..services import analytics as an
from ..services import arbsets
from ..services import botfiles
from ..services import ledger as ledgersvc
from ..services import periods as per
from ..services import pnl as pnlsvc
from ..services import returns as retsvc
from ..services import strategies as strat
from .periods import window_for

router = APIRouter(prefix="/api")
log = logging.getLogger("analytics")
STORE_TIMEOUT_S = 10.0
CACHE_TTL_S = 300.0                      # trade rows (the ledger's dataset) are kept this long; see the module doc
SUMMARY_TTL_S = 60.0                     # a cached summary is served this long (unchanged)
REFRESH_AFTER_S = 60.0                   # ledger: older rows are served stale while one background refresh runs
WARM_MIN_GAP_S = 120.0                   # a warm-up is skipped if another ran this recently (run-once spam)
WARM_DAYS = 30
CACHE_MAX_KEYS = 64                      # summaries per (env, days, period, tz); the oldest is dropped beyond this
RETURNS_CACHE_KEYS = 24                  # returns bodies per (env, days, tz, granularity); the oldest is dropped
ROWS_CACHE_KEYS = 2                      # cached trade-row sets (each up to MAX_FILLS rows)
BUSY_WAIT_S = 8.0                        # how long a request waits for another key's build before 429
BUSY_RETRY_S = 5
JOURNAL_OVERLAP = timedelta(hours=6)     # incremental journal reads re-read this much (late-synced rows)
JOURNAL_FULL_REFRESH_S = 6 * 3600.0      # rebuild the order_id -> cell map from scratch this often
SNAP_BEFORE = timedelta(days=14)           # ledger / scorecard read snapshots from this long before the window
SNAP_LIMIT = 20_000
HB_LIMIT = 20_000
ALERT_LIMIT = 2000
AUDIT_LIMIT = 1000
JOURNAL_MAP_MAX = 60_000                 # order ids kept in the map (oldest dropped beyond this)
RATE_LIMITS = {"analytics_summary": (30, 60.0), "analytics_trades": (60, 60.0),
               "analytics_ledger": (20, 60.0), "analytics_returns": (20, 60.0)}   # per user, per minute
_clock = time.monotonic                  # tests monkeypatch this
_SERIES_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.]{0,39}$")
_CHOICES = {"env": an.ENVS, "side": ("yes", "no"), "kind": ("maker", "taker"), "status": ("settled", "open"),
            "result": ("win", "loss"), "source": ("bot", "you", "all"),
            "strategy": tuple(strat.STRATEGIES)}
_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """Dedicated single worker for analytics builds (at most one runs at a time anyway)."""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics")
    return _POOL


def _bad(field: str, msg: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["query", field], "msg": msg}])


def _choice(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _CHOICES[field]:
        raise _bad(field, f"must be one of {list(_CHOICES[field])}")
    return value


def _int(field: str, raw: str | None, default: int, lo: int, hi: int | None = None,
         allowed: tuple[int, ...] | None = None) -> int:
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise _bad(field, "must be an integer") from None
    if allowed is not None and v not in allowed:
        raise _bad(field, f"must be one of {list(allowed)}")
    if v < lo or (hi is not None and v > hi):
        raise _bad(field, f"must be between {lo} and {hi}" if hi is not None else f"must be >= {lo}")
    return v


def _context(request: Request) -> tuple[list, list[str]]:
    """(config cells, known series) -- read only."""
    r = request.app.state.runner
    cfg = r.config or botfiles.read_config_file(r.config_path) or {}
    try:
        ms = botfiles.strategy()
        defaults = ms.DEFAULTS
    except Exception:  # noqa: BLE001
        defaults = {}
    cells = cfg.get("cells") or defaults.get("cells") or []
    series = list(dict.fromkeys(list(defaults.get("series") or []) + list(cfg.get("series") or [])
                                + list(((cfg.get("arb") or {}).get("arb_series")) or [])))
    return cells, series


def _rate_limit(request: Request, user: dict, bucket: str) -> None:
    """Per-user sliding window on the app's auth limiter (own bucket). 429 + Retry-After when exceeded."""
    lim = get_limiter(request)
    lim.limits.setdefault(bucket, RATE_LIMITS[bucket])
    ok, retry = lim.hit(bucket, "user:" + str(user.get("username") or "?"))
    if not ok:
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(retry)})


def _cache(request: Request) -> dict:
    c = getattr(request.app.state, "analytics_cache", None)
    if c is None:
        c = request.app.state.analytics_cache = {}
    return c


def _state(request: Request) -> dict:
    st = getattr(request.app.state, "analytics", None)
    if st is None:
        st = request.app.state.analytics = {"rows": {}, "inflight": {}, "gate": asyncio.Lock(), "jmap": None,
                                            "refresh_task": None, "warm_at": None, "last_tz": None,
                                            "returns": {}}
    return st


def clear_cache(app) -> None:
    """Drop cached summaries, trade rows and the journal cell map (tests / tooling)."""
    app.state.analytics_cache = {}
    st = getattr(app.state, "analytics", None)
    if st is not None:
        st["rows"].clear()
        st.setdefault("returns", {}).clear()
        st["jmap"] = None
        st["warm_at"] = None


def _env(request: Request, raw: str | None) -> str:
    if raw is None:
        mode = getattr(request.app.state.runner, "mode", None)
        return mode if mode in ("demo", "live") else "demo"
    return _choice("env", raw)


async def _read(coro):
    try:
        return await asyncio.wait_for(coro, timeout=STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


def _capped(rows: list, cap: int) -> tuple[list, bool]:
    return (rows[:cap], True) if len(rows) > cap else (rows, False)


def _fresh(entry, t0: float, ttl: float | None = None) -> bool:
    return entry is not None and 0 <= t0 - entry[0] < (SUMMARY_TTL_S if ttl is None else ttl)


async def _journal_map(request: Request, start: datetime, now: datetime) -> tuple[dict[str, str], bool]:
    """order_id -> cell id from journal quote rows since `start`, kept across requests (S-30).

    The first read (or one reaching further back, or one every JOURNAL_FULL_REFRESH_S) reads the window; later reads
    only fetch rows newer than the newest seen (minus JOURNAL_OVERLAP). Projected to event/note/cell/ts, and folded
    into the map right away (the rows are not kept). Returns (map, truncated)."""
    st = _state(request)
    jm = st["jmap"]
    t = _clock()
    full = jm is None or start < jm["from"] or not 0 <= t - jm["built"] < JOURNAL_FULL_REFRESH_S
    since = start if full else jm["hw"] - JOURNAL_OVERLAP
    rows, trunc = _capped(await _read(request.app.state.store.list_journal(
        since=since, limit=an.MAX_JOURNAL + 1, events=["quote"], fields=an.JOURNAL_FIELDS)), an.MAX_JOURNAL)
    entries = an.journal_cell_entries(rows)
    del rows
    hw = max((e[1] for e in entries.values() if e[1] is not None), default=since)
    if full:
        jm = {"from": start, "built": t, "hw": hw, "map": entries, "truncated": trunc}
    else:
        for k, v in entries.items():
            jm["map"].setdefault(k, v)
        jm["hw"] = max(jm["hw"], hw)
        jm["truncated"] = jm["truncated"] or trunc
    m = jm["map"]
    lo = now - timedelta(days=max(an.DAYS_ALLOWED)) - an.LOOKBACK
    for k in [k for k, v in m.items() if v[1] is not None and v[1] < lo]:
        del m[k]
    if len(m) > JOURNAL_MAP_MAX:                              # keep the newest
        jm["map"] = m = dict(sorted(m.items(), key=lambda kv: kv[1][1] or now, reverse=True)[:JOURNAL_MAP_MAX])
        jm["truncated"] = True
    st["jmap"] = jm
    return {k: v[0] for k, v in m.items()}, jm["truncated"]


async def _period(request: Request, days: int) -> tuple[int, dict | None]:
    """(days, period) for the optional `period=<id|current|all>` filter (default all = unchanged behaviour). With a
    period the rolling `days` is replaced by the smallest allowed value that reaches back to the period start, and
    the figures are attributed by FILL date inside [started_at, ended_at)."""
    p = await window_for(request, request.query_params.get("period"))
    if p is None:
        return days, None
    return per.cover_days(p["started_at"], datetime.now(timezone.utc), an.DAYS_ALLOWED), p


def _pkey(period: dict | None) -> tuple | None:
    return None if period is None else (period["id"], per.iso(period["started_at"]), per.iso(period["ended_at"]))


async def _build(request: Request, env: str, days: int, period: dict | None = None, tz: str = "UTC"
                 ) -> tuple[dict, list[dict]]:
    """Bounded, projected reads -> (summary, trade rows), computed on the dedicated executor. Runs under the gate."""
    store = request.app.state.store
    cells, series = _context(request)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    start = since - an.LOOKBACK
    until = None
    if period is not None:                                    # fill-date attribution: no look-back needed
        since = start = per.lookback_start(period["started_at"], now)
        until = period["ended_at"]
    fills, t1 = _capped(await _read(store.list_fills(since=start, until=until, limit=an.MAX_FILLS + 1,
                                                     fields=an.FILL_FIELDS)), an.MAX_FILLS)
    settl, t2 = _capped(await _read(store.list_settlements(since=start, limit=an.MAX_SETTLEMENTS + 1,
                                                           fields=an.SETTLEMENT_FIELDS)), an.MAX_SETTLEMENTS)
    eps, t3 = _capped(await _read(store.list_quote_episodes(since=start, env=None if env == "all" else env,
                                                            limit=an.MAX_EPISODES + 1, fields=an.EPISODE_FIELDS)),
                      an.MAX_EPISODES)
    jcells, t4 = await _journal_map(request, start, now)
    arbs, t5 = _capped(await _read(store.list_arb_opportunities(since=since, limit=an.MAX_ARB + 1)), an.MAX_ARB)
    truncated = any((t1, t2, t3, t4, t5))

    def build() -> tuple[dict, list[dict]]:
        rows = an.trade_rows(fills, settl, eps, cells=cells, series_list=series, env=env, jcells=jcells)
        return an.summary(rows, eps, arbs, days=days, env=env, now=now, cells=cells, truncated=truncated,
                          expectations=an.load_expectations(), jcells=jcells, series_list=series,
                          period=period, tz=tz), rows
    return await asyncio.get_running_loop().run_in_executor(_pool(), build)


def _store_result(request: Request, key: tuple, t0: float, summary: dict, rows: list[dict]) -> None:
    """key = (env, days, period key, validated tz). The summary is cached per full key; the rows per key without
    the zone (they do not depend on it). Both caches drop expired entries, then the OLDEST ones beyond their bound."""
    cache, rc = _cache(request), _state(request)["rows"]
    for c, ttl in ((cache, SUMMARY_TTL_S), (rc, CACHE_TTL_S)):
        for k in [k for k, v in c.items() if not _fresh(v, t0, ttl)]:
            c.pop(k, None)                                    # drop expired entries
    while len(cache) >= CACHE_MAX_KEYS:
        cache.pop(min(cache, key=lambda k: cache[k][0]))      # bounded: oldest first
    cache[key] = (t0, summary)
    rkey = key[:3]
    rc.pop(rkey, None)
    while len(rc) >= ROWS_CACHE_KEYS:                         # drop the oldest row set
        rc.pop(min(rc, key=lambda k: rc[k][0]))
    rc[rkey] = (t0, rows)


def _pick(res: tuple[dict, list[dict]], want: str):
    return res[0] if want == "summary" else res[1]


async def _run_build(request, st: dict, key: tuple, env: str, days: int, period: dict | None, tz: str):
    """Single-flight build + cache store. The CALLER holds the gate. Raises HTTPException on a store failure."""
    fut = asyncio.get_running_loop().create_future()
    st["inflight"][key] = fut
    t0 = _clock()
    try:
        res = await _build(request, env, days, period, tz)
    except BaseException as e:
        fut.set_exception(e if isinstance(e, HTTPException) else HTTPException(503, "store_unavailable"))
        fut.exception()                                       # mark retrieved (no "never retrieved" warning)
        raise
    finally:
        st["inflight"].pop(key, None)
    _store_result(request, key, t0, *res)
    fut.set_result(res)
    return res


async def _background_build(app, key: tuple, env: str, days: int, period: dict | None, tz: str, wait: bool) -> bool:
    """A build nobody is waiting on (stale refresh or warm-up). Never raises (only logs); True if it stored a result.
    wait=False gives up at once when another build holds the gate."""
    ctx = SimpleNamespace(app=app)
    st = _state(ctx)
    gate: asyncio.Lock = st["gate"]
    try:
        if wait:
            await asyncio.wait_for(gate.acquire(), timeout=BUSY_WAIT_S)
        elif gate.locked():
            return False
        else:
            await gate.acquire()
    except asyncio.TimeoutError:
        return False
    try:
        if key in st["inflight"]:
            return False
        await _run_build(ctx, st, key, env, days, period, tz)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("analytics background build failed: %s", type(e).__name__)
        return False
    finally:
        gate.release()


def _spawn_refresh(request: Request, key: tuple, env: str, days: int, period: dict | None, tz: str) -> None:
    """At most ONE background refresh at a time; skipped while any build holds the gate."""
    st = _state(request)
    t = st.get("refresh_task")
    if (t is not None and not t.done()) or st["gate"].locked() or key in st["inflight"]:
        return
    st["refresh_task"] = asyncio.create_task(_background_build(request.app, key, env, days, period, tz, wait=False))


async def warm(app) -> None:
    """Pre-build the default dataset (current mode, 30 days, UTC and the last time zone a viewer used) after a bot
    pass, so Home's ledger finds a copy. Read only, one build at a time through the gate, rate limited by
    WARM_MIN_GAP_S. NEVER raises (a failure only logs): it runs beside the trading loop, not in it."""
    try:
        st = _state(SimpleNamespace(app=app))
        t = _clock()
        if st.get("warm_at") is not None and 0 <= t - st["warm_at"] < WARM_MIN_GAP_S:
            return
        st["warm_at"] = t
        mode = getattr(app.state.runner, "mode", None)
        env = mode if mode in ("demo", "live") else "demo"
        zones = ["UTC"]
        if st.get("last_tz") and st["last_tz"] != "UTC":
            zones.append(st["last_tz"])
        for tz in zones:
            await _background_build(app, (env, WARM_DAYS, None, tz), env, WARM_DAYS, None, tz, wait=True)
    except Exception as e:  # noqa: BLE001
        log.warning("analytics warm-up failed: %s", type(e).__name__)


async def _dataset(request: Request, env: str, days: int, want: str, period: dict | None = None,
                   tz: str = "UTC", swr: bool = False):
    """Cached summary (want="summary") or trade rows (want="rows") for (env, days[, period][, tz]); at most one
    build at a time. `tz` must already be a validated name (pnl.resolve_tz); rows ignore it. swr=True (the ledger,
    rows only): a copy up to CACHE_TTL_S old is served immediately, and past REFRESH_AFTER_S one background refresh
    is started."""
    st, key = _state(request), (env, days, _pkey(period), tz)

    def entry():
        return _cache(request).get(key) if want == "summary" else st["rows"].get(key[:3])

    def cached():
        hit = entry()
        return hit[1] if _fresh(hit, _clock(), REFRESH_AFTER_S if want == "rows" else SUMMARY_TTL_S) else None

    res = cached()
    if res is not None:
        return res
    if swr and want == "rows":
        hit = entry()
        if _fresh(hit, _clock(), CACHE_TTL_S):
            _spawn_refresh(request, key, env, days, period, tz)
            return hit[1]
    fut = st["inflight"].get(key)
    if fut is not None:                                       # same key already building: share it
        return _pick(await asyncio.shield(fut), want)
    gate: asyncio.Lock = st["gate"]
    try:
        await asyncio.wait_for(gate.acquire(), timeout=BUSY_WAIT_S)
    except asyncio.TimeoutError:
        raise HTTPException(429, "analytics_busy", headers={"Retry-After": str(BUSY_RETRY_S)}) from None
    try:
        res = cached()                                        # built while we waited for the gate?
        if res is not None:
            return res
        fut = st["inflight"].get(key)
        if fut is not None:
            return _pick(await asyncio.shield(fut), want)
        return _pick(await _run_build(request, st, key, env, days, period, tz), want)
    finally:
        gate.release()


@router.get("/analytics/summary")
async def analytics_summary(request: Request, user: dict = Depends(current_user)) -> dict:
    _rate_limit(request, user, "analytics_summary")
    q = request.query_params
    days = _int("days", q.get("days"), 30, 1, allowed=an.DAYS_ALLOWED)
    env = _env(request, q.get("env"))
    _zone, tzname = pnlsvc.resolve_tz(q.get("tz"))       # unknown / malformed -> UTC, never an error
    _state(request)["last_tz"] = tzname                  # the warm-up also pre-builds this zone's summary
    days, period = await _period(request, days)
    out = await _dataset(request, env, days, "summary", period, tzname)
    return _with_restart(request, out)


def _with_restart(request: Request, out: dict) -> dict:
    """Merge the runner's in-memory counters into a COPY of the (cached) summary, labelled as since-restart."""
    arb_sum = dict(out.get("arb_summary") or {})
    arb_sum["since_restart"] = since_restart(request)
    return {**out, "arb_summary": arb_sum}


def since_restart(request: Request) -> dict:
    """The runner's raw arbitrage counters (reset by every restart) with the time they count from."""
    r = request.app.state.runner
    st = r.arb_status()
    started = getattr(r, "started_utc", None)
    return {"label": "since restart (in-memory, reset by every restart or deploy)",
            "since_utc": started.isoformat() if isinstance(started, datetime) else None,
            "scans": st.get("scans"), "sightings": st.get("found")}


def _filters(request: Request) -> tuple[int, str, dict]:
    """Validated (days, env, filters) shared by /trades and /trades.csv (422 on bad values)."""
    q = request.query_params
    days = _int("days", q.get("days"), 30, 1, allowed=an.DAYS_ALLOWED)
    env = _env(request, q.get("env"))
    cells, series = _context(request)
    flt = {k: _choice(k, q.get(k)) for k in ("side", "kind", "status", "result")}
    b = q.get("band")
    bands = an.band_labels(cells) + (an.BAND_OTHER,)          # derived from the installed cells, never hard-coded
    if b is not None and b not in bands:
        raise _bad("band", f"must be one of {list(bands)}")
    flt["band"] = b
    # Y1: bot-only by default (the "bot-only vs combined" toggle defaults to bot-only); source=all or you opt in
    src = _choice("source", q.get("source")) or "bot"
    flt["source"] = None if src == "all" else src
    flt["strategy"] = _choice("strategy", q.get("strategy"))
    s = q.get("series")
    if s is not None and (not _SERIES_RE.match(s) or s not in series):
        raise _bad("series", f"must be one of {series}")
    flt["series"] = s
    c = q.get("cell")
    if c is not None:
        ids = [an.cell_id(x) for x in an.parse_cells(cells)] + [an.CELL_NONE]
        if c not in ids:
            raise _bad("cell", f"must be one of {ids}")
    flt["cell"] = c
    return days, env, flt


async def _hits(request: Request, days: int, env: str, flt: dict) -> list[dict]:
    days, period = await _period(request, days)
    rows = await _dataset(request, env, days, "rows", period)
    since, until = datetime.now(timezone.utc) - timedelta(days=days), None
    if period is not None:                                    # attributed by fill date inside the period
        since = period["started_at"] or datetime(1970, 1, 1, tzinfo=timezone.utc)
        until = period["ended_at"]
    return an.filter_trades(rows, since=since, until=until, **flt)   # a filter over cached rows (no store read)


@router.get("/analytics/trades")
async def analytics_trades(request: Request, user: dict = Depends(current_user)) -> dict:
    _rate_limit(request, user, "analytics_trades")
    q = request.query_params
    limit = _int("limit", q.get("limit"), 50, 1, 200)
    offset = _int("offset", q.get("offset"), 0, 0, 1_000_000)
    days, env, flt = _filters(request)
    hits = await _hits(request, days, env, flt)
    return {"total": len(hits), "offset": offset, "limit": limit,
            "trades": [an.public_trade(r) for r in hits[offset:offset + limit]]}


@router.get("/analytics/my-trades")
async def analytics_my_trades(request: Request, user: dict = Depends(current_user)) -> dict:
    """Y5: the owner's own (manual) trades beside the bot. Same cache, gate and rate-limit bucket as /summary."""
    _rate_limit(request, user, "analytics_summary")
    q = request.query_params
    days = _int("days", q.get("days"), 30, 1, allowed=an.DAYS_ALLOWED)
    env = _env(request, q.get("env"))
    limit = _int("limit", q.get("limit"), 100, 1, 200)
    days, period = await _period(request, days)
    rows = await _dataset(request, env, days, "rows", period)
    return an.my_trades(rows, days=days, env=env, now=datetime.now(timezone.utc), limit=limit, period=period,
                        tz=q.get("tz"))


async def _soft(coro, notes: list[str], what: str, default=None):
    """An optional read: a store failure is reported in `notes` and the metric is null, never a 5xx."""
    try:
        return await asyncio.wait_for(coro, timeout=STORE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        notes.append(f"{what}: store read failed; the figures that need it are null")
        return default


async def _parallel(*coros):
    """Run independent store reads at once (total ~ the slowest, not the sum). Each coroutine keeps its own timeout
    and failure rule (`_read` raises 503, `_soft` returns its default); the first exception cancels the rest."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _window(request: Request, days: int) -> tuple[int, dict | None, datetime, datetime | None, datetime]:
    """(days, period, since, until, now) for the rolling window or the period (fill-date attribution)."""
    now = datetime.now(timezone.utc)
    days, period = await _period(request, days)
    if period is not None:
        return days, period, period["started_at"] or datetime(1970, 1, 1, tzinfo=timezone.utc),             period["ended_at"], now
    return days, None, now - timedelta(days=days), None, now


def _in_window(rows: list[dict], since: datetime, until: datetime | None) -> list[dict]:
    return [r for r in rows if r["_ts"] >= since and (until is None or r["_ts"] < until)]


@router.get("/analytics/ledger")
async def analytics_ledger(request: Request, user: dict = Depends(current_user)) -> dict:
    """The Honest Ledger (services/ledger.py): fills netted to settlements per strategy and per local day, plus the
    reconciliation of the account equity change against realized P&L and cash flows. Read only; signed-in session
    (no fresh 2FA); same filters as /analytics/trades (days, env, period) plus `tz` (validated; UTC fallback)."""
    _rate_limit(request, user, "analytics_ledger")
    q = request.query_params
    days = _int("days", q.get("days"), 30, 1, allowed=an.DAYS_ALLOWED)
    env = _env(request, q.get("env"))
    zone, tzname = pnlsvc.resolve_tz(q.get("tz"))
    _state(request)["last_tz"] = tzname
    days, period, since, until, now = await _window(request, days)
    all_rows = await _dataset(request, env, days, "rows", period, swr=True)
    rows = _in_window(all_rows, since, until)
    store, notes = request.app.state.store, []
    arb_raw, snaps, flows = await _parallel(
        _read(store.list_arb_opportunities(since=since, limit=an.MAX_ARB + 1)),
        _soft(store.list_account_snapshots(since=since - SNAP_BEFORE, limit=SNAP_LIMIT), notes,
              "account snapshots", []),
        _soft(store.list_cash_flows(since=since - SNAP_BEFORE), notes, "cash flows", []))
    arbs, _t = _capped(arb_raw, an.MAX_ARB)
    sets = arbsets.build_sets(arbs, [r for r in rows if r.get("strategy") == "arb"], env=env, since=since,
                              until=until, limit=arbsets.MAX_SETS * 5)
    out = ledgersvc.build(rows, sets, zone=zone, tzname=tzname, since=since, until=until, snapshots=snaps or [],
                          flows=flows or [], now=now, all_rows=all_rows, truncated=len(all_rows) >= an.MAX_FILLS)
    out["env"], out["days"] = env, days
    if period is not None:
        out["period"] = {"id": period.get("id"), "name": period.get("name")}
    out["notes"] = out["notes"] + notes
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)                  # tests monkeypatch this


def _parse_date(field: str, raw: str | None):
    try:
        return datetime.strptime(raw or "", "%Y-%m-%d").date()
    except ValueError:
        raise _bad(field, "must be a date YYYY-MM-DD") from None


@router.get("/analytics/returns")
async def analytics_returns(request: Request, user: dict = Depends(current_user)) -> dict:
    """Returns per calendar day / week / month (services/returns.py): realized P&L of bot fills by settlement date in
    the viewer's zone, over a modified-Dietz base from account snapshots and cash flows. Whole periods only. Read
    only; signed in (no 2FA); validated params (422); own cache per (env, days, tz, granularity), 60 s, 24 keys."""
    _rate_limit(request, user, "analytics_returns")
    q = request.query_params
    gran = q.get("granularity", "day")
    if gran not in retsvc.GRANULARITIES:
        raise _bad("granularity", f"must be one of {list(retsvc.GRANULARITIES)}")
    days = _int("days", q.get("days"), 30, 1, max(an.DAYS_ALLOWED))     # any 1..180: the app asks 84 for weeks
    ds_days = next(d for d in an.DAYS_ALLOWED if d >= days)           # rows come from the smallest cached set covering it
    env = _env(request, q.get("env"))
    zone, tzname = pnlsvc.resolve_tz(q.get("tz"))
    st = _state(request)
    st["last_tz"] = tzname
    key, t0, rc = (env, days, tzname, gran), _clock(), st.setdefault("returns", {})
    hit = rc.get(key)
    if _fresh(hit, t0, SUMMARY_TTL_S):
        return hit[1]
    now = _now()
    since = now - timedelta(days=days)
    all_rows = await _dataset(request, env, ds_days, "rows", None, swr=True)
    store, notes = request.app.state.store, []
    snaps, flows = await _parallel(
        _soft(store.list_account_snapshots(since=since - SNAP_BEFORE, limit=SNAP_LIMIT), notes,
              "account snapshots", []),
        _soft(store.list_cash_flows(since=since - SNAP_BEFORE), notes, "cash flows", []))
    if len(snaps or []) >= SNAP_LIMIT:
        notes.append("the snapshot read hit its row limit: the oldest periods may have no start balance")
    mode = getattr(request.app.state.runner, "mode", None)
    out = retsvc.build(all_rows, snaps or [], flows or [], zone=zone, tzname=tzname, granularity=gran, days=days,
                       env=env, env_is_current=env in ("demo", "live") and env == mode, now=now,
                       truncated=len(all_rows) >= an.MAX_FILLS, notes=notes)
    if not any("store read failed" in n for n in notes):     # only successful builds are cached
        for k in [k for k, v in rc.items() if not _fresh(v, t0, SUMMARY_TTL_S)]:
            rc.pop(k, None)
        while len(rc) >= RETURNS_CACHE_KEYS:
            rc.pop(min(rc, key=lambda k: rc[k][0]))
        rc[key] = (t0, out)
    return out


@router.get("/analytics/returns/fills")
async def analytics_returns_fills(request: Request, user: dict = Depends(current_user)) -> dict:
    """Drill-down for one period: bot fills settled in the local dates [start, end], newest settlement first, from the
    cached rows. `start` / `end` are the period's `start_local` / `end_local`. Read only; same bucket as /returns."""
    _rate_limit(request, user, "analytics_returns")
    q = request.query_params
    start, end = _parse_date("start", q.get("start")), _parse_date("end", q.get("end"))
    if end < start or (end - start).days >= retsvc.FILLS_MAX_SPAN_DAYS:
        raise _bad("end", f"must be on or after start and within {retsvc.FILLS_MAX_SPAN_DAYS} days")
    limit = _int("limit", q.get("limit"), 50, 1, 100)
    offset = _int("offset", q.get("offset"), 0, 0, 1_000_000)
    env = _env(request, q.get("env"))
    zone, tzname = pnlsvc.resolve_tz(q.get("tz"))
    now = _now()
    lo = retsvc.local_start_utc(start, zone)
    need = (now - lo).total_seconds() / 86400.0
    days = next((d for d in an.DAYS_ALLOWED if d >= need), None)
    if days is None:
        raise _bad("start", f"must be within the last {max(an.DAYS_ALLOWED)} days")
    all_rows = await _dataset(request, env, days, "rows", None, swr=True)
    hits = retsvc.period_fills(all_rows, zone=zone, start=start, end=end, now=now)
    return {"tz_used": tzname, "env": env,
            "period": {"start_local": start.isoformat(), "end_local": end.isoformat()},
            "total": len(hits), "offset": offset, "limit": limit, "fills": hits[offset:offset + limit]}


@router.get("/analytics/scorecard")
async def analytics_scorecard(request: Request, user: dict = Depends(current_user)) -> dict:
    """Functional scorecard for a period report (services/ledger.scorecard): passes ok %, churn, fill-to-settlement
    integrity, open positions past expected settlement, alerts by type, arbitrage per set, reconciliation and the
    return labelled with its sample size. `period=<id|current|all>` or `days`. A metric the stored data cannot give
    is null and explained in `notes`. Read only; signed-in session."""
    _rate_limit(request, user, "analytics_ledger")
    q = request.query_params
    days = _int("days", q.get("days"), 7, 1, allowed=an.DAYS_ALLOWED)
    env = _env(request, q.get("env"))
    days, period, since, until, now = await _window(request, days)
    all_rows = await _dataset(request, env, days, "rows", period)
    rows = _in_window(all_rows, since, until)
    store, notes = request.app.state.store, []
    hbs, jr, alerts, audit_rows, arb_raw, snaps, flows = await _parallel(
        _soft(store.list_heartbeats(since=since, limit=HB_LIMIT), notes, "heartbeats", []),
        _soft(store.list_journal(since=since, limit=an.MAX_JOURNAL + 1, events=["quote", "cancel"],
                                 fields=("event", "ts")), notes, "journal", []),
        _soft(store.list_alerts(limit=ALERT_LIMIT), notes, "alerts", []),
        _soft(store.list_audit(limit=AUDIT_LIMIT), notes, "audit log", []),
        _read(store.list_arb_opportunities(since=since, limit=an.MAX_ARB + 1)),
        _soft(store.list_account_snapshots(since=since - SNAP_BEFORE, limit=SNAP_LIMIT), notes,
              "account snapshots", []),
        _soft(store.list_cash_flows(since=since - SNAP_BEFORE), notes, "cash flows", []))
    arbs, _t = _capped(arb_raw, an.MAX_ARB)

    def win(docs, key="ts"):
        out = []
        for d in docs or []:
            t = arbsets._ts(d.get(key)) if isinstance(d, dict) else None
            if t is not None and t >= since and (until is None or t < until):
                out.append(d)
        return out
    sets = arbsets.build_sets(arbs, [r for r in rows if r.get("strategy") == "arb"], env=env, since=since,
                              until=until, limit=arbsets.MAX_SETS * 5)
    recon = ledgersvc.reconciliation(all_rows, snaps or [], flows or [], since=since, now=now)
    label = (period or {}).get("name") or f"last {days} days"
    out = ledgersvc.scorecard(
        env=env, rows=rows, sets=sets, arb_counts=arbsets.counters(arbs, env=env, since=since, until=until),
        heartbeats=win(hbs), journal=jr or [], alerts=win(alerts), audit_rows=win(audit_rows),
        reconciliation_obj=recon, since=since, until=until, now=now, label=label,
        truncated=len(all_rows) >= an.MAX_FILLS or len(jr or []) > an.MAX_JOURNAL)
    out["notes"] = out["notes"] + notes
    return out


NOTE_MAX = 200
_FILL_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
RATE_LIMITS["trade_note"] = (30, 60.0)


def clean_note(raw) -> str:
    """One line of plain text: control / bidi characters removed, whitespace collapsed. ValueError if too long."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("note: must be a string")
    s = " ".join(_CTRL_RE.sub(" ", raw).split())
    if len(s) > NOTE_MAX:
        raise ValueError(f"note: at most {NOTE_MAX} characters")
    return s


async def _note_fill(request: Request, fill_id: str) -> dict:
    if not _FILL_ID_RE.match(fill_id or ""):
        raise HTTPException(400, ["fill_id: invalid"])
    docs = await _read(request.app.state.store.list_fills(fill_ids=[fill_id], limit=1,
                                                          fields=("fill_id", "user_note", "source")))
    if not docs:
        raise HTTPException(404, "fill_not_found")
    return docs[0]


@router.get("/trades/{fill_id}/note")
async def get_trade_note(fill_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    doc = await _note_fill(request, fill_id)
    return {"fill_id": doc["fill_id"], "note": doc.get("user_note") or ""}


@router.put("/trades/{fill_id}/note")
async def put_trade_note(fill_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Y5 journal note on one fill. A display-only annotation (no trading effect), so a signed-in session is enough
    (no fresh 2FA); rate limited per user and audited. Plain text, <= 200 chars; "" clears it."""
    from ..auth.audit import audit
    _rate_limit(request, user, "trade_note")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) - {"note"}:
        raise HTTPException(400, ["body: must be {\"note\": \"...\"}"])
    try:
        note = clean_note(body.get("note"))
    except ValueError as e:
        raise HTTPException(400, [str(e)]) from e
    await _note_fill(request, fill_id)
    try:
        await asyncio.wait_for(request.app.state.store.set_fill_fields(fill_id, {"user_note": note or None}),
                               STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e
    for _, rows in list(_state(request)["rows"].values()):     # cached trade rows carry the note: patch in place
        for r in rows:
            if r.get("fill_id") == fill_id:
                r["_note"] = note or None
    await audit(request, "trade.note", user=user, details={"fill_id": fill_id, "length": len(note)})
    return {"fill_id": fill_id, "note": note}


@router.get("/analytics/trades.csv")
async def analytics_trades_csv(request: Request, user: dict = Depends(current_user)) -> Response:
    """All matching trade rows (newest first) up to CSV_MAX_ROWS as CSV; X-Truncated: true when there were more.
    Same auth, rate-limit bucket and filters as /analytics/trades (limit/offset ignored). CSV-injection safe."""
    _rate_limit(request, user, "analytics_trades")
    days, env, flt = _filters(request)
    hits = await _hits(request, days, env, flt)
    body = an.trades_csv([an.public_trade(r) for r in hits[:an.CSV_MAX_ROWS]])
    name = f"trades-{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "X-Truncated": "true" if len(hits) > an.CSV_MAX_ROWS else "false",
                             "Cache-Control": "no-store"})

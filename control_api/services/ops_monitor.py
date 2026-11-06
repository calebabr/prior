"""Read-only ops monitor (item 19): store storage vs the Atlas M0 limit, TTL index presence, clock skew.

Everything is best-effort and bounded by a timeout; an unavailable source yields None for that part, never an error.
`ops_report(store)` returns plain data; `maybe_alert(alerter, report)` raises at most one alert per kind per
ALERT_EVERY_S through the existing `alerter.notify` (one call, no alert internals touched)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

STORAGE_WARN_FRAC = 0.80
SKEW_WARN_S = 5.0
ALERT_EVERY_S = 6 * 3600
CACHE_S = 60.0
TIMEOUT_S = 5.0

_cache: dict[int, tuple[float, dict]] = {}
_last_alert: dict[str, float] = {}


async def _soft(coro: Awaitable[Any]) -> Any:
    try:
        return await asyncio.wait_for(coro, TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None


def parse_http_date(value: str | None) -> datetime | None:
    """An HTTP Date header as aware UTC, or None."""
    try:
        d = parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def storage_report(stats: dict | None) -> dict:
    if not stats or not stats.get("limit_bytes"):
        return {"used_mb": None, "limit_mb": None, "used_frac": None, "warn": False, "available": False}
    used, limit = float(stats["used_bytes"]), float(stats["limit_bytes"])
    frac = used / limit
    return {"used_mb": round(used / 1048576, 1), "limit_mb": round(limit / 1048576, 1), "used_frac": round(frac, 4),
            "warn": frac > STORAGE_WARN_FRAC, "available": True}


def ttl_report(idx: dict | None) -> dict:
    if idx is None:
        return {"ok": None, "missing": [], "available": False}
    missing = sorted(k for k, v in idx.items() if not v)
    return {"ok": not missing, "missing": missing, "available": True}


def clock_report(server_time: datetime | None, local_now: datetime, source: str = "store") -> dict:
    """skew_s = reference clock minus this server's clock (positive: this server is behind)."""
    if server_time is None:
        return {"skew_s": None, "source": None, "warn": False, "available": False}
    skew = (server_time - local_now).total_seconds()
    return {"skew_s": round(skew, 2), "source": source, "warn": abs(skew) > SKEW_WARN_S, "available": True}


async def ops_report(store, *, ref_time: Callable[[], Awaitable[datetime | None]] | None = None,
                     now: Callable[[], datetime] | None = None, use_cache: bool = True) -> dict:
    """{storage, ttl_indexes, clock, warnings}. `ref_time` is an optional external reference (e.g. the Kalshi Date
    header); else the store's server clock is used."""
    key = id(store)
    t = time.monotonic()
    if use_cache and key in _cache and t - _cache[key][0] < CACHE_S:
        return _cache[key][1]
    stats = await _soft(store.storage_stats())
    idx = await _soft(store.ttl_index_report())
    ref, source = (await _soft(ref_time()) if ref_time else None), "reference"
    if ref is None:
        ref, source = await _soft(store.server_time()), "store"
    local = (now or (lambda: datetime.now(timezone.utc)))()
    rep = {"storage": storage_report(stats), "ttl_indexes": ttl_report(idx), "clock": clock_report(ref, local, source)}
    warnings = []
    if rep["storage"]["warn"]:
        warnings.append(f"store storage at {rep['storage']['used_frac'] * 100:.0f}% of the Atlas M0 limit")
    if rep["ttl_indexes"]["ok"] is False:
        warnings.append("missing TTL index on " + ", ".join(rep["ttl_indexes"]["missing"]))
    if rep["clock"]["warn"]:
        warnings.append(f"clock skew {rep['clock']['skew_s']:+.1f} s against the {source} clock")
    rep["warnings"] = warnings
    _cache[key] = (t, rep)
    return rep


def maybe_alert(alerter, report: dict, *, clock: Callable[[], float] = time.monotonic) -> int:
    """One `alerter.notify` per warning kind, at most every ALERT_EVERY_S. Never raises. Returns alerts sent."""
    sent = 0
    kinds = (("storage", report["storage"]["warn"], "Store storage high"),
             ("ttl", report["ttl_indexes"]["ok"] is False, "Store TTL index missing"),
             ("clock", report["clock"]["warn"], "Clock skew"))
    texts = iter(report.get("warnings", []))
    msgs = {}
    for kind, bad, _ in kinds:
        if bad:
            msgs[kind] = next(texts, "")
    for kind, bad, title in kinds:
        if not bad:
            continue
        t = clock()
        if kind in _last_alert and t - _last_alert[kind] < ALERT_EVERY_S:
            continue
        try:
            alerter.notify("system", title, msgs.get(kind, ""))
            _last_alert[kind] = t
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    return sent


def reset() -> None:
    """Tests: clear the cache and alert throttle."""
    _cache.clear()
    _last_alert.clear()

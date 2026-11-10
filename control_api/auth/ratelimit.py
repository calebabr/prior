"""Rate limits for login, 2FA, step-up and the per-user / per-IP route buckets.

Two layers, both must allow a request:

1. **In-memory sliding window** (``SlidingWindowLimiter``): one per app on ``app.state.auth_ratelimiter`` (created
   lazily), so tests that build fresh apps get fresh limiters. Always enforced, needs nothing outside the process.
2. **Store-backed shared counter** (``SharedCounter``, Sprint 3 item 2): small documents in the store's
   ``rate_limits`` collection (Atlas TTL index on ``expires_at``), so a bucket survives a restart and is shared by
   every instance on the same store. Fixed windows with the usual sliding estimate
   ``previous * (1 - elapsed / window) + current``. A store error or timeout (``SHARED_TIMEOUT_S``) FAILS OPEN to
   layer 1 alone for ``SHARED_COOLDOWN_S``: the result is never weaker than before this layer existed. Login, 2FA
   and step-up keep their existing closed behaviour on top of that: the account lockout is unchanged, and those
   routes need the store for the user read anyway (503 ``store_unavailable`` when it is down).

Over the limit is always 429 ``rate_limited`` with Retry-After, never 5xx. Stop is never rate limited and never
reaches this module. Keys are stored hashed (no raw IPs or usernames in the counter collection).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
from collections import deque
from typing import Any, Callable

from fastapi import HTTPException, Request

log = logging.getLogger("control_api.auth")

# bucket -> (max attempts, window seconds)
DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "login": (10, 300.0),
    "2fa": (10, 300.0),
    "stepup": (20, 300.0),
}
AUTH_BUCKETS = frozenset(DEFAULT_LIMITS)

_MAX_KEYS = 10_000  # memory guard (512 MB Render instance)
SHARED_TIMEOUT_S = 0.5      # one store round trip for the shared counter; slower = treated as down
SHARED_COOLDOWN_S = 30.0    # after a store failure, skip the shared layer this long (no latency added per request)


class SlidingWindowLimiter:
    def __init__(self, limits: dict[str, tuple[int, float]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.limits = dict(limits or DEFAULT_LIMITS)
        self._clock = clock
        self._hits: dict[tuple[str, str], deque] = {}

    def hit(self, bucket: str, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        limit, window = self.limits[bucket]
        now = self._clock()
        q = self._hits.get((bucket, key))
        if q is None:
            if len(self._hits) >= _MAX_KEYS:
                self._sweep(now)
            q = self._hits[(bucket, key)] = deque()
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, math.ceil(window - (now - q[0])))
        q.append(now)
        return True, 0

    def reset(self) -> None:
        self._hits.clear()

    def _sweep(self, now: float) -> None:
        for k in list(self._hits):
            window = self.limits[k[0]][1]
            q = self._hits[k]
            while q and now - q[0] >= window:
                q.popleft()
            if not q:
                del self._hits[k]
        if len(self._hits) >= _MAX_KEYS:  # still full: drop everything rather than grow
            self._hits.clear()


_KEY_SECRET: bytes = b""


def configure_key_secret(secret: "str | bytes | None") -> None:
    """Key the counter hashes with a server secret (the JWT secret, the same on every instance), so stored keys
    cannot be reversed by hashing guessed IP addresses. Unset (tests, tools): plain unkeyed hashing, as before."""
    global _KEY_SECRET
    _KEY_SECRET = secret.encode("utf-8") if isinstance(secret, str) else (secret or b"")


def counter_key(bucket: str, key: str) -> str:
    """Stable, hashed store key for (bucket, client key): the same on every instance, no raw IP / username."""
    msg = f"ratelimit\x00{bucket}\x00{key}".encode("utf-8", "replace")
    if _KEY_SECRET:
        return hmac.new(_KEY_SECRET, msg, hashlib.sha256).hexdigest()[:40]
    return hashlib.sha256(msg).hexdigest()[:40]


def sliding_estimate(cur: int, prev: int, elapsed: float, window: float) -> float:
    return prev * max(0.0, 1.0 - elapsed / window) + cur


def shared_retry_after(cur: int, prev: int, elapsed: float, window: float, limit: int) -> int:
    """Seconds until one more attempt fits under the sliding estimate (advisory; >= 1, <= 2 windows)."""
    if cur + 1 > limit:                     # cannot fit in this window: wait into the next one, where prev' = cur
        need = window * max(0.0, 1.0 - (limit - 1) / cur) if cur > 0 else 0.0
        wait = (window - elapsed) + need
    elif prev > 0:
        wait = window * (1.0 - (limit - cur - 1) / prev) - elapsed
    else:
        wait = 1.0
    return max(1, math.ceil(min(wait, 2 * window)))


class SharedCounter:
    """The store-backed layer. ``hit`` returns (allowed, retry_after); allowed is None when the store is unavailable
    (the caller keeps the in-memory decision). ``clock`` is wall time (shared across instances); tests inject one."""

    def __init__(self, clock: Callable[[], float] = time.time, timeout_s: float = SHARED_TIMEOUT_S,
                 cooldown_s: float = SHARED_COOLDOWN_S):
        self._clock = clock
        self.timeout_s = timeout_s
        self.cooldown_s = cooldown_s
        self._down_until = 0.0
        self.failures = 0

    def available(self) -> bool:
        return self._clock() >= self._down_until

    async def hit(self, store: Any, bucket: str, key: str, limit: int, window: float) -> tuple[bool | None, int]:
        now = self._clock()
        if now < self._down_until:
            return None, 0
        try:
            fn = getattr(store, "ratelimit_hit")
            idx = int(now // window)
            cur, prev = await asyncio.wait_for(fn(counter_key(bucket, key), idx, window), self.timeout_s)
            cur, prev = int(cur), int(prev)
        except Exception as e:  # noqa: BLE001 - store down / slow / unsupported: fail open to the in-memory layer
            self.failures += 1
            self._down_until = now + self.cooldown_s
            log.warning("shared rate-limit counter unavailable (%s); in-memory limits only for %ds",
                        type(e).__name__, int(self.cooldown_s))
            return None, 0
        elapsed = now - idx * window
        if sliding_estimate(cur, prev, elapsed, window) <= limit:
            return True, 0
        return False, shared_retry_after(cur, prev, elapsed, window, limit)


def get_limiter(request: Request) -> SlidingWindowLimiter:
    state = request.app.state
    lim = getattr(state, "auth_ratelimiter", None)
    if lim is None:
        lim = SlidingWindowLimiter()
        state.auth_ratelimiter = lim
    return lim


def get_shared(request: Request) -> SharedCounter:
    state = request.app.state
    sc = getattr(state, "auth_shared_ratelimiter", None)
    if sc is None:
        sc = SharedCounter()
        state.auth_shared_ratelimiter = sc
    return sc


def client_ip(request: Request) -> str:
    """LAST X-Forwarded-For hop, else the peer address.

    The client controls everything before the last hop; Render's proxy (and Caddy on the VM path)
    appends the real peer address at the end, so only the last hop can be trusted (SECURITY_REVIEW S-1)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        last = xff.split(",")[-1].strip()
        if last:
            return last[:64]
    return request.client.host if request.client else "unknown"


def _limited(retry: int) -> HTTPException:
    return HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": str(max(1, int(retry)))})


async def check(request: Request, bucket: str, key: str, limit: tuple[int, float] | None = None,
                local_only: bool = False) -> None:
    """Count one attempt for ``key`` in ``bucket``; raise 429 ``rate_limited`` (Retry-After) if either layer is over.

    ``limit`` registers the bucket's (max, window) the first time it is seen (the route's own constant).
    ``local_only`` skips the shared store counter (for a safety action such as Disarm that must never wait on the store)."""
    lim = get_limiter(request)
    if limit is not None:
        lim.limits.setdefault(bucket, limit)
    ok, retry = lim.hit(bucket, key)
    if not ok:                      # refused locally: no store write (a flood never reaches Atlas)
        raise _limited(retry)
    if local_only:
        return
    n, window = lim.limits[bucket]
    shared_ok, retry = await get_shared(request).hit(getattr(request.app.state, "store", None), bucket, key,
                                                     int(n), float(window))
    if shared_ok is False:
        raise _limited(retry)


async def check_user(request: Request, user: dict, bucket: str, limit: tuple[int, float],
                     local_only: bool = False) -> None:
    """Per-user bucket (the authenticated username), as the route helpers use."""
    await check(request, bucket, "user:" + str(user.get("username") or "?"), limit, local_only=local_only)


async def enforce(request: Request, bucket: str) -> None:
    """Per-IP bucket: raise 429 ``rate_limited`` (with Retry-After) if this IP exceeded ``bucket``."""
    await check(request, bucket, client_ip(request))

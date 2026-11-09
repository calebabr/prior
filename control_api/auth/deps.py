"""FastAPI dependencies: ``current_user`` and ``require_fresh_2fa``, plus lockout helpers.

``current_user`` is deliberately store-light so Stop keeps working when Atlas is down:
it verifies the JWT locally, checks an in-process revocation set, and then asks the store
``is_revoked`` with a short timeout -- if the store errors it FAILS OPEN on the revocation
check only (signature/expiry/typ are always enforced; access tokens live <= 60 min).

Session epoch (SECURITY_REVIEW S-15): the user doc may carry ``sessions_valid_after`` (set by
bootstrap on any password/2FA reset or ``--revoke-sessions``, and by ``POST
/api/auth/logout-all``). A token is only valid if its ``iat`` is strictly greater than that
instant. The value is read from the user doc (short timeout, small per-process cache on
``app.state``). If the store is unreachable:

* **Stop** (``POST /api/bot/stop`` or the explicit ``current_user_stop`` dependency) FAILS
  OPEN: it checks against the last-known value cached in memory (or nothing, if none was
  ever read), so Stop never depends on Atlas;
* every other endpoint FAILS CLOSED with 503 ``store_unavailable``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request

from . import security as sec
from .audit import audit
from .ratelimit import enforce

log = logging.getLogger("control_api.auth")

STORE_TIMEOUT_S = 5.0
REVOCATION_TIMEOUT_S = 3.0
SESSIONS_TIMEOUT_S = 3.0
# Stop's two store reads (revocation + session epoch, run concurrently) are cut at this, then fail open, so a slow or
# black-holed Atlas delays Stop's flags by at most this much (Sprint 3 item 1; was the 3 s timeouts above).
STOP_STORE_TIMEOUT_S = 0.5
SESSIONS_CACHE_TTL_S = 10.0     # reuse a fresh read this long (override: app.state.auth_sessions_ttl_s)
SESSIONS_FIELD = "sessions_valid_after"
TWOFA_HEADER = "X-2FA-Code"
# Endpoints whose session-epoch check fails OPEN when the store is down (Stop only).
FAIL_OPEN_ROUTES = frozenset({("POST", "/api/bot/stop")})


# ---- per-app in-process state (on app.state, never module globals) -----------------------
def _local_revoked(request: Request) -> dict[str, datetime]:
    st = request.app.state
    d = getattr(st, "auth_revoked", None)
    if d is None:
        d = {}
        st.auth_revoked = d
    return d


def remember_revoked(request: Request, jti: str, exp: datetime) -> None:
    d = _local_revoked(request)
    now = sec.utcnow()
    for k in [k for k, v in d.items() if v < now]:  # prune expired
        d.pop(k, None)
    d[jti] = exp


def _sessions_cache(request: Request) -> dict[str, tuple[float, float]]:
    """username -> (sessions_valid_after as epoch seconds, monotonic time of last store read)."""
    st = request.app.state
    d = getattr(st, "auth_sessions_cache", None)
    if d is None:
        d = {}
        st.auth_sessions_cache = d
    return d


def sva_epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        return sec.as_utc(value).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError("bad sessions_valid_after")


def remember_sessions_valid_after(request: Request, username: str, value: Any,
                                  *, fresh: bool = True) -> float:
    """Record a value read from (or just written to) the store. Never moves backwards, so a
    stale replica / failed write can't resurrect sessions this process already killed."""
    cache = _sessions_cache(request)
    old_val, old_t = cache.get(username, (0.0, float("-inf")))
    val = max(old_val, sva_epoch(value))
    cache[username] = (val, time.monotonic() if fresh else old_t)
    return val


def _cached_sva(request: Request, username: str) -> tuple[float, float] | None:
    return _sessions_cache(request).get(username)


def _is_fail_open(request: Request) -> bool:
    method = request.method.upper()
    route = request.scope.get("route")
    candidates = {request.url.path, request.scope.get("path") or "",
                  getattr(route, "path", "") or ""}   # route template survives a root_path mount
    return any((method, (p.rstrip("/") or "/")) in FAIL_OPEN_ROUTES for p in candidates if p)


def token_predates_sessions(iat: Any, sva_epoch: float) -> bool:
    """True if a token issued at ``iat`` is invalidated by the session epoch.

    ``iat`` is whole seconds, so a token minted in the same second as the reset is rejected
    too (strictly-greater rule): fail-safe, the owner just logs in again a second later."""
    try:
        return int(iat) <= sva_epoch if sva_epoch > 0 else False
    except (TypeError, ValueError):
        return True


async def sessions_valid_after(request: Request, username: str, *, fail_open: bool,
                               timeout: float = SESSIONS_TIMEOUT_S) -> float:
    """Current session epoch for ``username`` (epoch seconds; 0 = no constraint).

    Store failure: ``fail_open`` -> last-known cached value (0 if never read); otherwise 503.
    Unknown user: fail-closed callers get 401; Stop treats it as no constraint (Stop only
    ever makes things safer)."""
    ttl = float(getattr(request.app.state, "auth_sessions_ttl_s", SESSIONS_CACHE_TTL_S))
    cached = _cached_sva(request, username)
    if cached is not None and time.monotonic() - cached[1] < ttl:
        return cached[0]
    try:
        doc = await asyncio.wait_for(request.app.state.store.get_user(username), timeout)
    except Exception as e:
        if fail_open:
            log.warning("session-epoch check unavailable (%s); Stop uses last-known value",
                        type(e).__name__)
            return cached[0] if cached else 0.0
        log.warning("session-epoch check unavailable (%s); failing closed", type(e).__name__)
        raise HTTPException(status_code=503, detail="store_unavailable") from None
    if not doc:
        if fail_open:
            return cached[0] if cached else 0.0
        raise _unauthorized("invalid_token")
    try:
        return remember_sessions_valid_after(request, username, doc.get(SESSIONS_FIELD))
    except ValueError:
        if fail_open:
            return cached[0] if cached else 0.0
        log.error("user doc has an unreadable %s; failing closed", SESSIONS_FIELD)
        raise HTTPException(status_code=503, detail="store_unavailable") from None


def _totp_lock(request: Request) -> asyncio.Lock:
    st = request.app.state
    lock = getattr(st, "auth_totp_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        st.auth_totp_lock = lock
    return lock


def _unauthorized(detail: str = "not_authenticated") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


# ---- store helpers -----------------------------------------------------------------------
async def store_call(coro: Any, timeout: float = STORE_TIMEOUT_S) -> Any:
    """Await a store coroutine; map any failure to 503 ``store_unavailable``."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("auth store call failed: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="store_unavailable") from None


async def token_revoked(request: Request, jti: str, timeout: float = REVOCATION_TIMEOUT_S) -> bool:
    exp = _local_revoked(request).get(jti)
    if exp is not None and exp >= sec.utcnow():
        return True
    try:
        return bool(await asyncio.wait_for(request.app.state.store.is_revoked(jti), timeout))
    except Exception as e:
        log.warning("revocation check unavailable (%s); allowing valid token", type(e).__name__)
        return False


def lock_remaining_s(user: dict) -> int:
    """Seconds left on the account lock (0 if not locked)."""
    until = sec.as_utc(user.get("locked_until"))
    if until is None:
        return 0
    left = (until - sec.utcnow()).total_seconds()
    return max(0, math.ceil(left))


def locked_exception(seconds: int) -> HTTPException:
    return HTTPException(status_code=429, detail="account_locked",
                         headers={"Retry-After": str(max(1, seconds))})


async def register_failure(request: Request, user: dict, reason: str) -> bool:
    """Count one consecutive failure; lock for 15 min at 5. Returns True if now locked.

    Field-level, atomic writes only ($inc / $set of the counter fields): never writes back the
    rest of a previously read doc, which could roll back ``sessions_valid_after`` (S-18)."""
    store = request.app.state.store
    username = user["username"]
    now = sec.utcnow()
    failures = int(user.get("failed_logins") or 0) + 1      # fallback if the store is down
    try:
        doc = await asyncio.wait_for(
            store.update_user_fields(username, inc={"failed_logins": 1},
                                     set={"updated_utc": now}), STORE_TIMEOUT_S)
        if doc is not None:
            failures = int(doc.get("failed_logins") or 0)
    except Exception as e:
        log.warning("could not persist failed-login counter: %s", type(e).__name__)
    locked = failures >= sec.MAX_FAILED_LOGINS
    user["failed_logins"] = 0 if locked else failures
    if locked:
        user["locked_until"] = now + timedelta(seconds=sec.LOCKOUT_SECONDS)
        try:
            await asyncio.wait_for(
                store.update_user_fields(username, set={"failed_logins": 0,
                                                        "locked_until": user["locked_until"],
                                                        "updated_utc": now}), STORE_TIMEOUT_S)
        except Exception as e:
            log.warning("could not persist account lock: %s", type(e).__name__)
        await audit(request, "auth.lockout", ok=False, user=user,
                    details={"reason": reason, "minutes": sec.LOCKOUT_SECONDS // 60})
    return locked


async def verify_and_consume_totp(request: Request, user: dict, code: Any) -> int | None:
    """Verify ``code`` against the user's secret and atomically (per process) consume its time
    step. On success persists ``last_totp_step`` (monotonic $max in the store), clears
    failures/lock and returns the step. Writes only those fields (S-18)."""
    settings = request.app.state.settings
    store = request.app.state.store
    async with _totp_lock(request):
        # Re-read under the lock so concurrent requests can't both use one step.
        fresh = await store_call(store.get_user(user["username"]))
        if fresh:
            user.clear()
            user.update(fresh)
        enc = user.get("totp_secret_enc")
        if not enc:
            return None
        try:
            secret = sec.decrypt_totp_secret(settings, enc)
        except RuntimeError as e:
            log.error("TOTP secret unavailable: %s", e)
            raise HTTPException(status_code=500, detail="2fa_misconfigured") from None
        step = sec.verify_totp(secret, code, last_step=user.get("last_totp_step"))
        if step is None:
            return None
        updated = await store_call(store.update_user_fields(
            user["username"],
            set={"failed_logins": 0, "locked_until": None, "updated_utc": sec.utcnow()},
            max={"last_totp_step": step}))
        if updated is None:            # user vanished between read and write
            return None
        user.clear()
        user.update(updated)
        return step


# ---- dependencies ------------------------------------------------------------------------
async def _authenticate(request: Request, *, fail_open: bool) -> dict:
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("not_authenticated")
    try:
        claims = sec.decode_token(request.app.state.settings, token.strip(), sec.TYP_ACCESS)
    except sec.TokenError:
        raise _unauthorized("invalid_token") from None
    # Both store reads run concurrently so Stop's worst case is one timeout, not two; for Stop (fail_open) that one
    # timeout is STOP_STORE_TIMEOUT_S, so the flags never wait seconds on a slow store.
    revoked, sva = await asyncio.gather(
        token_revoked(request, claims["jti"], STOP_STORE_TIMEOUT_S if fail_open else REVOCATION_TIMEOUT_S),
        sessions_valid_after(request, claims["sub"], fail_open=fail_open,
                             timeout=STOP_STORE_TIMEOUT_S if fail_open else SESSIONS_TIMEOUT_S),
    )
    if revoked:
        raise _unauthorized("token_revoked")
    if token_predates_sessions(claims["iat"], sva):
        raise _unauthorized("session_expired")
    user = {"username": claims["sub"], "jti": claims["jti"], "exp": sec.exp_datetime(claims),
            "iat": int(claims["iat"])}
    request.state.user = user
    return user


async def current_user(request: Request) -> dict:
    """Valid, unrevoked Bearer access token issued after the session epoch -> ``{"username",
    "jti", "exp", "iat"}``; else 401. No 2FA.

    The session-epoch check fails closed (503) if the store is down, EXCEPT on
    ``POST /api/bot/stop`` where it fails open to the last-known value."""
    return await _authenticate(request, fail_open=_is_fail_open(request))


async def current_user_stop(request: Request) -> dict:
    """Explicit Stop dependency: like ``current_user`` but the session-epoch check always
    fails open to the last-known cached value, so Stop never depends on the store."""
    return await _authenticate(request, fail_open=True)


async def require_fresh_2fa(request: Request) -> dict:
    """``current_user`` + a valid, never-before-used TOTP code in the ``X-2FA-Code`` header.

    403 ``2fa_required`` if missing, 403 ``2fa_invalid`` if wrong/reused (counts toward
    lockout), 429 ``account_locked`` while locked, 429 ``rate_limited`` per IP."""
    user = await current_user(request)
    code = request.headers.get(TWOFA_HEADER)
    if code is None or not code.strip():
        raise HTTPException(status_code=403, detail="2fa_required")
    await enforce(request, "stepup")
    doc = await store_call(request.app.state.store.get_user(user["username"]))
    if not doc:
        raise _unauthorized("invalid_token")
    left = lock_remaining_s(doc)
    if left:
        await audit(request, "auth.stepup", ok=False, user=user, details={"reason": "locked"})
        raise locked_exception(left)
    step = await verify_and_consume_totp(request, doc, code)
    if step is None:
        locked = await register_failure(request, doc, "stepup")
        await audit(request, "auth.stepup", ok=False, user=user,
                    details={"reason": "bad_code", "path": request.url.path})
        if locked:
            raise locked_exception(sec.LOCKOUT_SECONDS)
        raise HTTPException(status_code=403, detail="2fa_invalid")
    await audit(request, "auth.stepup", ok=True, user=user,
                details={"path": request.url.path, "method": request.method})
    request.state.fresh_2fa = True
    return user

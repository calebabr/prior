"""Auth endpoints (prefix ``/api/auth``): login -> 2fa -> access token; logout; me.

Single user, no registration (the owner is created with ``python -m control_api.auth.bootstrap``).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import security as sec
from .audit import audit
from .deps import (SESSIONS_FIELD, sva_epoch, current_user, lock_remaining_s, locked_exception,
                   register_failure, remember_revoked, remember_sessions_valid_after,
                   require_fresh_2fa, store_call, token_predates_sessions, token_revoked,
                   verify_and_consume_totp)
from .ratelimit import enforce

log = logging.getLogger("control_api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

_BAD_CREDENTIALS = "invalid_credentials"   # same for unknown user and wrong password


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class TwoFAIn(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=2048)
    code: str = Field(min_length=1, max_length=16)


@router.post("/login")
async def login(body: LoginIn, request: Request) -> dict:
    await enforce(request, "login")
    store = request.app.state.store
    user = await store_call(store.get_user(body.username))
    if not user:
        await sec.verify_password_async(None, body.password)  # equalize timing (B-3: off the event loop)
        await audit(request, "auth.login", ok=False, details={"reason": "bad_credentials"})
        raise HTTPException(status_code=401, detail=_BAD_CREDENTIALS)

    left = lock_remaining_s(user)
    if left:
        await audit(request, "auth.login", ok=False, user=user, details={"reason": "locked"})
        raise locked_exception(left)

    if not await sec.verify_password_async(user.get("password_hash"), body.password):
        # Wrong passwords are throttled per IP (enforce above) but do NOT count toward the account
        # lockout: otherwise anyone who knows the username could keep the owner locked out of the
        # app -- and out of Stop (SECURITY_REVIEW S-2). The lockout counts TOTP failures, which
        # require the correct password first.
        await audit(request, "auth.login", ok=False, user=user,
                    details={"reason": "bad_credentials"})
        raise HTTPException(status_code=401, detail=_BAD_CREDENTIALS)

    if not user.get("totp_secret_enc"):
        await audit(request, "auth.login", ok=False, user=user,
                    details={"reason": "2fa_not_enrolled"})
        raise HTTPException(status_code=403, detail="2fa_not_enrolled")

    # NB: failures are NOT reset here -- only a correct TOTP resets them, so knowing the
    # password alone can't be used to keep brute-forcing codes.
    if sec.password_needs_rehash(user["password_hash"]):
        try:
            await store.update_user_fields(user["username"], set={
                "password_hash": await sec.hash_password_async(body.password), "updated_utc": sec.utcnow()})
        except Exception as e:
            log.warning("password rehash not saved: %s", type(e).__name__)

    token, _ = sec.create_token(request.app.state.settings, user["username"], sec.TYP_MFA,
                                sec.MFA_TOKEN_TTL_S)
    await audit(request, "auth.login", ok=True, user=user, details={"stage": "password"})
    return {"mfa_token": token, "expires_in": sec.MFA_TOKEN_TTL_S}


@router.post("/2fa")
async def two_factor(body: TwoFAIn, request: Request) -> dict:
    await enforce(request, "2fa")
    settings = request.app.state.settings
    store = request.app.state.store
    try:
        claims = sec.decode_token(settings, body.mfa_token, sec.TYP_MFA)
    except sec.TokenError:
        await audit(request, "auth.2fa", ok=False, details={"reason": "invalid_mfa_token"})
        raise HTTPException(status_code=401, detail="invalid_mfa_token") from None
    if await token_revoked(request, claims["jti"]):
        await audit(request, "auth.2fa", ok=False, user=claims["sub"],
                    details={"reason": "mfa_token_used"})
        raise HTTPException(status_code=401, detail="invalid_mfa_token")

    user = await store_call(store.get_user(claims["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="invalid_mfa_token")
    try:
        sva = sva_epoch(user.get(SESSIONS_FIELD))
    except ValueError:
        raise HTTPException(status_code=503, detail="store_unavailable") from None
    if token_predates_sessions(claims["iat"], sva):   # mfa token issued before a reset
        await audit(request, "auth.2fa", ok=False, user=user,
                    details={"reason": "session_expired"})
        raise HTTPException(status_code=401, detail="invalid_mfa_token")
    left = lock_remaining_s(user)
    if left:
        await audit(request, "auth.2fa", ok=False, user=user, details={"reason": "locked"})
        raise locked_exception(left)

    step = await verify_and_consume_totp(request, user, body.code)
    if step is None:
        locked = await register_failure(request, user, "2fa")
        await audit(request, "auth.2fa", ok=False, user=user, details={"reason": "bad_code"})
        if locked:
            raise locked_exception(sec.LOCKOUT_SECONDS)
        raise HTTPException(status_code=401, detail="2fa_invalid")

    # The mfa token is single-use.
    exp = sec.exp_datetime(claims)
    remember_revoked(request, claims["jti"], exp)
    try:
        await store.revoke_jti(claims["jti"], exp)
    except Exception as e:
        log.warning("could not persist mfa-token revocation: %s", type(e).__name__)

    try:   # field-level write only: never write back the stale doc (S-18)
        await store.update_user_fields(user["username"], set={"last_login_utc": sec.utcnow()})
    except Exception as e:
        log.warning("last_login_utc not saved: %s", type(e).__name__)

    token, _ = sec.create_token(settings, user["username"], sec.TYP_ACCESS,
                                sec.ACCESS_TOKEN_TTL_S)
    await audit(request, "auth.2fa", ok=True, user=user, details={"stage": "totp"})
    return {"access_token": token, "token_type": "bearer", "expires_in": sec.ACCESS_TOKEN_TTL_S}


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(current_user)) -> dict:
    remember_revoked(request, user["jti"], user["exp"])  # effective immediately in-process
    persisted = True
    try:
        await store_call(request.app.state.store.revoke_jti(user["jti"], user["exp"]))
    except HTTPException:
        persisted = False
    await audit(request, "auth.logout", ok=True, user=user,
                details=None if persisted else {"revocation_persisted": False})
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    """Invalidate every token issued so far (this one included) by moving the session epoch
    to now. Needs fresh 2FA. Fails closed: 503 if the epoch can't be persisted (this process
    still rejects the old tokens, but other/restarted instances would not)."""
    now = sec.utcnow()
    remember_revoked(request, user["jti"], user["exp"])
    try:
        await store_call(request.app.state.store.save_user(
            {"username": user["username"], SESSIONS_FIELD: now, "updated_utc": now}))
    except HTTPException:
        remember_sessions_valid_after(request, user["username"], now, fresh=False)
        await audit(request, "auth.logout_all", ok=False, user=user,
                    details={"reason": "store_unavailable"})
        raise
    remember_sessions_valid_after(request, user["username"], now)
    # S-33: a push subscription registered with a revoked token must not keep receiving alerts.
    # Best effort: the session revocation above already succeeded, so a store hiccup here is only reported.
    try:
        cleared = await store_call(request.app.state.store.clear_push_subscriptions())
        push_details = {"push_cleared": int(cleared or 0)}
    except Exception:  # noqa: BLE001
        push_details = {"push_cleared": None, "push_clear_failed": True}
    await audit(request, "auth.logout_all", ok=True, user=user, details=push_details)
    return {"ok": True, "sessions_valid_after": now.isoformat()}


@router.get("/me")
async def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"]}

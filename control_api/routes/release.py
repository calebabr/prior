"""Pending releases (decision B2): the release workflow registers a release and polls it; the owner approves it
in the app with fresh 2FA (agents have no 2FA, so they can never approve) or rejects it.

* ``POST /api/release/pending``          X-Release-Token   register {version, commit, summary, ci,
                                                            trading_changes?: bool = false} -> {id, status}
* ``GET  /api/release/pending/{id}``     X-Release-Token   -> {status} (pending/approved/rejected/expired)
* ``GET  /api/release/pending``          login             -> {release: {...} | null} (latest pending, unexpired)
* ``POST /api/release/pending/{id}/approve``  fresh 2FA    -> {id, status}
* ``POST /api/release/pending/{id}/reject``   login        -> {id, status}

While env ``RELEASE_TOKEN`` is unset the token paths answer 404 (before any body parsing), so the feature is
invisible to the workflow side; the app paths authenticate first (401 / 2FA like every app route), then answer 404.
Token compares are
constant-time, inputs are size-limited, every write is audited, and nothing secret is returned or logged.
"""
from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa, store_call
from ..auth.ratelimit import enforce, get_limiter

log = logging.getLogger("control_api.release")
router = APIRouter(prefix="/api/release")

BUCKET = "release_token"
LIMIT = (60, 300.0)            # the workflow polls about every 10-15 s for up to 30 min from one runner IP
MAX_TOKEN_LEN = 256
MAX_BODY_BYTES = 8192
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_PUBLIC = ("id", "version", "commit", "summary", "ci", "trading_changes", "status", "created_at", "expires_at", "by",
           "at")


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
    commit: str = Field(min_length=7, max_length=40, pattern=r"^[0-9a-f]{7,40}$")
    summary: str = Field(default="", max_length=3000)
    ci: str = Field(default="", max_length=500)
    # S3 follow-up (a): true when the release changes trading behaviour (sizing, caps, quoting, orders, reliability
    # profile). Recorded in the register and approve audit rows, which the Periods page markers read.
    trading_changes: bool = Field(default=False, strict=True)


def _enabled(request: Request) -> str:
    tok = getattr(request.app.state.settings, "release_token", None)
    if not tok:
        raise HTTPException(status_code=404, detail="Not Found")
    return tok


def _require_token(request: Request, given: str | None) -> None:
    expected = _enabled(request)
    get_limiter(request).limits.setdefault(BUCKET, LIMIT)
    enforce(request, BUCKET)
    if not given or len(given) > MAX_TOKEN_LEN or not hmac.compare_digest(
            given.encode("utf-8", "replace"), expected.encode("utf-8")):
        log.warning("release endpoint: bad or missing release token")
        raise HTTPException(status_code=401, detail="unauthorized")


def _aware(v: Any) -> datetime | None:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _status(doc: dict, now: datetime | None = None) -> str:
    st = str(doc.get("status") or "pending")
    exp = _aware(doc.get("expires_at"))
    if st == "pending" and exp is not None and exp <= (now or datetime.now(timezone.utc)):
        return "expired"
    return st if st in ("pending", "approved", "rejected", "expired") else "expired"


def _public(doc: dict) -> dict:
    out = {k: doc.get(k) for k in _PUBLIC if k in doc}
    for k in ("created_at", "expires_at", "at"):
        d = _aware(out.get(k))
        if d is not None:
            out[k] = d.isoformat()
    out["status"] = _status(doc)
    return out


@router.post("/pending")
async def register(request: Request, x_release_token: str | None = Header(default=None)) -> dict:
    _require_token(request, x_release_token)          # before the body is parsed: no 422 leaks the route
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body_too_large")
    try:
        body = ReleaseIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=[{"loc": list(x.get("loc", ())), "msg": x.get("msg")}
                                                     for x in e.errors()[:10]]) from None
    doc = await store_call(request.app.state.store.save_pending_release(body.model_dump()))
    await audit(request, "release.register", user="release-workflow",
                details={"id": doc.get("id"), "version": body.version, "commit": body.commit, "ci": body.ci[:64],
                         "trading_changes": body.trading_changes})
    return {"id": doc["id"], "status": _status(doc)}


@router.get("/pending/{release_id}")
async def poll(request: Request, release_id: str = Path(pattern=ID_PATTERN),
               x_release_token: str | None = Header(default=None)) -> dict:
    _require_token(request, x_release_token)
    doc = await store_call(request.app.state.store.get_pending_release(release_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="release_not_found")
    return {"status": _status(doc)}


@router.get("/pending")
async def latest(request: Request, user: dict = Depends(current_user)) -> dict:
    _enabled(request)
    doc = await store_call(request.app.state.store.latest_pending_release())
    return {"release": _public(doc) if doc and _status(doc) == "pending" else None}


async def _resolve(request: Request, release_id: str, decision: str, user: dict) -> dict:
    store = request.app.state.store
    doc = await store_call(store.resolve_pending_release(release_id, decision, by=str(user.get("username"))[:64]))
    if doc is None:
        cur = await store_call(store.get_pending_release(release_id))
        await audit(request, f"release.{'approve' if decision == 'approved' else 'reject'}", ok=False, user=user,
                    details={"id": release_id, "reason": "not_found" if cur is None else _status(cur)})
        if cur is None:
            raise HTTPException(status_code=404, detail="release_not_found")
        raise HTTPException(status_code=409, detail="release_not_pending")
    await audit(request, f"release.{'approve' if decision == 'approved' else 'reject'}", ok=True, user=user,
                details={"id": release_id, "version": doc.get("version"), "commit": doc.get("commit"),
                         "trading_changes": doc.get("trading_changes") is True})
    return {"id": doc["id"], "status": _status(doc)}


@router.post("/pending/{release_id}/approve")
async def approve(request: Request, release_id: str = Path(pattern=ID_PATTERN),
                  user: dict = Depends(require_fresh_2fa)) -> dict:
    _enabled(request)
    return await _resolve(request, release_id, "approved", user)


@router.post("/pending/{release_id}/reject")
async def reject(request: Request, release_id: str = Path(pattern=ID_PATTERN),
                 user: dict = Depends(current_user)) -> dict:
    _enabled(request)
    return await _resolve(request, release_id, "rejected", user)

"""Loss / drawdown circuit breaker (Sprint 3 L1): GET/PUT /api/breaker.

GET (signed in): the settings, their schema and API ceilings, and the live state (tripped? which limit? the
high-water mark, the day's starting equity and the day's loss so far, per mode). Memory only: no store or Kalshi read.

PUT (fresh 2FA): a strict partial update of {live_enabled, demo_enabled, drawdown_frac, day_loss_frac}. Every
problem (bad JSON, unknown key, wrong type, outside the API ceilings) is 400 with detail = [messages]. Audited as
`breaker.set` with before/after and which fields were loosened (a limit raised or a mode switched off). The block is
not writable through PUT /api/cloud. Clearing a trip is not done here: Start (or arbitrage enable) with fresh 2FA and
`ack_breaker: true` does that.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..services import breaker as breakersvc

router = APIRouter(prefix="/api")


def breaker_view(request: Request) -> dict:
    r = request.app.state.runner
    return {"settings": r.breaker_settings(), "schema": breakersvc.SCHEMA,
            "ceilings": {k: {"min": lo, "max": hi} for k, (lo, hi) in breakersvc.CEILINGS.items()},
            "defaults": dict(breakersvc.DEFAULTS), "state": r.breaker_status(full=True)}


@router.get("/breaker")
async def get_breaker(request: Request, user: dict = Depends(current_user)) -> dict:
    return breaker_view(request)


@router.put("/breaker")
async def put_breaker(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    r = request.app.state.runner
    by = user.get("username", "user")
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    current = r.breaker_settings()
    try:
        new, loosened = breakersvc.validate_update(current, body)
    except ValueError as e:
        errs = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
        await audit(request, "breaker.set", ok=False, user=user, details={"errors": errs})
        raise HTTPException(400, errs) from e
    before = {k: current.get(k) for k in breakersvc.FIELDS}
    if new == before:
        return breaker_view(request)
    doc = {**new, "updated": datetime.now(timezone.utc).isoformat(), "by": str(by)[:64]}
    try:
        await request.app.state.store.save_cloud_settings({breakersvc.CLOUD_SETTINGS: doc}, by=by)
    except Exception as e:  # noqa: BLE001
        await audit(request, "breaker.set", ok=False, user=user, details={"error": "store"})
        raise HTTPException(503, "store_unavailable") from e
    if isinstance(r.cloud, dict):
        r.cloud[breakersvc.CLOUD_SETTINGS] = doc
    await audit(request, "breaker.set", user=user, details={"from": before, "to": new, "loosened": loosened})
    return breaker_view(request)

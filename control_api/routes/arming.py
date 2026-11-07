"""Per-strategy LIVE arming (Sprint 2B part 3, item 10; services/arming.py).

* ``GET /api/arming`` (signed in) -> {maker: {armed, mode}, arbitrage: {armed, mode}, live_allowed, updated}
* ``PUT /api/arming`` {strategy: "maker"|"arbitrage", armed: bool}
  - ``armed: true`` needs fresh 2FA (the same step-up guard Start uses), the live gates (409 with the reason) and
    the go-live preflight ready for target live (409 with the failing checks as a detail list).
  - ``armed: false`` needs only a signed-in session: disarming only removes risk. It is in force at once and cancels
    the maker's resting LIVE orders through Stop's cancel path.

Every PUT is audited (``arming.set``). Stop never goes through here and never depends on any of it.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..auth.ratelimit import get_limiter
from ..runner import RunnerDisabled
from ..services import arming as armsvc
from ..services import preflight as pf

router = APIRouter(prefix="/api")
BUCKET = "arming"
LIMIT = (10, 60.0)            # an arm runs the whole preflight (one authenticated Kalshi read)
MAX_BODY_BYTES = 1024


class ArmingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["maker", "arbitrage"]
    armed: bool = Field(strict=True)


def _rate_limit(request: Request, user: dict) -> None:
    lim = get_limiter(request)
    lim.limits.setdefault(BUCKET, LIMIT)
    ok, retry = lim.hit(BUCKET, "user:" + str(user.get("username") or "?"))
    if not ok:
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(retry)})


@router.get("/arming")
async def get_arming(request: Request, user: dict = Depends(current_user)) -> dict:
    return armsvc.view(request.app.state.runner)


@router.put("/arming")
async def put_arming(request: Request, user: dict = Depends(current_user)) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "body_too_large")
    try:
        body = ArmingIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        raise HTTPException(422, [{"loc": list(x.get("loc", ())), "msg": x.get("msg")}
                                  for x in e.errors()[:10]]) from None
    r = request.app.state.runner
    by = str(user.get("username") or "user")
    details = {"strategy": body.strategy, "armed": body.armed}
    if not body.armed:
        res = await r.set_armed(body.strategy, False, by=by)
        await audit(request, "arming.set", ok=True, user=user,
                    details={**details, "persisted": res["persisted"], "cancelled": res["cancelled"]})
        return {**armsvc.view(r), "result": res}
    user = await require_fresh_2fa(request)            # arming: the same step-up guard Start uses
    _rate_limit(request, user)
    if not r.live_allowed():
        why = r.live_block_reason() or "live_not_allowed"
        await audit(request, "arming.set", ok=False, user=user, details={**details, "error": why})
        raise HTTPException(409, why)
    app = request.app
    pre = await pf.run_preflight("live", runner=r, store=app.state.store, settings=app.state.settings,
                                 app_state=app.state)
    if not pre.get("ready"):
        failed = armsvc.failing_checks(pre)
        await audit(request, "arming.set", ok=False, user=user,
                    details={**details, "error": "preflight_not_ready", "failed": [f["id"] for f in failed]})
        raise HTTPException(409, failed)
    try:
        res = await r.set_armed(body.strategy, True, by=by)
    except (PermissionError, RunnerDisabled) as e:
        await audit(request, "arming.set", ok=False, user=user, details={**details, "error": str(e)})
        raise HTTPException(409, str(e)) from e
    except Exception as e:  # noqa: BLE001   (fail-safe: no store write -> not armed)
        await audit(request, "arming.set", ok=False, user=user, details={**details, "error": type(e).__name__})
        raise HTTPException(503, "store_unavailable") from e
    await audit(request, "arming.set", ok=True, user=user, details=details)
    return {**armsvc.view(r), "result": res}

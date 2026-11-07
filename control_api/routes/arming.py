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
DISARM_BUCKET = "disarm"      # I-1: generous, its own bucket (Stop has none and never shares this one)
DISARM_LIMIT = (30, 60.0)
# L-1: checks not re-run when live mode is switched on or Start is pressed in live. Start itself removes the maker
# kill file, and a stopped bot writes no fresh snapshot; both were checked when the strategy was armed.
RECHECK_EXEMPT = ("kill_switch", "snapshot")
MAX_BODY_BYTES = 1024


class ArmingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["maker", "arbitrage"]
    armed: bool = Field(strict=True)


def _rate_limit(request: Request, user: dict, bucket: str = BUCKET, limit: tuple = LIMIT) -> None:
    lim = get_limiter(request)
    lim.limits.setdefault(bucket, limit)
    ok, retry = lim.hit(bucket, "user:" + str(user.get("username") or "?"))
    if not ok:
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(retry)})


async def live_recheck(request: Request, *, user: dict, action: str, details: dict | None = None) -> None:
    """L-1 (security review 2B): re-run the go-live preflight (target live) when live mode is switched on or Start
    is pressed in live, so an arm from days ago cannot start live trading on checks that now fail. Raises 409 with
    the failing checks (RECHECK_EXEMPT ignored). Never used by Stop or disarm."""
    app = request.app
    pre = await pf.run_preflight("live", runner=app.state.runner, store=app.state.store,
                                 settings=app.state.settings, app_state=app.state)
    checks = pre.get("checks") if isinstance(pre, dict) else None
    failing = [c for c in checks or [] if isinstance(c, dict) and c.get("status") == pf.FAIL
               and c.get("id") not in RECHECK_EXEMPT]
    if failing or not checks:                    # no checks at all: fail closed
        failed = armsvc.failing_checks({"checks": failing})
        await audit(request, action, ok=False, user=user,
                    details={**(details or {}), "error": "preflight_not_ready", "failed": [f["id"] for f in failed]})
        raise HTTPException(409, failed)


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
        _rate_limit(request, user, DISARM_BUCKET, DISARM_LIMIT)
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
    gen = r.disarm_generation(body.strategy)     # M-1: before the preflight; any later disarm refuses this arm
    app = request.app
    pre = await pf.run_preflight("live", runner=r, store=app.state.store, settings=app.state.settings,
                                 app_state=app.state)
    if not pre.get("ready"):
        failed = armsvc.failing_checks(pre)
        await audit(request, "arming.set", ok=False, user=user,
                    details={**details, "error": "preflight_not_ready", "failed": [f["id"] for f in failed]})
        raise HTTPException(409, failed)
    try:
        res = await r.set_armed(body.strategy, True, by=by, disarm_gen=gen)
    except (PermissionError, RunnerDisabled) as e:
        await audit(request, "arming.set", ok=False, user=user, details={**details, "error": str(e)})
        raise HTTPException(409, str(e)) from e
    except Exception as e:  # noqa: BLE001   (fail-safe: no store write -> not armed)
        await audit(request, "arming.set", ok=False, user=user, details={**details, "error": type(e).__name__})
        raise HTTPException(503, "store_unavailable") from e
    await audit(request, "arming.set", ok=True, user=user, details=details)
    return {**armsvc.view(r), "result": res}

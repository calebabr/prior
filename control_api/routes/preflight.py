"""GET /api/preflight?target=live|demo: the go-live checklist (services/preflight.py).

Signed-in session (no 2FA: read only, it never places, changes or cancels an order), rate limited per user, and
every target=live run is audited ({target, ready, failed check ids}). The response never carries a secret: key
checks are booleans and errors are type names."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.audit import audit
from ..auth.deps import current_user
from ..auth.ratelimit import check_user
from ..services import preflight as pf

router = APIRouter(prefix="/api")
BUCKET = "preflight"
LIMIT = (6, 60.0)            # each run makes one authenticated Kalshi read and a few bounded store reads


async def _rate_limit(request: Request, user: dict) -> None:
    await check_user(request, user, BUCKET, LIMIT)


@router.get("/preflight")
async def preflight(request: Request, target: Literal["live", "demo"] = Query(...),
                    user: dict = Depends(current_user)) -> dict:
    await _rate_limit(request, user)
    app = request.app
    out = await pf.run_preflight(target, runner=app.state.runner, store=app.state.store,
                                 settings=app.state.settings, app_state=app.state)
    if target == "live":
        await audit(request, "preflight.live", ok=out["ready"], user=user,
                    details={"target": target, "ready": out["ready"],
                             "failed": [c["id"] for c in out["checks"] if c["status"] == pf.FAIL]})
    return out

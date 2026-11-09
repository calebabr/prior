"""GET /api/deploy/status: the read-only status the release workflow's pre-deploy gate reads (R5).

Auth: header ``X-Deploy-Status-Key`` == env ``DEPLOY_STATUS_KEY`` (constant-time compare). 401 when the key is
unset or wrong; per-IP rate limit (checked BEFORE the key, so guessing is throttled). The response is exactly
``{version, schema_version, lease_holder, generation, role, running, stopped, mode, arb_executing, healthy}``:
no secrets, no account data, no strategy values. Not audited per call (it is polled); failures are logged.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request

from .. import version as ver
from ..auth.ratelimit import enforce, get_limiter

log = logging.getLogger("control_api.deploy_status")
router = APIRouter(prefix="/api")

BUCKET = "deploy_status"
LIMIT = (30, 300.0)            # 30 calls / 5 min per IP (the workflow needs a handful)
MAX_KEY_LEN = 256
FIELDS = ("version", "schema_version", "lease_holder", "generation", "role", "running", "stopped", "mode",
          "arb_executing", "healthy")


def _key_ok(given: str | None, expected: str | None) -> bool:
    if not expected or not given or len(given) > MAX_KEY_LEN:
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


@router.get("/deploy/status")
async def deploy_status(request: Request,
                        x_deploy_status_key: str | None = Header(default=None)) -> dict:
    get_limiter(request).limits.setdefault(BUCKET, LIMIT)
    await enforce(request, BUCKET)
    if not _key_ok(x_deploy_status_key, getattr(request.app.state.settings, "deploy_status_key", None)):
        log.warning("deploy status: bad or missing key")
        raise _unauthorized()
    r = request.app.state.runner
    lease_doc, store_ok = None, True
    try:
        lease_doc = await asyncio.wait_for(r.lease.read(), 3)
    except Exception as e:  # noqa: BLE001
        store_ok = False
        log.warning("deploy status: lease read failed: %s", type(e).__name__)
    holder = (lease_doc or {}).get("holder") if store_ok else r.lease.holder
    generation = (lease_doc or {}).get("generation") if store_ok else r.lease.generation
    exp = (lease_doc or {}).get("expires_at")
    now = datetime.now(timezone.utc)
    if isinstance(exp, datetime):
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            holder = None                          # expired or released: nobody holds it
    running = bool(r.desired == "running" and not r.kill_switch() and r.settings.runner_enabled)
    healthy = store_ok and r.store_ok and r._schema_ok is True
    if healthy and running and r.role() == "leader":
        last = r.last_run_utc
        late = timedelta(minutes=r.poll_minutes() + max(5, r.poll_minutes()))
        healthy = last is not None and now - last <= late and not r.last_error
    out = {"version": ver.API_VERSION, "schema_version": ver.SCHEMA_VERSION,
           "lease_holder": str(holder) if holder else None,
           "generation": int(generation) if isinstance(generation, int) else None,
           "role": r.role(), "running": running, "stopped": r.desired != "running" or r.kill_switch(),
           "mode": r.mode if r.mode in ("demo", "live") else "demo",
           "arb_executing": bool(r.arb_running_scan), "healthy": bool(healthy)}
    return {k: out[k] for k in FIELDS}

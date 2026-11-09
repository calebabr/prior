"""Alert history (item 37), alert settings (item 38) and web push subscriptions (item 45).

* GET /api/alerts and GET /api/alerts/settings: signed-in session.
* PUT /api/alerts/settings: fresh 2FA, validated, one atomic store write (cloud_settings["alerts"]), audited.
* Push: the subscription endpoint and keys are sensitive. They are stored, but never logged, audited or returned.
  Subscribe/unsubscribe/test need a signed-in session and are rate limited per user.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..auth.ratelimit import check_user
from ..db import schemas as S
from ..services import alerts as alertsvc
from ..services import webpush

router = APIRouter(prefix="/api")
STORE_TIMEOUT_S = 5.0
PUSH_TEST_TIMEOUT_S = 30.0
RATE_LIMITS = {"push_subscribe": (10, 60.0), "push_test": (5, 60.0), "push_list": (30, 60.0)}


async def _rate_limit(request: Request, user: dict, bucket: str) -> None:
    await check_user(request, user, bucket, RATE_LIMITS[bucket])


async def _store(coro):
    try:
        return await asyncio.wait_for(coro, STORE_TIMEOUT_S)
    except NotImplementedError as e:
        raise HTTPException(503, "store_unavailable") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError:
        return None


# ---------------------------------------------------------------- history (item 37)
def _alert_view(d: dict) -> dict:
    ts = d.get("ts")
    return {"id": d.get("id"), "ts": ts.isoformat() if isinstance(ts, datetime) else ts, "type": d.get("type"),
            "title": d.get("title"), "body": d.get("body")}


@router.get("/alerts")
async def list_alerts(request: Request, type: str | None = Query(None), limit: int = Query(50, ge=1, le=200),
                      before: datetime | None = Query(None), user: dict = Depends(current_user)) -> list[dict]:
    """[{id, ts, type, title, body}] newest first. `before` (ISO) pages back; `type` filters."""
    if type is not None and type not in S.ALERT_TYPES:
        raise HTTPException(422, [{"loc": ["query", "type"], "msg": f"must be one of {list(S.ALERT_TYPES)}"}])
    if before is not None and before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    docs = await _store(request.app.state.store.list_alerts(type=type, limit=limit, before=before))
    return [_alert_view(d) for d in docs]


# ---------------------------------------------------------------- settings (item 38)
def _settings_view(request: Request) -> dict:
    return alertsvc.merged_settings(request.app.state.runner.cloud.get("alerts"))


@router.get("/alerts/settings")
async def get_alert_settings(request: Request, user: dict = Depends(current_user)) -> dict:
    return _settings_view(request)


@router.put("/alerts/settings")
async def put_alert_settings(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    """Partial update of {daily_summary_time, mode, quiet_hours{enabled,start,end}, tz}. 400 [messages] on any
    problem. Returns the full settings plus changes [{key, from, to}] for the toast."""
    r = request.app.state.runner
    body = await _json(request)
    before = _settings_view(request)
    try:
        after = alertsvc.validate_settings(before, body)
    except ValueError as e:
        await audit(request, "alerts.settings", ok=False, user=user, details={"errors": e.args[0]})
        raise HTTPException(400, e.args[0]) from e
    try:
        saved = await asyncio.wait_for(request.app.state.store.save_cloud_settings(
            {"alerts": after}, by=user.get("username", "user")), STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        await audit(request, "alerts.settings", ok=False, user=user, details={"error": "store"})
        raise HTTPException(503, "store_unavailable") from e
    r.cloud["alerts"] = (saved or {}).get("alerts") or after
    changes = alertsvc.changes(before, after)
    await audit(request, "alerts.settings", user=user, details={"changes": changes})
    return {**after, "settings": after, "changes": changes}


# ---------------------------------------------------------------- web push (item 45)
@router.get("/push/public-key")
async def push_public_key(request: Request, user: dict = Depends(current_user)) -> dict:
    push = request.app.state.runner.push
    return {"public_key": push.public_key() if push.enabled else None, "enabled": push.enabled}


@router.post("/push/subscribe")
async def push_subscribe(request: Request, user: dict = Depends(current_user)) -> dict:
    """Body = the browser's PushSubscription JSON: {endpoint, keys: {p256dh, auth}, expirationTime?}."""
    await _rate_limit(request, user, "push_subscribe")
    push = request.app.state.runner.push
    if not push.enabled:
        raise HTTPException(409, "push_not_configured")
    body = await _json(request)
    if not isinstance(body, dict) or not isinstance(body.get("keys"), dict):
        raise HTTPException(400, ["body: must be a PushSubscription {endpoint, keys: {p256dh, auth}}"])
    try:
        endpoint, p256dh, auth = webpush.validate_subscription(body.get("endpoint"), body["keys"].get("p256dh"),
                                                               body["keys"].get("auth"))
    except ValueError as e:
        await audit(request, "push.subscribe", ok=False, user=user, details={"errors": e.args[0]})
        raise HTTPException(400, e.args[0]) from e
    try:
        new = await asyncio.wait_for(request.app.state.store.save_push_subscription(
            {"endpoint": endpoint, "p256dh": p256dh, "auth": auth,
             "ua": (request.headers.get("user-agent") or "")[:128]}), STORE_TIMEOUT_S)
    except ValueError as e:
        raise HTTPException(409, "too_many_subscriptions") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e
    await audit(request, "push.subscribe", user=user, details={"new": bool(new)})     # never the endpoint/keys
    return {"subscribed": True}


@router.delete("/push/subscribe")
async def push_unsubscribe(request: Request, user: dict = Depends(current_user)) -> dict:
    """Body {endpoint}. Idempotent: unknown endpoints answer subscribed=false too."""
    await _rate_limit(request, user, "push_subscribe")
    body = await _json(request)
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > webpush.ENDPOINT_MAX:
        raise HTTPException(400, ["endpoint: required"])
    removed = await _store(request.app.state.store.delete_push_subscription(S.push_sub_id(endpoint)))
    await audit(request, "push.unsubscribe", user=user, details={"removed": bool(removed)})
    return {"subscribed": False}


def _sub_view(d: dict) -> dict:
    """Only the push service host and the created time: never the full endpoint (a capability URL) or keys."""
    try:
        host = urlsplit(str(d.get("endpoint") or "")).hostname or "?"
    except ValueError:
        host = "?"
    ts = d.get("created_utc")
    return {"host": host, "created_utc": ts.isoformat() if isinstance(ts, datetime) else ts}


@router.get("/push/subscriptions")
async def push_list(request: Request, user: dict = Depends(current_user)) -> dict:
    """{count, subscriptions: [{host, created_utc}]} oldest first (S-33)."""
    await _rate_limit(request, user, "push_list")
    docs = await _store(request.app.state.store.list_push_subscriptions())
    subs = sorted((_sub_view(d) for d in docs), key=lambda s: str(s["created_utc"] or ""))
    return {"count": len(subs), "subscriptions": subs}


@router.delete("/push/subscriptions")
async def push_clear(request: Request, user: dict = Depends(current_user)) -> dict:
    """Remove every subscription (all devices). Defensive, so session-only like unsubscribe (S-33)."""
    await _rate_limit(request, user, "push_subscribe")
    removed = await _store(request.app.state.store.clear_push_subscriptions())
    await audit(request, "push.clear", user=user, details={"removed": int(removed or 0)})
    return {"removed": int(removed or 0)}


@router.post("/push/test")
async def push_test(request: Request, user: dict = Depends(current_user)) -> dict:
    """Sends a test notification to every subscribed device now (quiet hours don't apply to tests) and logs it to
    the alert history. Returns {sent, failed, removed}."""
    await _rate_limit(request, user, "push_test")
    r = request.app.state.runner
    if not r.push.enabled:
        raise HTTPException(409, "push_not_configured")
    title, body = "Test Notification", "Prior notifications are working."
    try:   # S-35: one overall budget, like the runner's _deliver_alert
        res = await asyncio.wait_for(r.push.send_all(
            {"title": title, "body": body, "type": "test", "url": "/alerts",
             "ts": datetime.now(timezone.utc).isoformat()}, urgency="normal", ttl=600), PUSH_TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        res = {"sent": 0, "failed": 0, "removed": 0, "error": "timeout"}
    try:
        await asyncio.wait_for(request.app.state.store.add_alert(
            {"type": "test", "title": title, "body": body, "channels": ["push"] if res.get("sent") else []}),
            STORE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        pass
    out = {k: res.get(k, 0) for k in ("sent", "failed", "removed")}
    await audit(request, "push.test", ok=bool(res.get("sent")), user=user, details=out)
    if res.get("error"):
        out["error"] = res["error"]
    return out

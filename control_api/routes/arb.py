"""Bracket arbitrage: GET/PUT /api/arb and GET /api/arb/opportunities.

Log-only by default. Turning arb on, turning arb live on, and changing arb settings all need fresh 2FA (PUT).
Arb live also needs typed confirm "LIVE" and the same env gates as maker live (prod key + KALSHI_LIVE_CONFIRM).
Turning things OFF through PUT also needs 2FA (one endpoint); the 2FA-free way to stop arbitrage is Stop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..runner import RunnerDisabled, StopInProgress
from ..services import arb as arbsvc
from ..services import arbsets
from ..services import pnl as pnlsvc

router = APIRouter(prefix="/api")
CONTROL_KEYS = ("enabled", "live", "confirm", "ack_legging")


def _iso(doc: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in doc.items()}


DEMO_EXECUTE = "demo-execute"


def _pnl_of(filled: list[dict], now: datetime) -> dict | None:
    if not filled:
        return None
    d7 = sum(float(d.get("profit") or 0) for d in filled if d["ts"] >= now - timedelta(days=7))
    d30 = sum(float(d.get("profit") or 0) for d in filled)
    return {"d7": round(d7, 2), "d30": round(d30, 2), "sets": len(filled)}


async def arb_pnls(request: Request) -> tuple[dict | None, dict | None]:
    """(live, demo-execute) arb P&L = sum of the guaranteed profit of FILLED arb sets, by time executed (d7 / d30).
    Demo-execute sets (play money on the demo exchange) are never counted in the live figure."""
    store = request.app.state.store
    now = datetime.now(timezone.utc)
    try:
        docs = await store.list_arb_opportunities(since=now - timedelta(days=30), limit=100000, executed_only=True)
    except Exception:  # noqa: BLE001
        return None, None
    filled = [d for d in docs if str(d.get("executed", "")).startswith("FILLED") and isinstance(d.get("ts"), datetime)]
    live = [d for d in filled if d.get("mode") != DEMO_EXECUTE]
    demo = [d for d in filled if d.get("mode") == DEMO_EXECUTE]
    pd = _pnl_of(demo, now)
    return _pnl_of(live, now), ({**pd, "demo": True} if pd else None)


async def arb_pnl(request: Request) -> dict | None:
    """Live arb P&L only (unchanged shape)."""
    return (await arb_pnls(request))[0]


SUMMARY_DAYS = 30
SUMMARY_ROWS_WAIT_S = 3.0        # per-set results need the shared analytics dataset; never stall the page for it


async def arb_summary_view(request: Request, tz: str | None = None) -> dict:
    """UI-1a: distinct sets, gate outcomes and per-set results recomputed from the STORED arbitrage log (not the
    in-memory scan counters), plus those counters labelled `since_restart`. Counts never fail on the fills read:
    `per_set` is null when the shared analytics dataset is not available within a few seconds."""
    from . import analytics as ar          # late import: analytics imports nothing from here
    r = request.app.state.runner
    zone, tzname = pnlsvc.resolve_tz(tz)
    env = r.mode if getattr(r, "mode", None) in ("demo", "live") else "demo"
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=SUMMARY_DAYS)
    notes: list[str] = []
    try:
        docs = await asyncio.wait_for(request.app.state.store.list_arb_opportunities(since=since, limit=20001), 10)
    except Exception:  # noqa: BLE001
        docs = [d for d in r.arb_recent if isinstance(d.get("ts"), datetime) and d["ts"] >= since]
        notes.append("stored log unreadable: counts cover this process's recent sightings only")
    rows = None
    try:
        rows = await asyncio.wait_for(ar._dataset(request, env, SUMMARY_DAYS, "rows", None), SUMMARY_ROWS_WAIT_S)
    except BaseException as e:  # noqa: BLE001
        if not isinstance(e, Exception):
            raise
        notes.append("per-set results unavailable right now; counts are from the stored log")
    out = arbsets.summary(docs, rows or [], env=env, now=now, zone=zone, tzname=tzname, since=since,
                          label=f"last {SUMMARY_DAYS} days")
    if rows is None:
        out["per_set"] = None
    out["since_restart"] = ar.since_restart(request)
    out["notes"] = notes
    return out


async def arb_view(request: Request, tz: str | None = None) -> dict:
    r = request.app.state.runner
    s = request.app.state.settings
    cfg = r.arb_settings()
    pnl_live, pnl_demo = await arb_pnls(request)
    return {"settings": {"enabled": r.arb_desired == "running", "live": r.arb_mode == "live", **cfg},
            "schema": arbsvc.schema(s), "ceilings": arbsvc.ceilings(s), "status": r.arb_status(),
            "pnl": pnl_live, "pnl_demo_execute": pnl_demo, "expectation": arbsvc.EXPECTATION,
            "arb_summary": await arb_summary_view(request, tz)}


@router.get("/arb")
async def get_arb(request: Request, tz: str | None = Query(None), user: dict = Depends(current_user)) -> dict:
    return await arb_view(request, tz)


@router.put("/arb")
async def put_arb(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    r = request.app.state.runner
    by = user.get("username", "user")
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, ["body: must be a JSON object"])
    errs: list[str] = []
    for k in ("enabled", "live", "ack_legging"):
        if k in body and body[k] is not None and not isinstance(body[k], bool):
            errs.append(f"{k}: must be true/false")
    if "confirm" in body and body["confirm"] is not None and not isinstance(body["confirm"], str):
        errs.append("confirm: must be a string")
    update = {k: v for k, v in body.items() if k not in CONTROL_KEYS}
    new_cfg = None
    if update:
        try:
            new_cfg = arbsvc.validate(r.arb_settings(), update, request.app.state.settings)
        except ValueError as e:
            errs.extend(e.args[0])
    if errs:
        await audit(request, "arb.update", ok=False, user=user, details={"errors": errs})
        raise HTTPException(400, errs)
    enabled, live = body.get("enabled"), body.get("live")
    if live is True and body.get("confirm") != "LIVE":
        await audit(request, "arb.live", ok=False, user=user, details={"error": "confirm"})
        raise HTTPException(400, ["type LIVE to confirm"])
    if live is True and not r.live_allowed():
        why = r.live_block_reason() or "live_not_allowed"     # strategy_not_installed on the public stand-in
        await audit(request, "arb.live", ok=False, user=user, details={"error": why})
        raise HTTPException(409, why)
    if live is True and r.arb_mode != "live":
        # L-1: switching arbitrage live re-runs the go-live preflight (409 with the failing checks)
        from .arming import live_recheck
        await live_recheck(request, user=user, action="arb.live")

    if new_cfg is not None and new_cfg != r.arb_settings():
        before = dict(r.config or {})
        after = {**before, "arb": new_cfg}
        try:
            version = await request.app.state.store.save_config(after, before=before, by=by)
        except Exception as e:  # noqa: BLE001
            await audit(request, "arb.update", ok=False, user=user, details={"error": "store"})
            raise HTTPException(503, "store_unavailable") from e
        await r.apply_config(after)          # atomic file write, keeps _help keys and cells
        await audit(request, "arb.update", user=user, details={"update": update, "version": version})

    if enabled is not None or live is not None:
        try:
            res = await r.set_arb(enabled=enabled, live=live, by=by, ack_legging=bool(body.get("ack_legging")))
        except PermissionError as e:
            await audit(request, "arb.toggle", ok=False, user=user, details={"error": str(e)})
            raise HTTPException(409, str(e)) from e
        except (StopInProgress, RunnerDisabled) as e:
            await audit(request, "arb.toggle", ok=False, user=user, details={"error": str(e)})
            raise HTTPException(409, str(e)) from e
        except Exception as e:  # noqa: BLE001   (fail-safe: no store write -> no change)
            await audit(request, "arb.toggle", ok=False, user=user, details={"error": type(e).__name__})
            raise HTTPException(503, "store_unavailable") from e
        await audit(request, "arb.live" if live is not None else "arb.toggle", user=user,
                    details={"enabled": enabled, "live": live, **res})
    return await arb_view(request)


@router.get("/arb/opportunities")
async def opportunities(request: Request, limit: int = Query(100, ge=1, le=1000),
                        executed_only: bool = False, user: dict = Depends(current_user)) -> dict:
    r = request.app.state.runner
    try:
        items = await request.app.state.store.list_arb_opportunities(limit=limit, executed_only=executed_only)
    except Exception:  # noqa: BLE001   store down: serve this process's recent sightings
        items = [d for d in r.arb_recent if not executed_only or d.get("executed") != "log-only"][:limit]
    return {"items": [_iso(i) for i in items]}

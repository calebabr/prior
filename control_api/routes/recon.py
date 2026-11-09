"""Daily Kalshi reconciliation reports (services/kalshi_recon.py). Read-only.

* GET /api/recon/latest[?mode=demo|live]  -> {"report": {...}|null}   the newest report (newest day, then newest check)
* GET /api/recon/history?days=30[&mode=]  -> {"days": n, "reports": [...]}   newest day first

Signed-in session (like the other read routes) and a per-user rate limit. The reports hold ids and numbers only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.deps import current_user
from ..auth.ratelimit import check_user
from ..db import schemas as S

router = APIRouter(prefix="/api/recon")
STORE_TIMEOUT_S = 5.0
RATE_LIMITS = {"recon_read": (30, 60.0)}
HISTORY_MAX_DAYS = S.RECON_RETENTION_DAYS


async def _rate_limit(request: Request, user: dict, bucket: str = "recon_read") -> None:
    await check_user(request, user, bucket, RATE_LIMITS[bucket])


def _mode(mode: str | None) -> str | None:
    if mode is not None and mode not in S.MODE_VALUES:
        raise HTTPException(422, [{"loc": ["query", "mode"], "msg": f"must be one of {list(S.MODE_VALUES)}"}])
    return mode


async def _store(coro):
    try:
        return await asyncio.wait_for(coro, STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


def _view(d: dict) -> dict:
    """The stored report without the internal `ts` (the same instant as `checked_at`)."""
    out = {k: v for k, v in d.items() if k != "ts"}
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


@router.get("/latest")
async def recon_latest(request: Request, mode: str | None = Query(None), user: dict = Depends(current_user)) -> dict:
    await _rate_limit(request, user)
    doc = await _store(request.app.state.store.latest_recon_report(_mode(mode)))
    return {"report": _view(doc) if doc else None}


@router.get("/history")
async def recon_history(request: Request, days: int = Query(30, ge=1, le=HISTORY_MAX_DAYS),
                        mode: str | None = Query(None), user: dict = Depends(current_user)) -> dict:
    await _rate_limit(request, user)
    since_day = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    docs = await _store(request.app.state.store.list_recon_reports(since_day=since_day, mode=_mode(mode),
                                                                   limit=days * len(S.MODE_VALUES)))
    return {"days": days, "reports": [_view(d) for d in docs]}

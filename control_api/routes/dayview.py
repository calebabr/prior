"""GET /api/analytics/day: the Day view (services/dayview.py). Signed in, no 2FA, read only, rate limited in the
returns bucket, validated params (422). Uses the cached trade rows the other analytics routes share."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request

from ..auth.deps import current_user
from ..services import analytics as an
from ..services import dayview as dv
from ..services import pnl as pnlsvc
from ..services import returns as retsvc
from ..services import strategies as strat
from . import analytics as ar

router = APIRouter(prefix="/api")


@router.get("/analytics/day")
async def analytics_day(request: Request, user: dict = Depends(current_user)) -> dict:
    await ar._rate_limit(request, user, "analytics_returns")
    q = request.query_params
    day = ar._parse_date("date", q.get("date"))
    strategy = q.get("strategy") or None
    if strategy is not None and strategy not in strat.STRATEGIES:
        raise ar._bad("strategy", f"must be one of {list(strat.STRATEGIES)}")
    env = ar._env(request, q.get("env"))
    zone, tzname = pnlsvc.resolve_tz(q.get("tz"))
    now = ar._now()
    if day > now.astimezone(zone).date():
        raise ar._bad("date", "must not be in the future")
    first = retsvc.local_start_utc(day - timedelta(days=dv.TRAIL_DAYS), zone)
    need = (now - first).total_seconds() / 86400.0
    days = next((d for d in an.DAYS_ALLOWED if d >= need), None)
    if days is None:
        raise ar._bad("date", f"must be within the last {max(an.DAYS_ALLOWED) - dv.TRAIL_DAYS} days")
    rows = await ar._dataset(request, env, days, "rows", None, swr=True)
    notes: list[str] = []
    eps = await ar._soft(request.app.state.store.list_quote_episodes(
        since=first, env=None if env == "all" else env, limit=an.MAX_EPISODES, fields=an.EPISODE_FIELDS),
        notes, "quote episodes", [])
    return dv.build(rows, eps or [], day=day, zone=zone, tzname=tzname, env=env, now=now, strategy=strategy,
                    truncated=len(rows) >= an.MAX_FILLS, notes=notes)

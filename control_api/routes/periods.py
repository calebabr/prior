"""Periods, cash-flow events and the period-aware P&L views (Sprint 2A-1 section 1).

Reads need a signed-in session. Every write (a new period marker, a deposit / withdrawal, deleting one) needs fresh
2FA and is audited. Attribution is by fill date (services/periods.py). Stop is not involved here.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..auth.ratelimit import get_limiter
from ..services import markers as M
from ..services import periods as P
from ..services import pnl as pnlsvc

router = APIRouter(prefix="/api")
STORE_TIMEOUT_S = 10.0
RATE_LIMITS = {"period_summary": (30, 60.0)}
_LOCKS_GUARD = threading.Lock()


def _lock(request: Request) -> asyncio.Lock:
    """One lock per app: two simultaneous marker writes cannot both pass the typo guard."""
    with _LOCKS_GUARD:
        lk = getattr(request.app.state, "periods_lock", None)
        if lk is None:
            lk = request.app.state.periods_lock = asyncio.Lock()
    return lk


async def _store(coro):
    try:
        return await asyncio.wait_for(coro, STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError:
        return None


def _rate_limit(request: Request, user: dict, bucket: str) -> None:
    lim = get_limiter(request)
    lim.limits.setdefault(bucket, RATE_LIMITS[bucket])
    ok, retry = lim.hit(bucket, "user:" + str(user.get("username") or "?"))
    if not ok:
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(retry)})


async def payload(request: Request) -> dict:
    """{periods, cash_flows}: the GET /api/periods body."""
    store = request.app.state.store
    markers = await _store(store.list_periods())
    flows = await _store(store.list_cash_flows())
    flows = sorted(flows, key=lambda f: P.utc(f.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    tl = P.timeline(markers)
    try:       # additive and best effort: a slow or failing audit read never breaks the periods list
        marks = M.build_markers(await _store(store.list_audit(M.AUDIT_SCAN)), tl)
    except Exception:  # noqa: BLE001
        marks = []
    body = {"periods": [P.public_period(p) for p in tl], "cash_flows": [P.public_flow(f) for f in flows]}
    if marks:      # present only when there is something to show: the body is unchanged for an empty audit log
        body["markers"] = marks
    return body


async def window_for(request: Request, spec: str | None) -> dict | None:
    """The timeline entry for a `period=` filter value (None = no filter / `all`). 422 on an unknown id, 503 when
    the store cannot be read. Shared by the analytics, activity, history and by-source routes."""
    if spec is None or spec == "all":
        return None
    if spec != "current" and not P.valid_id(spec):
        raise HTTPException(422, [{"loc": ["query", "period"], "msg": "must be all, current or a period id"}])
    tl = P.timeline(await _store(request.app.state.store.list_periods()))
    try:
        return P.resolve(tl, spec)
    except ValueError:
        raise HTTPException(422, [{"loc": ["query", "period"], "msg": "unknown period"}]) from None


def _name(user: dict) -> str:
    return str(user.get("username") or "user")[:64]


# ---------------------------------------------------------------- list
@router.get("/periods")
async def list_periods(request: Request, user: dict = Depends(current_user)) -> dict:
    """{periods: [{id, name, started_at, ended_at, note, current, implicit}], cash_flows: [{id, ts, kind, amount,
    note, created_by}], markers: [{ts, kind, label, detail, changes_trading, period_id}] (newest first, max 100)}. Oldest first. Period 1 is implicit (`started_at` null = the beginning of data)."""
    return await payload(request)


# ---------------------------------------------------------------- markers
@router.post("/periods")
async def create_period(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    """Mark a new period starting now: {name, note?}. 409 `period_too_soon` if the last marker is under 5 minutes
    old (typo guard). Returns the GET body plus `created` (the new period's id)."""
    body = await _json(request)
    store = request.app.state.store
    now = datetime.now(timezone.utc)
    async with _lock(request):
        markers = await _store(store.list_periods())
        try:
            doc = P.validate_marker(body, markers, now)
        except ValueError as e:
            arg = e.args[0]
            await audit(request, "period.create", ok=False, user=user,
                        details={"error": "too_soon" if arg == "too_soon" else "invalid"})
            if arg == "too_soon":
                raise HTTPException(409, "period_too_soon") from e
            raise HTTPException(400, arg) from e
        doc["created_by"] = _name(user)           # the store assigns the id
        try:
            saved = await _store(store.add_period(doc))
        except HTTPException:
            await audit(request, "period.create", ok=False, user=user, details={"error": "store"})
            raise
    pid = str((saved or doc).get("id") or doc["id"])
    await audit(request, "period.create", user=user, details={"id": pid, "name": doc["name"]})
    return {**await payload(request), "created": pid}


# ---------------------------------------------------------------- cash flows
@router.post("/cash-flows")
async def add_cash_flow(request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    """Record a deposit or withdrawal: {kind: deposit|withdrawal, amount, ts, note?, confirm_duplicate?}. Guard rails
    (UI-1a): `ts` (the date the money moved) is REQUIRED and cannot be in the future (422 with the messages); a flow
    with the same kind, amount and date as an existing one is refused with 409 unless `confirm_duplicate` is true.
    Non-blocking `warnings` ([{code, message}]: date inside the current period, no matching balance jump) come back
    with the saved flow. Money movements are shown beside the period figures and never netted into P&L. Returns the
    GET body plus `created` (the new id) and `warnings`."""
    body = await _json(request)
    store = request.app.state.store
    now = datetime.now(timezone.utc)
    try:
        doc = P.validate_cash_flow(body, now)
    except ValueError as e:
        await audit(request, "cash_flow.create", ok=False, user=user, details={"error": "invalid"})
        raise HTTPException(422, e.args[0]) from e
    confirmed = doc.pop("confirm_duplicate", False)
    flows = await _store(store.list_cash_flows())
    dup = P.find_duplicate(doc, flows)
    if dup is not None and not confirmed:
        await audit(request, "cash_flow.create", ok=False, user=user,
                    details={"error": "duplicate", "kind": doc["kind"], "amount": doc["amount"]})
        raise HTTPException(409, [f"duplicate: a {doc['kind']} of ${doc['amount']:.2f} on "
                                  f"{doc['ts'].date().isoformat()} is already recorded; send confirm_duplicate=true "
                                  "to add it anyway"])
    markers = await _store(store.list_periods())
    cur = next((p for p in P.timeline(markers) if p.get("current")), None)
    try:
        snaps = await _store(store.list_account_snapshots(since=doc["ts"] - timedelta(days=1), limit=2000))
    except HTTPException:
        snaps = []                                    # a warning is optional; never block the write on it
    warnings = P.flow_warnings(doc, (cur or {}).get("started_at"), snaps)
    doc["created_by"] = _name(user)
    try:
        saved = await _store(store.add_cash_flow(doc))
    except HTTPException:
        await audit(request, "cash_flow.create", ok=False, user=user, details={"error": "store"})
        raise
    fid = (saved or {}).get("id")
    await audit(request, "cash_flow.create", user=user,
                details={"id": fid, "kind": doc["kind"], "amount": doc["amount"], "ts": P.iso(doc["ts"]),
                         "duplicate_confirmed": bool(dup is not None and confirmed),
                         "warnings": [w["code"] for w in warnings]})
    return {**await payload(request), "created": fid, "warnings": warnings}


@router.delete("/cash-flows/{flow_id}")
async def delete_cash_flow(flow_id: str, request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    """Delete one cash-flow entry (a typo fix). 404 if it does not exist. Returns the GET body plus `deleted`."""
    if not P.valid_id(flow_id):
        raise HTTPException(400, ["id: invalid"])
    try:
        ok = await _store(request.app.state.store.delete_cash_flow(flow_id))
    except HTTPException:
        await audit(request, "cash_flow.delete", ok=False, user=user, details={"id": flow_id, "error": "store"})
        raise
    if not ok:
        await audit(request, "cash_flow.delete", ok=False, user=user, details={"id": flow_id, "error": "not_found"})
        raise HTTPException(404, "cash_flow_not_found")
    await audit(request, "cash_flow.delete", user=user, details={"id": flow_id})
    return {**await payload(request), "deleted": flow_id}


# ---------------------------------------------------------------- summary
def _env(request: Request, raw: str | None) -> str | None:
    if raw is None:
        mode = getattr(request.app.state.runner, "mode", None)
        return mode if mode in ("demo", "live") else "demo"
    if raw not in ("demo", "live", "all"):
        raise HTTPException(422, [{"loc": ["query", "env"], "msg": "must be one of ['demo', 'live', 'all']"}])
    return None if raw == "all" else raw


@router.get("/periods/{period_id}/summary")
async def period_summary(period_id: str, request: Request, env: str | None = Query(None),
                         source: str = Query("bot"), user: dict = Depends(current_user)) -> dict:
    """The period summary (`period_id` may also be `current`). Top-level figures cover `source` (bot = the bot's
    strategies, default; you = manual trades; all); `by_source` always splits every source and `total` is all of
    them. Fields per block: fills, contracts, settled, wins, losses, win_rate, pnl (settled fills, net of fees),
    money_spent (cost of all fills), settled_cost, fees, return_on_settled_cost (pnl / settled_cost),
    return_on_all_cost (pnl / money_spent), open_fills, open_at_risk. `cash_flows` holds the deposits and
    withdrawals dated inside the period, shown beside (never netted into) P&L, and equity-based figures when
    account snapshots exist. Fills are attributed by FILL date: the headline `pnl` (`pnl_basis` = fill_date) is the
    period's P&L. `cash_flows.equity_change_ex_flows` is an account-equity move that ALSO includes settlements of
    positions filled in earlier periods (flagged by `equity_change_includes_earlier_settlements` and explained in
    `equity_change_label`), so do not read it as the period's P&L."""
    _rate_limit(request, user, "period_summary")
    if source not in P.SOURCES:
        raise HTTPException(422, [{"loc": ["query", "source"], "msg": f"must be one of {list(P.SOURCES)}"}])
    if period_id != "current" and not P.valid_id(period_id):
        raise HTTPException(404, "unknown_period")
    e = _env(request, env)
    store = request.app.state.store
    tl = P.timeline(await _store(store.list_periods()))
    try:
        period = P.resolve(tl, period_id)
    except ValueError:
        raise HTTPException(404, "unknown_period") from None
    return await _store(P.read_summary(store, period, env=e, source=source, now=datetime.now(timezone.utc)))


# ---------------------------------------------------------------- realised P&L by source for a period
@router.get("/pnl/by-source")
async def pnl_by_source(request: Request, period: str | None = Query(None), env: str | None = Query(None),
                        user: dict = Depends(current_user)) -> dict:
    """Realised P&L split by source: {period, env, by_source: {maker, arb, you, bot, total, settled_fills}}.
    `period=<id|current>` attributes by FILL date (a fill's P&L follows the period it was filled in, even when it
    settled later); `all` / omitted covers everything kept (the last 180 days)."""
    p = await window_for(request, period)
    e = _env(request, env)
    now = datetime.now(timezone.utc)
    start, end = P.window_of(p)
    store = request.app.state.store
    since = P.lookback_start(start, now)
    res = await _store(pnlsvc.by_source_for_fills(store, since, end, env=e))
    return {"period": P.public_period(p) if p else None, "env": e, "by_source": res}

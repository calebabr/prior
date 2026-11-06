"""Read-only routes: activity, history, health, logs, audit."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.deps import current_user
from ..services import ops_monitor as opsmon
from ..services import strategies as strat
from .periods import window_for

router = APIRouter(prefix="/api")


def _iso(doc: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in doc.items()}


async def _call(coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


async def _soft(coro, timeout: float = 5.0) -> list:
    """Best-effort read for the additive parts of a response: [] on any error (the main rows still answer)."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception:  # noqa: BLE001
        return []


def _since(since: datetime | None) -> datetime | None:
    """`since=` (item 30): aware UTC (a naive value is UTC); never earlier than the 180-day retention."""
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return max(since.astimezone(timezone.utc), datetime.now(timezone.utc) - timedelta(days=180))


async def _period_bounds(request: Request, period: str | None, s: datetime | None
                         ) -> tuple[dict | None, datetime | None, datetime | None]:
    """(period, since, until) once an optional `period=<id|current|all>` filter is applied to `since`."""
    p = await window_for(request, period)
    if p is None:
        return None, s, None
    start = _since(p["started_at"]) if p["started_at"] else None
    if start is not None:
        s = max(s, start) if s else start
    return p, s, p["ended_at"]


def _before(rows: list[dict], until: datetime | None) -> list[dict]:
    if until is None:
        return rows
    return [r for r in rows if not isinstance(r.get("ts"), datetime) or r["ts"] < until]


def _after(rows: list[dict], since: datetime | None) -> list[dict]:
    """Rows strictly after `since` (the store's `since` is inclusive)."""
    if since is None:
        return rows
    return [r for r in rows if not isinstance(r.get("ts"), datetime) or r["ts"] > since]


FILL_VIEW = ("fill_id", "ts", "ticker", "market_ticker", "side", "outcome_side", "action", "yes_price_dollars",
             "no_price_dollars", "yes_price", "no_price", "count_fp", "count", "fee_cost", "is_taker", "env",
             "order_id", "source", "strategy")


def _fill_row(f: dict) -> dict:
    from ..services import pnl as pnlsvc
    src, sid = strat.fill_label(f)
    side = pnlsvc.fill_side(f)
    ts = f.get("ts")
    return {"fill_id": f.get("fill_id"), "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
            "ticker": f.get("ticker") or f.get("market_ticker"), "side": side, "action": f.get("action") or "buy",
            "price": pnlsvc.fill_price(f, side) if side else None, "count": pnlsvc.fill_count(f),
            "fee": pnlsvc._f(f.get("fee_cost")), "kind": "taker" if f.get("is_taker") else "maker",
            "env": f.get("env"), "source": src, "strategy": sid}


@router.get("/activity")
async def activity(request: Request, limit: int = Query(100, ge=1, le=1000),
                   since: datetime | None = Query(None), source: str | None = Query(None, pattern="^(bot|you)$"),
                   period: str | None = Query(None), user: dict = Depends(current_user)) -> dict:
    """Journal items (the bot's own events, labelled bot/maker) plus, additively for Y4, recent fills with their
    source/strategy and recent settlements with the sources that traded them. `since` keeps rows after it.
    `period=<id|current|all>` (default all) keeps the rows of one period: journal rows and fills by their own
    time, settlements by the period their FILL was in (a fill's settlement follows the fill)."""
    store = request.app.state.store
    pinfo, s, until = await _period_bounds(request, period, _since(since))
    rows = _before(_after(await _call(store.list_journal(since=s, limit=limit)), s), until)
    items = [dict(_iso(r), source=strat.BOT, strategy="maker") for r in rows]
    fills = _after(await _soft(store.list_fills(since=s, until=until, limit=limit, fields=FILL_VIEW)), s)
    frows = [_fill_row(f) for f in fills]
    if source:
        frows = [f for f in frows if f["source"] == source]
    in_period: set[str] | None = None
    if pinfo is not None:                       # settlements of fills filled in the period (fill-date attribution)
        pf = await _soft(store.list_fills(since=s, until=until, limit=20000, fields=("ticker", "market_ticker")))
        in_period = {str(f.get("ticker") or f.get("market_ticker")) for f in pf}
    setts = _after(await _soft(store.list_settlements(since=s, limit=2000 if pinfo else min(limit, 200),
                                                      fields=("ticker", "market_result", "result", "ts", "revenue"))),
                   s)
    if in_period is not None:
        setts = [x for x in setts if str(x.get("ticker")) in in_period][:min(limit, 200)]
    srows = []
    if setts:
        tss = [x["ts"] for x in setts if isinstance(x.get("ts"), datetime)]
        lo = min(tss) - timedelta(days=35) if tss else None
        if pinfo is not None and s is not None:
            lo = min(lo, s) if lo else s
        lab = strat.ticker_sources(await _soft(store.list_fills(
            since=lo, until=until, limit=20000, fields=("ticker", "market_ticker", "is_taker", "source", "strategy"))))
        for x in setts:
            srcs = sorted(lab.get(str(x.get("ticker")), set()))
            if source and source not in srcs:
                continue
            srows.append({"ticker": x.get("ticker"), "result": x.get("market_result") or x.get("result"),
                          "ts": x["ts"].isoformat() if isinstance(x.get("ts"), datetime) else x.get("ts"),
                          "revenue": x.get("revenue"), "sources": srcs,
                          "source": strat.YOU if srcs == [strat.YOU] else (strat.BOT if srcs else None)})
    out = {"items": items, "fills": frows, "settlements": srows,
           "since": s.isoformat() if s else None}
    if pinfo is not None:
        out["period"] = {"id": pinfo["id"], "name": pinfo["name"]}
    return out


@router.get("/history")
async def history(request: Request, days: int = Query(30, ge=1, le=180), since: datetime | None = Query(None),
                  period: str | None = Query(None), user: dict = Depends(current_user)) -> dict:
    """Account snapshots (whole-account balances; they can't be split by source). `since` overrides `days`.
    `period=<id|current|all>` (default all) keeps the snapshots dated inside that period and overrides `days`."""
    pinfo, s, until = await _period_bounds(request, period, _since(since))
    since = s or datetime.now(timezone.utc) - timedelta(days=days)
    if pinfo is not None and s is None:                 # period 1 starts at the beginning of data
        since = datetime.now(timezone.utc) - timedelta(days=180)
    snaps = _before(_after(await _call(request.app.state.store.list_account_snapshots(since=since)), s), until)
    snaps = list(reversed(snaps))                                  # store returns newest first
    return {"snapshots": [{"ts": s["ts"].isoformat() if isinstance(s.get("ts"), datetime) else s.get("ts"),
                           "cash": s.get("cash"), "exposure": s.get("exposure"), "headroom": s.get("headroom")}
                          for s in snaps]}


async def _ops(request: Request) -> dict | None:
    """Storage / TTL index / clock-skew report (cached 60 s); raises an alert through the existing alerter."""
    try:
        rep = await opsmon.ops_report(request.app.state.store)
        opsmon.maybe_alert(getattr(request.app.state.runner, "alerter", None), rep)
        return rep
    except Exception:  # noqa: BLE001
        return None


@router.get("/health")
async def health(request: Request, user: dict = Depends(current_user)) -> dict:
    doc = await _call(request.app.state.store.last_monitor_summary())
    ops = await _ops(request)                           # additive, read-only (item 19); never fails the route
    if not doc:
        return {"summary": None, "report_text": None, "ops": ops}
    summary = doc.get("summary")
    if isinstance(summary, dict):
        summary = dict(summary, ts=doc["ts"].isoformat() if isinstance(doc.get("ts"), datetime) else doc.get("ts"))
    return {"summary": summary, "report_text": doc.get("report_text"), "ops": ops}


@router.get("/logs")
async def logs(request: Request, lines: int = Query(200, ge=1, le=2000),
               user: dict = Depends(current_user)) -> dict:
    return {"lines": request.app.state.runner.logs.tail(lines)}


@router.get("/audit")
async def audit_log(request: Request, limit: int = Query(100, ge=1, le=1000),
                    user: dict = Depends(current_user)) -> dict:
    items = await _call(request.app.state.store.list_audit(limit=limit))
    return {"items": [_iso(i) for i in items]}

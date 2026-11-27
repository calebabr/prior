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

# ---------------------------------------------------------------- B4: demo / live separation
ENVS = ("demo", "live")
ENV_PATTERN = "^(demo|live|all)$"
ENV_OVERFETCH = 3                 # rows read per row returned when an env filter can drop some (then truncated)
HB_SLACK_S = 5.0                  # clock slack around a pass window when placing a journal row
HB_DEFAULT_PASS_S = 900.0         # a heartbeat without duration_s: assume the longest pass window


def _env_param(request: Request, env: str | None) -> str:
    """`env=demo|live|all`; omitted = the current mode (same default as P&L and analytics)."""
    if env:
        return env
    mode = getattr(request.app.state.runner, "mode", None)
    return mode if mode in ENVS else "demo"


def fill_env(f: dict) -> str:
    """A stored fill's env; a row without one counts as demo (the backfill rule, as for snapshots)."""
    e = f.get("env")
    return e if e in ENVS else "demo"


def _pass_windows(heartbeats: list[dict]) -> list[tuple[datetime, datetime, str]]:
    """[(start, end, mode)] for every heartbeat that names its mode: the pass ran from ts - duration_s to ts."""
    out = []
    for h in heartbeats:
        ts, mode = h.get("ts"), h.get("mode")
        if not isinstance(ts, datetime) or mode not in ENVS:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            dur = float(h.get("duration_s"))
            dur = dur if 0 <= dur < 86400 else HB_DEFAULT_PASS_S
        except (TypeError, ValueError):
            dur = HB_DEFAULT_PASS_S
        out.append((ts - timedelta(seconds=dur + HB_SLACK_S), ts + timedelta(seconds=HB_SLACK_S), mode))
    return out


def journal_env(row: dict, windows: list[tuple[datetime, datetime, str]]) -> str | None:
    """B4 rule for a journal row (rows carry no env): the mode of the pass that wrote it, i.e. of every stored
    heartbeat whose pass window [ts - duration_s, ts] (+/- 5 s) contains the row's time. None when no window
    contains it, or windows of both modes do (cannot place: the row is shown in every view)."""
    ts = row.get("ts")
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    modes = {m for lo, hi, m in windows if lo <= ts <= hi}
    return modes.pop() if len(modes) == 1 else None


def _fill_row(f: dict) -> dict:
    from ..services import pnl as pnlsvc
    src, sid = strat.fill_label(f)
    side = pnlsvc.fill_side(f)
    ts = f.get("ts")
    return {"fill_id": f.get("fill_id"), "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
            "ticker": f.get("ticker") or f.get("market_ticker"), "side": side, "action": f.get("action") or "buy",
            "price": pnlsvc.fill_price(f, side) if side else None, "count": pnlsvc.fill_count(f),
            "fee": pnlsvc._f(f.get("fee_cost")), "kind": "taker" if f.get("is_taker") else "maker",
            "env": fill_env(f), "source": src, "strategy": sid}


@router.get("/activity")
async def activity(request: Request, limit: int = Query(100, ge=1, le=1000),
                   since: datetime | None = Query(None), source: str | None = Query(None, pattern="^(bot|you)$"),
                   period: str | None = Query(None), env: str | None = Query(None, pattern=ENV_PATTERN),
                   user: dict = Depends(current_user)) -> dict:
    """Journal items (the bot's own events, labelled bot/maker) plus, additively for Y4, recent fills with their
    source/strategy and recent settlements with the sources that traded them. `since` keeps rows after it.
    `period=<id|current|all>` (default all) keeps the rows of one period: journal rows and fills by their own
    time, settlements by the period their FILL was in (a fill's settlement follows the fill).

    B4: `env=demo|live|all` (default the current mode). Fills by their env (none = demo); journal rows by the
    pass that wrote them (journal_env); settlements by the env of the stored fills on their ticker. A journal row
    or settlement that cannot be placed is kept in every view with `env: null` (never hidden)."""
    store = request.app.state.store
    ev = _env_param(request, env)
    runner = request.app.state.runner
    if callable(getattr(runner, "request_fills_sync", None)):
        runner.request_fills_sync("read:activity")          # B3: background, never awaited
    pinfo, s, until = await _period_bounds(request, period, _since(since))
    fetch = limit if ev == "all" else min(limit * ENV_OVERFETCH, 3000)
    rows = _before(_after(await _call(store.list_journal(since=s, limit=fetch)), s), until)
    windows: list = []
    jts = [r["ts"] for r in rows if isinstance(r.get("ts"), datetime)]
    if jts:
        lo = min(jts) - timedelta(hours=1)
        windows = _pass_windows(await _soft(store.list_heartbeats(since=lo, limit=20000)))
    items = []
    for r in rows:
        je = journal_env(r, windows)
        if ev != "all" and je is not None and je != ev:
            continue
        items.append(dict(_iso(r), source=strat.BOT, strategy="maker", env=je))
    items = items[:limit]
    fills = _after(await _soft(store.list_fills(since=s, until=until, limit=fetch, fields=FILL_VIEW)), s)
    if ev != "all":
        fills = [f for f in fills if fill_env(f) == ev]
    frows = [_fill_row(f) for f in fills[:limit]]
    if source:
        frows = [f for f in frows if f["source"] == source]
    in_period: set[str] | None = None
    if pinfo is not None:                       # settlements of fills filled in the period (fill-date attribution)
        pf = await _soft(store.list_fills(since=s, until=until, limit=20000, fields=("ticker", "market_ticker")))
        in_period = {str(f.get("ticker") or f.get("market_ticker")) for f in pf}
    s_lim = min(limit, 200) if ev == "all" else min(limit, 200) * ENV_OVERFETCH
    setts = _after(await _soft(store.list_settlements(since=s, limit=2000 if pinfo else s_lim,
                                                      fields=("ticker", "market_result", "result", "ts", "revenue"))),
                   s)
    if in_period is not None:
        setts = [x for x in setts if str(x.get("ticker")) in in_period][:s_lim]
    srows = []
    if setts:
        tss = [x["ts"] for x in setts if isinstance(x.get("ts"), datetime)]
        lo = min(tss) - timedelta(days=35) if tss else None
        if pinfo is not None and s is not None:
            lo = min(lo, s) if lo else s
        sfills = await _soft(store.list_fills(
            since=lo, until=until, limit=20000,
            fields=("ticker", "market_ticker", "is_taker", "source", "strategy", "env")))
        tenvs: dict[str, set[str]] = {}
        for f in sfills:
            tenvs.setdefault(str(f.get("ticker") or f.get("market_ticker")), set()).add(fill_env(f))
        lab = strat.ticker_sources(sfills if ev == "all" else [f for f in sfills if fill_env(f) == ev])
        for x in setts:
            envs = sorted(tenvs.get(str(x.get("ticker")), set()))
            if ev != "all" and envs and ev not in envs:
                continue                     # placed, and only in the other env
            srcs = sorted(lab.get(str(x.get("ticker")), set()))
            if source and source not in srcs:
                continue
            srows.append({"ticker": x.get("ticker"), "result": x.get("market_result") or x.get("result"),
                          "ts": x["ts"].isoformat() if isinstance(x.get("ts"), datetime) else x.get("ts"),
                          "revenue": x.get("revenue"), "sources": srcs,
                          "source": strat.YOU if srcs == [strat.YOU] else (strat.BOT if srcs else None),
                          "envs": envs})
    out = {"items": items, "fills": frows, "settlements": srows[:min(limit, 200)],
           "since": s.isoformat() if s else None, "env": ev}
    if pinfo is not None:
        out["period"] = {"id": pinfo["id"], "name": pinfo["name"]}
    return out


@router.get("/history")
async def history(request: Request, days: int = Query(30, ge=1, le=180), since: datetime | None = Query(None),
                  period: str | None = Query(None), env: str | None = Query(None, pattern=ENV_PATTERN),
                  user: dict = Depends(current_user)) -> dict:
    """Account snapshots (whole-account balances; they can't be split by source). `since` overrides `days`.
    `period=<id|current|all>` (default all) keeps the snapshots dated inside that period and overrides `days`.
    B4: `env=demo|live|all` (default the current mode); a snapshot without env counts as demo. Every snapshot is
    stamped with its env when written, so none is unplaceable."""
    ev = _env_param(request, env)
    pinfo, s, until = await _period_bounds(request, period, _since(since))
    since = s or datetime.now(timezone.utc) - timedelta(days=days)
    if pinfo is not None and s is None:                 # period 1 starts at the beginning of data
        since = datetime.now(timezone.utc) - timedelta(days=180)
    snaps = _before(_after(await _call(request.app.state.store.list_account_snapshots(
        since=since, env=None if ev == "all" else ev)), s), until)
    snaps = list(reversed(snaps))                                  # store returns newest first
    return {"snapshots": [{"ts": s["ts"].isoformat() if isinstance(s.get("ts"), datetime) else s.get("ts"),
                           "cash": s.get("cash"), "exposure": s.get("exposure"), "headroom": s.get("headroom"),
                           "env": s.get("env") if s.get("env") in ENVS else "demo"}
                          for s in snaps], "env": ev}


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

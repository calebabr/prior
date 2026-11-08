"""UX read-only routes: strategy registry, changelog, "since you last
checked" and the read-only Kalshi account view. All behind a signed-in session; none of them writes anything."""
from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.deps import current_user
from ..auth.ratelimit import get_limiter
from .periods import window_for
from ..services import periods as per
from ..services import pnl as pnlsvc
from ..services import strategies as strat

router = APIRouter(prefix="/api")
CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "data" / "changelog.json"
CHANGELOG_MAX = 50
SINCE_MAX_DAYS = 35
STORE_TIMEOUT_S = 8.0
ACCOUNT_TIMEOUT_S = 25.0
ACCOUNT_CACHE_S = 30.0
ACCOUNT_MAX_TICKERS = 25              # GET /markets/{t} lookups per refresh (titles + current prices)
RATE_LIMITS = {"account_positions": (20, 60.0), "since": (60, 60.0)}
_POOL: ThreadPoolExecutor | None = None
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.\-]{0,79}$")


def _rate_limit(request: Request, user: dict, bucket: str) -> None:
    lim = get_limiter(request)
    lim.limits.setdefault(bucket, RATE_LIMITS[bucket])
    ok, retry = lim.hit(bucket, "user:" + str(user.get("username") or "?"))
    if not ok:
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(retry)})


def _pool() -> ThreadPoolExecutor:
    """Account reads run on their own worker, never the default pool Stop's cancel uses (S-25 family)."""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account")
    return _POOL


# ---------------------------------------------------------------- strategies (Y1)
@router.get("/strategies")
async def strategies(user: dict = Depends(current_user)) -> list[dict]:
    """[{id, source, name, color, prefix}] for every bot strategy, then {id: "you", ...}. The UI reads labels and
    colours from here, so a new strategy is one registry entry."""
    return strat.registry()


# ---------------------------------------------------------------- changelog (item 44)
_CL_CACHE: dict[str, Any] = {}


def load_changelog(path: Path = CHANGELOG_PATH) -> list[dict]:
    """[{version, date, items:[str]}] newest first, validated; [] if the file is missing or unreadable."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _CL_CACHE.get("key") == key:
            return _CL_CACHE["value"]
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("version"), str) or not isinstance(e.get("date"), str):
            continue
        items = [str(i)[:300] for i in (e.get("items") or []) if isinstance(i, str)][:30]
        out.append({"version": e["version"][:32], "date": e["date"][:10], "items": items})
    out = out[:CHANGELOG_MAX]
    _CL_CACHE.update(key=key, value=out)
    return out


@router.get("/changelog")
async def changelog(user: dict = Depends(current_user)) -> list[dict]:
    return load_changelog()


# ---------------------------------------------------------------- since you last checked (item 30)
@router.get("/since")
async def since_view(request: Request, ts: datetime = Query(...), period: str | None = Query(None),
                     user: dict = Depends(current_user)) -> dict:
    """{fills, settled, pnl_change, since} for the Overview card. `since` echoes the time actually used (clamped to
    the last 35 days and to now). pnl_change = realised bot P&L of markets settled since then (manual trades are
    reported separately in by_source, never in the bot number). Counts are for the current mode.

    `period=<all|current|id>` (Sprint 2A-2): `since` is clipped to the period start (never earlier), only fills
    FILLED inside the period count (fill-date attribution, as in the period summary; a position filled earlier
    that settles now stays with its own period), and the response gains `window` {label, from, to, period_id}
    and `clipped`. Omitted = unchanged."""
    _rate_limit(request, user, "since")
    pw = await window_for(request, period) if period is not None else None     # 422 on an unknown id
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    s = min(max(ts.astimezone(timezone.utc), now - timedelta(days=SINCE_MAX_DAYS)), now)
    pstart, pend = per.window_of(pw)
    clipped = pstart is not None and pstart > s
    if clipped:
        s = min(pstart, now)
    fill_window = (pstart, pend) if period is not None else None
    env = getattr(request.app.state.runner, "mode", None)
    store = request.app.state.store
    try:
        res, settl, fills = await asyncio.wait_for(pnlsvc.by_source_since(store, s, env=env, fill_window=fill_window),
                                                  STORE_TIMEOUT_S)
        new_fills = await asyncio.wait_for(store.list_fills(since=s, limit=5000,
                                                            fields=("fill_id", "ts", "env", "source", "strategy",
                                                                    "is_taker")), STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e
    new_fills = [f for f in new_fills if isinstance(f.get("ts"), datetime) and f["ts"] > s
                 and (not env or not f.get("env") or f.get("env") == env)
                 and (fill_window is None or per.in_window(f["ts"], pstart, pend))]
    by_src: dict[str, int] = {}
    for f in new_fills:
        src, sid = strat.fill_label(f)
        k = strat.YOU if src == strat.YOU else (sid or "maker")
        by_src[k] = by_src.get(k, 0) + 1
    settled = [x for x in settl if isinstance(x.get("ts"), datetime) and x["ts"] > s
               and (x.get("market_result") or x.get("result")) in ("yes", "no")]
    if fill_window is not None:     # count only markets that have a fill belonging to the period
        mine = {f.get("ticker") or f.get("market_ticker") for f in fills}
        settled = [x for x in settled if x.get("ticker") in mine]
    out = {"since": s.isoformat(), "fills": len(new_fills), "settled": len(settled),
           "pnl_change": res.get("bot"),
           "by_source": {k: {"fills": by_src.get(k, 0), "pnl": res.get(k)} for k in strat.bucket_ids()},
           "pnl_total": res.get("total")}
    if period is not None:
        hi = min(pend, now) if pend else now
        label = ("Since your last visit (all periods)" if pw is None
                 else f"Since your last visit, within {pw.get('name') or pw['id']}"
                 + (f" (since {per._day(pstart)})" if clipped else ""))
        out["window"] = {"label": label, "from": s.isoformat(), "to": hi.isoformat(),
                         "period_id": pw["id"] if pw else "all"}
        out["clipped"] = bool(clipped)
    return out


# ---------------------------------------------------------------- account positions (Y3)
def _f(v: Any) -> float | None:
    """A finite float, else None (Kalshi "inf" / "nan" strings are unknown, never a huge position)."""
    return pnlsvc._f(v)


def _dollars(d: dict, key: str) -> float | None:
    """Kalshi money field: `<key>_dollars` (string dollars) else `<key>` (cents)."""
    v = _f(d.get(f"{key}_dollars"))
    if v is None:
        c = _f(d.get(key))
        v = c / 100.0 if c is not None else None
    return v


def _market_info(client: Any, tickers: list[str]) -> dict[str, dict]:
    out = {}
    for t in tickers[:ACCOUNT_MAX_TICKERS]:
        if not _TICKER_RE.match(t):
            continue
        try:
            m = client.get_market(t) or {}
        except Exception:  # noqa: BLE001
            continue
        out[t] = {"title": str(m.get("title") or m.get("subtitle") or "")[:200] or None,
                  "yes_bid": _dollars(m, "yes_bid"), "no_bid": _dollars(m, "no_bid"),
                  "last": _dollars(m, "last_price")}
    return out


def _read_account(client: Any) -> tuple[list[dict], list[dict], dict[str, dict]]:
    body = client.get_positions(count_filter="position", limit=200) or {}
    positions = [p for p in (body.get("market_positions") or []) if isinstance(p, dict)]
    orders = [o for o in (client.get_orders(status="resting") or []) if isinstance(o, dict)]
    tickers = list(dict.fromkeys([str(p.get("ticker") or "") for p in positions]
                                 + [str(o.get("ticker") or "") for o in orders]))
    return positions, orders, _market_info(client, [t for t in tickers if t])


def _position_rows(positions: list[dict], info: dict[str, dict], labels: dict[str, dict]) -> list[dict]:
    rows = []
    for p in positions:
        t = str(p.get("ticker") or "")
        pos = _f(p.get("position_fp"))
        pos = _f(p.get("position")) if pos is None else pos
        if not t or not pos:
            continue
        side, count = ("yes" if pos > 0 else "no"), abs(pos)
        cost = _dollars(p, "market_exposure")
        price = round(cost / count, 4) if cost is not None and count else None
        mi = info.get(t) or {}
        bid = mi.get("yes_bid" if side == "yes" else "no_bid")
        base = {"kind": "position", "ticker": t, "title": mi.get("title"), "side": side}
        # split by source using the fills we hold for this ticker (net contracts on this side)
        lab = labels.get(t) or {}
        parts: list[tuple[str, str | None, float]] = []
        for (src, sid, s), n in sorted((lab.get("count") or {}).items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            if s == side and n > 0:
                parts.append((src, sid, n))
        if not parts:
            parts = [(strat.YOU, None, count)]               # no fill we know of: not the bot's (Y1 default)
        known = sum(n for _, _, n in parts)
        if known > 0 and abs(known - count) > 1e-9:          # scale to the live position
            parts = [(src, sid, count * n / known) for src, sid, n in parts]
        for src, sid, n in parts:
            n = round(n, 2)
            rows.append({**base, "count": n, "price": price,
                         "value": round(n * bid, 2) if bid is not None else None,
                         "payout_if_wins": round(n * 1.0, 2), "source": src, "strategy": sid})
    return rows


def _order_rows(orders: list[dict], info: dict[str, dict]) -> list[dict]:
    rows = []
    for o in orders:
        t = str(o.get("ticker") or "")
        side = o.get("side") if o.get("side") in ("yes", "no") else None
        n = _f(o.get("remaining_count_fp"))
        n = _f(o.get("remaining_count")) if n is None else n
        if not t or not n:
            continue
        price = _dollars(o, f"{side}_price") if side else None
        lab = strat.order_label(o)
        rows.append({"kind": "order", "ticker": t, "title": (info.get(t) or {}).get("title"), "side": side,
                     "action": o.get("action"), "count": round(n, 2), "price": price,
                     "value": round(n * price, 2) if price is not None else None,
                     "payout_if_wins": round(n * 1.0, 2), **lab})
    return rows


@router.get("/account/positions")
async def account_positions(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    """Y3: every open position and resting order on the Kalshi account (current mode), tagged bot/you. Read-only:
    GET /portfolio/positions, GET /portfolio/orders?status=resting and GET /markets/{t} (titles, prices). Cached
    30 s; 409 kalshi_not_configured without keys; 503 kalshi_unavailable on errors."""
    _rate_limit(request, user, "account_positions")
    r = request.app.state.runner
    mode = r.mode
    cache = getattr(request.app.state, "account_cache", None) or {}
    hit = cache.get(mode)
    if hit and 0 <= time.monotonic() - hit[0] < ACCOUNT_CACHE_S:
        return hit[1]
    loop = asyncio.get_running_loop()
    try:
        client = await asyncio.wait_for(loop.run_in_executor(_pool(), r.kalshi_factory, mode), ACCOUNT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "kalshi_unavailable") from e
    if client is None or not callable(getattr(client, "get_positions", None)):
        raise HTTPException(409, "kalshi_not_configured")
    try:
        positions, orders, info = await asyncio.wait_for(loop.run_in_executor(_pool(), _read_account, client),
                                                         ACCOUNT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "kalshi_unavailable") from e
    try:
        fills = await asyncio.wait_for(request.app.state.store.list_fills(
            since=datetime.now(timezone.utc) - timedelta(days=60), limit=50000, fields=pnlsvc.FILL_FIELDS),
            STORE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        fills = []
    labels = strat.ticker_labels([f for f in fills if not f.get("env") or f.get("env") == mode])
    rows = _position_rows(positions, info, labels) + _order_rows(orders, info)
    cache[mode] = (time.monotonic(), rows)
    request.app.state.account_cache = cache
    return rows

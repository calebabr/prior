"""Day view: what the bot did on ONE local day (viewer's zone), read only, from the cached trade rows.

Filled = bot fills whose fill time falls in the local day. Settled = bot fills whose market settled in the local day
(the same bucket the daily Returns bars use). Realized = P&L of the settled ones. Open fills carry the market's close
time (fill time + hours-to-close) as `settles_at`; `settles_later` counts them. Resting orders and future days are out
of scope. `source == "you"` rows are excluded.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable

from . import returns as retsvc

TRAIL_DAYS = 7
MAX_LIST = 500                      # rows per list in the body (the app collapses and paginates at 25)


def _iso(d: datetime) -> str:
    return retsvc.iso(d)


def _group(r: dict) -> str:
    return retsvc._group(r)


def _bot(rows: Iterable[dict], strategy: str | None) -> list[dict]:
    out = [r for r in rows if r.get("source") != "you"]
    return [r for r in out if _group(r) == strategy] if strategy else out


def _fill_row(r: dict, now: datetime) -> dict:
    settled = r.get("_settled") is not None and r["_settled"] <= now
    hrs = r.get("ttc_hours")
    at = None
    if not settled and hrs is not None:
        at = _iso(r["_ts"] + timedelta(hours=float(hrs)))
    return {"fill_id": r["fill_id"], "label": r["ticker"], "ticker": r["ticker"], "series": r.get("series"),
            "strategy": _group(r), "side": r["side"], "count": r.get("count"), "price": r.get("price"),
            "cost": r.get("cost"), "ts": r.get("ts"),
            "status": "open" if not settled else ("won" if r.get("result") == "win" else "lost"),
            "settles_at": at}


def _settled_row(r: dict, now: datetime) -> dict:
    out = _fill_row(r, now)
    out.update({"pnl": r.get("pnl"), "settled_utc": r.get("settled_utc")})
    return out


def _rate(num: int, den: int) -> float | None:
    return None if den <= 0 else round(num / den, 4)


def _window(rows: list[dict], key: str, lo: datetime, hi: datetime) -> list[dict]:
    return [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]


def _fill_rate(episodes: Iterable[dict], lo: datetime, hi: datetime) -> float | None:
    quoted = filled = 0
    for e in episodes or []:
        if not isinstance(e, dict):
            continue
        t = retsvc._ts(e.get("ts"))
        if t is None or not lo <= t < hi:
            continue
        quoted += 1
        try:
            filled += 1 if float(e.get("filled_count") or 0) > 0 else 0
        except (TypeError, ValueError):
            pass
    return _rate(filled, quoted)


def _avg(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return None if not v else round(sum(v) / len(v), 4)


def build(rows: list[dict], episodes: list[dict], *, day: date, zone: tzinfo, tzname: str, env: str, now: datetime,
          strategy: str | None = None, truncated: bool = False, notes: list[str] | None = None) -> dict:
    """The /api/analytics/day body. `rows` are trade rows (`_ts`, `_settled`, `_pnl`)."""
    lo = retsvc.local_start_utc(day, zone)
    hi = retsvc.local_start_utc(day + timedelta(days=1), zone)
    bot = _bot(rows, strategy)
    allbot = _bot(rows, None)
    filled = sorted(_window(bot, "_ts", lo, hi), key=lambda r: (r["_ts"], r["fill_id"]), reverse=True)
    settled = sorted([r for r in _window(bot, "_settled", lo, hi) if r["_settled"] <= now],
                     key=lambda r: (r["_settled"], r["fill_id"]), reverse=True)
    later = [r for r in filled if r.get("_settled") is None or r["_settled"] > now]
    wins = sum(1 for r in settled if r.get("result") == "win")
    realized = round(sum(r["_pnl"] for r in settled), 2)
    summary = {"filled": {"count": len(filled), "cost": round(sum(r["_cost"] for r in filled), 2)},
               "settled": {"count": len(settled), "wins": wins, "losses": len(settled) - wins},
               "realized": realized, "settles_later": len(later)}

    # analytics: today against the mean of the trailing TRAIL_DAYS local days (the day itself excluded)
    trail = [(retsvc.local_start_utc(day - timedelta(days=i), zone),
              retsvc.local_start_utc(day - timedelta(days=i - 1), zone)) for i in range(TRAIL_DAYS, 0, -1)]

    def maker_share(a: datetime, b: datetime) -> float | None:
        fs = _window(allbot, "_ts", a, b)
        return _rate(sum(1 for r in fs if _group(r) == "maker"), len(fs))

    def win_rate(a: datetime, b: datetime) -> float | None:
        ss = [r for r in _window(allbot, "_settled", a, b) if r["_settled"] <= now]
        return _rate(sum(1 for r in ss if r.get("result") == "win"), len(ss))

    analytics = [
        {"key": "fill_rate", "label": "Fill rate", "today": _fill_rate(episodes, lo, hi),
         "avg7": _avg([_fill_rate(episodes, a, b) for a, b in trail])},
        {"key": "maker_share", "label": "Maker share of fills", "today": maker_share(lo, hi),
         "avg7": _avg([maker_share(a, b) for a, b in trail])},
        {"key": "win_rate", "label": "Win rate", "today": win_rate(lo, hi),
         "avg7": _avg([win_rate(a, b) for a, b in trail])},
    ]
    cut = len(filled) > MAX_LIST or len(settled) > MAX_LIST
    notes = list(notes or [])
    if truncated:
        notes.append("the fill read hit its row limit: totals may be incomplete")
    if cut:
        notes.append(f"only the newest {MAX_LIST} rows per list are shown")
    return {"date": day.isoformat(), "tz_used": tzname, "env": env, "generated_utc": _iso(now),
            "strategy": strategy, "summary": summary,
            "filled": [_fill_row(r, now) for r in filled[:MAX_LIST]],
            "settled": [_settled_row(r, now) for r in settled[:MAX_LIST]],
            "analytics": analytics, "notes": notes}

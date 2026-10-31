"""Returns per calendar day, week and month (docs/ANALYTICS_CONTRACT.md, "Returns"). Pure functions, read only.

Numerator G: realized P&L of BOT fills (maker, arbitrage; `source == "you"` excluded and only counted in a note) whose
market SETTLED inside the period. A period is a LOCAL calendar span in the viewer's zone and the bucket is the local
DATE of the settlement time, so daylight-saving changes never split or merge a bucket.

Denominator B (modified Dietz): B = V0 + sum(F_i * w_i). V0 is the account equity (cash + exposure) of the last
snapshot at or before the period start, F_i a deposit (+) or withdrawal (-) after that snapshot and up to the period
end, w_i = (end_eff - max(ts_i, start)) / (end_eff - start) on REAL elapsed seconds (a 23 or 25 hour day is handled),
end_eff = min(period end, now). return_rate = G / B as a FRACTION, or null with a `return_reason`.

Whole periods only: the period that straddles the window start is dropped. Nothing is annualized or projected.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable

from . import pnl as pnlsvc
from . import strategies as strat

GRANULARITIES = ("day", "week", "month")
SMALL_SAMPLE = 5                         # fewer settled fills than this (and more than zero) is a small sample
MIN_BASE = 10.0                          # dollars; a base at or below this gives no percentage
STALE_START = timedelta(hours=24)        # a start snapshot older than this before the period start is not trusted
MAX_PERIODS = 200
FILLS_MAX_SPAN_DAYS = 31                 # drill-down: widest period (a month)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LABELS = {"demo": "demo, indicative only", "live": "live, small sample, indicative only"}
MIXED_LABEL = "mixed demo and live, indicative only"


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------- calendar
def period_start(d: date, granularity: str) -> date:
    """First local date of the period holding local date `d` (Monday for a week, the 1st for a month)."""
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    return d


def period_end(start: date, granularity: str) -> date:
    """Last local date (inclusive) of the period starting on local date `start`."""
    if granularity == "week":
        return start + timedelta(days=6)
    if granularity == "month":
        nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return nxt - timedelta(days=1)
    return start


def is_boundary(d: date, granularity: str) -> bool:
    return period_start(d, granularity) == d


def _prev(start: date, granularity: str) -> date:
    return period_start(start - timedelta(days=1), granularity)


def local_start_utc(d: date, zone: tzinfo) -> datetime:
    """First real instant of local date `d` (a midnight that does not exist gives the first instant after the gap)."""
    return pnlsvc.local_midnight_utc(datetime.combine(d, time(12), tzinfo=zone), zone)


def _fmt_day(d: date, with_dow: bool = False) -> str:
    s = f"{MONTHS[d.month - 1][:3]} {d.day}"
    return f"{DOW[d.weekday()]} {s}" if with_dow else s


def label_for(start: date, end: date, granularity: str) -> str:
    if granularity == "month":
        return f"{MONTHS[start.month - 1]} {start.year}"
    if granularity == "week":
        return f"{_fmt_day(start)} to {_fmt_day(end)}"
    return _fmt_day(start, True)


def enumerate_periods(now: datetime, zone: tzinfo, granularity: str, window_start: datetime) -> list[dict]:
    """Whole periods inside the window, oldest first, the current one last. A period whose start is before
    `window_start` (the one straddling the window start) is dropped. Each is {start, end, start_utc, end_utc}."""
    out: list[dict] = []
    s = period_start(now.astimezone(zone).date(), granularity)
    while len(out) < MAX_PERIODS:
        start_utc = local_start_utc(s, zone)
        if start_utc < window_start:
            break
        e = period_end(s, granularity)
        out.append({"start": s, "end": e, "start_utc": start_utc,
                    "end_utc": local_start_utc(e + timedelta(days=1), zone)})
        s = _prev(s, granularity)
    out.reverse()
    return out


# ---------------------------------------------------------------- rows
def _group(r: dict) -> str:
    return r.get("strategy") or ("arb" if r.get("kind") == "taker" else "maker")


def _settled_rows(rows: Iterable[dict], lo: datetime, now: datetime) -> list[dict]:
    return [r for r in rows if r.get("_settled") is not None and lo <= r["_settled"] <= now]


def _snapshots(snapshots: Iterable[dict]) -> list[tuple[datetime, float]]:
    out = []
    for s in snapshots or []:
        if not isinstance(s, dict):
            continue
        t, cash, expo = _ts(s.get("ts")), pnlsvc._f(s.get("cash")), pnlsvc._f(s.get("exposure"))
        if t is not None and cash is not None and expo is not None:
            out.append((t, cash + expo))
    out.sort(key=lambda x: x[0])
    return out


def _flows(flows: Iterable[dict]) -> list[tuple[datetime, float]]:
    out = []
    for f in flows or []:
        if not isinstance(f, dict):
            continue
        t, a = _ts(f.get("ts")), pnlsvc._f(f.get("amount"))
        if t is not None and a is not None:
            out.append((t, a if f.get("kind") == "deposit" else -a))
    out.sort(key=lambda x: x[0])
    return out


def _last_at_or_before(snaps: list[tuple[datetime, float]], t: datetime) -> tuple[datetime, float] | None:
    best = None
    for s in snaps:
        if s[0] <= t:
            best = s
        else:
            break
    return best


def dietz_base(snaps: list[tuple[datetime, float]], flows: list[tuple[datetime, float]], start: datetime,
               end_eff: datetime) -> tuple[float | None, str]:
    """(base, reason). base is None when no usable start snapshot (reason no_start_snapshot / stale_start_snapshot)."""
    s0 = _last_at_or_before(snaps, start)
    if s0 is None:
        return None, "no_start_snapshot"
    if start - s0[0] > STALE_START:
        return None, "stale_start_snapshot"
    span = (end_eff - start).total_seconds()
    base = s0[1]
    for t, amt in flows:
        if t <= s0[0] or t > end_eff:
            continue
        if span <= 0 or t <= start:
            w = 1.0
        else:
            w = (end_eff - t).total_seconds() / span
        base += amt * w
    return base, "ok"


def _strategy_ids(rows: Iterable[dict]) -> list[str]:
    ids = list(strat.STRATEGIES)
    for r in rows:
        g = _group(r)
        if r.get("source") != "you" and g not in ids:
            ids.append(g)
    return ids


# ---------------------------------------------------------------- build
def build(rows: list[dict], snapshots: list[dict], flows: list[dict], *, zone: tzinfo, tzname: str,
          granularity: str, days: int, env: str, env_is_current: bool, now: datetime,
          truncated: bool = False, notes: list[str] | None = None) -> dict:
    """The /api/analytics/returns body (see the module doc). `rows` are trade rows with `_settled`, `_pnl`."""
    notes = list(notes or [])
    window_start = now - timedelta(days=days)
    periods = enumerate_periods(now, zone, granularity, window_start)
    ids = _strategy_ids(rows)
    snaps, fl = _snapshots(snapshots), _flows(flows)
    buckets: dict[date, dict] = {p["start"]: {"pnl": dict.fromkeys(ids, 0.0), "n": dict.fromkeys(ids, 0),
                                              "you_n": 0, "you_pnl": 0.0} for p in periods}
    for r in _settled_rows(rows, window_start, now):
        b = buckets.get(period_start(r["_settled"].astimezone(zone).date(), granularity))
        if b is None:
            continue                                    # inside the dropped (straddling) period
        if r.get("source") == "you":
            b["you_n"] += 1
            b["you_pnl"] += r["_pnl"]
        else:
            g = _group(r)
            b["pnl"][g] += r["_pnl"]
            b["n"][g] += 1
    out_periods, tot_pnl, tot_n, you_n, you_pnl = [], 0.0, 0, 0, 0.0
    for p in periods:
        b = buckets[p["start"]]
        end_eff = min(p["end_utc"], now)
        partial = p["end_utc"] > now
        pnl = sum(b["pnl"].values())
        n = sum(b["n"].values())
        base, reason = dietz_base(snaps, fl, p["start_utc"], end_eff)
        if not env_is_current:
            base, reason = None, "env_not_current"
        elif base is not None and base <= MIN_BASE:
            base, reason = None, "base_too_small"
        rate = None if base is None else round(pnl / base, 6)
        inflow = [a for t, a in fl if p["start_utc"] < t <= end_eff]
        dep, wd = sum(a for a in inflow if a > 0), -sum(a for a in inflow if a < 0)
        s0 = _last_at_or_before(snaps, p["start_utc"])
        s1 = _last_at_or_before(snaps, end_eff)
        change = None
        if env_is_current and s0 is not None and s1 is not None and s1[0] > s0[0]:
            change = round(s1[1] - s0[1], 2)
        out_periods.append({
            "start_local": p["start"].isoformat(), "end_local": p["end"].isoformat(),
            "start_utc": iso(p["start_utc"]), "end_utc": iso(p["end_utc"]),
            "label": label_for(p["start"], p["end"], granularity), "partial": partial,
            "elapsed_hours": round((end_eff - p["start_utc"]).total_seconds() / 3600.0, 2) if partial else None,
            "realized_pnl": round(pnl, 2),
            "by_strategy": {g: round(b["pnl"][g], 2) for g in ids},
            "settled_fills": n, "fills_by_strategy": dict(b["n"]),
            "base": _r(base), "return_rate": rate, "return_reason": reason,
            "flows_in_period": {"count": len(inflow), "net": round(dep - wd, 2), "deposits": round(dep, 2),
                                "withdrawals": round(wd, 2)},
            "equity_change": change,
            "you": {"settled_fills": b["you_n"], "realized_pnl": round(b["you_pnl"], 2)},
            "small_sample": 0 < n < SMALL_SAMPLE, "empty": n == 0})
        tot_pnl += pnl
        tot_n += n
        you_n += b["you_n"]
        you_pnl += b["you_pnl"]
    if not periods:
        notes.append(f"no whole {granularity} fits in a {days} day window")
    if you_n:
        notes.append(f"Your own trades are not included ({you_n} settled, {you_pnl:+.2f})")
    if truncated:
        notes.append("the fill read hit its row limit: totals may be incomplete")
    notes.append("Whole periods only; percentages are not additive")
    return {"env": env, "days": days, "granularity": granularity, "tz_used": tzname,
            "generated_utc": iso(now), "attribution": "settlement date (local period in tz_used)",
            "label": LABELS.get(env, MIXED_LABEL), "small_sample_threshold": SMALL_SAMPLE, "min_base": MIN_BASE,
            "truncated": bool(truncated), "periods": out_periods,
            "totals": {"realized_pnl": round(tot_pnl, 2), "settled_fills": tot_n},
            "notes": notes}


def period_fills(rows: list[dict], *, zone: tzinfo, start: date, end: date, now: datetime) -> list[dict]:
    """Bot fills settled in local dates [start, end], newest settlement first (public fields only)."""
    lo, hi = local_start_utc(start, zone), local_start_utc(end + timedelta(days=1), zone)
    hits = [r for r in rows if r.get("source") != "you" and r.get("_settled") is not None
            and lo <= r["_settled"] < hi and r["_settled"] <= now]
    hits.sort(key=lambda r: (r["_settled"], r["fill_id"]), reverse=True)
    return [{"fill_id": r["fill_id"], "ticker": r["ticker"], "series": r.get("series"), "side": r["side"],
             "strategy": _group(r), "count": r.get("count"), "price": r.get("price"), "pnl": r.get("pnl"),
             "result": r.get("result"), "settled_utc": r.get("settled_utc"), "ts": r.get("ts")} for r in hits]

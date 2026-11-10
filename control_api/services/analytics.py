"""Read-only analytics (docs/ANALYTICS_CONTRACT.md): trade rows, P&L curve + drawdown, per-cell / series / band /
time-to-close edge with Wilson CIs, weekly fill quality and the arbitrage summary.

Pure functions over plain store docs; no I/O here (routes/analytics.py does the bounded store reads). The live
config `cells` are only READ to label fills, never modified.

Definitions (shared with the contract):
* settled fill = its ticker has a settlement with result yes/no; P&L = services.pnl.fill_pnl;
* kind = "maker" (is_taker false) or "taker";
* price band = one band per distinct price range [lo, hi) of the configured cells, labelled "lo-last" in dollars
  (last = hi - 1 cent; the topmost band includes hi, as the strategy's in_cell does), else "other" (price rounded
  to the cent). Bands are derived from the installed config, so no price range is hard-coded here;
* hours to close (ttc) = market close - fill time. Close time sources, first found: the fill's close_time /
  expiration_time, the quote episode's close_time, the settlement's close_time, else the settlement time (later
  than the real close for most markets, so ttc is an upper bound then);
* cell = the first config cell [lo, hi, h_lo, h_hi, side] matching (price, ttc, side) with the strategy's own test
  (maker_strategy.in_cell: lo <= price < hi + 1e-9, h_lo <= ttc < h_hi). When the close time is only known from the
  settlement time, a maker fill's cell comes from the journal `quote` row of its order (the cell the strategy
  itself matched when it placed the order), if that cell is in the live config;
* win-rate CI = Wilson score (95%); edge = win_rate - avg_price (avg over fills, same unit as win_rate);
* drawdown = cum_pnl - running max of cum_pnl, the running max starting at 0 (equity starts at 0).
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

from .. import cpuyield as cy
from . import arbsets
from . import benchmarks as bench
from . import fillquality as fq
from . import pnl as pnlsvc
from . import queuepos as qp
from .strategies import fill_label

DAYS_ALLOWED = (7, 30, 90, 180)
ENVS = ("demo", "live", "all")
LOOKBACK = timedelta(days=7)        # fills settled inside the window may have been filled up to ~2 days before it
MAX_FILLS = 20_000
MAX_SETTLEMENTS = 20_000
MAX_EPISODES = 20_000
MAX_JOURNAL = 20_000
MAX_ARB = 20_000
Z95 = 1.959963984540054
BAND_OTHER = "other"
TTC_UNKNOWN = "unknown"
CELL_NONE = "none"
FQ_LABELS = ("front", "trade_through")

# Projections (keep the per-request memory small on Atlas M0)
FILL_FIELDS = ("fill_id", "ts", "ticker", "market_ticker", "side", "outcome_side", "yes_price_dollars",
               "no_price_dollars", "yes_price", "no_price", "count_fp", "count", "fee_cost", "is_taker", "env",
               "order_id", "close_time", "expiration_time", fq.FIELD, "source", "strategy", "user_note")
SETTLEMENT_FIELDS = ("ticker", "market_result", "result", "ts", "settled_time", "close_time")
JOURNAL_FIELDS = ("event", "note", "cell", "ts")
EPISODE_FIELDS = ("order_id", "ts", "env", "status", "outcome", "queue_ahead_at_join", "fill_ids", "front_fill",
                  "close_time", "series", "ticker", "side", "price", "count", "filled_count", "first_fill_utc",
                  "closed_utc")
EXPECTATIONS_PATH = bench.EXPECTATIONS_PATH
DEFAULT_EXPECTATION = {"win_rate": None, "roi": None}   # no research number in code; the benchmark file has it
MIN_MARKETS = 20                       # verdicts need at least this many distinct markets in the cell
CAL_STEP_C = 2                         # calibration bucket width in cents, over the configured cells' price span
CSV_MAX_ROWS = 5000
TRADE_COLUMNS = ("fill_id", "ts", "ticker", "series", "side", "kind", "price", "count", "fee", "cost", "band",
                 "ttc_hours", "ttc_band", "cell", "status", "result", "settled_utc", "pnl", "fill_quality",
                 "source", "strategy")


# ---------------------------------------------------------------- small helpers
def _ts(v: Any) -> datetime | None:
    return fq._ts(v)


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat() if isinstance(d, datetime) else None


def _r(x: float | None, nd: int) -> float | None:
    return None if x is None else round(float(x), nd)


def _num(x: Any) -> str:
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:g}"


def iso_week(d: datetime | date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def price_bands(cells: Any) -> list[tuple[int, int, str]]:
    """(lo_cents, last_cents_inclusive, label) per distinct price range of the configured cells, sorted by price.
    A band covers lo <= price < hi; the topmost band also includes its hi (the strategy's in_cell treats a price
    equal to hi as inside). Read-only; no price range is hard-coded here."""
    ranges = sorted({(round(c[0] * 100), round(c[1] * 100)) for c in parse_cells(cells)})
    ranges = [(lo, hi) for lo, hi in ranges if 0 <= lo < hi <= 100]
    if not ranges:
        return []
    top = max(hi for _, hi in ranges)
    out = []
    for lo, hi in ranges:
        last = hi if hi == top else hi - 1
        out.append((lo, last, f"{lo / 100:.2f}-{last / 100:.2f}"))
    return out


def band_labels(cells: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(b[2] for b in price_bands(cells)))


def price_band(price: float | None, bands: list[tuple[int, int, str]] | None = None) -> str:
    """The first band (from price_bands) holding the price rounded to the cent, else "other"."""
    if price is None:
        return BAND_OTHER
    c = round(price * 100)
    for lo, last, label in bands or ():
        if lo <= c <= last:
            return label
    return BAND_OTHER


def ttc_bands(cells: Any) -> list[tuple[float, float | None, str]]:
    """(lo, hi or None, label) time-to-close buckets whose edges are the configured cells' hour edges (0 always
    included), plus an open-ended last bucket; no hour edge is hard-coded here. No cells -> one ">0h" bucket."""
    edges = sorted({0.0} | {float(e) for c in parse_cells(cells) for e in (c[2], c[3]) if float(e) > 0})
    out: list[tuple[float, float | None, str]] = [(a, b, f"{_num(a)}-{_num(b)}h") for a, b in zip(edges, edges[1:])]
    out.append((edges[-1], None, f">{_num(edges[-1])}h"))
    return out


def ttc_labels(cells: Any) -> tuple[str, ...]:
    return tuple(b[2] for b in ttc_bands(cells))


def ttc_band(hours: float | None, bands: list[tuple[float, float | None, str]] | None = None) -> str | None:
    """The bucket (from ttc_bands) holding `hours`; a fill after the close (negative ttc) counts in the first."""
    if hours is None:
        return None
    bands = bands if bands is not None else ttc_bands(None)
    for _lo, hi, label in bands:
        if hi is None or hours < hi:
            return label
    return bands[-1][2]


# ---------------------------------------------------------------- cells (read only)
def parse_cells(cells: Any) -> list[tuple[float, float, float, float, str]]:
    """Valid rows of the config `cells` as tuples; malformed rows are skipped. Never modifies the input."""
    out = []
    for c in cells if isinstance(cells, (list, tuple)) else []:
        try:
            lo, hi, h0, h1, side = c
            row = (float(lo), float(hi), float(h0), float(h1), str(side))
        except (TypeError, ValueError):
            continue
        if row[4] in ("yes", "no"):
            out.append(row)
    return out


def cell_id(c: Iterable) -> str:
    lo, hi, h0, h1, side = c
    return f"{float(lo):.2f}-{float(hi):.2f}|{_num(h0)}-{_num(h1)}h|{side}"


def match_cell(price: float | None, hours: float | None, side: str | None, cells: list[tuple]) -> tuple | None:
    """First matching cell, the same test as maker_strategy.in_cell."""
    if price is None or hours is None or side is None:
        return None
    for c in cells:
        lo, hi, h0, h1, s = c
        if s == side and lo <= price < hi + 1e-9 and h0 <= hours < h1:
            return c
    return None


def journal_cell_entries(rows: Iterable[dict]) -> dict[str, tuple[str, datetime | None]]:
    """order_id -> (cell id, row ts) from journal `quote` rows (the cell the strategy matched when it placed the
    order). Rows come newest first; the first row seen for an order id wins."""
    out: dict[str, tuple[str, datetime | None]] = {}
    for r in rows:
        cy.checkpoint()
        if not isinstance(r, dict) or r.get("event") != "quote":
            continue
        m = qp._OID.search(str(r.get("note") or ""))
        if not m or m.group(1) in out:
            continue
        try:
            c = json.loads(r.get("cell") or "")
        except (TypeError, ValueError):
            continue
        rows_ = parse_cells([c])
        if rows_:
            out[m.group(1)] = (cell_id(rows_[0]), _ts(r.get("ts")) or _ts(r.get("utc")))
    return out


def journal_cells(rows: Iterable[dict]) -> dict[str, str]:
    """order_id -> cell id from journal `quote` rows."""
    return {k: v[0] for k, v in journal_cell_entries(rows).items()}


# ---------------------------------------------------------------- statistics
def wilson(wins: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """Wilson score interval for a binomial proportion; (None, None) when n == 0."""
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def cluster_ratio_ci(clusters: Iterable[tuple[float, float]], z: float = Z95,
                     clip: tuple[float, float] | None = None) -> tuple[float | None, float | None, float | None]:
    """Ratio estimator R = sum(num) / sum(den) over clusters with a cluster-robust (between-cluster) variance:
    V = G/(G-1) * sum((num_c - R*den_c)^2) / (sum den)^2, CI = R +/- z*sqrt(V) (normal approximation).
    Returns (R, lo, hi); lo/hi are None with fewer than 2 clusters. Deterministic (no bootstrap)."""
    cl = [(float(a), float(b)) for a, b in clusters]
    num, den = sum(a for a, _ in cl), sum(b for _, b in cl)
    if den <= 0:
        return None, None, None
    r = num / den
    g = len(cl)
    if g < 2:
        return r, None, None
    v = g / (g - 1) * sum((a - r * b) ** 2 for a, b in cl) / (den * den)
    half = z * math.sqrt(v)
    lo, hi = r - half, r + half
    if clip:
        lo, hi = max(clip[0], lo), min(clip[1], hi)
    return r, lo, hi


def _clusters(rows: list[dict]) -> dict[str, list[float]]:
    """ticker -> [fills, wins, sum(win - price), pnl, cost]."""
    out: dict[str, list[float]] = {}
    for r in rows:
        cy.checkpoint()
        c = out.setdefault(r.get("ticker") or r.get("fill_id") or str(id(r)), [0.0, 0.0, 0.0, 0.0, 0.0])
        w = 1.0 if r["result"] == "win" else 0.0
        c[0] += 1
        c[1] += w
        c[2] += w - r["price"]
        c[3] += r["_pnl"]
        c[4] += r["_cost"]
    return out


def cluster_win_ci(cl: list[list[float]]) -> tuple[float | None, float | None]:
    """Win-rate CI clustered by ticker, clipped to [0, 1]. When every market won (or every market lost) the
    cluster variance is 0 and the normal interval collapses to a point; then use the Wilson interval with the
    number of markets as the effective n, so 'no loss seen yet' is never read as certainty. The result is never
    narrower than the per-fill Wilson interval (clustering only widens)."""
    p, lo, hi = cluster_ratio_ci(((c[1], c[0]) for c in cl), clip=(0.0, 1.0))
    if p is None or lo is None or hi is None:
        return None, None
    if p <= 1e-12 or p >= 1 - 1e-12:
        lo, hi = wilson(round(p * len(cl)), len(cl))
    n, wins = int(sum(c[0] for c in cl)), int(round(sum(c[1] for c in cl)))
    wlo, whi = wilson(wins, n)                     # clustering may only WIDEN the per-fill Wilson interval
    return min(lo, wlo), max(hi, whi)


def clustered(rows: list[dict]) -> dict:
    """Win-rate, edge and ROI 95% CIs clustered by ticker (fills in one market are correlated).

    * win rate: cluster-robust ratio CI, never narrower than the per-fill Wilson CI (cluster_win_ci);
    * edge = win rate - avg price, so its CI is the (floored) win-rate CI minus avg price;
    * ROI: cluster-robust ratio CI of sum(pnl) / sum(cost), widened to include the ROI implied by the floored
      win-rate bounds, roi(w) = (w - avg_price) / avg_price - fees / cost. So an all-won group can't show a
      zero-width ROI range either.
    All None with fewer than 2 markets."""
    cl = list(_clusters(rows).values())
    wlo, whi = cluster_win_ci(cl)
    elo = ehi = rlo = rhi = None
    if wlo is not None and whi is not None and rows:
        avg = sum(r["price"] for r in rows) / len(rows)
        elo, ehi = wlo - avg, whi - avg
        _, rlo, rhi = cluster_ratio_ci((c[3], c[4]) for c in cl)
        cost = sum(r["_cost"] for r in rows)
        if avg > 0 and cost > 0 and rlo is not None:
            fee_share = sum(float(r.get("fee") or 0.0) for r in rows) / cost
            rlo = min(rlo, (wlo - avg) / avg - fee_share)
            rhi = max(rhi, (whi - avg) / avg - fee_share)
    return {"cluster_ci_low": _r(wlo, 4), "cluster_ci_high": _r(whi, 4), "edge_cluster_ci_low": _r(elo, 4),
            "edge_cluster_ci_high": _r(ehi, 4), "roi_ci_low": _r(rlo, 4), "roi_ci_high": _r(rhi, 4),
            "_markets": len(cl)}


def group_stats(key: str, rows: list[dict]) -> dict:
    """Group = {key, n, wins, contracts, cost, pnl, roi, avg_price, win_rate, win_ci_*, edge, edge_ci_*,
    cluster_ci_*, edge_cluster_ci_*, roi_ci_*}. Private `_markets` = distinct tickers (popped by callers)."""
    n = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    contracts = sum(r["count"] for r in rows)
    cost = sum(r["_cost"] for r in rows)
    pnl = sum(r["_pnl"] for r in rows)
    avg_price = sum(r["price"] for r in rows) / n if n else None
    win_rate = wins / n if n else None
    lo, hi = wilson(wins, n)
    return {"key": key, "n": n, "wins": wins, "contracts": _r(contracts, 2), "cost": _r(cost, 2),
            "pnl": _r(pnl, 2), "roi": _r(pnl / cost, 4) if cost > 0 else None, "avg_price": _r(avg_price, 4),
            "win_rate": _r(win_rate, 4), "win_ci_low": _r(lo, 4), "win_ci_high": _r(hi, 4),
            "edge": _r(win_rate - avg_price, 4) if n else None,
            "edge_ci_low": _r(lo - avg_price, 4) if n else None,
            "edge_ci_high": _r(hi - avg_price, 4) if n else None, **clustered(rows)}


def drawdown(cum: list[float]) -> list[float]:
    """cum - running max of cum (the running max starts at 0). Every value <= 0."""
    peak, out = 0.0, []
    for c in cum:
        peak = max(peak, c)
        out.append(c - peak)
    return out


# ---------------------------------------------------------------- fills -> trade rows
def settlement_map(settlements: Iterable[dict]) -> dict[str, dict]:
    out = {}
    for s in settlements:
        cy.checkpoint()
        if not isinstance(s, dict):
            continue
        t, r = s.get("ticker"), s.get("market_result") or s.get("result")
        ts = _ts(s.get("ts")) or _ts(s.get("settled_time"))
        if t and r in ("yes", "no") and ts is not None:
            out[str(t)] = {"result": r, "ts": ts, "close": _ts(s.get("close_time"))}
    return out


def episode_maps(episodes: Iterable[dict]) -> tuple[dict[str, dict], dict[str, bool]]:
    """(fill_id -> episode, first fill_id -> front_fill) from quote episodes."""
    by_fill: dict[str, dict] = {}
    front: dict[str, bool] = {}
    for e in episodes:
        cy.checkpoint()
        if not isinstance(e, dict):
            continue
        ids = [str(i) for i in (e.get("fill_ids") or []) if i]
        for i in ids:
            by_fill.setdefault(i, e)
        if ids and e.get("front_fill") is not None:
            front[ids[0]] = bool(e["front_fill"])          # front_fill is evaluated at the episode's first fill
    return by_fill, front


def env_ok(doc: dict, env: str, key: str = "env") -> bool:
    return env == "all" or not doc.get(key) or doc.get(key) == env


def trade_rows(fills: Iterable[dict], settlements: Iterable[dict], episodes: Iterable[dict], *, cells: Any,
               series_list: Iterable[str], env: str, journal: Iterable[dict] = (),
               jcells: dict[str, str] | None = None) -> list[dict]:
    """Contract trade rows (newest first by fill time) plus private `_ts`, `_settled`, `_pnl`, `_cost`, `_front`
    fields used by the aggregates (strip with public_trade). Fills that can't be read are skipped.
    `jcells` (order_id -> cell id) replaces `journal` when the caller already built the map."""
    smap = settlement_map(settlements)
    by_fill, front = episode_maps(episodes)
    jcells = jcells if jcells is not None else journal_cells(journal)
    cell_rows = parse_cells(cells)
    config_ids = {cell_id(c) for c in cell_rows}
    bands = price_bands(cell_rows)
    tbands = ttc_bands(cell_rows)
    series_list = list(series_list)
    out = []
    for f in fills:
        cy.checkpoint()
        if not isinstance(f, dict) or not env_ok(f, env):
            continue
        side = pnlsvc.fill_side(f)
        ts = _ts(f.get("ts")) or _ts(f.get("created_time"))
        price = pnlsvc.fill_price(f, side) if side else None
        ticker = str(f.get("ticker") or f.get("market_ticker") or "")
        fid = str(f.get("fill_id") or f.get("trade_id") or "")
        if side is None or ts is None or price is None or not ticker or not fid:
            continue
        count = pnlsvc.fill_count(f)
        fee = pnlsvc._f(f.get("fee_cost")) or 0.0
        cost = count * price
        kind = "taker" if f.get("is_taker") else "maker"
        st = smap.get(ticker)
        ep = by_fill.get(fid) or {}
        close = _ts(f.get("close_time")) or _ts(f.get("expiration_time")) or _ts(ep.get("close_time")) \
            or (st or {}).get("close")
        exact_close = close is not None
        if close is None and st:
            close = st["ts"]
        hours = (close - ts).total_seconds() / 3600.0 if close else None
        c = match_cell(price, hours, side, cell_rows) if exact_close else None
        cell = cell_id(c) if c else None
        if cell is None and kind == "maker":
            jc = jcells.get(str(f.get("order_id") or ""))
            if jc in config_ids:
                cell = jc
        if cell is None and not exact_close and hours is not None:
            c = match_cell(price, hours, side, cell_rows)
            cell = cell_id(c) if c else None
        pnl = pnlsvc.fill_pnl(f, st["result"]) if st else None
        fqd = f.get(fq.FIELD) if isinstance(f.get(fq.FIELD), dict) else {}
        is_front = front.get(fid)
        label = "front" if is_front else ("trade_through" if fqd.get("trade_through") is True else None)
        src, strat = fill_label(f)
        out.append({
            "fill_id": fid, "ts": iso(ts), "ticker": ticker, "series": qp.series_of(ticker, series_list),
            "side": side, "kind": kind, "price": round(price, 4), "count": round(count, 2), "fee": round(fee, 4),
            "cost": round(cost, 2), "band": price_band(price, bands), "ttc_hours": _r(hours, 2),
            "ttc_band": ttc_band(hours, tbands), "cell": cell,
            "status": "settled" if st else "open",
            "result": None if not st else ("win" if st["result"] == side else "loss"),
            "settled_utc": iso(st["ts"]) if st else None, "pnl": _r(pnl, 2), "fill_quality": label,
            "source": src, "strategy": strat,
            "_note": str(f.get("user_note"))[:200] if f.get("user_note") else None,
            "_ts": ts, "_settled": st["ts"] if st else None, "_pnl": pnl or 0.0, "_cost": cost,
            "_front": is_front, "_tt": fqd.get("trade_through") is True})
    out.sort(key=lambda r: (r["_ts"], r["fill_id"]), reverse=True)
    return out


def public_trade(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---------------------------------------------------------------- filtering (GET /analytics/trades)
TRADE_FILTERS = ("series", "side", "kind", "status", "band", "cell", "result", "source", "strategy")


def filter_trades(rows: list[dict], *, since: datetime, until: datetime | None = None,
                  **flt: Any) -> list[dict]:
    """Rows with since <= fill time < until (until optional) and every given (non-None) filter equal;
    `cell="none"` = no cell."""
    out = []
    for r in rows:
        if r["_ts"] < since or (until is not None and r["_ts"] >= until):
            continue
        ok = True
        for k in TRADE_FILTERS:
            v = flt.get(k)
            if v is None:
                continue
            have = r.get(k)
            if k == "cell" and v == CELL_NONE:
                ok = have is None
            elif have != v:
                ok = False
            if not ok:
                break
        if ok:
            out.append(r)
    return out


# ---------------------------------------------------------------- summary (GET /analytics/summary)
def _groups(rows: list[dict], keyf, order: Iterable[str] | None = None) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        cy.checkpoint()
        buckets.setdefault(keyf(r), []).append(r)
    keys = list(buckets)
    if order is not None:
        rank = {k: i for i, k in enumerate(order)}
        keys.sort(key=lambda k: (rank.get(k, len(rank)), k))
    else:
        keys.sort()
    out = []
    for k in keys:
        cy.checkpoint()
        g = group_stats(k, buckets[k])
        g.pop("_markets", None)
        out.append(g)
    return out


def pnl_curve(settled: list[dict], start: date, end: date, zone: tzinfo = timezone.utc) -> list[dict]:
    """Daily points (settlement date in `zone`, default UTC) from start to end inclusive (dates in the same zone);
    [] when nothing settled. Storage stays UTC; only the bucketing date moves."""
    if not settled:
        return []
    day: dict[date, list[float]] = {}
    for r in settled:
        cy.checkpoint()
        d = r["_settled"].astimezone(zone).date()
        m = day.setdefault(d, [0.0, 0.0])
        m[0 if r["kind"] == "maker" else 1] += r["_pnl"]
    dates, d = [], start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)
    cum, run = [], 0.0
    for d in dates:
        run += sum(day.get(d, (0.0, 0.0)))
        cum.append(run)
    dd = drawdown(cum)
    return [{"date": d.isoformat(), "pnl": round(sum(day.get(d, (0.0, 0.0))), 2), "cum_pnl": round(c, 2),
             "drawdown": round(x, 2) + 0.0, "maker_pnl": round(day.get(d, (0.0, 0.0))[0], 2),
             "taker_pnl": round(day.get(d, (0.0, 0.0))[1], 2)}
            for d, c, x in zip(dates, cum, dd)]


def _weeks(lo: datetime, now: datetime) -> list[str]:
    out, d = [], lo.date()
    while d <= now.date():
        w = iso_week(d)
        if w not in out:
            out.append(w)
        d += timedelta(days=1)
    return out


def fill_quality_weekly(rows: list[dict], episodes: Iterable[dict], *, lo: datetime, now: datetime,
                        env: str) -> list[dict]:
    """Maker fills (by fill time) and quote episodes (by join time) per ISO week; [] when there is neither."""
    makers = [r for r in rows if r["kind"] == "maker" and r["_ts"] >= lo]
    eps = []
    for e in episodes:
        cy.checkpoint()
        t = _ts(e.get("ts")) if isinstance(e, dict) else None
        if t is not None and t >= lo and env_ok(e, env):
            eps.append((t, e))
    if not makers and not eps:
        return []
    weeks = {w: {"week": w, "fills": 0, "front": 0, "trade_through": 0, "_known": 0, "_q": [], "requotes": 0}
             for w in _weeks(lo, now)}
    for r in makers:
        cy.checkpoint()
        w = weeks.get(iso_week(r["_ts"]))
        if w is None:
            continue
        w["fills"] += 1
        if r["_front"] is not None:
            w["_known"] += 1
            w["front"] += int(bool(r["_front"]))
        w["trade_through"] += int(r["_tt"])
    for t, e in eps:
        cy.checkpoint()
        w = weeks.get(iso_week(t))
        if w is None:
            continue
        q = qp._f(e.get("queue_ahead_at_join"))
        if q is not None:
            w["_q"].append(q)
        if e.get("outcome") == "repriced":
            w["requotes"] += 1
    out = []
    for w in weeks.values():
        known, q = w.pop("_known"), w.pop("_q")
        w["front_rate"] = round(w["front"] / known, 4) if known else None
        w["avg_queue_ahead"] = round(sum(q) / len(q), 2) if q else None
        out.append({k: w[k] for k in ("week", "fills", "front", "trade_through", "front_rate", "avg_queue_ahead",
                                      "requotes")})
    return out


def _arb_filled(d: dict) -> bool:
    return str(d.get("executed", "")).startswith("FILLED")


def arb_summary(docs: Iterable[dict], *, lo: datetime, env: str, zone: tzinfo = timezone.utc) -> dict:
    """{opportunities, executed, pnl, daily}. executed = FILLED or PARTIAL sets; pnl = guaranteed profit of FILLED
    sets at execution (same as /api/arb), null when none filled. `daily` is bucketed by the local date in `zone`."""
    mine = []
    for d in docs:
        cy.checkpoint()
        t = _ts(d.get("ts")) if isinstance(d, dict) else None
        if t is not None and t >= lo and env_ok(d, env, "mode"):
            mine.append((t, d))
    daily: dict[str, list[int]] = {}
    executed, filled, pnl = 0, 0, 0.0
    for t, d in mine:
        cy.checkpoint()
        ex = str(d.get("executed", "")).startswith(("FILLED", "PARTIAL"))
        executed += ex
        if _arb_filled(d):
            filled += 1
            pnl += pnlsvc._f(d.get("profit")) or 0.0
        day = daily.setdefault(t.astimezone(zone).date().isoformat(), [0, 0])
        day[0] += 1
        day[1] += ex
    return {"opportunities": len(mine), "executed": executed, "pnl": round(pnl, 2) if filled else None,
            "daily": [{"date": k, "opportunities": v[0], "executed": v[1]} for k, v in sorted(daily.items())]}


# ---------------------------------------------------------------- expectations + verdict (sprint ext. item 1)
def expectations_path() -> Path:
    """CELL_EXPECTATIONS_PATH (e.g. a Render secret file; SECURITY_REVIEW S-32) if set, else the git-ignored
    control_api/data/cell_expectations.json (see services/benchmarks.py)."""
    return bench.path()


def load_expectations(path: Path | str | None = None) -> dict:
    """{source, loaded, default, cells} from `path`, else expectations_path() (read-only benchmark data, NOT the
    strategy cells; private, so its contents are never logged). Missing/invalid file -> loaded False and no
    expectation at all (null ROI)."""
    doc = bench.load_doc(path or expectations_path())
    if doc is None:
        return {"source": "", "loaded": False, "default": dict(DEFAULT_EXPECTATION), "cells": {}}
    cells = doc.get("cells") if isinstance(doc.get("cells"), dict) else {}
    default = doc.get("default") if isinstance(doc.get("default"), dict) else DEFAULT_EXPECTATION
    return {"source": str(doc.get("source") or ""), "loaded": True, "default": dict(default),
            "cells": {str(k): v for k, v in cells.items() if isinstance(v, dict)}}


def _num_or_none(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def expectation_for(cell: str | None, exp: dict) -> tuple[float | None, float | None]:
    e = (exp.get("cells") or {}).get(cell) if cell else None
    if not isinstance(e, dict):
        e = exp.get("default") or DEFAULT_EXPECTATION
    return _num_or_none(e.get("win_rate")), _num_or_none(e.get("roi"))


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def verdict(g: dict, markets: int, exp_wr: float | None, exp_roi: float | None) -> tuple[str, str]:
    """too_early (< MIN_MARKETS markets) | below | above | on_track, using the ticker-clustered CI on the win rate
    when an expected win rate is known, else on ROI. Returns (verdict, one plain-English sentence)."""
    if markets < MIN_MARKETS:
        return "too_early", (f"Only {markets} market{'s' if markets != 1 else ''} so far; at least {MIN_MARKETS} "
                             f"are needed before comparing with the research.")
    if exp_wr is not None:
        what, val, lo, hi, exp = "Win rate", g.get("win_rate"), g.get("cluster_ci_low"), g.get("cluster_ci_high"), exp_wr
    elif exp_roi is not None:
        what, val, lo, hi, exp = "ROI", g.get("roi"), g.get("roi_ci_low"), g.get("roi_ci_high"), exp_roi
    else:
        return "on_track", "There is no research expectation for this cell, so nothing to compare with."
    if lo is None or hi is None or val is None:
        return "too_early", "Not enough independent markets to compute a confidence interval yet."
    if hi < exp:
        return "below", (f"{what} {_pct(val)} is below the research expectation of {_pct(exp)} "
                         f"(95% range {_pct(lo)} to {_pct(hi)}).")
    if lo > exp:
        return "above", (f"{what} {_pct(val)} is above the research expectation of {_pct(exp)} "
                         f"(95% range {_pct(lo)} to {_pct(hi)}).")
    return "on_track", (f"{what} {_pct(val)} is consistent with the research expectation of {_pct(exp)} "
                        f"(95% range {_pct(lo)} to {_pct(hi)}).")


# ---------------------------------------------------------------- calibration (item 3)
def cal_buckets(cells: Any) -> tuple[int, ...]:
    """Bucket lows in cents: CAL_STEP_C-wide from the lowest to the highest configured cell price (the last bucket
    includes the top price). Empty when no valid cells are configured."""
    bands = price_bands(cells)
    if not bands:
        return ()
    lo, top = min(b[0] for b in bands), max(b[1] for b in bands)
    return tuple(range(lo, top, CAL_STEP_C)) or (lo,)


def calibration(maker_settled: list[dict], cells: Any = None) -> list[dict]:
    """CAL_STEP_C-cent buckets over the configured cells' price span (last bucket includes the top price) of maker
    settled fills, CI clustered by ticker."""
    lows = cal_buckets(cells)
    buckets: dict[int, list[dict]] = {b: [] for b in lows}
    if lows:
        first, top = lows[0], max(b[1] for b in price_bands(cells))
        for r in maker_settled:
            cy.checkpoint()
            c = round(r["price"] * 100)
            if first <= c <= top:
                buckets[min(first + (c - first) // CAL_STEP_C * CAL_STEP_C, lows[-1])].append(r)
    out = []
    for b in lows:
        rows = buckets[b]
        n = len(rows)
        wins = sum(1 for r in rows if r["result"] == "win")
        cl = list(_clusters(rows).values())
        lo, hi = cluster_win_ci(cl)
        out.append({"bucket": f"{b / 100:.2f}-{(b + 1) / 100:.2f}", "lo": b / 100, "hi": (b + 2) / 100, "n": n,
                    "markets": len(cl), "wins": wins, "win_rate": _r(wins / n, 4) if n else None,
                    "avg_price": _r(sum(r["price"] for r in rows) / n, 4) if n else None,
                    "ci_low": _r(lo, 4), "ci_high": _r(hi, 4)})
    return out


# ---------------------------------------------------------------- fill rate from quote episodes (item 4)
def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def _fr(eps: list[dict]) -> dict:
    """FR: quotes = closed episodes = filled + unfilled; `partial` is the subset of `filled` where only part of the
    order filled; fill_rate = filled / quotes."""
    filled = partial = unfilled = 0
    rest, to_fill = [], []
    for e in eps:
        cy.checkpoint()
        got, cnt = qp._f(e.get("filled_count")) or 0.0, qp._f(e.get("count"))
        joined, closed, first = _ts(e.get("ts")), _ts(e.get("closed_utc")), _ts(e.get("first_fill_utc"))
        if got > 1e-9:
            filled += 1                                   # any fill (partial ones included)
            if cnt is not None and got < cnt - 1e-9:
                partial += 1                              # ...of which only part of the order filled
            if joined and first:
                to_fill.append(max(0.0, (first - joined).total_seconds() / 60))
        else:
            unfilled += 1                                 # closed with no fill (expired/cancelled/repriced/stopped)
        if joined and closed:
            rest.append(max(0.0, (closed - joined).total_seconds() / 60))
    q = len(eps)
    return {"quotes": q, "filled": filled, "partial": partial, "unfilled": unfilled,
            "fill_rate": round(filled / q, 4) if q else None,
            "median_rest_minutes": _median(rest), "median_minutes_to_fill": _median(to_fill)}


def fill_rate(episodes: Iterable[dict], *, lo: datetime, env: str, cells: Any, jcells: dict[str, str],
              series_list: Iterable[str]) -> dict:
    """quotes = CLOSED episodes whose end (first fill, else close, else join) is in the window. Cell from the
    journal quote row of the order, else from price + close time at join (if the episode recorded it)."""
    cell_rows = parse_cells(cells)
    series_list = list(series_list)
    mine = []
    for e in episodes:
        cy.checkpoint()
        if not isinstance(e, dict) or e.get("status") != "closed" or not env_ok(e, env):
            continue
        t = _ts(e.get("first_fill_utc")) or _ts(e.get("closed_utc")) or _ts(e.get("ts"))
        if t is None or t < lo:
            continue
        cell = jcells.get(str(e.get("order_id") or ""))
        if cell is None:
            joined, close = _ts(e.get("ts")), _ts(e.get("close_time"))
            side = e.get("side") if e.get("side") in ("yes", "no") else None
            hours = (close - joined).total_seconds() / 3600 if joined and close else None
            c = match_cell(qp._f(e.get("price")), hours, side, cell_rows)
            cell = cell_id(c) if c else None
        series = e.get("series") or qp.series_of(str(e.get("ticker") or ""), series_list)
        mine.append((cell or CELL_NONE, str(series or ""), e))
    order = [cell_id(c) for c in cell_rows] + [CELL_NONE]
    rank = {k: i for i, k in enumerate(order)}
    by_cell: dict[str, list[dict]] = {}
    by_series: dict[str, list[dict]] = {}
    for c, s_, e in mine:
        cy.checkpoint()
        by_cell.setdefault(c, []).append(e)
        by_series.setdefault(s_, []).append(e)
    return {"totals": _fr([e for _, _, e in mine]),
            "by_cell": [{"key": k, **_fr(by_cell[k])} for k in sorted(by_cell, key=lambda k: (rank.get(k, 99), k))],
            "by_series": [{"key": k, **_fr(by_series[k])} for k in sorted(by_series)]}


# ---------------------------------------------------------------- CSV export (item 7)
_CSV_DANGER = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v: Any) -> Any:
    """Neutralise spreadsheet formulas: a STRING starting with = + - @ (or tab / CR) gets a leading apostrophe.
    Numbers are written as numbers (a negative P&L stays numeric; it cannot carry a formula)."""
    if isinstance(v, str) and v.startswith(_CSV_DANGER):
        return "'" + v
    return v


def trades_csv(trades: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(TRADE_COLUMNS)
    for t in trades:
        w.writerow(["" if t.get(k) is None else csv_safe(t.get(k)) for k in TRADE_COLUMNS])
    return buf.getvalue()


def _in(ts: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    return ts is not None and (start is None or ts >= start) and (end is None or ts < end)


def summary(rows: list[dict], episodes: list[dict], arb_docs: list[dict], *, days: int, env: str,
            now: datetime, cells: Any, truncated: bool = False, expectations: dict | None = None,
            jcells: dict[str, str] | None = None, series_list: Iterable[str] = (),
            period: dict | None = None, tz: str | None = None) -> dict:
    """`period` ({id, name, started_at, ended_at}; datetimes or None) switches to period mode: only fills filled
    inside [started_at, ended_at) count (attribution by FILL date) and each one is settled whenever its market
    settled, instead of the rolling `days` window by settlement date.

    `tz` (an IANA name; unknown or malformed means UTC, see pnl.resolve_tz) moves only the DATE BUCKETS (P&L curve,
    arbitrage daily rows, "today") to that zone; the rolling window edges and all storage stay UTC. The zone used
    is returned as `tz_used`."""
    zone, tzname = pnlsvc.resolve_tz(tz)
    lo = now - timedelta(days=days)
    p0 = p1 = None
    arb_all = list(arb_docs)
    if period is not None:
        p0, p1 = period.get("started_at"), period.get("ended_at")
        lo = max(p0, now - timedelta(days=max(DAYS_ALLOWED))) if p0 else now - timedelta(days=max(DAYS_ALLOWED))
        rows = [r for r in rows if _in(r["_ts"], p0, p1)]
        episodes = [e for e in episodes if _in(_ts(e.get("ts")), p0, p1)]
        arb_docs = [d for d in arb_docs if _in(_ts(d.get("ts")), p0, p1)]
    rows = [r for r in rows if r.get("source") != "you"]   # Y1: manual trades are never a bot statistic
    settled = [r for r in rows if r["_settled"] is not None and (period is not None or lo <= r["_settled"])
               and r["_settled"] <= now]
    maker = [r for r in settled if r["kind"] == "maker"]
    n = len(settled)
    wins = sum(1 for r in settled if r["result"] == "win")
    cost = sum(r["_cost"] for r in settled)
    pnl = sum(r["_pnl"] for r in settled)
    curve = pnl_curve(settled, lo.astimezone(zone).date(), now.astimezone(zone).date(), zone)
    config_order = [cell_id(c) for c in parse_cells(cells)]
    exp = expectations if expectations is not None else load_expectations()
    cell_rows: dict[str, list[dict]] = {}
    for r in maker:
        cy.checkpoint()
        cell_rows.setdefault(r["cell"] or CELL_NONE, []).append(r)
    by_cell = _groups(maker, lambda r: r["cell"] or CELL_NONE, order=config_order + [CELL_NONE])
    for g in by_cell:
        g["cell"] = None if g["key"] == CELL_NONE else g["key"]
        g["in_config"] = g["key"] in config_order
        g["markets"] = len(_clusters(cell_rows.get(g["key"], [])))
        g["expected_win_rate"], g["expected_roi"] = expectation_for(g["cell"], exp)
        g["verdict"], g["verdict_reason"] = verdict(g, g["markets"], g["expected_win_rate"], g["expected_roi"])
    out = {
        "env": env, "days": days, "generated_utc": iso(now), "truncated": bool(truncated),
        "totals": {"settled_fills": n, "contracts": _r(sum(r["count"] for r in settled), 2), "cost": round(cost, 2),
                   "pnl": round(pnl, 2), "roi": _r(pnl / cost, 4) if cost > 0 else None,
                   "win_rate": _r(wins / n, 4) if n else None,
                   "open_fills": sum(1 for r in rows if r["_settled"] is None and r["_ts"] >= lo),
                   "max_drawdown": min((p["drawdown"] for p in curve), default=0.0)},
        "pnl_curve": curve,
        "by_cell": by_cell,
        "by_series": _groups(maker, lambda r: r["series"]),
        "by_band": _groups(maker, lambda r: r["band"], order=band_labels(cells) + (BAND_OTHER,)),
        "by_ttc": _groups(maker, lambda r: r["ttc_band"] or TTC_UNKNOWN, order=ttc_labels(cells) + (TTC_UNKNOWN,)),
        "fill_quality": fill_quality_weekly(rows, episodes, lo=lo, now=now, env=env),
        "arb": arb_summary(arb_docs, lo=lo, env=env, zone=zone),
        "expectations": {"source": exp.get("source") or "", "loaded": bool(exp.get("loaded"))},
        "calibration": calibration(maker, cells),
        "fill_rate": fill_rate(episodes, lo=lo, env=env, cells=cells, jcells=jcells or {}, series_list=series_list),
    }
    out["tz_used"] = tzname
    # per-strategy win rate (never pooled): maker per FILL, arbitrage per SET (hedged legs are not independent
    # trials: a NO set always has losing legs)
    arb_rows = [r for r in rows if r.get("strategy") == "arb"]
    sets_all = arbsets.build_sets(arb_all, arb_rows, env=env, since=p0 if period is not None else lo,
                                  until=p1 if period is not None else None, limit=arbsets.MAX_SETS * 5)
    sstats = arbsets.set_stats(sets_all)
    out["win_rate_by_strategy"] = {
        "maker": _rate("fill", sum(1 for r in maker if r["result"] == "win"), len(maker)),
        "arb": _rate("set", sstats["wins"], sstats["settled"])}
    out["deprecated"] = {"totals.win_rate": "pools maker fills and arbitrage legs; use win_rate_by_strategy"}
    out["arb_summary"] = arbsets.summary(arb_all, arb_rows, env=env, now=now, zone=zone, tzname=tzname,
                                         since=p0 if period is not None else lo,
                                         until=p1 if period is not None else None,
                                         label=(period or {}).get("name") or f"last {days} days")
    out["arb_sets"] = sets_all[:100]
    out["arb_sets_total"] = len(sets_all)
    if period is not None:
        out["period"] = {"id": period.get("id"), "name": period.get("name"), "started_at": iso(p0),
                         "ended_at": iso(p1)}
    return out


def _rate(unit: str, wins: int, n: int) -> dict:
    lo, hi = wilson(wins, n)
    return {"unit": unit, "n": n, "wins": wins, "losses": n - wins, "win_rate": _r(wins / n, 4) if n else None,
            "ci_low": _r(lo, 4), "ci_high": _r(hi, 4), "method": "wilson95"}


def _totals(settled: list[dict], open_n: int) -> dict:
    n = len(settled)
    cost = sum(r["_cost"] for r in settled)
    pnl = sum(r["_pnl"] for r in settled)
    wins = sum(1 for r in settled if r["result"] == "win")
    return {"settled_fills": n, "cost": round(cost, 2), "pnl": round(pnl, 2),
            "roi": _r(pnl / cost, 4) if cost > 0 else None, "win_rate": _r(wins / n, 4) if n else None,
            "open_fills": open_n}


def my_trades(rows: list[dict], *, days: int, env: str, now: datetime, limit: int = 200,
              period: dict | None = None, tz: str | None = None) -> dict:
    """The owner's own trades (source "you", Y5) beside the bot: totals, daily P&L curve, newest trades with notes.
    Manual rows never feed the bot summary (summary() drops them); bot totals here are for the side-by-side line.
    `period` (see summary) attributes by fill date instead of the rolling settlement window. `tz` as in summary."""
    zone, tzname = pnlsvc.resolve_tz(tz)
    lo = now - timedelta(days=days)
    if period is not None:
        p0, p1 = period.get("started_at"), period.get("ended_at")
        lo = max(p0, now - timedelta(days=max(DAYS_ALLOWED))) if p0 else now - timedelta(days=max(DAYS_ALLOWED))
        rows = [r for r in rows if _in(r["_ts"], p0, p1)]
    mine = [r for r in rows if r.get("source") == "you"]
    bot = [r for r in rows if r.get("source") != "you"]

    def settled(rs):
        return [r for r in rs if r["_settled"] is not None and (period is not None or lo <= r["_settled"])
                and r["_settled"] <= now]
    ms, bs = settled(mine), settled(bot)
    curve = [{"date": p["date"], "pnl": p["pnl"], "cum_pnl": p["cum_pnl"], "drawdown": p["drawdown"]}
             for p in pnl_curve(ms, lo.astimezone(zone).date(), now.astimezone(zone).date(), zone)]
    recent = [r for r in mine if r["_ts"] >= lo]
    trades = [dict(public_trade(r), note=r.get("_note")) for r in recent[:max(0, limit)]]
    return {"env": env, "days": days, "generated_utc": iso(now),
            "totals": _totals(ms, sum(1 for r in recent if r["_settled"] is None)),
            "bot": _totals(bs, sum(1 for r in bot if r["_settled"] is None and r["_ts"] >= lo)),
            "pnl_curve": curve, "trades": trades, "total_trades": len(recent), "tz_used": tzname}


def summarize(*, fills: Iterable[dict], settlements: Iterable[dict], episodes: Iterable[dict] = (),
              arbs: Iterable[dict] = (), config: dict | None = None, now: datetime, days: int, env: str,
              journal: Iterable[dict] = (), series_list: Iterable[str] | None = None,
              truncated: bool = False, tz: str | None = None) -> dict:
    """One-call summary from raw store docs (used by tools/pull_data.py). `config` supplies the read-only `cells`
    and `series`; `series_list` defaults to config series. Same output as GET /api/analytics/summary."""
    cfg = config or {}
    eps = list(episodes)
    sl = list(series_list if series_list is not None else (cfg.get("series") or []))
    jc = journal_cells(journal)
    rows = trade_rows(fills, settlements, eps, cells=cfg.get("cells"), series_list=sl, env=env, jcells=jc)
    return summary(rows, eps, list(arbs), days=days, env=env, now=now, cells=cfg.get("cells"), truncated=truncated,
                   jcells=jc, series_list=sl, tz=tz)

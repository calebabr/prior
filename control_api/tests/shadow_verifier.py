"""Independent recomputation of the shadow headline from raw episodes (Sprint 2A-3 verifier).

Written from the sprint contract and `docs/SHADOW_CONTRACT.md` only. It does NOT import `shadow_model` or
`services.shadow`: every number here is derived again from the episode fields, so `test_shadow_verifier.py` can
compare the two implementations. Pure functions, no I/O, standard library only.

Rules implemented (contract wording in brackets):
- fills [F4]: not through: lo = clamp(traded - q0), hi = clamp(traded - min(q0, level_min)), mid uses the average
  of those two queue sizes; through with a known through volume: hi = count, lo = clamp(traded + through_vol - q0),
  mid = (lo + hi) / 2; through with through_vol None (old episode): count in all three.
- fees [F7]: maker = ceil(0.0175 * n * p * (1 - p) * 100) / 100 on the filled contracts n; zero = 0. An episode's
  own `fee_mode` (None = maker) drives the headline; `headline_zero_fee` is zero fee everywhere.
- scored episode: not capped, result yes/no, a fill estimate. Capped episodes count only in `capped_episodes`.
- win rates [F2]: break-even = sum(mid fill x price) / sum(mid fill); observed = sum(mid fill x win) / sum(mid fill).
- peak held capital [F5]: sweep of mid fill x price over [through_ts if through else end_ts, settled_ts).
- peak resting bids: sweep of the quoted cost over [join_ts, end_ts or close_time) for non-capped episodes.
- first fill per market [F1]: earliest-joined episode with a mid fill > 0 per (ticker, side).
- days [F6]: key = UTC date of settled_ts (else close_time); `days_covered` counts days with a filled episode;
  too early while days_covered < 7 or central fills < 30.
- caps [F1]: `cap_violations` checks a cycle-by-cycle episode history against the limits.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Iterable

SCEN = ("lo", "mid", "hi")
MIN_DAYS = 7
GATE_MIN_EPISODES = 30
CAP_REASONS = ("series_cap", "market_cap", "open_cap", "run_budget")


# ---------------------------------------------------------------- per episode
def _clamp(x: float, count: float) -> float:
    return float(min(max(x, 0.0), count))


def fill_scenarios(q0: float, level_min: float, traded_vol: float, through: bool, count: float,
                   through_vol: float | None = None) -> dict:
    count = float(count)
    if through:
        if through_vol is None:
            return {"lo": count, "mid": count, "hi": count}
        lo = _clamp(traded_vol + through_vol - q0, count)
        return {"lo": lo, "mid": (lo + count) / 2.0, "hi": count}
    a_min = min(q0, level_min)
    a_mid = (q0 + a_min) / 2.0
    return {"lo": _clamp(traded_vol - q0, count), "mid": _clamp(traded_vol - a_mid, count),
            "hi": _clamp(traded_vol - a_min, count)}


def fee(n: float, price: float, mode: str | None) -> float:
    if mode == "zero" or n <= 0:
        return 0.0
    raw = 0.0175 * n * price * (1.0 - price) * 100.0
    return math.ceil(round(raw, 9)) / 100.0          # round(.., 9): no float-noise cent (1.5750000001 -> 1.58)


def ep_mode(ep: dict) -> str:
    return "zero" if ep.get("fee_mode") == "zero" else "maker"


def win(ep: dict) -> int:
    return 1 if ep.get("result") == ep.get("side") else 0


def ep_pnl(ep: dict, mode: str) -> dict:
    f, p, w = ep["fill"], float(ep["price"]), win(ep)
    return {s: f[s] * (w - p) - fee(f[s], p, mode) for s in SCEN}


def is_scored(ep: dict) -> bool:
    return (not ep.get("capped")) and ep.get("result") in ("yes", "no") and ep.get("fill") is not None


def day_of(ep: dict) -> str | None:
    t = ep.get("settled_ts") or ep.get("close_time")
    return t.date().isoformat() if isinstance(t, datetime) else None


# ---------------------------------------------------------------- aggregates
def _sweep(intervals: Iterable[tuple[datetime, datetime, float]]) -> float:
    """Peak of the running sum over half-open [start, end) intervals (an end at t is applied before a start at t)."""
    ev = []
    for a, b, w in intervals:
        if a is None or b is None or w <= 0 or b <= a:
            continue
        ev.append((a, 1, w))
        ev.append((b, 0, -w))
    run = peak = 0.0
    for _, _, w in sorted(ev, key=lambda e: (e[0], e[1])):
        run += w
        peak = max(peak, run)
    return peak


def _ratio(a: float, b: float) -> float | None:
    return a / b if b > 0 else None


def _band(d: dict) -> dict:
    """A dollar range: a losing bid loses more when it fills more, so lo / hi are the min / max of the three
    scenarios (keeps lo <= mid <= hi). Same convention as the API contract's ranges."""
    v = [d[s] for s in SCEN]
    return {"lo": min(v), "mid": d["mid"], "hi": max(v)}


def _roi(pnl: dict, cost: dict) -> dict:
    """Per-day return: a scenario with no cost has no return; lo / hi are the min / max of the defined ones;
    all null when the central scenario has no cost."""
    r = {s: _ratio(pnl[s], cost[s]) for s in SCEN}
    if r["mid"] is None:
        return {s: None for s in SCEN}
    vals = [v for v in r.values() if v is not None]
    return {"lo": min(vals), "mid": r["mid"], "hi": max(vals)}


def _roi_headline(pnl: dict, cost: dict) -> dict:
    """Headline ROI: the empty-summary shape reports 0.0 (not null) for a scenario with no cost, then lo / hi are
    the min / max of the three. (Differs from the per-day return, which drops a no-cost scenario: see report.)"""
    return _band({s: (pnl[s] / cost[s] if cost[s] > 0 else 0.0) for s in SCEN})


def _headline(scored: list[dict], fee_of) -> dict:
    pnl = {s: 0.0 for s in SCEN}
    cost = {s: 0.0 for s in SCEN}
    fills = {s: 0.0 for s in SCEN}
    count = 0.0
    for e in scored:
        pe = ep_pnl(e, fee_of(e))
        for s in SCEN:
            pnl[s] += pe[s]
            cost[s] += e["fill"][s] * float(e["price"])
            fills[s] += e["fill"][s]
        count += float(e["count"])
    filled = [e for e in scored if e["fill"]["mid"] > 0]
    fm = sum(e["fill"]["mid"] for e in filled)
    held = _sweep(((e.get("through_ts") if e.get("through") and e.get("through_ts") else e.get("end_ts")),
                   e.get("settled_ts"), e["fill"]["mid"] * float(e["price"])) for e in filled)
    first: dict[tuple, dict] = {}
    for e in sorted(filled, key=lambda e: e["join_ts"]):
        first.setdefault((e.get("ticker"), e.get("side")), e)
    ff_cost = sum(e["fill"]["mid"] * float(e["price"]) for e in first.values())
    ff_pnl = sum(ep_pnl(e, fee_of(e))["mid"] for e in first.values())
    return {
        "pnl": _band(pnl), "roi": _roi_headline(pnl, cost), "cost": cost,
        "fill_rate": {s: (fills[s] / count if count > 0 else 0.0) for s in SCEN},
        "break_even_win_rate": _ratio(sum(e["fill"]["mid"] * float(e["price"]) for e in filled), fm),
        "observed_win_rate": _ratio(sum(e["fill"]["mid"] * win(e) for e in filled), fm),
        "n_settled_fills": len(filled),
        "peak_held_capital": held,
        "return_on_held": _ratio(pnl["mid"], held),
        "first_fill_per_market": {"cost": ff_cost, "pnl": ff_pnl, "roi": _ratio(ff_pnl, ff_cost),
                                  "markets": len(first)},
    }


def _days(scored: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for e in scored:
        d = day_of(e)
        row = by.setdefault(d, {"day": d, "episodes": 0, "filled_episodes": 0,
                                "cost": {s: 0.0 for s in SCEN}, "pnl": {s: 0.0 for s in SCEN}})
        row["episodes"] += 1
        row["filled_episodes"] += 1 if e["fill"]["mid"] > 0 else 0
        pe = ep_pnl(e, ep_mode(e))
        for s in SCEN:
            row["cost"][s] += e["fill"][s] * float(e["price"])
            row["pnl"][s] += pe[s]
    out = []
    for d in sorted(by):
        row = by[d]
        if row["filled_episodes"] > 0:
            row["return"] = _roi(row["pnl"], row["cost"])
        else:
            row["return"] = {s: None for s in SCEN}
        row["pnl_scen"] = dict(row["pnl"])
        row["pnl"] = _band(row["pnl"])
        out.append(row)
    return out


def _day_stats(days: list[dict]) -> dict:
    filled = [d for d in days if d["filled_episodes"] > 0]
    rets = [d["return"]["mid"] for d in filled if d["return"]["mid"] is not None]
    total = sum(d["pnl"]["mid"] for d in days)
    by_ret = sorted((d for d in filled if d["return"]["mid"] is not None), key=lambda d: d["return"]["mid"])
    pnls = sorted((d["pnl"]["mid"] for d in days), reverse=True)

    def pick(d):
        return None if d is None else {"day": d["day"], "return_mid": d["return"]["mid"], "pnl_mid": d["pnl"]["mid"]}

    return {
        "event_days": len(days), "days_with_fills": len(filled),
        "filled_episodes": sum(d["filled_episodes"] for d in days),
        "mean_return_mid": statistics.fmean(rets) if rets else None,
        "sd_return_mid": statistics.stdev(rets) if len(rets) >= 2 else None,
        "worst_day": pick(by_ret[0] if by_ret else None), "best_day": pick(by_ret[-1] if by_ret else None),
        # two readings of "best day P&L share": the largest day P&L, and the best-RETURN day's P&L
        "best_day_pnl_share": (pnls[0] / total) if total > 0 and pnls else None,
        "best_return_day_pnl_share": (by_ret[-1]["pnl"]["mid"] / total) if total > 0 and by_ret else None,
        "top3_pnl_share": (sum(pnls[:3]) / total) if total > 0 else None,
    }


def recompute(episodes: Iterable[dict], now: datetime | None = None) -> dict:
    """The whole headline, independently. Returns plain floats (unrounded)."""
    eps = [e for e in episodes if isinstance(e, dict)]
    scored = [e for e in eps if is_scored(e)]
    capped = [e for e in eps if e.get("capped")]
    days = _days(scored)
    days_covered = sum(1 for d in days if d["filled_episodes"] > 0)
    central = sum(e["fill"]["mid"] for e in scored)
    filled_eps = sum(1 for e in scored if e["fill"]["mid"] > 0)     # the owner-approved gate: 7 filled days and 30 filled episodes
    too_early = days_covered < MIN_DAYS or filled_eps < GATE_MIN_EPISODES
    maker = _headline(scored, ep_mode)
    zero = _headline(scored, lambda e: "zero")
    resting = _sweep((e.get("join_ts"), e.get("end_ts") or e.get("close_time") or now,
                      float(e.get("cost") if e.get("cost") is not None else e["count"] * e["price"]))
                     for e in eps if not e.get("capped"))
    for h in (maker, zero):
        h["peak_capital"] = resting
        h["ci_reliable"] = not too_early
        h["headline_status"] = "too_early" if too_early else "ok"

    def group(key):
        g: dict[Any, dict] = {}
        for e in scored:
            k = key(e)
            row = g.setdefault(k, {"episodes": 0, "fills_mid": 0.0, "pnl": {s: 0.0 for s in SCEN}})
            row["episodes"] += 1
            row["fills_mid"] += e["fill"]["mid"]
            pe = ep_pnl(e, ep_mode(e))
            for s in SCEN:
                row["pnl"][s] += pe[s]
        for row in g.values():
            row["pnl"] = _band(row["pnl"])
        return g

    cum = {s: 0.0 for s in SCEN}
    daily = []
    for d in days:
        for s in SCEN:
            cum[s] += d["pnl_scen"][s]
        b = _band(cum)
        daily.append({"day": d["day"], "cum_lo": b["lo"], "cum_mid": b["mid"], "cum_hi": b["hi"]})
    modes = {"maker": 0, "zero": 0}
    for e in scored:
        modes[ep_mode(e)] += 1
    ds = _day_stats(days)
    return {
        "headline": maker, "headline_zero_fee": zero,
        "status": {"days_covered": days_covered, "settled_days": len(days), "settled_episodes": len(scored),
                   "central_fills": central, "too_early": too_early,
                   "headline_status": "too_early" if too_early else "ok", "capped_episodes": len(capped)},
        "by_series": {k: v for k, v in group(lambda e: e.get("series")).items()},
        "by_cell": {tuple(k) if k is not None else None: v for k, v in group(lambda e: tuple(e.get("cell") or ())).items()},
        "daily": daily, "by_event_day": days, "day_stats": ds,
        "reliability_note_expected": ds["days_with_fills"] < MIN_DAYS or ds["filled_episodes"] < GATE_MIN_EPISODES,
        "fee_modes": modes, "fee_mode": "per_series" if modes["zero"] else "maker", "day_basis": "close_day",
    }


# ---------------------------------------------------------------- F1 cap invariants
def held_cost(ep: dict) -> float:
    """Ended, unsettled, not capped: mid fill x price; an unknown fill counts in full."""
    if ep.get("capped") or ep.get("end_ts") is None or ep.get("result") is not None:
        return 0.0
    f = ep.get("fill")
    if f is None:
        return float(ep["count"]) * float(ep["price"])
    return float(f["mid"]) * float(ep["price"])


def resting_cost(ep: dict) -> float:
    if ep.get("capped") or ep.get("end_ts") is not None:
        return 0.0
    return float(ep["count"]) * float(ep["price"])


def cap_violations(episodes: Iterable[dict], *, capital: float, max_open_frac: float, run_budget: float,
                   series: list[str], series_cap: float | None, tol: float = 0.005 + 1e-6) -> list[str]:
    """Invariants after one cycle, from the episode states alone (independent of the pass doc):
    held + resting <= ceiling; resting <= run budget and <= capital - held; resting per series <= headroom / n;
    held + resting per series <= series cap x ceiling (a series at or over it rests nothing new); capped episodes carry a reason, a zero fill and are read."""
    eps = list(episodes)
    ceiling = max_open_frac * capital
    held = sum(held_cost(e) for e in eps)
    rest = sum(resting_cost(e) for e in eps)
    head = max(0.0, min(run_budget, ceiling - held, capital - held))
    out = []
    if held + rest > ceiling + tol:
        out.append(f"held {held:.4f} + resting {rest:.4f} > ceiling {ceiling:.4f}")
    if rest > head + tol:
        out.append(f"resting {rest:.4f} > headroom {head:.4f}")
    per = head / max(1, len(series))
    for s in series:
        hs = sum(held_cost(e) for e in eps if e.get("series") == s)
        rs = sum(resting_cost(e) for e in eps if e.get("series") == s)
        if rs > per + tol:
            out.append(f"series {s}: resting {rs:.4f} > share {per:.4f}")
        # series room: a bid is sized to limit x ceiling minus the series' held cost and resting bids, so held plus
        # resting stays within it, and a series already at or over it rests nothing
        if series_cap is not None and rs > tol and hs + rs > series_cap * ceiling + tol:
            out.append(f"series {s}: held {hs:.4f} + resting {rs:.4f} > cap {series_cap * ceiling:.4f}")
    for e in eps:
        if e.get("capped"):
            if e.get("cap_reason") not in CAP_REASONS:
                out.append(f"capped {e.get('ep_id')}: reason {e.get('cap_reason')!r}")
            if e.get("fill") != {"lo": 0.0, "mid": 0.0, "hi": 0.0} or e.get("trades_read") is not True:
                out.append(f"capped {e.get('ep_id')}: fill {e.get('fill')} trades_read {e.get('trades_read')}")
            if e.get("pnl") is not None:
                out.append(f"capped {e.get('ep_id')}: scored")
    return out


def series_funding_violations(snapshots: Iterable[tuple[datetime, dict]], final: dict, *, ceiling: float,
                              series_cap: float | None, tol: float = 0.005 + 1e-6) -> list[str]:
    """The series version of the economic invariant, with hindsight (see `funding_violations`): at every cycle time
    the positions actually held in one series plus its resting bids never exceed series_cap x ceiling. Counted from
    the stored episodes alone, so it checks the shadow's series room independently of its own bookkeeping."""
    if series_cap is None:
        return []
    cap = series_cap * ceiling
    out = []
    for k, (t, eps) in enumerate(snapshots):
        tot: dict[str, float] = {}
        for eid, e in eps.items():
            if e.get("capped"):
                continue
            s = e.get("series")
            if e.get("end_ts") is None:
                tot[s] = tot.get(s, 0.0) + resting_cost(e)
                continue
            f = final.get(eid, e)
            st = f.get("settled_ts")
            if f.get("result") is not None and st is not None and st <= t:
                continue
            fill = f.get("fill")
            tot[s] = tot.get(s, 0.0) + (float(e["count"]) * float(e["price"]) if fill is None
                                        else float(fill["mid"]) * float(e["price"]))
        for s, v in tot.items():
            if v > cap + tol:
                out.append(f"cycle {k} ({t:%H:%M}): series {s} held + resting {v:.4f} > cap {cap:.4f}")
    return out


def funding_violations(snapshots: Iterable[tuple[datetime, dict]], final: dict, *, ceiling: float,
                       tol: float = 0.005 + 1e-6) -> list[str]:
    """The economic invariant, with hindsight: at every cycle time t, the positions actually held (episodes ended
    by t and not settled by t, valued at their FINAL mid fill, unknown counted in full) never exceed the
    open-exposure ceiling, and neither do held + resting bids. `snapshots`: (t, {ep_id: state after the cycle});
    `final`: {ep_id: last state}."""
    out = []
    for k, (t, eps) in enumerate(snapshots):
        held = rest = 0.0
        for eid, e in eps.items():
            if e.get("capped"):
                continue
            if e.get("end_ts") is None:
                rest += resting_cost(e)
                continue
            f = final.get(eid, e)
            st = f.get("settled_ts")
            if f.get("result") is not None and st is not None and st <= t:
                continue
            fill = f.get("fill")
            held += float(e["count"]) * float(e["price"]) if fill is None else float(fill["mid"]) * float(e["price"])
        if held > ceiling + tol:
            out.append(f"cycle {k} ({t:%H:%M}): held {held:.4f} > ceiling {ceiling:.4f}")
        if held + rest > ceiling + tol:
            out.append(f"cycle {k} ({t:%H:%M}): held {held:.4f} + resting {rest:.4f} > ceiling {ceiling:.4f}")
    return out

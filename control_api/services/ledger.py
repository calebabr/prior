"""Honest Ledger and functional scorecard (UI-1a). Read-only, pure functions over trade rows and plain store docs.

Ledger (GET /api/analytics/ledger): ONE reconciled table. Attribution is by FILL date (the local day of the fill in the
viewer's zone), so every fill appears once and its realized P&L follows the fill, exactly as the period summary does.
Per strategy: fills, cost, settled fills (wins / losses per fill; arbitrage ALSO per SET, because a hedged set always
has losing legs), settled cost, realized P&L (revenue - cost - fees, from the stored settlements), open fills and open
cost (valued at cost, no mark-to-market). A second small table gives realized P&L by SETTLEMENT day, and `settled_by_strategy` gives the same
settlement-date attribution per strategy (every row settled inside the window, whenever it was filled; no row cap).
Home uses `settled_by_strategy` so its rows add up to its settlement-date headline.

Reconciliation (model `pair_time_v2`): equity (cash + exposure from account snapshots) change over the window against
what the books explain:
    expected = net cash flows + realized P&L (settled in the window)
               + fees of fills SETTLED in the window - fees of fills FILLED in the window      (fee_timing)
               + pair P&L formed in the window - pair P&L of the markets settled in the window  (pair_timing)
A fee leaves the cash when the fill happens, but fill_pnl books it when the market settles; the two fee terms undo that
timing. Kalshi also credits $1 to cash per matched YES / NO pair the moment the pair FORMS (the pair's cost leaves
exposure then), while fill_pnl books that profit or loss at settlement; pair_timing moves it to the formation time
(pairs per market in fill order, min(yes, no) matched contracts at average cost per side). residual = equity change -
expected; flag = |residual| > tolerance. Anything that cannot be computed from stored data is null and explained in
`notes`; nothing is invented.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable

from . import arbsets
from . import pnl as pnlsvc

MODEL = "pair_time_v2"
TOLERANCE = 0.10                       # dollars; fee rounding and un-booked pair gains stay inside it
STALE_GRACE = timedelta(hours=24)      # a fill whose market closed longer ago than this should have settled
LATE_GRACE = timedelta(hours=6)        # "open past expected settlement" grace after the market close
DAILY_MAX = 400


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _group(r: dict) -> str:
    if r.get("source") == "you":
        return "you"
    return r.get("strategy") or ("arb" if r.get("kind") == "taker" else "maker")


def _blank() -> dict:
    return {"fills": 0, "contracts": 0.0, "cost": 0.0, "fees": 0.0, "settled_fills": 0, "wins": 0, "losses": 0,
            "settled_cost": 0.0, "realized_pnl": 0.0, "open_fills": 0, "open_cost": 0.0}


def _add(b: dict, r: dict) -> None:
    b["fills"] += 1
    b["contracts"] += float(r.get("count") or 0)
    b["cost"] += r["_cost"]
    b["fees"] += float(r.get("fee") or 0)
    if r["_settled"] is None:
        b["open_fills"] += 1
        b["open_cost"] += r["_cost"]
    else:
        b["settled_fills"] += 1
        b["settled_cost"] += r["_cost"]
        b["realized_pnl"] += r["_pnl"]
        if r.get("result") == "win":
            b["wins"] += 1
        else:
            b["losses"] += 1


def _fin(b: dict, **extra: Any) -> dict:
    out = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in b.items()}
    out["roi_on_settled"] = _r(b["realized_pnl"] / b["settled_cost"], 4) if b["settled_cost"] > 0 else None
    out.update(extra)
    return out


def netting(open_rows: Iterable[dict]) -> dict:
    """Opposite YES / NO positions in one market are netted by Kalshi into $1 pairs. For each ticker holding both
    sides open, pairs = min(yes, no); the cost of a pair is taken at the AVERAGE cost of each side (the way Kalshi
    reports exposure; FIFO was off by up to $0.15 against it). Informational: the open positions are not read from
    Kalshi, so it never changes the ledger totals."""
    by: dict[str, dict[str, list[float]]] = {}
    for r in open_rows:
        if r.get("side") in ("yes", "no"):
            s = by.setdefault(r["ticker"], {"yes": [0.0, 0.0], "no": [0.0, 0.0]})[r["side"]]
            c = float(r.get("count") or 0)
            s[0] += c
            s[1] += c * float(r.get("price") or 0)
    pairs = cost = 0.0
    tickers = 0
    for sides in by.values():
        q = min(sides["yes"][0], sides["no"][0])
        if q <= 1e-9:
            continue
        tickers += 1
        pairs += q
        cost += sum(q * s[1] / s[0] for s in sides.values())
    return {"tickers": tickers, "pairs": round(pairs, 2), "pair_cost": round(cost, 2),
            "pair_value": round(pairs, 2), "pair_gain": round(pairs - cost, 2), "cost_basis": "average",
            "verified": False}


def pair_events(rows: Iterable[dict], until: datetime | None = None) -> dict[str, list[tuple[datetime, float, float]]]:
    """Pairs as they form, per market, in fill order (ts, fill_id). A fill on one side first matches the open
    contracts of the other side (average cost of that side); each matched pair returns $1, so its P&L is
    pairs - (cost of the matched opposite contracts) - (price x pairs). Returns {ticker: [(ts, pairs, pair_pnl)]}
    for the fills that formed pairs; fills after `until` are ignored."""
    ordered = sorted((r for r in rows if r.get("side") in ("yes", "no") and (until is None or r["_ts"] <= until)),
                     key=lambda r: (r["_ts"], str(r.get("fill_id") or "")))
    state: dict[str, dict[str, list[float]]] = {}
    out: dict[str, list[tuple[datetime, float, float]]] = {}
    for r in ordered:
        st = state.setdefault(r["ticker"], {"yes": [0.0, 0.0], "no": [0.0, 0.0]})
        mine, other = st[r["side"]], st["no" if r["side"] == "yes" else "yes"]
        c, p = float(r.get("count") or 0), float(r.get("price") or 0)
        pairs = min(c, other[0])
        if pairs > 1e-9:
            pc = other[1] * pairs / other[0]
            other[0] -= pairs
            other[1] -= pc
            out.setdefault(r["ticker"], []).append((r["_ts"], pairs, pairs - pc - p * pairs))
        else:
            pairs = 0.0
        mine[0] += c - pairs
        mine[1] += p * (c - pairs)
    return out


def reconciliation(rows: list[dict], snapshots: list[dict], flows: list[dict], *, since: datetime,
                   now: datetime, tolerance: float = TOLERANCE, truncated: bool = False) -> dict:
    """The reconciliation object (see module doc). `rows` are ALL trade rows read (any fill date), `snapshots` any
    order, `flows` the stored cash-flow docs. The window runs from the snapshot at or before `since` (else the
    first one after it) to the newest snapshot."""
    notes: list[str] = []
    snaps = []
    for s in snapshots:
        t = arbsets._ts(s.get("ts")) if isinstance(s, dict) else None
        cash, expo = pnlsvc._f(s.get("cash")), pnlsvc._f(s.get("exposure"))
        if t is not None and cash is not None and expo is not None:
            snaps.append((t, cash + expo, cash, expo))
    snaps.sort(key=lambda x: x[0])
    empty = {"tolerance": tolerance, "flag": None, "residual": None, "equity_start": None, "equity_end": None,
             "equity_change": None, "net_flows": None, "realized_pnl": None, "fee_timing": None,
             "expected_equity_change": None, "start_utc": None, "end_utc": None, "start_exact": None,
             "open_cost": None, "pair_netting": None, "pair_timing": None, "pairs_formed_in_window": None,
             "pair_pnl_formed_in_window": None, "pairs_before_window_settled_in_window": None,
             "pair_pnl_before_window_settled_in_window": None, "open_pair_markets": None,
             "model": MODEL, "notes": notes}
    if len(snaps) < 2:
        notes.append("fewer than two account snapshots in the stored data: equity change cannot be computed")
        return empty
    before = [s for s in snaps if s[0] <= since]
    start = before[-1] if before else snaps[0]
    exact = bool(before)
    if not exact:
        notes.append("no account snapshot at or before the window start: the first stored snapshot is used")
    end = snaps[-1]
    if end[0] <= start[0]:
        notes.append("start and end snapshot are the same: nothing to compare")
        return {**empty, "start_utc": arbsets.iso(start[0]), "end_utc": arbsets.iso(end[0])}
    t0, t1 = start[0], end[0]
    net_flows = 0.0
    for f in flows:
        t = arbsets._ts(f.get("ts"))
        a = pnlsvc._f(f.get("amount"))
        if t is None or a is None or not (t0 < t <= t1):
            continue
        net_flows += a if f.get("kind") == "deposit" else -a
    realized = fee_settled = fee_filled = 0.0
    for r in rows:
        fee = float(r.get("fee") or 0)
        if r["_settled"] is not None and t0 < r["_settled"] <= t1:
            realized += r["_pnl"]
            fee_settled += fee
        if t0 < r["_ts"] <= t1:
            fee_filled += fee
    fee_timing = fee_settled - fee_filled
    # pair P&L is booked when the pair forms (Kalshi credits $1 per pair then), not at settlement
    events = pair_events(rows, until=t1)
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    settled_in = {tk for tk, rs in by_ticker.items()
                  if all(r["_settled"] is not None for r in rs) and t0 < max(r["_settled"] for r in rs) <= t1}
    pairs_win = pnl_win = pairs_old = pnl_old = pnl_settled_mkts = 0.0
    for tk, evs in events.items():
        for ts, q, g in evs:
            if t0 < ts <= t1:
                pairs_win += q
                pnl_win += g
            if tk in settled_in:
                pnl_settled_mkts += g
                if ts <= t0:
                    pairs_old += q
                    pnl_old += g
    pair_timing = pnl_win - pnl_settled_mkts
    change = end[1] - start[1]
    expected = net_flows + realized + fee_timing + pair_timing
    residual = change - expected
    open_rows = [r for r in rows if r["_settled"] is None and r["_ts"] <= t1]
    open_cost = sum(r["_cost"] for r in open_rows)
    net = netting(open_rows)
    if truncated:
        notes.append("the fill read hit its row limit: totals may be incomplete")
    if any(r["_ts"] < t0 - timedelta(days=7) for r in rows if r["_settled"] is not None and t0 < r["_settled"] <= t1):
        notes.append("some settled fills were filled long before the window; their fees are counted at settlement")
    open_pair_markets = sum(1 for tk in events if tk not in settled_in
                            and any(r["_settled"] is None for r in by_ticker[tk]))
    if net["pairs"]:
        notes.append(f"{net['tickers']} open market(s) hold both YES and NO ({net['pairs']} matched pairs): Kalshi "
                     "already credited $1 per pair to cash, so open cost by fills sits above the reported exposure. "
                     "Pair P&L is counted when the pair formed (pair_timing), the rest at settlement")
    return {"tolerance": tolerance, "flag": abs(residual) > tolerance, "residual": round(residual, 2),
            "equity_start": round(start[1], 2), "equity_end": round(end[1], 2), "equity_change": round(change, 2),
            "net_flows": round(net_flows, 2), "realized_pnl": round(realized, 2),
            "fee_timing": round(fee_timing, 2), "pair_timing": round(pair_timing, 2),
            "pairs_formed_in_window": round(pairs_win, 2), "pair_pnl_formed_in_window": round(pnl_win, 2),
            "pairs_before_window_settled_in_window": round(pairs_old, 2),
            "pair_pnl_before_window_settled_in_window": round(pnl_old, 2), "open_pair_markets": open_pair_markets,
            "model": MODEL, "expected_equity_change": round(expected, 2),
            "start_utc": arbsets.iso(t0), "end_utc": arbsets.iso(t1), "start_exact": exact,
            "open_cost": {"by_fills": round(open_cost, 2), "snapshot_exposure": round(end[3], 2),
                          "gap": round(open_cost - end[3], 2)},
            "pair_netting": net, "notes": notes}


def build(rows: list[dict], sets: list[dict], *, zone: tzinfo, tzname: str, since: datetime, until: datetime | None,
          snapshots: list[dict], flows: list[dict], now: datetime, all_rows: list[dict] | None = None,
          truncated: bool = False) -> dict:
    """The ledger body. `rows` = trade rows filled inside the window (attribution by fill date), `all_rows` = every
    row read (the reconciliation needs fills that settled inside the window whenever they were filled)."""
    notes: list[str] = []
    overall: dict[str, dict] = {}
    daily: dict[tuple[str, str], dict] = {}
    by_settle: dict[tuple[str, str], dict] = {}
    by_strategy_settled: dict[str, dict] = {}
    for r in rows:
        g = _group(r)
        _add(overall.setdefault(g, _blank()), r)
        _add(daily.setdefault((r["_ts"].astimezone(zone).date().isoformat(), g), _blank()), r)
    for r in all_rows if all_rows is not None else rows:
        if r["_settled"] is not None and r["_settled"] <= now and r["_settled"] >= since \
                and (until is None or r["_settled"] < until):
            b = by_settle.setdefault((r["_settled"].astimezone(zone).date().isoformat(), _group(r)),
                                     {"settled_fills": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0})
            b["settled_fills"] += 1
            b["wins" if r.get("result") == "win" else "losses"] += 1
            b["realized_pnl"] += r["_pnl"]
            g = _group(r)
            sb = by_strategy_settled.setdefault(g, {"settled_fills": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0})
            sb["settled_fills"] += 1
            sb["wins" if r.get("result") == "win" else "losses"] += 1
            sb["realized_pnl"] += r["_pnl"]
    for g in ("maker", "arb"):
        by_strategy_settled.setdefault(g, {"settled_fills": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0})
    settled_total = {"settled_fills": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0}
    for v in by_strategy_settled.values():
        for k in settled_total:
            settled_total[k] += v[k]
    settled_by_strategy = [
        {"strategy": g, **{k: (round(v, 2) if k == "realized_pnl" else v) for k, v in b.items()}}
        for g, b in sorted(by_strategy_settled.items(), key=lambda kv: ({"maker": 0, "arb": 1}.get(kv[0], 2), kv[0]))]
    settled_by_strategy.append({"strategy": "total", **{k: (round(v, 2) if k == "realized_pnl" else v)
                                                       for k, v in settled_total.items()}})
    sstats = arbsets.set_stats(sets)
    strategies = []
    total = _blank()
    for g in sorted(overall, key=lambda k: ({"maker": 0, "arb": 1}.get(k, 2), k)):
        b = overall[g]
        for k in total:
            total[k] += b[k]
        extra: dict[str, Any] = {"strategy": g, "unit": "fill"}
        if g == "arb":
            extra["sets"] = sstats
            extra["unit"] = "fill (see sets for the per-set result)"
        strategies.append(_fin(b, **extra))
    open_rows = [r for r in rows if r["_settled"] is None]
    if not rows:
        notes.append("no fills in this window")
    if truncated:
        notes.append("the fill read hit its row limit: totals may be incomplete")
    ds = sorted(daily, reverse=True)[:DAILY_MAX]
    return {
        "tz_used": tzname, "attribution": "fill date (local day in tz_used); realized P&L follows the fill",
        "window": {"since": arbsets.iso(since), "until": arbsets.iso(until), "generated_utc": arbsets.iso(now)},
        "strategies": strategies,
        "settled_attribution": "settlement date (local day in tz_used)",
        "settled_by_strategy": settled_by_strategy,
        "total": _fin(total, strategy="total"),
        "daily": [_fin(daily[k], date=k[0], strategy=k[1]) for k in ds],
        "daily_by_settlement": [{"date": k[0], "strategy": k[1], "settled_fills": v["settled_fills"],
                                 "wins": v["wins"], "losses": v["losses"], "realized_pnl": round(v["realized_pnl"], 2)}
                                for k, v in sorted(by_settle.items(), reverse=True)[:DAILY_MAX]],
        "netting": netting(open_rows),
        "reconciliation": reconciliation(all_rows if all_rows is not None else rows, snapshots, flows,
                                         since=since, now=now, truncated=truncated),
        "notes": notes,
    }


# ---------------------------------------------------------------- scorecard
def _count(rows: Iterable[dict], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = key(r)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def scorecard(*, env: str, rows: list[dict], sets: list[dict], arb_counts: dict, heartbeats: list[dict],
              journal: list[dict], alerts: list[dict], audit_rows: list[dict], reconciliation_obj: dict,
              since: datetime, until: datetime | None, now: datetime, label: str, truncated: bool = False) -> dict:
    """Functional metrics for a period report (built from stored data only). Missing metric -> null + a note."""
    notes: list[str] = []
    hbs = [h for h in heartbeats if isinstance(h, dict)]
    ok = sum(1 for h in hbs if h.get("ok") is True)
    dur = [float(h["duration_s"]) for h in hbs if isinstance(h.get("duration_s"), (int, float))]
    passes = {"n": len(hbs), "ok": ok, "ok_pct": _r(100.0 * ok / len(hbs), 1) if hbs else None,
              "errors": len(hbs) - ok, "median_duration_s": _r(statistics.median(dur), 1) if dur else None,
              "max_duration_s": _r(max(dur), 1) if dur else None}
    if not hbs:
        notes.append("passes: no heartbeats stored in this window")
    quotes = sum(1 for j in journal if j.get("event") == "quote")
    cancels = sum(1 for j in journal if j.get("event") == "cancel")
    churn = {"quotes": quotes, "cancels": cancels,
             "cancels_per_quote": _r(cancels / quotes, 3) if quotes else None}
    if not quotes:
        notes.append("churn: no quote rows in the stored journal for this window")
    err_alerts = [a for a in alerts if a.get("type") == "error"]
    order_errors = {"value": None, "error_alerts": len(err_alerts)}
    notes.append("order errors: per-pass write-error counters are not stored; alerts of type 'error' are counted "
                 "instead")
    gate_rules = arb_counts.get("skipped_by_rule") or {}
    cap_bindings = {"maker": None, "arbitrage_gate": gate_rules}
    notes.append("cap bindings (maker): not stored per pass; arbitrage gate skips by rule are shown")
    due = [r for r in rows if r["_settled"] is None and r.get("ttc_hours") is not None
           and r["_ts"] + timedelta(hours=float(r["ttc_hours"])) + LATE_GRACE < now]
    unknown_close = sum(1 for r in rows if r["_settled"] is None and r.get("ttc_hours") is None)
    past = {"count": len(due), "cost": round(sum(r["_cost"] for r in due), 2), "grace_hours": LATE_GRACE.total_seconds() / 3600,
            "close_unknown": unknown_close}
    stale = [r for r in rows if r.get("ttc_hours") is not None
             and r["_ts"] + timedelta(hours=float(r["ttc_hours"])) + STALE_GRACE < now]
    stale_settled = sum(1 for r in stale if r["_settled"] is not None)
    integrity = {"due_fills": len(stale), "settled": stale_settled, "unsettled": len(stale) - stale_settled,
                 "rate": _r(stale_settled / len(stale), 4) if stale else None,
                 "grace_hours": STALE_GRACE.total_seconds() / 3600}
    if not stale:
        notes.append("fill-to-settlement integrity: no fills are old enough to judge")
    settled = [r for r in rows if r["_settled"] is not None and r.get("source") != "you"]
    cost = sum(r["_cost"] for r in settled)
    pnl = sum(r["_pnl"] for r in settled)
    label_ret = {"demo": "demo, indicative only", "live": "live, small sample, indicative only"}.get(
        env, "mixed demo and live, indicative only")
    ret = {"label": label_ret, "sample_settled_fills": len(settled), "realized_pnl": round(pnl, 2),
           "cost": round(cost, 2), "roi": _r(pnl / cost, 4) if cost > 0 else None}
    arb = {"legged": sum(1 for s in sets if s["outcome"] in ("partial", "error")),
           "complete": sum(1 for s in sets if s["outcome"] == "filled"),
           "skipped_by_gate": (arb_counts.get("by_outcome") or {}).get("skipped", 0),
           "skipped_by_rule": gate_rules,
           "log_only": (arb_counts.get("by_outcome") or {}).get("log_only", 0),
           "settled_sets": arbsets.set_stats(sets)["settled"], "set_wins": arbsets.set_stats(sets)["wins"],
           "distinct_sets_logged": arb_counts.get("sets", 0)}
    am = {"stops": 0, "stop_failures": 0, "starts": 0}
    for a in audit_rows:
        act = a.get("action")
        if act == "bot.stop":
            am["stops"] += 1
            am["stop_failures"] += a.get("ok") is False
        elif act == "bot.start" and a.get("ok") is not False:
            am["starts"] += 1
    if truncated:
        notes.append("a read hit its row limit: counts may be incomplete")
    return {"env": env, "label": label, "window": {"since": arbsets.iso(since), "until": arbsets.iso(until),
                                                     "generated_utc": arbsets.iso(now)},
            "passes": passes, "order_errors": order_errors, "churn": churn, "cap_bindings": cap_bindings,
            "fill_to_settlement": integrity, "open_past_expected_settlement": past,
            "reconciliation": {k: reconciliation_obj.get(k) for k in ("residual", "tolerance", "flag", "start_utc",
                                                                       "end_utc")},
            "alerts_by_type": _count(alerts, lambda a: str(a.get("type") or "system")),
            "arbitrage_per_set": arb, "stop_start_log": am, "return": ret, "notes": notes}

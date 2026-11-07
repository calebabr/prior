"""Daily reconciliation of the stored books against Kalshi's own records. READ-ONLY toward Kalshi.

`run_daily_recon(store, client_factory, mode, now)` checks the previous local day (and the open items today):

  1. fills        store vs Kalshi, by fill id: count, contracts, cost; a fill on one side only is a difference
  2. settlements  store vs Kalshi, by ticker: presence, result, revenue
  3. cash         latest account snapshot (cash, exposure) vs Kalshi balance and the sum of position exposures
  4. positions    open positions derived from the stored fills (markets with no stored settlement) vs Kalshi positions

Report: {ok, day, mode, checked_at, differences: [{kind, id, store_value, kalshi_value, delta, severity}], totals, ...}.
`delta` = kalshi_value - store_value (what the store is short by). `ok` is False when any difference has severity
"warn" or "error"; "info" entries (a stale or missing snapshot, so cash could not be checked) never flip it.

Guarantees: never writes to Kalshi (only get_balance / get_positions / get_fills / get_settlements are called); never
raises (Kalshi or store trouble comes back as a report with ok False and kind "unreachable" / "store_unavailable");
the report holds ids and numbers only (no keys, no raw exceptions); a rerun for the same day and mode replaces the
stored report. The caller (the runner) decides when to run it and sends `recon_alert(report)` through the alerter.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable

from ..db import schemas as S
from . import pnl as pnlsvc

log = logging.getLogger("control_api.recon")

TOLERANCE = 0.10                        # dollars: cash / exposure may differ by this much
URGENT_FACTOR = 5                       # a cash delta beyond 5 x tolerance makes the alert urgent
AMOUNT_EPS = 0.005                      # per-fill cost / revenue / position rounding slack
SNAPSHOT_MAX_AGE = timedelta(minutes=20)
POSITION_LOOKBACK = timedelta(days=35)  # same reach as pnl.FILL_LOOKBACK: fills older than this are not looked at
KALSHI_TIMEOUT_S = 120.0
STORE_TIMEOUT_S = 30.0
POSITION_PAGES = 20
SEVERITIES = ("info", "warn", "error")


# ---------------------------------------------------------------- small helpers
def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def _dollars(d: dict, key: str) -> float | None:
    """Kalshi money field: `<key>_dollars` (dollars) else `<key>` (cents)."""
    v = _f(d.get(f"{key}_dollars"))
    if v is None:
        c = _f(d.get(key))
        v = c / 100.0 if c is not None else None
    return v


def _ts(doc: dict) -> datetime | None:
    for k in ("ts", "created_time", "settled_time"):
        t = S._safe_to_utc(doc.get(k))
        if t is not None:
            return t
    return None


def _fill_id(f: dict) -> str | None:
    v = f.get("fill_id") or f.get("trade_id")
    return str(v) if v else None


def _fill_ticker(f: dict) -> str:
    return str(f.get("ticker") or f.get("market_ticker") or "")


def _sign(f: dict) -> float:
    return -1.0 if str(f.get("action") or "buy").lower() == "sell" else 1.0


def _fill_cost(f: dict) -> float:
    side = pnlsvc.fill_side(f)
    price = pnlsvc.fill_price(f, side) if side else None
    return _sign(f) * pnlsvc.fill_count(f) * price if price is not None else 0.0


def _fill_contracts(f: dict) -> float:
    return _sign(f) * pnlsvc.fill_count(f)


def _mk_diff(kind: str, ident: str, store_value: Any, kalshi_value: Any, severity: str, **extra: Any) -> dict:
    a, b = _f(store_value), _f(kalshi_value)
    delta = _r(b - a) if a is not None and b is not None and not isinstance(store_value, bool) else None
    out = {"kind": kind, "id": ident, "store_value": store_value, "kalshi_value": kalshi_value, "delta": delta,
           "severity": severity}
    out.update(extra)
    return out


def _tzinfo(tz: Any) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    return pnlsvc.resolve_tz(tz)[0]


def day_window(now: datetime, tz: Any = None, day: date | str | None = None) -> tuple[str, datetime, datetime]:
    """(day 'YYYY-MM-DD', start, end) as aware UTC: the previous local calendar day of `now` in `tz` (UTC when
    unknown), or `day` when given. DST-safe (the day may be 23 or 25 hours long)."""
    zone = _tzinfo(tz)
    now = S.norm_dt(now)
    if day is None:
        d = now.astimezone(zone).date() - timedelta(days=1)
    elif isinstance(day, date):
        d = day
    else:
        d = date.fromisoformat(str(day))
    noon = lambda x: datetime.combine(x, time(12), tzinfo=zone)  # noqa: E731
    return d.isoformat(), pnlsvc.local_midnight_utc(noon(d), zone), pnlsvc.local_midnight_utc(noon(d + timedelta(days=1)), zone)


# ---------------------------------------------------------------- Kalshi reads (blocking; run in a worker thread)
def _read_positions(client: Any) -> tuple[list[dict], bool]:
    positions: list[dict] = []
    cursor = None
    for _ in range(POSITION_PAGES):
        try:
            body = client.get_positions(count_filter="position", limit=200, cursor=cursor)
        except TypeError:                                   # a minimal client that takes no parameters
            body = client.get_positions()
            cursor = None
        body = body if isinstance(body, dict) else {}
        positions.extend(p for p in (body.get("market_positions") or []) if isinstance(p, dict))
        if body.get("truncated") is True:
            return positions, True
        cursor = body.get("cursor")
        if not cursor or not body.get("market_positions"):
            return positions, False
    return positions, True                                  # more pages than we are willing to read


def _read_settlements(client: Any, min_ts: int) -> list[dict]:
    getter = getattr(client, "get_settlements", None)
    if callable(getter):
        return list(getter(min_ts=min_ts) or [])
    out: list[dict] = []
    if callable(getattr(client, "request", None)):
        cursor = None
        for _ in range(50):                                  # bounded pagination (same as the runner's sync)
            body = client.request("GET", "/portfolio/settlements",
                                  params={"min_ts": min_ts, "limit": 200, "cursor": cursor}, auth=True)
            out.extend(body.get("settlements") or [])
            cursor = body.get("cursor")
            if not cursor or not body.get("settlements"):
                break
    return out


def _fetch_kalshi(client_factory: Callable[[str], Any], mode: str, min_ts: int) -> dict:
    """Everything the check needs from Kalshi, GET requests only. Raises on any failure (the caller reports it)."""
    client = client_factory(mode)
    if client is None:
        raise LookupError("not_configured")
    cash = _f((client.get_balance() or {}).get("balance"))
    positions, truncated = _read_positions(client)
    fills = [f for f in (client.get_fills(min_ts=min_ts) or []) if isinstance(f, dict)]
    settlements = [s for s in _read_settlements(client, min_ts) if isinstance(s, dict)]
    return {"cash": cash / 100.0 if cash is not None else None, "positions": positions,
            "positions_truncated": truncated, "fills": fills, "settlements": settlements}


# ---------------------------------------------------------------- the comparisons (pure)
def _fill_totals(rows: list[dict]) -> dict:
    return {"count": len(rows), "contracts": _r(sum(_fill_contracts(f) for f in rows), 2),
            "cost": _r(sum(_fill_cost(f) for f in rows), 2)}


def compare_fills(store_fills: list[dict], kalshi_fills: list[dict]) -> tuple[list[dict], dict]:
    s = {i: f for f in store_fills if (i := _fill_id(f))}
    k = {i: f for f in kalshi_fills if (i := _fill_id(f))}
    diffs: list[dict] = []
    for i in sorted(k.keys() - s.keys()):
        diffs.append(_mk_diff("fill_missing_in_store", i, None, {"ticker": _fill_ticker(k[i]),
                                                                  "contracts": _r(_fill_contracts(k[i]), 2),
                                                                  "cost": _r(_fill_cost(k[i]), 4)}, "error"))
    for i in sorted(s.keys() - k.keys()):
        diffs.append(_mk_diff("fill_missing_at_kalshi", i, {"ticker": _fill_ticker(s[i]),
                                                            "contracts": _r(_fill_contracts(s[i]), 2),
                                                            "cost": _r(_fill_cost(s[i]), 4)}, None, "error"))
    for i in sorted(s.keys() & k.keys()):
        sc, kc = _fill_contracts(s[i]), _fill_contracts(k[i])
        if abs(sc - kc) > AMOUNT_EPS:
            diffs.append(_mk_diff("fill_count_mismatch", i, _r(sc, 2), _r(kc, 2), "warn", ticker=_fill_ticker(s[i])))
        sp, kp = _fill_cost(s[i]), _fill_cost(k[i])
        if abs(sp - kp) > AMOUNT_EPS:
            diffs.append(_mk_diff("fill_cost_mismatch", i, _r(sp), _r(kp), "warn", ticker=_fill_ticker(s[i])))
    return diffs, {"store": _fill_totals(list(s.values())), "kalshi": _fill_totals(list(k.values()))}


def _settle_result(x: dict) -> str | None:
    r = x.get("market_result") or x.get("result")
    return str(r) if r else None


def _settle_revenue(x: dict) -> float | None:
    return _dollars(x, "revenue")


def compare_settlements(store_rows: list[dict], kalshi_rows: list[dict]) -> tuple[list[dict], dict]:
    s = {str(r["ticker"]): r for r in store_rows if r.get("ticker")}
    k = {str(r["ticker"]): r for r in kalshi_rows if r.get("ticker")}
    diffs: list[dict] = []
    for t in sorted(k.keys() - s.keys()):
        diffs.append(_mk_diff("settlement_missing_in_store", t, None,
                              {"result": _settle_result(k[t]), "revenue": _settle_revenue(k[t])}, "error"))
    for t in sorted(s.keys() - k.keys()):
        diffs.append(_mk_diff("settlement_missing_at_kalshi", t,
                              {"result": _settle_result(s[t]), "revenue": _settle_revenue(s[t])}, None, "warn"))
    for t in sorted(s.keys() & k.keys()):
        sr, kr = _settle_result(s[t]), _settle_result(k[t])
        if sr != kr:
            diffs.append(_mk_diff("settlement_result_mismatch", t, sr, kr, "error"))
        sv, kv = _settle_revenue(s[t]), _settle_revenue(k[t])
        if sv is not None and kv is not None and abs(sv - kv) > AMOUNT_EPS:
            diffs.append(_mk_diff("settlement_revenue_mismatch", t, _r(sv), _r(kv), "warn"))

    def tot(rows: dict[str, dict]) -> dict:
        return {"count": len(rows), "revenue": _r(sum(_settle_revenue(r) or 0.0 for r in rows.values()), 2)}
    return diffs, {"store": tot(s), "kalshi": tot(k)}


def kalshi_exposure(positions: list[dict]) -> float:
    return sum(abs(_dollars(p, "market_exposure") or 0.0) for p in positions)


def kalshi_position_map(positions: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions:
        t = str(p.get("ticker") or "")
        n = _f(p.get("position_fp"))
        n = _f(p.get("position")) if n is None else n
        if t and n:
            out[t] = out.get(t, 0.0) + n
    return out


def store_position_map(fills: list[dict], settled: set[str]) -> dict[str, float]:
    """Net open contracts per market from stored fills (YES positive, NO negative), markets with a stored settlement
    excluded. Buys add, sells subtract (Kalshi nets YES against NO in one market)."""
    out: dict[str, float] = {}
    for f in fills:
        t, side = _fill_ticker(f), pnlsvc.fill_side(f)
        if not t or side is None or t in settled:
            continue
        n = _fill_contracts(f)
        out[t] = out.get(t, 0.0) + (n if side == "yes" else -n)
    return {t: v for t, v in out.items() if abs(v) > 1e-9}


def compare_positions(store_pos: dict[str, float], kalshi_pos: dict[str, float]) -> list[dict]:
    diffs: list[dict] = []
    for t in sorted(set(store_pos) | set(kalshi_pos)):
        sv, kv = store_pos.get(t, 0.0), kalshi_pos.get(t, 0.0)
        if abs(sv - kv) <= AMOUNT_EPS:
            continue
        if abs(sv) <= AMOUNT_EPS:
            diffs.append(_mk_diff("position_missing_in_store", t, 0.0, _r(kv, 2), "error"))
        elif abs(kv) <= AMOUNT_EPS:
            diffs.append(_mk_diff("position_missing_at_kalshi", t, _r(sv, 2), 0.0, "error"))
        else:
            diffs.append(_mk_diff("position_mismatch", t, _r(sv, 2), _r(kv, 2), "warn"))
    return diffs


def compare_cash(snapshot: dict | None, kalshi_cash: float | None, kalshi_expo: float | None, *, now: datetime,
                 tolerance: float, max_age: timedelta, positions_complete: bool) -> tuple[list[dict], dict]:
    diffs: list[dict] = []
    totals: dict[str, Any] = {"snapshot_ts": None, "snapshot_age_s": None, "tolerance": tolerance}
    snap_ts = S._safe_to_utc((snapshot or {}).get("ts"))
    if snapshot is None or snap_ts is None:
        diffs.append(_mk_diff("snapshot_missing", "account", None, None, "info"))
        return diffs, totals
    age = now - snap_ts
    totals["snapshot_ts"] = snap_ts.isoformat()
    totals["snapshot_age_s"] = int(age.total_seconds())
    if age > max_age:                          # a balance moves with every fill: an old snapshot proves nothing
        diffs.append(_mk_diff("snapshot_stale", "account", int(age.total_seconds()), int(max_age.total_seconds()),
                              "info"))
        return diffs, totals
    checks = [("cash", _f(snapshot.get("cash")), kalshi_cash)]
    if positions_complete:
        checks.append(("exposure", _f(snapshot.get("exposure")), kalshi_expo))
    for name, sv, kv in checks:
        totals[name] = {"store": None if sv is None else _r(sv, 2), "kalshi": None if kv is None else _r(kv, 2)}
        if sv is None or kv is None:
            continue
        if abs(kv - sv) > tolerance:
            diffs.append(_mk_diff(f"{name}_delta", name, _r(sv, 2), _r(kv, 2),
                                  "error" if abs(kv - sv) > URGENT_FACTOR * tolerance else "warn"))
    return diffs, totals


# ---------------------------------------------------------------- the run
def _finish(report: dict) -> dict:
    diffs = report["differences"]
    report["ok"] = not any(d["severity"] in ("warn", "error") for d in diffs)
    by_sev = {s: 0 for s in SEVERITIES}
    for d in diffs:
        by_sev[d["severity"]] = by_sev.get(d["severity"], 0) + 1
    report["totals"]["differences"] = {"n": len(diffs), **by_sev}
    return report


async def _blocking(run_blocking: Callable | None, fn: Callable, *args: Any) -> Any:
    if run_blocking is not None:
        return await run_blocking(fn, *args)
    return await asyncio.to_thread(fn, *args)


async def run_daily_recon(store: Any, client_factory: Callable[[str], Any], mode: str, now: datetime | None = None,
                          *, tz: Any = None, day: date | str | None = None, tolerance: float = TOLERANCE,
                          snapshot_max_age: timedelta = SNAPSHOT_MAX_AGE,
                          run_blocking: Callable | None = None) -> dict:
    """Reconcile `mode`'s previous local day (see the module doc). `client_factory(mode)` returns a Kalshi client (or
    None when there are no keys). `tz`: IANA name or tzinfo for the day boundary (default UTC). `run_blocking(fn,
    *args)` -> awaitable runs the blocking Kalshi reads (the runner passes its telemetry pool; default
    asyncio.to_thread). Persists the report with store.save_recon_report (replacing the same day+mode) and returns
    it. Never raises, never writes to Kalshi."""
    now = S.norm_dt(now or S.utcnow())
    if mode not in S.MODE_VALUES:
        raise ValueError(f"mode must be one of {S.MODE_VALUES}")
    tol = abs(float(tolerance))
    day_s, start, end = day_window(now, tz, day)
    report: dict = {"ok": False, "day": day_s, "mode": mode, "checked_at": now.isoformat(),
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                    "tz": str(getattr(_tzinfo(tz), "key", None) or "UTC"), "tolerance": tol,
                    "differences": [], "totals": {}}
    diffs: list[dict] = report["differences"]

    async def st(coro: Any) -> Any:
        return await asyncio.wait_for(coro, STORE_TIMEOUT_S)

    try:                                                    # 1. Kalshi (GET only)
        kal = await asyncio.wait_for(_blocking(run_blocking, _fetch_kalshi, client_factory, mode, int(start.timestamp())),
                                     KALSHI_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001   (unreachable / no keys / bad answer: a finding, not a crash)
        log.warning("recon: kalshi read failed: %s", type(e).__name__)
        diffs.append(_mk_diff("unreachable", "kalshi", None, None, "error",
                              detail="not_configured" if isinstance(e, LookupError) else type(e).__name__))
        return await _save(store, _finish(report))
    try:                                                    # 2. the store
        env_ok = lambda d: d.get("env") in (None, mode)  # noqa: E731   (demo and live books never mix)
        s_fills = [f for f in await st(store.list_fills(since=start, until=end, limit=50000, fields=pnlsvc.FILL_FIELDS))
                   if env_ok(f)]
        s_settle = [x for x in await st(store.list_settlements(since=start, until=end, limit=20000)) if env_ok(x)]
        open_fills = [f for f in await st(store.list_fills(since=now - POSITION_LOOKBACK, limit=50000,
                                                           fields=pnlsvc.FILL_FIELDS)) if env_ok(f)]
        settled = {str(x["ticker"]) for x in await st(store.list_settlements(
            since=now - POSITION_LOOKBACK, limit=20000, fields=("ticker",))) if x.get("ticker")}
        snaps = await st(store.list_account_snapshots(limit=1, env=mode))
    except Exception as e:  # noqa: BLE001
        log.warning("recon: store read failed: %s", type(e).__name__)
        diffs.append(_mk_diff("store_unavailable", "store", None, None, "error", detail=type(e).__name__))
        return await _save(store, _finish(report))

    k_fills = [f for f in kal["fills"] if (t := _ts(f)) is not None and start <= t < end]
    k_settle = [x for x in kal["settlements"] if (t := _ts(x)) is not None and start <= t < end]
    d, fill_tot = compare_fills(s_fills, k_fills)
    diffs.extend(d)
    d, settle_tot = compare_settlements(s_settle, k_settle)
    diffs.extend(d)
    complete = not kal["positions_truncated"]
    cash_d, cash_tot = compare_cash(snaps[0] if snaps else None, kal["cash"],
                                    kalshi_exposure(kal["positions"]) if complete else None, now=now,
                                    tolerance=tol, max_age=snapshot_max_age, positions_complete=complete)
    diffs.extend(cash_d)
    s_pos, k_pos = store_position_map(open_fills, settled), kalshi_position_map(kal["positions"])
    if complete:
        diffs.extend(compare_positions(s_pos, k_pos))
    else:                                                   # a partial read cannot prove a position missing
        diffs.append(_mk_diff("positions_truncated", "kalshi", len(s_pos), len(k_pos), "warn"))
    report["totals"] = {"fills": fill_tot, "settlements": settle_tot, "account": cash_tot,
                        "positions": {"store": len(s_pos), "kalshi": len(k_pos), "complete": complete}}
    return await _save(store, _finish(report))


async def _save(store: Any, report: dict) -> dict:
    """Persist (replacing the same day+mode). A storage failure is noted on the report, never raised."""
    try:
        saved = await asyncio.wait_for(store.save_recon_report(report), STORE_TIMEOUT_S)
        report["persisted"] = saved is not None
    except Exception as e:  # noqa: BLE001
        log.warning("recon: report not saved: %s", type(e).__name__)
        report["persisted"] = False
    return report


# ---------------------------------------------------------------- alert
def recon_alert(report: dict | None) -> dict | None:
    """None when the report is clean (or not a report). Else {kind, title, message, priority} for the existing
    alerter: normal ("default") priority, "urgent" when a cash delta exceeds 5 x the report's tolerance. Ids and
    numbers only; the message stays under the alert body limit."""
    if not isinstance(report, dict) or report.get("ok") is not False:
        return None
    diffs = [d for d in report.get("differences") or [] if isinstance(d, dict)]
    mode, day = str(report.get("mode") or "?"), str(report.get("day") or "?")
    tol = _f(report.get("tolerance")) or TOLERANCE
    if any(d.get("kind") == "unreachable" for d in diffs):
        return {"kind": "recon", "title": f"Reconciliation could not reach Kalshi ({mode})",
                "message": f"The daily check for {day} did not run. It will be tried again.", "priority": "default"}
    big_cash = [d for d in diffs if d.get("kind") == "cash_delta" and abs(_f(d.get("delta")) or 0.0) > URGENT_FACTOR * tol]
    kinds: dict[str, int] = {}
    for d in diffs:
        if d.get("severity") in ("warn", "error"):
            kinds[str(d.get("kind"))] = kinds.get(str(d.get("kind")), 0) + 1
    n = sum(kinds.values())
    parts = ", ".join(f"{c} {k}" for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
    msg = f"{n} difference(s) for {day}: {parts}."
    if big_cash:
        msg += f" Cash differs by ${abs(_f(big_cash[0]['delta'])):.2f}."
    return {"kind": "recon", "title": f"Reconciliation differences ({mode})", "message": msg[:200],
            "priority": "urgent" if big_cash else "default"}


def dumps(report: dict) -> str:
    """JSON text of a report (used by tests to prove it holds no secrets)."""
    return json.dumps(report, default=str)

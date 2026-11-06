"""Realistic FAKE data for frontend development and tests. Never run against a production database.

    from control_api.db import MemoryStore
    from control_api.db.seed import seed
    store = MemoryStore(); counts = await seed(store)

Deterministic for a given `now` and `rng_seed`. Users are NOT seeded (password/TOTP handling belongs
to the auth bootstrap CLI). Run directly to print a summary of a seeded MemoryStore:
    python -m control_api.db.seed
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import schemas as S
from .store import Store

SERIES = ["KXDEMOA", "KXDEMOB", "KXDEMOC", "KXDEMOD", "KXDEMOE", "KXDEMOF"]
_CONFIG_FILE = Path(__file__).resolve().parents[2] / "kalshi_bot" / "maker_config.json"


def _base_config() -> dict:
    """Current bot settings (read-only from kalshi_bot/maker_config.json, `_` comment keys dropped)."""
    try:
        raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {"run_budget": 10, "max_order": 5, "max_open_frac": 0.2, "series": list(SERIES),
                "poll_minutes": 10, "cells": []}


EXAMPLE_CELLS = [[0.35, 0.50, 3, 9, "yes"]]       # only if the config has no cells (made-up values)


def _cells(cfg: dict) -> list:
    """The installed config cells (read only); seed prices are drawn inside them, so no price range is coded here."""
    return [c for c in (cfg.get("cells") or []) if isinstance(c, (list, tuple)) and len(c) == 5] or EXAMPLE_CELLS


def _price_in(rng: random.Random, cell, top_c: int) -> float:
    """A cent price inside [lo, hi) of the cell (hi itself only for the topmost cell, as the strategy does)."""
    lo_c, hi_c = round(float(cell[0]) * 100), round(float(cell[1]) * 100)
    return round(rng.randint(lo_c, max(lo_c, hi_c if hi_c >= top_c else hi_c - 1)) / 100, 2)


def _ticker(rng: random.Random, series: str, day: datetime) -> str:
    d = day.strftime("%y%b%d").upper()
    return f"{series}-{d}-B{rng.randint(55, 95)}.5"


def _iso(d: datetime) -> str:
    return d.isoformat()


async def seed(store: Store, *, now: datetime | None = None, rng_seed: int = 7) -> dict:
    """Fill `store` with fake but realistic data. Returns {collection: docs written}."""
    rng = random.Random(rng_seed)
    now = S.norm_dt(now or datetime.now(timezone.utc))
    counts: dict[str, int] = {}

    # state + cloud settings
    await store.set_state(by="seed", desired="stopped", mode="demo")
    counts["state"] = 1
    await store.save_cloud_settings({**S.DEFAULT_CLOUD_SETTINGS, "cronjob_job_id": 5551234,
                                     "pinger_sync": {"ok": True, "ts": now - timedelta(hours=3), "error": None}},
                                    by="seed")
    counts["cloud_settings"] = 1

    # config: two versions (run_budget raised 30 -> 40)
    cfg = _base_config()
    v1 = {**cfg, "run_budget": 30}
    await store.save_config(v1, before=None, by="seed")
    await store.save_config(cfg, before=v1, by="owner")
    counts["config_versions"] = 2

    # journal: one pass every 30 min for 2 days
    rows = []
    t = now - timedelta(days=2)
    cash = 212.40
    cells = _cells(cfg)
    top_c = max(round(float(c[1]) * 100) for c in cells)
    while t <= now:
        exposure = round(rng.uniform(40, 95), 2)
        headroom = round(max(0.0, min(40, 0.5 * (cash + exposure) - exposure, cash)), 2)
        rows.append({"utc": _iso(t), "event": "account", "ticker": "", "side": "", "price": "", "count": "",
                     "cost": "", "cell": "", "note": json.dumps({"cash": cash, "exposure": exposure,
                                                                 "headroom": headroom})})
        for s in rng.sample(SERIES, rng.randint(2, 4)):
            cell = list(cells[rng.randrange(len(cells))])
            price = _price_in(rng, cell, top_c)
            count = rng.randint(2, 11)
            side = str(cell[4])
            ts = t + timedelta(seconds=rng.randint(1, 50))
            rows.append({"utc": _iso(ts), "event": "quote", "ticker": _ticker(rng, s, ts), "side": side,
                         "price": str(price), "count": str(count), "cost": str(round(price * count, 2)),
                         "cell": json.dumps(cell), "note": f"live order_id=demo-{rng.getrandbits(48):012x}"})
            if rng.random() < 0.15:
                rows.append({"utc": _iso(ts + timedelta(seconds=1)), "event": "cancel", "ticker": "", "side": "",
                             "price": "", "count": "", "cost": "", "cell": "",
                             "note": json.dumps([_ticker(rng, s, ts), side, f"{1 - price:.4f}"])})
        cash = round(cash + rng.uniform(-1.5, 1.9), 2)
        t += timedelta(minutes=30)
    counts["journal_rows"] = await store.add_journal_rows(rows)

    # account snapshots: hourly for 30 days, slow upward drift
    t, cash, n = now - timedelta(days=30), 200.0, 0
    while t <= now:
        exposure = round(rng.uniform(35, 100), 2)
        cash = round(cash + rng.gauss(0.03, 0.6), 2)
        headroom = round(max(0.0, min(40, 0.5 * (cash + exposure) - exposure, cash)), 2)
        await store.add_account_snapshot({"ts": t, "cash": cash, "exposure": exposure, "headroom": headroom,
                                         "env": "demo"})
        t += timedelta(hours=1)
        n += 1
    counts["account_snapshots"] = n

    # heartbeats: every 10 min for 24 h, one failure
    t, n = now - timedelta(hours=24), 0
    while t <= now:
        hb = {"ts": t, "ok": True, "duration_s": round(rng.uniform(35, 75), 1), "mode": "demo",
              "desired": "running", "quotes": rng.randint(8, 22)}
        if n == 37:
            hb.update(ok=False, error="HTTPError 503 from demo-api.kalshi.co (retry next pass)", quotes=0)
        await store.add_heartbeat(hb)
        t += timedelta(minutes=10)
        n += 1
    counts["heartbeats"] = n

    # fills: ~40 over 30 days
    fills = []
    for i in range(40):
        ts = now - timedelta(minutes=rng.randint(30, 30 * 24 * 60))
        s = rng.choice(SERIES)
        cell = cells[rng.randrange(len(cells))]
        side = str(cell[4])
        price = _price_in(rng, cell, top_c)
        cnt = rng.randint(1, 10)
        fills.append({"fill_id": f"demo-fill-{i:04d}", "order_id": f"demo-{rng.getrandbits(48):012x}",
                      "ticker": _ticker(rng, s, ts), "side": side, "action": "buy", "count": cnt,
                      "yes_price_dollars": f"{(1 - price) if side == 'no' else price:.4f}",
                      "no_price_dollars": f"{price if side == 'no' else (1 - price):.4f}",
                      "is_taker": False, "created_time": _iso(ts).replace("+00:00", "Z")})
    counts["fills"] = await store.upsert_fills(fills)

    # settlements: one per distinct filled ticker that is at least a day old (most settle as a win)
    settled = {}
    for f in fills:
        ts = S.to_utc(f["created_time"])
        if ts > now - timedelta(days=1) or f["ticker"] in settled:
            continue
        won = rng.random() < 0.55
        side_won = f["side"] if won else ("yes" if f["side"] == "no" else "no")
        cost_c = round(float(f["no_price_dollars" if f["side"] == "no" else "yes_price_dollars"]) * f["count"] * 100)
        settled[f["ticker"]] = {
            "ticker": f["ticker"], "market_result": side_won,
            "yes_count": f["count"] if f["side"] == "yes" else 0, "no_count": f["count"] if f["side"] == "no" else 0,
            "yes_total_cost": cost_c if f["side"] == "yes" else 0, "no_total_cost": cost_c if f["side"] == "no" else 0,
            "revenue": f["count"] * 100 if won else 0,
            "settled_time": _iso(ts + timedelta(hours=rng.randint(6, 20))).replace("+00:00", "Z")}
    counts["settlements"] = await store.upsert_settlements(list(settled.values()))

    # bracket-arbitrage log: a few log-only sightings (one seen repeatedly) over the last 3 days
    n = 0
    for i in range(6):
        ts = now - timedelta(hours=rng.randint(1, 72), minutes=rng.randint(0, 59))
        event = f"{rng.choice(SERIES[:4])}-{ts.strftime('%y%b%d').upper()}"
        k = rng.randint(5, 7)
        contracts = float(rng.randint(5, 40))
        bids = [round(rng.uniform(0.05, 0.4), 2) for _ in range(k)]
        legs = [[f"{event}-B{60 + 2 * j}.5", "ask", b, contracts] for j, b in enumerate(bids)]
        cost = round(contracts * sum(1 - b for b in bids) + 0.02 * k * contracts, 4)
        profit = round(rng.uniform(0.26, 2.4), 4)
        op = {"ts": ts, "event": event, "kind": "NO", "contracts": contracts, "profit": profit, "cost": cost,
              "sum_best": round(sum(bids), 4), "legs": legs, "executed": "log-only", "mode": "demo"}
        n += await store.add_arb_opportunity(op)
        if i == 0:                                        # same book seen on the next two 15 s polls
            for j in (1, 2):
                await store.add_arb_opportunity({**op, "ts": ts + timedelta(seconds=15 * j)})
    counts["arb_opportunities"] = n

    # monitor summary (shape of kalshi_bot/maker_monitor.py summary)
    per_series = {}
    for s in SERIES:
        f = rng.randint(4, 30)
        pnl = round(rng.uniform(-3, 9), 2)
        per_series[s] = {"fills": f, "events": max(1, f // 2), "pnl": pnl, "cost": round(f * 7.5, 2),
                         "roi": round(pnl / (f * 7.5), 4), "ci_low": round(pnl / (f * 7.5) - 0.03, 4),
                         "ci_high": round(pnl / (f * 7.5) + 0.03, 4)}
    gen = now - timedelta(days=2, hours=5)
    await store.add_monitor_summary({
        "generated_utc": _iso(gen), "window_days": 30, "expected_roi": None,
        "quoted_dollars": 1840.55, "filled_dollars": 612.30, "fill_rate": 0.3327,
        "overall": {"fills": 96, "events": 51, "pnl": 8.73, "cost": 612.30, "roi": 0.0143,
                    "ci_low": -0.0041, "ci_high": 0.0327},
        "per_series": per_series,
        "actions": {"pause": [], "collateral_return": "not yet",
                    "need_more": ["KXDEMOE"]},
        "last_account": {"utc": _iso(gen), "cash": 214.10, "exposure": 71.25, "headroom": 35.92},
        "note": "",
    })
    counts["monitor_summaries"] = 1

    # audit log
    audit = [("login", True, None), ("2fa", True, None), ("bot.start", True, None),
             ("config.update", True, {"run_budget": [30, 40], "version": 2}), ("login", False, "bad password"),
             ("bot.stop", True, {"cancelled": 6, "errors": []}), ("cloud.update", True, {"pinger_minutes": [10, 5]})]
    for i, (action, ok, details) in enumerate(audit):
        await store.add_audit({"ts": now - timedelta(hours=(len(audit) - i) * 5), "user": "owner",
                               "action": action, "ok": ok, "details": details, "ip": "203.0.113.24",
                               "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
    counts["audit_log"] = len(audit)

    # periods and cash flows: one marker 10 days ago (starts "Demo Period 2"), a deposit and a withdrawal.
    # Fixed ids, so re-seeding is a no-op for these (a duplicate id raises ValueError, which is skipped).
    n_period = n_flow = 0
    try:
        await store.add_period({"id": "seed-p2", "name": "Demo Period 2", "started_at": now - timedelta(days=10),
                                "note": "Risk gate on", "created_by": "owner"})
        n_period += 1
    except ValueError:
        pass
    for fid, kind, amount, days, note in (("seed-cf1", "deposit", 100.0, 20, "Initial demo funding"),
                                          ("seed-cf2", "withdrawal", 25.0, 4, None)):
        try:
            await store.add_cash_flow({"id": fid, "ts": now - timedelta(days=days), "kind": kind,
                                       "amount": amount, "note": note, "created_by": "owner"})
            n_flow += 1
        except ValueError:
            pass
    counts["periods"], counts["cash_flows"] = n_period, n_flow

    # analytics (GET /api/analytics/*): ~90 days of in-cell maker fills, a few taker fills, quote episodes,
    # fill quality and executed arb sets. Separate RNG, so the sections above are unchanged.
    for k, v in (await _seed_analytics(store, now, cfg, rng_seed)).items():
        counts[k] = counts.get(k, 0) + v
    await store.set_schema_version(S.SCHEMA_VERSION)
    return counts


async def _seed_analytics(store: Store, now: datetime, cfg: dict, rng_seed: int) -> dict:
    rng = random.Random(rng_seed + 1000)
    cells = _cells(cfg)
    top_c = max(round(float(c[1]) * 100) for c in cells)
    series = [s for s in (cfg.get("series") or SERIES)] or SERIES
    fills, settles, eps, results = [], {}, [], {}
    for i in range(420):
        ts = now - timedelta(minutes=rng.randint(60, 90 * 24 * 60))
        lo, hi, h0, h1, side = cells[rng.randrange(len(cells))]
        price = _price_in(rng, (lo, hi, h0, h1, side), top_c)
        hours = rng.uniform(float(h0), float(h1) - 0.05)
        close = ts + timedelta(hours=hours)
        s = rng.choice(series)
        ticker = f"{_ticker(rng, s, close)}-A{i % 37}"
        cnt = rng.randint(1, 12)
        age_days = (now - ts).total_seconds() / 86400
        p_win = min(0.995, price) if not 40 <= age_days <= 46 else price - 0.08   # fair odds; a losing stretch
        fid, oid = f"demo-afill-{i:04d}", f"demo-aord-{i:04d}"
        f = {"fill_id": fid, "order_id": oid, "ticker": ticker, "side": side, "action": "buy", "count": cnt,
             "yes_price_dollars": f"{(1 - price) if side == 'no' else price:.4f}",
             "no_price_dollars": f"{price if side == 'no' else (1 - price):.4f}", "fee_cost": "0.0000",
             "is_taker": False, "env": "demo", "created_time": _iso(ts).replace("+00:00", "Z")}
        if age_days < 30 and rng.random() < 0.7:
            f["fill_quality"] = {"trade_through": rng.random() < 0.35, "move_60m_c": round(rng.gauss(-0.1, 0.8), 3),
                                 "trades_60m": rng.randint(0, 25), "fill_price": price, "side": side,
                                 "evaluated_utc": ts + timedelta(minutes=70)}
        fills.append(f)
        if close + timedelta(hours=2) < now:
            if ticker not in results:
                won = rng.random() < p_win
                results[ticker] = side if won else ("yes" if side == "no" else "no")
            settles[ticker] = {"ticker": ticker, "market_result": results[ticker],
                               "close_time": _iso(close).replace("+00:00", "Z"),
                               "settled_time": _iso(close + timedelta(hours=rng.uniform(2, 10))).replace("+00:00", "Z")}
        if age_days < 44:                                 # quote episodes (45-day TTL in Atlas)
            join = ts - timedelta(minutes=rng.randint(5, 240))
            partial = rng.random() < 0.15                 # order bigger than what filled; rest expired at close
            eps.append({"order_id": oid, "ts": join, "env": "demo", "series": s, "ticker": ticker, "side": side,
                        "book_side": "bid" if side == "yes" else "ask", "price": price,
                        "order_price": price if side == "yes" else round(1 - price, 4),
                        "count": float(cnt + (rng.randint(1, 5) if partial else 0)), "close_time": close,
                        "status": "closed", "outcome": "expired" if partial else "filled",
                        "queue_ahead_at_join": float(rng.choice([0, 3, 12, 40, 75, 150, 400, 1500])),
                        "join_source": "book", "filled_count": float(cnt),
                        "fill_ids": [fid], "first_fill_utc": ts, "front_fill": rng.random() < 0.45,
                        "front_eval": True, "closed_utc": close if partial else ts, "updated_utc": ts})
            for k in range(rng.choice((0, 1, 1, 2))):     # quotes in the same cell that never filled
                uj = join - timedelta(hours=rng.uniform(0.2, 3))
                ut = f"{ticker}-U{k}"
                end = min(uj + timedelta(minutes=rng.randint(10, 300)), now)
                eps.append({"order_id": f"{oid}-u{k}", "ts": uj, "env": "demo", "series": s, "ticker": ut,
                            "side": side, "book_side": "bid" if side == "yes" else "ask", "price": price,
                            "order_price": price if side == "yes" else round(1 - price, 4), "count": float(cnt),
                            "close_time": uj + timedelta(hours=hours), "status": "closed",
                            "outcome": rng.choice(("expired", "cancelled", "cancelled", "stopped")),
                            "queue_ahead_at_join": float(rng.choice([40, 150, 400, 1500, 4000])),
                            "join_source": "book", "filled_count": 0.0, "fill_ids": [], "front_fill": None,
                            "front_eval": False, "closed_utc": end, "updated_utc": end})
            if rng.random() < 0.3:                         # an earlier order on the same book, re-quoted away
                eps.append({"order_id": f"{oid}-r", "ts": join - timedelta(minutes=30), "env": "demo", "series": s,
                            "ticker": ticker, "side": side, "book_side": "bid" if side == "yes" else "ask",
                            "price": round(price - 0.01, 2), "count": float(cnt), "status": "closed",
                            "outcome": "repriced", "queue_ahead_at_join": float(rng.randint(0, 300)),
                            "filled_count": 0.0, "fill_ids": [], "front_fill": None, "front_eval": False,
                            "close_time": close, "closed_utc": join, "updated_utc": join})
    for j in range(12):                                   # taker legs of executed arb sets
        ts = now - timedelta(hours=rng.randint(3, 60 * 24))
        ticker = f"{_ticker(rng, rng.choice(SERIES[:4]), ts)}-T{j}"
        price = round(rng.uniform(0.4, 0.7), 2)
        fills.append({"fill_id": f"demo-tfill-{j:03d}", "order_id": f"demo-tord-{j:03d}", "ticker": ticker,
                      "side": "no", "action": "buy", "count": 5, "yes_price_dollars": f"{1 - price:.4f}",
                      "no_price_dollars": f"{price:.4f}", "fee_cost": "0.0700", "is_taker": True, "env": "demo",
                      "created_time": _iso(ts).replace("+00:00", "Z")})
        settles[ticker] = {"ticker": ticker, "market_result": "no" if rng.random() < 0.85 else "yes",
                           "settled_time": _iso(ts + timedelta(hours=14)).replace("+00:00", "Z")}
        await store.add_arb_opportunity({"ts": ts, "event": ticker.rsplit("-", 2)[0], "kind": "NO", "contracts": 5.0,
                                         "profit": round(rng.uniform(0.1, 0.9), 4), "cost": round(5 * price, 4),
                                         "legs": [[ticker, "ask", round(1 - price, 2), 5.0]], "mode": "demo",
                                         "executed": "FILLED " + json.dumps({"legs": 1})})
    n_fills = await store.upsert_fills(fills)
    n_settles = await store.upsert_settlements(list(settles.values()))
    n_eps = await store.upsert_quote_episodes(eps)
    return {"fills": n_fills, "settlements": n_settles, "quote_episodes": n_eps, "arb_opportunities": 12}


def _main() -> None:
    from .store import MemoryStore

    async def go():
        store = MemoryStore()
        print(json.dumps(await seed(store), indent=2))
    asyncio.run(go())


if __name__ == "__main__":
    _main()

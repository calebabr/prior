"""Synthetic bulk rows so Analytics is built at the 180-day, 20,000-row size. Generic made-up tickers; prices are
drawn from whatever cells the installed config has (the same helper control_api.db.seed uses), so no strategy
value appears in this file."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from control_api.db import seed as base


async def seed_bulk(store, *, rows: int = 20000, days: int = 180, rng_seed: int = 11) -> dict:
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc)
    cfg = base._base_config()
    cells = base._cells(cfg)
    top_c = max(round(float(c[1]) * 100) for c in cells)
    series = list(cfg.get("series") or base.SERIES) or base.SERIES
    fills, settles = [], {}
    for i in range(rows):
        ts = now - timedelta(minutes=rng.randint(90, days * 24 * 60 - 60))
        cell = cells[rng.randrange(len(cells))]
        side = cell[4]
        price = base._price_in(rng, cell, top_c)
        close = ts + timedelta(hours=rng.uniform(float(cell[2]), max(float(cell[2]), float(cell[3]) - 0.05)))
        ticker = f"{base._ticker(rng, rng.choice(series), close)}-L{i % 997}"
        cnt = rng.randint(1, 12)
        fills.append({"fill_id": f"load-fill-{i:06d}", "order_id": f"load-ord-{i:06d}", "ticker": ticker,
                      "side": side, "action": "buy", "count": cnt,
                      "yes_price_dollars": f"{(1 - price) if side == 'no' else price:.4f}",
                      "no_price_dollars": f"{price if side == 'no' else (1 - price):.4f}",
                      "fee_cost": "0.0000", "is_taker": False, "env": "demo",
                      "created_time": ts.isoformat().replace("+00:00", "Z")})
        if close + timedelta(hours=2) < now and ticker not in settles:
            res = side if rng.random() < min(0.99, price) else ("yes" if side == "no" else "no")
            settles[ticker] = {"ticker": ticker, "market_result": res,
                               "close_time": close.isoformat().replace("+00:00", "Z"),
                               "settled_time": (close + timedelta(hours=3)).isoformat().replace("+00:00", "Z")}
    for k in range(0, len(fills), 2000):
        await store.upsert_fills(fills[k:k + 2000])
    await store.upsert_settlements(list(settles.values()))
    return {"bulk_fills": len(fills), "bulk_settlements": len(settles)}

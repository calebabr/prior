"""DEMO PLACEHOLDER STRATEGY (public stand-in). It holds no trading edge and places NO orders.

It exists so the public control API, its tests and CI run without the private strategy. It has the same
interface the control API uses: LOG, KILL, CONFIG_PATH, DEFAULTS, load_config, validate_config, in_cell, main, Quote, desired_quotes
(Quote and desired_quotes are the shadow-mode interface; here desired_quotes returns an empty list).
`main()` reads and validates maker_config.json, journals nothing and returns 0 without any network call.

The `cells` in DEFAULTS and maker_config.json are EXAMPLE values only (made up, not a strategy).

IS_STANDIN = True makes the control API refuse LIVE (real-money) mode. The private strategy is overlaid at deploy
time by scripts/overlay_private.py (see kalshi_bot/README.md, "Overlay contract"); the real file must not set
IS_STANDIN = True, and the overlay writes _overlay.py with IS_STANDIN = False.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

IS_STANDIN = True

LOG = Path(__file__).with_name("logs") / "maker_journal.csv"
KILL = Path(__file__).with_name("MAKER_KILL_SWITCH")
CONFIG_PATH = Path(__file__).with_name("maker_config.json")

JOURNAL_FIELDS = ["utc", "event", "ticker", "side", "price", "count", "cost", "cell", "note"]

# EXAMPLE settings. `cells` rows: (min_price, max_price, min_hours_to_close, max_hours_to_close, side).
DEFAULTS = {
    "run_budget": 10, "max_order": 5, "max_open_frac": 0.2, "poll_minutes": 10,
    "series": ["KXDEMOA", "KXDEMOB", "KXDEMOC", "KXDEMOD", "KXDEMOE", "KXDEMOF"],
    "cells": [(0.35, 0.50, 3, 9, "yes"), (0.50, 0.62, 9, 30, "no")],
}


def load_config(path: Path | None = None) -> dict:
    path = Path(path if path is not None else CONFIG_PATH)   # resolved at call time, not at import
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")})
    cfg["cells"] = [tuple(c) for c in cfg["cells"]]
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    """Reject out-of-range settings (same ranges and message format the control API expects)."""
    errs = []
    n = len(cfg["series"]) or 1
    if not isinstance(cfg["series"], list) or not cfg["series"]:
        errs.append("series: must list at least 1 market")
    elif not set(cfg["series"]) <= set(DEFAULTS["series"]):
        errs.append(f"series: only the confirmed markets are allowed {DEFAULTS['series']}")
    if not (1 <= float(cfg["max_order"]) <= 1000):
        errs.append("max_order: dollars per order, must be 1-1000")
    if not (n <= float(cfg["run_budget"]) <= 10000):
        errs.append(f"run_budget: dollars per run, must be {n}-10000 (at least $1 per market)")
    if not (0.05 <= float(cfg["max_open_frac"]) <= 0.9):
        errs.append("max_open_frac: fraction of balance, must be 0.05-0.9")
    if not (2 <= int(cfg["poll_minutes"]) <= 60):
        errs.append("poll_minutes: minutes, must be 2-60")
    if "record_decisions" in cfg and not isinstance(cfg["record_decisions"], bool):
        errs.append("record_decisions: must be true or false")   # optional on/off setting, default off
    if [tuple(c) for c in cfg["cells"]] != [tuple(c) for c in DEFAULTS["cells"]]:
        errs.append("cells: changed from the validated strategy; restore the original values")
    if errs:
        raise ValueError("maker_config.json invalid -> " + "; ".join(errs))


def in_cell(price: float, hours: float, side: str, cells=None):
    """First (example) cell matching price, hours to close and side: lo <= price < hi (+1e-9), h0 <= hours < h1."""
    for c in cells if cells is not None else DEFAULTS["cells"]:
        lo, hi, h0, h1, s = c
        if s == side and lo <= price < hi + 1e-9 and h0 <= hours < h1:
            return tuple(c)
    return None


def journal(event: str, note: str) -> None:
    """Append one journal row (same CSV columns as the bot), e.g. for a demo of the journal sync."""
    LOG.parent.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(JOURNAL_FIELDS)
        w.writerow([datetime.now(timezone.utc).isoformat(), event, "", "", "", "", "", "", note])


@dataclass(frozen=True)
class Quote:
    """One desired resting bid (shape only; the stand-in never creates one)."""
    ticker: str
    side: str
    price: float
    count: int
    book_side: str
    order_price: float
    cell: tuple

    @property
    def cost(self) -> float:
        return self.count * self.price


def desired_quotes(markets, books, now, max_order_dollars, budget) -> list:
    """Stand-in: places and plans nothing, so it always returns an empty list."""
    return []


def main() -> int:
    """One pass of the placeholder: validate the settings file and do nothing else (no quotes, no orders)."""
    cfg = load_config()
    print(f"stand-in strategy (demo placeholder): no quotes placed; {len(cfg['series'])} series configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

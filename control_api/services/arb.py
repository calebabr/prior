"""Bracket-arbitrage helpers: the arb_scanner import, the settings block (schema, validation, API ceilings),
the kill-switch reason and the opportunity doc. arb_scanner's math is used as is and never changed here.

Settings live in the maker config's "arb" block (stored and versioned with the maker settings; `cells` untouched).
On/off and live are NOT settings: they live in the store's state doc (arb_desired / arb_mode), so one Stop
write stops both strategies.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import botfiles

ARB_FIELDS = ("arb_max_contracts", "arb_min_profit", "arb_series", "arb_poll_seconds", "arb_scan_cap", "arb_day_cap",
              "arb_series_cap_frac", "arb_demo_execute", "arb_complete_legs",
              "arb_set_loss_frac", "arb_scan_cap_frac", "arb_day_cap_frac", "max_event_frac")
DEFAULT_MAX_CONTRACTS = 5          # small on purpose: a NO-arb set of N brackets costs about (N-1) x contracts $
DEFAULT_MIN_PROFIT = 0.25
DEFAULT_POLL_SECONDS = 15
POLL_MIN, POLL_MAX = 5, 60         # plan: 5-60 s (Kalshi read rate limit, Render's 0.1 CPU)
MIN_PROFIT_MIN, MIN_PROFIT_MAX = 0.01, 100.0
# risk gate (services/riskgate.py; owner decision 2026-10-21): spend caps per scan and per UTC day, and the share of
# the open-exposure ceiling one series may hold. arb_demo_execute: execute on the DEMO exchange (play money only).
DEFAULT_SCAN_CAP, SCAN_CAP_MIN, SCAN_CAP_MAX = 15.0, 1.0, 100.0
DEFAULT_DAY_CAP, DAY_CAP_MIN, DAY_CAP_MAX = 45.0, 1.0, 500.0
DEFAULT_SERIES_CAP_FRAC, SERIES_CAP_FRAC_MIN, SERIES_CAP_FRAC_MAX = 0.35, 0.05, 0.9
_RANGES = {"arb_scan_cap": (SCAN_CAP_MIN, SCAN_CAP_MAX, "USD per scan"),
           "arb_day_cap": (DAY_CAP_MIN, DAY_CAP_MAX, "USD per UTC day"),
           "arb_series_cap_frac": (SERIES_CAP_FRAC_MIN, SERIES_CAP_FRAC_MAX, "share of the open-exposure ceiling")}
# Sprint 2B items 3, 4 and 8: optional shares of equity (cash + open exposure). None = OFF, which is the default in
# this repo: no per-set loss cap is passed to the scanner (today's behaviour), the scan/day caps are exactly the dollar
# values above, and there is no per-event limit. When set, a cap is min(share x equity, the dollar cap).
FRAC_FIELDS = {"arb_set_loss_frac": (0.5, "share of equity one partial set may lose before the global kill switch"),
               "arb_scan_cap_frac": (1.0, "share of equity per scan (the dollar scan cap stays the maximum)"),
               "arb_day_cap_frac": (1.0, "share of equity per UTC day (the dollar day cap stays the maximum)"),
               "max_event_frac": (1.0, "share of equity one event may hold, maker and arbitrage together")}
EXPECTATION = ("Bracket arbitrage is rare and small at a small balance. Opportunities last seconds, so a "
               "15-second poll catches only some. The maker strategy is the main earner.")


def scanner():
    """kalshi_bot/arb_scanner.py as a top-level module (kalshi_bot is not a package)."""
    botfiles.ensure_bot_path()
    import arb_scanner
    return arb_scanner


def defaults() -> dict:
    return {"arb_max_contracts": DEFAULT_MAX_CONTRACTS, "arb_min_profit": DEFAULT_MIN_PROFIT,
            "arb_series": list(scanner().DEFAULT_SERIES), "arb_poll_seconds": DEFAULT_POLL_SECONDS,
            "arb_scan_cap": DEFAULT_SCAN_CAP, "arb_day_cap": DEFAULT_DAY_CAP,
            "arb_series_cap_frac": DEFAULT_SERIES_CAP_FRAC, "arb_demo_execute": False, "arb_complete_legs": False,
            **{k: None for k in FRAC_FIELDS}}


_GATE_DEFAULTS = {"arb_scan_cap": DEFAULT_SCAN_CAP, "arb_day_cap": DEFAULT_DAY_CAP,
                  "arb_series_cap_frac": DEFAULT_SERIES_CAP_FRAC}


def gate_caps(cfg: dict) -> dict:
    """The risk-gate caps from effective arb settings (services/riskgate.check_arb's `caps`)."""
    return {k: cfg.get(k, d) for k, d in _GATE_DEFAULTS.items()}


def frac_of(cfg: dict | None, key: str) -> float | None:
    """A share-of-equity setting as a float in (0, its ceiling], or None (OFF, or not a usable number)."""
    v = (cfg or {}).get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    hi = FRAC_FIELDS[key][0]
    return float(v) if 0 < float(v) <= hi else None


def _usable_equity(equity: Any) -> float | None:
    if isinstance(equity, bool) or not isinstance(equity, (int, float)) or equity != equity:
        return None
    return float(equity) if 0 < float(equity) < float("inf") else None


def effective_caps(cfg: dict, equity: Any) -> dict:
    """gate_caps with the item-4 percentage caps applied: when arb_scan_cap_frac / arb_day_cap_frac is set and equity
    is known, that cap is min(share x equity, the dollar cap). Unset share or unknown equity: the dollar cap exactly.
    max_event_frac is passed through (None = no per-event limit)."""
    caps = gate_caps(cfg)
    eq = _usable_equity(equity)
    for key, frac_key in (("arb_scan_cap", "arb_scan_cap_frac"), ("arb_day_cap", "arb_day_cap_frac")):
        f = frac_of(cfg, frac_key)
        if f is not None and eq is not None:
            try:
                caps[key] = round(min(float(caps[key]), f * eq), 6)
            except (TypeError, ValueError):
                pass                           # a bad dollar value is refused by the gate itself (fails closed)
    caps["max_event_frac"] = frac_of(cfg, "max_event_frac")
    return caps


def set_loss_cap(cfg: dict, equity: Any) -> float | None:
    """Item 3: the per-set loss cap in dollars passed to arb_scanner.execute (arb_set_loss_frac x equity), or None
    when the share is unset or equity is unknown (None = today's behaviour: any partial set trips the kill switch)."""
    f, eq = frac_of(cfg, "arb_set_loss_frac"), _usable_equity(equity)
    return round(f * eq, 6) if f is not None and eq is not None else None


def current(config: dict | None) -> dict:
    """Effective arb settings: defaults overlaid with the config's "arb" block (unknown keys dropped)."""
    out = defaults()
    block = (config or {}).get("arb")
    if isinstance(block, dict):
        out.update({k: v for k, v in block.items() if k in ARB_FIELDS})
    return out


def schema(settings: Any) -> dict:
    return {
        "arb_max_contracts": {"type": "integer", "unit": "contracts per leg", "min": 1,
                              "max": int(settings.max_arb_contracts),
                              "help": "Largest set size. A NO-arb on N brackets costs about (N-1) x this in $.",
                              "editable": True},
        "arb_min_profit": {"type": "number", "unit": "USD per set after fees", "min": MIN_PROFIT_MIN,
                           "max": MIN_PROFIT_MAX, "help": "Smallest guaranteed profit worth acting on.",
                           "editable": True},
        "arb_series": {"type": "list", "unit": "Kalshi series", "min": 1, "max": len(scanner().DEFAULT_SERIES),
                       "options": list(scanner().DEFAULT_SERIES),
                       "help": "Bracket-event series to scan.", "editable": True},
        "arb_poll_seconds": {"type": "integer", "unit": "seconds", "min": POLL_MIN, "max": POLL_MAX,
                             "help": "How often to scan (keeps under Kalshi's read rate limit).",
                             "editable": True},
        "arb_scan_cap": {"type": "number", "unit": "USD per scan", "min": SCAN_CAP_MIN, "max": SCAN_CAP_MAX,
                         "help": "Most money one scan may put into arbitrage sets.", "editable": True},
        "arb_day_cap": {"type": "number", "unit": "USD per UTC day", "min": DAY_CAP_MIN, "max": DAY_CAP_MAX,
                        "help": "Most money arbitrage may spend in one UTC day.", "editable": True},
        "arb_series_cap_frac": {"type": "number", "unit": "share of the open-exposure ceiling",
                                "min": SERIES_CAP_FRAC_MIN, "max": SERIES_CAP_FRAC_MAX,
                                "help": "Largest share of the open-exposure ceiling one series may hold before an "
                                        "arbitrage set is refused (counts all open positions; applies to "
                                        "arbitrage only).", "editable": True},
        "arb_demo_execute": {"type": "boolean", "unit": "demo exchange only",
                             "help": "In demo mode, place the sets on the Kalshi DEMO exchange (play money) "
                                     "instead of only logging them. Never touches real money.", "editable": True},
        "arb_complete_legs": {"type": "boolean", "unit": "demo exchange only",
                              "help": "With demo-execute on: if a set fills only partly, try ONCE to buy the missing "
                                      "legs at the current price, only when the whole set still cannot lose money "
                                      "and the caps allow it. Otherwise the kill switch stops arbitrage as before. "
                                      "Never used in live mode.", "editable": True},
        **{k: {"type": "number", "unit": unit, "min": 0, "max": hi, "nullable": True, "default": None,
               "help": f"Optional {unit}. Empty (null) = off.", "editable": True}
           for k, (hi, unit) in FRAC_FIELDS.items()},
    }


def ceilings(settings: Any) -> dict:
    return {"arb_max_contracts": int(settings.max_arb_contracts), "arb_poll_seconds": POLL_MAX,
            "arb_scan_cap": SCAN_CAP_MAX, "arb_day_cap": DAY_CAP_MAX, "arb_series_cap_frac": SERIES_CAP_FRAC_MAX,
            **{k: hi for k, (hi, _u) in FRAC_FIELDS.items()}}


def _whole(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and int(v) == v


def validate(cur: dict, update: dict, settings: Any) -> dict:
    """Merge a partial arb-settings update into `cur`; raise ValueError([msgs]) on any problem."""
    errs: list[str] = []
    out = dict(cur)
    allowed = set(scanner().DEFAULT_SERIES)
    for k, v in update.items():
        if k not in ARB_FIELDS:
            errs.append(f"{k}: not an arbitrage setting")
            continue
        if k == "arb_max_contracts":
            if not _whole(v):
                errs.append("arb_max_contracts: must be a whole number")
            elif not 1 <= int(v) <= int(settings.max_arb_contracts):
                errs.append(f"arb_max_contracts: contracts per leg, must be 1-{int(settings.max_arb_contracts)} "
                            "(API ceiling)")
            else:
                out[k] = int(v)
        elif k == "arb_min_profit":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                errs.append("arb_min_profit: must be a number")
            elif not MIN_PROFIT_MIN <= float(v) <= MIN_PROFIT_MAX:
                errs.append(f"arb_min_profit: USD per set, must be {MIN_PROFIT_MIN}-{MIN_PROFIT_MAX:g}")
            else:
                out[k] = round(float(v), 4)
        elif k == "arb_poll_seconds":
            if not _whole(v):
                errs.append("arb_poll_seconds: must be a whole number")
            elif not POLL_MIN <= int(v) <= POLL_MAX:
                errs.append(f"arb_poll_seconds: seconds, must be {POLL_MIN}-{POLL_MAX}")
            else:
                out[k] = int(v)
        elif k == "arb_series":
            if not isinstance(v, list) or not v or not all(isinstance(s, str) for s in v):
                errs.append("arb_series: must list at least 1 series")
            elif len(v) != len(set(v)):
                errs.append("arb_series: duplicate entries are not allowed")
            elif not set(v) <= allowed:
                errs.append(f"arb_series: only these series are allowed {sorted(allowed)}")
            else:
                out[k] = list(v)
        elif k in _RANGES:
            lo, hi, unit = _RANGES[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                errs.append(f"{k}: must be a number")
            elif not lo <= float(v) <= hi:
                errs.append(f"{k}: {unit}, must be {lo:g}-{hi:g}")
            else:
                out[k] = round(float(v), 4)
        elif k in FRAC_FIELDS:
            hi = FRAC_FIELDS[k][0]
            if v is None:
                out[k] = None                  # clears it: the feature is OFF again
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                errs.append(f"{k}: must be a number or null (off)")
            elif not 0 < float(v) <= hi:
                errs.append(f"{k}: {FRAC_FIELDS[k][1]}, must be more than 0 and at most {hi:g} (API ceiling)")
            else:
                out[k] = round(float(v), 6)
        elif k in ("arb_demo_execute", "arb_complete_legs"):
            if not isinstance(v, bool):
                errs.append(f"{k}: must be true/false")
            else:
                out[k] = v
    if errs:
        raise ValueError(errs)
    return out


def demo_base_ok(client: Any) -> bool:
    """True only if the client's base URL is one of the Kalshi DEMO exchange URLs and none of the production ones.
    Checked on the client actually used to send, not on a mode name. A client without a base URL is refused."""
    base = str(getattr(client, "base_url", "") or "").strip().rstrip("/").lower()
    if not base:
        return False
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    demo = {str(BASE_URLS[k]).rstrip("/").lower() for k in ("demo", "demo_alt") if k in BASE_URLS}
    prod = {str(BASE_URLS[k]).rstrip("/").lower() for k in ("prod", "prod_alt") if k in BASE_URLS}
    if base not in demo or base in prod:
        return False
    host = urlsplit(base).hostname or ""
    return ".demo." in f".{host}" or host.startswith("demo-")      # belt and braces: the host names the demo


def kill_reason(path: Path) -> str | None:
    """None if no arb kill switch; "legging" if arb_scanner (or the runner) wrote it after a partial set or a
    failed leg; "stop" if Stop wrote it; "unknown" if it can't be read (treated like legging by the API)."""
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    if text.startswith("stopped by"):
        return "stop"
    try:
        doc = json.loads(text)
        if isinstance(doc, dict) and "legging" in str(doc.get("reason", "")):
            return "legging"
    except ValueError:
        pass
    return "unknown"


def opportunity_doc(op: Any, executed: str, mode: str) -> dict:
    d = asdict(op) if is_dataclass(op) else dict(op)
    d["legs"] = [list(leg) for leg in d.get("legs") or []]
    d["executed"] = executed
    d["mode"] = mode
    return d

"""Loss / drawdown circuit breaker (Sprint 3 L1). Pure functions, no I/O.

The runner calls ``evaluate`` after each maker pass (with the pass's account snapshot equity, cash + open position
value) and after each arbitrage set it sent (the gate's own cash + exposure read, minus a contained partial's loss).
No new network call and no new store read is made for it. Two limits, both shares of equity:

* **drawdown**: equity at or below ``(1 - drawdown_frac) x`` the running high-water mark of equity.
* **day loss**: start-of-day equity minus equity (realised plus marked, since 00:00 America/New_York) at or beyond
  ``day_loss_frac x`` the start-of-day equity.

On a trip the runner runs the normal full Stop (maker and arbitrage), persists the trip, sends one urgent ``breaker``
alert and writes one ``breaker.trip`` audit row. While tripped nothing is evaluated again (one alert per trip). Start or
arbitrage enable then needs fresh 2FA plus ``ack_breaker: true``; the acknowledgement clears the trip and re-bases the
high-water mark and the day start to the last equity seen (the owner accepted the loss so far).

Two cloud-settings keys, neither editable through ``PUT /api/cloud``, neither holding a secret:

* ``breaker`` = {live_enabled, demo_enabled, drawdown_frac, day_loss_frac, updated, by}: the owner's settings
  (``PUT /api/breaker``, fresh 2FA, API ceilings).
* ``breaker_state`` = {tripped: trip doc | null, envs: {live|demo: {hwm, hwm_utc, day, day_start_equity,
  last_equity, last_utc}}}: survives restarts and leader changes.

Bad data never trips and never raises: an unusable equity or an unreadable state skips the check and leaves a
visible warning. Defaults are the owner-approved lines (fractions of equity, no dollar figures).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

CLOUD_SETTINGS = "breaker"
CLOUD_STATE = "breaker_state"
ENVS = ("live", "demo")
REASONS = ("drawdown", "day_loss")
BOOL_FIELDS = ("live_enabled", "demo_enabled")
FRAC_FIELDS = ("drawdown_frac", "day_loss_frac")
FIELDS = BOOL_FIELDS + FRAC_FIELDS
DEFAULTS: dict[str, Any] = {"live_enabled": True, "demo_enabled": False, "drawdown_frac": 0.15,
                            "day_loss_frac": 0.10}
CEILINGS: dict[str, tuple[float, float]] = {"drawdown_frac": (0.02, 0.30), "day_loss_frac": (0.01, 0.25)}
MARKET_TZ = ZoneInfo("America/New_York")
EPS = 1e-9                                   # float slack so "exactly at the limit" trips
DAY_SEED_MAX_AGE = timedelta(hours=6)        # the last equity of the previous day seeds the new day only if recent
PERSIST_EVERY = timedelta(minutes=30)        # last_equity alone is persisted at most this often
STR_MAX = 120
ALERT_TITLE = "Circuit breaker tripped"      # services/alerts.classify -> "breaker" (urgent, never grouped)
SCHEMA = {
    "live_enabled": {"type": "boolean", "help": "Trip in live mode (on by default)."},
    "demo_enabled": {"type": "boolean", "help": "Trip in demo mode (off by default)."},
    "drawdown_frac": {"type": "number", "min": CEILINGS["drawdown_frac"][0], "max": CEILINGS["drawdown_frac"][1],
                      "help": "Stop when equity falls this share below its high-water mark."},
    "day_loss_frac": {"type": "number", "min": CEILINGS["day_loss_frac"][0], "max": CEILINGS["day_loss_frac"][1],
                      "help": "Stop when today's loss (since 00:00 New York) reaches this share of the day's "
                              "starting equity."},
}


# ------------------------------------------------------------------ small helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _pos(v: Any) -> float | None:
    v = _num(v)
    return v if v is not None and v > 0 else None


def _iso(ts: Any) -> str | None:
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str) or not v:
        return None
    try:
        ts = datetime.fromisoformat(v[:40])
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _s(v: Any, n: int = STR_MAX) -> str:
    return str(v)[:n] if v is not None else ""


def market_day(ts: datetime) -> str:
    """The America/New_York date of `ts` (the day-loss window starts at 00:00 there)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(MARKET_TZ).date().isoformat()


def env_of(mode: Any) -> str:
    return "live" if mode == "live" else "demo"


def snapshot_equity(snap: Any) -> float | None:
    """Cash + open position value of an account snapshot, or None when either part is missing or not a finite
    number (a missing exposure would understate equity, so it is never read as zero here)."""
    if not isinstance(snap, Mapping):
        return None
    cash, exp = _num(snap.get("cash")), _num(snap.get("exposure"))
    if cash is None or exp is None:
        return None
    return cash + exp


# ------------------------------------------------------------------ settings
def settings_of(raw: Any) -> dict:
    """The stored ``breaker`` block -> effective settings. The store is not trusted: a field with the wrong type or
    outside its API ceiling falls back to its default (the owner-approved line), never to "off"."""
    out = dict(DEFAULTS)
    out["updated"], out["by"] = None, None
    if not isinstance(raw, Mapping):
        return out
    for k in BOOL_FIELDS:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    for k, (lo, hi) in CEILINGS.items():
        v = _num(raw.get(k))
        if v is not None and lo <= v <= hi:
            out[k] = v
    out["updated"] = _iso(_parse_ts(raw.get("updated"))) if raw.get("updated") else None
    out["by"] = _s(raw.get("by"), 64) or None
    return out


def validate_update(current: Mapping, body: Any) -> tuple[dict, list[str]]:
    """Strict partial update -> (the four new values, the fields loosened). ValueError([messages]) on any problem:
    a non-object body, an unknown key, a wrong type (bool must be bool, a share a number, never a bool or a string)
    or a share outside its API ceiling. "Loosened" = a share raised or a mode switched off (audited as such)."""
    if not isinstance(body, Mapping):
        raise ValueError(["body: must be a JSON object"])
    errs: list[str] = []
    new = {k: current.get(k, DEFAULTS[k]) for k in FIELDS}
    for k, v in body.items():
        if k not in FIELDS:
            errs.append(f"{str(k)[:40]}: unknown field")
            continue
        if k in BOOL_FIELDS:
            if not isinstance(v, bool):
                errs.append(f"{k}: must be true/false")
                continue
            new[k] = v
            continue
        x = _num(v)
        lo, hi = CEILINGS[k]
        if x is None:
            errs.append(f"{k}: must be a number")
        elif not (lo <= x <= hi):
            errs.append(f"{k}: must be between {lo} and {hi}")
        else:
            new[k] = round(x, 6)
    if errs:
        raise ValueError(errs)
    loosened = [k for k in FRAC_FIELDS if new[k] > float(current.get(k, DEFAULTS[k])) + EPS]
    loosened += [k for k in BOOL_FIELDS if current.get(k, DEFAULTS[k]) is True and new[k] is False]
    return new, loosened


# ------------------------------------------------------------------ state
def empty_state() -> dict:
    return {"tripped": None, "envs": {}}


def _norm_trip(raw: Any) -> dict | None:
    if raw is None or raw is False:
        return None
    if not isinstance(raw, Mapping):
        # something is there but unreadable: stay tripped (an acknowledged Start clears it)
        return {"reason": "unknown", "limits": [], "env": "", "utc": None, "detail": "stored trip unreadable"}
    reason = raw.get("reason") if raw.get("reason") in REASONS else "unknown"
    out: dict[str, Any] = {"reason": reason, "env": raw.get("env") if raw.get("env") in ENVS else "",
                           "utc": _iso(_parse_ts(raw.get("utc"))), "limits": []}
    lim = raw.get("limits")
    if isinstance(lim, list):
        out["limits"] = [x for x in lim if x in REASONS]
    for k in ("equity", "hwm", "drawdown", "drawdown_frac", "day_start_equity", "day_loss", "day_loss_frac"):
        out[k] = _num(raw.get(k))
    out["day"] = _s(raw.get("day"), 10) or None
    out["source"] = _s(raw.get("source"), 20) or None
    return out


def _norm_env(raw: Any) -> tuple[dict | None, bool]:
    """(entry, ok). ok is False when something was stored but is unusable (re-seeded with a warning)."""
    if raw is None:
        return None, True
    if not isinstance(raw, Mapping):
        return None, False
    hwm, last = _pos(raw.get("hwm")), _pos(raw.get("last_equity"))
    day, start = raw.get("day"), _pos(raw.get("day_start_equity"))
    if hwm is None:
        return None, False
    out = {"hwm": hwm, "hwm_utc": _iso(_parse_ts(raw.get("hwm_utc"))),
           "day": day if isinstance(day, str) and len(day) == 10 else None, "day_start_equity": start,
           "last_equity": last, "last_utc": _iso(_parse_ts(raw.get("last_utc")))}
    if out["day"] is None or start is None:
        out["day"], out["day_start_equity"] = None, None     # the day start is re-seeded at the next observation
    return out, True


def normalize_state(raw: Any) -> tuple[dict, list[str]]:
    """Stored ``breaker_state`` -> (state, warnings). Never raises. None (never saved) is the empty state."""
    warnings: list[str] = []
    if raw is None:
        return empty_state(), warnings
    if not isinstance(raw, Mapping):
        return {"tripped": _norm_trip(True if raw else None), "envs": {}}, ["stored breaker state unreadable"]
    envs: dict[str, dict] = {}
    raw_envs = raw.get("envs") if isinstance(raw.get("envs"), Mapping) else {}
    for e in ENVS:
        entry, ok = _norm_env(raw_envs.get(e))
        if entry is not None:
            envs[e] = entry
        if not ok:
            warnings.append(f"{e}: stored high-water mark unreadable; re-based at the next equity reading")
    return {"tripped": _norm_trip(raw.get("tripped")), "envs": envs}, warnings


def encode_state(state: Mapping) -> dict:
    """State -> stored form (plain JSON types only)."""
    envs = {}
    for e, d in (state.get("envs") or {}).items():
        if e in ENVS and isinstance(d, Mapping):
            envs[e] = {k: d.get(k) for k in ("hwm", "hwm_utc", "day", "day_start_equity", "last_equity", "last_utc")}
    trip = state.get("tripped")
    return {"tripped": dict(trip) if isinstance(trip, Mapping) else None, "envs": envs}


# ------------------------------------------------------------------ the check
def evaluate(settings: Mapping, state: Mapping, env: str, equity: Any, ts: Any, *,
             saved_utc: datetime | None = None, source: str = "maker") -> dict:
    """One equity observation -> {state, trip, warning, persist}. Pure; never raises on bad input.

    * Unusable equity (not a finite number > 0) or time: nothing changes, warning set, never a trip.
    * Already tripped: nothing is evaluated (no second alert).
    * The high-water mark and the day start are tracked whether or not the mode's breaker is enabled; a trip needs
      `<env>_enabled`. A new New York day starts from the previous reading when it is recent (DAY_SEED_MAX_AGE), so an
      overnight loss counts toward the new day; otherwise from this reading.
    * `persist` is True when the stored copy should be written: a trip, a new high-water mark, a new day, or the last
      reading not saved for PERSIST_EVERY."""
    out = {"state": state, "trip": None, "warning": None, "persist": False}
    try:
        if env not in ENVS:
            out["warning"] = "unknown environment: check skipped"
            return out
        eq = _pos(equity)
        when = _parse_ts(ts)
        if eq is None or when is None:
            out["warning"] = f"{env}: equity reading unusable; check skipped"
            return out
        if state.get("tripped"):
            return out
        envs = dict(state.get("envs") or {})
        prev = dict(envs.get(env) or {})
        persist = False
        hwm = _pos(prev.get("hwm"))
        if hwm is None:
            hwm, hwm_utc, persist = eq, _iso(when), True              # first reading: the mark starts here
        elif eq > hwm:
            hwm, hwm_utc, persist = eq, _iso(when), True
        else:
            hwm_utc = prev.get("hwm_utc")
        day = market_day(when)
        start = _pos(prev.get("day_start_equity"))
        if prev.get("day") != day or start is None:
            last, last_ts = _pos(prev.get("last_equity")), _parse_ts(prev.get("last_utc"))
            recent = last is not None and last_ts is not None and timedelta(0) <= when - last_ts <= DAY_SEED_MAX_AGE
            start = last if recent and prev.get("day") not in (None, day) else eq
            persist = True
        new_env = {"hwm": hwm, "hwm_utc": hwm_utc, "day": day, "day_start_equity": start, "last_equity": eq,
                   "last_utc": _iso(when)}
        envs[env] = new_env
        if saved_utc is None or when - saved_utc >= PERSIST_EVERY:
            persist = True
        new_state = {"tripped": None, "envs": envs}
        out["state"] = new_state
        drawdown = max(0.0, hwm - eq)
        day_loss = max(0.0, start - eq)
        dd_frac, dl_frac = float(settings.get("drawdown_frac")), float(settings.get("day_loss_frac"))
        limits = []
        if drawdown > 0 and drawdown >= dd_frac * hwm - EPS:
            limits.append("drawdown")
        if day_loss > 0 and day_loss >= dl_frac * start - EPS:
            limits.append("day_loss")
        if limits and settings.get(f"{env}_enabled") is True:
            trip = {"reason": limits[0], "limits": limits, "env": env, "utc": _iso(when), "day": day,
                    "equity": round(eq, 6), "hwm": round(hwm, 6), "drawdown": round(drawdown, 6),
                    "drawdown_frac": dd_frac, "day_start_equity": round(start, 6), "day_loss": round(day_loss, 6),
                    "day_loss_frac": dl_frac, "source": _s(source, 20)}
            new_state["tripped"] = trip
            out["trip"] = trip
            persist = True
        out["persist"] = persist
        return out
    except Exception as e:  # noqa: BLE001   (bad data never trips and never raises)
        return {"state": state, "trip": None, "warning": f"check skipped: {type(e).__name__}", "persist": False}


def acknowledge(state: Mapping) -> dict:
    """The acknowledged restart: no trip, and each mode's high-water mark and day start re-based to its last reading
    (otherwise the very next pass would trip again on the loss the owner just accepted)."""
    envs = {}
    for e, d in (state.get("envs") or {}).items():
        if e not in ENVS or not isinstance(d, Mapping):
            continue
        last = _pos(d.get("last_equity"))
        if last is None:
            continue
        envs[e] = {"hwm": last, "hwm_utc": d.get("last_utc"), "day": d.get("day"), "day_start_equity": last,
                   "last_equity": last, "last_utc": d.get("last_utc")}
    return {"tripped": None, "envs": envs}


# ------------------------------------------------------------------ views
def env_view(settings: Mapping, d: Mapping | None, env: str) -> dict:
    d = d or {}
    hwm, last, start = _pos(d.get("hwm")), _pos(d.get("last_equity")), _pos(d.get("day_start_equity"))
    drawdown = round(max(0.0, hwm - last), 2) if hwm is not None and last is not None else None
    day_loss = round(max(0.0, start - last), 2) if start is not None and last is not None else None
    return {"enabled": settings.get(f"{env}_enabled") is True,
            "hwm": round(hwm, 2) if hwm is not None else None, "hwm_utc": d.get("hwm_utc"),
            "drawdown": drawdown, "drawdown_share": round(drawdown / hwm, 4) if drawdown is not None and hwm else None,
            "day": d.get("day"), "day_start_equity": round(start, 2) if start is not None else None,
            "day_loss": day_loss,
            "day_loss_share": round(day_loss / start, 4) if day_loss is not None and start else None,
            "last_equity": round(last, 2) if last is not None else None, "last_utc": d.get("last_utc")}


def status_view(settings: Mapping, state: Mapping, *, readable: bool, warning: str | None, mode: Any) -> dict:
    trip = state.get("tripped")
    envs = state.get("envs") or {}
    return {"readable": bool(readable), "tripped": bool(trip),
            "trip": dict(trip) if isinstance(trip, Mapping) else None, "warning": warning, "env": env_of(mode),
            "envs": {e: env_view(settings, envs.get(e), e) for e in ENVS}}


def short_status(settings: Mapping, state: Mapping, *, readable: bool, warning: str | None, mode: Any) -> dict:
    """The few fields /api/status carries (the full view is GET /api/breaker)."""
    trip = state.get("tripped")
    env = env_of(mode)
    return {"tripped": bool(trip), "reason": trip.get("reason") if isinstance(trip, Mapping) else None,
            "utc": trip.get("utc") if isinstance(trip, Mapping) else None,
            "enabled": settings.get(f"{env}_enabled") is True, "readable": bool(readable), "warning": warning}


def _money(v: Any) -> str:
    v = _num(v)
    return f"${v:,.2f}" if v is not None else "?"


def _pct(v: Any) -> str:
    v = _num(v)
    return f"{v * 100:.1f}%" if v is not None else "?"


def alert(trip: Mapping) -> dict:
    """The urgent alert for a trip: which limit, the numbers, and what the owner must do."""
    env = trip.get("env") or "?"
    limits = [x for x in (trip.get("limits") or [trip.get("reason")]) if x in REASONS]
    parts = []
    for which in limits:
        if which == "drawdown":
            hwm, dd = _num(trip.get("hwm")), _num(trip.get("drawdown"))
            share = dd / hwm if hwm and dd is not None else None
            parts.append(f"drawdown: equity {_money(trip.get('equity'))} is {_money(dd)} ({_pct(share)}) below the "
                         f"high-water mark {_money(hwm)}; limit {_pct(trip.get('drawdown_frac'))}")
        else:
            start, dl = _num(trip.get("day_start_equity")), _num(trip.get("day_loss"))
            share = dl / start if start and dl is not None else None
            parts.append(f"day loss: {_money(dl)} ({_pct(share)}) since 00:00 New York from "
                         f"{_money(start)}; limit {_pct(trip.get('day_loss_frac'))}")
    which = " and ".join("drawdown" if x == "drawdown" else "day loss" for x in limits) or "loss limit"
    title = f"{ALERT_TITLE}: {which} ({env})"
    body = (f"{'; '.join(parts) or 'a loss limit was reached'}. Maker and arbitrage are stopped and resting bot "
            "orders were cancelled. To trade again: Start with a fresh 2FA code and acknowledge the breaker.")
    return {"type": "breaker", "title": title[:200], "body": body[:2000], "priority": "urgent", "channels": []}


def audit_details(trip: Mapping) -> dict:
    return {k: trip.get(k) for k in ("reason", "limits", "env", "utc", "equity", "hwm", "drawdown", "drawdown_frac",
                                     "day_start_equity", "day_loss", "day_loss_frac", "source")}


__all__ = ["ALERT_TITLE", "CEILINGS", "CLOUD_SETTINGS", "CLOUD_STATE", "DEFAULTS", "ENVS", "FIELDS", "SCHEMA",
           "acknowledge", "alert", "audit_details", "empty_state", "encode_state", "env_of", "evaluate",
           "market_day", "normalize_state", "settings_of", "short_status", "status_view", "validate_update"]

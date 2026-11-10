"""Arbitrage read models (UI-1a): distinct sets and gate outcomes recomputed from the STORED arbitrage log, and one
grouped row per executed set (legs, status, guaranteed vs realized).

Pure functions over plain store docs / trade rows; no I/O, no scan or execute logic (those stay in the runner and
arb_scanner). The stored log has ONE doc per distinct signature (a log-only sighting identical to one seen within
the dedupe window only bumps `seen_count`), so counting docs counts distinct sets and summing `seen_count` counts
sightings; neither is affected by a restart, unlike the in-memory scan counters.

Doc `executed` values (runner / arb_scanner): "log-only", "FILLED {ticker: n, ...}", "FILLED (completed) {...} ...",
"PARTIAL {...} ...", "ERROR <Type>", "skipped: <reason>". Legs are [ticker, book_side, limit_yes_price, contracts];
a NO set buys NO at 1 - limit, a YES set buys YES at limit.
"""
from __future__ import annotations

import ast
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .. import cpuyield as cy
from . import pnl as pnlsvc

OUTCOMES = ("filled", "partial", "error", "skipped", "log_only")
EXECUTED_OUTCOMES = ("filled", "partial", "error")
ENV_MODES = {"demo": ("demo", "demo-execute"), "live": ("live",)}
WINDOW_BEFORE = timedelta(minutes=10)      # a set's fills precede its log doc (written after the scan); same window
WINDOW_AFTER = timedelta(minutes=2)        # services.strategies uses to label arbitrage fills
MAX_SETS = 200
_RULES = (("total exposure", "total_exposure_ceiling"), ("series cap", "series_cap"), ("scan cap", "scan_cap"),
          ("day cap", "day_cap"), ("opposite set", "opposite_set"), ("cost ", "set_cost_cap"))


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


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat() if isinstance(d, datetime) else None


def outcome_of(executed: Any) -> str:
    """filled | partial | error | skipped | log_only for a doc's `executed` value."""
    s = str(executed or "")
    if s.startswith("FILLED"):
        return "filled"
    if s.startswith("PARTIAL"):
        return "partial"
    if s.startswith("ERROR"):
        return "error"
    if s.startswith("skipped"):
        return "skipped"
    return "log_only"


def skip_rule(executed: Any) -> str:
    """A stable key for the rule that skipped a set, so the numbers in the reason do not split the counts."""
    s = str(executed or "")
    body = s.split(":", 1)[1].strip() if ":" in s else s
    if body.startswith("gate "):
        body = body[5:]
    low = body.lower()
    for needle, key in _RULES:
        if low.startswith(needle):
            return key
    slug = re.sub(r"[-+]?\$?\d[\d.,]*", "", low)
    slug = re.sub(r"[^a-z]+", "_", slug).strip("_")[:40]
    return slug or "other"


def parse_filled(executed: Any) -> dict[str, float]:
    """{ticker: filled contracts} from "FILLED {...}" / "PARTIAL {...}"; {} when absent or unreadable. Only a plain
    dict of str -> number literal is accepted (ast.literal_eval; nothing is executed)."""
    s = str(executed or "")
    i, j = s.find("{"), s.find("}")
    if i < 0 or j < i:
        return {}
    try:
        d = ast.literal_eval(s[i:j + 1])
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in d.items():
        # finite, non-negative and not absurd only: "inf" must never read as a full fill (fail closed: skipped)
        if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        try:
            x = float(v)
        except OverflowError:
            continue
        if math.isfinite(x) and 0 <= x <= pnlsvc.MAX_FILL_COUNT:
            out[k] = x
    return out


def env_match(doc: dict, env: str) -> bool:
    """log-only sightings carry no environment; skipped / executed docs carry the execution mode."""
    if env == "all":
        return True
    mode = str(doc.get("mode") or "log-only")
    return mode == "log-only" or mode in ENV_MODES.get(env, (env,))


def _legs(doc: dict) -> list[dict]:
    kind = doc.get("kind")
    out = []
    for leg in doc.get("legs") or []:
        try:
            ticker, _side, limit_yes, n = leg[0], leg[1], float(leg[2]), float(leg[3])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        paid = (1.0 - limit_yes) if kind == "NO" else limit_yes
        out.append({"ticker": str(ticker), "side": "no" if kind == "NO" else "yes", "price": round(paid, 4),
                    "contracts": n})
    return out


def counters(docs: Iterable[dict], *, env: str, since: datetime | None = None, until: datetime | None = None
             ) -> dict:
    """{sets, events, sightings, by_outcome, skipped_by_rule} over log docs first seen in [since, until)."""
    by = dict.fromkeys(OUTCOMES, 0)
    rules: dict[str, int] = {}
    events: set[str] = set()
    sets = sightings = 0
    for d in docs:
        cy.checkpoint()
        t = _ts(d.get("ts")) if isinstance(d, dict) else None
        if t is None or (since is not None and t < since) or (until is not None and t >= until):
            continue
        if not env_match(d, env):
            continue
        o = outcome_of(d.get("executed"))
        by[o] += 1
        if o == "skipped":
            k = skip_rule(d.get("executed"))
            rules[k] = rules.get(k, 0) + 1
        sets += 1
        try:
            sightings += max(1, int(d.get("seen_count") or 1))
        except (TypeError, ValueError):
            sightings += 1
        events.add(str(d.get("event") or ""))
    return {"sets": sets, "events": len(events), "sightings": sightings, "by_outcome": by,
            "skipped_by_rule": dict(sorted(rules.items(), key=lambda kv: (-kv[1], kv[0])))}


def build_sets(docs: Iterable[dict], rows: Iterable[dict], *, env: str, since: datetime | None = None,
               until: datetime | None = None, limit: int = MAX_SETS) -> list[dict]:
    """One row per EXECUTED set (FILLED / PARTIAL / ERROR docs) first seen in [since, until), newest first.

    `rows` are trade rows (services.analytics.trade_rows) of arbitrage fills: each fill is matched to the set whose
    log time is nearest after it (same ticker and side, inside the arbitrage window), so a fill is never counted
    in two sets. Row: {set_id, event, kind, mode, executed_utc, outcome, contracts, cost, guaranteed_profit, status,
    complete, legs: [{ticker, side, price, contracts, filled, settled, result, pnl}], realized_pnl, settled_utc,
    notes}. status: PARTIAL (a leg short of its size), FILLED (all legs filled, none settled yet), OPEN (all legs
    filled, some settled), SETTLED (every filled leg settled; realized_pnl = sum of the fills' P&L, fees included,
    else null)."""
    sets = []
    for d in docs:
        cy.checkpoint()
        t = _ts(d.get("ts")) if isinstance(d, dict) else None
        if t is None or not env_match(d, env) or outcome_of(d.get("executed")) not in EXECUTED_OUTCOMES:
            continue
        if (since is not None and t < since) or (until is not None and t >= until):
            continue
        sets.append({"doc": d, "t": t, "legs": _legs(d), "rows": {}})
    if not sets:
        return []
    index: dict[tuple[str, str], list[dict]] = {}
    for s in sets:
        for leg in s["legs"]:
            index.setdefault((leg["ticker"], leg["side"]), []).append(s)
    for r in rows:
        cy.checkpoint()
        if r.get("strategy") != "arb":
            continue
        cands = [s for s in index.get((r.get("ticker"), r.get("side")), ())
                 if s["t"] - WINDOW_BEFORE <= r["_ts"] <= s["t"] + WINDOW_AFTER]
        # the set logged soonest after the fill; a fill logged slightly before its doc is the common case
        cands = [s for s in cands if s["t"] >= r["_ts"]] or cands
        if cands:
            best = min(cands, key=lambda s: abs((s["t"] - r["_ts"]).total_seconds()))
            best["rows"].setdefault((r["ticker"], r["side"]), []).append(r)
    out = []
    for s in sorted(sets, key=lambda s: s["t"], reverse=True)[:max(0, int(limit))]:
        out.append(_set_row(s))
    return out


def _set_row(s: dict) -> dict:
    d, notes = s["doc"], []
    parsed = parse_filled(d.get("executed"))
    legs = []
    for leg in s["legs"]:
        rs = s["rows"].get((leg["ticker"], leg["side"]), [])
        from_rows = sum(float(r.get("count") or 0) for r in rs) if rs else None
        filled = parsed.get(leg["ticker"], from_rows)
        if rs:
            settled = all(r["_settled"] is not None for r in rs)
            pnl = sum(float(r["_pnl"]) for r in rs) if settled else None
            wins = sum(1 for r in rs if r.get("result") == "win")
            result = None if not settled else ("win" if wins == len(rs) else ("loss" if wins == 0 else "mixed"))
        else:
            settled = None if (filled or 0) > 0 else False
            pnl = result = None
            if (filled or 0) > 0:
                notes.append(f"{leg['ticker']}: filled but no stored fill matched yet")
        legs.append({**leg, "filled": None if filled is None else round(filled, 2), "settled": settled,
                     "result": result, "pnl": None if pnl is None else round(pnl, 2)})
    held = [lg for lg in legs if (lg["filled"] or 0) > 0]
    complete = bool(legs) and all(lg["filled"] is not None and lg["filled"] + 1e-9 >= lg["contracts"] for lg in legs)
    if held and all(lg["settled"] is True for lg in held):
        status = "SETTLED"
    elif not complete:
        status = "PARTIAL"
    elif any(lg["settled"] is True for lg in held):
        status = "OPEN"
    else:
        status = "FILLED"
    realized = None
    settled_utc = None
    if status == "SETTLED":
        realized = round(sum(lg["pnl"] for lg in held if lg["pnl"] is not None), 2)
        ts = [r["_settled"] for rs in s["rows"].values() for r in rs if r["_settled"] is not None]
        settled_utc = iso(max(ts)) if ts else None
    if str(d.get("executed", "")).startswith("ERROR"):
        notes.append("order error mid-set: fill counts come from stored fills")
    profit = pnlsvc._f(d.get("profit"))
    cost = pnlsvc._f(d.get("cost"))
    return {"set_id": f"{d.get('sig') or ''}:{iso(s['t'])}", "event": d.get("event"), "kind": d.get("kind"),
            "mode": d.get("mode"), "executed_utc": iso(s["t"]), "outcome": outcome_of(d.get("executed")),
            "contracts": pnlsvc._f(d.get("contracts")), "cost": None if cost is None else round(cost, 2),
            "guaranteed_profit": None if profit is None else round(profit, 2), "status": status,
            "complete": complete, "legs": legs, "realized_pnl": realized, "settled_utc": settled_utc,
            "notes": notes}


def set_stats(sets: list[dict]) -> dict:
    """Win rate per SET (executed sets whose filled legs all settled: win if the set's P&L > 0), plus guaranteed vs
    realized profit. Hedged legs of one set are never counted separately (a NO set always has losing legs)."""
    settled = [s for s in sets if s["status"] == "SETTLED" and s["realized_pnl"] is not None]
    wins = sum(1 for s in settled if s["realized_pnl"] > 0)
    filled = [s for s in sets if s["outcome"] == "filled" and s["guaranteed_profit"] is not None]
    guaranteed = sum(s["guaranteed_profit"] for s in filled)
    same = [s for s in settled if s["guaranteed_profit"] is not None]
    return {"executed": len(sets), "settled": len(settled), "wins": wins, "losses": len(settled) - wins,
            "legged": sum(1 for s in sets if s["outcome"] in ("partial", "error")),
            "complete": len(filled), "open": sum(1 for s in sets if s["status"] != "SETTLED"),
            "guaranteed_profit": round(guaranteed, 2) if filled else None,
            "realized_pnl": round(sum(s["realized_pnl"] for s in settled), 2) if settled else None,
            "settled_guaranteed_profit": round(sum(s["guaranteed_profit"] for s in same), 2) if same else None,
            "settled_realized_pnl": round(sum(s["realized_pnl"] for s in same), 2) if same else None}


def summary(docs: list[dict], rows: list[dict], *, env: str, now: datetime, zone, tzname: str,
            since: datetime | None, until: datetime | None = None, label: str = "") -> dict:
    """The `arb_summary` block: distinct sets / events / sightings today (local midnight in `zone`) and over
    [since, until), gate-outcome breakdown, guaranteed vs realized per set. Recomputed from the stored log on
    every call, so a restart changes nothing. The runner's in-memory `since_restart` counters are merged in by the
    route (they are labelled as such)."""
    docs = [d for d in docs if isinstance(d, dict)]
    midnight = pnlsvc.local_midnight_utc(now, zone)
    sets = build_sets(docs, rows, env=env, since=since, until=until, limit=MAX_SETS * 5)
    return {"tz": tzname, "source": "stored arbitrage log (one row per distinct set signature)",
            "today": {"since": iso(midnight), **counters(docs, env=env, since=midnight)},
            "period": {"label": label, "since": iso(since), "until": iso(until),
                       **counters(docs, env=env, since=since, until=until)},
            "per_set": set_stats(sets)}

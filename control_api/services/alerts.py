"""Alerts: ntfy (best effort, never raises, never sends secrets), plus the UX-sprint pieces (items 37, 38, 45):

* every alert is classified (fill, settled, daily, error, stop, breaker, test, system) and handed to an optional
  `on_alert` hook, which the runner uses to log it to `alert_history` and forward it as a web push;
* `notify()` is the everyday channel (fills, settled markets, daily summary): history + push only, never ntfy;
* alert settings (daily summary time, "problems" vs "every_fill", quiet hours) live in cloud_settings["alerts"],
  validated here. Stop and critical alerts always bypass quiet hours. ntfy keeps getting every alert it got before.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable

import httpx

from .logbuffer import redact

log = logging.getLogger("control_api.alerts")

TYPES = ("fill", "settled", "daily", "error", "stop", "breaker", "test", "system")
PROBLEM_TYPES = ("error", "stop", "breaker")
EVERYDAY_TYPES = ("fill", "settled")
MODES = ("problems", "every_fill")
DEFAULT_TZ = "America/Chicago"
DEFAULT_ALERT_SETTINGS = {"daily_summary_time": "08:00", "mode": "problems",
                          "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
                          "tz": DEFAULT_TZ}
ALERT_FIELDS = ("daily_summary_time", "mode", "quiet_hours", "tz")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$|^UTC$")

# title prefix -> type, for the existing ntfy alerts (first match wins)
_TITLE_TYPES = (
    ("Arbitrage legging risk", "breaker"), ("Arbitrage stopped: kill switch", "breaker"),
    ("Bot stopped", "stop"),
    ("Trading pass error", "error"), ("No heartbeat", "error"), ("Monitor failed", "error"),
    ("Self-ping failed", "error"), ("Pinger sync failed", "error"),
    ("Arbitrage executed", "fill"),
    ("Order errors", "error"), ("Trading paused by Kalshi", "error"),
)


def classify(title: str, priority: str = "default") -> str:
    for prefix, t in _TITLE_TYPES:
        if str(title).startswith(prefix):
            return t
    return "error" if priority in ("high", "urgent") and "fail" in str(title).lower() else "system"


def is_critical(kind: str, priority: str = "default") -> bool:
    """Stop / circuit breaker / high-priority alerts: always delivered, quiet hours or not."""
    return kind in ("stop", "breaker") or priority in ("high", "urgent")


# ---------------------------------------------------------------- settings (item 38)
def merged_settings(stored: Any) -> dict:
    out = {**DEFAULT_ALERT_SETTINGS, "quiet_hours": dict(DEFAULT_ALERT_SETTINGS["quiet_hours"])}
    if isinstance(stored, dict):
        for k in ("daily_summary_time", "mode", "tz"):
            if k in stored:
                out[k] = stored[k]
        if isinstance(stored.get("quiet_hours"), dict):
            out["quiet_hours"].update({k: v for k, v in stored["quiet_hours"].items()
                                       if k in ("enabled", "start", "end")})
    return out


def _tz_ok(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def validate_settings(current: dict, update: Any) -> dict:
    """Merge a partial update into the current alert settings; ValueError([msgs]) on any problem."""
    if not isinstance(update, dict):
        raise ValueError(["body: must be a JSON object"])
    errs = []
    unknown = [k for k in update if k not in ALERT_FIELDS]
    if unknown:
        errs.append(f"unknown fields: {sorted(map(str, unknown))[:10]}")
    out = merged_settings(current)
    if "daily_summary_time" in update:
        v = update["daily_summary_time"]
        if v is not None and not (isinstance(v, str) and _HHMM.match(v)):
            errs.append("daily_summary_time: must be \"HH:MM\" (24 h) or null to turn it off")
        else:
            out["daily_summary_time"] = v
    if "mode" in update:
        if update["mode"] not in MODES:
            errs.append(f"mode: must be one of {list(MODES)}")
        else:
            out["mode"] = update["mode"]
    if "tz" in update:
        v = update["tz"]
        if not (isinstance(v, str) and len(v) <= 64 and _TZ.match(v) and _tz_ok(v)):
            errs.append("tz: must be an IANA time zone such as America/Chicago")
        else:
            out["tz"] = v
    if "quiet_hours" in update:
        q = update["quiet_hours"]
        if not isinstance(q, dict) or set(q) - {"enabled", "start", "end"}:
            errs.append("quiet_hours: must be {enabled, start, end}")
        else:
            if "enabled" in q and not isinstance(q["enabled"], bool):
                errs.append("quiet_hours.enabled: must be true or false")
            for k in ("start", "end"):
                if k in q and not (isinstance(q[k], str) and _HHMM.match(q[k])):
                    errs.append(f"quiet_hours.{k}: must be \"HH:MM\" (24 h)")
            if not errs:
                out["quiet_hours"].update(q)
    if errs:
        raise ValueError(errs)
    return out


def changes(before: dict, after: dict) -> list[dict]:
    """[{key, from, to}] for the toast (item 39); nested quiet_hours keys as quiet_hours.start etc."""
    def flat(d):
        o = {}
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                o.update({f"{k}.{kk}": vv for kk, vv in v.items()})
            else:
                o[k] = v
        return o
    a, b = flat(before), flat(after)
    return [{"key": k, "from": a.get(k), "to": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def local_now(settings: dict, now: datetime | None = None) -> datetime:
    """`now` in the configured time zone (UTC if the zone database is unavailable)."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(str(settings.get("tz") or DEFAULT_TZ)))
    except Exception:  # noqa: BLE001
        return now.astimezone(timezone.utc)


def _hm(s: str) -> dtime:
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(settings: dict, now: datetime | None = None) -> bool:
    q = settings.get("quiet_hours") or {}
    if not q.get("enabled"):
        return False
    try:
        start, end = _hm(q["start"]), _hm(q["end"])
    except (KeyError, ValueError):
        return False
    t = local_now(settings, now).time().replace(second=0, microsecond=0)
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)   # overnight window wraps midnight


def should_push(settings: dict, kind: str, priority: str = "default", now: datetime | None = None) -> bool:
    """Web push filter: mode + quiet hours. Stop/critical alerts always go; tests always go."""
    if kind == "test":
        return True
    critical = is_critical(kind, priority)
    if not critical:
        if kind in EVERYDAY_TYPES and settings.get("mode") != "every_fill":
            return False
        if kind == "daily" and not settings.get("daily_summary_time"):
            return False
        if in_quiet_hours(settings, now):
            return False
    return True


# ---------------------------------------------------------------- grouping (Sprint 2B part 3, item 18a)
GROUP_WINDOW_S = 30 * 60
GROUP_MAX_KEYS = 200


def never_grouped(kind: str, title: str, priority: str) -> bool:
    """Urgent, stop, breaker and kill-switch alerts always send immediately."""
    return priority == "urgent" or kind in ("stop", "breaker") or "kill switch" in str(title).lower()


def _hhmm_utc(ts: datetime | None) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M") if isinstance(ts, datetime) else "?"


def group_note(count: int, first: datetime | None, last: datetime | None) -> str:
    return f"Repeated {count} times, first {_hhmm_utc(first)}, last {_hhmm_utc(last)} UTC."


class AlertGrouper:
    """Collapses repeats of the same (type, key) inside a window into one sent alert with a count.

    * The first alert of a group goes out at once and starts the window.
    * Repeats inside the window are held back and counted.
    * The first repeat after the window sends ONE alert saying how many times it happened (count includes this one)
      with the first and last times, and starts a new window. `due()` hands out held-back groups whose window has
      closed with no new repeat, so a quiet tail is still reported (the runner calls `Alerter.flush_groups`).
    * Urgent / stop / breaker / kill-switch alerts are never grouped (`check` answers "send, count 1" and keeps no
      state). In memory only: a restart forgets the window, so at worst one alert is sent a little early.
    `key` defaults to the exact title; pass `key=` to group titles that carry changing numbers."""

    def __init__(self, window_s: float = GROUP_WINDOW_S, max_keys: int = GROUP_MAX_KEYS):
        self.window = timedelta(seconds=max(1.0, float(window_s)))
        self.max_keys = max(1, int(max_keys))
        self._g: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        n = now or datetime.now(timezone.utc)
        return n if n.tzinfo else n.replace(tzinfo=timezone.utc)

    def check(self, kind: str, title: str, priority: str = "default", *, key: str | None = None,
              now: datetime | None = None) -> dict:
        """{send: bool, count: int, first, last}. send=False: held back (counted)."""
        now = self._now(now)
        if never_grouped(kind, title, priority):
            return {"send": True, "count": 1, "first": now, "last": now}
        gk = (str(kind), str(key if key is not None else title))
        g = self._g.get(gk)
        if g is None or now >= g["start"] + self.window:
            n = g["held"] if g else 0
            first = g["first"] if g and n else now
            self._g.pop(gk, None)
            self._g[gk] = {"start": now, "held": 0, "first": None, "last": None,
                           "kind": kind, "title": title, "priority": priority}
            self._trim()
            return {"send": True, "count": n + 1, "first": first, "last": now}
        g["held"] += 1
        g["first"] = g["first"] or now
        g["last"] = now
        g["title"], g["priority"] = title, priority
        return {"send": False, "count": g["held"], "first": g["first"], "last": now}

    def due(self, now: datetime | None = None) -> list[dict]:
        """Groups with held-back repeats whose window has closed: [{kind, title, priority, count, first, last}].
        Each is reset (a new window starts at `now`), so it is reported once."""
        now = self._now(now)
        out = []
        for g in self._g.values():
            if g["held"] and now >= g["start"] + self.window:
                out.append({"kind": g["kind"], "title": g["title"], "priority": g["priority"],
                            "count": g["held"], "first": g["first"], "last": g["last"]})
                g.update(start=now, held=0, first=None, last=None)
        return out

    def _trim(self) -> None:
        if len(self._g) <= self.max_keys:
            return
        for gk in [k for k, g in self._g.items() if not g["held"]][:len(self._g) - self.max_keys]:
            self._g.pop(gk, None)
        while len(self._g) > self.max_keys:           # all hold repeats: drop the oldest
            self._g.pop(next(iter(self._g)), None)


# ---------------------------------------------------------------- sender
class Alerter:
    def __init__(self, settings, transport=None, grouper: AlertGrouper | None = None):
        self.topic = getattr(settings, "ntfy_topic", None)
        self.server = getattr(settings, "ntfy_server", "https://ntfy.sh")
        self.transport = transport
        # Opt-in: None keeps every alert as before. The runner sets `alerter.grouper = AlertGrouper()`.
        self.grouper = grouper
        self.sent: list[dict] = []          # recent alerts, for tests/debugging
        # on_alert(kind, title, body, priority, channels): set by the runner (history + push). Plain callable that
        # must not block (it spawns its own task); exceptions are swallowed here.
        self.on_alert: Callable[..., Any] | None = None

    def _hook(self, kind: str, title: str, message: str, priority: str, channels: list[str]) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert(kind, title, message, priority, channels)
        except Exception as e:  # noqa: BLE001
            log.warning("alert hook failed: %s", type(e).__name__)

    async def send(self, title: str, message: str, *, priority: str = "default", tags: str = "",
                   group_key: str | None = None) -> bool:
        """Send one alert. With a grouper set, a repeat inside the window is held back (returns True: nothing failed)
        and the next one after the window carries the count and first/last times. Urgent / kill-switch never wait."""
        if self.grouper is not None:
            try:
                d = self.grouper.check(classify(title, priority), title, priority, key=group_key)
            except Exception as e:  # noqa: BLE001   (a grouping bug must never swallow an alert)
                log.warning("alert grouping failed: %s", type(e).__name__)
                d = {"send": True, "count": 1}
            if not d["send"]:
                return True
            if d["count"] > 1:
                message = f"{message}\n\n{group_note(d['count'], d.get('first'), d.get('last'))}"
        return await self._deliver(title, message, priority=priority, tags=tags)

    async def flush_groups(self, now: datetime | None = None) -> int:
        """Send one summary for every group whose window closed with repeats still held back. Returns how many.
        Cheap when nothing is held; the runner calls it once per pass. Never raises."""
        if self.grouper is None:
            return 0
        n = 0
        try:
            for g in self.grouper.due(now):
                await self._deliver(g["title"], group_note(g["count"], g["first"], g["last"]),
                                    priority=g["priority"])
                n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("alert group flush failed: %s", type(e).__name__)
        return n

    async def send_alert(self, alert: dict) -> bool:
        """Send a prepared alert dict ({title, body, priority, ...}), e.g. from `legging_alert`."""
        return await self.send(str(alert.get("title") or ""), str(alert.get("body") or ""),
                               priority=str(alert.get("priority") or "default"))

    async def _deliver(self, title: str, message: str, *, priority: str = "default", tags: str = "") -> bool:
        ok = False
        try:
            message, title = redact(message)[:2000], redact(title)[:200]
            self.sent = (self.sent + [{"title": title, "message": message, "priority": priority}])[-50:]
            log.warning("ALERT %s: %s", title, message)
            if self.topic:
                headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
                if tags:
                    headers["Tags"] = tags
                async with httpx.AsyncClient(timeout=10, transport=self.transport) as c:
                    r = await c.post(f"{self.server}/{self.topic}", content=message.encode("utf-8"), headers=headers)
                    ok = r.status_code < 300
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy send failed: %s", type(e).__name__)
            ok = False
        self._hook(classify(title, priority), title, message, priority, ["ntfy"] if self.topic else [])
        return ok

    def notify(self, kind: str, title: str, message: str, *, priority: str = "default") -> None:
        """Everyday alert (fill / settled / daily / test): history + web push only, never ntfy. Never raises."""
        try:
            self._hook(kind if kind in TYPES else "system", redact(title)[:200], redact(message)[:2000], priority, [])
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", type(e).__name__)


# ---------------------------------------------------------------- urgent legging alert (Sprint 2B part 3, item 5)
LEGGING_TYPE = "arb_legging"
LEGGING_TITLE_PREFIX = "Arbitrage legging risk"       # keeps `classify` -> "breaker" (always delivered, push + ntfy)


def _fnum(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _leg_pairs(fills: Any) -> list[tuple[str, float]]:
    """[(ticker, contracts)] from {ticker: n} or [[ticker, n], ...]; anything else gives []."""
    out: list[tuple[str, float]] = []
    items = fills.items() if isinstance(fills, dict) else (fills if isinstance(fills, (list, tuple)) else [])
    for it in items:
        try:
            t, n = it
        except (TypeError, ValueError):
            continue
        n = _fnum(n)
        if isinstance(t, str) and t and n is not None:
            out.append((t[:60], n))
    return out


def legging_alert(parsed: dict, series_paused_until: str | None, loss_cap: float | None) -> dict:
    """The urgent alert for a partly filled arbitrage set: {type "arb_legging", title, body, priority "urgent",
    channels []}. Pure; never raises; plain words, no secrets.

    `parsed` = {kind: filled|skipped|contained|partial|error, fills: {ticker: n}, loss, event} (arbcontain.parse
    plus the event). Contained = kind "contained" with a finite loss at most `loss_cap` (a positive finite number).
    Anything else, including a kind it does not know, reads as uncontained: the global arbitrage kill switch
    tripped (fails toward the loud message). `series_paused_until` (e.g. "2026-11-09") is only used when contained."""
    p = parsed if isinstance(parsed, dict) else {}
    kind = str(p.get("kind") or "partial")
    event = str(p.get("event") or "an unknown event")[:80]
    loss = _fnum(p.get("loss"))
    cap = _fnum(loss_cap)
    legs = _leg_pairs(p.get("fills"))
    held = [f"{t} ({n:g})" for t, n in legs if n > 0]
    unfilled = [t for t, n in legs if n <= 0]
    if not legs:
        legs_s = "Which legs filled is unknown."
    else:
        legs_s = "Legs filled: " + (", ".join(held) if held else "none") + "."
        if unfilled:
            legs_s += " Not filled: " + ", ".join(unfilled) + "."
    loss_s = f"${max(0.0, loss):.2f}" if loss is not None else "unknown"
    contained = kind == "contained" and loss is not None and cap is not None and cap > 0 and loss <= cap + 1e-9
    if contained:
        until = f" until {str(series_paused_until)[:10]}" if series_paused_until else ""
        title = f"{LEGGING_TITLE_PREFIX}: partial set contained"
        body = (f"Set {event} filled only partly. {legs_s} Loss so far: {loss_s}. The bot closed it out within the "
                f"per-set loss cap of ${cap:.2f}, so only this series is paused{until}; the global kill switch did "
                "not trip and the other series keep running. Next: check the positions in the app, and look at "
                "the loss before you let the series trade again.")
    else:
        if kind == "contained":
            why = ("its loss was outside the per-set cap" if loss is not None and cap
                   else "no usable loss cap or loss was available")
        elif kind == "error":
            why = "the order step failed, so the state is unknown"
        else:
            why = "the bot could not close it out"
        title = f"{LEGGING_TITLE_PREFIX}: arb kill switch tripped"
        body = (f"Set {event} filled only partly and {why}. {legs_s} Loss so far: {loss_s}. The global arbitrage "
                "kill switch tripped: arbitrage is stopped everywhere and will not restart on its own. Next: open "
                "the app, check the positions and close any leg you do not want, then restart arbitrage "
                "yourself once you have looked at it.")
    return {"type": LEGGING_TYPE, "title": title, "body": body[:2000], "priority": "urgent", "channels": []}


# ---------------------------------------------------------------- weekly summary (Sprint 2B part 3, item 18b)
WEEKLY_HOUR = 18                      # Sunday evening, local
WEEKLY_DAYS = 7
DEMO_LABEL = "demo, indicative only"


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{'+' if v >= 0 else '-'}${abs(v):.2f}"


def _iso_week(d) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def _read(coro, default, timeout: float = 10.0):
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as e:  # noqa: BLE001   (a missing section must not lose the whole summary)
        log.warning("weekly summary read failed: %s", type(e).__name__)
        return default


async def weekly_summary(store, now: datetime, tz: Any = None) -> dict:
    """Plain-text weekly report from existing store reads: {title, text, mode, demo, since, until, strategies, arb,
    stops, errors, open_positions}.

    Covers the last 7 local days (today included) in `tz` (IANA name or tzinfo; UTC if unknown): per strategy the
    settled P&L (labelled "demo, indicative only" in demo), fills and settled fills; arbitrage sets
    filled / partial / skipped; stops and errors from the alert history; cash and exposure from the latest account
    snapshot; open positions (bot fills in the last 35 days whose market has no result yet). A read that fails
    shows as n/a; it never raises."""
    from . import arbsets
    from . import pnl as pnlsvc
    from . import strategies as strat
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    zone = tz if hasattr(tz, "utcoffset") else pnlsvc.resolve_tz(tz)[0]
    since = pnlsvc.local_midnight_utc(now, zone) - timedelta(days=WEEKLY_DAYS - 1)
    state = await _read(store.get_state(), None)
    mode = (state or {}).get("mode")
    mode = mode if mode in ("demo", "live") else "demo"
    demo = mode != "live"
    tag = f" ({DEMO_LABEL})" if demo else ""

    lookback = since - pnlsvc.FILL_LOOKBACK
    settlements = await _read(store.list_settlements(since=lookback, limit=20000,
                                                     fields=("ticker", "market_result", "result", "ts")), None)
    fills = await _read(store.list_fills(since=lookback, limit=50000, fields=pnlsvc.FILL_FIELDS), None)
    ids = [s["id"] for s in strat.registry() if s["id"] != strat.YOU]
    names = {s["id"]: s["name"] for s in strat.registry()}
    per = {i: {"pnl": None, "fills": 0, "settled": 0} for i in ids}
    avail = settlements is not None and fills is not None
    open_n = {i: 0 for i in ids}
    if avail:
        result, in_window = {}, {}
        for s in settlements:
            t, r, ts = s.get("ticker"), s.get("market_result") or s.get("result"), s.get("ts")
            if t and r in ("yes", "no"):
                result[t] = r
                if isinstance(ts, datetime) and since <= ts <= now:
                    in_window[t] = r
        open_t: dict[str, set] = {i: set() for i in ids}
        for f in fills:
            if f.get("env") and f.get("env") != mode:
                continue
            src, sid = strat.fill_label(f)
            if src != strat.BOT or sid not in per:
                continue
            tk = f.get("ticker") or f.get("market_ticker")
            ts = f.get("ts")
            if isinstance(ts, datetime) and ts >= since:
                per[sid]["fills"] += 1
            if tk not in result:
                open_t[sid].add(tk)
            r = in_window.get(tk)
            p = pnlsvc.fill_pnl(f, r) if r else None
            if p is not None:
                per[sid]["pnl"] = (per[sid]["pnl"] or 0.0) + p
                per[sid]["settled"] += 1
        open_n = {i: len(v) for i, v in open_t.items()}

    arb_docs = await _read(store.list_arb_opportunities(since=since, limit=5000, executed_only=True), None)
    arb = {"filled": 0, "partial": 0, "skipped": 0, "errors": 0}
    for d in arb_docs or []:
        if not isinstance(d, dict) or not arbsets.env_match(d, mode):
            continue
        ex = str(d.get("executed") or "")
        key = ("filled" if ex.startswith("FILLED") else "partial" if ex.startswith("PARTIAL") else
               "errors" if ex.startswith("ERROR") else "skipped" if ex.startswith("skipped") else None)
        if key:
            arb[key] += 1

    counts: dict[str, int | None] = {}
    for kind in ("stop", "error"):
        docs = await _read(store.list_alerts(type=kind, limit=200), None)
        counts[kind] = None if docs is None else sum(
            1 for d in docs if isinstance(d.get("ts"), datetime) and d["ts"] >= since)
    stops, errs = counts["stop"], counts["error"]
    snaps = await _read(store.list_account_snapshots(limit=1, env=mode), None)
    snap = snaps[0] if snaps else None

    def cnt(v):
        return "n/a" if v is None else str(v)

    lines = [f"Last {WEEKLY_DAYS} days (from {since.astimezone(zone).date().isoformat()}), {mode} mode{tag}."]
    for i in ids:
        s = per[i]
        pl = "n/a" if not avail else (_money(s["pnl"]) if s["pnl"] is not None else "$0.00")
        lines.append(f"{names.get(i, i)}: settled P&L {pl}{tag}, {s['fills'] if avail else 'n/a'} fills, "
                     f"{s['settled'] if avail else 'n/a'} settled.")
    if arb_docs is None:
        lines.append("Arbitrage sets: n/a.")
    else:
        lines.append(f"Arbitrage sets: {arb['filled']} filled, {arb['partial']} partial, {arb['skipped']} skipped"
                     + (f", {arb['errors']} errors" if arb["errors"] else "") + ".")
    lines.append(f"Stops: {cnt(stops)}. Errors: {cnt(errs)}.")
    if snap:
        cash, expo = _fnum(snap.get("cash")), _fnum(snap.get("exposure"))
        cash_s = "n/a" if cash is None else f"${cash:.2f}"
        expo_s = "n/a" if expo is None else f"${expo:.2f}"
        lines.append(f"Cash {cash_s}, exposure {expo_s}.")
    else:
        lines.append("Cash and exposure: n/a.")
    if avail:
        lines.append("Open positions: " + ", ".join(f"{open_n[i]} {i}" for i in ids) + " markets.")
    else:
        lines.append("Open positions: n/a.")
    return {"title": "Weekly Summary", "text": redact("\n".join(lines)), "mode": mode, "demo": demo,
            "since": since.isoformat(), "until": now.isoformat(), "strategies": per, "arb": arb, "stops": stops,
            "errors": errs, "open_positions": open_n}


async def maybe_send_weekly(alerter, store, now: datetime, cloud_state: dict) -> bool:
    """Send the weekly summary once per ISO week, from Sunday WEEKLY_HOUR local on. The guard is
    cloud_state["alerts_state"]["weekly_sent"] (ISO week, e.g. "2026-W45"), saved to the store with the other
    alerts_state keys kept, so a restart does not repeat it. Normal priority, history + push (kind "daily").
    `cloud_state` is the runner's cloud settings dict (mutated). Returns whether it sent. Never raises."""
    try:
        cloud_state = cloud_state if isinstance(cloud_state, dict) else {}
        s = merged_settings(cloud_state.get("alerts"))
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        loc = local_now(s, now)
        if loc.weekday() != 6 or loc.hour < WEEKLY_HOUR:
            return False
        wk = _iso_week(loc.date())
        prev = cloud_state.get("alerts_state")
        prev = dict(prev) if isinstance(prev, dict) else {}
        if prev.get("weekly_sent") == wk:
            return False
        new_state = {**prev, "weekly_sent": wk}
        cloud_state["alerts_state"] = new_state           # set first: a slow store never causes a repeat
        rep = await weekly_summary(store, now, s.get("tz"))
        fn = getattr(alerter, "notify", None)
        if callable(fn):
            fn("daily", rep["title"], rep["text"])
        else:
            await alerter.send(rep["title"], rep["text"])
        try:
            await asyncio.wait_for(store.save_cloud_settings({"alerts_state": new_state}, by="weekly-summary"), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("weekly summary guard not saved: %s", type(e).__name__)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("weekly summary failed: %s", type(e).__name__)
        return False

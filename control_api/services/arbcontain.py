"""Sprint 2B item 3 (runner side): contain and degrade after a partial arbitrage set. Pure functions, no I/O.

arb_scanner.execute result strings (the scanner owns them; this module only reads them):

* ``FILLED ...`` / ``FILLED (completed) ...``: the whole set is held.
* ``skipped: <reason>``: nothing new is held (no kill file).
* ``PARTIAL <fills> contained: <completed|flattened> loss <dollars>``: the set filled only partly but the scanner
  completed or flattened it within the per-set loss cap the runner passed. The scanner writes NO kill file.
* ``PARTIAL <fills>`` (anything else after PARTIAL): uncontained. The scanner writes the kill file as before.
* ``ERROR <Type>`` (the runner's own value when execute raised): treated like an uncontained partial.

Pause rule for a contained partial (documented in the agent report):

* only the offending series is paused (series = the event ticker before its first "-");
* the pause lasts until the next market day: the next 00:00 in America/New_York after the partial. The first scan
  on or after that instant trades the series again (nothing to clear: the pause is computed from the record's time);
* the other series keep running.

The GLOBAL arbitrage kill switch still trips (the legging kind, which needs the owner's acknowledgement to restart):

* an uncontained partial or an ERROR (the scanner or the runner writes the kill file);
* a contained result when the runner passed no loss cap, or whose loss is not a finite number at most the cap (the
  loss is signed; a negative loss, a gain, counts as 0);
* a second contained partial within 24 h, in any series;
* contained losses over the last 24 h, this one included, adding up to more than the current per-set loss cap.

Records are rebuilt from the stored arbitrage log (each contained doc carries a ``contain`` block), so a pause and the
24 h counts survive a restart.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
WINDOW = timedelta(hours=24)
MAX_CONTAINED_IN_WINDOW = 1          # a second contained partial inside WINDOW trips the global kill switch
REBUILD_LOOKBACK = timedelta(hours=48)
KINDS = ("filled", "completed", "skipped", "contained", "partial", "error", "other")
_CONTAINED = re.compile(r"^PARTIAL\s.*?\scontained:\s*(completed|flattened)\s+loss\s+\$?(-?[0-9]+(?:\.[0-9]+)?)\b",
                        re.S)
UNCONFIRMED = "order state unconfirmed"


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def local_parse(s: Any) -> dict:
    """{kind, fills, loss, how} from a result string, by its prefix only."""
    from .arbsets import parse_filled
    s = str(s or "")
    out: dict = {"kind": "other", "fills": {}, "loss": None, "how": None}
    if s.startswith("FILLED (completed)"):
        out["kind"] = "completed"
    elif s.startswith("FILLED"):
        out["kind"] = "filled"
    elif s.startswith("skipped"):
        out["kind"] = "skipped"
    elif s.startswith("ERROR"):
        out["kind"] = "error"
    elif s.startswith("PARTIAL"):
        m = _CONTAINED.match(s)
        if m:
            out.update(kind="contained", how=m.group(1), loss=_num(m.group(2)))
        else:
            out["kind"] = "partial"
    if out["kind"] in ("filled", "completed", "contained", "partial"):
        out["fills"] = parse_filled(s)
    return out


def parse(s: Any, scanner: Any = None) -> dict:
    """The scanner's parse_result when it has one and its answer is usable, else local_parse. Never raises.
    Safety: "contained" is believed only for a string that starts with PARTIAL and says "contained:"; anything else
    the scanner calls contained is read as an uncontained partial (fails toward the global kill switch)."""
    local = local_parse(s)
    fn = getattr(scanner, "parse_result", None) if scanner is not None else None
    if not callable(fn):
        return local
    try:
        got = fn(str(s))
    except Exception:  # noqa: BLE001
        return local
    if not isinstance(got, Mapping) or got.get("kind") not in KINDS:
        return local
    kind = got.get("kind")
    out = {"kind": kind, "fills": got.get("fills") if isinstance(got.get("fills"), Mapping) else local["fills"],
           "loss": _num(got.get("loss")), "how": local["how"]}
    if kind == "contained" and local["kind"] != "contained":
        out["kind"] = "partial"
    if out["kind"] == "contained" and out["loss"] is None:
        out["loss"] = local["loss"]
    if local["kind"] in ("partial", "error") and kind not in ("partial", "error"):
        out["kind"] = local["kind"]           # never let a parser downgrade an uncontained result
    return out


def is_unconfirmed_skip(s: Any) -> bool:
    """`skipped: transient error ... (order state unconfirmed ...)`: a first leg may have filled unseen."""
    s = str(s or "")
    return s.startswith("skipped") and UNCONFIRMED in s


def series_of(event: Any) -> str:
    return event.split("-", 1)[0] if isinstance(event, str) and event else ""


def _aware(ts: Any) -> datetime | None:
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def pause_until(ts: datetime) -> datetime:
    """The next 00:00 America/New_York after `ts`, in UTC (the start of the next market day)."""
    local = _aware(ts).astimezone(MARKET_TZ)
    nxt = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=MARKET_TZ)
    return nxt.astimezone(timezone.utc)


def record(event: str, loss: float, ts: datetime, rid: str, how: str | None = None) -> dict:
    return {"id": str(rid), "ts": _aware(ts), "event": str(event), "series": series_of(event),
            "loss": float(loss), "how": how}


def records_from_docs(docs: Iterable[Mapping[str, Any]] | None, mode: str) -> list[dict]:
    """Contained-partial records from stored arbitrage docs of `mode` (their `contain` block)."""
    out: list[dict] = []
    for d in docs or []:
        if not isinstance(d, Mapping) or d.get("mode") != mode:
            continue
        c = d.get("contain")
        if not isinstance(c, Mapping) or not c.get("id"):
            continue
        ts = _aware(c.get("ts")) or _aware(d.get("ts"))
        loss = _num(c.get("loss"))
        ev = d.get("event")
        if ts is None or not isinstance(ev, str) or not ev:
            continue
        out.append(record(ev, loss if loss is not None and loss >= 0 else 0.0, ts, c["id"], c.get("how")))
    return out


def merge(a: Iterable[dict], b: Iterable[dict]) -> list[dict]:
    """Union by record id, oldest first."""
    seen: dict[str, dict] = {}
    for r in list(a) + list(b):
        seen.setdefault(r["id"], r)
    return sorted(seen.values(), key=lambda r: r["ts"])


def prune(records: Iterable[dict], now: datetime) -> list[dict]:
    lo = _aware(now) - REBUILD_LOOKBACK
    return [r for r in records if r["ts"] >= lo]


def window(records: Iterable[dict], now: datetime) -> tuple[int, float]:
    """(count, total loss) of contained partials in the 24 h before `now`."""
    lo = _aware(now) - WINDOW
    rs = [r for r in records if lo <= r["ts"] <= _aware(now) + timedelta(seconds=1)]
    return len(rs), round(sum(r["loss"] for r in rs), 6)


def paused(records: Iterable[dict], now: datetime) -> dict[str, dict]:
    """{series: {until_utc, until_date, event, loss}} of series paused at `now` (latest record per series)."""
    now = _aware(now)
    out: dict[str, dict] = {}
    for r in sorted(records, key=lambda r: r["ts"]):
        until = pause_until(r["ts"])
        if r["ts"] <= now + timedelta(seconds=1) and now < until and r["series"]:
            out[r["series"]] = {"until_utc": until, "until_date": until.astimezone(MARKET_TZ).date().isoformat(),
                                "event": r["event"], "loss": r["loss"]}
    return out


def must_trip(records_before: Iterable[dict], loss: float | None, loss_cap: float | None,
              now: datetime) -> str | None:
    """Why a NEW contained partial (not yet in `records_before`) must trip the global kill switch, or None."""
    cap = _num(loss_cap)
    if cap is None or cap <= 0:
        return "contained partial without a loss cap"
    lv = _num(loss)
    if lv is None or lv > cap + 1e-9:
        return "contained partial loss outside the cap"
    lv = max(0.0, lv)                    # the scanner's loss is signed: a negative loss (a gain) counts as 0
    n, total = window(records_before, now)
    if n >= MAX_CONTAINED_IN_WINDOW:
        return "two contained partials in 24 h"
    if total + lv > cap + 1e-9:
        return "contained losses over 24 h exceed the loss cap"
    return None

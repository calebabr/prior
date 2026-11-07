"""Shadow mode: record what the installed strategy WOULD bid on production markets, using public reads only.

Nothing here can place an order. Three independent layers make sure of it:
  1. `ReadOnlyMarketData` wraps a market-data client by composition and exposes only four read methods
     (`markets`, `orderbook`, `market`, `trades`). It has no generic request method and no account or order methods.
  2. `GetOnlySession` (a requests.Session) refuses every non-GET request, every host other than the configured
     public API host, every path outside the four public market-data paths, and any request carrying credentials.
  3. `build_market_data` builds the client with no key id and no private key, and `ReadOnlyMarketData` refuses a
     client that has any credentials.

The strategy logic is NOT here: one cycle calls the installed strategy module's own pure `desired_quotes` (the
public stand-in returns an empty list) and only records, per hypothetical bid ("episode"), the queue facts the
estimate model (services/shadow_model.py) needs. `run_cycle` is synchronous (it runs on the runner's own one-worker
shadow pool) and returns the documents to write; it never touches the store, the process environment or any lock.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

from . import botfiles
from . import closetime as ctsvc
from . import fillquality as fq
from . import queuepos as qp
from . import shadow_model as sm

# ---- limits (docs/SHADOW_CONTRACT.md)
SHADOW_BUDGET_S = 60                 # one whole cycle (store reads + the worker thread); a timeout writes nothing
SHADOW_MAX_BOOKS = 150               # order books read per cycle
SHADOW_MAX_TRADE_READS = 40          # ended episodes whose public trades are read per cycle
SHADOW_DAILY_NEW_CAP = 1500          # new episodes per UTC day (bounds Atlas M0 storage)
SHADOW_MAX_SETTLE_READS = 40         # market lookups for settlement per cycle
SHADOW_MAX_MARKET_PAGES = 5          # pages of GET /markets per series
SHADOW_MAX_TRADE_PAGES = 3           # pages of GET /markets/trades per episode
SHADOW_STALE_AFTER = timedelta(hours=1)   # an open episode not observed for this long ends "stale"
SHADOW_MAX_TRADE_ATTEMPTS = 3        # SH-3: a trade read failing this many times gives up (fill unknown)
SHADOW_VOID_AFTER = timedelta(days=7)     # SH-1: closed this long with no yes/no result -> "void"
SHADOW_SUMMARY_MAX_ROWS = 20_000     # SH-2: settled episodes loaded for one summary (newest kept)
SHADOW_SUMMARY_MAX_OPEN = 3_000
SHADOW_SUMMARY_EVERY = timedelta(minutes=30)   # SH-2: cached summary refresh (sooner when something settled)
# SH-2: the only episode fields summarize() reads (a projection keeps memory and Atlas transfer small)
SUMMARY_FIELDS = ("result", "fill", "price", "count", "cost", "side", "settled_ts", "join_ts", "end_ts",
                  "close_time", "series", "cell",
                  # 2A-3: caps (F1), held capital (F5), fee schedule (F7), first fill per market (F1)
                  "capped", "through", "through_ts", "fee_mode", "ticker",
                  "interval",                       # 2A-3 wave 2: the baseline summary keeps interval 10 only
                  "methodology")                    # hotfix: the headline uses methodology-2 episodes only
SETTLED_STATUSES = ("settled", "finalized", "determined")
SHADOW_SUMMARY_DAYS = 30
SHADOW_STORE_TIMEOUT_S = 10.0
# S3: the arbitrage shadow scan (off unless env SHADOW_ARB=true). At most one per shadow cycle, after the episode work;
# it stops itself at its own request and time budgets and never raises ShadowDeadline (the cycle's episodes are kept).
SHADOW_ARB_ENV = "SHADOW_ARB"
SHADOW_ARB_MAX_REQUESTS = 60         # market-list pages + order books per scan
SHADOW_ARB_BUDGET_S = 15.0           # wall time per scan (also never past the cycle deadline)
SHADOW_ARB_MAX_EVENTS = 40           # events scanned per scan
CLIENT_TIMEOUT_S = 8.0
CLIENT_MAX_RETRIES = 1
SHADOW_ARB_READ_RESERVE_S = CLIENT_TIMEOUT_S * (CLIENT_MAX_RETRIES + 1)   # L1: worst case of one read
DEADLINE_MARGIN_S = 5.0              # the worker stops itself this long before the cycle budget runs out
# 2A-3 F1: held positions and exposure caps (the real strategy's limits, applied to an assumed capital)
SHADOW_HELD_SCAN = 3000              # ended, unsettled episodes read per cycle to rebuild the held position
# A bid still unsettled this long after its market closed is out of the held position (the real bot's capital would
# have been released or the market voided), and so is a legacy (methodology 1) episode. Counted in the pass doc.
SHADOW_HELD_GRACE = timedelta(hours=36)
# The summary says the ceiling is full once this many of the newest baseline passes in a row were full (no
# headroom, or more capped than uncapped new bids): Shadow is then not estimating.
SHADOW_CEILING_FULL_CYCLES = 2
SHADOW_CAPITAL_DEFAULT = 150.0       # cloud setting shadow_capital (validated 20-100000 by the cloud schema)
SHADOW_CAPITAL_MIN, SHADOW_CAPITAL_MAX = 20.0, 100000.0
DEFAULT_MAX_OPEN_FRAC = 0.5
CAP_TOL = 0.005                      # dollars: rounding slack before a bid counts as over a budget
CAP_REASONS = ("series_cap", "market_cap", "open_cap", "run_budget")
PASS_MARKET_CAPPED_MAX = 50          # tickers listed in a pass doc's market_capped
# 2A-3 F7: optional private fee schedule (absent in the public repo: every series uses the maker fee)
FEE_SCHEDULE_PATH = Path(__file__).resolve().parents[1] / "data" / "fee_schedule.json"
FEE_SCHEDULE_ENV = "FEE_SCHEDULE_PATH"
FEE_SCHEDULE_MAX_BYTES = 64 * 1024
FEE_SCHEDULE_MAX_RULES = 200
# 2A-3: optional private limits file (absent in the public repo: no per-series and no per-market cap).
# {"series_share_limit": 0-1, "market_share_limit": 0-1}, both shares of the open-exposure ceiling
SHADOW_LIMITS_PATH = Path(__file__).resolve().parents[1] / "data" / "shadow_limits.json"
SHADOW_LIMITS_ENV = "SHADOW_LIMITS_PATH"
SHADOW_LIMITS_MAX_BYTES = 4 * 1024
# hotfix (legacy episodes): episodes created by the capped 2A-3 code carry methodology 2; none = 1 (legacy)
METHODOLOGY = 2
LEGACY_METHODOLOGY = 1
# The 2A-3 (capped) backend finished deploying at this time (release run 39865501268, deploy-backend): an
# episode with no stored methodology that joined at or after it was created by the capped code (owner, no backfill).
METHODOLOGY2_SINCE = datetime(2026, 10, 22, 22, 33, 41, tzinfo=timezone.utc)
# 2A-3 wave 2: interval variants (off unless env SHADOW_VARIANTS=true; cadences from cloud shadow_intervals)
SHADOW_VARIANTS_ENV = "SHADOW_VARIANTS"
BASELINE_INTERVAL = 10               # the existing shadow cycle; its episodes and passes carry interval 10
VARIANT_INTERVALS_ALLOWED = (5, 7, 10)
SHADOW_VARIANT_LOAD_S = 20.0         # load guard: no variant cycle while the last shadow pass took longer than this
SHADOW_VARIANT_NEW_SHARE = 0.5       # variants stop adding episodes once today's total reaches this share of the cap
SHADOW_READ_CACHE_S = 60.0           # shared read cache: identical public reads within this window are reused
SHADOW_READ_CACHE_MAX = 1000

END_REASONS = ("cancel", "closed", "shadow_off", "stale")
_SEG = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}"     # one ticker path segment (never "." or "..")
_CRED_HEADERS = ("authorization", "kalshi-access-key", "kalshi-access-signature", "kalshi-access-timestamp")


class ShadowDeadline(RuntimeError):
    """The worker ran past its deadline; the cycle's partial result is discarded."""


# ================================================================ layer 2: the session
class GetOnlySession(requests.Session):
    """A requests.Session that can only GET the public market-data paths of one host. Checked in both `request`
    and `send` (redirects go through `send` too). Raises PermissionError otherwise."""

    def __init__(self, base_url: str):
        super().__init__()
        u = urlsplit(base_url)
        self._scheme = u.scheme.lower()
        self._netloc = u.netloc.lower()
        prefix = re.escape(u.path.rstrip("/"))
        self._allowed = re.compile(rf"^{prefix}/markets(?:/trades|/{_SEG}|/{_SEG}/orderbook)?$")
        self.trust_env = False          # no proxy or netrc credentials picked up from the environment

    def _check(self, method: Any, url: Any) -> None:
        if str(method or "").upper() != "GET":
            raise PermissionError("shadow: only GET requests are allowed")
        u = urlsplit(str(url))
        if u.scheme.lower() != self._scheme or u.netloc.lower() != self._netloc:
            raise PermissionError("shadow: host not allowed")
        if "%" in u.path or "\\" in u.path or not self._allowed.fullmatch(u.path):
            raise PermissionError("shadow: path not allowed")

    def request(self, method, url, *args, **kwargs):  # noqa: D401
        self._check(method, url)
        return super().request(method, url, *args, **kwargs)

    def send(self, request, **kwargs):
        self._check(request.method, request.url)
        for h in request.headers:
            if str(h).lower() in _CRED_HEADERS:
                raise PermissionError("shadow: credentials are not allowed")
        return super().send(request, **kwargs)


# ================================================================ layer 1: the wrapper
def _body(b: Any) -> dict:
    return b if isinstance(b, dict) else {}


class ReadOnlyMarketData:
    """Public market data only. Holds the client privately; exposes four read methods and nothing else."""

    __slots__ = ("_client",)

    def __init__(self, client: Any):
        if (getattr(client, "has_credentials", False) or getattr(client, "key_id", None)
                or getattr(client, "private_key", None) is not None):
            raise PermissionError("shadow: the market-data client must have no credentials")
        self._client = client

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        return _body(self._client.request("GET", endpoint, params=params))

    @staticmethod
    def _ticker(t: Any) -> str:
        if not ctsvc.valid_ticker(t):
            raise ValueError("shadow: invalid ticker")
        return t

    def markets(self, series: str, status: str, min_close_ts: int | None = None) -> tuple[list[dict], bool]:
        """(markets of one series, truncated). Bounded pagination."""
        self._ticker(series)
        out: list[dict] = []
        cursor = None
        for _ in range(SHADOW_MAX_MARKET_PAGES):
            body = self._get("/markets", {"series_ticker": series, "status": status, "min_close_ts": min_close_ts,
                                          "limit": 200, "cursor": cursor})
            out.extend(qp.dicts(body.get("markets")))
            cursor = body.get("cursor")
            if not cursor:
                return out, False
        return out, True

    def orderbook(self, ticker: str, depth: int | None = None) -> dict:
        body = self._get(f"/markets/{self._ticker(ticker)}/orderbook", {"depth": depth})
        book = body.get("orderbook_fp") or body.get("orderbook") or {}
        return book if isinstance(book, dict) else {}

    def market(self, ticker: str) -> dict:
        body = self._get(f"/markets/{self._ticker(ticker)}")
        m = body.get("market", body)
        return m if isinstance(m, dict) else {}

    def trades(self, ticker: str, min_ts: int, max_ts: int) -> tuple[list[dict], bool]:
        """(public trades of one market in [min_ts, max_ts] (unix seconds), truncated). Bounded pagination."""
        self._ticker(ticker)
        out: list[dict] = []
        cursor = None
        for _ in range(SHADOW_MAX_TRADE_PAGES):
            body = self._get("/markets/trades", {"ticker": ticker, "min_ts": int(min_ts), "max_ts": int(max_ts),
                                                 "limit": 1000, "cursor": cursor})
            page = qp.dicts(body.get("trades"))
            out.extend(page)
            cursor = body.get("cursor")
            if not cursor or not page:
                return out, False
        return out, True


# ================================================================ layer 3: the factory
def build_market_data(base_url: str | None = None, *, adapter: Any = None) -> ReadOnlyMarketData:
    """The production public market-data client: no key id, no private key, GET-only session, short timeout.
    `adapter` (tests): a requests transport adapter mounted on the session, so the whole real chain is exercised
    without the network."""
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    base = (base_url or BASE_URLS["prod"]).rstrip("/")
    session = GetOnlySession(base)
    if adapter is not None:
        session.mount(f"{urlsplit(base).scheme}://", adapter)
    client = KalshiClient(base, key_id=None, private_key=None, session=session,
                          max_retries=CLIENT_MAX_RETRIES, timeout=CLIENT_TIMEOUT_S)
    return ReadOnlyMarketData(client)


def default_shadow_factory() -> ReadOnlyMarketData:
    return build_market_data()


# ================================================================ one cycle
@dataclass
class CycleInput:
    series: list[str]
    run_budget: float
    max_order: float
    open_eps: list[dict] = field(default_factory=list)
    ended_unread: list[dict] = field(default_factory=list)     # up to SHADOW_MAX_TRADE_READS + 1 (oldest first)
    unsettled: list[dict] = field(default_factory=list)        # result null and close_time <= now (oldest first)
    new_today: int = 0
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    standin: bool = False
    # S3: None = no arbitrage scan this cycle; else {"series", "max_contracts", "min_profit", "scan_cap"}
    arb: dict | None = None
    # 2A-3 F1: ended but unsettled episodes (the held position), the assumed capital and the open-exposure share
    held: list[dict] = field(default_factory=list)
    capital: float = SHADOW_CAPITAL_DEFAULT
    max_open_frac: float = DEFAULT_MAX_OPEN_FRAC
    # 2A-3 F7: the parsed fee schedule, None (every series maker), or "file" (load it in the worker)
    fee_schedule: Any = "file"
    # 2A-3 F1: the per-series share of the open-exposure ceiling, None (no series cap), or "file" (load it)
    series_share_limit: Any = "file"
    # H2: the per-market share of the same ceiling, None (no market cap), or "file" (load it); absent = no cap
    market_share_limit: Any = "file"
    interval: int = 10                                          # cadence in minutes (recorded on new episodes)
    new_cap: int = SHADOW_DAILY_NEW_CAP                         # wave 2: variants get a smaller share of the cap
    retire: list[dict] = field(default_factory=list)            # wave 2: open episodes of a variant now off


@dataclass
class CycleResult:
    upserts: list[dict]
    pass_doc: dict
    arb: dict | None = None          # S3: the arbitrage scan result (None when it did not run)


def ep_id_for(ticker: str, book_side: str, order_price: float, join_ts: datetime,
              interval: int = 10) -> str:
    """Baseline ids are unchanged; a variant's id adds its interval so two cadences never share an episode."""
    key = f"{ticker}|{book_side}|{float(order_price):.4f}|{join_ts.isoformat()}"
    if int(interval) != BASELINE_INTERVAL:
        key += f"|i{int(interval)}"
    return hashlib.sha256(key.encode()).hexdigest()


def key_of(ticker: Any, book_side: Any, order_price: Any) -> tuple[str, str, str] | None:
    p = qp._f(order_price)
    if not ticker or book_side not in ("bid", "ask") or p is None:
        return None
    return str(ticker), str(book_side), f"{p:.4f}"


def _dt(v: Any) -> datetime | None:
    return ctsvc.to_utc(v)


def _err(errors: list[str], e: BaseException | str) -> None:
    """Record an error by exception type name only (messages can echo URLs or bodies)."""
    name = e if isinstance(e, str) else type(e).__name__
    if len(errors) < 50:
        errors.append(name)


def first_through(trades: Iterable[dict], side: str, price: float, since: datetime,
                  until: datetime | None) -> datetime | None:
    """Time of the first trade below our price on our side (a trade-through) in [since, until], or None."""
    first = None
    for t in qp.dicts(list(trades)):
        tt, yp = fq._ts(t.get("created_time") or t.get("ts")), fq.trade_yes_price(t)
        if tt is None or yp is None or tt < since or (until is not None and tt > until):
            continue
        q = yp if side == "yes" else 1.0 - yp
        if q < price - qp.PRICE_TOL and (first is None or tt < first):
            first = tt
    return first


def _quote_fields(q: Any) -> dict | None:
    """The plain fields of one strategy Quote, or None if it is malformed."""
    try:
        ticker, side, book_side = getattr(q, "ticker"), getattr(q, "side"), getattr(q, "book_side")
        price, count, order_price = float(q.price), float(q.count), float(q.order_price)
        cell = list(getattr(q, "cell", ()) or ())
    except (AttributeError, TypeError, ValueError):
        return None
    if not ctsvc.valid_ticker(ticker) or side not in ("yes", "no") or book_side not in ("bid", "ask"):
        return None
    if not (0 < price < 1) or not (0 < order_price < 1) or count <= 0:
        return None
    return {"ticker": ticker, "side": side, "book_side": book_side, "price": price, "count": count,
            "order_price": order_price, "cell": cell}


def _end(ep: dict, reason: str, at: datetime) -> dict:
    ep["end_ts"], ep["end_reason"] = at, reason
    return ep


def _maybe_score(ep: dict) -> None:
    if ep.get("capped") is True:                                # F1: a refused bid is never scored
        ep["pnl"] = None
        return
    if ep.get("result") in ("yes", "no") and ep.get("fill"):
        try:
            ep["pnl"] = sm.score_episode(ep)
        except (ValueError, TypeError):
            ep["pnl"] = None


# ================================================================ 2A-3 helpers: fee schedule, caps, trade window
def fee_schedule_path() -> Path:
    """FEE_SCHEDULE_PATH if set, else control_api/data/fee_schedule.json (absent in the public repo)."""
    env = (os.environ.get(FEE_SCHEDULE_ENV) or "").strip()
    return Path(env) if env else FEE_SCHEDULE_PATH


def parse_fee_schedule(doc: Any) -> dict | None:
    """{"default": mode, "rules": [(prefix, mode), ...]} from the file's JSON, or None when it is invalid (then
    every series uses the maker fee). Format: {"default": "maker"|"zero", "prefix_rules": [{"series_prefix": str,
    "mode": "zero"|"maker"}, ...]}. Never raises."""
    if not isinstance(doc, dict):
        return None
    default = doc.get("default", "maker")
    rules = doc.get("prefix_rules", [])
    if default not in sm.FEE_MODES or not isinstance(rules, list) or len(rules) > FEE_SCHEDULE_MAX_RULES:
        return None
    out = []
    for r in rules:
        if not isinstance(r, dict):
            return None
        pre, mode = r.get("series_prefix"), r.get("mode")
        if not isinstance(pre, str) or not pre or len(pre) > 64 or mode not in sm.FEE_MODES:
            return None
        out.append((pre, mode))
    return {"default": default, "rules": out}


def _read_optional_json(p: Path, max_bytes: int) -> Any:
    """The JSON of an optional private data file, or None when it is missing, too large, unreadable or malformed
    (a deeply nested document's RecursionError included). Never raises; the contents are never logged."""
    try:
        if not p.is_file() or p.stat().st_size > max_bytes:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError, MemoryError):
        return None


def load_fee_schedule(path: Path | str | None = None) -> dict | None:
    """Read and parse the optional fee schedule once (per cycle). Missing, unreadable, too large or invalid ->
    None. The file's contents are never logged."""
    p = Path(path) if path is not None else fee_schedule_path()
    try:
        return parse_fee_schedule(_read_optional_json(p, FEE_SCHEDULE_MAX_BYTES))
    except RecursionError:
        return None


def shadow_limits_path() -> Path:
    """SHADOW_LIMITS_PATH if set, else control_api/data/shadow_limits.json (absent in the public repo)."""
    env = (os.environ.get(SHADOW_LIMITS_ENV) or "").strip()
    return Path(env) if env else SHADOW_LIMITS_PATH


def parse_series_limit(v: Any) -> float | None:
    """A per-series share of the ceiling in (0, 1], else None (no series cap)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and 0 < f <= 1 else None


def load_series_limit(path: Path | str | None = None) -> float | None:
    """The optional private limits file's `series_share_limit`, read once per cycle. Missing, unreadable,
    invalid or out of range -> None: no per-series cap (as in the public repo)."""
    doc = _read_optional_json(Path(path) if path is not None else shadow_limits_path(), SHADOW_LIMITS_MAX_BYTES)
    return parse_series_limit(doc.get("series_share_limit")) if isinstance(doc, dict) else None


def load_market_limit(path: Path | str | None = None) -> float | None:
    """The optional private limits file's `market_share_limit` (a share of the open-exposure ceiling in (0, 1]),
    read once per cycle like `series_share_limit`. Missing, unreadable, invalid or out of range -> None: no
    per-market cap (as in the public repo)."""
    doc = _read_optional_json(Path(path) if path is not None else shadow_limits_path(), SHADOW_LIMITS_MAX_BYTES)
    return parse_series_limit(doc.get("market_share_limit")) if isinstance(doc, dict) else None


def market_room(cap: float, spent: float) -> float:
    """Dollars a market may still hold under `cap` after `spent` (held plus resting), never negative."""
    return max(0.0, float(cap) - float(spent))


def sized_to_room(count: float, price: float, room: float) -> float:
    """The strategy's size-down rule: the largest count in 0.01-contract steps at `price` that fits `room` dollars
    (int(room / price x 100) / 100, with 1e-9 slack against float noise), at most `count`; 0 (no quote) when the
    result is below one contract."""
    if price <= 0 or room <= 0:
        return 0.0
    n = math.floor(room / price * 100 + 1e-9) / 100
    return float(min(n, count)) if n >= 1 else 0.0


def fee_mode_for(ticker: Any, sched: dict | None) -> str:
    """A market's fee mode: the first rule whose prefix starts its series (the ticker before the first '-'),
    else the schedule's default; no schedule -> "maker"."""
    if not sched:
        return "maker"
    series = str(ticker or "").split("-", 1)[0]
    for pre, mode in sched["rules"]:
        if series.startswith(pre):
            return mode
    return sched["default"]


def capital_of(cloud: Any) -> float:
    """The cloud setting shadow_capital, clamped to its range; the default when missing or malformed."""
    v = (cloud or {}).get("shadow_capital") if isinstance(cloud, dict) else None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return SHADOW_CAPITAL_DEFAULT
    if isinstance(v, bool) or not math.isfinite(f):
        return SHADOW_CAPITAL_DEFAULT
    return min(SHADOW_CAPITAL_MAX, max(SHADOW_CAPITAL_MIN, f))


def open_frac_of(cfg: Any, ceiling: Any = None) -> float:
    """The strategy's max_open_frac (config), never above the API ceiling `ceiling` when given."""
    try:
        f = float((cfg or {}).get("max_open_frac"))
    except (TypeError, ValueError, AttributeError):
        f = DEFAULT_MAX_OPEN_FRAC
    if not math.isfinite(f) or f <= 0:
        f = DEFAULT_MAX_OPEN_FRAC
    try:
        if ceiling is not None:
            f = min(f, float(ceiling))
    except (TypeError, ValueError):
        pass
    return min(1.0, f)


def held_cost(ep: dict) -> float:
    """Dollars one ended, unsettled episode holds: central fill x price; an unknown fill counts in full (the
    conservative side). Capped, voided and settled episodes hold nothing."""
    if ep.get("capped") is True or ep.get("result") is not None or ep.get("end_ts") is None:
        return 0.0
    price, count = qp._f(ep.get("price")) or 0.0, qp._f(ep.get("count")) or 0.0
    fill = ep.get("fill")
    if isinstance(fill, dict):
        return max(0.0, (qp._f(fill.get("mid")) or 0.0) * price)
    return max(0.0, count * price)


def held_ignored(ep: dict, now: datetime) -> bool:
    """True for an ended episode that no longer counts toward the simulated held position: a legacy (methodology 1,
    uncapped) one, or one whose market closed more than SHADOW_HELD_GRACE ago, whether or not a result was read.
    An episode with no stored methodology and no close time (a bare fixture) is never ignored."""
    if "methodology" in ep and methodology_of(ep) != METHODOLOGY:
        return True
    close = _dt(ep.get("close_time"))
    return close is not None and now - close > SHADOW_HELD_GRACE


def series_of(ep: dict) -> str:
    return str(ep.get("series") or str(ep.get("ticker") or "").split("-", 1)[0])


def through_volume(trades: Iterable[dict], side: str, price: float, since: datetime, until: datetime | None) -> float:
    """F4: contracts traded strictly through (below) our price on our side in [since, until], any taker side."""
    vol = 0.0
    for t in qp.dicts(list(trades)):
        tt, yp = fq._ts(t.get("created_time") or t.get("ts")), fq.trade_yes_price(t)
        if tt is None or yp is None or tt < since or (until is not None and tt > until):
            continue
        q = yp if side == "yes" else 1.0 - yp
        if q < price - qp.PRICE_TOL:
            vol += qp.trade_count(t)
    return vol


def trade_window(trades: list[dict], end: datetime, exclusive: bool) -> list[dict]:
    """F8: an episode ended by cancel owns [join, end): a trade at exactly `end` belongs to the next episode."""
    if not exclusive:
        return trades
    out = []
    for t in qp.dicts(trades):
        tt = fq._ts(t.get("created_time") or t.get("ts"))
        if tt is not None and tt < end:
            out.append(t)
    return out


def run_cycle(md: Any, strategy: Any, inp: CycleInput, *, deadline: float | None = None) -> CycleResult:
    """One shadow cycle against `md` (ReadOnlyMarketData or a test fake). Synchronous; network reads only.
    Raises ShadowDeadline past `deadline` (time.monotonic()), so the caller discards a partial result.

    Order: (1) books and the strategy's uncapped desired quotes; (2) diff against the open episodes; (3) public
    trades of ended episodes (including those ended in step 2, so the held position is current); (4) settlement;
    (5) the held position and the real limits (2A-3 F1): headroom = min(run budget, max_open_frac x capital - held,
    capital - held), split evenly across series, zero for a series at its per-series share (optional limits file) of the
    ceiling; the strategy is asked again with the capped budget, the resting total per series is checked against
    it, and a bid the limits refuse is recorded as a capped episode (never scored); (6) new episodes."""
    t0 = time.monotonic()
    now = inp.now
    errors: list[str] = []
    truncated = False
    updates: dict[str, dict] = {}

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise ShadowDeadline("shadow cycle past its deadline")

    def cur(ep: dict) -> dict:
        eid = ep["ep_id"]
        if eid not in updates:
            updates[eid] = dict(ep)
        return updates[eid]

    sched = load_fee_schedule() if isinstance(inp.fee_schedule, str) else inp.fee_schedule
    series = [str(s).strip() for s in inp.series if str(s).strip()]
    n = max(1, len(series))
    run_budget = max(0.0, float(inp.run_budget))
    per_series = run_budget / n
    order_cap = min(float(inp.max_order), per_series)
    # H2: the optional per-market cap (a share of the ceiling), read once per cycle; None = no market cap
    mfrac = (load_market_limit() if isinstance(inp.market_share_limit, str)
             else parse_series_limit(inp.market_share_limit))
    ceiling = max(0.0, float(inp.max_open_frac)) * max(0.0, float(inp.capital))
    mcap = mfrac * ceiling if mfrac is not None else None
    market_capped: set[str] = set()                             # tickers whose quote was refused or sized down

    # ---- 1) markets, books and the strategy's desired quotes (uncapped), per series
    books_read = n_markets = n_quotes = new_eps = ended_eps = capped_new = 0
    listed: set[str] = set()                 # series whose market list is complete
    observed: set[str] = set()               # tickers whose book was read AND whose series' quotes were computed
    open_tickers: set[str] = set()
    books: dict[str, dict] = {}
    market_close: dict[str, datetime | None] = {}
    desired: dict[tuple[str, str, str], dict] = {}
    # H2: keys the budget flow might quote (each market asked alone with the whole series budget); only used to
    # keep a continuing bid in step 2 until step 5 decides, so a flowed-to market is not cancelled and re-joined
    potential: dict[tuple[str, str, str], dict] = {}
    seen_series: dict[str, tuple[list[dict], dict]] = {}       # series -> (usable markets, their books)
    for s in series:
        check_deadline()
        try:
            mkts, trunc = md.markets(s, "open")
        except Exception as e:  # noqa: BLE001
            _err(errors, e)
            continue
        mkts = [m for m in qp.dicts(mkts) if ctsvc.valid_ticker(m.get("ticker"))]
        truncated |= bool(trunc)
        if not trunc:
            listed.add(s)
        n_markets += len(mkts)
        usable: list[dict] = []
        for m in mkts:
            t = m["ticker"]
            open_tickers.add(t)
            market_close[t] = ctsvc.from_market(m)
        for m in mkts:
            if books_read >= SHADOW_MAX_BOOKS:
                truncated = True
                break
            check_deadline()
            try:
                books[m["ticker"]] = md.orderbook(m["ticker"])
                books_read += 1
                usable.append(m)
            except Exception as e:  # noqa: BLE001
                _err(errors, e)
        if not usable:
            continue
        sbooks = {m["ticker"]: books[m["ticker"]] for m in usable}
        try:
            want = list(strategy.desired_quotes(usable, sbooks, now, order_cap, per_series) or [])
        except Exception as e:  # noqa: BLE001   (the installed strategy failed: leave this series alone)
            _err(errors, e)
            continue
        observed.update(sbooks)
        seen_series[s] = (usable, sbooks)
        for f in _desired_fields(want, sbooks, errors):
            k = key_of(f["ticker"], f["book_side"], f["order_price"])
            if k is not None and k not in desired:
                f["series"] = s
                desired[k] = f
        n_quotes += len(want)
        if mcap is not None:
            for m in usable:
                try:
                    alone = list(strategy.desired_quotes([m], {m["ticker"]: sbooks[m["ticker"]]}, now,
                                                         order_cap, per_series) or [])
                except Exception as e:  # noqa: BLE001
                    _err(errors, e)
                    continue
                for f in _desired_fields(alone, sbooks, errors):
                    k = key_of(f["ticker"], f["book_side"], f["order_price"])
                    if k is not None and k not in desired and k not in potential:
                        f["series"] = s
                        potential[k] = f

    # ---- 2) diff the desired keys against the open episodes (first: retire the open bids of a variant now off)
    for ep in inp.retire:
        if ep.get("ep_id") and ep.get("end_ts") is None:
            _end(cur(ep), "shadow_off", _dt(ep.get("last_seen_ts")) or _dt(ep.get("join_ts")) or now)
            ended_eps += 1
    by_key: dict[tuple[str, str, str], dict] = {}
    for ep in inp.open_eps:
        check_deadline()
        k = key_of(ep.get("ticker"), ep.get("book_side"), ep.get("order_price"))
        t = str(ep.get("ticker") or "")
        close = _dt(ep.get("close_time"))
        last = _dt(ep.get("last_seen_ts")) or _dt(ep.get("join_ts")) or now
        if k is None or k in by_key:                           # unreadable or a duplicate: end it
            _end(cur(ep), "stale", last); ended_eps += 1
            continue
        if (close is not None and close <= now) or (ep.get("series") in listed and t not in open_tickers):
            _end(cur(ep), "closed", min(now, close) if close else now); ended_eps += 1
            continue
        if ep.get("series") not in series or now - last > SHADOW_STALE_AFTER:
            _end(cur(ep), "stale", last); ended_eps += 1
            continue
        if t in observed:
            if k in desired or k in potential:
                by_key[k] = ep
                f = desired.get(k) or potential[k]
                level = qp.level_size(books.get(t), f["side"], f["price"])
                c = cur(ep)
                c["last_seen_ts"] = now
                if level is not None:
                    lm = qp._f(c.get("level_min"))
                    c["level_min"] = level if lm is None else min(lm, level)
                    c["level_last"] = level
                    c["n_obs"] = int(c.get("n_obs") or 0) + 1
            else:
                _end(cur(ep), "cancel", now); ended_eps += 1
        # else: not observed this cycle (a failed read or truncation): untouched

    # ---- 3) public trades, once per ended episode (older ones first, then the ones ended just now)
    trade_reads = 0
    if len(inp.ended_unread) > SHADOW_MAX_TRADE_READS:
        truncated = True
    queue = list(inp.ended_unread[:SHADOW_MAX_TRADE_READS])
    queued = {e.get("ep_id") for e in queue}
    queue += [u for u in list(updates.values()) if u.get("end_ts") is not None and u.get("trades_read") is not True
              and u.get("capped") is not True and u.get("ep_id") not in queued]
    for ep in queue[:SHADOW_MAX_TRADE_READS]:
        check_deadline()
        c = cur(ep)
        join, end = _dt(c.get("join_ts")), _dt(c.get("end_ts"))
        side, price = c.get("side"), qp._f(c.get("price"))
        if join is None or end is None or side not in ("yes", "no") or price is None:
            c["trades_read"], c["fill"] = True, None
            continue
        try:
            trades, trunc = md.trades(c["ticker"], int(join.timestamp()), int(end.timestamp()) + 1)
        except Exception as e:  # noqa: BLE001   (retried next cycle, up to SHADOW_MAX_TRADE_ATTEMPTS)
            _err(errors, e)
            c["trade_attempts"] = int(c.get("trade_attempts") or 0) + 1
            if c["trade_attempts"] >= SHADOW_MAX_TRADE_ATTEMPTS:         # SH-3: give up, never block the queue
                c.update({"trades_read": True, "fill": None, "truncated": True, "trade_failed": True})
            continue
        trade_reads += 1
        trades = trade_window(qp.dicts(list(trades)), end, c.get("end_reason") == "cancel")    # F8
        vol, through = qp.scan_trades(trades, side, price, join, end)
        tvol = through_volume(trades, side, price, join, end)                                   # F4
        c.update({"trades_read": True, "traded_vol": round(vol, 4), "through": bool(through),
                  "through_ts": first_through(trades, side, price, join, end) if through else None,
                  "through_vol": round(tvol, 4), "truncated": bool(trunc),
                  "fill": sm.fill_scenarios(c.get("q0"), c.get("level_min"), vol, through, c.get("count"),
                                            through_vol=tvol)})
        truncated |= bool(trunc)
        _maybe_score(c)

    # ---- 4) settlement from the market's result
    settled = voided = 0
    groups: dict[str, list[dict]] = {}
    for ep in inp.unsettled:
        if ctsvc.valid_ticker(ep.get("ticker")):
            groups.setdefault(ep["ticker"], []).append(ep)
    if len(groups) > SHADOW_MAX_SETTLE_READS:
        truncated = True
    for t in list(groups)[:SHADOW_MAX_SETTLE_READS]:
        check_deadline()
        for ep in groups[t]:                                    # SH-1: rotate the queue, even on a failed read
            cur(ep)["settle_checked_ts"] = now
        try:
            m = md.market(t)
        except Exception as e:  # noqa: BLE001
            _err(errors, e)
            continue
        res = str(m.get("result") or "").lower()
        if res not in ("yes", "no"):
            closes = [d for d in (_dt(e.get("close_time")) for e in groups[t]) if d is not None]
            final = str(m.get("status") or "").lower() in SETTLED_STATUSES
            if not (final or (closes and now - max(closes) > SHADOW_VOID_AFTER)):
                continue                                        # not resolved yet: checked again later
            res = "void"                                        # SH-1: voided / never resolved: out of the estimate
        for ep in groups[t]:
            c = cur(ep)
            close = _dt(c.get("close_time"))
            c["result"] = res
            c["settled_ts"] = _dt(m.get("settlement_ts")) or close or now
            if c.get("end_ts") is None:
                _end(c, "closed", min(now, close) if close else now); ended_eps += 1
            if res == "void":
                c["pnl"] = None
                voided += 1
            else:
                _maybe_score(c)
                settled += 1

    # ---- 5) the held position and the real limits (F1)
    held_eps: dict[str, dict] = {}
    for ep in inp.held:
        if isinstance(ep, dict) and ep.get("ep_id"):
            held_eps[ep["ep_id"]] = ep
    if len(inp.held) >= SHADOW_HELD_SCAN:
        truncated = True
    for eid, u in updates.items():                              # this cycle's version wins
        held_eps[eid] = u
    held_total = 0.0
    held_series: dict[str, float] = {}
    held_markets: set[tuple[str, str]] = set()
    held_ticker: dict[str, float] = {}                          # H2: a market's held cost, both sides together
    held_stale_ignored, held_stale_cost = 0, 0.0
    for ep in held_eps.values():
        h = held_cost(ep)
        if h <= 0:
            continue
        if held_ignored(ep, now):                               # legacy, or long past its close: not held capital
            held_stale_ignored += 1
            held_stale_cost += h
            continue
        held_total += h
        sk = series_of(ep)
        held_series[sk] = held_series.get(sk, 0.0) + h
        tk = str(ep.get("ticker") or "")
        held_ticker[tk] = held_ticker.get(tk, 0.0) + h
        held_markets.add((str(ep.get("ticker")), str(ep.get("side"))))
    capital = max(0.0, float(inp.capital))
    sfrac = (load_series_limit() if isinstance(inp.series_share_limit, str)
             else parse_series_limit(inp.series_share_limit))

    def _cost(ep: dict) -> float:
        c = qp._f(ep.get("cost"))
        return c if c is not None else (qp._f(ep.get("count")) or 0.0) * (qp._f(ep.get("price")) or 0.0)

    # open bids not observed this cycle (a failed read) still rest: their cost is spoken for
    unseen: dict[str, float] = {}
    unseen_ticker: dict[str, float] = {}
    kept_ids = {e.get("ep_id") for e in by_key.values()}
    for ep in inp.open_eps:
        u = updates.get(ep.get("ep_id"), ep)
        if u.get("end_ts") is None and u.get("capped") is not True and u.get("ep_id") not in kept_ids:
            sk = str(u.get("series") or "")
            unseen[sk] = unseen.get(sk, 0.0) + _cost(u)
            tk = str(u.get("ticker") or "")
            unseen_ticker[tk] = unseen_ticker.get(tk, 0.0) + _cost(u)

    s_hit: set[str] = set()                                     # series whose limit sized a bid away this round

    def _size_with_room(sk: str, usable: list, sbooks: dict, budget: float,
                        sroom: float | None) -> tuple[dict, dict]:
        """The strategy's budget flow with the room limits: the installed strategy is asked one market at a time with
        the budget left; each quote is sized down to the SMALLER of its market's room (H2, only with a market cap) and
        the series' room (`sroom`: series limit x ceiling minus the series' held cost, unseen bids and what this pass
        has already taken; None = no series limit), in 0.01-contract steps, and the series budget is debited by the
        cost actually taken, so what a capped market cannot use flows to the later markets (bounded by their own
        caps, the series budget and max order). A quote with room for under one contract is refused and debits
        nothing. Sizing stops once less than $1 is left. Returns (sized quotes, {refused key: cap reason})."""
        left, out, refused = float(budget), {}, {}
        used: dict[str, float] = {}
        used_series = 0.0
        for m in usable:
            if left < 1.0 - 1e-9:
                break
            tk = m["ticker"]
            try:
                want = list(strategy.desired_quotes([m], {tk: sbooks[tk]}, now,
                                                    min(float(inp.max_order), left), left) or [])
            except Exception as e:  # noqa: BLE001   (this market is left alone)
                _err(errors, e)
                continue
            for f in _desired_fields(want, {tk: sbooks[tk]}, errors):
                k = key_of(f["ticker"], f["book_side"], f["order_price"])
                if k is None or k in out or k in refused:
                    continue
                f["series"] = sk
                m_room = (market_room(mcap, held_ticker.get(tk, 0.0) + extra_ticker.get(tk, 0.0)
                                      + unseen_ticker.get(tk, 0.0) + used.get(tk, 0.0))
                          if mcap is not None else math.inf)
                s_room = math.inf if sroom is None else max(0.0, sroom - used_series)
                room = min(m_room, s_room)
                if f["count"] * f["price"] > room + CAP_TOL:
                    why = "market_cap" if m_room <= s_room else "series_cap"
                    if why == "market_cap":
                        market_capped.add(tk)
                    n = sized_to_room(f["count"], f["price"], room)
                    if n < 1:
                        refused[k] = why
                        if why == "series_cap":
                            s_hit.add(sk)
                        continue
                    f = {**f, "count": n}
                cost = f["count"] * f["price"]
                debit = min(cost, cont_cost[k]) if k in cont_cost else cost
                out[k] = f
                used[tk] = used.get(tk, 0.0) + debit
                used_series += debit
                left -= round(debit, 2)
        return out, refused

    # V1: a continuing bid the caps cancel may already have filled; its trades are read next cycle, so until then
    # it counts as held at its full cost (an unknown fill). Cancelling more bids only adds to the held position and
    # shrinks the budgets, so this loop settles after at most one pass per continuing bid.
    cancel_now: dict[tuple[str, str, str], dict] = {}
    continuing = [(k, ep) for k, ep in by_key.items() if ep.get("capped") is not True]
    cont_cost = {k: _cost(ep) for k, ep in continuing}          # H2: what a continuing bid really rests
    for _round in range(len(continuing) + 1):
        extra_total = sum(_cost(ep) for ep in cancel_now.values())
        extra_series: dict[str, float] = {}
        extra_ticker: dict[str, float] = {}
        for ep in cancel_now.values():
            sk = series_of(ep)
            extra_series[sk] = extra_series.get(sk, 0.0) + _cost(ep)
            tk = str(ep.get("ticker") or "")
            extra_ticker[tk] = extra_ticker.get(tk, 0.0) + _cost(ep)
        h_total = held_total + extra_total
        h_series = {k: held_series.get(k, 0.0) + extra_series.get(k, 0.0)
                    for k in set(held_series) | set(extra_series)}
        headroom = max(0.0, min(run_budget, ceiling - h_total, capital - h_total))
        # F1 + series room: the series may hold, rest and be quoted up to limit x ceiling in all, so a new bid is
        # sized to what is left of that after the series' held cost (a cancelled bid's unknown fill included) and its
        # unseen resting bids; bids that continue and new ones are debited as they are sized (see _size_with_room).
        # A series with under a cent of room cannot take even one 1-cent contract: it is series-capped
        cap_amt = sfrac * ceiling if sfrac is not None else None

        def series_room0(s: str) -> float | None:
            if cap_amt is None:
                return None
            return max(0.0, cap_amt - h_series.get(s, 0.0) - unseen.get(s, 0.0))

        s_hit.clear()
        series_capped = sorted(s for s in series if sfrac is not None
                               and h_series.get(s, 0.0) + unseen.get(s, 0.0) > 0 and series_room0(s) < 0.01 - 1e-9)
        capped_per_series = headroom / n
        limited = headroom < run_budget - 1e-9

        allowed: dict[tuple[str, str, str], dict] = {}
        m_refused: dict[tuple[str, str, str], str] = {}         # refused for want of room: key -> cap reason
        budgets: dict[str, float] = {}
        reason_of: dict[str, str] = {}
        for s in series:
            sroom = series_room0(s)
            if s in series_capped:
                b = bud = 0.0
            else:
                b = capped_per_series if sroom is None else min(capped_per_series, sroom)
                bud = capped_per_series if cap_amt is None else min(capped_per_series,
                                                                    max(0.0, cap_amt - h_series.get(s, 0.0)))
            budgets[s] = bud                                    # `spent` starts at the unseen bids
            series_binds = s in series_capped or (sroom is not None and sroom < capped_per_series - 1e-9)
            reason_of[s] = "series_cap" if series_binds else ("open_cap" if limited else "run_budget")
            if s not in seen_series:
                continue
            if mcap is None and b >= per_series - 1e-9:         # no limit binds: the uncapped answer stands
                for k, f in desired.items():
                    if f["series"] == s:
                        allowed[k] = f
                continue
            if b <= 0:
                continue
            usable, sbooks = seen_series[s]
            check_deadline()
            if mcap is not None or sroom is not None:           # size market by market, debiting what is taken
                got, refused = _size_with_room(s, usable, sbooks, capped_per_series, sroom)
                allowed.update(got)
                m_refused.update(refused)
                continue
            try:
                want = list(strategy.desired_quotes(usable, sbooks, now, min(float(inp.max_order), b), b) or [])
            except Exception as e:  # noqa: BLE001   (the capped call failed: nothing new for this series)
                _err(errors, e)
                continue
            for f in _desired_fields(want, sbooks, errors):
                k = key_of(f["ticker"], f["book_side"], f["order_price"])
                if k is not None and k not in allowed:
                    f["series"] = s
                    allowed[k] = f

        # resting cost per series never above its budget: unseen bids, then continuing ones, then new ones
        spent: dict[str, float] = {s: unseen.get(s, 0.0) for s in series}

        # H2: per market, held (filled, plus a cancelled bid's unknown fill) and resting cost, both sides together
        mspent: dict[str, float] = {}
        if mcap is not None:
            for tk in set(held_ticker) | set(extra_ticker) | set(unseen_ticker):
                mspent[tk] = held_ticker.get(tk, 0.0) + extra_ticker.get(tk, 0.0) + unseen_ticker.get(tk, 0.0)

        def fits(s: str, cost: float, tk: str = "") -> bool:
            if spent.get(s, 0.0) + cost > budgets.get(s, 0.0) + CAP_TOL:
                return False
            if mcap is not None and mspent.get(tk, 0.0) + cost > mcap + CAP_TOL:
                return False
            spent[s] = spent.get(s, 0.0) + cost
            if mcap is not None:
                mspent[tk] = mspent.get(tk, 0.0) + cost
            return True

        more = {k: ep for k, ep in continuing
                if k not in cancel_now
                and not (k in allowed and fits(str(ep.get("series") or ""), _cost(ep), str(ep.get("ticker") or "")))}
        if not more:
            break
        cancel_now.update(more)
    for k, ep in cancel_now.items():
        _end(cur(ep), "cancel", now); ended_eps += 1           # the real bot would have cancelled it
        del by_key[k]
    held_for_doc = h_total

    # ---- 6) new episodes (at the back of the queue: the whole level is ahead of us)
    candidates = list(allowed.items()) + [(k, f) for k, f in desired.items() if k not in allowed]
    for k, f in candidates:
        prev = by_key.get(k)
        if prev is not None and prev.get("capped") is not True:
            continue                                            # an uncapped bid already rests at this key
        ok = k in allowed and fits(f["series"], f["count"] * f["price"], f["ticker"])
        reason = reason_of.get(f["series"], "run_budget")
        if not ok and k in m_refused:                           # no room for one contract in its market / series
            reason = m_refused[k]
            if reason == "market_cap":
                market_capped.add(f["ticker"])
        elif not ok and mcap is not None and k in allowed:
            # H2: only the market cap refused it (the series budget takes the whole cost): size it down to the room
            # left (0.01-contract floor, like the strategy); below one contract it stays refused as "market_cap"
            full = f["count"] * f["price"]
            if spent.get(f["series"], 0.0) + full <= budgets.get(f["series"], 0.0) + CAP_TOL:
                market_capped.add(f["ticker"])
                n_fit = sized_to_room(f["count"], f["price"], market_room(mcap, mspent.get(f["ticker"], 0.0)))
                if n_fit >= 1:
                    f = {**f, "count": n_fit}
                    ok = fits(f["series"], f["count"] * f["price"], f["ticker"])
                if not ok:
                    reason = "market_cap"
        if not ok and reason == "series_cap":
            s_hit.add(f["series"])
        if prev is not None and not ok:
            continue                                            # still refused: the capped episode continues
        if inp.new_today + new_eps >= min(SHADOW_DAILY_NEW_CAP, int(inp.new_cap)):
            truncated = True
            break
        level = qp.level_size(books.get(f["ticker"]), f["side"], f["price"])
        if level is None:
            _err(errors, "BookUnreadable")
            continue
        eid = ep_id_for(f["ticker"], f["book_side"], f["order_price"], now, inp.interval)
        if eid in updates:
            continue
        if prev is not None:                                    # now affordable: the refused one ends here
            _end(cur(prev), "cancel", now); ended_eps += 1
            del by_key[k]
        doc = {
            "ep_id": eid, "env": "prod", "ticker": f["ticker"], "series": f["series"], "side": f["side"],
            "book_side": f["book_side"], "price": f["price"], "order_price": f["order_price"], "count": f["count"],
            "cost": round(f["count"] * f["price"], 4), "cell": f["cell"], "close_time": market_close.get(f["ticker"]),
            "join_ts": now, "last_seen_ts": now, "end_ts": None, "end_reason": None, "q0": level,
            "level_min": level, "level_last": level, "n_obs": 1, "trades_read": False, "traded_vol": 0.0,
            "through": False, "through_ts": None, "through_vol": None, "truncated": False, "fill": None,
            "result": None, "settled_ts": None, "pnl": None, "standin": bool(inp.standin), "ts": now,
            "capped": False, "cap_reason": None, "interval": int(inp.interval), "methodology": METHODOLOGY,
            "fee_mode": fee_mode_for(f["ticker"], sched)}
        if not ok:                                              # F1: refused by a limit: recorded, never scored
            doc.update({"capped": True, "cap_reason": reason,
                        "trades_read": True, "fill": {"lo": 0.0, "mid": 0.0, "hi": 0.0}})
            capped_new += 1
        updates[eid] = doc
        new_eps += 1

    pass_doc = {"ts": now, "ok": not errors, "duration_s": round(time.monotonic() - t0, 3), "markets": n_markets,
                "books": books_read, "quotes": n_quotes, "new_eps": new_eps, "ended_eps": ended_eps,
                "trade_reads": trade_reads, "settled": settled, "voided": voided, "truncated": bool(truncated),
                "errors": list(dict.fromkeys(errors)),
                # 2A-3 F1
                "capped_new": capped_new, "held_cost": round(held_for_doc, 4), "held_markets": len(held_markets),
                "ceiling": round(ceiling, 4), "headroom": round(headroom, 4),
                "series_capped": sorted(set(series_capped) | s_hit),
                "held_stale_ignored": held_stale_ignored, "held_stale_cost": round(held_stale_cost, 4)}
    if mcap is not None:                                        # H2: additive, only when a market cap is in force
        pass_doc["market_capped"] = sorted(market_capped)[:PASS_MARKET_CAPPED_MAX]
    arb = None
    if inp.arb:                                                 # S3: last, own budgets, never raises
        arb = arb_scan(md, inp.arb, now, deadline=deadline)
        pass_doc["arb_opps"] = len(arb["opps"])
        pass_doc["arb_skipped"] = arb["skipped"]
    return CycleResult(upserts=list(updates.values()), pass_doc=pass_doc, arb=arb)


def _desired_fields(want: list, sbooks: dict, errors: list[str]) -> list[dict]:
    """The plain fields of each valid quote on a market whose book was read; a bad one is recorded by name."""
    out = []
    for q in want:
        f = _quote_fields(q)
        if f is None or f["ticker"] not in sbooks:
            _err(errors, "BadQuote")
            continue
        out.append(f)
    return out


# ================================================================ S3: arbitrage shadow scan (read only)
def arb_enabled() -> bool:
    """Env SHADOW_ARB (default off), read at each cycle so it can be measured and switched without a deploy of
    code (an env change on Render restarts the service)."""
    return os.getenv(SHADOW_ARB_ENV, "false").strip().lower() == "true"


def arb_input(arb_settings: dict) -> dict:
    """The CycleInput.arb block from the effective arbitrage settings (services/arb.current)."""
    return {"series": [str(s) for s in (arb_settings.get("arb_series") or [])],
            "max_contracts": float(arb_settings["arb_max_contracts"]),
            "min_profit": float(arb_settings["arb_min_profit"]),
            "scan_cap": float(arb_settings["arb_scan_cap"])}


def arb_scan(md: Any, cfg: dict, now: datetime, *, deadline: float | None = None, scanner: Any = None) -> dict:
    """One read-only arbitrage scan over production public data. Uses only `md.markets` / `md.orderbook` (the
    GET-allowlisted, credential-less client) and the installed scanner's pure `best_opportunity`; it never calls the
    scanner's scan or order functions and writes no file. Stops at SHADOW_ARB_MAX_REQUESTS, SHADOW_ARB_BUDGET_S or
    the cycle deadline (whichever first) and says so in `skipped`. Never raises."""
    t0 = time.monotonic()
    stop_at = t0 + SHADOW_ARB_BUDGET_S
    if deadline is not None:
        # L1: a read may start only if even a worst-case slow read (timeout x attempts) ends before the cycle
        # deadline, so the scan can never push the cycle past its budget and lose that cycle's episodes.
        stop_at = min(stop_at, deadline - SHADOW_ARB_READ_RESERVE_S)
    out = {"ts": now, "ok": True, "skipped": None, "requests": 0, "events_scanned": 0, "opps": [], "errors": []}

    def budget(n: int) -> str | None:
        if time.monotonic() > stop_at:
            return "time_budget"
        if out["requests"] + n > SHADOW_ARB_MAX_REQUESTS:
            return "request_budget"
        return None

    class _Stop(Exception):
        pass

    try:
        if scanner is None:
            from . import arb as arbsvc
            scanner = arbsvc.scanner()
        best = scanner.best_opportunity
        max_c, min_p, cap = float(cfg["max_contracts"]), float(cfg["min_profit"]), float(cfg["scan_cap"])
        series = list(cfg.get("series") or [])
        if series:                                   # rotate the start so a budget-capped scan covers every series
            k = int(now.timestamp() // 60) % len(series)
            series = series[k:] + series[:k]
        for s in series:
            why = budget(1)
            if why:
                out["skipped"] = why
                break
            try:
                mkts, trunc = md.markets(s, "open")
            except Exception as e:  # noqa: BLE001
                out["requests"] += 1
                _err(out["errors"], e)
                continue
            out["requests"] += SHADOW_MAX_MARKET_PAGES if trunc else 1
            events: dict[str, list[str]] = {}
            for m in qp.dicts(mkts):
                ev, t = m.get("event_ticker"), m.get("ticker")
                if ctsvc.valid_ticker(ev) and ctsvc.valid_ticker(t):
                    events.setdefault(ev, []).append(t)
            for ev, tickers in events.items():
                if out["events_scanned"] >= SHADOW_ARB_MAX_EVENTS:
                    out["skipped"] = "event_budget"
                    break
                why = budget(len(tickers))
                if why:
                    out["skipped"] = why
                    break
                books: dict[str, dict] = {}
                try:
                    for t in tickers:
                        why = budget(1)                 # L1: checked before EVERY book read, not once per event
                        if why:
                            out["skipped"] = why
                            raise _Stop
                        out["requests"] += 1
                        books[t] = md.orderbook(t)
                except _Stop:
                    break                               # a partly read event is dropped (it needs every leg)
                except Exception as e:  # noqa: BLE001   (an event needs every leg's book)
                    _err(out["errors"], e)
                    continue
                out["events_scanned"] += 1
                op = best(ev, books, max_c)
                if op is not None and float(op.profit) >= min_p:
                    cost = float(op.cost)
                    out["opps"].append({"event": str(op.event), "kind": str(op.kind), "profit": float(op.profit),
                                        "cost": cost, "contracts": float(op.contracts),
                                        "legs": len(list(op.legs or [])), "gate_ok": cost <= cap})
            if out["skipped"]:
                break
    except Exception as e:  # noqa: BLE001   (a scanner or settings problem: record it, keep the cycle)
        _err(out["errors"], e)
        out["skipped"] = out["skipped"] or "error"
    out["ok"] = not out["errors"]
    out["duration_s"] = round(time.monotonic() - t0, 3)
    out["errors"] = list(dict.fromkeys(out["errors"]))
    return out


async def persist_arb(store: Any, scan: dict, now: datetime, timeout: float = SHADOW_STORE_TIMEOUT_S) -> None:
    """Fold one scan into the cached summary doc's `arb` block (read, merge, replace). Raises on store errors."""
    doc = await asyncio.wait_for(store.get_shadow_summary(), timeout)
    doc = dict(doc) if isinstance(doc, dict) else sm.empty_summary(enabled=True)
    doc["arb"] = sm.arb_merge(doc.get("arb"), scan, now)
    await asyncio.wait_for(store.set_shadow_summary(doc), timeout)


async def carry_arb(store: Any, summary: dict, now: datetime, timeout: float = SHADOW_STORE_TIMEOUT_S) -> dict:
    """Copy the stored `arb` block into a freshly computed summary (summarize() does not know it), so a summary
    refresh never drops the arbitrage buckets. A failed read keeps the empty block."""
    try:
        doc = await asyncio.wait_for(store.get_shadow_summary(), timeout)
    except Exception:  # noqa: BLE001
        doc = None
    summary["arb"] = sm.arb_merge((doc or {}).get("arb") if isinstance(doc, dict) else None, None, now)
    return summary


# ================================================================ 2A-3 wave 2: interval variants
def variants_enabled() -> bool:
    """Env SHADOW_VARIANTS (default off), read each time like SHADOW_ARB."""
    return os.getenv(SHADOW_VARIANTS_ENV, "false").strip().lower() == "true"


def interval_of(doc: Any) -> int:
    """An episode's or pass doc's cadence; missing or malformed = the baseline (10)."""
    v = doc.get("interval") if isinstance(doc, dict) else None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return BASELINE_INTERVAL
    return v


def variant_intervals(cloud: Any) -> list[int]:
    """The extra cadences to run: cloud shadow_intervals minus the baseline, only while SHADOW_VARIANTS is on.
    Malformed values are dropped (the cloud schema validates writes; this guards reads)."""
    if not variants_enabled():
        return []
    raw = (cloud or {}).get("shadow_intervals") if isinstance(cloud, dict) else None
    out = {v for v in (raw if isinstance(raw, list) else [])
           if isinstance(v, int) and not isinstance(v, bool) and v in VARIANT_INTERVALS_ALLOWED}
    return sorted(out - {BASELINE_INTERVAL})[:3]


def summary_intervals(cloud: Any) -> list[int] | None:
    """None while variants are off (the summary carries no variant rows), else the active variant cadences."""
    return variant_intervals(cloud) if variants_enabled() else None


def methodology_of(doc: Any) -> int:
    """An episode's methodology: a stored 2 is 2. Otherwise (no stored value) it is 2 when the bid joined at or
    after METHODOLOGY2_SINCE (the capped code was live) and 1 (legacy) before it. The store fills a missing field
    with 1 on read and the code only ever writes 2, so a 1 (or any other value) is read as "not stored"."""
    v = doc.get("methodology") if isinstance(doc, dict) else None
    if not isinstance(v, bool) and v == METHODOLOGY:
        return METHODOLOGY
    j = ctsvc.to_utc(doc.get("join_ts")) if isinstance(doc, dict) else None
    return METHODOLOGY if j is not None and j >= METHODOLOGY2_SINCE else LEGACY_METHODOLOGY


def pass_ceiling_full(p: dict) -> bool:
    """One baseline pass was ceiling-full: no headroom left, or it refused more new bids than it opened."""
    new, capped = qp._f(p.get("new_eps")) or 0.0, qp._f(p.get("capped_new")) or 0.0
    head = qp._f(p.get("headroom"))
    return (head is not None and head <= 0) or (capped > 0 and capped > new - capped)


def ceiling_status(passes: Iterable[dict]) -> dict:
    """status fields: `ceiling_full` is true when the newest SHADOW_CEILING_FULL_CYCLES baseline passes were all
    ceiling-full (then Shadow is not estimating); `held_cost` and `ceiling` come from the newest pass."""
    ordered = sorted((p for p in passes if _dt(p.get("ts")) is not None), key=lambda p: _dt(p["ts"]), reverse=True)
    last = ordered[:SHADOW_CEILING_FULL_CYCLES]
    full = len(last) >= SHADOW_CEILING_FULL_CYCLES and all(pass_ceiling_full(p) for p in last)
    out: dict[str, Any] = {"ceiling_full": full}
    if full:
        out["held_cost"] = qp._f(last[0].get("held_cost"))
        out["ceiling"] = qp._f(last[0].get("ceiling"))
    return out


def build_summary(episodes: Iterable[dict], passes: Iterable[dict], now: datetime, *, enabled: bool = True,
                  standin: bool = False, intervals: list[int] | None = None) -> dict:
    """The served summary. The headline (and every existing field, `days_covered` and the too-early gate
    included) comes from baseline (interval 10) episodes of methodology 2 only, plus the baseline passes.
    Legacy episodes (methodology 1: stored before the caps, so stacked and uncapped) are summarized apart under
    `legacy` with `legacy_banner`; when there is no corrected episode at all but legacy ones exist, the headline
    says `headline_status: "no_corrected_data"` with null numbers. With variants on, `variants` and
    `variant_compare` (corrected episodes only)."""
    eps = [e for e in (episodes or []) if isinstance(e, dict)]
    plist = [p for p in (passes or []) if isinstance(p, dict)]
    corrected = [e for e in eps if methodology_of(e) == METHODOLOGY]
    legacy = [e for e in eps if methodology_of(e) != METHODOLOGY]
    base = [e for e in corrected if interval_of(e) == BASELINE_INTERVAL]
    bpasses = [p for p in plist if interval_of(p) == BASELINE_INTERVAL]
    out = sm.summarize(base, now, passes=bpasses, enabled=enabled, standin=standin)
    out["methodology"] = METHODOLOGY
    out["status"].update(ceiling_status(bpasses))
    if legacy:
        out["legacy"] = sm.legacy_block(legacy, now)
        out["legacy_banner"] = sm.LEGACY_BANNER
        if not corrected:
            sm.mark_no_corrected_data(out)
    out["variants_enabled"] = intervals is not None
    if intervals is not None:
        out["variants"], out["variant_compare"] = sm.variant_blocks(corrected, now, intervals, passes=plist,
                                                                   interval_of=interval_of)
    return out


class ReadCache:
    """Shared short-lived cache of public reads, so overlapping variant cycles do not repeat them. Values are
    deep-copied in and out; failures are never cached. Thread-safe; bounded."""

    def __init__(self, ttl_s: float = SHADOW_READ_CACHE_S, max_items: int = SHADOW_READ_CACHE_MAX):
        self.ttl_s, self.max_items = float(ttl_s), int(max_items)
        self._lock = threading.Lock()
        self._data: dict[tuple, tuple[float, Any]] = {}
        self.hits = self.misses = 0

    def get(self, key: tuple, fetch) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] > now:
                self.hits += 1
                return copy.deepcopy(hit[1])
        val = fetch()                                    # may raise: nothing is stored
        with self._lock:
            self.misses += 1
            self._data = {k: v for k, v in self._data.items() if v[0] > now}    # expired entries never linger
            if len(self._data) >= self.max_items:
                for k in sorted(self._data, key=lambda k: self._data[k][0])[:len(self._data) - self.max_items + 1]:
                    del self._data[k]                                         # bounded: drop the oldest first
            self._data[key] = (now + self.ttl_s, copy.deepcopy(val))
        return val

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def view(self, md: Any) -> "CachedMarketData":
        return CachedMarketData(md, self)


class CachedMarketData:
    """The same four read methods as ReadOnlyMarketData, answered from a ReadCache in front of it. No other
    attribute: it adds no request, account or order path."""

    __slots__ = ("_md", "_cache")

    def __init__(self, md: Any, cache: ReadCache):
        self._md, self._cache = md, cache

    def markets(self, series: str, status: str, min_close_ts: int | None = None):
        return self._cache.get(("markets", series, status, min_close_ts),
                               lambda: self._md.markets(series, status, min_close_ts))

    def orderbook(self, ticker: str, depth: int | None = None):
        return self._cache.get(("orderbook", ticker, depth), lambda: self._md.orderbook(ticker, depth))

    def market(self, ticker: str):
        return self._cache.get(("market", ticker), lambda: self._md.market(ticker))

    def trades(self, ticker: str, min_ts: int, max_ts: int):
        """Never cached: each episode's trade window is read once, so a cached copy would only cost memory."""
        return self._md.trades(ticker, min_ts, max_ts)


def end_all(open_eps: Iterable[dict], reason: str, now: datetime) -> list[dict]:
    """End every open episode (shadow turned off): end_ts = when it was last observed."""
    out = []
    for ep in open_eps:
        if ep.get("ep_id") and ep.get("end_ts") is None:
            d = dict(ep)
            out.append(_end(d, reason, _dt(d.get("last_seen_ts")) or _dt(d.get("join_ts")) or now))
    return out


def day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

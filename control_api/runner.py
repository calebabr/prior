"""Bot runner: an asyncio background task in the API process that runs maker_strategy passes.

Fail-safe: trades only after reading desired=running from the store. Stop never raises and never depends on
the store, Kalshi or the network for its first two steps. Trading logic lives in kalshi_bot/ and is untouched.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import io
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from collections import deque

from .db import schemas
from .services import arb as arbsvc
from .services import botfiles
from .services import closetime as ctsvc
from .services import fillquality as fq
from .services import pnl as pnlsvc
from .services import queuepos as qp
from .services import riskgate
from .services import alerts as alertsvc
from .services import strategies as strat
from .services import shadow as shadowsvc
from .services import shadow_model as shadow_model
from .services.alerts import Alerter
from .services.webpush import PushService
from .services.logbuffer import install as install_logbuffer
from . import version as ver
from .services import handover as hosvc
from .services.lease import LEADER, LeaseManager, hostname, new_instance_id

log = logging.getLogger("control_api.runner")

from .settings import LIVE_CONFIRM, live_gates_from  # noqa: E402,F401  (re-exported)
JOURNAL_REHYDRATE_DAYS = 35
MONITOR_EVERY = timedelta(days=7)
TIMER_SECONDS = 60
RUN_ONCE_MIN_GAP = timedelta(seconds=60)     # S-22: Run once can't stack passes on the 512 MB instance
FILL_QUALITY_BUDGET_S = 20                   # S-24: total time budget for one fill-quality evaluation
QUEUE_BUDGET_S = 20                          # same pattern for one queue-position evaluation
CLOSE_TIME_BUDGET_S = 20                     # same pattern for one close-time backfill
CLOSE_TIME_INSERT_TIMEOUT_S = 5              # close-time lookups for new episodes (inside the queue budget)
FILLS_SYNC_EVERY = timedelta(minutes=30)
FILLS_FIRST_DAYS = 35
# fill source attribution (UX item Y1; services/strategies.py)
MAKER_OID_MAX = 150_000             # journal maker order ids kept in memory (~35 days of quotes)
ORDER_LOOKUPS_PER_SYNC = 20         # GET /portfolio/orders/{id} for fills no local evidence explains
SOURCE_BACKFILL_MAX = 20_000        # fills relabelled by the startup backfill
SOURCE_BACKFILL_BUDGET_S = 60
PNL_TODAY_TTL_S = 60                # /status today P&L cache
PNL_TODAY_CACHE_MAX = 16            # entries kept, one per (mode, validated time zone); oldest dropped first
RECENT_PASSES = 12
FILL_ALERT_MAX_LINES = 3
# release process (docs/RELEASE_PROCESS.md section 4): lease, fencing, graceful handover
FENCE_MARGIN_S = 5.0                # a pass starts only if the lease outlives PASS_BUDGET_S + this margin
SHUTDOWN_TAIL_S = 10.0              # after the order step: time for the pass's journal/heartbeat writes
SHUTDOWN_STORE_TIMEOUT_S = 5.0
CANCEL_TIMEOUT_S = 25.0             # same bound as Stop's own cancel
# CA-2: Stop's cancel runs on its own pool, never the default executor, so monitor runs, abandoned arb scans or
# anything else using asyncio.to_thread can never queue it behind them. 4 workers: a few Stop taps against a hung
# Kalshi can each hold one for up to CANCEL_TIMEOUT_S and a new tap still gets a thread.
STOP_WORKERS = 4
ARB_SCAN_TIMEOUT_S = 120.0          # an arb scan is abandoned after this (its thread stays on the 1-worker arb pool)
ARB_HELD_READ_MAX = 20000           # executed-opportunity rows read to rebuild the opposite-set gate (more: refuse)
ARB_SENT_PREFIXES = ("FILLED", "PARTIAL", "ERROR")   # executed values whose cost counts toward the day's arb spend
ARB_SKIP_PERSIST_EVERY = timedelta(hours=1)          # a repeated skip (same event + reason kind) is stored once/hour
ARB_SKIP_SEEN_MAX = 2000
# grouped order-error alerts from the strategy's write_errors:/trading_paused lines (one per pass, throttled)
ORDER_ERROR_ALERT_EVERY = timedelta(minutes=30)
PAUSED_ALERT_EVERY = timedelta(hours=1)
SHADOW_RETRY_S = 60                 # a shadow cycle skipped for a transient reason retries after at most this
SHADOW_TRANSIENT = ("standby", "pass_running", "busy", "stop_running", "error")
SHADOW_VARIANT_TICK_S = 30           # 2A-3 wave 2: how often the variant loop checks which cadence is due
# S-25: queue-position, fill-quality and fills-sync Kalshi reads run on their own small pool with a short client,
# never on the default executor that Stop's cancel uses.
TELEMETRY_WORKERS = 2
TELEMETRY_CLIENT_TIMEOUT_S = 10.0
TELEMETRY_CLIENT_MAX_RETRIES = 1
_ENV_KEYS = ("KALSHI_ENV", "MAKER_LIVE", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH",
             "KALSHI_LIVE_CONFIRM", "KALSHI_BASE_URL", "ARB_LIVE")   # the runner never uses env ARB_LIVE
_ENV_LOCK = threading.Lock()      # os.environ is process-global: one pass/monitor/factory at a time


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _state_stamp(st: dict | None) -> tuple:
    """What identifies a stored state doc version. updated_utc alone can tie (ms precision, two writes in one
    millisecond), which would hide a Stop written by another instance; the control fields cannot."""
    st = st or {}
    return tuple(st.get(k) for k in ("updated_utc", "updated_by", "desired", "mode", "arb_desired", "arb_mode"))


def _where(e: BaseException) -> str:
    """Exception type plus the innermost file:line, never the message (messages can echo secrets)."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return type(e).__name__
    return f"{type(e).__name__} at {os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"


def _env(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def normalize_pem(text: str | None) -> str | None:
    """Rebuild a PEM private key whose line breaks were lost or turned into spaces or a literal backslash-n when it was
    pasted into a one-line dashboard field, and add the BEGIN/END lines when only the base64 body was pasted. Anything
    else is returned unchanged."""
    if not text:
        return text
    t = text.replace("\n", "\n").strip()
    m = re.fullmatch(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", t, re.S)
    if m:
        label, raw = m.group(1), m.group(2)
        body = re.sub(r"\s+", "", raw)
        return _wrap_pem(label, body)
    body = re.sub(r"\s+", "", t)
    if re.fullmatch(r"[A-Za-z0-9+/=]{200,}", body):          # a bare base64 body: find the label that loads
        from cryptography.hazmat.primitives import serialization
        for label in ("PRIVATE KEY", "RSA PRIVATE KEY"):
            pem = _wrap_pem(label, body)
            try:
                serialization.load_pem_private_key(pem.encode(), password=None)
                return pem
            except ValueError:
                continue
    return t


def _wrap_pem(label: str, body: str) -> str:
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----"]) + "\n"


def mode_keys(mode: str) -> tuple[str | None, str | None]:
    p = "KALSHI_PROD_" if mode == "live" else "KALSHI_DEMO_"
    return _env(p + "API_KEY_ID"), normalize_pem(_env(p + "PRIVATE_KEY"))


_SAVED_ENV: dict | None = None    # the owner's real env while a pass temporarily overrides it


def live_block_reason(settings: Any) -> str | None:
    """Why LIVE (maker or arb) is refused, or None if allowed. Fail-closed:
    * "strategy_not_installed": kalshi_bot is the public stand-in (IS_STANDIN), i.e. the private strategy was not
      overlaid at build time. The stand-in can never trade real money, whatever the env says.
    * "live_not_allowed": env gates 2+3 (prod keys + KALSHI_LIVE_CONFIRM), from the startup snapshot in Settings."""
    if botfiles.is_standin():
        return "strategy_not_installed"
    if not (getattr(settings, "prod_keys_present", False) and getattr(settings, "live_confirm_ok", False)):
        return "live_not_allowed"
    return None


def live_allowed_settings(settings: Any) -> bool:
    """All live gates (strategy installed + env gates 2+3 from the startup snapshot). Fail-closed."""
    return live_block_reason(settings) is None


@contextlib.contextmanager
def kalshi_env(mode: str):
    """Set the bot's env for one pass (under the global env lock); restore afterwards."""
    global _SAVED_ENV
    kid, pk = mode_keys(mode)
    with _ENV_LOCK:
        saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        _SAVED_ENV = saved
        try:
            for k in _ENV_KEYS:
                os.environ.pop(k, None)
            os.environ["KALSHI_ENV"] = "prod" if mode == "live" else "demo"
            os.environ["MAKER_LIVE"] = "true"
            if kid:
                os.environ["KALSHI_API_KEY_ID"] = kid
            if pk:
                os.environ["KALSHI_PRIVATE_KEY"] = pk
            # demo = demo exchange, real orders with play money; live needs the owner's env confirmation
            confirm = LIVE_CONFIRM if mode == "demo" else ((saved.get("KALSHI_LIVE_CONFIRM") or "").strip())
            if confirm:
                os.environ["KALSHI_LIVE_CONFIRM"] = confirm
            yield
        finally:
            # S-19: restore the owner's env FIRST, then clear the marker (still inside the lock)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            _SAVED_ENV = None


def default_kalshi_factory(mode: str):
    """Authenticated KalshiClient for the mode from env keys, or None if keys are missing."""
    kid, pk = mode_keys(mode)
    if not (kid and pk):
        return None
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(pk.replace("\\n", "\n").encode(), password=None)
    # built directly (no env lock), so Stop's cancel never waits for a running pass
    return KalshiClient(BASE_URLS["prod" if mode == "live" else "demo"], key_id=kid, private_key=key, max_retries=2)


def default_public_factory(env: str):
    """Unauthenticated KalshiClient for public market data (arb scans). env: "demo" | "live"."""
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    return KalshiClient(BASE_URLS["prod" if env == "live" else "demo"], max_retries=2)


def closetime_public_factory(env: str):
    """S-31: unauthenticated client for close-time lookups only: no retries and a short timeout, so a hung GET frees
    its (dedicated) worker quickly. The trading and arb clients keep their own settings."""
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    return KalshiClient(BASE_URLS["prod" if env == "live" else "demo"], max_retries=ctsvc.CLIENT_MAX_RETRIES,
                        timeout=ctsvc.CLIENT_TIMEOUT_S)


def telemetry_kalshi_factory(mode: str):
    """S-25: authenticated client for read-only telemetry (fills sync, order lookups, queue-position account
    reads): one retry and a short timeout, so an abandoned worker on the telemetry pool ends quickly."""
    kid, pk = mode_keys(mode)
    if not (kid and pk):
        return None
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(pk.replace("\\n", "\n").encode(), password=None)
    return KalshiClient(BASE_URLS["prod" if mode == "live" else "demo"], key_id=kid, private_key=key,
                        max_retries=TELEMETRY_CLIENT_MAX_RETRIES, timeout=TELEMETRY_CLIENT_TIMEOUT_S)


def telemetry_public_factory(env: str):
    """S-25: unauthenticated short client for fill-quality trades and queue-position books."""
    botfiles.ensure_bot_path()
    from config import BASE_URLS
    from kalshi_client import KalshiClient
    return KalshiClient(BASE_URLS["prod" if env == "live" else "demo"], max_retries=TELEMETRY_CLIENT_MAX_RETRIES,
                        timeout=TELEMETRY_CLIENT_TIMEOUT_S)


_WRITE_ERRORS_RE = re.compile(r"^write_errors:\s*cancel=(\d{1,6})\s+place=(\d{1,6})\s+paused=([01])\s*$", re.M)
_SERIES_CAPPED_RE = re.compile(r"^series_capped:\s*([A-Za-z0-9_.\-]{1,40})\s+exposure=(\S{1,20})\s+cap=(\S{1,20})\s*$",
                               re.M)
_PAUSED_RE = re.compile(r"^trading_paused\b", re.M)
_CAP_SKIPPED_RE = re.compile(r"^series_cap_skipped:", re.M)


def _float_or_none(s: str) -> float | None:
    try:
        f = float(s.lstrip("$"))
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def parse_write_report(out: str | None) -> dict:
    """The strategy's write-path summary from one pass's captured stdout: the LAST `write_errors:` line (cancel and
    place failure counts, paused flag), every `series_capped:` line, a `trading_paused` line and a
    `series_cap_skipped:` line. Missing lines mean zero / none. Pure; never raises."""
    rep = {"cancel": 0, "place": 0, "paused": False, "series_capped": [], "series_cap_skipped": False}
    if not isinstance(out, str) or not out:
        return rep
    m = None
    for m in _WRITE_ERRORS_RE.finditer(out):
        pass
    if m is not None:
        rep["cancel"], rep["place"] = int(m.group(1)), int(m.group(2))
        rep["paused"] = m.group(3) == "1"
    if _PAUSED_RE.search(out):
        rep["paused"] = True
    seen = set()
    for s in _SERIES_CAPPED_RE.finditer(out):
        if s.group(1) in seen or len(seen) >= 50:
            continue
        seen.add(s.group(1))
        rep["series_capped"].append({"series": s.group(1), "exposure": _float_or_none(s.group(2)),
                                     "cap": _float_or_none(s.group(3))})
    rep["series_cap_skipped"] = bool(_CAP_SKIPPED_RE.search(out))
    return rep


class FenceBlocked(RuntimeError):
    """The lease is not confirmed (standby, expired, lost or unknown): nothing may be sent to Kalshi."""


class StopInProgress(RuntimeError):
    """A Stop is running, or landed while a Start/arb-enable was in flight: Stop wins and the start is refused."""


class RunnerDisabled(RuntimeError):
    """RUNNER_ENABLED=false: nothing can start trading (S-10)."""


class ConfigInvalid(RuntimeError):
    """Start refused: the stored config still fails validation (S-12). str() is the plain-English reason."""


INVALID_CONFIG_PREFIXES = ("stored config failed validation", "monitor wrote an invalid config")


class ArbCompletionGate:
    """H3-B: the runner's re-checks for arb_scanner.execute's ONE-shot completion of a partial DEMO-EXECUTE set.
    approve(extra_cost) -> None (go) | reason: once only; the demo-execute re-checks (stop generation, not stopping,
    not shutting down, leader + R1 fence, kill switch, maker and arb in demo, the setting still on), the demo
    exchange asserted on the client, then the account risk gate on the extra spend with fresh balance and positions,
    re-checked under the budget lock and reserved before any order (counted on top of the set's reservation).
    ok() -> bool: the same re-checks, called by the scanner right before each completion order. Never raises.
    Never built for live: the runner passes it only for kind "demo-execute"."""

    def __init__(self, runner: "Runner", client: Any, event: str, gen: int, budget: dict, frac: float) -> None:
        self.runner, self.client, self.event, self.gen = runner, client, event, gen
        self.budget, self.frac = budget, frac
        self.used = False

    def ok(self) -> bool:
        r = self.runner
        try:
            return bool(not (self.budget or {}).get("abandoned") and r._arb_demo_ok(self.gen)
                        and r._arb_complete_wanted() and r.mode == "demo" and arbsvc.demo_base_ok(self.client))
        except Exception:  # noqa: BLE001
            return False

    def approve(self, extra_cost: Any) -> str | None:
        if self.used:
            return "already attempted"
        self.used = True
        if not self.ok():
            return "stopped"
        try:
            extra = float(extra_cost)
        except (TypeError, ValueError):
            return "bad input"
        try:
            cash = float((self.client.get_balance() or {}).get("balance") or 0) / 100.0
        except Exception as e:  # noqa: BLE001
            return f"balance {type(e).__name__}"
        try:
            body = self.client.get_positions() if callable(getattr(self.client, "get_positions", None)) else None
            truncated = isinstance(body, dict) and body.get("truncated") is True
            by_series = riskgate.exposure_by_series((body or {}).get("market_positions")
                                                    if isinstance(body, dict) else None)
        except Exception:  # noqa: BLE001
            return "gate positions unreadable"
        if truncated:
            return "gate positions truncated"
        r = self.runner
        day = (self.budget or {}).get("day")
        if day is None:
            return "gate day spend unknown"
        with r._arb_budget_lock:
            ok, why = riskgate.check_arb(extra, riskgate.series_of(self.event), by_series, cash, self.frac,
                                         self.budget["scan"], day["amount"], arbsvc.gate_caps(r.arb_settings()))
            if not ok:
                return f"gate {why}"
            if not self.ok():
                return "stopped"
            self.budget["scan"] += extra
            day["amount"] = round(day["amount"] + extra, 6)
        return None


def _scanner_can_complete(scanner: Any) -> bool:
    """The installed arb_scanner.execute takes `complete` (an older overlay without it never completes)."""
    try:
        return "complete" in inspect.signature(scanner.execute).parameters
    except (TypeError, ValueError, AttributeError):
        return False


class Runner:
    def __init__(self, settings: Any, store: Any, kalshi_factory: Callable[[str], Any] | None = None,
                 alerter: Alerter | None = None, public_factory: Callable[[str], Any] | None = None,
                 shadow_factory: Callable[[], Any] | None = None):
        self.settings = settings
        self.store = store
        self.kalshi_factory = kalshi_factory or default_kalshi_factory
        self.public_factory = public_factory or default_public_factory
        # shadow mode (services/shadow.py): its own read-only, credential-free market-data factory. It never gets the
        # trading factory above, the pass lock, the env lock, the fence or the Stop pool.
        self.shadow_factory = shadow_factory or shadowsvc.default_shadow_factory
        self.alerter = alerter or Alerter(settings)
        self.logs = install_logbuffer()
        self.desired = "stopped"
        self.mode = "demo"
        self.store_ok = False
        self.config: dict = {}
        self.cloud: dict = dict(schemas.DEFAULT_CLOUD_SETTINGS)
        self.running_pass = False
        self.last_pass_started_utc: datetime | None = None
        self.fill_quality_cache: dict | None = None   # S-23: {mode, overall, by_series}; served by /status, /markets
        self._fq_task: asyncio.Task | None = None
        self.post_pass_hook = None     # optional zero-argument callable run after a pass (read-only warm-ups); never raises
        # queue-position telemetry (read-only): journal quote/cancel rows waiting for the next evaluation,
        # order ids cancelled by Stop, and the cached aggregate (same S-23 pattern as fill quality)
        self.queue_stats_cache: dict | None = None
        self._qp_task: asyncio.Task | None = None
        self._qp_rows: deque = deque(maxlen=5000)          # (mode, row)
        self._stop_cancelled: deque = deque(maxlen=2000)   # order ids; appended from Stop's cancel thread
        self.last_stop_utc: datetime | None = None
        self._qp_day: str | None = None
        self._qp_day_count = 0
        # market close time (read-only; services/closetime.py): LRU+TTL cache and the single-flight backfill task
        self.close_times = ctsvc.CloseTimeCache()
        self._ct_task: asyncio.Task | None = None
        # S-31: close-time GETs run on their own small pool, never on the default executor (Stop's cancel uses that).
        # Threads start only on first use; shut down in shutdown().
        self._ct_executor = ThreadPoolExecutor(max_workers=ctsvc.EXECUTOR_WORKERS, thread_name_prefix="closetime")
        self.last_run_utc: datetime | None = None
        self._silence_ref: datetime | None = None   # when the watchdog first saw the bot running (grace for the first pass)
        self.last_error: str | None = None
        self.last_monitor_utc: datetime | None = None
        self.keepalive_state: dict = {"last_ping_utc": None, "ok": None, "error": None}
        self.last_inbound_utc: datetime | None = None
        self._pass_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self._state_retry: asyncio.Task | None = None
        self._stop_pending = False        # S-3: a Stop whose desired=stopped is not yet in the store
        self._silent_alerted = False
        self._tail: botfiles.JournalTail | None = None
        self._live_downgraded: list[str] = []
        # Start/Stop race: Stop bumps _stop_gen synchronously as its very first statement and holds
        # _stopping > 0 while it runs. Start (and arb enable) refuse if either is set or changed across their awaits.
        self._stop_gen = 0
        self._stopping = 0
        # fills / P&L sync
        self.last_fills_sync_utc: datetime | None = None
        self.fills_sync_error: str | None = None
        # bracket arbitrage (on/off + live live in the store's state doc; numeric settings in config["arb"])
        self.arb_desired = "stopped"
        self.arb_mode = "demo"           # demo = log-only; live = execute on prod (needs the env gates)
        self.arb_lock = asyncio.Lock()
        self.arb_running_scan = False
        self.arb_last_scan_utc: datetime | None = None
        self.arb_last_error: str | None = None
        self.arb_scans = 0
        self.arb_found = 0
        self.started_utc = utcnow()          # when the in-memory counters (arb_scans / arb_found) began counting
        self.arb_recent: deque = deque(maxlen=200)   # in-memory copy, used if the store is unreachable
        # risk gate: today's arb spend per kind ("live" / "demo-execute"): {date, amount, mark}; see _arb_day_state
        self._arb_day: dict[str, dict] = {}
        # opposite-set gate: per kind, {date, mark, events: {event: {"YES"/"NO"}}} of executed sets; see _arb_held_state
        self._arb_held: dict[str, dict] = {}
        self._arb_budget_lock = threading.Lock()
        self._arb_skip_seen: dict[tuple[str, str], datetime] = {}   # (event, reason kind) -> last stored skip
        # strategy write-path results of the last pass (write_errors: / series_capped: / trading_paused lines)
        self.last_write_report: dict | None = None
        self._order_alert_utc: datetime | None = None
        self._order_alert_suppressed = 0
        self._paused_alert_utc: datetime | None = None
        # UX sprint: fill sources, today P&L cache, alert history + web push, daily summary
        self._maker_oids: dict[str, None] = {}       # journal maker order ids (insertion ordered, bounded)
        self.source_backfill: dict | None = None
        self._pnl_today: dict[tuple[str, str], tuple[float, dict, str]] = {}   # (env, tz) -> (monotonic t, value, since)
        self.push = PushService(settings, store)
        self._daily_sent: str | None = None          # local date of the last daily summary
        # S-25: telemetry Kalshi reads on their own pool (threads start on first use; shut down in shutdown())
        self._tel_executor = ThreadPoolExecutor(max_workers=TELEMETRY_WORKERS, thread_name_prefix="telemetry")
        # CA-2: dedicated pools (threads start on first use). Stop's cancel only on _stop_executor; the monitor and
        # the arb scan each get one worker, so neither can pile threads onto the default executor.
        self._stop_executor = ThreadPoolExecutor(max_workers=STOP_WORKERS, thread_name_prefix="stop")
        self._monitor_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor")
        self._arb_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arb")
        self._shadow_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow")
        self._shadow_task: asyncio.Task | None = None
        self._shadow_cf: Any = None           # the worker's concurrent future (busy while not done)
        self._shadow_active = False           # a cycle ran since shadow was last turned on
        self._shadow_summary_utc: datetime | None = None   # last cached-summary refresh (SH-2)
        self.shadow_state: dict = {"last_cycle_utc": None, "last_reason": None, "last_error": None}
        # 2A-3 wave 2 interval variants (env SHADOW_VARIANTS): one shadow cycle at a time (never a trading lock),
        # a shared short read cache, the variant loop, and per-cadence state (last duration feeds the load guard)
        self._shadow_cycle_lock = asyncio.Lock()
        self._shadow_cache = shadowsvc.ReadCache()
        self._shadow_variant_task: asyncio.Task | None = None
        self._shadow_last_duration_s: float | None = None
        self.shadow_variant_state: dict = {}
        self._monitor_running = False     # CA-2: single-flight; a second run while one runs gets monitor_running
        # ---- release process: single-leader lease + fencing (R1), graceful handover (R2/R3), schema (R4)
        g = lambda n, d: getattr(settings, n, d)  # noqa: E731   (tests may pass a minimal settings object)
        self.instance_id = new_instance_id()
        self.pass_budget_s = float(g("pass_budget_s", 25.0))
        self.handover_max_age_s = float(g("handover_max_age_s", 300.0))
        self.lease = LeaseManager(store, instance_id=self.instance_id, version=ver.API_VERSION,
                                  ttl_s=float(g("lease_ttl_s", 60.0)), renew_s=float(g("lease_renew_s", 20.0)))
        self._leader_ready = False        # leader AND the leadership rules (S-3/S-21/handover) have been applied
        self._ready_mark: tuple | None = None   # (generation, transitions) the rules were applied for
        self._schema_ok: bool | None = None     # None unknown (re-checked), False: stored schema is newer
        self._schema_alerted = False
        self._shutting_down = False
        self._strategy_running = False    # the order step (maker_strategy.main in its thread) is executing
        self._state_seen_utc: Any = None  # updated_utc of the stored state last applied (remote-Stop detection)
        self._start_epoch = 0             # bumped by every local Start (state sync ignores reads that raced it)
        self._stop_persisted = False      # a Stop in THIS process reached the store
        self._pre_acquire_lease: dict | None = None   # lease doc read just before acquiring (handover token)
        self.handover_result: dict | None = None      # {"kept_live": bool, "reason": str, "utc": str}
        self.fence_blocked = 0
        if isinstance(self.alerter, Alerter):
            self.alerter.on_alert = self._on_alert

    # ---- paths (read from the bot module each time, so tests can monkeypatch them)
    @property
    def ms(self):
        return botfiles.strategy()

    @property
    def kill_path(self) -> Path:
        return Path(self.ms.KILL)

    @property
    def config_path(self) -> Path:
        return Path(self.ms.CONFIG_PATH)

    @property
    def journal_path(self) -> Path:
        return Path(self.ms.LOG)

    @property
    def arb_kill_path(self) -> Path:
        return Path(arbsvc.scanner().KILL_PATH)

    def arb_kill_switch(self) -> bool:
        try:
            return self.arb_kill_path.exists()
        except OSError:
            return True

    def arb_settings(self) -> dict:
        return arbsvc.current(self.config)

    def arb_state(self) -> str:
        if self.arb_desired != "running" or self.arb_kill_switch():
            return "stopped"
        return "live" if self.arb_mode == "live" and self.live_allowed() else "log-only"

    def kill_switch(self) -> bool:
        try:
            return self.kill_path.exists()
        except OSError:
            return True

    def poll_minutes(self) -> int:
        try:
            p = int(self.config.get("poll_minutes") or self.ms.DEFAULTS["poll_minutes"])
        except (TypeError, ValueError):
            p = 10
        return max(2, min(p, int(self.settings.max_poll_minutes)))

    def next_due_utc(self) -> datetime | None:
        if self.last_run_utc is None:
            return utcnow() if self.desired == "running" else None
        return self.last_run_utc + timedelta(minutes=self.poll_minutes())

    def live_allowed(self) -> bool:
        """Live gates: real strategy installed + env gates from the startup snapshot (S-19), never os.environ."""
        return live_allowed_settings(self.settings)

    def live_block_reason(self) -> str | None:
        return live_block_reason(self.settings)

    # ---- release process: lease + fencing (R1)
    def _fence_ok(self, *, min_remaining_s: float = 0.0) -> bool:
        """May this instance send orders right now? Leader, leadership rules applied, schema not newer than ours,
        and the lease renewed within the TTL (local monotonic clock) with `min_remaining_s` to spare. Pure local
        reads: safe from worker threads. Stop and the persisted stop never call this."""
        return (self._leader_ready and self._schema_ok is True and self.lease.role == LEADER
                and self.lease.fence_ok(min_remaining_s=min_remaining_s))

    def role(self) -> str:
        return "leader" if self._leader_ready and self.lease.role == LEADER else "standby"

    def lease_status(self) -> dict:
        snap = self.lease.snapshot()
        snap["role"] = self.role()
        return snap

    async def _journal_fence_blocked(self, what: str) -> None:
        """Record a refused order step (journal row in the store; never the CSV the strategy writes). Never raises."""
        self.fence_blocked += 1
        log.warning("fence_blocked: %s (role=%s, generation=%s)", what, self.lease.role, self.lease.generation)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.store.add_journal_rows([{
                "utc": utcnow().isoformat(), "event": "fence_blocked", "ticker": "", "side": "", "price": "",
                "count": "", "cost": "", "cell": "", "note": f"{what}; instance {self.instance_id[:8]}"}]), 5)

    async def _check_schema(self) -> None:
        """R4: a stored schema version newer than ours -> standby forever (alert once); older/missing -> ours.
        A store error leaves it unknown (no trading) and it is re-checked on the next lease tick."""
        try:
            stored = await asyncio.wait_for(self.store.get_schema_version(), 5)
            if stored is not None and int(stored) > ver.SCHEMA_VERSION:
                self._schema_ok = False
                if not self._schema_alerted:
                    self._schema_alerted = True
                    msg = (f"Stored data schema {int(stored)} is newer than this release's {ver.SCHEMA_VERSION}: "
                           "this instance stays standby (no trading). Deploy the newer release.")
                    log.error(msg)
                    with contextlib.suppress(Exception):
                        await self.alerter.send("Bot standby: newer data schema", msg, priority="urgent")
                return
            if stored is None or int(stored) < ver.SCHEMA_VERSION:
                await asyncio.wait_for(self.store.set_schema_version(ver.SCHEMA_VERSION), 5)
            self._schema_ok = True
        except Exception as e:  # noqa: BLE001
            self._schema_ok = None if self._schema_ok is not False else False
            log.warning("schema version check failed: %s", type(e).__name__)

    async def _lease_tick(self, *, rehydrate_state: dict | None = None, config_ok: bool = True,
                          stop_gen: int | None = None, stop_persisted: bool | None = None) -> None:
        """One lease iteration: (re)check the schema, renew or acquire, apply the leadership rules on a new
        leadership, adopt remote Stops as leader, mirror the store as standby. Never raises."""
        try:
            if self._shutting_down or not self.settings.runner_enabled:
                return
            if self._schema_ok is not True:
                await self._check_schema()
                if self._schema_ok is not True:
                    self._leader_ready = False
                    return
            if self.lease.role != LEADER:
                try:
                    self._pre_acquire_lease = await self.lease.read()
                except Exception:  # noqa: BLE001
                    self._pre_acquire_lease = None
            was_ready = self._leader_ready
            leader = await self.lease.step()
            mark = (self.lease.generation, self.lease.transitions)
            if leader and (not self._leader_ready or self._ready_mark != mark):
                self._leader_ready = False
                await self._become_leader(st=rehydrate_state, config_ok=config_ok,
                                          reload_config=rehydrate_state is None, stop_gen=stop_gen,
                                          stop_persisted=stop_persisted)
            elif not leader and was_ready:
                self._leader_ready = False
                log.warning("now standby: maker passes, arbitrage and non-Stop cancels are fenced off")
                with contextlib.suppress(Exception):
                    self._spawn(self.alerter.send("Bot is standby", "This instance lost the trading lease; "
                                                  "another instance (or nobody) trades now.", priority="high"))
            if self._leader_ready:
                if rehydrate_state is None:
                    await self._sync_state_from_store()
            elif rehydrate_state is None:
                await self._mirror_state()
        except Exception as e:  # noqa: BLE001
            log.error("lease tick failed: %s", type(e).__name__)

    async def _lease_loop(self) -> None:
        while True:
            await asyncio.sleep(self.lease.renew_s)
            await self._lease_tick()

    def _apply_local_stop_rules(self, desired: str, arb_desired: str, *, gen: int,
                                persisted_before: bool) -> tuple[str, str]:
        """Stop beats the lease: a Stop in this process (now or during our awaits), a pending Stop write, or a kill
        switch on disk keeps the bot stopped (S-3). Exception: our OWN Stop reached the store and the store now
        says running, so a later Start (2FA, on any instance) superseded it; the stale local file is removed. That
        exception needs the Stop to have been persisted BEFORE the state was read (`persisted_before`), else the
        read may simply predate the Stop's write."""
        if gen != self._stop_gen or self._stopping or self._stop_pending:
            return "stopped", "stopped"
        if desired == "running" and self.kill_switch():
            if persisted_before and self._stop_persisted:
                with contextlib.suppress(OSError):
                    self.kill_path.unlink()
            else:
                log.warning("store says running but the kill switch is present: staying stopped")
                self._stop_pending = True
                self._ensure_stop_retry("rehydrate")
                desired = "stopped"
        if arb_desired == "running" and self.arb_kill_switch():
            arb_desired = "stopped"
        return desired, arb_desired

    async def _become_leader(self, *, st: dict | None = None, config_ok: bool = True,
                             reload_config: bool = True, stop_gen: int | None = None,
                             stop_persisted: bool | None = None) -> None:
        """Apply the restart rules once per new leadership, then open the fence. Re-reads the store (the state may
        have changed while we were standby), reloads the config, keeps LIVE only with a valid signed handover (A2),
        otherwise drops LIVE to demo (S-21). On a store error the fence stays shut and the next tick retries."""
        # the Stop generation from BEFORE the state was read: a Stop after that read always wins below
        gen = self._stop_gen if st is None or stop_gen is None else stop_gen
        persisted_before = self._stop_persisted if st is None or stop_persisted is None else stop_persisted
        lease_mark = (self.lease.generation, self.lease.transitions)
        try:
            if st is None:
                st = await asyncio.wait_for(self.store.get_state(), 5)
            try:
                post = await self.lease.read()
            except Exception:  # noqa: BLE001
                post = None
        except Exception as e:  # noqa: BLE001
            log.error("leadership: state read failed: %s (fence stays shut)", type(e).__name__)
            return
        st = dict(st or schemas.DEFAULT_STATE)
        if reload_config:
            config_ok = await self._load_config()
        desired = st.get("desired", "stopped") if config_ok else "stopped"
        arb_desired = st.get("arb_desired", "stopped") if config_ok else "stopped"
        desired, arb_desired = self._apply_local_stop_rules(desired, arb_desired, gen=gen,
                                                            persisted_before=persisted_before)
        mode, arb_mode = st.get("mode", "demo"), st.get("arb_mode", "demo")
        reason = None
        kept_live = False
        prev_holder = (self._pre_acquire_lease or {}).get("holder")
        took_over = bool(prev_holder) and prev_holder != self.instance_id    # another process held it before
        if mode == "live":
            token = ((self._pre_acquire_lease or {}).get("handover") or (post or {}).get("handover"))
            reason = await self._check_handover(token, st, desired)
            kept_live = reason is None
            self.handover_result = {"kept_live": kept_live, "reason": reason or "valid", "utc": iso(utcnow())}
            log.warning("signed handover: %s", "LIVE kept" if kept_live else f"refused ({reason}): demo")
        self._pre_acquire_lease = None
        self._live_downgraded = [n for n, m in (("maker", "demo" if kept_live else mode), ("arbitrage", arb_mode))
                                 if m == "live"]
        # S-54: `seen` stays the updated_utc of OUR READ. Our own writes below change the doc, so the next sync
        # (lease tick, or _pre_order_fence before the first pass) re-reads it and adopts a Stop that another instance
        # wrote between our read and our write.
        seen = _state_stamp(st)
        if self._live_downgraded:
            log.warning("live state in store not restored after restart: %s now demo", self._live_downgraded)
            kw = {"mode": "demo"} if "maker" in self._live_downgraded else {}
            if "arbitrage" in self._live_downgraded:
                kw["arb_mode"] = "demo"
            try:
                await asyncio.wait_for(self.store.set_state(by="rehydrate", **kw), timeout=5)
            except Exception as e:  # noqa: BLE001   (memory says demo below; fail-safe either way)
                log.error("demo mode not persisted at startup: %s", type(e).__name__)
        if st.get("arb_desired") == "running" and arb_desired == "stopped" and config_ok:
            try:
                await asyncio.wait_for(self.store.set_state(by="rehydrate", arb_desired="stopped"), timeout=5)
            except Exception as e:  # noqa: BLE001
                log.error("arb stopped not persisted at startup: %s", type(e).__name__)
        # a Stop that landed during the awaits above wins; no await from here to the fence opening
        desired, arb_desired = self._apply_local_stop_rules(desired, arb_desired, gen=gen,
                                                            persisted_before=persisted_before)
        if (self.lease.generation, self.lease.transitions) != lease_mark or self.lease.role != LEADER:
            return                                    # lost the lease meanwhile: next tick starts again
        self.desired, self.arb_desired = desired, arb_desired
        self.mode = "live" if kept_live else "demo"
        self.arb_mode = "demo"
        self._state_seen_utc = seen
        self._leader_ready = True
        self._ready_mark = lease_mark
        log.warning("leader (generation %s): desired=%s mode=%s", self.lease.generation, self.desired, self.mode)
        if self.desired == "stopped" and took_over:
            # S-53 (b): the previous holder may have left orders resting after a Stop it never saw (SIGKILL, an
            # overrun, or a Stop that landed elsewhere mid-pass). Best effort, in the background, stored mode.
            # Only on a takeover from another process: a first-ever lease has no predecessor to clean up after.
            stored_mode = st.get("mode") if st.get("mode") in ("demo", "live") else self.mode
            self._spawn(self._best_effort_cancel(stored_mode, "leader started stopped"))
        await self._alert_resumed_state(st, kept_live=kept_live)

    async def _best_effort_cancel(self, mode: str, why: str) -> int:
        """Cancel resting mm- orders (the same call Stop makes; no fence: it only removes risk). Never raises."""
        try:
            n = await asyncio.wait_for(self._cancel_in_stop_pool(mode), timeout=CANCEL_TIMEOUT_S)
            if n:
                log.warning("%s: cancelled %s resting mm- orders (%s)", why, n, mode)
            return n
        except Exception as e:  # noqa: BLE001
            log.error("%s: cancel failed: %s", why, type(e).__name__)
            return 0

    async def _check_handover(self, token: Any, st: dict, desired: str) -> str | None:
        """None if the signed handover lets us keep LIVE (consumes its nonce), else the refusal reason."""
        if not token:
            return "no_token"
        reason = hosvc.verify(hosvc.key_bytes(self.settings), token,
                              config_hash=hosvc.config_hash(botfiles.public_config(self.config)),
                              stop_seq=hosvc.state_fingerprint(st), now=utcnow(), max_age_s=self.handover_max_age_s)
        if reason:
            return reason
        if desired != "running" or self._stop_gen != 0 or self._stop_pending or self.kill_switch():
            return "stop_since"
        if not self.live_allowed():
            return self.live_block_reason() or "live_not_allowed"
        if not all(mode_keys("live")):
            return "no_prod_keys"
        try:
            fresh = await asyncio.wait_for(self.store.consume_handover(str(token.get("nonce"))), 5)
        except Exception as e:  # noqa: BLE001
            return f"consume_failed:{type(e).__name__}"
        return None if fresh is True else "replayed"

    async def _sync_state_from_store(self) -> None:
        """Leader: adopt a Stop written to the store by another instance (a standby got the Stop request). Only a
        state doc that CHANGED since we last applied it counts. Never raises."""
        gen, ep = self._stop_gen, self._start_epoch
        try:
            st = await asyncio.wait_for(self.store.get_state(), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("state sync failed: %s", type(e).__name__)
            return
        if not st or gen != self._stop_gen or ep != self._start_epoch or self._stopping:
            return
        u = _state_stamp(st)
        if u == self._state_seen_utc:
            return
        self._state_seen_utc = u
        if st.get("desired") == "stopped" and self.desired == "running":
            log.warning("persisted Stop found in the store (by %s): stopping here too",
                        str(st.get("updated_by") or "?")[:64])
            await self.stop(by="persisted-stop")
        elif st.get("arb_desired") == "stopped" and self.arb_desired == "running":
            self.arb_desired, self.arb_mode = "stopped", "demo"

    async def _mirror_state(self) -> None:
        """Standby: show what the store says (never trades). Local Stops still win. Never raises."""
        gen, ep, persisted = self._stop_gen, self._start_epoch, self._stop_persisted
        try:
            st = await asyncio.wait_for(self.store.get_state(), 5)
        except Exception:  # noqa: BLE001
            return
        if not st or gen != self._stop_gen or ep != self._start_epoch or self._stopping:
            return
        desired, arb_desired = st.get("desired", "stopped"), st.get("arb_desired", "stopped")
        if self._stop_pending or (self.kill_switch() and not (persisted and self._stop_persisted)):
            desired = "stopped"
        self.desired, self.arb_desired = desired, arb_desired
        self.mode, self.arb_mode = st.get("mode", "demo"), st.get("arb_mode", "demo")
        self._state_seen_utc = _state_stamp(st)

    # ---- lifecycle
    async def start(self) -> None:
        await self.rehydrate()
        if not self.settings.runner_enabled:
            # S-10: RUNNER_ENABLED=false is the out-of-band emergency stop. Run Stop once (kill switches, cancel
            # resting mm- orders, persist stopped; never raises), then nothing trades until the env is changed.
            res = await self.stop(by="env:RUNNER_ENABLED=false")
            log.warning("runner disabled by env: stop ran at startup (errors=%s)", res.get("errors"))
        if self.settings.runner_enabled:
            from .services.keepalive import keepalive_loop
            self._tasks = [asyncio.create_task(self._timer_loop(), name="runner-timer"),
                           asyncio.create_task(self._watchdog_loop(), name="runner-watchdog"),
                           asyncio.create_task(keepalive_loop(self), name="runner-keepalive"),
                           asyncio.create_task(self._arb_loop(), name="runner-arb"),
                           asyncio.create_task(self._lease_loop(), name="runner-lease")]
            if getattr(self.settings, "shadow_enabled", False):
                # kept out of self._tasks: shutdown cancels it without waiting. It idles while the cloud setting
                # shadow_enabled is false, so the app can turn it on and off without a restart.
                self._shadow_task = asyncio.create_task(self._shadow_loop(), name="runner-shadow")
                if shadowsvc.variants_enabled():         # 2A-3 wave 2: off by default (env SHADOW_VARIANTS)
                    self._shadow_variant_task = asyncio.create_task(self._shadow_variant_loop(),
                                                                    name="runner-shadow-variants")
            self._spawn(self.run_source_backfill())     # read-only relabel of older fills; budgeted, never raises
        log.info("runner started: instance %s on %s, role %s", self.instance_id[:8], hostname(), self.role())

    def shutdown_budget_s(self) -> float:
        """The whole graceful shutdown must end well inside Render's shutdown delay (120 s)."""
        delay = float(getattr(self.settings, "shutdown_delay_s", 120.0))
        return max(5.0, min(delay - 30.0, self.pass_budget_s + SHUTDOWN_TAIL_S + CANCEL_TIMEOUT_S
                            + 5 * SHUTDOWN_STORE_TIMEOUT_S))

    async def shutdown(self) -> None:
        """R2 graceful shutdown (SIGTERM -> lifespan): stop scheduling, let a running order step finish (hard
        PASS_BUDGET_S), persist, then hand over and release the lease if still leader. Never raises. A pass that
        overran its budget keeps the lease (it expires after the TTL), so no other instance trades beside it."""
        self._shutting_down = True
        if self._shadow_task is not None:
            self._shadow_task.cancel()                 # shadow never delays shutdown: cancel, never await
        if self._shadow_variant_task is not None:
            self._shadow_variant_task.cancel()
        try:
            await asyncio.wait_for(self._graceful_handover(), timeout=self.shutdown_budget_s())
        except BaseException as e:  # noqa: BLE001   (incl. the budget timeout and cancellation)
            log.error("graceful shutdown incomplete: %s", type(e).__name__)
            if self.lease.role == LEADER and not self._strategy_running and not self.arb_running_scan:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(self.lease.release(handover=None), SHUTDOWN_STORE_TIMEOUT_S)
        self._leader_ready = False
        for ex in (self._ct_executor, self._tel_executor, self._monitor_executor, self._arb_executor,
                   self._shadow_executor):
            with contextlib.suppress(Exception):
                ex.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):       # last, and without cancelling a queued Stop cancel
            self._stop_executor.shutdown(wait=False)
        for t in self._tasks + list(self._bg) + ([self._state_retry] if self._state_retry else []):
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []

    async def _graceful_handover(self) -> None:
        for t in self._tasks:                            # 1) no new passes, scans, renews or pings
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.pass_budget_s       # 2) the order step / arb set finishes (never cut)
        while (self._strategy_running or self.arb_running_scan) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        overran = self._strategy_running or self.arb_running_scan
        tail = loop.time() + SHUTDOWN_TAIL_S              # 3) the pass's own journal/heartbeat writes
        while self._pass_lock.locked() and not overran and loop.time() < tail:
            await asyncio.sleep(0.05)
        with contextlib.suppress(BaseException):          # 4) persist what the journal tail has not sent yet
            await asyncio.wait_for(self._sync_journal(), SHUTDOWN_STORE_TIMEOUT_S)
        await self._shutdown_stop_checks()
        if self.lease.role != LEADER:
            return
        if overran:
            msg = (f"a pass overran the {self.pass_budget_s:.0f} s budget at shutdown: the lease is NOT released "
                   f"(it expires in {self.lease.ttl_s:.0f} s), so no other instance trades beside it")
            log.error(msg)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.alerter.send("Shutdown: pass overran", msg, priority="high"), 5)
            return
        handover = await self._make_handover() if self._leader_ready else None
        ok = await self.lease.release(handover=handover)  # 5) hand over
        log.warning("lease released at shutdown (ok=%s, signed handover=%s)", ok, handover is not None)

    async def _shutdown_stop_checks(self) -> None:
        """S-53 (a): a Stop written to the store by ANOTHER instance (the new standby got the request) while this
        one was shutting down mid-pass: the pass may have placed orders after that instance's cancel. Adopt it
        (no token is then written) and cancel resting mm- orders once more. S-59: a Stop of ours whose store write
        is still pending gets one last write before the lease goes; if that fails, alert. Never raises."""
        if self._stop_pending:
            try:
                await asyncio.wait_for(self._write_stopped("stop-retry-at-shutdown"), SHUTDOWN_STORE_TIMEOUT_S)
                self._stop_pending, self._stop_persisted = False, True
            except Exception as e:  # noqa: BLE001
                log.error("Stop not persisted before shutdown: %s", type(e).__name__)
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(self.alerter.send(
                        "Stop not persisted across deploy", "A Stop on the old instance never reached the store. "
                        "Press Stop again on the new instance.", priority="urgent"), 5)
        if self.desired != "running":
            return                                       # a local Stop already cancelled (and S-4 re-cancelled)
        try:
            st = await asyncio.wait_for(self.store.get_state(), SHUTDOWN_STORE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.warning("shutdown state check failed: %s", type(e).__name__)
            return
        if not st or st.get("desired") != "stopped":
            return
        log.warning("shutdown: the store says stopped (by %s): cancelling resting mm- orders",
                    str(st.get("updated_by") or "?")[:64])
        self.desired = "stopped"                         # also means no handover token below
        self.arb_desired, self.arb_mode = "stopped", "demo"
        await self._best_effort_cancel(self.mode, "shutdown after a remote Stop")

    async def _make_handover(self) -> dict | None:
        """A2: a signed token only for a planned shutdown while the maker runs LIVE with no Stop pending. Never
        logged. None (the new leader drops to demo) in every other case."""
        try:
            key = hosvc.key_bytes(self.settings)
            if key is None or self.mode != "live" or self.desired != "running":
                return None
            if self.kill_switch() or self._stop_pending or self._stopping:
                return None
            gen = self._stop_gen
            st = await asyncio.wait_for(self.store.get_state(), SHUTDOWN_STORE_TIMEOUT_S)
            if not st or st.get("desired") != "running" or st.get("mode") != "live":
                return None
            fp = hosvc.state_fingerprint(st)
            if fp is None:
                return None
            token = hosvc.make_token(key, from_instance=self.instance_id, mode="live", desired="running",
                                     config_hash=hosvc.config_hash(botfiles.public_config(self.config)),
                                     stop_seq=fp, now=utcnow())
            if gen != self._stop_gen or self._stopping or self.desired != "running":
                return None                              # a Stop landed meanwhile: Stop wins
            return token
        except Exception as e:  # noqa: BLE001
            log.error("handover token not written: %s", type(e).__name__)
            return None

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def rehydrate(self) -> None:
        """State, config, journal and cloud settings from the store. On failure: stay stopped and alert."""
        self._leader_ready = False          # fence shut until the leadership rules run again on the fresh state
        gen0, persisted0 = self._stop_gen, self._stop_persisted
        try:
            await self.store.ensure_indexes()
            st = await self.store.get_state()
            st = st or dict(schemas.DEFAULT_STATE)
            self.desired = st.get("desired", "stopped")
            self.mode = st.get("mode", "demo")
            self.arb_desired = st.get("arb_desired", "stopped")
            self.arb_mode = st.get("arb_mode", "demo")
            self.store_ok = True
        except Exception as e:  # noqa: BLE001
            self.desired, self.store_ok = "stopped", False
            self.arb_desired, self.arb_mode = "stopped", "demo"
            self.last_error = f"store unreachable at startup: {type(e).__name__}"
            log.error(self.last_error)
            await self.alerter.send("Bot stopped: store unreachable", self.last_error, priority="high")
            self.config = self._safe_local_config()
            return
        # S-3 fail-safe: the kill switch file is written by Stop before the store. If it is still present while
        # the store says running, a Stop never reached the store (e.g. Atlas was down, then a restart): stay
        # stopped and keep retrying the store write. Start removes the file only after the store says running.
        # Remaining window: on a host whose disk is ephemeral (the file is lost on restart) AND Atlas was down at
        # Stop AND the process restarted before the background retry succeeded, rehydrate still reads running.
        # The Stop alert (ntfy) and the returned `errors` are the owner's signal in that case.
        if self.desired == "running" and self.kill_switch():
            log.warning("store says running but the kill switch is present: staying stopped")
            self.desired = "stopped"
            self._stop_pending = True
            self._ensure_stop_retry("rehydrate")
        # S-21 (owner decision): LIVE is never restored from the store, and the arb kill-switch rule: both are applied
        # (and persisted) only when this instance becomes LEADER (_become_leader), because a standby must not write
        # the state the running leader trades on. Until then a standby mirrors the store and never trades.
        self._state_seen_utc = _state_stamp(st)
        await self._check_schema()                   # R4: newer stored schema -> standby (alerted)
        config_ok = await self._load_config()
        try:
            rows = await self.store.list_journal(since=utcnow() - timedelta(days=JOURNAL_REHYDRATE_DAYS),
                                                 limit=200000)
            if rows:
                rows = list(reversed(rows))
                botfiles.write_journal_rows(self.journal_path, rows, schemas.JOURNAL_FIELDS)
                self._index_journal(rows)            # maker order ids for fill attribution (no extra read)
        except Exception as e:  # noqa: BLE001
            log.error("journal rehydrate failed: %s", e)
        self._tail = botfiles.JournalTail(self.journal_path)
        self._tail.skip_to_end()
        await self.load_cloud()
        st_alerts = self.cloud.get("alerts_state")
        if isinstance(st_alerts, dict) and isinstance(st_alerts.get("daily_sent"), str):
            self._daily_sent = st_alerts["daily_sent"][:10]
        try:
            hb = await self.store.last_heartbeat()
            if hb and hb.get("ts"):
                self.last_run_utc = hb["ts"]
            ms = await self.store.last_monitor_summary()
            if ms and ms.get("ts"):
                self.last_monitor_utc = ms["ts"]
        except Exception:  # noqa: BLE001
            pass
        # S-27: the two cache refreshes run in the background, so the API and Stop come up without waiting on them
        self._spawn(self._refresh_caches())
        # R1: every instance starts as standby; try for the lease now (and every LEASE_RENEW_S in the lease loop)
        await self._lease_tick(rehydrate_state=st, config_ok=config_ok, stop_gen=gen0, stop_persisted=persisted0)
        if self.role() != "leader":
            log.warning("standby: not trading (lease held elsewhere, store unconfirmed or schema newer)")

    async def _refresh_caches(self) -> None:
        """S-27: fill-quality and queue-stats aggregates after startup (each bounded, never raises)."""
        with contextlib.suppress(Exception):
            await self.refresh_fill_quality()
        with contextlib.suppress(Exception):
            await self.refresh_queue_stats()

    async def _load_config(self) -> bool:
        """Config from the store (seeded from the file if empty), validated (S-12) and written to the bot file.
        False only when the stored config failed validation (the bot then stays stopped)."""
        ok = True
        try:
            cfg = await self.store.get_config()
            if cfg is None:
                cfg = self._safe_local_config()
                await self.store.save_config(cfg, before=None, by="seed")
            else:
                try:
                    # S-12: store contents are not trusted; same checks as PUT /api/config + API ceilings
                    cfg = botfiles.validate_stored(cfg, self.settings)
                except ValueError as ve:
                    msgs = ve.args[0] if ve.args and isinstance(ve.args[0], list) else [str(ve)]
                    await self._invalid_config_stop("stored config failed validation", msgs)
                    cfg = self._safe_local_config()
                    ok = False
                else:
                    botfiles.write_config_file(self.config_path, cfg)
            self.config = botfiles.public_config(cfg)
        except Exception as e:  # noqa: BLE001
            log.error("config rehydrate failed: %s", type(e).__name__)
            self.config = self._safe_local_config()
        return ok

    async def _alert_resumed_state(self, st: dict, *, kept_live: bool = False) -> None:
        """S-21: anything that resumes trading or live from the store (no 2FA at restart) is announced loudly."""
        parts = []
        if kept_live and self.desired == "running":
            msg = ("LIVE kept across a planned deploy: the signed handover from the previous instance was valid "
                   "(same config, no Stop since, under 5 minutes old). Stop works as always.")
            log.warning(msg)
            with contextlib.suppress(Exception):
                await self.alerter.send("LIVE continued after deploy", msg, priority="urgent")
            return
        if self.desired == "running":
            parts.append(f"maker running ({self.mode})")
        if self.arb_desired == "running":
            parts.append("arbitrage on (log-only)")
        downgraded = list(getattr(self, "_live_downgraded", []) or [])
        if not parts and not downgraded:
            return
        by = str((st or {}).get("updated_by") or "unknown")[:64]
        msg = f"Restored from the store after a restart: {', '.join(parts) or 'nothing running'}. "               f"Last state change by: {by}."
        if downgraded:
            msg += (f" LIVE was NOT restored for {' and '.join(downgraded)}: now DEMO. Re-arm LIVE from the phone "
                    "(2FA + type LIVE) if you want real money again.")
        log.warning(msg)
        with contextlib.suppress(Exception):
            await self.alerter.send("LIVE not restored: re-arm from the phone" if downgraded
                                    else "Trading resumed after restart", msg, priority="urgent")

    def _safe_local_config(self) -> dict:
        """maker_config.json if it passes validate_stored, else DEFAULTS (always valid)."""
        try:
            return botfiles.validate_stored(botfiles.read_config_file(self.config_path), self.settings)
        except ValueError:
            log.error("local maker_config.json failed validation: using DEFAULTS")
            cfg = botfiles.public_config(self.ms.DEFAULTS)
            with contextlib.suppress(Exception):
                botfiles.write_config_file(self.config_path, cfg)
            return cfg

    async def _invalid_config_stop(self, what: str, msgs: list[str]) -> None:
        """S-12: never trade on a config that fails validation; stay stopped (maker and arb) and alert."""
        self.desired = "stopped"
        self.arb_desired = "stopped"
        self.last_error = f"{what}: {'; '.join(msgs)[:300]}"
        log.error(self.last_error)
        await self.alerter.send("Bot stopped: invalid config", self.last_error, priority="high")

    async def load_cloud(self) -> dict:
        merged = dict(schemas.DEFAULT_CLOUD_SETTINGS)
        try:
            merged.update(await self.store.get_cloud_settings() or {})
        except Exception as e:  # noqa: BLE001
            log.warning("cloud settings load failed: %s", type(e).__name__)
        self.cloud = merged
        return merged

    # ---- scheduling
    def _skip_reason(self) -> str | None:
        if not self.settings.runner_enabled:
            return "runner_disabled"             # S-10: RUNNER_ENABLED=false is a hard stop for every entry point
        if self.desired != "running":
            return "stopped"
        if self._stop_pending:
            return "stop_pending"
        if self.kill_switch():
            return "kill_switch"
        if self.running_pass or self._pass_lock.locked():
            return "pass_running"
        if self.mode == "live" and not self.live_allowed():
            return "live_not_allowed"
        if self._shutting_down:
            return "shutting_down"
        if not self._fence_ok(min_remaining_s=self.pass_budget_s + FENCE_MARGIN_S):
            return "standby"                     # R1: only the lease holder trades
        return None

    async def tick(self, reason: str = "tick") -> dict:
        """Starts a pass in the background if one is due; returns immediately."""
        why = self._skip_reason()
        if why:
            return {"ran": False, "reason": why}
        due = self.next_due_utc()
        if due and utcnow() < due:
            return {"ran": False, "reason": "not_due"}
        self.running_pass = True           # claim immediately so concurrent ticks skip
        self._spawn(self._pass(reason))
        return {"ran": True, "reason": f"started ({reason})"}

    async def run_once(self, by: str) -> dict:
        why = self._skip_reason()
        if why:
            return {"ran": False, "reason": why}
        last = self.last_pass_started_utc
        if last is not None and utcnow() - last < RUN_ONCE_MIN_GAP:
            return {"ran": False, "reason": "too_soon"}                 # S-22 (route -> 409 too_soon)
        self.running_pass = True
        self._spawn(self._pass(f"run-once by {by}"))
        return {"ran": True, "reason": "started"}

    async def _timer_loop(self) -> None:
        while True:
            try:
                await self.tick("timer")
            except Exception as e:  # noqa: BLE001
                log.error("timer tick failed: %s", e)
            await asyncio.sleep(TIMER_SECONDS)

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(TIMER_SECONDS)
            try:
                await self.check_silence()
            except Exception as e:  # noqa: BLE001
                log.error("watchdog failed: %s", e)
            try:
                await self.check_daily_summary()
            except Exception as e:  # noqa: BLE001
                log.error("daily summary failed: %s", type(e).__name__)

    async def check_silence(self) -> None:
        if self.desired != "running":
            self._silent_alerted = False
            self._silence_ref = None
            return
        limit = int(self.cloud.get("silent_alert_minutes") or 30)
        # A fresh Start (or restart) must get the full window before the first pass, so measure from the later of
        # the last pass and the moment the watchdog first saw the bot running.
        if self._silence_ref is None:
            self._silence_ref = utcnow()
        last = max(filter(None, (self.last_run_utc, self._silence_ref)))
        if utcnow() - last > timedelta(minutes=limit):
            if not self._silent_alerted:
                self._silent_alerted = True
                await self.alerter.send("No heartbeat", f"No trading pass for over {limit} min", priority="high")
        else:
            self._silent_alerted = False

    # ---- a pass
    def _run_strategy_sync(self, mode: str) -> tuple[int, str]:
        buf = io.StringIO()
        with kalshi_env(mode), contextlib.redirect_stdout(buf):
            # R1 fence, re-checked in the worker thread right before the strategy can send anything (the env lock
            # above may have been waited on): the lease must outlive the whole pass budget
            if not self._fence_ok(min_remaining_s=self.pass_budget_s):
                raise FenceBlocked("lease not confirmed before the order step")
            rc = self.ms.main()
        return rc, buf.getvalue()

    def _run_order_step(self, mode: str) -> tuple[int, str]:
        """The order step in its worker thread, with the R1 fence checked first."""
        if not self._fence_ok(min_remaining_s=self.pass_budget_s):
            raise FenceBlocked("lease not confirmed before the order step")
        return self._run_strategy_sync(mode)

    async def _pre_order_fence(self) -> bool:
        """Before a pass: renew the lease (fresh TTL for the whole pass), adopt a Stop persisted by another
        instance, then check the fence with the pass budget to spare. False = send nothing."""
        if not self._leader_ready:
            return False
        if not await self.lease.renew():           # fail safe: an unconfirmed lease (store error too) = no orders
            return False
        if self._leader_ready:
            await self._sync_state_from_store()
        return self._fence_ok(min_remaining_s=self.pass_budget_s + FENCE_MARGIN_S)

    async def _pass(self, reason: str) -> None:
        async with self._pass_lock:
            self.running_pass = True
            started = utcnow()
            self.last_pass_started_utc = started
            ok, err = True, None
            strategy_ran = False
            mode = self.mode
            try:
                kid, pk = mode_keys(mode)
                if not (kid and pk):
                    raise RuntimeError(f"no Kalshi {mode} keys in the environment")
                if mode == "live" and not self.live_allowed():
                    raise RuntimeError("live not allowed by env")
                if not self.settings.runner_enabled:
                    raise RuntimeError("runner disabled (RUNNER_ENABLED=false)")
                if self.kill_switch() or self.desired != "running" or self._stop_pending:
                    raise RuntimeError("stopped before pass")
                if self._shutting_down or not await self._pre_order_fence():
                    raise FenceBlocked("lease not confirmed")
                if self.kill_switch() or self.desired != "running" or self._stop_pending:
                    raise RuntimeError("stopped before pass")      # a persisted Stop adopted just above
                strategy_ran = True
                self._strategy_running = True
                t0 = time.monotonic()
                try:
                    rc, out = await asyncio.to_thread(self._run_order_step, mode)
                finally:
                    self._strategy_running = False
                took = time.monotonic() - t0
                if took > self.pass_budget_s:
                    log.warning("order step took %.1f s (budget %.0f s)", took, self.pass_budget_s)
                self.logs.add(out)
                await self._handle_write_report(out, mode)     # grouped order-error alerts; the pass stays ok
                if rc:
                    raise RuntimeError(f"maker_strategy exited {rc}")
            except FenceBlocked as e:
                strategy_ran = False
                await self._journal_fence_blocked(str(e))
                self.running_pass = False
                return                                   # standby: no heartbeat, no pass error alert
            except Exception as e:  # noqa: BLE001
                ok, err = False, f"{type(e).__name__}: {e}"
                log.error("pass failed: %s", err)
            if strategy_ran and (self.desired != "running" or self.kill_switch()):
                # S-4: a Stop landed during the pass; an order POST in flight at Stop time may have rested
                # after Stop's cancel. Cancel resting mm- orders once more.
                try:
                    n = await asyncio.wait_for(self._cancel_in_stop_pool(mode), timeout=CANCEL_TIMEOUT_S)
                    log.warning("post-pass cancel after Stop: cancelled=%s", n)
                except Exception as e:  # noqa: BLE001
                    log.error("post-pass cancel failed: %s", type(e).__name__)
            self.last_run_utc = utcnow()
            self.last_error = err
            await self._sync_journal(mode)
            if strategy_ran:
                await self.sync_fills(mode)
            try:
                await self.store.add_heartbeat({"ts": self.last_run_utc, "ok": ok, "error": err, "mode": mode,
                                                "reason": reason,
                                                "duration_s": round((self.last_run_utc - started).total_seconds(), 1)})
            except Exception as e:  # noqa: BLE001
                log.error("heartbeat write failed: %s", type(e).__name__)
            if not ok:
                await self.alerter.send("Trading pass error", err or "unknown", priority="high")
            self.running_pass = False
        if strategy_ran:
            self._start_fill_quality()          # S-24: outside the pass lock, own time budget, never blocks Stop
            self._start_queue_position(mode)    # same pattern: read-only, budgeted, Stop never waits on it
            self._start_close_time(mode)        # same pattern; runs after the queue evaluation above
            self._fire_post_pass_hook()         # read-only analytics warm-up; wrapped, never blocks or raises
        if ok and (self.last_monitor_utc is None or utcnow() - self.last_monitor_utc > MONITOR_EVERY):
            await self.run_monitor("weekly")

    def _fire_post_pass_hook(self) -> None:
        """Call the optional post-pass hook (it only schedules a background task). Any failure is logged, never raised
        into the trading loop."""
        hook = self.post_pass_hook
        if hook is None:
            return
        try:
            hook()
        except Exception as e:  # noqa: BLE001
            log.warning("post-pass hook failed: %s", type(e).__name__)

    async def _handle_write_report(self, out: str | None, mode: str, now: datetime | None = None) -> dict:
        """After a pass: record the strategy's write-path summary and send at most ONE grouped order-error alert
        (throttled to one per ORDER_ERROR_ALERT_EVERY; suppressed passes are counted into the next alert) and a
        separate 'trading paused' alert at most once per PAUSED_ALERT_EVERY. Plain text, no order ids or secrets.
        A pass with order errors is still an ok pass. Never raises."""
        now = now or utcnow()
        rep = parse_write_report(out)
        try:
            self.last_write_report = {**rep, "utc": iso(now), "mode": mode}
            if rep["series_capped"]:
                log.warning("series capped this pass: %s", ", ".join(s["series"] for s in rep["series_capped"]))
            n_err = rep["cancel"] + rep["place"]
            if n_err > 0:
                last = self._order_alert_utc
                if last is None or now - last >= ORDER_ERROR_ALERT_EVERY:
                    body = (f"The last trading pass ({mode}) could not cancel {rep['cancel']} and could not place "
                            f"{rep['place']} order(s). The pass kept going; a market whose cancel failed got no "
                            "replacement order.")
                    if self._order_alert_suppressed:
                        body += (f" {self._order_alert_suppressed} more pass(es) had order errors since the last "
                                 "alert.")
                    self._order_alert_utc, self._order_alert_suppressed = now, 0
                    with contextlib.suppress(Exception):
                        await self.alerter.send(f"Order errors: {rep['cancel']} cancel, {rep['place']} place", body,
                                                priority="high")
                else:
                    self._order_alert_suppressed += 1
            if rep["paused"]:
                last = self._paused_alert_utc
                if last is None or now - last >= PAUSED_ALERT_EVERY:
                    self._paused_alert_utc = now
                    with contextlib.suppress(Exception):
                        await self.alerter.send("Trading paused by Kalshi",
                                                f"Kalshi reported trading is paused ({mode}). The last pass stopped "
                                                "placing new orders; cancels were still tried.", priority="high")
        except Exception as e:  # noqa: BLE001
            log.warning("write report handling failed: %s", type(e).__name__)
        return rep

    async def _sync_journal(self, mode: str | None = None) -> int:
        if self._tail is None:
            self._tail = botfiles.JournalTail(self.journal_path)
        try:
            rows = self._tail.read_new()
            if not rows:
                return 0
            self._capture_queue_rows(rows, mode or self.mode)
            self._index_journal(rows)
            n = await self.store.add_journal_rows(rows)
            for r in rows:
                if r.get("event") == "account":
                    try:
                        info = json.loads(r.get("note") or "{}")
                    except ValueError:
                        info = {}
                    await self.store.add_account_snapshot({
                        "ts": datetime.fromisoformat(r["utc"]) if r.get("utc") else utcnow(),
                        "cash": info.get("cash"), "exposure": info.get("exposure"),
                        "headroom": info.get("headroom", 0.0)})
            return n
        except Exception as e:  # noqa: BLE001
            log.error("journal sync failed: %s", type(e).__name__)
            return 0

    # ---- fills + P&L (read-only Kalshi calls)
    def _fetch_fills_sync(self, mode: str, since: datetime) -> tuple[list[dict], list[dict]]:
        """Fills and settlements since `since`. The client is built directly (no env lock); S-25 short client."""
        client = self._tel_kalshi(mode)
        if client is None:
            return [], []
        min_ts = int(since.timestamp())
        fills = list(client.get_fills(min_ts=min_ts) or [])
        settlements: list[dict] = []
        getter = getattr(client, "get_settlements", None)
        if callable(getter):
            settlements = list(getter(min_ts=min_ts) or [])
        elif callable(getattr(client, "request", None)):
            cursor = None
            for _ in range(50):                          # bounded pagination
                body = client.request("GET", "/portfolio/settlements",
                                      params={"min_ts": min_ts, "limit": 200, "cursor": cursor}, auth=True)
                settlements.extend(body.get("settlements") or [])
                cursor = body.get("cursor")
                if not cursor or not body.get("settlements"):
                    break
        return fills, settlements

    async def sync_fills(self, mode: str, *, force: bool = False) -> dict:
        """Copies Kalshi fills + settlements to the store, at most every FILLS_SYNC_EVERY. Never raises."""
        now = utcnow()
        if not force and self.last_fills_sync_utc and now - self.last_fills_sync_utc < FILLS_SYNC_EVERY:
            return {"synced": False, "reason": "not_due"}
        since = (self.last_fills_sync_utc - timedelta(days=2)) if self.last_fills_sync_utc \
            else now - timedelta(days=FILLS_FIRST_DAYS)
        try:
            fills, settlements = await asyncio.wait_for(
                self._run_tel(self._fetch_fills_sync, mode, since), timeout=60)
            for f in fills:
                f.setdefault("env", mode)                 # demo and live P&L never mix
            fills = [f for f in fills if isinstance(f, dict) and (f.get("fill_id") or f.get("trade_id"))]
            settlements = [x for x in settlements if isinstance(x, dict) and x.get("ticker")]
            known = await self._label_fills(fills, mode, since)        # Y1 source/strategy; never raises
            known_settled = await self._known_settlements(settlements, since)
            self._stamp_cached_close_times(fills, mode)
            self._stamp_cached_close_times(settlements, mode)
            nf = await self.store.upsert_fills(fills)
            ns = await self.store.upsert_settlements(settlements)
            self.last_fills_sync_utc, self.fills_sync_error = now, None
            if known is not None:
                self._alert_new_fills([f for f in fills if str(f.get("fill_id") or f.get("trade_id")) not in known])
            if known_settled is not None:
                self._alert_settled([x for x in settlements if str(x["ticker"]) not in known_settled], fills)
            return {"synced": True, "fills": nf, "settlements": ns}
        except Exception as e:  # noqa: BLE001
            self.fills_sync_error = type(e).__name__
            log.error("fills sync failed: %s", _where(e))
            return {"synced": False, "reason": type(e).__name__}

    # ---- fill sources (UX item Y1; services/strategies.py). Read-only: labels only, never orders.
    def _index_journal(self, rows: list[dict]) -> None:
        """Remember maker order ids from journal quote rows (bounded; oldest dropped first). Never raises."""
        try:
            for oid in strat.journal_order_ids(rows):
                self._maker_oids.pop(oid, None)
                self._maker_oids[oid] = None
            while len(self._maker_oids) > MAKER_OID_MAX:
                self._maker_oids.pop(next(iter(self._maker_oids)))
        except Exception:  # noqa: BLE001
            pass

    async def _order_index(self, since: datetime) -> strat.OrderIndex:
        idx = strat.OrderIndex(self._maker_oids)
        try:
            arbs = await asyncio.wait_for(self.store.list_arb_opportunities(
                since=since - timedelta(hours=1), limit=5000, executed_only=True), 5)
            idx.add_arb_docs(arbs)
        except Exception as e:  # noqa: BLE001
            log.warning("fill sources: arb log read failed: %s", type(e).__name__)
        return idx

    def _lookup_orders_sync(self, mode: str, oids: list[str]) -> list[dict]:
        """GET /portfolio/orders/{id} (read-only) for order ids no local evidence explains: their client_order_id
        prefix settles bot vs manual. Ids are validated before they go into the path."""
        client = self._tel_kalshi(mode)
        req = getattr(client, "request", None)
        if client is None or not callable(req):
            return []
        out = []
        for oid in oids:
            if not strat.ORDER_ID_RE.match(str(oid)):
                continue
            try:
                body = req("GET", f"/portfolio/orders/{oid}", auth=True)
            except Exception as e:  # noqa: BLE001
                log.warning("order lookup failed: %s", type(e).__name__)
                continue
            o = body.get("order") if isinstance(body, dict) and isinstance(body.get("order"), dict) else body
            if isinstance(o, dict) and str(o.get("order_id") or oid) == oid:
                out.append({"order_id": oid, "client_order_id": o.get("client_order_id")})
        return out

    async def _label_fills(self, fills: list[dict], mode: str, since: datetime) -> set[str] | None:
        """Stamp source/strategy/source_by on fills about to be upserted, never weakening a stored label.
        Returns the fill ids already in the store, or None (and labels nothing) if that read failed."""
        try:
            ids = [str(f.get("fill_id") or f.get("trade_id")) for f in fills]
            if not ids:
                return set()
            docs = await asyncio.wait_for(self.store.list_fills(
                fill_ids=ids, limit=len(ids) + 1, fields=("fill_id",) + strat.LABEL_FIELDS), 10)
            stored = {str(d.get("fill_id")): d for d in docs}
        except Exception as e:  # noqa: BLE001
            log.warning("fill sources: stored labels read failed: %s", type(e).__name__)
            return None
        try:
            idx = await self._order_index(since)
            weak = [f for f, fid in zip(fills, ids)
                    if strat.EVIDENCE_RANK.get(str((stored.get(fid) or {}).get("source_by")), 0) == 0]
            oids = strat.unresolved(weak, idx, ORDER_LOOKUPS_PER_SYNC)
            if oids:
                try:
                    idx.add_orders(await asyncio.wait_for(self._run_tel(self._lookup_orders_sync, mode, oids),
                                                          20))
                except Exception as e:  # noqa: BLE001
                    log.warning("fill sources: order lookups failed: %s", type(e).__name__)
            for f, fid in zip(fills, ids):
                old = stored.get(fid) or {}
                m = strat.merge_label(old, idx.classify(f))
                f.update(m if m is not None else {k: old.get(k) for k in strat.LABEL_FIELDS})
        except Exception as e:  # noqa: BLE001
            log.warning("fill source labelling failed: %s", type(e).__name__)
        return set(stored)

    async def _known_settlements(self, settlements: list[dict], since: datetime) -> set[str] | None:
        if not settlements:
            return set()
        try:
            docs = await asyncio.wait_for(self.store.list_settlements(
                since=since - timedelta(days=3), limit=20000, fields=("ticker",)), 10)
            return {str(d.get("ticker")) for d in docs}
        except Exception as e:  # noqa: BLE001
            log.warning("settlement alert check failed: %s", type(e).__name__)
            return None

    async def backfill_sources(self, *, days: int = schemas.RETENTION_DAYS) -> dict:
        """Label stored fills that have no source yet (or only the 'default' guess) from the journal (incl. rows older
        than the in-memory index), the arb log and, bounded, order lookups. Read-only toward Kalshi. Never raises."""
        res: dict = {"checked": 0, "labelled": 0, "you": 0, "lookups": 0}
        try:
            now = utcnow()
            since = now - timedelta(days=days)
            fields = ("fill_id", "ts", "ticker", "market_ticker", "order_id", "is_taker", "env",
                      "client_order_id") + strat.LABEL_FIELDS
            fills = await asyncio.wait_for(self.store.list_fills(since=since, limit=SOURCE_BACKFILL_MAX * 3,
                                                                 fields=fields), 20)
            todo = [f for f in fills if f.get("source") not in strat.SOURCES
                    or strat.EVIDENCE_RANK.get(str(f.get("source_by")), 0) == 0][:SOURCE_BACKFILL_MAX]
            res["checked"] = len(todo)
            if not todo:
                return res
            oldest = min((f["ts"] for f in todo if isinstance(f.get("ts"), datetime)), default=since)
            idx = await self._order_index(oldest)
            if len(self._maker_oids) == 0 or oldest < now - timedelta(days=JOURNAL_REHYDRATE_DAYS):
                rows = await asyncio.wait_for(self.store.list_journal(
                    since=oldest - timedelta(days=1), limit=300000, events=["quote"], fields=("event", "note")), 20)
                idx.add_journal(rows)
                del rows
            by_env: dict[str, list[dict]] = {}
            for f in todo:
                by_env.setdefault(str(f.get("env") or self.mode), []).append(f)
            for env, group in by_env.items():
                oids = strat.unresolved(group, idx, ORDER_LOOKUPS_PER_SYNC)
                if oids and env in ("demo", "live"):
                    try:
                        found = await asyncio.wait_for(self._run_tel(self._lookup_orders_sync, env, oids), 20)
                        idx.add_orders(found)
                        res["lookups"] += len(oids)
                    except Exception as e:  # noqa: BLE001
                        log.warning("source backfill: order lookups failed: %s", type(e).__name__)
            updates = {}
            for f in todo:
                m = strat.merge_label(f, idx.classify(f))
                if m is not None:
                    updates[str(f["fill_id"])] = m
                    res["you"] += m["source"] == strat.YOU
            if updates:
                res["labelled"] = await asyncio.wait_for(self.store.set_fills_fields(updates), 30)
            return res
        except Exception as e:  # noqa: BLE001
            log.warning("source backfill failed: %s", type(e).__name__)
            return {**res, "error": type(e).__name__}

    async def run_source_backfill(self) -> dict:
        try:
            res = await asyncio.wait_for(self.backfill_sources(), timeout=SOURCE_BACKFILL_BUDGET_S)
        except Exception as e:  # noqa: BLE001   (incl. the budget timeout)
            res = {"error": type(e).__name__}
        self.source_backfill = {**res, "ts": iso(utcnow())}
        log.info("fill source backfill: %s", {k: v for k, v in res.items() if k != "error"} or res)
        return res

    # ---- today P&L + recent passes (/status, UX item 25)
    async def pnl_today_view(self, tz: str | None = None) -> dict:
        """Today's realised P&L from the viewer's LOCAL midnight: {value: {maker, arb, you, bot, total} | None, tz,
        since}. `tz` is an IANA name from the client; unknown or malformed means UTC (pnl.resolve_tz). Settlements and
        storage stay UTC; only the boundary moves. Cached PNL_TODAY_TTL_S per (mode, validated zone), at most
        PNL_TODAY_CACHE_MAX entries. value is None if the store read fails (not cached)."""
        zone, tzname = pnlsvc.resolve_tz(tz)
        t, env = time.monotonic(), self.mode
        key = (env, tzname)
        c = self._pnl_today.get(key)
        if c and 0 <= t - c[0] < PNL_TODAY_TTL_S:
            return {"value": c[1], "tz": tzname, "since": c[2]}
        since = pnlsvc.local_midnight_utc(utcnow(), zone)
        try:
            res, _, _ = await asyncio.wait_for(pnlsvc.by_source_since(self.store, since, env=env), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("today pnl failed: %s", type(e).__name__)
            return {"value": None, "tz": tzname, "since": iso(since)}
        val = {k: res.get(k) for k in strat.bucket_ids()}
        val.update({"bot": res.get("bot"), "total": res.get("total")})
        for k in [k for k, v in self._pnl_today.items() if not 0 <= t - v[0] < PNL_TODAY_TTL_S]:
            del self._pnl_today[k]                                     # drop expired entries first
        while len(self._pnl_today) >= PNL_TODAY_CACHE_MAX:
            del self._pnl_today[min(self._pnl_today, key=lambda k: self._pnl_today[k][0])]
        self._pnl_today[key] = (t, val, iso(since))
        return {"value": val, "tz": tzname, "since": iso(since)}

    async def pnl_today(self, tz: str | None = None) -> dict | None:
        """Just the figures of pnl_today_view(); None if the store read fails."""
        return (await self.pnl_today_view(tz))["value"]

    async def recent_passes(self, n: int = RECENT_PASSES) -> list[dict]:
        """Last n passes, newest last: {ts, ok, late, errors}. late = the gap since the previous pass was more than
        poll_minutes + max(2, poll/2) minutes. [] if none or the store is unreachable."""
        try:
            hbs = await asyncio.wait_for(self.store.list_heartbeats(limit=n + 1), 5)
        except Exception:  # noqa: BLE001
            return []
        hbs = [h for h in reversed(hbs or []) if isinstance(h.get("ts"), datetime)]
        poll = self.poll_minutes()
        late_after = timedelta(minutes=poll + max(2.0, poll / 2))
        out = []
        for i, h in enumerate(hbs):
            prev = hbs[i - 1]["ts"] if i > 0 else None
            out.append({"ts": iso(h["ts"]), "ok": bool(h.get("ok")),
                        "late": bool(prev is not None and h["ts"] - prev > late_after),
                        "errors": 0 if h.get("ok") else 1})
        return out[-n:]

    # ---- alert history, web push, everyday alerts (UX items 37, 38, 45)
    def alert_settings(self) -> dict:
        return alertsvc.merged_settings(self.cloud.get("alerts"))

    def _on_alert(self, kind: str, title: str, body: str, priority: str, channels: list[str]) -> None:
        """Alerter hook: history + push in the background, so no alert (Stop's included) waits on it."""
        try:
            self._spawn(self._deliver_alert(kind, title, body, priority, list(channels)))
        except Exception as e:  # noqa: BLE001   (no running loop)
            log.warning("alert delivery not scheduled: %s", type(e).__name__)

    ALERT_URLS = {"fill": "/activity", "settled": "/activity", "daily": "/", "error": "/health", "stop": "/",
                  "breaker": "/", "test": "/alerts", "system": "/"}

    async def _deliver_alert(self, kind: str, title: str, body: str, priority: str, channels: list[str]) -> None:
        try:
            if self.push.enabled and alertsvc.should_push(self.alert_settings(), kind, priority):
                critical = alertsvc.is_critical(kind, priority)
                payload = {"title": title[:schemas.ALERT_TITLE_MAX], "body": body[:schemas.ALERT_BODY_MAX],
                           "type": kind, "url": self.ALERT_URLS.get(kind, "/"), "ts": iso(utcnow())}
                res = await asyncio.wait_for(self.push.send_all(payload, urgency="high" if critical else "normal",
                                                                ttl=86400 if critical else 3600), 30)
                if res.get("sent"):
                    channels.append("push")
        except Exception as e:  # noqa: BLE001
            log.warning("push delivery failed: %s", type(e).__name__)
        try:
            await asyncio.wait_for(self.store.add_alert({"type": kind, "title": title, "body": body,
                                                         "priority": priority, "channels": channels}), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("alert history write failed: %s", type(e).__name__)

    def _notify(self, kind: str, title: str, body: str) -> None:
        fn = getattr(self.alerter, "notify", None)
        if callable(fn):
            fn(kind, title, body)

    @staticmethod
    def _money(v: float | None) -> str:
        if v is None:
            return "$0.00"
        return f"{'+' if v >= 0 else '−'}${abs(v):.2f}"

    def _alert_new_fills(self, new: list[dict]) -> None:
        """'every_fill' mode: one alert per sync listing the new fills. Skipped for a first bulk import."""
        try:
            if not new or self.alert_settings().get("mode") != "every_fill" or len(new) > 50:
                return
            names = {s["id"]: s["name"] for s in strat.registry()}
            lines = []
            for f in new[:FILL_ALERT_MAX_LINES]:
                src, sid = strat.fill_label(f)
                side = pnlsvc.fill_side(f) or "?"
                price = pnlsvc.fill_price(f, side) if side in ("yes", "no") else None
                lines.append(f"{names.get(sid or src, 'You')}: {f.get('ticker') or f.get('market_ticker')} "
                             f"{side.upper()} {pnlsvc.fill_count(f):g}" + (f" @ ${price:.2f}" if price else ""))
            if len(new) > FILL_ALERT_MAX_LINES:
                lines.append(f"+{len(new) - FILL_ALERT_MAX_LINES} more")
            self._notify("fill", "New Fill" if len(new) == 1 else f"{len(new)} New Fills", "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log.warning("fill alert failed: %s", type(e).__name__)

    def _alert_settled(self, new: list[dict], fills: list[dict]) -> None:
        try:
            if not new or self.alert_settings().get("mode") != "every_fill" or len(new) > 50:
                return
            pnl = pnlsvc.realised_by_source(fills, [dict(s, ts=schemas._safe_to_utc(s.get("ts") or s.get(
                "settled_time")) or utcnow()) for s in new], since=utcnow() - timedelta(days=400))
            lines = [f"{s['ticker']}: {str(s.get('market_result') or s.get('result') or '?').upper()}"
                     for s in new[:FILL_ALERT_MAX_LINES]]
            if len(new) > FILL_ALERT_MAX_LINES:
                lines.append(f"+{len(new) - FILL_ALERT_MAX_LINES} more")
            if pnl.get("total") is not None:
                lines.append(f"P&L {self._money(pnl['total'])}")
            title = "Market Settled" if len(new) == 1 else f"{len(new)} Markets Settled"
            self._notify("settled", title, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log.warning("settled alert failed: %s", type(e).__name__)

    async def daily_summary_text(self, now: datetime | None = None) -> str:
        now = now or utcnow()
        since = now - timedelta(days=1)
        parts = []
        try:
            res, _, _ = await asyncio.wait_for(pnlsvc.by_source_since(self.store, since, env=self.mode), 10)
            parts.append(f"Bot {self._money(res.get('bot'))}")
            if res.get("you") is not None:
                parts.append(f"You {self._money(res['you'])}")
        except Exception as e:  # noqa: BLE001
            log.warning("daily summary P&L failed: %s", type(e).__name__)
        try:
            n = len(await asyncio.wait_for(self.store.list_fills(since=since, limit=5000, fields=("fill_id",)), 10))
            parts.append(f"{n} fill{'s' if n != 1 else ''}")
        except Exception:  # noqa: BLE001
            pass
        state = "running" if self.desired == "running" and not self.kill_switch() else "stopped"
        parts.append(f"Maker {state} ({self.mode})")
        return "Last 24 h: " + " · ".join(parts)

    async def check_daily_summary(self, now: datetime | None = None) -> bool:
        """Once per local day at/after daily_summary_time: one 'daily' alert (history + push). Never raises."""
        try:
            s = self.alert_settings()
            hhmm = s.get("daily_summary_time")
            if not hhmm:
                return False
            loc = alertsvc.local_now(s, now)
            today = loc.date().isoformat()
            if self._daily_sent == today or loc.strftime("%H:%M") < str(hhmm):
                return False
            self._daily_sent = today                     # set first: a slow store never causes a repeat
            self._notify("daily", "Daily Summary", await self.daily_summary_text(now))
            self.cloud["alerts_state"] = {"daily_sent": today}
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.store.save_cloud_settings({"alerts_state": {"daily_sent": today}},
                                                                      by="daily-summary"), 5)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("daily summary failed: %s", type(e).__name__)
            return False

    async def evaluate_fill_quality(self) -> dict:
        """Read-only telemetry: for up to MAX_FILLS_PER_PASS maker fills whose 60-min window has closed, record
        trade-through and the 60-min maker-side move on the fill doc (public trades only). Never raises."""
        now = utcnow()
        done = 0
        try:
            fills = await asyncio.wait_for(
                self.store.list_fills(since=now - fq.MAX_AGE, until=now - fq.SETTLE_DELAY, limit=2000), timeout=10)
            todo = [f for f in fills if not f.get("is_taker") and strat.is_bot(f)
                    and not isinstance(f.get(fq.FIELD), dict)
                    and f.get("fill_id") and (f.get("ticker") or f.get("market_ticker"))]
            for f in todo[:fq.MAX_FILLS_PER_PASS]:
                ticker = f.get("ticker") or f.get("market_ticker")
                ts = fq._ts(f.get("ts"))
                if ts is None:
                    continue
                env = f.get("env") or self.mode

                def _get(env=env, ticker=ticker, ts=ts):
                    return fq.fetch_trades(self._tel_public(env), ticker, ts, ts + fq.WINDOW)
                try:
                    trades = await asyncio.wait_for(self._run_tel(_get), timeout=30)
                except Exception as e:  # noqa: BLE001
                    log.warning("fill-quality trades fetch failed: %s", type(e).__name__)
                    continue
                res = fq.evaluate(f, trades) or {"unreadable": True, "evaluated_utc": utcnow()}
                await self.store.set_fill_fields(f["fill_id"], {fq.FIELD: res})
                done += 1
            return {"evaluated": done, "pending": max(0, len(todo) - done)}
        except Exception as e:  # noqa: BLE001
            log.error("fill-quality evaluation failed: %s", type(e).__name__)
            return {"evaluated": done, "error": type(e).__name__}

    def _start_fill_quality(self) -> asyncio.Task | None:
        """One background evaluation at a time (skipped if one is still running)."""
        if self._fq_task is not None and not self._fq_task.done():
            return None
        self._fq_task = self._spawn(self.run_fill_quality())
        return self._fq_task

    async def run_fill_quality(self) -> dict:
        """Evaluate (bounded by FILL_QUALITY_BUDGET_S in total), then refresh the cached aggregate. Never raises."""
        try:
            res = await asyncio.wait_for(self.evaluate_fill_quality(), timeout=FILL_QUALITY_BUDGET_S)
        except Exception as e:  # noqa: BLE001   (incl. the budget timeout: the rest is scored next pass)
            log.warning("fill-quality evaluation stopped: %s", type(e).__name__)
            res = {"error": type(e).__name__}
        await self.refresh_fill_quality()
        return res

    async def refresh_fill_quality(self, *, days: int = 30) -> dict | None:
        """S-23: compute the aggregate once and cache it (overall + per series). None if the store read fails."""
        try:
            fills = await asyncio.wait_for(
                self.store.list_fills(since=utcnow() - timedelta(days=days), limit=100000,
                                      fields=("fill_id", "ticker", "market_ticker", "is_taker", "env", "source",
                                              "strategy", fq.FIELD)), timeout=5)     # S-26 projection
        except Exception as e:  # noqa: BLE001
            log.warning("fill-quality aggregate failed: %s", type(e).__name__)
            self.fill_quality_cache = None          # store down: serve null rather than a silently stale value
            return None
        fills = [f for f in fills if strat.is_bot(f)]     # Y1: manual trades are never a bot statistic
        mode = self.mode
        by_series = {}
        for ser in self.ms.DEFAULTS["series"]:
            pre = ser + "-"
            by_series[ser] = fq.aggregate([f for f in fills if str(f.get("ticker") or f.get("market_ticker") or "")
                                           .startswith(pre)], env=mode)
        self.fill_quality_cache = {"mode": mode, "overall": fq.aggregate(fills, env=mode), "by_series": by_series,
                                   "ts": utcnow()}
        return self.fill_quality_cache

    def fill_quality(self, series: str | None = None) -> dict | None:
        """Cached aggregate for the current mode ({n, trade_through_share, avg_move_60m_c, baseline}), or None
        if never computed or the mode changed since. Serving it costs no store read."""
        c = self.fill_quality_cache
        if not c or c.get("mode") != self.mode:
            return None
        return c["by_series"].get(series) if series else c["overall"]

    # ---- queue-position telemetry (read-only; services/queuepos.py)
    def _capture_queue_rows(self, rows: list[dict], mode: str) -> None:
        try:
            for r in rows:
                if r.get("event") in ("quote", "cancel"):
                    self._qp_rows.append((mode, r))
        except Exception:  # noqa: BLE001
            pass

    def _start_queue_position(self, mode: str | None = None) -> asyncio.Task | None:
        """One background evaluation at a time (skipped if one is still running; its rows wait for the next)."""
        if self._qp_task is not None and not self._qp_task.done():
            return None
        self._qp_task = self._spawn(self.run_queue_position(mode or self.mode))
        return self._qp_task

    async def run_queue_position(self, mode: str | None = None) -> dict:
        """Evaluate (bounded by QUEUE_BUDGET_S in total), then refresh the cached aggregate. Never raises."""
        try:
            res = await asyncio.wait_for(self.evaluate_queue_position(mode or self.mode), timeout=QUEUE_BUDGET_S)
        except Exception as e:  # noqa: BLE001   (incl. the budget timeout: open episodes are observed next pass)
            log.warning("queue-position evaluation stopped: %s", type(e).__name__)
            res = {"error": type(e).__name__}
        await self.refresh_queue_stats()
        return res

    def _qp_allowance(self, now: datetime) -> int:
        day = now.strftime("%Y-%m-%d")
        if day != self._qp_day:
            self._qp_day, self._qp_day_count = day, 0
        return max(0, qp.DAILY_NEW_CAP - self._qp_day_count)

    async def evaluate_queue_position(self, mode: str) -> dict:
        """Journal rows -> new/ended episodes; then (capped) read-only Kalshi reads -> join size, observations,
        fills and outcomes for the open episodes of `mode`. Never raises."""
        now = utcnow()
        rows_by_env: dict[str, list[dict]] = {}
        while self._qp_rows:
            env, row = self._qp_rows.popleft()
            rows_by_env.setdefault(env, []).append(row)
        res: dict = {"new": 0, "closed": 0, "observed": 0, "dropped": 0, "calls": 0}
        try:
            open_list = await asyncio.wait_for(self.store.list_quote_episodes(status="open", limit=qp.MAX_OPEN),
                                               timeout=10)
        except Exception as e:  # noqa: BLE001
            for env, rows in rows_by_env.items():          # keep the rows for the next run (bounded deque)
                for r in rows:
                    self._qp_rows.append((env, r))
            log.warning("queue-position: store read failed: %s", type(e).__name__)
            return {**res, "error": type(e).__name__}
        open_eps = {e["order_id"]: e for e in open_list}
        new_ids: set[str] = set()
        try:
            series = list(self.ms.DEFAULTS["series"])
            for env, rows in rows_by_env.items():
                new, closed, dropped = qp.apply_journal(rows, open_eps, env=env, series_list=series,
                                                        allowance=self._qp_allowance(now), now=now)
                self._qp_day_count += len(new)
                new_ids.update(e["order_id"] for e in new)
                if new:
                    await self._close_times_for_new_episodes(new)
                res["new"] += len(new)
                res["closed"] += len(closed)
                res["dropped"] += dropped
                if new or closed:
                    await asyncio.wait_for(self.store.upsert_quote_episodes(
                        [qp.strip_private(e) for e in new + closed]), timeout=10)
            if res["dropped"]:
                log.warning("queue-position: daily cap reached, %s episodes not tracked", res["dropped"])
            eps = [e for e in open_eps.values() if e.get("env") == mode]
            if not eps:
                return res
            before = {e["order_id"]: repr(qp.strip_private(e)) for e in eps}
            tickers = sorted({e["ticker"] for e in eps})
            fills_since = max(min(e["ts"] for e in eps), now - timedelta(days=qp.FILLS_MAX_DAYS))

            def _account():
                return qp.collect_account(self._tel_kalshi(mode), tickers, fills_since)
            acct = await asyncio.wait_for(self._run_tel(_account), timeout=30)
            res["calls"] += acct["calls"]
            store_fills = None
            if acct["fills"] is None:
                with contextlib.suppress(Exception):
                    store_fills = await asyncio.wait_for(self.store.list_fills(since=fills_since, limit=5000), 5)
            qp.apply_account(eps, acct, now=now, new_ids=new_ids, stop_ids=set(self._stop_cancelled),
                             last_stop_utc=self.last_stop_utc, store_fills=store_fills)
            books, trade_specs = qp.plan_public(eps, new_ids)

            def _public():
                return qp.collect_public(self._tel_public(mode), books, trade_specs)
            if books or trade_specs:
                pub = await asyncio.wait_for(self._run_tel(_public), timeout=60)
            else:
                pub = {"books": {}, "trades": {}, "calls": 0, "errors": []}
            res["calls"] += pub["calls"]
            qp.apply_public(eps, pub, now=now, new_ids=new_ids)
            res["observed"] = sum(1 for e in eps if e.get("status") == "open" and e.get("updated_utc") == now)
            res["closed"] += sum(1 for e in eps if e.get("status") == "closed" and e.get("closed_utc") == now)
            changed = [qp.strip_private(e) for e in eps
                       if e["order_id"] in new_ids or repr(qp.strip_private(e)) != before.get(e["order_id"])]
            if changed:
                await asyncio.wait_for(self.store.upsert_quote_episodes(changed), timeout=10)
            res["written"] = len(changed)
            errs = acct["errors"] + pub["errors"]
            if errs:
                res["errors"] = errs[:10]
            return res
        except Exception as e:  # noqa: BLE001
            log.warning("queue-position evaluation failed: %s", type(e).__name__)
            return {**res, "error": type(e).__name__}

    async def refresh_queue_stats(self, *, days: int = 30) -> dict | None:
        """Compute queue_stats once (overall + per series) and cache it. None if the store read fails.
        Loads only the aggregate's fields (projection) and keeps only the fills it links to (512 MB instance)."""
        now = utcnow()
        mode = self.mode
        try:
            eps = await asyncio.wait_for(self.store.list_quote_episodes(
                since=now - timedelta(days=days), env=mode, limit=60000, fields=qp.AGG_FIELDS), timeout=5)
            fills_by_id: dict = {}
            wanted = {str(e["fill_ids"][0]) for e in eps if e.get("fill_ids")}
            if wanted:
                fills = await asyncio.wait_for(self.store.list_fills(       # S-26: projection + only linked fills
                    since=now - timedelta(days=days + 1), limit=100000, fields=("fill_id", fq.FIELD),
                    fill_ids=sorted(wanted)), timeout=5)
                fills_by_id = {str(f.get("fill_id")): {fq.FIELD: f.get(fq.FIELD)} for f in fills
                               if str(f.get("fill_id")) in wanted}
                del fills
        except Exception as e:  # noqa: BLE001
            log.warning("queue stats aggregate failed: %s", type(e).__name__)
            self.queue_stats_cache = None
            return None
        mine = [e for e in eps if e.get("env") == mode]
        by_series = {ser: qp.aggregate([e for e in mine if e.get("series") == ser], fills_by_id, now=now)
                     for ser in self.ms.DEFAULTS["series"]}
        self.queue_stats_cache = {"mode": mode, "overall": qp.aggregate(mine, fills_by_id, now=now),
                                  "by_series": by_series, "ts": now}
        return self.queue_stats_cache

    def queue_stats(self, series: str | None = None) -> dict | None:
        """Cached queue_stats for the current mode, or None if never computed, the store was down at the last
        refresh, or the mode changed since. Serving it costs no store read."""
        c = self.queue_stats_cache
        if not c or c.get("mode") != self.mode:
            return None
        return c["by_series"].get(series) if series else c["overall"]

    # ---- market close time (read-only; services/closetime.py)
    def _stamp_cached_close_times(self, docs: list[dict], env: str) -> int:
        """close_time from the doc itself or the cache (no Kalshi call) on docs about to be written."""
        n = 0
        try:
            for d in docs:
                if not isinstance(d, dict):
                    continue
                t = ctsvc.doc_ticker(d)
                e = str(d.get("env") or env)
                have = ctsvc.to_utc(d.get(ctsvc.FIELD))
                if have is not None:
                    self.close_times.seed(e, t, have)
                    continue
                d.pop(ctsvc.FIELD, None)
                c = self.close_times.get(e, t) if ctsvc.valid_ticker(t) else None
                if c is not None:
                    d[ctsvc.FIELD] = c
                    n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("close-time stamp failed: %s", type(e).__name__)
        return n

    async def _close_times_for_new_episodes(self, eps: list[dict]) -> None:
        """Set close_time on new episodes before their insert (cache, else at most MAX_FETCH_EPISODES GETs, short
        timeout). Unresolved ones are filled in later by the backfill. Never raises."""
        try:
            wanted = [(e["env"], e["ticker"]) for e in eps if ctsvc.to_utc(e.get(ctsvc.FIELD)) is None]
            if not wanted:
                return
            deadline = time.monotonic() + CLOSE_TIME_INSERT_TIMEOUT_S - 0.5
            found = await asyncio.wait_for(self._ct_resolve(wanted, ctsvc.MAX_FETCH_EPISODES, deadline),
                                           timeout=CLOSE_TIME_INSERT_TIMEOUT_S)
            for e in eps:
                c = found.get((e["env"], e["ticker"]))
                if c is not None and ctsvc.to_utc(e.get(ctsvc.FIELD)) is None:
                    e[ctsvc.FIELD] = c
        except Exception as e:  # noqa: BLE001
            log.warning("close-time lookup for new episodes failed: %s", type(e).__name__)

    def _tel_kalshi(self, mode: str):
        """S-25: telemetry client: the injected factory if one was given (tests), else the short one."""
        f = self.kalshi_factory
        return telemetry_kalshi_factory(mode) if f is default_kalshi_factory else f(mode)

    def _tel_public(self, env: str):
        f = self.public_factory
        return telemetry_public_factory(env) if f is default_public_factory else f(env)

    def _run_tel(self, fn, *args):
        """S-25: run a blocking telemetry read on the dedicated pool (never the default executor Stop uses)."""
        return asyncio.get_running_loop().run_in_executor(self._tel_executor, functools.partial(fn, *args))

    def _ct_factory(self, env: str):
        """Close-time client: the injected public factory if one was given (tests), else the no-retry, 5 s client."""
        f = self.public_factory
        return closetime_public_factory(env) if f is default_public_factory else f(env)

    def _ct_resolve(self, wanted: list, max_fetch: int, deadline: float):
        """closetime.resolve on the dedicated executor (S-31). A timeout cancels it if it hasn't started yet."""
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._ct_executor, functools.partial(
            self.close_times.resolve, self._ct_factory, wanted, max_fetch=max_fetch, deadline=deadline))

    def _start_close_time(self, mode: str | None = None) -> asyncio.Task | None:
        """One background backfill at a time (skipped if one is still running)."""
        if self._ct_task is not None and not self._ct_task.done():
            return None
        self._ct_task = self._spawn(self.run_close_time(mode or self.mode))
        return self._ct_task

    async def run_close_time(self, mode: str | None = None) -> dict:
        """Wait (bounded) for the queue evaluation so both share one cache, then backfill within
        CLOSE_TIME_BUDGET_S. Never raises."""
        qt = self._qp_task
        if qt is not None and not qt.done():
            with contextlib.suppress(Exception):
                await asyncio.wait({qt}, timeout=QUEUE_BUDGET_S + 5)
        try:
            return await asyncio.wait_for(self.backfill_close_times(mode or self.mode), timeout=CLOSE_TIME_BUDGET_S)
        except Exception as e:  # noqa: BLE001   (incl. the budget timeout: the rest is filled next pass)
            log.warning("close-time backfill stopped: %s", type(e).__name__)
            return {"error": type(e).__name__}

    async def backfill_close_times(self, mode: str) -> dict:
        """Bounded best-effort backfill of close_time on open episodes, recent fills (maker and taker) and recent
        settlements that lack it. Values already in the store seed the cache first; then at most
        MAX_FETCH_BACKFILL GET /markets/{t} and MAX_BACKFILL_WRITES writes. Never raises."""
        res: dict = {"episodes": 0, "fills": 0, "settlements": 0, "fetches": 0}
        try:
            since = utcnow() - timedelta(days=ctsvc.BACKFILL_DAYS)
            eps = await asyncio.wait_for(self.store.list_quote_episodes(status="open", limit=qp.MAX_OPEN), 10)
            fills = await asyncio.wait_for(self.store.list_fills(
                since=since, limit=2000, fields=("fill_id", "ticker", "market_ticker", "env", "close_time", "ts")), 10)
            try:
                setts = await asyncio.wait_for(self.store.list_settlements(
                    since=since, limit=500, fields=("ticker", "env", "close_time", "ts")), 10)
            except Exception as e:  # noqa: BLE001
                log.warning("close-time: settlements read failed: %s", type(e).__name__)
                setts = []
            groups = (("episodes", eps), ("fills", fills), ("settlements", setts))
            for _, docs in groups:                    # values already in the store cost nothing
                for d in docs:
                    c = ctsvc.to_utc(d.get(ctsvc.FIELD))
                    if c is not None:
                        self.close_times.seed(str(d.get("env") or mode), ctsvc.doc_ticker(d), c)
            todo = [(name, d) for name, docs in groups for d in ctsvc.missing(docs)
                    if ctsvc.valid_ticker(ctsvc.doc_ticker(d))]
            if not todo:
                return res
            wanted = [(str(d.get("env") or mode), ctsvc.doc_ticker(d)) for _, d in todo]
            before = self.close_times.fetches
            deadline = time.monotonic() + max(0.1, CLOSE_TIME_BUDGET_S - 5)
            found = await asyncio.wait_for(self._ct_resolve(wanted, ctsvc.MAX_FETCH_BACKFILL, deadline),
                                           timeout=max(0.2, CLOSE_TIME_BUDGET_S - 3))
            res["fetches"] = self.close_times.fetches - before
            writes = 0
            for name, d in todo:
                if writes >= ctsvc.MAX_BACKFILL_WRITES:
                    break
                c = found.get((str(d.get("env") or mode), ctsvc.doc_ticker(d)))
                if c is None:
                    continue
                try:
                    if name == "episodes":
                        ok = await asyncio.wait_for(self.store.set_quote_episode_close_time(d["order_id"], c), 5)
                    elif name == "fills":
                        ok = await asyncio.wait_for(self.store.set_fill_fields(d["fill_id"], {ctsvc.FIELD: c}), 5)
                    else:
                        ok = await asyncio.wait_for(
                            self.store.set_settlement_fields(d["ticker"], {ctsvc.FIELD: c}), 5)
                except Exception as e:  # noqa: BLE001
                    log.warning("close-time write failed (%s): %s", name, type(e).__name__)
                    continue
                writes += 1
                if ok:
                    res[name] += 1
            res["pending"] = max(0, len(todo) - res["episodes"] - res["fills"] - res["settlements"])
            return res
        except Exception as e:  # noqa: BLE001
            log.warning("close-time backfill failed: %s", type(e).__name__)
            return {**res, "error": type(e).__name__}

    async def pnl(self, *, taker: bool = False) -> dict | None:
        """d7/d30 realised P&L in the current maker mode (taker=True: arbitrage legs). None if no data or error."""
        try:
            return await asyncio.wait_for(pnlsvc.compute(self.store, utcnow(), taker=taker, env=self.mode), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("pnl compute failed: %s", type(e).__name__)
            return None

    async def pnl_pair(self) -> tuple[dict | None, dict | None]:
        """(maker, arbitrage) d7/d30 realised P&L by settlement time in the current mode, from one pair of store
        reads. (None, None) if no data or error."""
        try:
            return await asyncio.wait_for(pnlsvc.compute_pair(self.store, utcnow(), env=self.mode), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("pnl compute failed: %s", type(e).__name__)
            return None, None

    # ---- bracket arbitrage
    def _arb_skip_reason(self) -> str | None:
        if not self.settings.runner_enabled:
            return "runner_disabled"
        if self.arb_desired != "running":
            return "stopped"
        if self.arb_kill_switch():
            return "kill_switch"
        if self.arb_lock.locked():
            return "scan_running"
        if self._shutting_down:
            return "shutting_down"
        if not self._fence_ok():
            return "standby"                     # R1: only the lease holder scans (and executes)
        return None

    # ---- shadow mode (read only; docs/SHADOW_CONTRACT.md)
    def shadow_enabled(self) -> bool:
        """Env SHADOW_ENABLED (startup snapshot) AND the cloud setting shadow_enabled."""
        return bool(getattr(self.settings, "shadow_enabled", False)) and self.cloud.get("shadow_enabled") is True

    def _shadow_skip_reason(self) -> str | None:
        """Pure local reads; never takes a lock."""
        if not self.settings.runner_enabled:
            return "runner_disabled"
        if self._shutting_down:
            return "shutting_down"
        if self.role() != "leader":
            return "standby"
        if self._stopping:
            return "stop_running"
        if self._strategy_running or self.running_pass or self._pass_lock.locked():
            return "pass_running"
        if self._shadow_cf is not None and not self._shadow_cf.done():
            return "busy"                       # an abandoned worker is still finishing: never queue behind it
        return None

    async def _shadow_loop(self) -> None:
        await asyncio.sleep(self.poll_minutes() * 30)        # offset by half an interval from the passes
        while True:
            res: dict | None = None
            try:
                res = await self.shadow_cycle()
            except Exception as e:  # noqa: BLE001
                log.error("shadow cycle failed: %s", type(e).__name__)
            await asyncio.sleep(self._shadow_next_sleep_s(res))

    def _shadow_next_sleep_s(self, res: dict | None) -> float:
        """Seconds until the next shadow cycle: the full poll interval after a completed cycle (or a skip that will
        not clear soon, such as disabled), at most SHADOW_RETRY_S after a transient skip (not leader yet, a pass or
        an earlier worker busy, Stop running, an upstream read error) or an exception."""
        full = float(self.poll_minutes() * 60)
        if res is None or (not res.get("ran") and res.get("reason") in SHADOW_TRANSIENT):
            return min(full, float(SHADOW_RETRY_S))
        return full

    def _shadow_sync(self, inp: Any, strategy: Any, deadline: float, cached: bool = False) -> Any:
        md = self.shadow_factory()
        if cached:                                       # variants on: overlapping cadences share public reads
            md = self._shadow_cache.view(md)
        return shadowsvc.run_cycle(md, strategy, inp, deadline=deadline)

    async def _shadow_store(self, coro, timeout: float = shadowsvc.SHADOW_STORE_TIMEOUT_S):
        return await asyncio.wait_for(coro, timeout)

    async def _shadow_compute(self, now: datetime, standin: bool, interval: int = shadowsvc.BASELINE_INTERVAL) -> Any:
        st = self.store
        variants = shadowsvc.variant_intervals(self.cloud)
        k = 1 + len(variants)                            # wave 2: more cadences share the stored episodes
        mine = (lambda eps: [e for e in (eps or []) if shadowsvc.interval_of(e) == interval])
        all_open = list(await self._shadow_store(st.shadow_open_episodes()) or [])
        open_eps = mine(all_open)
        retire = ([e for e in all_open if shadowsvc.interval_of(e) not in (interval, *variants)]
                  if interval == shadowsvc.BASELINE_INTERVAL else [])   # a variant turned off: end its open bids
        # trade reads and settlement do not depend on the cadence: whichever cycle runs drains the shared queues
        unread = await self._shadow_store(st.shadow_ended_unread(shadowsvc.SHADOW_MAX_TRADE_READS + 1))
        unsettled = await self._shadow_store(st.shadow_unsettled(now, shadowsvc.SHADOW_MAX_SETTLE_READS * 10))
        new_today = await self._shadow_store(st.shadow_count_new_since(shadowsvc.day_start(now)))
        held = mine(await self._shadow_store(st.shadow_recent(shadowsvc.SHADOW_HELD_SCAN * k, "ended")))  # F1
        ms = self.ms
        cfg = {**ms.DEFAULTS, **botfiles.public_config(self.config)}
        inp = shadowsvc.CycleInput(series=list(cfg.get("series") or []), run_budget=float(cfg["run_budget"]),
                                   max_order=float(cfg["max_order"]), open_eps=list(open_eps or []),
                                   ended_unread=list(unread or []), unsettled=list(unsettled or []),
                                   new_today=int(new_today or 0), now=now, standin=standin,
                                   held=list(held or []), capital=shadowsvc.capital_of(self.cloud),
                                   max_open_frac=shadowsvc.open_frac_of(
                                       cfg, getattr(self.settings, "max_open_frac", None)),
                                   interval=interval, retire=retire)
        if interval != shadowsvc.BASELINE_INTERVAL:
            inp.new_cap = int(shadowsvc.SHADOW_DAILY_NEW_CAP * shadowsvc.SHADOW_VARIANT_NEW_SHARE)
        elif shadowsvc.arb_enabled() and not standin:    # S3 (env SHADOW_ARB, default off): read-only, own budgets
            inp.arb = shadowsvc.arb_input(arbsvc.current(self.config))
        deadline = time.monotonic() + shadowsvc.SHADOW_BUDGET_S - shadowsvc.DEADLINE_MARGIN_S
        self._shadow_cf = self._shadow_executor.submit(self._shadow_sync, inp, ms, deadline, bool(variants))
        return await asyncio.wrap_future(self._shadow_cf)

    async def _shadow_refresh_summary(self, now: datetime, standin: bool) -> None:
        """SH-2: only the fields summarize() reads, only scored episodes (yes/no result with a fill), capped."""
        st = self.store
        fields = shadowsvc.SUMMARY_FIELDS
        cap, cap_open = shadowsvc.SHADOW_SUMMARY_MAX_ROWS, shadowsvc.SHADOW_SUMMARY_MAX_OPEN
        settled = list(await self._shadow_store(st.shadow_settled(
            now - timedelta(days=shadowsvc.SHADOW_SUMMARY_DAYS), fields=fields, limit=cap, scored_only=True)) or [])
        open_eps = list(await self._shadow_store(st.shadow_open_episodes(fields=fields, limit=cap_open)) or [])
        intervals = shadowsvc.summary_intervals(self.cloud)
        passes = await self._shadow_store(st.shadow_recent_passes(300 * (1 + len(intervals or []))))
        fut = asyncio.get_running_loop().run_in_executor(
            self._shadow_executor, functools.partial(shadowsvc.build_summary, settled + open_eps, list(passes or []),
                                                     now, enabled=True, standin=standin, intervals=intervals))
        summary = await asyncio.wait_for(fut, shadowsvc.SHADOW_BUDGET_S / 3)
        if len(settled) >= cap or len(open_eps) >= cap_open:
            summary["status"]["truncated"] = True
        await shadowsvc.carry_arb(st, summary, now)      # S3: keep the stored arbitrage buckets
        if not self._shadow_leader_ok():
            return                                       # SH-4: lost the lease meanwhile: write nothing
        await self._shadow_store(st.set_shadow_summary(summary))
        self._shadow_summary_utc = now

    def _shadow_leader_ok(self) -> bool:
        """SH-4: still the leader and not shutting down, re-checked right before every shadow write."""
        return self.role() == "leader" and not self._shutting_down

    def _shadow_summary_due(self, now: datetime, pass_doc: dict) -> bool:
        """SH-2: refresh the cached summary when something settled, else at most every SHADOW_SUMMARY_EVERY."""
        last = self._shadow_summary_utc
        return (last is None or bool(pass_doc.get("settled")) or bool(pass_doc.get("voided"))
                or now - last >= shadowsvc.SHADOW_SUMMARY_EVERY)

    async def _shadow_off(self) -> None:
        """Shadow was turned off: end the open episodes (shadow_off, at their last observation). Store only."""
        try:
            open_eps = await self._shadow_store(self.store.shadow_open_episodes())
            docs = shadowsvc.end_all(open_eps or [], "shadow_off", utcnow())
            if docs:
                await self._shadow_store(self.store.shadow_upsert_episodes(docs))
            self._shadow_active = False
        except Exception as e:  # noqa: BLE001
            log.warning("shadow close-out failed: %s", type(e).__name__)

    def _shadow_variant_skip(self, interval: int) -> str | None:
        """2A-3 wave 2 load guard for a variant cadence (pure local reads, never a lock)."""
        if interval not in shadowsvc.variant_intervals(self.cloud):
            return "variant_off"                         # env SHADOW_VARIANTS off or not in shadow_intervals
        if botfiles.is_standin():
            return "standin"
        last = self._shadow_last_duration_s
        if last is not None and last > shadowsvc.SHADOW_VARIANT_LOAD_S:
            return "load"                                # the last shadow pass ran long: let the baseline recover
        return None

    async def shadow_cycle(self, interval: int = shadowsvc.BASELINE_INTERVAL) -> dict:
        """One shadow cycle if allowed. Reads and computes inside SHADOW_BUDGET_S (a timeout or error writes no
        episodes), then writes the episodes in one upsert, the pass doc and the cached summary. Never raises.
        `interval` other than the baseline (10) runs a variant cadence (wave 2): its own episodes and pass doc,
        tagged `interval`; no arbitrage scan and no summary refresh."""
        variant = interval != shadowsvc.BASELINE_INTERVAL
        why = self._shadow_skip_reason()
        if why is None and not self.shadow_enabled():
            why = "disabled"
            if self._shadow_active and not variant:
                await self._shadow_off()
        if why is None and variant:
            why = self._shadow_variant_skip(interval)
        if why is None and self._shadow_cycle_lock.locked():
            why = "busy"                                 # another cadence's cycle is writing: never queue behind it
        if variant:
            self.shadow_variant_state[interval] = {**self.shadow_variant_state.get(interval, {}), "last_reason": why}
        else:
            self.shadow_state["last_reason"] = why
        if why:
            return {"ran": False, "reason": why}
        async with self._shadow_cycle_lock:
            return await self._shadow_cycle_locked(interval)

    async def _shadow_cycle_locked(self, interval: int) -> dict:
        variant = interval != shadowsvc.BASELINE_INTERVAL
        state = (self.shadow_variant_state.setdefault(interval, {"last_cycle_utc": None, "last_error": None})
                 if variant else self.shadow_state)
        now = utcnow()
        t0 = time.monotonic()
        standin = botfiles.is_standin()
        try:
            res = await asyncio.wait_for(self._shadow_compute(now, standin, interval), shadowsvc.SHADOW_BUDGET_S)
        except Exception as e:  # noqa: BLE001   (incl. the budget timeout): nothing half-done is written
            name = type(e).__name__
            state.update(last_cycle_utc=now, last_error=name)
            self._shadow_last_duration_s = round(time.monotonic() - t0, 3)
            log.warning("shadow cycle discarded: %s", name)
            if not self._shadow_leader_ok():
                return {"ran": False, "reason": "standby"}
            fail = {"ts": now, "ok": False, "duration_s": round(time.monotonic() - t0, 3), "markets": 0,
                    "books": 0, "quotes": 0, "new_eps": 0, "ended_eps": 0, "trade_reads": 0, "settled": 0,
                    "truncated": False, "errors": [name]}
            if variant:
                fail["interval"] = interval
            with contextlib.suppress(Exception):
                await self._shadow_store(self.store.add_shadow_pass(fail), 5)
            return {"ran": False, "reason": "error", "error": name}
        if self._shutting_down:
            return {"ran": False, "reason": "shutting_down"}
        if not self._shadow_leader_ok():                 # SH-4: lease lost mid-cycle: no episode or pass writes
            state.update(last_cycle_utc=now, last_error="standby")
            return {"ran": False, "reason": "standby"}
        self._shadow_active = True
        pass_doc = dict(res.pass_doc)
        if variant:
            pass_doc["interval"] = interval
        try:
            if res.upserts:
                await self._shadow_store(self.store.shadow_upsert_episodes(res.upserts))
        except Exception as e:  # noqa: BLE001
            pass_doc["ok"] = False
            pass_doc["errors"] = list(pass_doc.get("errors") or []) + [f"store:{type(e).__name__}"]
        pass_doc["duration_s"] = round(time.monotonic() - t0, 3)
        self._shadow_last_duration_s = pass_doc["duration_s"]
        if self._shadow_leader_ok():
            with contextlib.suppress(Exception):
                await self._shadow_store(self.store.add_shadow_pass(pass_doc), 5)
        if variant:
            state.update(last_cycle_utc=now, last_error=None if pass_doc["ok"] else "partial")
            return {"ran": True, "pass": pass_doc}
        if getattr(res, "arb", None) is not None and self._shadow_leader_ok():
            try:
                await shadowsvc.persist_arb(self.store, res.arb, now)       # S3: daily buckets in the summary doc
            except Exception as e:  # noqa: BLE001
                log.warning("shadow arb persist failed: %s", type(e).__name__)
        if self._shadow_summary_due(now, pass_doc):
            try:
                await self._shadow_refresh_summary(now, standin)
            except Exception as e:  # noqa: BLE001
                log.warning("shadow summary refresh failed: %s", type(e).__name__)
        self.shadow_state.update(last_cycle_utc=now, last_error=None if pass_doc["ok"] else "partial")
        return {"ran": True, "pass": pass_doc}

    async def _shadow_variant_loop(self) -> None:
        """2A-3 wave 2: each variant cadence in cloud shadow_intervals (minus the baseline) runs on its own clock.
        A transient skip (busy, a pass or Stop running, the load guard) retries at the next tick."""
        last: dict[int, float] = {}
        while True:
            await asyncio.sleep(SHADOW_VARIANT_TICK_S)
            try:
                for iv in shadowsvc.variant_intervals(self.cloud):
                    if time.monotonic() - last.get(iv, float("-inf")) < iv * 60:
                        continue
                    res = await self.shadow_cycle(interval=iv)
                    if res.get("ran") or res.get("reason") not in SHADOW_TRANSIENT + ("load",):
                        last[iv] = time.monotonic()
            except Exception as e:  # noqa: BLE001
                log.error("shadow variant cycle failed: %s", type(e).__name__)

    async def _arb_loop(self) -> None:
        while True:
            try:
                await self.arb_scan()
            except Exception as e:  # noqa: BLE001
                log.error("arb loop failed: %s", type(e).__name__)
            try:
                secs = int(self.arb_settings()["arb_poll_seconds"])
            except (TypeError, ValueError, KeyError):
                secs = arbsvc.DEFAULT_POLL_SECONDS
            await asyncio.sleep(max(arbsvc.POLL_MIN, min(arbsvc.POLL_MAX, secs)))

    def _arb_live_ok(self, gen: int) -> bool:
        """Re-checked right before every arb execution (a Stop may land mid-scan)."""
        return (self.settings.runner_enabled and self.arb_desired == "running" and self.arb_mode == "live"
                and gen == self._stop_gen
                and not self._stopping and self.live_allowed() and not self.arb_kill_switch()
                and not self._shutting_down and self._fence_ok(min_remaining_s=FENCE_MARGIN_S))   # R1 fence

    def _arb_demo_wanted(self) -> bool:
        """Demo-execute is asked for: arb in demo, the maker in demo, and the arb_demo_execute setting on."""
        return (self.arb_mode == "demo" and self.mode == "demo"
                and self.arb_settings().get("arb_demo_execute") is True)

    def _arb_complete_wanted(self) -> bool:
        """H3-B: the arb_complete_legs setting is on (demo-execute only; never consulted for live)."""
        return self.arb_settings().get("arb_complete_legs") is True

    def _arb_demo_ok(self, gen: int) -> bool:
        """Same re-checks as _arb_live_ok, for demo-execute. Never true while the maker or arb is LIVE."""
        return (self.settings.runner_enabled and self.arb_desired == "running" and self._arb_demo_wanted()
                and gen == self._stop_gen and not self._stopping and not self.arb_kill_switch()
                and not self._shutting_down and self._fence_ok(min_remaining_s=FENCE_MARGIN_S))   # R1 fence

    @staticmethod
    def _demo_keys_ok() -> bool:
        """Demo keys are set, and they are not the production key id (a key pasted into both fields)."""
        kid, pk = mode_keys("demo")
        prod_kid, _ = mode_keys("live")
        return bool(kid and pk) and kid != prod_kid

    def arb_exec_kind(self) -> str | None:
        """How this scan's opportunities would be handled: "live", "demo-execute", or None (log-only)."""
        if self.arb_mode == "live" and self.live_allowed():
            return "live"
        if self._arb_demo_wanted():
            return "demo-execute"
        return None

    def _arb_execute_sync(self, scanner: Any, op: Any, gen: int, kind: str = "live",
                          budget: dict | None = None) -> str:
        """One opportunity through the gates, then arb_scanner.execute. kind: "live" (prod) or "demo-execute"
        (demo exchange only). `budget` holds this scan's spend and today's spend state (risk gate)."""
        demo = kind == "demo-execute"
        ok_now = self._arb_demo_ok if demo else self._arb_live_ok
        if kind not in ("live", "demo-execute") or not ok_now(gen):
            return "skipped: stopped"
        if (budget or {}).get("abandoned"):
            return "skipped: scan abandoned"            # H3S-2: its results would never be stored or alerted
        if demo:
            if not self._demo_keys_ok():
                return "skipped: no demo keys"
            client = self.kalshi_factory("demo")
            if client is None:
                return "skipped: no demo keys"
            if not arbsvc.demo_base_ok(client):
                return "skipped: not the demo exchange"       # asserted on the client, not on the mode name
        else:
            client = self.kalshi_factory("live")
            if client is None:
                return "skipped: no prod keys"
        try:
            cash = float((client.get_balance() or {}).get("balance") or 0) / 100.0
        except Exception as e:  # noqa: BLE001
            return f"skipped: balance {type(e).__name__}"
        try:
            frac = float(self.config.get("max_open_frac") or 0.5)
        except (TypeError, ValueError):
            frac = 0.5
        frac = min(frac, float(self.settings.max_open_frac))
        cap = cash * frac
        if op.cost > cap:
            return f"skipped: cost {op.cost:.2f} > cap {cap:.2f}"
        # account risk gate (services/riskgate.py): ceiling, series cap, scan cap, day cap. Fails closed.
        try:
            body = client.get_positions() if callable(getattr(client, "get_positions", None)) else None
            truncated = isinstance(body, dict) and body.get("truncated") is True
            by_series = riskgate.exposure_by_series((body or {}).get("market_positions")
                                                    if isinstance(body, dict) else None)
        except Exception:  # noqa: BLE001
            return "skipped: gate positions unreadable"
        if truncated:
            return "skipped: gate positions truncated"     # S2: a partial read understates exposure (fail closed)
        if budget is None or budget.get("day") is None:
            return "skipped: gate day spend unknown"
        with self._arb_budget_lock:
            day = budget["day"]
            held = budget.get("held")
            if held is None:
                return "skipped: gate held sets unknown"
            if riskgate.opposite_set_held(op.event, op.kind, held):
                return "skipped: gate opposite set held on event"
            ok, why = riskgate.check_arb(op.cost, riskgate.series_of(op.event), by_series, cash, frac,
                                         budget["scan"], day["amount"], arbsvc.gate_caps(self.arb_settings()))
            if not ok:
                return f"skipped: gate {why}"
            if not ok_now(gen):
                return "skipped: stopped"
            if budget.get("abandoned"):
                return "skipped: scan abandoned"
            if demo:
                if not (self.mode == "demo" and arbsvc.demo_base_ok(client)):
                    return "skipped: not the demo exchange"
            elif not (live_allowed_settings(self.settings) and all(mode_keys("live"))):
                return "skipped: live not allowed"       # S-19: second live check right before execution
            budget["scan"] += float(op.cost)             # reserved before sending (a partial set still counts)
            day["amount"] = round(day["amount"] + float(op.cost), 6)
        kw: dict = {}
        if demo and self._arb_complete_wanted() and _scanner_can_complete(scanner):
            kw["complete"] = ArbCompletionGate(self, client, op.event, gen, budget, frac)   # H3-B, demo only
        try:
            out = str(scanner.execute(client, op, **kw))  # arb_scanner trips its own kill switch on a partial set
            if out.startswith(riskgate.HELD_PREFIXES):
                with self._arb_budget_lock:
                    budget["held"].setdefault(str(op.event), set()).add(str(op.kind))
            return out
        except Exception as e:  # noqa: BLE001
            # an order error mid-set may leave a leg filled: legging risk, so trip the arb kill switch
            try:
                self.arb_kill_path.write_text(json.dumps({"reason": "legging risk: order error mid-set",
                                                          "event": op.event, "error": type(e).__name__,
                                                          "utc": utcnow().isoformat()}), encoding="utf-8")
            except OSError:
                pass
            with self._arb_budget_lock:                  # H3S-1: an errored set may hold legs
                budget["held"].setdefault(str(op.event), set()).add(str(op.kind))
            return f"ERROR {type(e).__name__}"

    def _arb_scan_sync(self, cfg: dict, live: Any, gen: int, data_env: str,
                       budget: dict | None = None) -> list[tuple[Any, str]]:
        """live: True/"live" (prod), "demo-execute" (demo exchange), or False/None (log-only)."""
        kind = "live" if live is True else (live if live in ("live", "demo-execute") else None)
        scanner = arbsvc.scanner()
        pub = self.public_factory(data_env)
        if pub is not None:
            pub = ctsvc.RecordingPublic(pub, self.close_times, data_env)   # market close times at no extra call
        # client=None: scan_once itself never executes; execution goes through the gated path above
        ops = scanner.scan_once(pub, None, list(cfg["arb_series"]), float(cfg["arb_max_contracts"]),
                                float(cfg["arb_min_profit"]))
        if kind is not None and budget is None:
            budget = {"scan": 0.0, "day": None}          # no day state: the gate refuses (fail closed)
        return [(op, self._arb_execute_sync(scanner, op, gen, kind, budget) if kind else "log-only") for op in ops]

    async def _arb_day_state(self, kind: str) -> dict | None:
        """Today's (UTC) arb spend for `kind`, in memory. Rebuilt from the store's executed opportunities the first
        time it is needed after a restart or a leadership change (so a deploy never resets it); a rebuild never goes
        below the in-memory amount. None if the store cannot be read (the gate then refuses)."""
        now = utcnow()
        today = now.date().isoformat()
        mark = (self.lease.generation, self.lease.transitions)
        cur = self._arb_day.get(kind)
        if cur is not None and cur["date"] == today and cur["mark"] == mark:
            return cur
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            # S2A1-1: a server-side sum with no row limit (sent sets only), so skip docs can never push them out
            spent = float(await asyncio.wait_for(self.store.arb_spend_since(midnight, kind), 5))
        except Exception as e:  # noqa: BLE001
            log.warning("arb day spend rebuild failed: %s", type(e).__name__)
            return None
        if not math.isfinite(spent) or spent < 0:
            log.warning("arb day spend rebuild returned an unusable value")
            return None
        if cur is not None and cur["date"] == today:
            spent = max(spent, float(cur["amount"]))
        state = {"date": today, "amount": round(spent, 6), "mark": mark}
        self._arb_day[kind] = state
        return state

    async def _arb_held_state(self, kind: str) -> dict | None:
        """{event: {"YES"/"NO"}} of executed (FILLED / PARTIAL) sets of `kind` since 00:00 UTC YESTERDAY (an event
        can span a UTC midnight), in memory. Rebuilt from the store's executed opportunities the first time it is
        needed after a restart, a leadership change or a new UTC day; a same-day rebuild is merged with memory. None if the
        store cannot be read or the read hits its row limit (the opposite-set gate then refuses: fail closed)."""
        now = utcnow()
        today = now.date().isoformat()
        mark = (self.lease.generation, self.lease.transitions)
        cur = self._arb_held.get(kind)
        if cur is not None and cur["date"] == today and cur["mark"] == mark:
            return cur["events"]
        since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        try:
            docs = await asyncio.wait_for(self.store.list_arb_opportunities(
                since=since, limit=ARB_HELD_READ_MAX, executed_only=True), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("arb held sets rebuild failed: %s", type(e).__name__)
            return None
        if not isinstance(docs, list) or len(docs) >= ARB_HELD_READ_MAX:
            log.warning("arb held sets rebuild unusable (truncated or bad)")
            return None
        events = riskgate.held_sets(docs, kind)
        if cur is not None and cur["date"] == today:        # same day (leadership change): never forget a set
            for ev, kinds in cur["events"].items():
                events.setdefault(ev, set()).update(kinds)
        self._arb_held[kind] = {"date": today, "mark": mark, "events": events}
        return events

    def _arb_skip_persist_ok(self, event: Any, executed: str, now: datetime) -> bool:
        """S2A1-1: a `skipped: ...` result is stored at most once per (event, reason kind) per ARB_SKIP_PERSIST_EVERY,
        so a stale book seen on every scan cannot flood the store (or push real sets out of a rebuild). Everything
        else (log-only, FILLED, PARTIAL, ERROR) is always handed to the store. In memory only, bounded."""
        if not str(executed).startswith("skipped:"):
            return True
        kind = re.sub(r"[-+]?\$?\d[\d.,]*", "#", str(executed))[:120]     # numbers out: one key per reason kind
        key = (str(event)[:80], kind)
        seen = self._arb_skip_seen
        last = seen.get(key)
        if last is not None and now - last < ARB_SKIP_PERSIST_EVERY:
            return False
        if len(seen) >= ARB_SKIP_SEEN_MAX:
            cutoff = now - ARB_SKIP_PERSIST_EVERY
            for k in [k for k, t in seen.items() if t < cutoff]:
                del seen[k]
            while len(seen) >= ARB_SKIP_SEEN_MAX:
                del seen[next(iter(seen))]                   # oldest inserted first
        seen.pop(key, None)
        seen[key] = now
        return True

    async def arb_scan(self) -> dict:
        """One arbitrage scan if arb is on. Log-only unless arb_mode=live AND the env allows live, or demo-execute
        is on in demo (demo exchange only). Every execution passes the account risk gate first."""
        if self.arb_desired == "running" and self.arb_kill_switch():
            await self._arb_tripped()
        why = self._arb_skip_reason()
        if why:
            return {"scanned": False, "reason": why}
        async with self.arb_lock:
            gen = self._stop_gen
            kind = self.arb_exec_kind()
            live = kind == "live"
            data_env = "live" if live else self.mode
            budget = None
            if kind is not None:
                budget = {"scan": 0.0, "day": await self._arb_day_state(kind),
                          "held": await self._arb_held_state(kind)}
            self.arb_running_scan = True
            try:
                # CA-2: own 1-worker pool. A scan abandoned at the timeout keeps that worker until it ends, so the
                # next scan queues behind it (and times out) instead of taking another thread from the default pool.
                fut = asyncio.get_running_loop().run_in_executor(
                    self._arb_executor, self._arb_scan_sync, self.arb_settings(), kind, gen, data_env, budget)
                results = await asyncio.wait_for(fut, timeout=ARB_SCAN_TIMEOUT_S)
                self.arb_last_error = None
            except BaseException as e:  # noqa: BLE001
                if budget is not None:
                    # H3S-2: the worker thread may keep running; it must start no new set and no completion
                    budget["abandoned"] = True
                if not isinstance(e, Exception):
                    raise
                self.arb_last_error = type(e).__name__
                log.error("arb scan failed: %s", type(e).__name__)
                return {"scanned": False, "reason": "error", "error": type(e).__name__}
            finally:
                self.arb_running_scan = False
                self.arb_last_scan_utc = utcnow()
            self.arb_scans += 1
            self.arb_found += len(results)
            for op, executed in results:
                doc = arbsvc.opportunity_doc(op, executed, kind or "log-only")
                doc["ts"] = utcnow()
                self.arb_recent.appendleft(doc)
                if self._arb_skip_persist_ok(op.event, executed, doc["ts"]):
                    try:
                        await asyncio.wait_for(self.store.add_arb_opportunity(dict(doc)), timeout=5)
                    except Exception as e:  # noqa: BLE001
                        log.error("arb log write failed: %s", type(e).__name__)
                if executed.startswith(("PARTIAL", "ERROR")):
                    await self.alerter.send("Arbitrage legging risk: arb kill switch tripped",
                                            f"{op.event}: {executed}", priority="urgent")
                elif executed.startswith("FILLED"):
                    where = " (demo exchange)" if kind == "demo-execute" else ""
                    done = " (missing legs completed)" if executed.startswith("FILLED (completed)") else ""
                    await self.alerter.send("Arbitrage executed",
                                            f"{op.event} {op.kind} profit ${op.profit}{where}{done}")
            if self.arb_kill_switch() and self.arb_desired == "running":
                await self._arb_tripped()
            return {"scanned": True, "found": len(results)}

    async def _arb_tripped(self) -> None:
        """The arb kill switch is on disk (legging) while arb is on: turn arb off and persist it, because Render's
        disk is ephemeral and the file alone would not survive a restart. Never raises."""
        self.arb_desired = "stopped"
        self.arb_mode = "demo"
        reason = arbsvc.kill_reason(self.arb_kill_path)
        try:
            await asyncio.wait_for(self.store.set_state(by="arb-kill-switch", arb_desired="stopped",
                                                        arb_mode="demo"), timeout=5)
        except Exception as e:  # noqa: BLE001
            log.error("arb stopped not persisted: %s", type(e).__name__)
        with contextlib.suppress(Exception):
            await self.alerter.send("Arbitrage stopped: kill switch", f"reason: {reason}", priority="urgent")

    async def set_arb(self, *, enabled: bool | None, live: bool | None, by: str, ack_legging: bool = False) -> dict:
        """Arb on/off and the arb-live switch (the route enforces fresh 2FA and typed LIVE). Store first (fail-safe).
        Raises PermissionError("live_not_allowed" | "arb_legging_kill"), StopInProgress, or the store's error."""
        gen = self._stop_gen
        desired = None if enabled is None else ("running" if enabled else "stopped")
        mode = None if live is None else ("live" if live else "demo")
        if desired == "stopped" and mode is None:
            mode = "demo"            # turning arb off also drops live: re-enabling live needs LIVE + 2FA again
        turning_on =desired == "running" or mode == "live"
        if turning_on and not self.settings.runner_enabled:
            raise RunnerDisabled("runner_disabled")
        if turning_on and self._stopping:
            raise StopInProgress("stop_in_progress")
        if mode == "live" and not self.live_allowed():
            raise PermissionError(self.live_block_reason() or "live_not_allowed")
        if desired == "running" and arbsvc.kill_reason(self.arb_kill_path) in ("legging", "unknown") \
                and not ack_legging:
            raise PermissionError("arb_legging_kill")
        if desired is None and mode is None:
            return {"arb_desired": self.arb_desired, "arb_mode": self.arb_mode}
        await self.store.set_state(by=by, arb_desired=desired, arb_mode=mode)
        if turning_on and (gen != self._stop_gen or self._stopping):
            # a Stop landed during our write, which may have reached the store after Stop's: re-assert stopped
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.store.set_state(by="stop-wins", arb_desired="stopped",
                                                            arb_mode="demo"), timeout=5)
            raise StopInProgress("stopped_during_start")
        # no await between the check above and these assignments, so a Stop can't interleave
        if mode is not None:
            self.arb_mode = mode
        if desired is not None:
            if desired == "running":
                with contextlib.suppress(FileNotFoundError):
                    self.arb_kill_path.unlink()
            self.arb_desired = desired
        if mode == "live":
            self._spawn(self.alerter.send("Arbitrage LIVE enabled", f"arb_mode=live set by {by}", priority="urgent"))
        return {"arb_desired": self.arb_desired, "arb_mode": self.arb_mode}

    def arb_status(self) -> dict:
        return {"state": self.arb_state(), "running": self.arb_running_scan, "kill_switch": self.arb_kill_switch(),
                "kill_reason": arbsvc.kill_reason(self.arb_kill_path), "last_scan_utc": iso(self.arb_last_scan_utc),
                "last_error": self.arb_last_error, "live_allowed": self.live_allowed(), "scans": self.arb_scans,
                "found": self.arb_found, "since_restart_utc": iso(self.started_utc),
                # demo-execute (additive): true while sets would be placed on the DEMO exchange
                "demo_execute": self.arb_state() != "stopped" and self.arb_exec_kind() == "demo-execute"}

    # ---- controls
    async def stop(self, by: str) -> dict:
        """Never raises. Stops the maker AND arbitrage.
        1) in-memory flags + stop generation 2) kill switches 3) cancel mm- orders 4) store (retried in the
        background). Nothing before step 3 awaits, touches the store, Kalshi or the network."""
        self._stop_gen += 1          # first statement: an in-flight Start / arb enable sees this and backs off
        self._stopping += 1
        errors: list[str] = []
        cancelled = 0
        try:
            self.desired = "stopped"
            self.arb_desired = "stopped"
            self.arb_mode = "demo"
            self.last_stop_utc = utcnow()          # queue telemetry: orders gone after this were stopped
            stamp = f"stopped by {by} at {utcnow().isoformat()}\n"
            try:
                self.kill_path.parent.mkdir(parents=True, exist_ok=True)
                self.kill_path.write_text(stamp, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                errors.append(f"kill_switch: {type(e).__name__}")
            try:
                if arbsvc.kill_reason(self.arb_kill_path) is None:     # keep a legging record if one is there
                    self.arb_kill_path.parent.mkdir(parents=True, exist_ok=True)
                    self.arb_kill_path.write_text(stamp, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                errors.append(f"arb_kill_switch: {type(e).__name__}")
            try:
                cancelled = await asyncio.wait_for(self._cancel_in_stop_pool(self.mode), timeout=CANCEL_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                errors.append(f"cancel: {type(e).__name__}")
            self._stop_pending = True
            try:
                await asyncio.wait_for(self._write_stopped(by), timeout=5)
                if self.desired == "stopped":
                    self._stop_pending = False
                    self._stop_persisted = True
            except Exception as e:  # noqa: BLE001
                errors.append(f"store: {type(e).__name__} (retrying in background)")
                self._ensure_stop_retry(by)
            try:
                log.warning("STOP by %s: cancelled=%s errors=%s", by, cancelled, errors)
                self._spawn(self.alerter.send("Bot stopped", f"by {by}; cancelled {cancelled}; errors {errors}"))
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001   (belt and braces: Stop never raises)
            errors.append(f"stop: {type(e).__name__}")
        finally:
            self._stopping -= 1
        return {"desired": "stopped", "kill_switch": self.kill_switch(), "arb_kill_switch": self.arb_kill_switch(),
                "cancelled": cancelled, "errors": errors,
                # toast fields (UX item 39); same values as above, new names, nothing removed
                "stopped_at": iso(self.last_stop_utc), "orders_cancelled": cancelled}

    async def _write_stopped(self, by: str) -> None:
        """One store write that stops both strategies (maker-only on a store without the arb fields)."""
        try:
            await self.store.set_state(by=by, desired="stopped", arb_desired="stopped", arb_mode="demo")
        except TypeError:
            await self.store.set_state(by=by, desired="stopped")

    def _cancel_in_stop_pool(self, mode: str) -> asyncio.Future:
        """CA-2: _cancel_resting on Stop's dedicated pool (never the default executor). If that pool was already shut
        down (a Stop racing the very end of shutdown), fall back to a one-off thread so the cancel still runs."""
        loop = asyncio.get_running_loop()
        try:
            return loop.run_in_executor(self._stop_executor, self._cancel_resting, mode)
        except RuntimeError:
            fut = loop.create_future()

            def _run() -> None:
                try:
                    n = self._cancel_resting(mode)
                    loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(n))
                except BaseException as e:  # noqa: BLE001
                    loop.call_soon_threadsafe(lambda: fut.done() or fut.set_exception(e))
            threading.Thread(target=_run, name="stop-fallback", daemon=True).start()
            return fut

    def _cancel_resting(self, mode: str) -> int:
        client = self.kalshi_factory(mode)
        if client is None:
            return 0
        n = 0
        for o in client.get_orders(status="resting"):
            if str(o.get("client_order_id", "")).startswith("mm-"):
                try:
                    client.cancel_order(o["order_id"], market_ticker=o.get("ticker"))
                    n += 1
                    q = getattr(self, "_stop_cancelled", None)
                    if q is not None:
                        q.append(str(o["order_id"]))      # queue telemetry: outcome "stopped" (in-memory only)
                except Exception as e:  # noqa: BLE001
                    log.warning("cancel failed for an order: %s", type(e).__name__)
        return n

    def _ensure_stop_retry(self, by: str) -> None:
        try:
            if self._state_retry is None or self._state_retry.done():
                self._state_retry = asyncio.create_task(self._retry_stopped(by))
        except Exception:  # noqa: BLE001
            pass

    RETRY_FIRST_DELAY = 5

    async def _retry_stopped(self, by: str) -> None:
        """Retries the desired=stopped store write forever (backoff to 300 s) until it lands or Start wins."""
        delay = self.RETRY_FIRST_DELAY
        while self.desired == "stopped":
            await asyncio.sleep(delay)
            if self.desired != "stopped":
                return
            try:
                await asyncio.wait_for(self._write_stopped(by), timeout=10)
                self._stop_pending = False
                self._stop_persisted = True
                log.warning("Stop persisted to store after retry")
                return
            except Exception:  # noqa: BLE001
                delay = min(delay * 2, 300)

    async def start_trading(self, by: str) -> dict:
        """Removes the kill switch and sets desired=running (store first: fail-safe).
        Stop always wins a race: if a Stop is running, or one ran while the store write was in flight, the start is
        refused (StopInProgress) and stopped is re-asserted in the store. Arbitrage is not started by this."""
        if not self.settings.runner_enabled:
            raise RunnerDisabled("runner_disabled")
        if self.mode == "live" and botfiles.is_standin():
            # never start real-money trading on the public stand-in (mode is normally demo here: LIVE is refused
            # by set_mode and never restored after a restart; this is the belt-and-braces check)
            raise PermissionError("strategy_not_installed")
        gen = self._stop_gen
        if self._stopping:
            raise StopInProgress("stop_in_progress")
        # reload the config from the store (the owner may have fixed it since the last load). A config that still
        # fails validation keeps the bot stopped with the reason; a good one clears a stale "invalid config" error.
        # Stop never goes through here.
        if not await self._load_config():
            raise ConfigInvalid(f"invalid_config: {self.last_error or 'stored config failed validation'}"[:400])
        if isinstance(self.last_error, str) and self.last_error.startswith(INVALID_CONFIG_PREFIXES):
            self.last_error = None
        if gen != self._stop_gen or self._stopping:
            raise StopInProgress("stopped_during_start")
        kw ={"arb_desired": "stopped"} if self.arb_desired == "stopped" else {}   # keep the store's arb state honest
        try:
            await self.store.set_state(by=by, desired="running", **kw)
        except TypeError:
            await self.store.set_state(by=by, desired="running")
        if gen != self._stop_gen or self._stopping:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._write_stopped("stop-wins"), timeout=5)
            raise StopInProgress("stopped_during_start")
        # from here to the return there is no await, so a Stop cannot interleave
        self._start_epoch += 1
        self._stop_persisted = False       # the store now says running: a later kill file is a real Stop again
        if self._state_retry is not None and not self._state_retry.done():
            self._state_retry.cancel()     # the store now says running; a late stopped-retry must not override
        self._stop_pending = False
        with contextlib.suppress(FileNotFoundError):
            self.kill_path.unlink()
        self.desired = "running"
        self.store_ok = True
        return {"desired": "running"}

    async def set_mode(self, mode: str, by: str) -> dict:
        if mode not in ("demo", "live"):
            raise ValueError("mode must be demo or live")
        if mode == "live" and not self.live_allowed():
            raise PermissionError(self.live_block_reason() or "live_not_allowed")
        await self.store.set_state(by=by, mode=mode)
        self.mode = mode
        if mode == "live":
            await self.alerter.send("LIVE trading enabled", f"mode=live set by {by}", priority="urgent")
        return {"mode": mode}

    async def apply_config(self, cfg: dict) -> None:
        """Writes a config the API already validated. Re-validated here too (S-12); raises ValueError([msgs])."""
        cfg = botfiles.validate_stored(cfg, self.settings)
        botfiles.write_config_file(self.config_path, cfg)
        self.config = botfiles.public_config(cfg)

    async def run_monitor(self, by: str) -> dict:
        """CA-2: single-flight. A run while another is in progress returns {"error": "monitor_running"} at once
        (the route answers 409) instead of parking another thread behind it."""
        if self._monitor_running:
            return {"summary": None, "error": "monitor_running"}
        self._monitor_running = True      # set before the first await: no second run can slip in
        try:
            return await self._run_monitor(by)
        finally:
            self._monitor_running = False

    async def _run_monitor(self, by: str) -> dict:
        mon = botfiles.monitor()
        before = botfiles.read_config_file(self.config_path)
        summary: dict = {}
        try:
            def _go():
                buf = io.StringIO()
                with kalshi_env(self.mode), contextlib.redirect_stdout(buf):
                    mon.main(["--apply"])
                return buf.getvalue()
            self.logs.add(await asyncio.get_running_loop().run_in_executor(self._monitor_executor, _go))
            logs_dir = Path(mon.LOGS)
            with contextlib.suppress(OSError, ValueError):
                summary = json.loads((logs_dir / "monitor_summary.json").read_text(encoding="utf-8"))
            report = None
            with contextlib.suppress(OSError):
                report = (logs_dir / "monitor_report.txt").read_text(encoding="utf-8")
            if self.kill_switch() and self.desired == "running":
                # maker_monitor --apply created MAKER_KILL_SWITCH (every market confidently losing). Make it a
                # real Stop (persisted, orders cancelled), since the file alone is lost on Render restarts.
                await self.stop(by="monitor")
            doc = {"ts": utcnow(), "by": by, "summary": summary, "report_text": report}
            await self.store.add_monitor_summary(doc)
            self.last_monitor_utc = doc["ts"]
            after = botfiles.read_config_file(self.config_path)
            if after and after != before:
                try:
                    after = botfiles.validate_stored(after, self.settings)          # S-12
                except ValueError as ve:
                    if before:
                        botfiles.write_config_file(self.config_path, before)
                    msgs = ve.args[0] if ve.args and isinstance(ve.args[0], list) else [str(ve)]
                    await self._invalid_config_stop("monitor wrote an invalid config (reverted)", msgs)
                    return {"summary": summary, "error": "invalid_config"}
                await self.store.save_config(after, before=before, by="monitor")
                self.config = after
                await self.alerter.send("Monitor changed config", str(summary.get("applied", "")))
            return {"summary": summary}
        except Exception as e:  # noqa: BLE001
            log.error("monitor failed: %s", e)
            await self.alerter.send("Monitor failed", f"{type(e).__name__}: {e}")
            self.last_monitor_utc = utcnow()      # don't retry every pass
            return {"summary": summary or None, "error": f"{type(e).__name__}"}

    def status(self) -> dict:
        return {"running_pass": self.running_pass, "last_run_utc": iso(self.last_run_utc),
                "last_error": self.last_error, "next_due_utc": iso(self.next_due_utc()),
                "kill_switch": self.kill_switch(), "desired": self.desired, "mode": self.mode,
                "live_allowed": self.live_allowed(), "live_block_reason": self.live_block_reason(),
                "arb": {"state": self.arb_state(), "last_scan_utc": iso(self.arb_last_scan_utc)},
                "fills_sync": {"last_utc": iso(self.last_fills_sync_utc), "error": self.fills_sync_error},
                # last pass's write-path summary (additive): cancel/place failures, paused, capped series
                "write_report": self.last_write_report,
                # R4 version handshake + R1 lease (additive fields)
                **ver.handshake(), **self.lease_status(), "instance_id": self.instance_id}

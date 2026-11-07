"""Document shapes, constants and normalisation helpers shared by MemoryStore and MongoStore.

Both stores run every incoming document through the ``prepare_*`` helpers here, so they store
(and return) exactly the same thing: plain dicts, no ``_id``, timezone-aware UTC datetimes
truncated to millisecond precision (what BSON keeps).
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

DB_NAME_DEFAULT = "kalshi_control"
RETENTION_DAYS = 180                      # TTL for heartbeats, journal_rows, account_snapshots
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600

# Collection names
STATE = "state"
CLOUD_SETTINGS = "cloud_settings"
CONFIG_VERSIONS = "config_versions"
HEARTBEATS = "heartbeats"
JOURNAL_ROWS = "journal_rows"
ACCOUNT_SNAPSHOTS = "account_snapshots"
FILLS = "fills"
MONITOR_SUMMARIES = "monitor_summaries"
AUDIT_LOG = "audit_log"
USERS = "users"
REVOKED_TOKENS = "revoked_tokens"
SETTLEMENTS = "settlements"
SHADOW_EPISODES = "shadow_episodes"
SHADOW_PASSES = "shadow_passes"
ARB_OPPORTUNITIES = "arb_opportunities"
QUOTE_EPISODES = "quote_episodes"
ALERT_HISTORY = "alert_history"
PERIODS = "periods"
CASH_FLOWS = "cash_flows"
RECON_REPORTS = "recon_reports"
PUSH_SUBSCRIPTIONS = "push_subscriptions"
# Release process (Sprint 1): leader lease, signed-handover replay guard, schema marker, pending releases.
RUNNER_LEASE = "runner_lease"
HANDOVER_NONCES = "handover_nonces"
META = "meta"
RELEASES = "releases"
LEASE_ID = "leader"                       # the single runner_lease doc
META_SCHEMA_ID = "schema"                 # the single meta doc holding the data schema version
SCHEMA_VERSION = 1                        # everything that existed before the release process; changes are additive only
HANDOVER_NONCE_TTL_SECONDS = 3600         # a consumed nonce is remembered for 1 h (tokens expire far sooner)
RELEASE_RETENTION_SECONDS = 30 * 24 * 3600
PENDING_RELEASE_TTL_SECONDS = 30 * 60     # a pending release can be approved for 30 minutes
RELEASE_DECISIONS = ("approved", "rejected")
COLLECTIONS = (STATE, CLOUD_SETTINGS, CONFIG_VERSIONS, HEARTBEATS, JOURNAL_ROWS, ACCOUNT_SNAPSHOTS,
               FILLS, MONITOR_SUMMARIES, AUDIT_LOG, USERS, REVOKED_TOKENS, SETTLEMENTS, ARB_OPPORTUNITIES,
               QUOTE_EPISODES, ALERT_HISTORY, PUSH_SUBSCRIPTIONS, RUNNER_LEASE, HANDOVER_NONCES, META, RELEASES,
               SHADOW_EPISODES, SHADOW_PASSES, PERIODS, CASH_FLOWS, RECON_REPORTS)

# Daily reconciliation against Kalshi (services/kalshi_recon.py): one report per (mode, local day), replaced when the
# same day is checked again. Read-only data about the books; no secrets. Additive change: SCHEMA_VERSION stays 1.
RECON_RETENTION_DAYS = 90
RECON_RETENTION_SECONDS = RECON_RETENTION_DAYS * 24 * 3600
RECON_DIFFS_MAX = 500                     # differences kept per report (the rest is counted, not stored)

# Performance periods and cash flows (Sprint 2A-1). Both small, permanent (no TTL), append-style.
# Period 1 is IMPLICIT (id "p1", from the start of data to the first marker) and is never stored: a stored marker
# is the period that STARTS at its started_at. "p1" is reserved. Additive change: SCHEMA_VERSION stays 1.
IMPLICIT_PERIOD_ID = "p1"
PERIOD_NAME_MAX = 60
PERIOD_NOTE_MAX = 200
CASH_FLOW_KINDS = ("deposit", "withdrawal")
CASH_FLOW_MAX = 100000.0

# Alert history (UX sprint item 37): one small doc per alert the system sent (ntfy and/or web push).
# No secrets (title/body are redacted before they get here); body capped at ALERT_BODY_MAX chars.
ALERT_RETENTION_DAYS = 90
ALERT_RETENTION_SECONDS = ALERT_RETENTION_DAYS * 24 * 3600
ALERT_TYPES = ("fill", "settled", "daily", "error", "stop", "breaker", "test", "system",
               "weekly", "arb_legging")    # S3 item 18 weekly summary, item 5 urgent legging alert (additive)
ALERT_TITLE_MAX = 120
ALERT_BODY_MAX = 200
# Web push subscriptions (item 45): sensitive (the endpoint is a bearer capability for pushing to the device).
# Never logged, never returned by the API, never exported by tools/pull_data.py. Keyed on sub_id = sha256(endpoint).
PUSH_SUBSCRIPTIONS_MAX = 10

STATE_ID = "bot"
CLOUD_ID = "cloud"

# Journal CSV columns, in the order kalshi_bot/maker_strategy.journal() writes them.
JOURNAL_FIELDS = ("utc", "event", "ticker", "side", "price", "count", "cost", "cell", "note")

DESIRED_VALUES = ("running", "stopped")
MODE_VALUES = ("demo", "live")

# State used when nothing has been saved yet (fail-safe: stopped, demo), for the maker and for the
# bracket-arbitrage loop. Both live in the one state doc so a single Stop write stops both.
DEFAULT_STATE = {"desired": "stopped", "mode": "demo", "arb_desired": "stopped", "arb_mode": "demo"}
STATE_FIELDS = ("desired", "mode", "arb_desired", "arb_mode", "updated_utc", "updated_by")

# Bracket-arbitrage log: an identical log-only sighting within this window is collapsed into the
# previous doc (seen_count += 1, last_seen updated) instead of inserting a new one.
ARB_DEDUPE_SECONDS = 120
# Shorter TTL than the other logs: worst case (a book that changes every 15 s poll all day) is ~4 MB/day,
# so 180 d could exceed Atlas M0's 512 MB. 45 d still covers the app's d30 arb P&L window.
ARB_RETENTION_DAYS = 45
ARB_RETENTION_SECONDS = ARB_RETENTION_DAYS * 24 * 3600
ARB_LOG_ONLY = "log-only"
ARB_SPEND_PREFIXES = ("FILLED", "PARTIAL", "ERROR")   # executed values that count as money spent

# Queue-position telemetry (control_api/services/queuepos.py): one doc per maker order ("quote episode").
# 45-day TTL on ts (= when the order joined the book). Worst case ~1,200 episodes/day (queuepos.DAILY_NEW_CAP)
# x ~1.1 KB = ~60 MB + ~8 MB of indexes at 45 days; typical use is a few MB.
QUEUE_RETENTION_DAYS = 45
QUEUE_RETENTION_SECONDS = QUEUE_RETENTION_DAYS * 24 * 3600
# Fields fixed when the episode is created (MongoStore writes them with $setOnInsert, so a later write can't
# move an episode to another ticker, price or join time).
QUOTE_EPISODE_IMMUTABLE = ("order_id", "ts", "env", "series", "ticker", "side", "book_side", "price",
                           "order_price", "count", "close_time")
# close_time (market close, aware UTC; services/closetime.py): never stored as null. prepare_* drop a null or
# unreadable value, so $set / $setOnInsert can't erase a known close time. An episode created before its close
# time was known gets it once from Store.set_quote_episode_close_time (writes only while the field is missing).
CLOSE_TIME = "close_time"

# Shadow mode (services/shadow.py): hypothetical bids ("episodes") and one doc per cycle ("passes"), both with a
# 30-day TTL on ts. Episode _id = ep_id (sha256 hex), so a write is an idempotent upsert. The cached summary
# lives in the meta collection under SHADOW_SUMMARY_ID (no TTL, one doc). Worst case ~1,500 episodes/day
# (SHADOW_DAILY_NEW_CAP) x ~1 KB = ~45 MB + indexes at 30 days; passes are ~0.3 KB x ~100/day.
SHADOW_RETENTION_DAYS = 30
SHADOW_RETENTION_SECONDS = SHADOW_RETENTION_DAYS * 24 * 3600
SHADOW_SUMMARY_ID = "shadow_summary"
SHADOW_STATES = ("open", "ended", "settled")
SHADOW_DATE_FIELDS = ("ts", "join_ts", "last_seen_ts", "end_ts", "through_ts", "close_time", "settled_ts",
                      "settle_checked_ts")
# result: "yes" | "no" | "void" (SH-1: a market closed without a yes/no result; excluded from the estimate) | None
SHADOW_RESULTS = ("yes", "no", "void")
SHADOW_SCORED_RESULTS = ("yes", "no")

# Sprint 2A-3 additive episode fields. Old episodes lack them; reads fill the defaults (a partial write never
# resets a stored value, so the defaults are applied on read only).
SHADOW_EPISODE_DEFAULTS = {"through_vol": None, "capped": False, "cap_reason": None, "interval": 10,
                           "fee_mode": None, "methodology": 1}   # methodology 1 = legacy (uncapped), 2 = capped
SHADOW_INTERVALS_ALLOWED = (5, 7, 10)
SHADOW_CAP_REASON_MAX = 200


def shadow_with_defaults(doc: dict, fields=None) -> dict:
    """Copy of an episode with the missing 2A-3 fields defaulted (only the requested ones under a projection)."""
    out = dict(doc)
    for k, v in SHADOW_EPISODE_DEFAULTS.items():
        if (fields is None or k in fields) and k not in out:
            out[k] = v
    return out


def _check_shadow_fields(out: dict) -> None:
    tv = out.get("through_vol")
    if "through_vol" in out and tv is not None:
        if isinstance(tv, bool) or not isinstance(tv, (int, float)) or tv != tv or tv < 0:
            raise ValueError("through_vol must be a number >= 0 or null")
    if "capped" in out and not isinstance(out["capped"], bool):
        raise ValueError("capped must be true/false")
    for k in ("cap_reason", "fee_mode"):
        if out.get(k) is not None:
            if not isinstance(out[k], str):
                raise ValueError(f"{k} must be text or null")
            out[k] = out[k][:SHADOW_CAP_REASON_MAX]
    if "methodology" in out:
        m = out["methodology"]
        if isinstance(m, bool) or not isinstance(m, int) or m not in (1, 2):
            raise ValueError("methodology must be 1 or 2")
    if "interval" in out:
        iv = out["interval"]
        if isinstance(iv, bool) or not isinstance(iv, int) or not 1 <= iv <= 1440:
            raise ValueError("interval must be whole minutes")


# Defaults/ranges for cloud_settings (see DASHBOARD_PLAN.md "Keep-awake"). The API validates ranges;
# the store only persists. Exposed here so backend/seed share one source.
DEFAULT_CLOUD_SETTINGS = {
    "keepalive_minutes": 10,
    "pinger_minutes": 5,
    "pinger_enabled": True,
    "silent_alert_minutes": 30,
    "cronjob_job_id": None,
    "pinger_sync": {"ok": None, "ts": None, "error": None},
    "shadow_enabled": False,            # shadow mode: read-only estimate of what the strategy would bid
    "shadow_capital": 150,              # capital the shadow estimate assumes (20-100000)
    "shadow_intervals": [10],           # shadow cadences in minutes (subset of {5, 7, 10}, max 3)
}

# Fields the store never accepts (secrets must never reach the database).
FORBIDDEN_KEYS = frozenset({"password", "totp_secret", "api_key", "private_key", "jwt_secret",
                            "cronjob_api_key", "mongodb_uri", "tick_token"})


# ---------------------------------------------------------------- time helpers
def utcnow() -> datetime:
    return norm_dt(datetime.now(timezone.utc))


def norm_dt(d: datetime) -> datetime:
    """Aware UTC datetime with millisecond precision (naive values are taken as UTC)."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    return d.replace(microsecond=(d.microsecond // 1000) * 1000)


def to_utc(value: Any) -> datetime | None:
    """datetime | ISO-8601 string | epoch seconds (int/float) -> aware UTC datetime; None/'' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return norm_dt(value)
    if isinstance(value, bool):
        raise TypeError("bool is not a timestamp")
    if isinstance(value, (int, float)):
        return norm_dt(datetime.fromtimestamp(float(value), tz=timezone.utc))
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return norm_dt(datetime.fromisoformat(s))
    raise TypeError(f"cannot convert {type(value).__name__} to datetime")


def _safe_to_utc(value: Any) -> datetime | None:
    try:
        return to_utc(value)
    except (TypeError, ValueError):
        return None


def normalize(obj: Any) -> Any:
    """Deep copy with every datetime normalised (aware UTC, ms precision). Tuples become lists (as BSON)."""
    if isinstance(obj, datetime):
        return norm_dt(obj)
    if isinstance(obj, dict):
        return {str(k): normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize(v) for v in obj]
    return copy.deepcopy(obj)


ATLAS_M0_LIMIT_BYTES = 512 * 1024 * 1024  # Atlas M0 storage cap
TTL_COLLECTIONS = (HEARTBEATS, JOURNAL_ROWS, ACCOUNT_SNAPSHOTS)   # collections whose ts_ttl index must exist (read-only check)

SNAPSHOT_ENVS = ("demo", "live")
SNAPSHOT_ENV_DEFAULT = "demo"             # backfill rule: rows stored before the env tag existed are demo (no live before 11/24)


def snapshot_env(doc: dict | None) -> str:
    """The env of an account snapshot. THE one place the backfill rule lives: a missing or unknown env reads as demo."""
    e = (doc or {}).get("env")
    return e if e in SNAPSHOT_ENVS else SNAPSHOT_ENV_DEFAULT


def snapshot_env_query(env: str | None) -> dict:
    """Mongo filter for an env (None = no filter). 'demo' also matches rows with no env field (the backfill rule)."""
    if env is None:
        return {}
    if env == SNAPSHOT_ENV_DEFAULT:
        return {"$or": [{"env": env}, {"env": {"$exists": False}}, {"env": {"$nin": list(SNAPSHOT_ENVS)}}]}
    return {"env": env}


def with_snapshot_env(doc: dict) -> dict:
    """Copy of a snapshot doc whose env is always set (backfill applied on read)."""
    out = dict(doc)
    out["env"] = snapshot_env(doc)
    return out


def filter_snapshots(snaps: Iterable[dict], env: str | None) -> list[dict]:
    """Snapshots of one env (None = all). Works on docs from any source; rows without env count as demo."""
    return [s for s in snaps if env is None or snapshot_env(s) == env]


def strip_id(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


# User fields that may only move forward (SECURITY_REVIEW S-18): a stale write can never roll
# back the session epoch or let a used TOTP step be replayed. Stores apply them with $max.
# An explicit None still resets the field (tests / dev tooling only; auth never writes None).
MONOTONIC_USER_FIELDS = ("sessions_valid_after", "last_totp_step")


def split_monotonic(fields: dict) -> tuple[dict, dict]:
    """-> (plain $set fields, $max fields) for a user write."""
    plain, mono = {}, {}
    for k, v in fields.items():
        (mono if k in MONOTONIC_USER_FIELDS and v is not None else plain)[k] = v
    return plain, mono


def check_no_secrets(doc: dict) -> None:
    bad = FORBIDDEN_KEYS.intersection(k.lower() for k in doc)
    if bad:
        raise ValueError(f"refusing to store secret field(s): {sorted(bad)}")


# ---------------------------------------------------------------- row hash / document builders
def row_hash(row: dict) -> str:
    """sha1 over the journal CSV fields (in JOURNAL_FIELDS order, str() of each, missing -> '').

    Values are joined with the ASCII unit separator, so ("a,b","c") and ("a","b,c") differ.
    The same CSV line always hashes the same, whether values are strings (csv.DictReader) or numbers.
    """
    parts = []
    for f in JOURNAL_FIELDS:
        v = row.get(f, "")
        parts.append("" if v is None else str(v))
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def prepare_journal_row(row: dict) -> dict:
    """journal_rows doc: the 9 CSV fields as given (strings from csv) + row_hash + ts (parsed from utc)."""
    doc = {f: row.get(f, "") for f in JOURNAL_FIELDS}
    doc = normalize(doc)
    doc["row_hash"] = row.get("row_hash") or row_hash(doc)
    doc["ts"] = _safe_to_utc(row.get("ts")) or _safe_to_utc(row.get("utc")) or utcnow()
    return doc


def prepare_ts_doc(doc: dict, *, ts_fallbacks: Iterable[str] = ()) -> dict:
    """Copy of doc with `ts` set (from doc['ts'], then any fallback field, else now)."""
    out = normalize(dict(doc))
    out.pop("_id", None)
    ts = _safe_to_utc(out.get("ts"))
    for f in ts_fallbacks:
        if ts is None:
            ts = _safe_to_utc(out.get(f))
    out["ts"] = ts or utcnow()
    return out


def _close_time_field(doc: dict) -> dict:
    """close_time as an aware UTC datetime; a null / unreadable value is dropped (never stored, never erases)."""
    if CLOSE_TIME in doc:
        ct = _safe_to_utc(doc.get(CLOSE_TIME))
        if ct is None:
            doc.pop(CLOSE_TIME)
        else:
            doc[CLOSE_TIME] = ct
    return doc


def prepare_fill(fill: dict) -> dict:
    """fills doc: Kalshi fill as given + `ts` (from `ts` [datetime/ISO/epoch s], else `created_time`, else now).
    Requires `fill_id` (falls back to `trade_id`)."""
    fid = fill.get("fill_id") or fill.get("trade_id")
    if not fid:
        raise ValueError("fill without fill_id")
    doc = prepare_ts_doc(fill, ts_fallbacks=("created_time",))
    doc["fill_id"] = str(fid)
    return _close_time_field(doc)


def to_float(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def arb_signature(doc: dict) -> str:
    """sha1 over what makes two sightings 'the same opportunity': event, kind, contracts, profit, cost, legs."""
    legs = [[str(x) for x in leg] if isinstance(leg, (list, tuple)) else str(leg) for leg in (doc.get("legs") or [])]
    parts = [str(doc.get("event", "")), str(doc.get("kind", "")), repr(to_float(doc.get("contracts"))),
             repr(to_float(doc.get("profit"))), repr(to_float(doc.get("cost"))), json.dumps(legs)]
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def prepare_arb_opportunity(op: Any) -> dict:
    """arb_opportunities doc from a dict or an arb_scanner.Opportunity dataclass.
    Adds ts (given, else `utc`, else now), last_seen=ts, seen_count=1, sig, executed (default 'log-only')."""
    if not isinstance(op, dict):
        op = {k: getattr(op, k) for k in ("event", "kind", "contracts", "profit", "cost", "legs", "sum_best")
              if hasattr(op, k)}
    if not op.get("event") or op.get("kind") not in ("NO", "YES"):
        raise ValueError("arb opportunity needs an event and kind 'NO'|'YES'")
    doc = prepare_ts_doc(op, ts_fallbacks=("utc",))
    doc.pop("utc", None)
    doc["executed"] = str(doc.get("executed") or ARB_LOG_ONLY)
    doc["sig"] = arb_signature(doc)
    doc["last_seen"] = doc["ts"]
    doc["seen_count"] = 1
    return doc


def _opt_note(value: Any) -> str | None:
    if value is None:
        return None
    note = str(value).strip()
    if len(note) > PERIOD_NOTE_MAX:
        raise ValueError(f"note must be at most {PERIOD_NOTE_MAX} characters")
    return note or None


def prepare_period(doc: dict) -> dict:
    """periods doc: {id, name, started_at, note, created_by}. name 1-60 chars (trimmed), note <= 200 or None,
    started_at aware UTC (default now), id uuid hex unless given ("p1" is reserved for the implicit first period).
    Raises ValueError."""
    import uuid
    name = str(doc.get("name") or "").strip()
    if not 1 <= len(name) <= PERIOD_NAME_MAX:
        raise ValueError(f"name must be 1-{PERIOD_NAME_MAX} characters")
    if doc.get("started_at") is None:
        started = utcnow()
    else:
        started = _safe_to_utc(doc["started_at"])
        if started is None:
            raise ValueError("started_at is not a valid time")
    pid = str(doc.get("id") or uuid.uuid4().hex)[:40]
    if pid == IMPLICIT_PERIOD_ID:
        raise ValueError(f"period id {IMPLICIT_PERIOD_ID!r} is reserved for the implicit first period")
    return {"id": pid, "name": name, "started_at": started, "note": _opt_note(doc.get("note")),
            "created_by": str(doc.get("created_by") or "")[:64]}


def prepare_cash_flow(doc: dict) -> dict:
    """cash_flows doc: {id, ts, kind, amount, note, created_by}. kind deposit|withdrawal, amount a finite float
    with 0 < x <= 100000 (never a bool), ts aware UTC (default now). Raises ValueError."""
    import math
    import uuid
    kind = doc.get("kind")
    if kind not in CASH_FLOW_KINDS:
        raise ValueError(f"kind must be one of {CASH_FLOW_KINDS}")
    raw = doc.get("amount")
    try:
        if isinstance(raw, bool):
            raise TypeError
        amount = float(raw)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number") from None
    if not math.isfinite(amount) or not 0 < amount <= CASH_FLOW_MAX:
        raise ValueError(f"amount must be greater than 0 and at most {CASH_FLOW_MAX:g}")
    if doc.get("ts") is None:
        ts = utcnow()
    else:
        ts = _safe_to_utc(doc["ts"])
        if ts is None:
            raise ValueError("ts is not a valid time")
    return {"id": str(doc.get("id") or uuid.uuid4().hex)[:40], "ts": ts, "kind": kind, "amount": amount,
            "note": _opt_note(doc.get("note")), "created_by": str(doc.get("created_by") or "")[:64]}


def prepare_quote_episode(doc: dict) -> dict:
    """quote_episodes doc: normalised copy with order_id (str, required) and ts (join time; from ts, else
    joined_utc, else now)."""
    if not doc.get("order_id"):
        raise ValueError("quote episode without order_id")
    out = prepare_ts_doc(doc, ts_fallbacks=("joined_utc",))
    out["order_id"] = str(doc["order_id"])
    return _close_time_field(out)


def prepare_shadow_episode(doc: dict) -> dict:
    """shadow_episodes doc: normalised copy keyed on ep_id (str, required). Date fields become aware UTC
    datetimes (an explicit null stays null: end_ts / settled_ts null is meaningful). `ts` (the TTL clock) is
    taken from ts, else join_ts; when neither is given it is left out so a partial update never moves it."""
    if not doc.get("ep_id"):
        raise ValueError("shadow episode without ep_id")
    out = normalize(dict(doc))
    out.pop("_id", None)
    out["ep_id"] = str(doc["ep_id"])
    for f in SHADOW_DATE_FIELDS:
        if out.get(f) is not None:
            out[f] = _safe_to_utc(out[f])
    if out.get("ts") is None:
        out.pop("ts", None)
        if out.get("join_ts") is not None:
            out["ts"] = out["join_ts"]
    _check_shadow_fields(out)
    return out


def prepare_shadow_pass(doc: dict) -> dict:
    """shadow_passes doc: normalised copy with ts (given, else now)."""
    return prepare_ts_doc(doc)


def prepare_alert(doc: dict) -> dict:
    """alert_history doc: {id, ts, type, title, body, priority, channels}. Unknown type -> 'system'; title/body
    truncated (callers redact first). `id` is generated here (uuid4 hex) unless given."""
    import uuid
    out = prepare_ts_doc({k: doc.get(k) for k in ("ts", "type", "title", "body", "priority", "channels", "id")
                          if doc.get(k) is not None})
    t = str(out.get("type") or "system")
    out["type"] = t if t in ALERT_TYPES else "system"
    out["title"] = str(out.get("title") or "")[:ALERT_TITLE_MAX]
    out["body"] = str(out.get("body") or "")[:ALERT_BODY_MAX]
    out["priority"] = str(out.get("priority") or "default")[:16]
    ch = out.get("channels")
    out["channels"] = [str(c)[:16] for c in ch][:4] if isinstance(ch, list) else []
    out["id"] = str(out.get("id") or uuid.uuid4().hex)[:40]
    return out


def prepare_recon_report(doc: dict) -> dict:
    """recon_reports doc: the report as given plus `ts` (from `checked_at`, else now). Keyed on (mode, day).
    ValueError for a bad mode or day, or a secret-looking top-level field. Differences beyond RECON_DIFFS_MAX are
    dropped (`differences_truncated` counts them)."""
    import re
    if doc.get("mode") not in MODE_VALUES:
        raise ValueError(f"recon report mode must be one of {MODE_VALUES}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(doc.get("day") or "")):
        raise ValueError("recon report day must be YYYY-MM-DD")
    check_no_secrets(doc)
    out = prepare_ts_doc(dict(doc), ts_fallbacks=("checked_at",))
    diffs = out.get("differences")
    diffs = diffs if isinstance(diffs, list) else []
    out["differences"] = diffs[:RECON_DIFFS_MAX]
    out["differences_truncated"] = max(0, len(diffs) - RECON_DIFFS_MAX)
    out["ok"] = bool(out.get("ok"))
    return out


def push_sub_id(endpoint: str) -> str:
    return hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest()


def prepare_push_subscription(doc: dict) -> dict:
    """push_subscriptions doc: {sub_id, endpoint, p256dh, auth, created_utc, ua}. Validation is the route's job."""
    endpoint = str(doc.get("endpoint") or "")
    if not endpoint:
        raise ValueError("push subscription without endpoint")
    return {"sub_id": push_sub_id(endpoint), "endpoint": endpoint, "p256dh": str(doc.get("p256dh") or ""),
            "auth": str(doc.get("auth") or ""), "ua": str(doc.get("ua") or "")[:128],
            "created_utc": _safe_to_utc(doc.get("created_utc")) or utcnow()}


def prepare_settlement(row: dict) -> dict:
    """settlements doc: Kalshi /portfolio/settlements row as given + `ts` (from `ts`, else `settled_time`).
    Keyed on `ticker` (a market settles once)."""
    if not row.get("ticker"):
        raise ValueError("settlement without ticker")
    doc = prepare_ts_doc(row, ts_fallbacks=("settled_time",))
    doc["ticker"] = str(row["ticker"])
    return _close_time_field(doc)


# Shapes (documentation; plain dicts are used at runtime)
STATE_DOC = {"desired": "running|stopped", "mode": "demo|live", "arb_desired": "running|stopped",
             "arb_mode": "demo|live", "updated_utc": "datetime", "updated_by": "str"}
CLOUD_SETTINGS_DOC = {**DEFAULT_CLOUD_SETTINGS, "updated_utc": "datetime", "updated_by": "str"}
CONFIG_VERSION_DOC = {"version": "int", "ts": "datetime", "by": "str", "before": "dict|None", "after": "dict"}
HEARTBEAT_DOC = {"ts": "datetime", "ok": "bool", "error": "str?", "duration_s": "float", "mode": "str",
                 "desired": "str", "quotes": "int"}
JOURNAL_ROW_DOC = {**{f: "str" for f in JOURNAL_FIELDS}, "row_hash": "sha1 hex", "ts": "datetime"}
ACCOUNT_SNAPSHOT_DOC = {"ts": "datetime", "cash": "float", "exposure": "float", "headroom": "float"}
FILL_DOC = {"fill_id": "str", "ts": "datetime", "close_time": "datetime? (market close)",
            "source": "bot|you (services/strategies.py)", "strategy": "maker|arb|None",
            "source_by": "order|journal|episode|arb_log|default", "user_note": "str? (<= 200 chars, owner journal)",
            "...": "Kalshi fill fields"}
ALERT_DOC = {"id": "uuid hex", "ts": "datetime (TTL 90 d)", "type": "|".join(ALERT_TYPES), "title": "str <= 120",
             "body": "str <= 200", "priority": "str", "channels": "[ntfy|push]"}
PERIOD_DOC = {"id": "str (unique)", "name": "str 1-60", "started_at": "datetime (indexed; no TTL)",
              "note": "str <= 200 | None", "created_by": "str"}
CASH_FLOW_DOC = {"id": "str (unique)", "ts": "datetime (indexed; no TTL)", "kind": "deposit|withdrawal",
                 "amount": "float, 0 < x <= 100000", "note": "str <= 200 | None", "created_by": "str"}
RECON_REPORT_DOC = {"mode": "demo|live", "day": "YYYY-MM-DD (local day checked)", "ts": "datetime (checked_at; TTL 90 d)",
                    "ok": "bool", "checked_at": "iso str", "differences": "[{kind, id, store_value, kalshi_value, delta, "
                    "severity}] (<= 500)", "differences_truncated": "int", "totals": "dict", "...": "tz, tolerance"}
PUSH_SUBSCRIPTION_DOC = {"sub_id": "sha256(endpoint) (unique)", "endpoint": "https URL (sensitive)",
                         "p256dh": "b64url (sensitive)", "auth": "b64url (sensitive)", "ua": "str",
                         "created_utc": "datetime"}
MONITOR_SUMMARY_DOC = {"ts": "datetime", "generated_utc": "iso str", "...": "maker_monitor summary"}
AUDIT_DOC = {"ts": "datetime", "user": "str|None", "action": "str", "ok": "bool", "details": "any",
             "ip": "str", "ua": "str"}
USER_DOC = {"username": "str", "password_hash": "argon2 str", "totp_secret_enc": "Fernet token str",
            "failed_logins": "int", "locked_until": "datetime|None", "last_totp_step": "int|None"}
REVOKED_TOKEN_DOC = {"jti": "str", "exp": "datetime"}
SETTLEMENT_DOC = {"ticker": "str", "ts": "datetime", "market_result": "yes|no", "revenue": "cents",
                  "close_time": "datetime? (market close)",
                  "...": "Kalshi settlement fields"}
QUOTE_EPISODE_DOC = {"order_id": "str (unique)", "ts": "datetime (joined the book; TTL 45 d)", "env": "demo|live",
                     "series": "str", "ticker": "str", "side": "yes|no (bought)", "book_side": "bid|ask",
                     "close_time": "datetime? (market close; set on insert or once by backfill, then immutable)",
                     "price": "float (paid for side)", "order_price": "float (V2 YES price)", "count": "float",
                     "status": "open|closed",
                     "outcome": "None|filled|repriced|cancelled|stopped|expired",
                     "queue_ahead_at_join": "float|None", "join_source": "kalshi|book|None",
                     "join_level_size": "float|None", "kalshi_queue_at_join": "float|None",
                     "last_obs": "{ts, level, traded, through, lo, hi, nc, kq, rem}|None",
                     "obs": "[last 4 obs]", "obs_count": "int", "filled_count": "float",
                     "fill_ids": "[str] (first fill first)", "first_fill_utc": "datetime|None",
                     "upper_at_fill": "float|None", "front_fill": "bool|None", "front_eval": "bool",
                     "gone_utc": "datetime|None", "closed_utc": "datetime|None", "updated_utc": "datetime"}
ARB_OPPORTUNITY_DOC = {"ts": "datetime (first seen)", "last_seen": "datetime", "seen_count": "int",
                       "event": "str", "kind": "NO|YES", "contracts": "float", "profit": "float $",
                       "cost": "float $", "sum_best": "float", "legs": "[[ticker, side, limit, n]]",
                       "executed": "log-only | FILLED {...} | PARTIAL {...} | ERROR ...", "mode": "demo|live?",
                       "sig": "sha1 hex"}


RUNNER_LEASE_DOC = {"_id": "leader", "holder": "str (instance id)", "version": "str (build)",
                    "generation": "int (fencing token)", "expires_at": "datetime", "acquired_at": "datetime",
                    "handover": "dict|None (signed token envelope)"}
HANDOVER_NONCE_DOC = {"nonce": "str (unique)", "ts": "datetime (TTL 1 h)"}
META_SCHEMA_DOC = {"_id": "schema", "version": "int", "updated_utc": "datetime"}
RELEASE_DOC = {"id": "uuid hex (unique)", "version": "str", "commit": "str", "summary": "str|dict", "ci": "str|dict",
               "status": "pending|approved|rejected", "created_at": "datetime (TTL 30 d)", "expires_at": "datetime",
               "by": "str? (decider)", "at": "datetime? (decision time)"}


def retention_cutoff(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=RETENTION_DAYS)

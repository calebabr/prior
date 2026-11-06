"""Async storage interface (DASHBOARD_PLAN.md "Interface contract -> Store") and the in-process MemoryStore.

Conventions (both implementations):
  * every method is async;
  * returned dicts are fresh copies, never contain `_id`, and hold tz-aware UTC datetimes (ms precision);
  * list_* methods return newest first (by `ts`, or `version` for config versions);
  * `since` filters are inclusive (ts >= since);
  * retention TTLs (180 d) are enforced by Atlas only; MemoryStore does not expire history
    (it does honour token-revocation expiry, since that affects behaviour).
"""
from __future__ import annotations

import abc
import asyncio
import copy
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import schemas as S


class Store(abc.ABC):
    # ---- health
    @abc.abstractmethod
    async def ping(self) -> bool:
        """True if the backing database answers. Never raises."""

    @abc.abstractmethod
    async def ensure_indexes(self) -> None:
        """Create indexes/TTLs. Idempotent."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    # ---- state
    @abc.abstractmethod
    async def get_state(self) -> dict | None:
        """{desired, mode, arb_desired, arb_mode, updated_utc, updated_by} or None if never set.
        Fields missing from an older stored doc are filled from schemas.DEFAULT_STATE."""

    @abc.abstractmethod
    async def set_state(self, *, by: str, desired: str | None = None, mode: str | None = None,
                        arb_desired: str | None = None, arb_mode: str | None = None) -> dict:
        """Update any of desired/mode (maker) and arb_desired/arb_mode (bracket arbitrage) in ONE atomic
        write (unspecified fields keep their value; defaults stopped/demo). Stop passes
        desired="stopped", arb_desired="stopped" together. Raises ValueError on an invalid value."""

    # ---- config
    @abc.abstractmethod
    async def get_config(self) -> dict | None:
        """`after` of the highest config version, or None."""

    @abc.abstractmethod
    async def save_config(self, after: dict, *, before: dict | None, by: str) -> int:
        """Append a config version; returns its number (1, 2, ...)."""

    @abc.abstractmethod
    async def list_config_versions(self, limit: int = 20) -> list[dict]:
        """[{version, ts, by, before, after}] newest first."""

    # ---- heartbeats
    @abc.abstractmethod
    async def add_heartbeat(self, doc: dict) -> None: ...

    @abc.abstractmethod
    async def last_heartbeat(self) -> dict | None: ...

    @abc.abstractmethod
    async def list_heartbeats(self, *, since: datetime | None = None, limit: int = 1000) -> list[dict]:
        """Heartbeats with ts >= since, newest first."""

    # ---- journal
    @abc.abstractmethod
    async def add_journal_rows(self, rows: Iterable[dict]) -> int:
        """Insert journal CSV rows; idempotent on row_hash. Returns the number newly inserted."""

    @abc.abstractmethod
    async def list_journal(self, *, since: datetime | None = None, limit: int = 200,
                           events: Iterable[str] | None = None, fields: Iterable[str] | None = None) -> list[dict]:
        """Journal rows with ts >= since (optionally only these events), newest first.
        `fields`: return only these keys (projection)."""

    # ---- account snapshots
    @abc.abstractmethod
    async def add_account_snapshot(self, doc: dict) -> None: ...

    @abc.abstractmethod
    async def list_account_snapshots(self, *, since: datetime | None = None, limit: int = 10000,
                                     env: str | None = None) -> list[dict]:
        """Newest first (reverse for a chart). Every returned doc carries `env` ('demo' | 'live'); a stored row
        without one reads as 'demo' (schemas.snapshot_env, the backfill rule). `env` filters to one env
        (None = both, the old behaviour)."""

    # ---- ops monitor (read-only; item 19). Non-abstract so other Store implementations keep working.
    async def storage_stats(self) -> dict | None:
        """{used_bytes, limit_bytes} for the store, or None when the store cannot say."""
        return None

    async def ttl_index_report(self) -> dict | None:
        """{collection: bool} - whether each TTL index in schemas.TTL_INDEXES exists, or None when not applicable."""
        return None

    async def server_time(self) -> datetime | None:
        """The database server's clock (aware UTC), or None when not available."""
        return None

    # ---- fills
    @abc.abstractmethod
    async def upsert_fills(self, fills: Iterable[dict]) -> int:
        """Upsert on fill_id (existing fills are overwritten field-by-field). Returns the number NEW."""

    @abc.abstractmethod
    async def set_fill_fields(self, fill_id: str, fields: dict) -> bool:
        """Merge extra fields (e.g. fill_quality) into ONE existing fill. No upsert; fill_id and ts never change.
        Returns True if the fill exists."""

    @abc.abstractmethod
    async def list_fills(self, *, since: datetime | None = None, until: datetime | None = None,
                         limit: int = 500, fields: Iterable[str] | None = None,
                         fill_ids: Iterable[str] | None = None) -> list[dict]:
        """Fills with since <= ts < until, newest first. For P&L windows pass a large limit.
        `fields`: return only these keys (projection). `fill_ids`: only these fills (fill_id $in)."""

    # ---- settlements (Kalshi /portfolio/settlements; realised P&L of held-to-settlement positions)
    @abc.abstractmethod
    async def upsert_settlements(self, rows: Iterable[dict]) -> int:
        """Upsert on ticker (fields merged). ts from `ts`, else `settled_time`. Returns the number NEW."""

    @abc.abstractmethod
    async def list_settlements(self, *, since: datetime | None = None, until: datetime | None = None,
                               limit: int = 500, fields: Iterable[str] | None = None) -> list[dict]:
        """Settlements with since <= ts < until, newest first. `fields`: return only these keys (projection)."""

    # ---- bracket-arbitrage opportunity log
    @abc.abstractmethod
    async def add_arb_opportunity(self, doc: Any) -> bool:
        """Record one opportunity (dict or arb_scanner.Opportunity). A log-only sighting identical
        (schemas.arb_signature) to one whose last_seen is within ARB_DEDUPE_SECONDS before this ts is
        collapsed into it (seen_count += 1, last_seen = ts) and False is returned. Executed
        opportunities are always inserted. Returns True when a new doc was inserted."""

    @abc.abstractmethod
    async def list_arb_opportunities(self, *, since: datetime | None = None, limit: int = 100,
                                     executed_only: bool = False) -> list[dict]:
        """Newest first by ts (first seen). executed_only drops 'log-only' docs."""

    @abc.abstractmethod
    async def arb_spend_since(self, since: datetime, mode: str) -> float:
        """Sum of `cost` over arb opportunity docs with ts >= since, doc mode == mode and executed starting with
        FILLED, PARTIAL or ERROR (skipped / log-only excluded). No row limit."""

    # ---- queue-position telemetry (one doc per maker order: a "quote episode")
    @abc.abstractmethod
    async def upsert_quote_episodes(self, docs: Iterable[dict]) -> int:
        """Atomic per-doc upsert on order_id. Fields in schemas.QUOTE_EPISODE_IMMUTABLE are written only when
        the episode is created; every other given field is merged ($set). Returns the number NEW."""

    @abc.abstractmethod
    async def list_quote_episodes(self, *, since: datetime | None = None, env: str | None = None,
                                  status: str | None = None, limit: int = 5000,
                                  fields: Iterable[str] | None = None) -> list[dict]:
        """Episodes with ts >= since (optionally one env / status), newest first by ts (join time).
        `fields`: return only these keys (a projection; keeps the per-pass aggregate small in memory)."""

    # ---- shadow mode (services/shadow.py). Not abstract, so older test fakes keep working.
    async def shadow_upsert_episodes(self, docs: list[dict]) -> int:
        """Idempotent upsert by ep_id, $set every given field. Returns the number NEW."""
        raise NotImplementedError

    async def shadow_open_episodes(self, *, fields: Iterable[str] | None = None,
                                   limit: int | None = None) -> list[dict]:
        """Episodes with end_ts null, oldest join first. `fields`: projection; `limit`: cap."""
        raise NotImplementedError

    async def shadow_ended_unread(self, limit: int) -> list[dict]:
        """end_ts set and trades_read not true, oldest end first."""
        raise NotImplementedError

    async def shadow_unsettled(self, now: datetime, limit: int) -> list[dict]:
        """result null and close_time <= now; never-checked first, then least recently checked
        (settle_checked_ts), then oldest close: a stuck ticker rotates to the back (SH-1)."""
        raise NotImplementedError

    async def shadow_settled(self, since: datetime, *, fields: Iterable[str] | None = None,
                             limit: int | None = None, scored_only: bool = False) -> list[dict]:
        """result set and settled_ts >= since, oldest settle first. `scored_only`: result yes/no and a fill
        estimate (what the summary scores). `fields`: projection. `limit`: the NEWEST `limit` (newest first)."""
        raise NotImplementedError

    async def shadow_recent(self, limit: int, state: str | None = None) -> list[dict]:
        """Newest first by join_ts. state: open (end_ts null) | ended (end_ts set, no result) | settled (result set)."""
        raise NotImplementedError

    async def shadow_count_new_since(self, ts: datetime) -> int:
        """Episodes with join_ts >= ts."""
        raise NotImplementedError

    async def add_shadow_pass(self, doc: dict) -> None:
        raise NotImplementedError

    async def shadow_recent_passes(self, limit: int) -> list[dict]:
        raise NotImplementedError

    async def get_shadow_summary(self) -> dict | None:
        raise NotImplementedError

    async def set_shadow_summary(self, doc: dict) -> None:
        raise NotImplementedError

    # ---- market close time (services/closetime.py). Not abstract, so older test fakes keep working.
    async def set_quote_episode_close_time(self, order_id: str, close_time: datetime) -> bool:
        """Set close_time on ONE existing episode only while it has none (close_time is immutable once set).
        No upsert. Returns True if this call wrote it."""
        raise NotImplementedError

    async def set_settlement_fields(self, ticker: str, fields: dict) -> bool:
        """Merge extra fields (e.g. close_time) into ONE existing settlement. No upsert; ticker and ts never
        change. Returns True if the settlement exists."""
        raise NotImplementedError

    # ---- UX sprint additions. Not abstract, so older test fakes keep working.
    async def set_fills_fields(self, updates: dict[str, dict]) -> int:
        """Bulk set_fill_fields: {fill_id: fields}. No upsert; fill_id/ts never change. Returns the number matched."""
        n = 0
        for fid, fields in dict(updates).items():
            n += bool(await self.set_fill_fields(fid, fields))
        return n

    async def add_alert(self, doc: dict) -> dict:
        """Append one alert_history doc (schemas.prepare_alert); returns it."""
        raise NotImplementedError

    async def list_alerts(self, *, type: str | None = None, limit: int = 50,
                          before: datetime | None = None) -> list[dict]:
        """Alerts newest first, optionally one type and only ts < before (paging)."""
        raise NotImplementedError

    # ---- performance periods and cash flows (Sprint 2A-1). Not abstract, so older test fakes keep working.
    async def add_period(self, doc: dict) -> dict:
        """Append one period marker (schemas.prepare_period; append-only, never edited). Returns the stored doc.
        Raises ValueError on a bad name/note/started_at, the reserved id "p1", or a duplicate id. Period 1 is
        implicit and is NOT stored."""
        raise NotImplementedError

    async def list_periods(self) -> list[dict]:
        """Stored markers, oldest first by started_at (ties: insertion order). Excludes the implicit p1."""
        raise NotImplementedError

    async def add_cash_flow(self, doc: dict) -> dict:
        """Append one deposit/withdrawal (schemas.prepare_cash_flow). Returns the stored doc. Raises ValueError
        on a bad kind, an amount outside 0 < x <= 100000, a bad note/ts, or a duplicate id."""
        raise NotImplementedError

    async def list_cash_flows(self, *, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        """Cash flows with since <= ts < until, newest first (ties: newest insertion first)."""
        raise NotImplementedError

    async def delete_cash_flow(self, id: str) -> bool:
        """Remove one cash flow (typo undo). True if it existed."""
        raise NotImplementedError

    async def save_push_subscription(self, doc: dict) -> bool:
        """Upsert on sub_id (= sha256(endpoint)). Returns True if new. Raises ValueError when the
        PUSH_SUBSCRIPTIONS_MAX cap would be exceeded by a new one."""
        raise NotImplementedError

    async def list_push_subscriptions(self) -> list[dict]:
        """Every subscription (sensitive: callers never log or return endpoint/keys)."""
        raise NotImplementedError

    async def delete_push_subscription(self, sub_id: str) -> bool:
        raise NotImplementedError

    async def clear_push_subscriptions(self) -> int:
        """Delete every subscription (logout-all, owner 'clear all'). Returns how many were removed."""
        raise NotImplementedError

    # ---- release process: leader lease, handover replay guard, schema version, pending releases.
    # Not abstract (older test fakes keep working); MemoryStore and MongoStore implement all of them.
    async def acquire_lease(self, holder: str, version: str, ttl_s: float, *, now: datetime | None = None) -> dict:
        """Atomic. Succeeds when there is no lease, it has expired, or `holder` already owns it (a renew).
        Returns {"acquired", "generation", "holder", "expires_at"}; on failure these describe the current
        holder. `generation` only increases when the holder changes or the lease is created."""
        raise NotImplementedError

    async def renew_lease(self, holder: str, generation: int, ttl_s: float, *, now: datetime | None = None) -> bool:
        """True only if holder AND generation still match (the lease was not taken by someone else)."""
        raise NotImplementedError

    async def release_lease(self, holder: str, generation: int, *, handover: dict | None = None,
                            now: datetime | None = None) -> bool:
        """Expire the lease now if holder+generation match; stores `handover` (None clears) on the doc."""
        raise NotImplementedError

    async def get_lease(self, *, now: datetime | None = None) -> dict | None:
        """{holder, version, generation, expires_at, handover} or None if never created."""
        raise NotImplementedError

    async def consume_handover(self, nonce: str, *, now: datetime | None = None) -> bool:
        """Atomic single use: True the first time a nonce is consumed, False on replay."""
        raise NotImplementedError

    async def get_schema_version(self) -> int | None:
        raise NotImplementedError

    async def set_schema_version(self, version: int) -> None:
        raise NotImplementedError

    async def save_pending_release(self, doc: dict) -> dict:
        """doc {version, commit, summary, ci} -> doc + {id, status "pending", created_at, expires_at (+30 min)}."""
        raise NotImplementedError

    async def get_pending_release(self, release_id: str) -> dict | None:
        raise NotImplementedError

    async def latest_pending_release(self, *, now: datetime | None = None) -> dict | None:
        """Newest release that is still "pending" and not expired, else None."""
        raise NotImplementedError

    async def resolve_pending_release(self, release_id: str, decision: str, by: str, *,
                                      now: datetime | None = None) -> dict | None:
        """decision in {"approved","rejected"}; only from "pending" and not expired, else None.
        Records `by` and `at` and sets status. Raises ValueError on an invalid decision."""
        raise NotImplementedError

    # ---- monitor
    @abc.abstractmethod
    async def add_monitor_summary(self, doc: dict) -> None: ...

    @abc.abstractmethod
    async def last_monitor_summary(self) -> dict | None: ...

    async def list_monitor_summaries(self, *, since: datetime | None = None, limit: int = 100) -> list[dict]:
        """Monitor summaries with ts >= since, newest first. Default (for minimal subclasses): the last one only."""
        last = await self.last_monitor_summary()
        return [last] if last is not None and limit > 0 and _since_ok(last, since) else []

    # ---- audit
    @abc.abstractmethod
    async def add_audit(self, doc: dict) -> None: ...

    @abc.abstractmethod
    async def list_audit(self, limit: int = 100) -> list[dict]: ...

    # ---- users
    @abc.abstractmethod
    async def get_user(self, username: str) -> dict | None: ...

    @abc.abstractmethod
    async def save_user(self, doc: dict) -> dict:
        """Upsert on doc['username'], merging the given fields into any existing user. Returns the user.

        ``sessions_valid_after`` / ``last_totp_step`` (non-None) are applied as a max: they never
        move backwards (S-18)."""

    @abc.abstractmethod
    async def update_user_fields(self, username: str, *, set: dict | None = None,
                                 max: dict | None = None, inc: dict | None = None) -> dict | None:
        """Atomic field-level update of an EXISTING user (no upsert); returns the updated user or
        None if it doesn't exist. ``set`` overwrites, ``max`` only raises, ``inc`` adds. Monotonic
        fields passed in ``set`` are treated as ``max``. Use this instead of writing back a
        previously read doc (lost updates, S-18)."""

    # ---- token revocation
    @abc.abstractmethod
    async def revoke_jti(self, jti: str, exp: datetime | int | float) -> None:
        """Remember a revoked JWT id until `exp` (datetime or epoch seconds, as in the JWT `exp` claim)."""

    @abc.abstractmethod
    async def is_revoked(self, jti: str) -> bool: ...

    # ---- cloud settings
    @abc.abstractmethod
    async def get_cloud_settings(self) -> dict | None:
        """Stored cloud_settings doc, or None if never saved (callers merge schemas.DEFAULT_CLOUD_SETTINGS)."""

    @abc.abstractmethod
    async def save_cloud_settings(self, doc: dict, *, by: str) -> dict:
        """Merge the given top-level fields into the stored doc (e.g. {"pinger_minutes": 7} or
        {"cronjob_job_id": 123, "pinger_sync": {...}}); sets updated_utc/updated_by. Returns the full doc."""


# ------------------------------------------------------------------ shared validation
def _validate_state(desired: str | None, mode: str | None,
                    arb_desired: str | None = None, arb_mode: str | None = None) -> dict:
    """Validate and return the fields to set (only the non-None ones)."""
    upd = {}
    for name, val, allowed in (("desired", desired, S.DESIRED_VALUES), ("mode", mode, S.MODE_VALUES),
                               ("arb_desired", arb_desired, S.DESIRED_VALUES),
                               ("arb_mode", arb_mode, S.MODE_VALUES)):
        if val is not None:
            if val not in allowed:
                raise ValueError(f"{name} must be one of {allowed}")
            upd[name] = val
    return upd


def _state_out(doc: dict | None) -> dict | None:
    """Stored state doc -> contract shape (defaults filled for fields an older doc lacks)."""
    if doc is None:
        return None
    out = {**S.DEFAULT_STATE, **S.strip_id(doc)}
    return {k: copy.deepcopy(out.get(k)) for k in S.STATE_FIELDS}


def _window_ok(doc: dict, since: datetime | None, until: datetime | None) -> bool:
    return _since_ok(doc, since) and (until is None or doc["ts"] < S.norm_dt(until))


def _since_ok(doc: dict, since: datetime | None) -> bool:
    return since is None or doc["ts"] >= S.norm_dt(since)


def _project(docs: list[dict], fields: Iterable[str] | None) -> list[dict]:
    """Keep only `fields` (like a Mongo inclusion projection without _id); None = everything."""
    if fields is None:
        return docs
    keep = {str(f) for f in fields}
    return [{k: v for k, v in d.items() if k in keep} for d in docs]


def _lease_out(doc: dict | None) -> dict | None:
    """Stored lease doc -> {holder, version, generation, expires_at, handover}."""
    if doc is None:
        return None
    return {"holder": doc.get("holder"), "version": doc.get("version"), "generation": int(doc.get("generation") or 0),
            "expires_at": doc.get("expires_at"), "handover": copy.deepcopy(doc.get("handover"))}


def _lease_ttl(ttl_s: float) -> timedelta:
    ttl = float(ttl_s)
    if not ttl > 0:
        raise ValueError("ttl_s must be > 0")
    return timedelta(seconds=ttl)


def _check_decision(decision: str) -> None:
    if decision not in S.RELEASE_DECISIONS:
        raise ValueError(f"decision must be one of {S.RELEASE_DECISIONS}")


def _new_release(doc: dict, now: datetime) -> dict:
    S.check_no_secrets(doc)
    out = S.strip_id(S.normalize(doc))
    out.pop("id", None)
    out.pop("status", None)
    out.update({"id": uuid.uuid4().hex, "status": "pending", "created_at": now,
                "expires_at": now + timedelta(seconds=S.PENDING_RELEASE_TTL_SECONDS)})
    return out


def _newest(docs: list[dict], key: str = "ts") -> list[dict]:
    return sorted(docs, key=lambda d: d[key], reverse=True)


class MemoryStore(Store):
    """In-process store for dev and tests. Same semantics as MongoStore."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: dict | None = None
        self._cloud: dict | None = None
        self._config_versions: list[dict] = []
        self._heartbeats: list[dict] = []
        self._journal: dict[str, dict] = {}          # row_hash -> doc
        self._snapshots: list[dict] = []
        self._fills: dict[str, dict] = {}            # fill_id -> doc
        self._monitor: list[dict] = []
        self._audit: list[dict] = []
        self._users: dict[str, dict] = {}
        self._revoked: dict[str, datetime] = {}      # jti -> exp
        self._settlements: dict[str, dict] = {}      # ticker -> doc
        self._arb: list[dict] = []
        self._episodes: dict[str, dict] = {}         # order_id -> doc
        self._alerts: list[dict] = []
        self._periods: dict[str, dict] = {}          # id -> doc
        self._cash_flows: dict[str, dict] = {}       # id -> doc
        self._shadow_eps: dict[str, dict] = {}       # ep_id -> doc
        self._shadow_passes: list[dict] = []
        self._shadow_summary: dict | None = None
        self._push: dict[str, dict] = {}             # sub_id -> doc
        self._lease: dict | None = None              # the single runner_lease doc
        self._nonces: dict[str, datetime] = {}       # consumed handover nonce -> ts
        self._schema_version: int | None = None
        self._releases: dict[str, dict] = {}         # id -> doc
        self._seq = 0                               # insertion order tiebreak for equal ts

    @staticmethod
    def _out(doc: dict | None) -> dict | None:
        return None if doc is None else copy.deepcopy(S.strip_id(doc))

    def _stamp(self, doc: dict) -> dict:
        self._seq += 1
        doc["\x00seq"] = self._seq
        return doc

    @staticmethod
    def _clean(doc: dict) -> dict:
        d = copy.deepcopy(doc)
        d.pop("\x00seq", None)
        d.pop("_id", None)
        return d

    @staticmethod
    def _newest_seq(docs: Iterable[dict], key: str = "ts") -> list[dict]:
        return sorted(docs, key=lambda d: (d[key], d.get("\x00seq", 0)), reverse=True)

    # ---- health
    async def ping(self) -> bool:
        return True

    async def ensure_indexes(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ---- state
    async def get_state(self) -> dict | None:
        return _state_out(self._state)

    async def set_state(self, *, by: str, desired: str | None = None, mode: str | None = None,
                        arb_desired: str | None = None, arb_mode: str | None = None) -> dict:
        upd = _validate_state(desired, mode, arb_desired, arb_mode)
        async with self._lock:
            cur = {**S.DEFAULT_STATE, **(self._state or {})}
            cur.update(upd)
            now = S.utcnow()
            prev = (self._state or {}).get("updated_utc")
            if prev is not None and now <= prev:         # ms precision: two writes in one ms must still differ,
                now = prev + timedelta(milliseconds=1)   # because the runner detects a changed doc by this stamp
            cur["updated_utc"] = now
            cur["updated_by"] = by
            self._state = {k: cur[k] for k in S.STATE_FIELDS}
            return _state_out(self._state)

    # ---- config
    async def get_config(self) -> dict | None:
        if not self._config_versions:
            return None
        latest = max(self._config_versions, key=lambda d: d["version"])
        return copy.deepcopy(latest["after"])

    async def save_config(self, after: dict, *, before: dict | None, by: str) -> int:
        async with self._lock:
            version = max((d["version"] for d in self._config_versions), default=0) + 1
            self._config_versions.append({"version": version, "ts": S.utcnow(), "by": by,
                                          "before": S.normalize(before) if before is not None else None,
                                          "after": S.normalize(after)})
            return version

    async def list_config_versions(self, limit: int = 20) -> list[dict]:
        docs = _newest(self._config_versions, "version")[:max(0, int(limit))]
        return [self._out(d) for d in docs]

    # ---- heartbeats
    async def add_heartbeat(self, doc: dict) -> None:
        self._heartbeats.append(self._stamp(S.prepare_ts_doc(doc)))

    async def last_heartbeat(self) -> dict | None:
        if not self._heartbeats:
            return None
        return self._clean(self._newest_seq(self._heartbeats)[0])

    async def list_heartbeats(self, *, since: datetime | None = None, limit: int = 1000) -> list[dict]:
        docs = [d for d in self._heartbeats if _since_ok(d, since)]
        return [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]

    # ---- journal
    async def add_journal_rows(self, rows: Iterable[dict]) -> int:
        n = 0
        async with self._lock:
            for row in rows:
                doc = S.prepare_journal_row(row)
                if doc["row_hash"] in self._journal:
                    continue
                self._journal[doc["row_hash"]] = self._stamp(doc)
                n += 1
        return n

    async def list_journal(self, *, since: datetime | None = None, limit: int = 200,
                           events: Iterable[str] | None = None, fields: Iterable[str] | None = None) -> list[dict]:
        ev = set(events) if events is not None else None
        docs = [d for d in self._journal.values()
                if _since_ok(d, since) and (ev is None or d.get("event") in ev)]
        return _project([self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]], fields)

    # ---- account snapshots
    async def add_account_snapshot(self, doc: dict) -> None:
        self._snapshots.append(self._stamp(S.prepare_ts_doc(doc)))

    async def list_account_snapshots(self, *, since: datetime | None = None, limit: int = 10000,
                                     env: str | None = None) -> list[dict]:
        docs = [d for d in self._snapshots if _since_ok(d, since) and (env is None or S.snapshot_env(d) == env)]
        return [S.with_snapshot_env(self._clean(d)) for d in self._newest_seq(docs)[:max(0, int(limit))]]

    # ---- fills
    async def upsert_fills(self, fills: Iterable[dict]) -> int:
        n = 0
        async with self._lock:
            for f in fills:
                doc = S.prepare_fill(f)
                old = self._fills.get(doc["fill_id"])
                if old is None:
                    self._fills[doc["fill_id"]] = self._stamp(doc)
                    n += 1
                else:
                    old.update(doc)                      # $set semantics: merge fields
        return n

    async def set_fill_fields(self, fill_id: str, fields: dict) -> bool:
        clean = S.normalize({k: v for k, v in dict(fields).items() if k not in ("fill_id", "ts", "_id")})
        async with self._lock:
            old = self._fills.get(str(fill_id))
            if old is None:
                return False
            old.update(clean)
            return True

    async def list_fills(self, *, since: datetime | None = None, until: datetime | None = None,
                         limit: int = 500, fields: Iterable[str] | None = None,
                         fill_ids: Iterable[str] | None = None) -> list[dict]:
        ids = None if fill_ids is None else {str(i) for i in fill_ids}
        docs = [d for d in self._fills.values() if _window_ok(d, since, until)
                and (ids is None or d["fill_id"] in ids)]
        out = [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]
        if fields is not None:
            keep = set(fields)
            out = [{k: v for k, v in d.items() if k in keep} for d in out]
        return out

    # ---- settlements
    async def upsert_settlements(self, rows: Iterable[dict]) -> int:
        n = 0
        async with self._lock:
            for r in rows:
                doc = S.prepare_settlement(r)
                old = self._settlements.get(doc["ticker"])
                if old is None:
                    self._settlements[doc["ticker"]] = self._stamp(doc)
                    n += 1
                else:
                    old.update(doc)                      # $set semantics: merge fields
        return n

    async def list_settlements(self, *, since: datetime | None = None, until: datetime | None = None,
                               limit: int = 500, fields: Iterable[str] | None = None) -> list[dict]:
        docs = [d for d in self._settlements.values() if _window_ok(d, since, until)]
        return _project([self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]], fields)

    async def set_settlement_fields(self, ticker: str, fields: dict) -> bool:
        clean = S.normalize({k: v for k, v in dict(fields).items() if k not in ("ticker", "ts", "_id")})
        if S.CLOSE_TIME in clean and S._safe_to_utc(clean[S.CLOSE_TIME]) is None:
            clean.pop(S.CLOSE_TIME)                  # close_time is never stored as null
        async with self._lock:
            old = self._settlements.get(str(ticker))
            if old is None:
                return False
            old.update(clean)
            return True

    # ---- arbitrage log
    async def add_arb_opportunity(self, doc: Any) -> bool:
        new = S.prepare_arb_opportunity(doc)
        async with self._lock:
            if new["executed"] == S.ARB_LOG_ONLY:
                lo = new["ts"] - timedelta(seconds=S.ARB_DEDUPE_SECONDS)
                cands = [d for d in self._arb if d["sig"] == new["sig"] and d["executed"] == S.ARB_LOG_ONLY
                         and lo <= d["last_seen"] <= new["ts"]]
                if cands:
                    cur = max(cands, key=lambda d: (d["last_seen"], d["\x00seq"]))
                    cur["seen_count"] += 1
                    cur["last_seen"] = new["ts"]
                    return False
            self._arb.append(self._stamp(new))
            return True

    async def list_arb_opportunities(self, *, since: datetime | None = None, limit: int = 100,
                                     executed_only: bool = False) -> list[dict]:
        docs = [d for d in self._arb if _since_ok(d, since)
                and (not executed_only or d["executed"] != S.ARB_LOG_ONLY)]
        return [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]

    async def arb_spend_since(self, since: datetime, mode: str) -> float:
        return sum(float(d.get("cost") or 0) for d in self._arb
                   if _since_ok(d, since) and d.get("mode") == mode
                   and str(d.get("executed", "")).startswith(S.ARB_SPEND_PREFIXES))

    # ---- queue-position telemetry
    async def upsert_quote_episodes(self, docs: Iterable[dict]) -> int:
        n = 0
        async with self._lock:
            for d in docs:
                doc = S.prepare_quote_episode(d)
                old = self._episodes.get(doc["order_id"])
                if old is None:
                    self._episodes[doc["order_id"]] = self._stamp(doc)
                    n += 1
                else:
                    old.update({k: v for k, v in doc.items() if k not in S.QUOTE_EPISODE_IMMUTABLE})
        return n

    async def set_quote_episode_close_time(self, order_id: str, close_time: datetime) -> bool:
        ct = S._safe_to_utc(close_time)
        if ct is None:
            return False
        async with self._lock:
            old = self._episodes.get(str(order_id))
            if old is None or old.get(S.CLOSE_TIME) is not None:
                return False
            old[S.CLOSE_TIME] = ct
            return True

    async def list_quote_episodes(self, *, since: datetime | None = None, env: str | None = None,
                                  status: str | None = None, limit: int = 5000,
                                  fields: Iterable[str] | None = None) -> list[dict]:
        docs = [d for d in self._episodes.values() if _since_ok(d, since)
                and (env is None or d.get("env") == env) and (status is None or d.get("status") == status)]
        out = [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]
        if fields is not None:
            keep = set(fields)
            out = [{k: v for k, v in d.items() if k in keep} for d in out]
        return out

    # ---- shadow mode
    async def shadow_upsert_episodes(self, docs: list[dict]) -> int:
        n = 0
        async with self._lock:
            for d in docs:
                doc = S.prepare_shadow_episode(d)
                old = self._shadow_eps.get(doc["ep_id"])
                if old is None:
                    doc.setdefault("ts", S.utcnow())
                    self._shadow_eps[doc["ep_id"]] = self._stamp(doc)
                    n += 1
                else:
                    old.update(doc)
        return n

    def _shadow_list(self, pred, key, *, reverse: bool = False, limit: int | None = None) -> list[dict]:
        docs = [d for d in self._shadow_eps.values() if pred(d)]
        docs.sort(key=lambda d: (key(d), d.get("\x00seq", 0)), reverse=reverse)
        if limit is not None:
            docs = docs[:max(0, int(limit))]
        return [S.shadow_with_defaults(self._clean(d)) for d in docs]

    @staticmethod
    def _project(docs: list[dict], fields: Iterable[str] | None) -> list[dict]:
        if fields is None:
            return docs
        keep = set(fields) | {"ep_id"}
        return [S.shadow_with_defaults({k: v for k, v in d.items() if k in keep}, keep) for d in docs]

    async def shadow_open_episodes(self, *, fields: Iterable[str] | None = None,
                                   limit: int | None = None) -> list[dict]:
        return self._project(self._shadow_list(lambda d: d.get("end_ts") is None,
                                               lambda d: d.get("join_ts") or d["ts"], limit=limit), fields)

    async def shadow_ended_unread(self, limit: int) -> list[dict]:
        return self._shadow_list(lambda d: d.get("end_ts") is not None and d.get("trades_read") is not True,
                                 lambda d: d["end_ts"], limit=limit)

    async def shadow_unsettled(self, now: datetime, limit: int) -> list[dict]:
        t = S.norm_dt(now)
        return self._shadow_list(lambda d: d.get("result") is None and d.get("close_time") is not None
                                 and d["close_time"] <= t,
                                 lambda d: (d.get("settle_checked_ts") is not None,
                                            d.get("settle_checked_ts") or d["close_time"], d["close_time"]),
                                 limit=limit)

    async def shadow_settled(self, since: datetime, *, fields: Iterable[str] | None = None,
                             limit: int | None = None, scored_only: bool = False) -> list[dict]:
        t = S.norm_dt(since)
        ok = S.SHADOW_SCORED_RESULTS if scored_only else None
        pred = (lambda d: d.get("result") is not None and d.get("settled_ts") is not None and d["settled_ts"] >= t
                and (ok is None or (d.get("result") in ok and d.get("fill") is not None)))
        if limit is None:
            return self._project(self._shadow_list(pred, lambda d: d["settled_ts"]), fields)
        return self._project(self._shadow_list(pred, lambda d: d["settled_ts"], reverse=True, limit=limit), fields)

    async def shadow_recent(self, limit: int, state: str | None = None) -> list[dict]:
        preds = {None: lambda d: True,
                 "open": lambda d: d.get("end_ts") is None,
                 "ended": lambda d: d.get("end_ts") is not None and d.get("result") is None,
                 "settled": lambda d: d.get("result") is not None}
        if state not in preds:
            raise ValueError(f"unknown shadow state {state!r}")
        return self._shadow_list(preds[state], lambda d: d.get("join_ts") or d["ts"], reverse=True, limit=limit)

    async def shadow_count_new_since(self, ts: datetime) -> int:
        t = S.norm_dt(ts)
        return sum(1 for d in self._shadow_eps.values() if d.get("join_ts") is not None and d["join_ts"] >= t)

    async def add_shadow_pass(self, doc: dict) -> None:
        d = S.prepare_shadow_pass(doc)
        async with self._lock:
            self._shadow_passes.append(self._stamp(d))
            if len(self._shadow_passes) > 5000:              # memory guard (Atlas uses the 30-day TTL instead)
                self._shadow_passes = self._shadow_passes[-5000:]

    async def shadow_recent_passes(self, limit: int) -> list[dict]:
        return [self._clean(d) for d in self._newest_seq(self._shadow_passes)[:max(0, int(limit))]]

    async def get_shadow_summary(self) -> dict | None:
        return self._out(self._shadow_summary)

    async def set_shadow_summary(self, doc: dict) -> None:
        async with self._lock:
            self._shadow_summary = S.strip_id(S.normalize(doc))

    # ---- UX sprint additions
    async def set_fills_fields(self, updates: dict[str, dict]) -> int:
        n = 0
        async with self._lock:
            for fid, fields in dict(updates).items():
                old = self._fills.get(str(fid))
                if old is None:
                    continue
                old.update(S.normalize({k: v for k, v in dict(fields).items() if k not in ("fill_id", "ts", "_id")}))
                n += 1
        return n

    async def add_alert(self, doc: dict) -> dict:
        d = S.prepare_alert(doc)
        async with self._lock:
            self._alerts.append(self._stamp(d))
            if len(self._alerts) > 5000:                 # memory guard (Atlas uses the 90-day TTL instead)
                self._alerts = self._alerts[-5000:]
        return self._clean(d)

    async def list_alerts(self, *, type: str | None = None, limit: int = 50,
                          before: datetime | None = None) -> list[dict]:
        b = S.norm_dt(before) if before is not None else None
        docs = [d for d in self._alerts if (type is None or d["type"] == type) and (b is None or d["ts"] < b)]
        return [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]

    async def add_period(self, doc: dict) -> dict:
        d = S.prepare_period(doc)
        async with self._lock:
            if d["id"] in self._periods:
                raise ValueError("period id already exists")
            self._periods[d["id"]] = self._stamp(d)
        return self._clean(d)

    async def list_periods(self) -> list[dict]:
        docs = sorted(self._periods.values(), key=lambda d: (d["started_at"], d.get("\x00seq", 0)))
        return [self._clean(d) for d in docs]

    async def add_cash_flow(self, doc: dict) -> dict:
        d = S.prepare_cash_flow(doc)
        async with self._lock:
            if d["id"] in self._cash_flows:
                raise ValueError("cash flow id already exists")
            self._cash_flows[d["id"]] = self._stamp(d)
        return self._clean(d)

    async def list_cash_flows(self, *, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        docs = [d for d in self._cash_flows.values() if _window_ok(d, since, until)]
        return [self._clean(d) for d in self._newest_seq(docs)]

    async def delete_cash_flow(self, id: str) -> bool:
        async with self._lock:
            return self._cash_flows.pop(str(id), None) is not None

    async def save_push_subscription(self, doc: dict) -> bool:
        d = S.prepare_push_subscription(doc)
        async with self._lock:
            new = d["sub_id"] not in self._push
            if new and len(self._push) >= S.PUSH_SUBSCRIPTIONS_MAX:
                raise ValueError("too many push subscriptions")
            if not new:
                d["created_utc"] = self._push[d["sub_id"]]["created_utc"]
            self._push[d["sub_id"]] = d
            return new

    async def list_push_subscriptions(self) -> list[dict]:
        return [copy.deepcopy(d) for d in self._push.values()]

    async def delete_push_subscription(self, sub_id: str) -> bool:
        async with self._lock:
            return self._push.pop(str(sub_id), None) is not None

    async def clear_push_subscriptions(self) -> int:
        async with self._lock:
            n = len(self._push)
            self._push.clear()
            return n

    # ---- release process
    async def acquire_lease(self, holder: str, version: str, ttl_s: float, *, now: datetime | None = None) -> dict:
        holder = str(holder)
        ttl = _lease_ttl(ttl_s)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        async with self._lock:
            cur = self._lease
            if cur is None:
                cur = self._lease = {"holder": holder, "version": str(version), "generation": 1,
                                     "expires_at": t + ttl, "acquired_at": t, "handover": None}
                acquired = True
            elif cur["holder"] == holder:                         # already ours: a renew, same generation
                cur.update({"version": str(version), "expires_at": t + ttl})
                acquired = True
            elif cur["expires_at"] <= t:                          # expired: take over with a higher generation
                cur.update({"holder": holder, "version": str(version), "generation": cur["generation"] + 1,
                            "expires_at": t + ttl, "acquired_at": t})
                acquired = True
            else:
                acquired = False
            return {"acquired": acquired, "generation": cur["generation"], "holder": cur["holder"],
                    "expires_at": cur["expires_at"]}

    async def renew_lease(self, holder: str, generation: int, ttl_s: float, *, now: datetime | None = None) -> bool:
        ttl = _lease_ttl(ttl_s)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        async with self._lock:
            cur = self._lease
            if cur is None or cur["holder"] != str(holder) or cur["generation"] != int(generation):
                return False
            cur["expires_at"] = t + ttl
            return True

    async def release_lease(self, holder: str, generation: int, *, handover: dict | None = None,
                            now: datetime | None = None) -> bool:
        t = S.norm_dt(now) if now is not None else S.utcnow()
        async with self._lock:
            cur = self._lease
            if cur is None or cur["holder"] != str(holder) or cur["generation"] != int(generation):
                return False
            cur["expires_at"] = t
            cur["handover"] = S.normalize(handover) if handover is not None else None
            return True

    async def get_lease(self, *, now: datetime | None = None) -> dict | None:
        return _lease_out(self._lease)

    async def consume_handover(self, nonce: str, *, now: datetime | None = None) -> bool:
        if not isinstance(nonce, str) or not nonce:
            return False
        t = S.norm_dt(now) if now is not None else S.utcnow()
        async with self._lock:
            lo = t - timedelta(seconds=S.HANDOVER_NONCE_TTL_SECONDS)      # Atlas TTL equivalent
            for k in [k for k, ts in self._nonces.items() if ts <= lo]:
                del self._nonces[k]
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = t
            return True

    async def get_schema_version(self) -> int | None:
        return self._schema_version

    async def set_schema_version(self, version: int) -> None:
        self._schema_version = int(version)

    async def save_pending_release(self, doc: dict) -> dict:
        d = _new_release(doc, S.utcnow())
        async with self._lock:
            self._releases[d["id"]] = self._stamp(d)
        return self._clean(d)

    async def get_pending_release(self, release_id: str) -> dict | None:
        d = self._releases.get(str(release_id))
        return None if d is None else self._clean(d)

    async def latest_pending_release(self, *, now: datetime | None = None) -> dict | None:
        t = S.norm_dt(now) if now is not None else S.utcnow()
        docs = [d for d in self._releases.values() if d["status"] == "pending" and d["expires_at"] > t]
        return self._clean(self._newest_seq(docs, "created_at")[0]) if docs else None

    async def resolve_pending_release(self, release_id: str, decision: str, by: str, *,
                                      now: datetime | None = None) -> dict | None:
        _check_decision(decision)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        async with self._lock:
            d = self._releases.get(str(release_id))
            if d is None or d["status"] != "pending" or d["expires_at"] <= t:
                return None
            d.update({"status": decision, "by": str(by), "at": t})
            return self._clean(d)

    # ---- monitor
    async def add_monitor_summary(self, doc: dict) -> None:
        self._monitor.append(self._stamp(S.prepare_ts_doc(doc, ts_fallbacks=("generated_utc",))))

    async def last_monitor_summary(self) -> dict | None:
        if not self._monitor:
            return None
        return self._clean(self._newest_seq(self._monitor)[0])

    async def list_monitor_summaries(self, *, since: datetime | None = None, limit: int = 100) -> list[dict]:
        docs = [d for d in self._monitor if _since_ok(d, since)]
        return [self._clean(d) for d in self._newest_seq(docs)[:max(0, int(limit))]]

    # ---- audit
    async def add_audit(self, doc: dict) -> None:
        self._audit.append(self._stamp(S.prepare_ts_doc(doc)))

    async def list_audit(self, limit: int = 100) -> list[dict]:
        return [self._clean(d) for d in self._newest_seq(self._audit)[:max(0, int(limit))]]

    # ---- users
    async def get_user(self, username: str) -> dict | None:
        return self._out(self._users.get(username))

    async def save_user(self, doc: dict) -> dict:
        username = doc.get("username")
        if not username:
            raise ValueError("user doc needs a username")
        S.check_no_secrets(doc)
        plain, mono = S.split_monotonic(S.strip_id(S.normalize(doc)))
        async with self._lock:
            cur = self._users.get(username, {})
            cur.update(plain)
            self._apply_max(cur, mono)
            self._users[username] = cur
            return self._out(cur)

    @staticmethod
    def _apply_max(cur: dict, mono: dict) -> None:
        for k, v in mono.items():
            old = cur.get(k)
            try:
                cur[k] = v if old is None or v > old else old
            except TypeError:          # mismatched types: same as Mongo's BSON order is not worth emulating
                cur[k] = v

    async def update_user_fields(self, username: str, *, set: dict | None = None,
                                 max: dict | None = None, inc: dict | None = None) -> dict | None:
        set_ = dict(set or {})
        set_.pop("username", None)
        S.check_no_secrets(set_)
        plain, mono = S.split_monotonic(S.normalize(set_))
        mono.update({k: v for k, v in S.normalize(dict(max or {})).items() if v is not None})
        async with self._lock:
            cur = self._users.get(username)
            if cur is None:
                return None
            cur.update(plain)
            self._apply_max(cur, mono)
            for k, v in (inc or {}).items():
                cur[k] = (cur.get(k) or 0) + v
            return self._out(cur)

    # ---- token revocation
    async def revoke_jti(self, jti: str, exp: datetime | int | float) -> None:
        self._revoked[str(jti)] = S.to_utc(exp)

    async def is_revoked(self, jti: str) -> bool:
        exp = self._revoked.get(str(jti))
        if exp is None:
            return False
        if exp <= S.utcnow():                            # expired: the token is dead anyway; purge (TTL)
            self._revoked.pop(str(jti), None)
            return False
        return True

    # ---- cloud settings
    async def get_cloud_settings(self) -> dict | None:
        return self._out(self._cloud)

    async def save_cloud_settings(self, doc: dict, *, by: str) -> dict:
        S.check_no_secrets(doc)
        async with self._lock:
            cur = dict(self._cloud or {})
            cur.update(S.strip_id(S.normalize(doc)))
            cur["updated_utc"] = S.utcnow()
            cur["updated_by"] = by
            self._cloud = cur
            return self._out(cur)

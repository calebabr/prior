"""MongoStore: the Store contract on MongoDB Atlas via PyMongo's async API (pymongo.AsyncMongoClient).

The client is created lazily-connected (PyMongo connects on first operation), so constructing a
MongoStore never touches the network. The URI comes only from settings.mongodb_uri.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError, PyMongoError

from . import schemas as S
from .store import (Store, _check_decision, _lease_out, _lease_ttl, _new_release, _state_out,
                    _validate_state)

NO_ID = {"_id": 0}


def _proj(fields) -> dict:
    """Inclusion projection without _id (None = whole doc)."""
    return NO_ID if fields is None else {"_id": 0, **{str(f): 1 for f in fields}}


NEWEST = [("ts", DESCENDING), ("_id", DESCENDING)]
RECON_NEWEST = [("day", DESCENDING), ("ts", DESCENDING)]


def _window(since: datetime | None, until: datetime | None) -> dict:
    """{ts: {$gte: since, $lt: until}} (either bound optional)."""
    rng: dict = {}
    if since is not None:
        rng["$gte"] = S.norm_dt(since)
    if until is not None:
        rng["$lt"] = S.norm_dt(until)
    return {"ts": rng} if rng else {}


class MongoStore(Store):
    def __init__(self, uri: str, db_name: str = S.DB_NAME_DEFAULT, *, timeout_ms: int = 5000,
                 client: Any | None = None) -> None:
        if client is None:
            if not uri:
                raise ValueError("MongoStore needs a MongoDB URI (settings.mongodb_uri)")
            client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=timeout_ms,
                                      connectTimeoutMS=timeout_ms, socketTimeoutMS=max(timeout_ms * 3, 15000),
                                      appname="kalshi-control")
        self._client = client
        self._db = client[db_name or S.DB_NAME_DEFAULT]

    @property
    def client(self):
        return self._client

    @property
    def db(self):
        return self._db

    def _c(self, name: str):
        return self._db[name]

    # ---- health
    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:                                    # noqa: BLE001 - ping never raises
            return False

    async def ensure_indexes(self) -> None:
        ttl = S.RETENTION_SECONDS
        await self._c(S.CONFIG_VERSIONS).create_index([("version", DESCENDING)], unique=True, name="version_unique")
        await self._c(S.HEARTBEATS).create_index([("ts", ASCENDING)], expireAfterSeconds=ttl, name="ts_ttl")
        await self._c(S.JOURNAL_ROWS).create_index([("row_hash", ASCENDING)], unique=True, name="row_hash_unique")
        await self._c(S.JOURNAL_ROWS).create_index([("ts", ASCENDING)], expireAfterSeconds=ttl, name="ts_ttl")
        await self._c(S.JOURNAL_ROWS).create_index([("event", ASCENDING), ("ts", DESCENDING)], name="event_ts")
        await self._c(S.ACCOUNT_SNAPSHOTS).create_index([("ts", ASCENDING)], expireAfterSeconds=ttl, name="ts_ttl")
        await self._c(S.FILLS).create_index([("fill_id", ASCENDING)], unique=True, name="fill_id_unique")
        await self._c(S.FILLS).create_index([("ts", DESCENDING)], name="ts_desc")
        await self._c(S.SETTLEMENTS).create_index([("ticker", ASCENDING)], unique=True, name="ticker_unique")
        await self._c(S.SETTLEMENTS).create_index([("ts", DESCENDING)], name="ts_desc")
        await self._c(S.ARB_OPPORTUNITIES).create_index([("ts", ASCENDING)], expireAfterSeconds=S.ARB_RETENTION_SECONDS, name="ts_ttl")
        await self._c(S.ARB_OPPORTUNITIES).create_index([("sig", ASCENDING), ("last_seen", DESCENDING)],
                                                        name="sig_last_seen")
        await self._c(S.QUOTE_EPISODES).create_index([("order_id", ASCENDING)], unique=True, name="order_id_unique")
        await self._c(S.QUOTE_EPISODES).create_index([("ts", ASCENDING)], expireAfterSeconds=S.QUEUE_RETENTION_SECONDS,
                                                     name="ts_ttl")
        await self._c(S.QUOTE_EPISODES).create_index([("status", ASCENDING), ("env", ASCENDING), ("ts", DESCENDING)],
                                                     name="status_env_ts")
        eps = self._c(S.SHADOW_EPISODES)
        await eps.create_index([("ts", ASCENDING)], expireAfterSeconds=S.SHADOW_RETENTION_SECONDS, name="ts_ttl")
        await eps.create_index([("end_ts", ASCENDING), ("join_ts", DESCENDING)], name="open_join")
        await eps.create_index([("trades_read", ASCENDING), ("end_ts", ASCENDING)], name="ended_unread")
        await eps.create_index([("result", ASCENDING), ("settle_checked_ts", ASCENDING), ("close_time", ASCENDING)],
                               name="unsettled_rotation")
        await eps.create_index([("settled_ts", ASCENDING)], name="settled_ts")
        await eps.create_index([("join_ts", DESCENDING)], name="join_ts_desc")
        await self._c(S.SHADOW_PASSES).create_index([("ts", ASCENDING)],
                                                    expireAfterSeconds=S.SHADOW_RETENTION_SECONDS, name="ts_ttl")
        await self._c(S.ALERT_HISTORY).create_index([("ts", ASCENDING)], expireAfterSeconds=S.ALERT_RETENTION_SECONDS,
                                                    name="ts_ttl")
        await self._c(S.ALERT_HISTORY).create_index([("type", ASCENDING), ("ts", DESCENDING)], name="type_ts")
        # periods / cash_flows: small and permanent, so no TTL
        await self._c(S.PERIODS).create_index([("id", ASCENDING)], unique=True, name="id_unique")
        await self._c(S.PERIODS).create_index([("started_at", ASCENDING)], name="started_at")
        await self._c(S.CASH_FLOWS).create_index([("id", ASCENDING)], unique=True, name="id_unique")
        await self._c(S.CASH_FLOWS).create_index([("ts", DESCENDING)], name="ts_desc")
        # recon_reports: one per (mode, day); 90-day TTL on ts (the check time)
        await self._c(S.RECON_REPORTS).create_index([("mode", ASCENDING), ("day", ASCENDING)], unique=True,
                                                    name="mode_day_unique")
        await self._c(S.RECON_REPORTS).create_index([("ts", ASCENDING)], expireAfterSeconds=S.RECON_RETENTION_SECONDS,
                                                    name="ts_ttl")
        await self._c(S.RECON_REPORTS).create_index([("day", DESCENDING), ("ts", DESCENDING)], name="day_ts_desc")
        await self._c(S.PUSH_SUBSCRIPTIONS).create_index([("sub_id", ASCENDING)], unique=True, name="sub_id_unique")
        await self._c(S.PUSH_SUBSCRIPTIONS).create_index([("slot", ASCENDING)], unique=True, sparse=True,
                                                         name="slot_unique")
        await self._c(S.MONITOR_SUMMARIES).create_index([("ts", DESCENDING)], name="ts_desc")
        await self._c(S.AUDIT_LOG).create_index([("ts", DESCENDING)], name="ts_desc")
        await self._c(S.USERS).create_index([("username", ASCENDING)], unique=True, name="username_unique")
        await self._c(S.REVOKED_TOKENS).create_index([("jti", ASCENDING)], unique=True, name="jti_unique")
        await self._c(S.REVOKED_TOKENS).create_index([("exp", ASCENDING)], expireAfterSeconds=0, name="exp_ttl")
        # release process (single-doc runner_lease and meta need no index beyond _id)
        await self._c(S.HANDOVER_NONCES).create_index([("nonce", ASCENDING)], unique=True, name="nonce_unique")
        await self._c(S.HANDOVER_NONCES).create_index([("ts", ASCENDING)],
                                                      expireAfterSeconds=S.HANDOVER_NONCE_TTL_SECONDS, name="ts_ttl")
        await self._c(S.RELEASES).create_index([("id", ASCENDING)], unique=True, name="id_unique")
        await self._c(S.RELEASES).create_index([("created_at", ASCENDING)],
                                               expireAfterSeconds=S.RELEASE_RETENTION_SECONDS, name="created_ttl")

    async def close(self) -> None:
        await self._client.close()

    # ---- state
    async def get_state(self) -> dict | None:
        return _state_out(await self._c(S.STATE).find_one({"_id": S.STATE_ID}, NO_ID))

    async def set_state(self, *, by: str, desired: str | None = None, mode: str | None = None,
                        arb_desired: str | None = None, arb_mode: str | None = None) -> dict:
        upd: dict = _validate_state(desired, mode, arb_desired, arb_mode)
        upd.update({"updated_utc": S.utcnow(), "updated_by": by})
        on_insert = {k: v for k, v in S.DEFAULT_STATE.items() if k not in upd}
        update: dict = {"$set": upd}
        if on_insert:
            update["$setOnInsert"] = on_insert
        # One atomic single-document upsert: a Stop that sets desired and arb_desired lands together.
        doc = await self._c(S.STATE).find_one_and_update(
            {"_id": S.STATE_ID}, update, projection=NO_ID, upsert=True, return_document=ReturnDocument.AFTER)
        return _state_out(doc)

    # ---- config
    async def get_config(self) -> dict | None:
        doc = await self._c(S.CONFIG_VERSIONS).find_one({}, {"_id": 0, "after": 1}, sort=[("version", DESCENDING)])
        return None if doc is None else doc.get("after")

    async def save_config(self, after: dict, *, before: dict | None, by: str) -> int:
        coll = self._c(S.CONFIG_VERSIONS)
        for _ in range(10):                                  # retry on a concurrent writer taking the number
            last = await coll.find_one({}, {"version": 1}, sort=[("version", DESCENDING)])
            version = (last["version"] if last else 0) + 1
            try:
                await coll.insert_one({"version": version, "ts": S.utcnow(), "by": by,
                                       "before": S.normalize(before) if before is not None else None,
                                       "after": S.normalize(after)})
                return version
            except DuplicateKeyError:
                continue
        raise RuntimeError("could not allocate a config version")

    async def list_config_versions(self, limit: int = 20) -> list[dict]:
        if limit <= 0:
            return []
        cur = self._c(S.CONFIG_VERSIONS).find({}, NO_ID).sort("version", DESCENDING).limit(int(limit))
        return await cur.to_list()

    # ---- heartbeats
    async def add_heartbeat(self, doc: dict) -> None:
        await self._c(S.HEARTBEATS).insert_one(S.prepare_ts_doc(doc))

    async def last_heartbeat(self) -> dict | None:
        return await self._c(S.HEARTBEATS).find_one({}, NO_ID, sort=NEWEST)

    async def list_heartbeats(self, *, since: datetime | None = None, limit: int = 1000) -> list[dict]:
        if limit <= 0:
            return []
        q = {} if since is None else {"ts": {"$gte": S.norm_dt(since)}}       # served by the ts_ttl index
        return await self._c(S.HEARTBEATS).find(q, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    # ---- journal
    async def add_journal_rows(self, rows: Iterable[dict]) -> int:
        ops = []
        for row in rows:
            doc = S.prepare_journal_row(row)
            ops.append(UpdateOne({"row_hash": doc["row_hash"]}, {"$setOnInsert": doc}, upsert=True))
        if not ops:
            return 0
        res = await self._c(S.JOURNAL_ROWS).bulk_write(ops, ordered=True)
        return res.upserted_count

    async def list_journal(self, *, since: datetime | None = None, limit: int = 200,
                           events: Iterable[str] | None = None, fields: Iterable[str] | None = None) -> list[dict]:
        if limit <= 0:
            return []
        q: dict = {}
        if since is not None:
            q["ts"] = {"$gte": S.norm_dt(since)}
        if events is not None:
            q["event"] = {"$in": list(events)}
        return await self._c(S.JOURNAL_ROWS).find(q, _proj(fields)).sort(NEWEST).limit(int(limit)).to_list()

    # ---- account snapshots
    async def add_account_snapshot(self, doc: dict) -> None:
        await self._c(S.ACCOUNT_SNAPSHOTS).insert_one(S.prepare_ts_doc(doc))

    async def list_account_snapshots(self, *, since: datetime | None = None, limit: int = 10000,
                                     env: str | None = None) -> list[dict]:
        if limit <= 0:
            return []
        q = {} if since is None else {"ts": {"$gte": S.norm_dt(since)}}
        q.update(S.snapshot_env_query(env))
        rows = await self._c(S.ACCOUNT_SNAPSHOTS).find(q, NO_ID).sort(NEWEST).limit(int(limit)).to_list()
        return [S.with_snapshot_env(r) for r in rows]

    # ---- ops monitor (read-only)
    async def storage_stats(self) -> dict | None:
        try:
            st = await self._db.command("dbStats")
            used = float(st.get("storageSize", 0)) + float(st.get("indexSize", 0))
            return {"used_bytes": int(used), "limit_bytes": S.ATLAS_M0_LIMIT_BYTES}
        except Exception:                                    # noqa: BLE001
            return None

    async def ttl_index_report(self) -> dict | None:
        try:
            out = {}
            for coll in S.TTL_COLLECTIONS:
                idx = await self._c(coll).index_information()
                out[coll] = any(v.get("expireAfterSeconds") is not None for v in idx.values())
            return out
        except Exception:                                    # noqa: BLE001
            return None

    async def server_time(self) -> datetime | None:
        try:
            r = await self._client.admin.command("hello")
            t = r.get("localTime")
            if isinstance(t, datetime):
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except Exception:                                    # noqa: BLE001
            pass
        return None

    # ---- fills
    async def upsert_fills(self, fills: Iterable[dict]) -> int:
        ops = []
        for f in fills:
            doc = S.prepare_fill(f)
            ops.append(UpdateOne({"fill_id": doc["fill_id"]}, {"$set": doc}, upsert=True))
        if not ops:
            return 0
        res = await self._c(S.FILLS).bulk_write(ops, ordered=True)
        return res.upserted_count

    async def set_fill_fields(self, fill_id: str, fields: dict) -> bool:
        clean = S.normalize({k: v for k, v in dict(fields).items() if k not in ("fill_id", "ts", "_id")})
        if not clean:                                        # nothing to write: report existence, like MemoryStore
            return await self._c(S.FILLS).find_one({"fill_id": str(fill_id)}, {"_id": 1}) is not None
        res = await self._c(S.FILLS).update_one({"fill_id": str(fill_id)}, {"$set": clean})
        return res.matched_count > 0

    async def list_fills(self, *, since: datetime | None = None, until: datetime | None = None,
                         limit: int = 500, fields: Iterable[str] | None = None,
                         fill_ids: Iterable[str] | None = None) -> list[dict]:
        if limit <= 0:
            return []
        q = _window(since, None if until is None else until)
        if fill_ids is not None:
            q["fill_id"] = {"$in": sorted({str(i) for i in fill_ids})}      # served by fill_id_unique
        proj = NO_ID if fields is None else {"_id": 0, **{str(f): 1 for f in fields}}
        return await self._c(S.FILLS).find(q, proj).sort(NEWEST).limit(int(limit)).to_list()

    # ---- settlements
    async def upsert_settlements(self, rows: Iterable[dict]) -> int:
        ops = []
        for r in rows:
            doc = S.prepare_settlement(r)
            ops.append(UpdateOne({"ticker": doc["ticker"]}, {"$set": doc}, upsert=True))
        if not ops:
            return 0
        res = await self._c(S.SETTLEMENTS).bulk_write(ops, ordered=True)
        return res.upserted_count

    async def list_settlements(self, *, since: datetime | None = None, until: datetime | None = None,
                               limit: int = 500, fields: Iterable[str] | None = None) -> list[dict]:
        if limit <= 0:
            return []
        return await self._c(S.SETTLEMENTS).find(_window(since, until), _proj(fields)).sort(NEWEST)             .limit(int(limit)).to_list()

    async def set_settlement_fields(self, ticker: str, fields: dict) -> bool:
        clean = S.normalize({k: v for k, v in dict(fields).items() if k not in ("ticker", "ts", "_id")})
        if S.CLOSE_TIME in clean and S._safe_to_utc(clean[S.CLOSE_TIME]) is None:
            clean.pop(S.CLOSE_TIME)                  # close_time is never stored as null
        if not clean:
            return await self._c(S.SETTLEMENTS).find_one({"ticker": str(ticker)}, {"_id": 1}) is not None
        res = await self._c(S.SETTLEMENTS).update_one({"ticker": str(ticker)}, {"$set": clean})
        return res.matched_count > 0

    # ---- arbitrage log
    async def add_arb_opportunity(self, doc: Any) -> bool:
        new = S.prepare_arb_opportunity(doc)
        coll = self._c(S.ARB_OPPORTUNITIES)
        if new["executed"] == S.ARB_LOG_ONLY:
            lo = new["ts"] - timedelta(seconds=S.ARB_DEDUPE_SECONDS)
            # Atomic collapse into the newest matching sighting (single-doc update; served by sig_last_seen).
            hit = await coll.find_one_and_update(
                {"sig": new["sig"], "executed": S.ARB_LOG_ONLY, "last_seen": {"$gte": lo, "$lte": new["ts"]}},
                {"$inc": {"seen_count": 1}, "$set": {"last_seen": new["ts"]}},
                sort=[("last_seen", DESCENDING), ("_id", DESCENDING)], projection={"_id": 1})
            if hit is not None:
                return False
        await coll.insert_one(new)
        return True

    async def list_arb_opportunities(self, *, since: datetime | None = None, limit: int = 100,
                                     executed_only: bool = False) -> list[dict]:
        if limit <= 0:
            return []
        q = _window(since, None)
        if executed_only:
            q["executed"] = {"$ne": S.ARB_LOG_ONLY}
        return await self._c(S.ARB_OPPORTUNITIES).find(q, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    async def arb_spend_since(self, since: datetime, mode: str) -> float:
        # served by the ts index (ts_ttl); the prefix regex is anchored
        pipe = [{"$match": {"ts": {"$gte": S.norm_dt(since)}, "mode": str(mode),
                            "executed": {"$regex": "^(" + "|".join(S.ARB_SPEND_PREFIXES) + ")"}}},
                {"$group": {"_id": None, "total": {"$sum": "$cost"}}}]
        rows = await (await self._c(S.ARB_OPPORTUNITIES).aggregate(pipe)).to_list()
        return float(rows[0]["total"]) if rows else 0.0

    # ---- queue-position telemetry
    async def upsert_quote_episodes(self, docs: Iterable[dict]) -> int:
        ops = []
        for d in docs:
            doc = S.prepare_quote_episode(d)
            on_insert = {k: doc[k] for k in S.QUOTE_EPISODE_IMMUTABLE if k in doc and k != "order_id"}
            mutable = {k: v for k, v in doc.items() if k not in S.QUOTE_EPISODE_IMMUTABLE}
            update: dict = {"$setOnInsert": on_insert}
            if mutable:
                update["$set"] = mutable
            ops.append(UpdateOne({"order_id": doc["order_id"]}, update, upsert=True))
        if not ops:
            return 0
        res = await self._c(S.QUOTE_EPISODES).bulk_write(ops, ordered=True)
        return res.upserted_count

    async def set_quote_episode_close_time(self, order_id: str, close_time: datetime) -> bool:
        ct = S._safe_to_utc(close_time)
        if ct is None:
            return False
        # {close_time: None} matches a missing or null field: a close time, once set, is never overwritten
        res = await self._c(S.QUOTE_EPISODES).update_one({"order_id": str(order_id), S.CLOSE_TIME: None},
                                                         {"$set": {S.CLOSE_TIME: ct}})
        return res.modified_count > 0

    async def list_quote_episodes(self, *, since: datetime | None = None, env: str | None = None,
                                  status: str | None = None, limit: int = 5000,
                                  fields: Iterable[str] | None = None) -> list[dict]:
        if limit <= 0:
            return []
        q = _window(since, None)
        if env is not None:
            q["env"] = env
        if status is not None:
            q["status"] = status
        proj = NO_ID if fields is None else {"_id": 0, **{str(f): 1 for f in fields}}
        return await self._c(S.QUOTE_EPISODES).find(q, proj).sort(NEWEST).limit(int(limit)).to_list()

    # ---- shadow mode
    async def shadow_upsert_episodes(self, docs: list[dict]) -> int:
        ops = []
        for d in docs:
            doc = S.prepare_shadow_episode(d)
            update: dict = {"$set": doc}
            if "ts" not in doc:                              # partial update: ts is set once, when created
                update["$setOnInsert"] = {"ts": S.utcnow()}
            ops.append(UpdateOne({"_id": doc["ep_id"]}, update, upsert=True))
        if not ops:
            return 0
        res = await self._c(S.SHADOW_EPISODES).bulk_write(ops, ordered=True)
        return res.upserted_count

    async def _shadow_find(self, q: dict, sort: list, limit: int | None,
                           fields: Iterable[str] | None = None) -> list[dict]:
        proj = NO_ID if fields is None else {"_id": 0, "ep_id": 1, **{str(f): 1 for f in fields}}
        cur = self._c(S.SHADOW_EPISODES).find(q, proj).sort(sort)
        if limit is not None:
            if limit <= 0:
                return []
            cur = cur.limit(int(limit))
        keep = None if fields is None else set(fields) | {"ep_id"}
        return [S.shadow_with_defaults(d, keep) for d in await cur.to_list()]

    async def shadow_open_episodes(self, *, fields: Iterable[str] | None = None,
                                   limit: int | None = None) -> list[dict]:
        return await self._shadow_find({"end_ts": None}, [("join_ts", ASCENDING)], limit, fields)

    async def shadow_ended_unread(self, limit: int) -> list[dict]:
        return await self._shadow_find({"end_ts": {"$ne": None}, "trades_read": {"$ne": True}},
                                       [("end_ts", ASCENDING)], limit)

    async def shadow_unsettled(self, now: datetime, limit: int) -> list[dict]:
        return await self._shadow_find({"result": None, "close_time": {"$ne": None, "$lte": S.norm_dt(now)}},
                                       [("settle_checked_ts", ASCENDING), ("close_time", ASCENDING)], limit)

    async def shadow_settled(self, since: datetime, *, fields: Iterable[str] | None = None,
                             limit: int | None = None, scored_only: bool = False) -> list[dict]:
        q: dict = {"result": {"$ne": None}, "settled_ts": {"$gte": S.norm_dt(since)}}
        if scored_only:
            q = {"result": {"$in": list(S.SHADOW_SCORED_RESULTS)}, "fill": {"$ne": None},
                 "settled_ts": {"$gte": S.norm_dt(since)}}
        if limit is None:
            return await self._shadow_find(q, [("settled_ts", ASCENDING)], None, fields)
        return await self._shadow_find(q, [("settled_ts", DESCENDING)], limit, fields)

    async def shadow_recent(self, limit: int, state: str | None = None) -> list[dict]:
        q = {None: {}, "open": {"end_ts": None}, "ended": {"end_ts": {"$ne": None}, "result": None},
             "settled": {"result": {"$ne": None}}}
        if state not in q:
            raise ValueError(f"unknown shadow state {state!r}")
        return await self._shadow_find(q[state], [("join_ts", DESCENDING)], limit)

    async def shadow_count_new_since(self, ts: datetime) -> int:
        return await self._c(S.SHADOW_EPISODES).count_documents({"join_ts": {"$gte": S.norm_dt(ts)}})

    async def add_shadow_pass(self, doc: dict) -> None:
        await self._c(S.SHADOW_PASSES).insert_one(S.prepare_shadow_pass(doc))

    async def shadow_recent_passes(self, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        return await self._c(S.SHADOW_PASSES).find({}, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    async def get_shadow_summary(self) -> dict | None:
        return await self._c(S.META).find_one({"_id": S.SHADOW_SUMMARY_ID}, NO_ID)

    async def set_shadow_summary(self, doc: dict) -> None:
        body = S.strip_id(S.normalize(doc))
        await self._c(S.META).replace_one({"_id": S.SHADOW_SUMMARY_ID}, body, upsert=True)

    # ---- UX sprint additions
    async def set_fills_fields(self, updates: dict[str, dict]) -> int:
        ops = []
        for fid, fields in dict(updates).items():
            clean = S.normalize({k: v for k, v in dict(fields).items() if k not in ("fill_id", "ts", "_id")})
            if clean:
                ops.append(UpdateOne({"fill_id": str(fid)}, {"$set": clean}))     # no upsert
        if not ops:
            return 0
        n = 0
        for i in range(0, len(ops), 1000):                   # bounded batches
            res = await self._c(S.FILLS).bulk_write(ops[i:i + 1000], ordered=False)
            n += res.matched_count
        return n

    async def add_alert(self, doc: dict) -> dict:
        d = S.prepare_alert(doc)
        await self._c(S.ALERT_HISTORY).insert_one(dict(d))
        return d

    async def list_alerts(self, *, type: str | None = None, limit: int = 50,
                          before: datetime | None = None) -> list[dict]:
        if limit <= 0:
            return []
        q: dict = {}
        if type is not None:
            q["type"] = str(type)
        if before is not None:
            q["ts"] = {"$lt": S.norm_dt(before)}
        return await self._c(S.ALERT_HISTORY).find(q, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    async def add_period(self, doc: dict) -> dict:
        d = S.prepare_period(doc)
        try:
            await self._c(S.PERIODS).insert_one(dict(d))
        except DuplicateKeyError:
            raise ValueError("period id already exists") from None
        return d

    async def list_periods(self) -> list[dict]:
        return await self._c(S.PERIODS).find({}, NO_ID).sort([("started_at", ASCENDING), ("_id", ASCENDING)]).to_list()

    async def add_cash_flow(self, doc: dict) -> dict:
        d = S.prepare_cash_flow(doc)
        try:
            await self._c(S.CASH_FLOWS).insert_one(dict(d))
        except DuplicateKeyError:
            raise ValueError("cash flow id already exists") from None
        return d

    async def list_cash_flows(self, *, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        return await self._c(S.CASH_FLOWS).find(_window(since, until), NO_ID).sort(NEWEST).to_list()

    async def delete_cash_flow(self, id: str) -> bool:
        res = await self._c(S.CASH_FLOWS).delete_one({"id": str(id)})
        return res.deleted_count > 0

    async def save_recon_report(self, doc: dict) -> dict | None:
        d = S.prepare_recon_report(doc)
        # one atomic replace-upsert on (mode, day): a rerun replaces, never duplicates
        await self._c(S.RECON_REPORTS).replace_one({"mode": d["mode"], "day": d["day"]}, dict(d), upsert=True)
        return d

    async def latest_recon_report(self, mode: str | None = None) -> dict | None:
        q = {"mode": str(mode)} if mode is not None else {}
        return await self._c(S.RECON_REPORTS).find_one(q, NO_ID, sort=RECON_NEWEST)

    async def list_recon_reports(self, *, since_day: str | None = None, mode: str | None = None,
                                 limit: int = 100) -> list[dict]:
        if limit <= 0:
            return []
        q: dict = {}
        if mode is not None:
            q["mode"] = str(mode)
        if since_day is not None:
            q["day"] = {"$gte": str(since_day)}
        return await self._c(S.RECON_REPORTS).find(q, NO_ID).sort(RECON_NEWEST).limit(int(limit)).to_list()

    async def save_push_subscription(self, doc: dict) -> bool:
        d = S.prepare_push_subscription(doc)
        coll = self._c(S.PUSH_SUBSCRIPTIONS)
        cap = S.PUSH_SUBSCRIPTIONS_MAX
        created = d.pop("created_utc")

        async def refresh() -> bool:                         # existing subscription: update in place, never new
            res = await coll.update_one({"sub_id": d["sub_id"]}, {"$set": d})
            return res.matched_count == 0                    # False = updated; True only if it vanished meanwhile

        # Atomic cap: every new doc claims one of `cap` slots guarded by a unique sparse index on `slot`, so two
        # concurrent sign-ups can never both take the same slot and the table can never exceed `cap` rows.
        for _ in range(cap + 2):
            docs = await coll.find({}, {"_id": 0, "sub_id": 1, "slot": 1}).limit(cap * 2).to_list()
            if any(x.get("sub_id") == d["sub_id"] for x in docs):
                if not await refresh():
                    return False
                continue
            used = {x.get("slot") for x in docs}
            free = [s for s in range(cap) if s not in used]
            if len(docs) >= cap or not free:
                raise ValueError("too many push subscriptions")
            try:
                await coll.insert_one({**d, "created_utc": created, "slot": free[0]})
            except DuplicateKeyError:
                continue                                     # slot or sub_id raced: re-evaluate
            if await coll.count_documents({}) > cap:         # legacy slot-less rows: back out our own insert
                await coll.delete_one({"sub_id": d["sub_id"]})
                raise ValueError("too many push subscriptions")
            return True
        raise ValueError("too many push subscriptions")

    async def list_push_subscriptions(self) -> list[dict]:
        docs = await self._c(S.PUSH_SUBSCRIPTIONS).find({}, NO_ID).limit(S.PUSH_SUBSCRIPTIONS_MAX * 2).to_list()
        for x in docs:
            x.pop("slot", None)
        return docs

    async def delete_push_subscription(self, sub_id: str) -> bool:
        res = await self._c(S.PUSH_SUBSCRIPTIONS).delete_one({"sub_id": str(sub_id)})
        return res.deleted_count > 0

    async def clear_push_subscriptions(self) -> int:
        res = await self._c(S.PUSH_SUBSCRIPTIONS).delete_many({})
        return int(res.deleted_count)

    # ---- release process. Every lease change is ONE filtered find_one_and_update (or one unique insert):
    # never read-modify-write, so two instances can never both win.
    async def acquire_lease(self, holder: str, version: str, ttl_s: float, *, now: datetime | None = None) -> dict:
        holder = str(holder)
        ttl = _lease_ttl(ttl_s)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        coll = self._c(S.RUNNER_LEASE)
        key = {"_id": S.LEASE_ID}
        fields = {"version": str(version), "expires_at": t + ttl}
        for _ in range(3):                                   # a race can only be lost to a state change; re-evaluate
            # 1. already ours (expired or not): a renew, generation unchanged
            doc = await coll.find_one_and_update(
                {**key, "holder": holder}, {"$set": fields}, return_document=ReturnDocument.AFTER)
            # 2. held by someone else but expired: take over with generation + 1
            if doc is None:
                doc = await coll.find_one_and_update(
                    {**key, "holder": {"$ne": holder}, "expires_at": {"$lte": t}},
                    {"$set": {**fields, "holder": holder, "acquired_at": t}, "$inc": {"generation": 1}},
                    return_document=ReturnDocument.AFTER)
            # 3. no lease yet: the unique _id makes exactly one creator win
            if doc is None:
                try:
                    doc = {**key, "holder": holder, **fields, "generation": 1, "acquired_at": t, "handover": None}
                    await coll.insert_one(dict(doc))
                except DuplicateKeyError:
                    doc = None
            if doc is not None:
                return {"acquired": True, "generation": int(doc.get("generation") or 0), "holder": holder,
                        "expires_at": doc["expires_at"]}
            cur = await coll.find_one(key, NO_ID)
            if cur is not None and cur.get("holder") != holder and cur.get("expires_at") > t:
                return {"acquired": False, "generation": int(cur.get("generation") or 0),
                        "holder": cur.get("holder"), "expires_at": cur.get("expires_at")}
        cur = await coll.find_one(key, NO_ID) or {}
        return {"acquired": False, "generation": int(cur.get("generation") or 0), "holder": cur.get("holder"),
                "expires_at": cur.get("expires_at")}

    async def renew_lease(self, holder: str, generation: int, ttl_s: float, *, now: datetime | None = None) -> bool:
        ttl = _lease_ttl(ttl_s)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        doc = await self._c(S.RUNNER_LEASE).find_one_and_update(
            {"_id": S.LEASE_ID, "holder": str(holder), "generation": int(generation)},
            {"$set": {"expires_at": t + ttl}}, projection={"_id": 1})
        return doc is not None

    async def release_lease(self, holder: str, generation: int, *, handover: dict | None = None,
                            now: datetime | None = None) -> bool:
        t = S.norm_dt(now) if now is not None else S.utcnow()
        doc = await self._c(S.RUNNER_LEASE).find_one_and_update(
            {"_id": S.LEASE_ID, "holder": str(holder), "generation": int(generation)},
            {"$set": {"expires_at": t, "handover": S.normalize(handover) if handover is not None else None}},
            projection={"_id": 1})
        return doc is not None

    async def get_lease(self, *, now: datetime | None = None) -> dict | None:
        return _lease_out(await self._c(S.RUNNER_LEASE).find_one({"_id": S.LEASE_ID}, NO_ID))

    async def consume_handover(self, nonce: str, *, now: datetime | None = None) -> bool:
        if not isinstance(nonce, str) or not nonce:
            return False
        t = S.norm_dt(now) if now is not None else S.utcnow()
        try:                                                 # unique index on nonce: exactly one insert wins
            await self._c(S.HANDOVER_NONCES).insert_one({"nonce": nonce, "ts": t})
        except DuplicateKeyError:
            return False
        return True

    async def get_schema_version(self) -> int | None:
        doc = await self._c(S.META).find_one({"_id": S.META_SCHEMA_ID}, NO_ID)
        return None if doc is None or doc.get("version") is None else int(doc["version"])

    async def set_schema_version(self, version: int) -> None:
        await self._c(S.META).update_one({"_id": S.META_SCHEMA_ID},
                                         {"$set": {"version": int(version), "updated_utc": S.utcnow()}}, upsert=True)

    async def save_pending_release(self, doc: dict) -> dict:
        d = _new_release(doc, S.utcnow())
        await self._c(S.RELEASES).insert_one(dict(d))
        return d

    async def get_pending_release(self, release_id: str) -> dict | None:
        return await self._c(S.RELEASES).find_one({"id": str(release_id)}, NO_ID)

    async def latest_pending_release(self, *, now: datetime | None = None) -> dict | None:
        t = S.norm_dt(now) if now is not None else S.utcnow()
        return await self._c(S.RELEASES).find_one(
            {"status": "pending", "expires_at": {"$gt": t}}, NO_ID, sort=[("created_at", DESCENDING), ("_id", DESCENDING)])

    async def resolve_pending_release(self, release_id: str, decision: str, by: str, *,
                                      now: datetime | None = None) -> dict | None:
        _check_decision(decision)
        t = S.norm_dt(now) if now is not None else S.utcnow()
        return await self._c(S.RELEASES).find_one_and_update(
            {"id": str(release_id), "status": "pending", "expires_at": {"$gt": t}},
            {"$set": {"status": decision, "by": str(by), "at": t}}, projection=NO_ID,
            return_document=ReturnDocument.AFTER)

    # ---- monitor
    async def add_monitor_summary(self, doc: dict) -> None:
        await self._c(S.MONITOR_SUMMARIES).insert_one(S.prepare_ts_doc(doc, ts_fallbacks=("generated_utc",)))

    async def last_monitor_summary(self) -> dict | None:
        return await self._c(S.MONITOR_SUMMARIES).find_one({}, NO_ID, sort=NEWEST)

    async def list_monitor_summaries(self, *, since: datetime | None = None, limit: int = 100) -> list[dict]:
        if limit <= 0:
            return []
        q = {} if since is None else {"ts": {"$gte": S.norm_dt(since)}}
        return await self._c(S.MONITOR_SUMMARIES).find(q, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    # ---- audit
    async def add_audit(self, doc: dict) -> None:
        await self._c(S.AUDIT_LOG).insert_one(S.prepare_ts_doc(doc))

    async def list_audit(self, limit: int = 100) -> list[dict]:
        if limit <= 0:
            return []
        return await self._c(S.AUDIT_LOG).find({}, NO_ID).sort(NEWEST).limit(int(limit)).to_list()

    # ---- users
    async def get_user(self, username: str) -> dict | None:
        return await self._c(S.USERS).find_one({"username": username}, NO_ID)

    async def save_user(self, doc: dict) -> dict:
        username = doc.get("username")
        if not username:
            raise ValueError("user doc needs a username")
        S.check_no_secrets(doc)
        plain, mono = S.split_monotonic(S.strip_id(S.normalize(doc)))
        update: dict = {"$set": plain}             # always holds at least "username"
        if mono:
            update["$max"] = mono                  # S-18: epoch / TOTP step never move backwards
        return await self._c(S.USERS).find_one_and_update(
            {"username": username}, update, projection=NO_ID, upsert=True,
            return_document=ReturnDocument.AFTER)

    async def update_user_fields(self, username: str, *, set: dict | None = None,
                                 max: dict | None = None, inc: dict | None = None) -> dict | None:
        set_ = dict(set or {})
        set_.pop("username", None)
        set_.pop("_id", None)
        S.check_no_secrets(set_)
        plain, mono = S.split_monotonic(S.normalize(set_))
        mono.update({k: v for k, v in S.normalize(dict(max or {})).items() if v is not None})
        update: dict = {}
        if plain:
            update["$set"] = plain
        if mono:
            update["$max"] = mono
        if inc:
            update["$inc"] = dict(inc)
        if not update:
            return await self.get_user(username)
        return await self._c(S.USERS).find_one_and_update(
            {"username": username}, update, projection=NO_ID, upsert=False,
            return_document=ReturnDocument.AFTER)

    # ---- token revocation
    async def revoke_jti(self, jti: str, exp: datetime | int | float) -> None:
        await self._c(S.REVOKED_TOKENS).update_one(
            {"jti": str(jti)}, {"$set": {"exp": S.to_utc(exp)}}, upsert=True)

    async def is_revoked(self, jti: str) -> bool:
        # TTL deletion runs about once a minute, so also check exp explicitly.
        doc = await self._c(S.REVOKED_TOKENS).find_one({"jti": str(jti), "exp": {"$gt": S.utcnow()}}, {"_id": 1})
        return doc is not None

    # ---- cloud settings
    async def get_cloud_settings(self) -> dict | None:
        return await self._c(S.CLOUD_SETTINGS).find_one({"_id": S.CLOUD_ID}, NO_ID)

    async def save_cloud_settings(self, doc: dict, *, by: str) -> dict:
        S.check_no_secrets(doc)
        fields = S.strip_id(S.normalize(doc))
        fields["updated_utc"] = S.utcnow()
        fields["updated_by"] = by
        return await self._c(S.CLOUD_SETTINGS).find_one_and_update(
            {"_id": S.CLOUD_ID}, {"$set": fields}, projection=NO_ID, upsert=True,
            return_document=ReturnDocument.AFTER)


__all__ = ["MongoStore", "PyMongoError"]

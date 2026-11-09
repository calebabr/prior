"""Shared helpers for backup_export.py and restore_drill.py (stdlib only; pymongo is imported lazily).

A backup is a folder: one gzip NDJSON file per collection (`<collection>.ndjson.gz`) plus `manifest.json`
(database name at export time, per-collection document counts and sha256, UTC timestamp). Documents are encoded
with a tiny Extended-JSON subset ({"$date": iso}, {"$oid": hex}, {"$bytes": base64}) so datetimes survive.

Secrets: the connection string is read only from the environment and is never printed or written.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol

DEFAULT_DB = "kalshi_control"
# Collections skipped unless --include-sensitive: credentials / sessions / leases that must not sit in a backup
# folder by default (after a rebuild the owner re-runs the bootstrap CLI and logs in again).
SENSITIVE = ("users", "revoked_tokens", "push_subscriptions", "runner_lease", "handover_nonces")
BATCH = 500
_URI_RE = re.compile(r"mongodb(\+srv)?://[^\s'\"]+", re.I)


def scrub(text: str) -> str:
    """Remove any connection string from a message before it is printed."""
    return _URI_RE.sub("mongodb://<redacted>", str(text))


def enc(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return {"$date": v.astimezone(timezone.utc).isoformat()}
    if isinstance(v, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, dict):
        return {str(k): enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [enc(x) for x in v]
    if type(v).__name__ == "ObjectId":
        return {"$oid": str(v)}
    return v


def dec(v: Any) -> Any:
    if isinstance(v, dict):
        if set(v) == {"$date"}:
            return datetime.fromisoformat(v["$date"])
        if set(v) == {"$bytes"}:
            return base64.b64decode(v["$bytes"])
        if set(v) == {"$oid"}:
            try:
                from bson import ObjectId  # type: ignore
                return ObjectId(v["$oid"])
            except ImportError:
                return v["$oid"]
        return {k: dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [dec(x) for x in v]
    return v


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_collection(path: Path, docs: Iterable[dict]) -> int:
    n = 0
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:   # mtime=0: stable checksum
        for d in docs:
            gz.write((json.dumps(enc(d), sort_keys=True, separators=(",", ":")) + "\n").encode())
            n += 1
    return n


def read_collection(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield dec(json.loads(line))


# ---- database adapters (the real one and an in-memory one for tests)
class Db(Protocol):
    name: str

    def collection_names(self) -> list[str]: ...
    def find_all(self, coll: str) -> Iterator[dict]: ...
    def count(self, coll: str) -> int: ...
    def insert_many(self, coll: str, docs: list[dict]) -> None: ...
    def drop(self, coll: str) -> None: ...
    def close(self) -> None: ...


class MemoryDb:
    def __init__(self, name: str, data: dict[str, list[dict]] | None = None) -> None:
        self.name = name
        self.data: dict[str, list[dict]] = {k: [dict(d) for d in v] for k, v in (data or {}).items()}

    def collection_names(self) -> list[str]:
        return sorted(self.data)

    def find_all(self, coll: str) -> Iterator[dict]:
        yield from (dict(d) for d in self.data.get(coll, []))

    def count(self, coll: str) -> int:
        return len(self.data.get(coll, []))

    def insert_many(self, coll: str, docs: list[dict]) -> None:
        self.data.setdefault(coll, []).extend(dict(d) for d in docs)

    def drop(self, coll: str) -> None:
        self.data.pop(coll, None)

    def close(self) -> None:
        pass


class MongoDb:
    """Synchronous PyMongo adapter. Constructed lazily; never logs the URI."""

    def __init__(self, uri: str, name: str) -> None:
        from pymongo import MongoClient
        self._client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=8000, appname="kalshi-backup")
        self.name = name
        self._db = self._client[name]

    def collection_names(self) -> list[str]:
        return sorted(n for n in self._db.list_collection_names() if not n.startswith("system."))

    def find_all(self, coll: str) -> Iterator[dict]:
        yield from self._db[coll].find({}).batch_size(BATCH)

    def count(self, coll: str) -> int:
        return self._db[coll].count_documents({})

    def insert_many(self, coll: str, docs: list[dict]) -> None:
        self._db[coll].insert_many(docs, ordered=False)

    def drop(self, coll: str) -> None:
        self._db[coll].drop()

    def close(self) -> None:
        self._client.close()

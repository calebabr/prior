"""Durable journal sync (item 13): local CSV journal -> store, without losing or duplicating rows.

The gap this closes: `JournalTail.read_new()` advances its byte offset BEFORE the rows reach the store. If
`store.add_journal_rows` then fails (Atlas blip, process killed mid-call), the rows are never read again from the
file; once the (ephemeral) disk is replaced, or the startup rehydrate rewrites the file from the store, they are gone.

`DurableTail` commits the offset only after the store accepted the rows (rewind on any failure), and
`replay_local` pushes whatever a local file holds into the store at startup, BEFORE the file is rewritten from the
store. Both rely on `add_journal_rows` being idempotent on row_hash, so a replay or a retry never duplicates.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from . import botfiles

log = logging.getLogger("control_api.journalsync")
REPLAY_CHUNK = 500


class DurableTail:
    """A JournalTail whose offset moves only after the store has the rows."""

    def __init__(self, path: Path, tail: botfiles.JournalTail | None = None) -> None:
        self.tail = tail or botfiles.JournalTail(path)

    async def sync(self, push: Callable[[list[dict]], Awaitable[int]]) -> tuple[list[dict], int]:
        """Read new rows, `await push(rows)` (the store write); on any failure the offset is rewound so the next
        call reads the same rows again. Returns (rows, number newly stored). Re-raises the push error."""
        before = self.tail.offset
        rows = self.tail.read_new()
        if not rows:
            return [], 0
        try:
            n = await push(rows)
        except BaseException:                       # incl. CancelledError at shutdown: keep the rows for the next sync
            self.tail.offset = before
            raise
        return rows, n


async def replay_local(path: Path, store, *, chunk: int = REPLAY_CHUNK) -> int:
    """Push every row of the local journal file into the store (idempotent on row_hash). Call at startup before the
    file is rehydrated from the store. Returns the number of rows the store did not have. Never raises."""
    try:
        rows = botfiles.JournalTail(path).read_new()          # fresh tail: offset 0, whole file
    except Exception as e:  # noqa: BLE001
        log.warning("journal replay read failed: %s", type(e).__name__)
        return 0
    added = 0
    for i in range(0, len(rows), chunk):
        try:
            added += await store.add_journal_rows(rows[i:i + chunk])
        except Exception as e:  # noqa: BLE001
            log.warning("journal replay write failed: %s", type(e).__name__)
            break
    return added

"""Cooperative CPU yielding so Stop is never starved by an Analytics build (load suite scenario `contention`).

On Render free the whole process gets about a tenth of one core. A 180-day Analytics rebuild is pure Python CPU on
the analytics worker thread; while it runs it takes most of that tenth, and a Stop tap that needs only a few
milliseconds of CPU waits for its share behind it. Two cheap, result-neutral levers fix the share:

* Urgent requests. `urgent()` marks a request that must not wait (the main app wraps POST /api/bot/stop in it). It
  is a plain counter: entering and leaving never raise, never block, and never touch the store, Kalshi or the
  network, so Stop does not depend on anything new.
* Checkpoints. The heavy build loops call `checkpoint()` every few rows. Outside an urgent request it costs one
  global read and returns. While a Stop is in flight, a checkpoint on a COOPERATIVE thread (a build started through
  `cooperative(fn)`, i.e. the analytics worker) waits on an Event instead of computing: it holds no lock, releases
  the interpreter lock and uses no CPU, so the event loop gets the whole CPU share until Stop has answered. The wait
  is bounded per build (MAX_PAUSE_PER_BUILD_S) so a stuck marker can only slow a build, never stop it. On any other
  thread (the event loop, tests calling a builder directly) a checkpoint never waits.
* `aio_checkpoint()` is the event-loop twin for long synchronous loops that run ON the loop (the in-memory store's
  big reads): it yields one loop iteration, and while a Stop is in flight it sleeps in short steps (bounded).

Scheduling only: no value, order or byte of any response changes.
"""
from __future__ import annotations

import asyncio
import threading
import time

PAUSE_SLICE_S = 0.05                 # one wait while a Stop is in flight (re-checked after each slice)
MAX_PAUSE_PER_BUILD_S = 3.0          # a build pauses at most this long in total, however long Stops take
LOOP_PAUSE_SLICE_S = 0.002           # aio_checkpoint: one sleep while urgent
LOOP_MAX_PAUSE_S = 0.5               # aio_checkpoint: at most this long per call

_urgent = 0                          # requests in flight that must not wait (only read by the workers)
_lock = threading.Lock()             # held for an increment only; never while waiting
_clear = threading.Event()           # set while no urgent request is in flight
_clear.set()
_local = threading.local()


class _Urgent:
    """Context manager (sync and async) marking one urgent request. Never raises."""

    def __enter__(self):
        global _urgent
        try:
            with _lock:
                _urgent += 1
                _clear.clear()
        except Exception:  # noqa: BLE001
            pass
        return self

    def __exit__(self, *exc):
        global _urgent
        try:
            with _lock:
                _urgent = max(0, _urgent - 1)
                if _urgent == 0:
                    _clear.set()
        except Exception:  # noqa: BLE001
            pass
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)


def urgent() -> _Urgent:
    return _Urgent()


def in_flight() -> int:
    """Urgent requests currently in flight (diagnostics and tests)."""
    return _urgent


def cooperative(fn, /, *args, **kwargs):
    """Run fn(*args, **kwargs) with this thread's checkpoints allowed to pause (the analytics worker uses this)."""
    prev = getattr(_local, "budget", None)
    _local.budget = MAX_PAUSE_PER_BUILD_S
    try:
        return fn(*args, **kwargs)
    finally:
        _local.budget = prev


def checkpoint() -> None:
    """Call every few rows inside a heavy build loop. Returns at once unless a Stop is in flight AND this thread runs
    a cooperative build; then it waits (no CPU, no lock held) until the Stop has answered or the budget is spent."""
    if _urgent:
        _pause()


def _pause() -> None:
    budget = getattr(_local, "budget", None)
    if not budget or budget <= 0:
        return                        # not a cooperative build thread (never block the loop), or budget spent
    while _urgent and budget > 0:
        t0 = time.monotonic()
        _clear.wait(min(PAUSE_SLICE_S, budget))
        budget -= time.monotonic() - t0
    _local.budget = max(0.0, budget)


async def aio_checkpoint() -> None:
    """Event-loop checkpoint: give other coroutines (a Stop) a turn; while a Stop is in flight, step aside in short
    sleeps for at most LOOP_MAX_PAUSE_S."""
    await asyncio.sleep(0)
    if _urgent:
        end = time.monotonic() + LOOP_MAX_PAUSE_S
        while _urgent and time.monotonic() < end:
            await asyncio.sleep(LOOP_PAUSE_SLICE_S)

"""A virtual-time event loop for shutdown/handover tests (no real sleeps).

``loop.time()`` is a virtual clock. Whenever the loop would block waiting for its next timer, the clock jumps straight
to that timer instead (after a zero-timeout poll, so ready sockets and thread callbacks are still picked up). So
``asyncio.sleep(10)``, ``asyncio.wait_for(..., 5)`` and the runner's ``loop.time()`` deadlines all run instantly in
real time while keeping their order and their virtual durations. Code that only waits on another thread (a hung
"Kalshi" call) never finishes in virtual time, which is exactly a hang.

Each test makes its own loop (parallel-safe, no global state)."""
from __future__ import annotations

import asyncio
import selectors
from typing import Any, Awaitable, Callable


class _VirtualSelector(selectors.SelectSelector):
    def __init__(self, clock: list) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout: float | None = None):
        ready = super().select(0)
        if ready:
            return ready
        if timeout is None:                       # nothing scheduled: only threads can wake us; wait a little, really
            return super().select(0.01)
        if timeout > 0:
            self._clock[0] += timeout             # jump to the next timer
        return []


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._vclock = [1000.0]
        super().__init__(_VirtualSelector(self._vclock))

    def time(self) -> float:
        return self._vclock[0]


def run_virtual(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    loop = VirtualLoop()
    try:
        return loop.run_until_complete(fn(*args))
    finally:
        loop.close()


class SlowStore:
    """Wraps a store: every coroutine method first sleeps `delay` virtual seconds (inf = hangs forever)."""

    def __init__(self, inner: Any, delay: float) -> None:
        self._inner = inner
        self.delay = delay
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not asyncio.iscoroutinefunction(attr):
            return attr

        async def slow(*a: Any, **k: Any) -> Any:
            self.calls.append(name)
            if self.delay == float("inf"):
                await asyncio.Event().wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            return await attr(*a, **k)
        return slow

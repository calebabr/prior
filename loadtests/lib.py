"""Shared pieces: latency recording, the API client, the local server handle, the memory sampler, target guards."""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
STOP_P99_BAR_S = 1.0            # Stop must answer in under a second at p99, even under load
MEMORY_BUDGET_MB = 512.0        # the Render free instance
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100); None for no data."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]


@dataclass
class Rec:
    label: str
    status: int                 # 0 = transport error / timeout
    secs: float
    detail: str = ""            # error type, or the JSON `detail` of a 4xx/5xx (never a body that could hold secrets)


@dataclass
class Recorder:
    recs: list[Rec] = field(default_factory=list)

    def add(self, r: Rec) -> None:
        self.recs.append(r)

    def of(self, *labels: str) -> list[Rec]:
        return [r for r in self.recs if not labels or r.label in labels]

    def summary(self, *labels: str) -> dict:
        rs = self.of(*labels)
        lat = [r.secs for r in rs]
        by = Counter(r.status for r in rs)
        det = Counter(r.detail for r in rs if r.detail and r.status >= 400)
        ms = lambda v: None if v is None else round(v * 1000, 1)       # noqa: E731
        return {"n": len(rs), "status": {str(k): v for k, v in sorted(by.items())},
                "n_5xx": sum(v for k, v in by.items() if k >= 500), "n_errors": by.get(0, 0),
                "n_429": by.get(429, 0), "detail": dict(det),
                "p50_ms": ms(pct(lat, 50)), "p95_ms": ms(pct(lat, 95)), "p99_ms": ms(pct(lat, 99)),
                "max_ms": ms(max(lat) if lat else None)}


class Api:
    """httpx wrapper that records every call. Never raises on HTTP errors or transport errors (they are data)."""

    def __init__(self, base: str, token: str | None, rec: Recorder, *, timeout: float = 30.0, max_conn: int = 100):
        self.base, self.rec = base.rstrip("/"), rec
        self.token = token
        self.http = httpx.AsyncClient(base_url=self.base, timeout=timeout,
                                      limits=httpx.Limits(max_connections=max_conn, max_keepalive_connections=max_conn))

    async def aclose(self) -> None:
        await self.http.aclose()

    def auth(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return {**h, **(extra or {})}

    async def call(self, label: str, method: str, path: str, *, headers: dict | None = None, anon: bool = False,
                   **kw) -> tuple[int, dict | None, httpx.Response | None]:
        h = (headers or {}) if anon else self.auth(headers)
        t0 = time.perf_counter()
        try:
            r = await self.http.request(method, path, headers=h, **kw)
        except Exception as e:  # noqa: BLE001
            self.rec.add(Rec(label, 0, time.perf_counter() - t0, type(e).__name__))
            return 0, None, None
        dt = time.perf_counter() - t0
        try:
            body = r.json()
        except ValueError:
            body = None
        det = ""
        if r.status_code >= 400 and isinstance(body, dict):
            d = body.get("detail")
            det = d if isinstance(d, str) else "validation"
        self.rec.add(Rec(label, r.status_code, dt, det))
        return r.status_code, body, r


# ---------------------------------------------------------------- targets
def check_remote_target(url: str, confirm_demo: bool) -> str:
    """Normalise and vet a --target URL. Raises SystemExit with the reason. Remote runs are for the Render DEMO deploy only."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SystemExit(f"--target must be 'local' or an http(s) URL, got {url!r}")
    host = u.hostname.lower()
    if host not in LOCAL_HOSTS and u.scheme != "https":
        raise SystemExit("--target: a remote host must use https")
    if re.search(r"live|prod", host):
        raise SystemExit(f"--target host {host!r} looks like a live/production deploy; load tests run on demo only")
    if not confirm_demo:
        raise SystemExit("--target <url> needs --confirm-demo (this sends bursts of requests, including Stop calls, "
                         "to that deploy; use the DEMO deploy only)")
    return f"{u.scheme}://{u.netloc}"


def remote_state_refusal(body: dict | None) -> str | None:
    """Why a remote run must not proceed, from GET /api/status, or None when the deploy is demo-only. The deploy must be
    in demo mode for maker AND arbitrage, hold no live arming, and not be live-capable at all (live_allowed): after go-live
    the single service is the live deploy and a load run there could stop real trading or lock the owner out."""
    b = body or {}
    if b.get("mode") != "demo":
        return f"mode={b.get('mode')!r}; the target must be in demo mode"
    arb = b.get("arb") if isinstance(b.get("arb"), dict) else {}
    for name, v in (("arb_mode", b.get("arb_mode")), ("arb.arb_mode", arb.get("arb_mode"))):
        if v is not None and v != "demo":
            return f"{name}={v!r}; arbitrage must be in demo mode too"
    arming = b.get("arming") if isinstance(b.get("arming"), dict) else {}
    if any(arming.get(k) is True for k in ("maker", "arbitrage")):
        return "live trading is armed on the target"
    if b.get("live_allowed") is True or arb.get("live_allowed") is True:
        return "the target is live-capable (live_allowed); load tests run on a demo-only deploy"
    return None


# ---------------------------------------------------------------- local server
class LocalServer:
    """The throwaway API from loadtests.server as a subprocess on an OS-chosen port."""

    def __init__(self, rows: int, pass_seconds: float, startup_timeout: float = 180.0, push_rate_limit: int = 1000,
                 extra_args: list[str] | None = None):
        self.rows, self.pass_seconds, self.startup_timeout, self.push_rate_limit = (
            rows, pass_seconds, startup_timeout, push_rate_limit)
        self.extra_args = list(extra_args or [])
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}
        self._err = tempfile.TemporaryFile()

    def start(self) -> dict:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "loadtests.server", "--rows", str(self.rows), "--pass-seconds",
             str(self.pass_seconds), "--push-rate-limit", str(self.push_rate_limit), *self.extra_args],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=self._err, text=True)
        out: list[str] = []
        t = threading.Thread(target=lambda: out.append(self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(self.startup_timeout)
        if not out or not out[0].strip():
            self.stop()
            raise RuntimeError("local load-test server did not start: " + self._stderr_tail())
        self.info = json.loads(out[0])
        return self.info

    def _stderr_tail(self) -> str:
        try:
            self._err.seek(0)
            return self._err.read().decode("utf-8", "replace")[-1500:]
        except Exception:  # noqa: BLE001
            return "(no stderr)"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.info['port']}"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- memory
def rss_mb(pid: int) -> float | None:
    """Resident memory of the process and its children, or None without psutil."""
    try:
        import psutil
        p = psutil.Process(pid)
        return round((p.memory_info().rss + sum(c.memory_info().rss for c in p.children(recursive=True))) / 1048576, 1)
    except Exception:  # noqa: BLE001
        return None


class MemSampler:
    def __init__(self, pid: int | None, every: float = 0.5):
        self.pid, self.every = pid, every
        self.samples: list[tuple[float, float]] = []          # (seconds since start, MB)
        self._task: asyncio.Task | None = None
        self._t0 = time.monotonic()

    async def _run(self) -> None:
        while True:
            v = rss_mb(self.pid) if self.pid else None
            if v is not None:
                self.samples.append((round(time.monotonic() - self._t0, 2), v))
            await asyncio.sleep(self.every)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def mark(self) -> int:
        return len(self.samples)

    def peak(self, since: int = 0) -> float | None:
        v = [m for _, m in self.samples[since:]]
        return max(v) if v else None

    def growth(self, warmup_frac: float = 0.25, window_frac: float = 0.25) -> dict | None:
        """Mean of the first window after warm-up vs the mean of the last window (both MB)."""
        v = [m for _, m in self.samples]
        if len(v) < 8:
            return None
        n = len(v)
        w0 = int(n * warmup_frac)
        k = max(2, int(n * window_frac))
        first, last = sum(v[w0:w0 + k]) / k, sum(v[-k:]) / k
        return {"first_mb": round(first, 1), "last_mb": round(last, 1), "growth_mb": round(last - first, 1),
                "growth_pct": round((last - first) / first * 100, 1) if first else None, "samples": n}

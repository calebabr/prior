"""Throwaway LOCAL API for the load tests: real FastAPI app + real Runner, MemoryStore, fake Kalshi, fake pass.

    python -m loadtests.server [--rows 20000] [--pass-seconds 2.0]

Prints one JSON line on stdout when ready ({"ready": true, "port": ..., credentials ...}), then serves on
127.0.0.1 until the process is terminated. Everything is throwaway:
- STORE=memory; no real KALSHI_* variable is read (they are stripped), the Kalshi client is an in-process fake that
  records cancels (3 resting mm- orders and 1 manual order), and the strategy's main() is replaced by a sleep, so
  a "bot pass" costs time but touches no network and no order code;
- bot files (config, journal, kill switches) live in a temp directory, never the real ones;
- test credentials are generated per run and returned in the ready line (local only, never written to disk).
Extra routes live under /__loadtest/ (stats, reset). They exist only in this launcher, never in control_api.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_KEY_ID, FAKE_KEY = "loadtest-demo-key-id", "loadtest-not-a-real-key"


class FakeKalshi:
    """Records cancels. get_orders returns the not-yet-cancelled resting orders (mm- bot orders + one manual)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.orders = [{"order_id": f"o{i}", "client_order_id": f"mm-{i}", "ticker": f"KXLOAD-{i}"}
                           for i in range(3)]
            self.orders.append({"order_id": "manual-1", "client_order_id": "manual-1", "ticker": "KXLOAD-M"})
            self.cancelled: list[str] = []

    def get_orders(self, status=None):
        with self.lock:
            return list(self.orders)

    def cancel_order(self, order_id, market_ticker=None):
        with self.lock:
            self.cancelled.append(order_id)
            self.orders = [o for o in self.orders if o["order_id"] != order_id]

    def get_fills(self, **kw):
        return []

    def get_settlements(self, **kw):
        return []

    def get_balance(self, *a, **k):
        return {"balance": 10000}

    def get_positions(self, *a, **k):
        return {}


class NoPublicData:
    def get_markets(self, **_kw):
        return []

    def get_orderbook(self, *_a, **_kw):
        return {}


class PassCounter:
    def __init__(self, seconds: float) -> None:
        self.seconds, self.lock = seconds, threading.Lock()
        self.started = self.finished = self.active = self.max_active = 0

    def main(self) -> int:                       # replaces maker_strategy.main(): runs in the runner's worker thread
        with self.lock:
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.seconds)
        finally:
            with self.lock:
                self.active -= 1
                self.finished += 1
        return 0


def _rss_mb() -> float | None:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001
        return None


async def build(args, sock_port: int):
    import pyotp
    from fastapi import Request
    from cryptography.hazmat.primitives.asymmetric import ec
    from control_api.auth import security as sec
    from control_api.db import MemoryStore
    from control_api.db.seed import seed
    from control_api.main import create_app
    from control_api.routes import alerts as alerts_routes
    from control_api.runner import Runner
    from control_api.services import botfiles
    from control_api.services import webpush as w
    from control_api.settings import Settings
    from loadtests.seed import seed_bulk

    for k in [k for k in os.environ if k.upper().startswith("KALSHI_") or k.upper() == "ARB_LIVE"]:
        os.environ.pop(k, None)
    os.environ["KALSHI_DEMO_API_KEY_ID"], os.environ["KALSHI_DEMO_PRIVATE_KEY"] = FAKE_KEY_ID, FAKE_KEY

    tmp = Path(tempfile.mkdtemp(prefix="loadtest_bot_"))
    ms = botfiles.strategy()
    (tmp / "logs").mkdir()
    cfg = tmp / "maker_config.json"
    shutil.copy(ROOT / "kalshi_bot" / "maker_config.json", cfg)
    ms.CONFIG_PATH, ms.LOG, ms.KILL = cfg, tmp / "logs" / "maker_journal.csv", tmp / "MAKER_KILL_SWITCH"
    from control_api.services import arb as arbsvc
    sc = arbsvc.scanner()
    sc.KILL_PATH, sc.LOG_PATH = tmp / "ARB_KILL_SWITCH", tmp / "logs" / "arb_opportunities.csv"
    import maker_monitor as mm
    mm.HERE, mm.LOGS, mm.JOURNAL = tmp, tmp / "logs", tmp / "logs" / "maker_journal.csv"
    mm.CONFIG, mm.KILL = cfg, tmp / "MAKER_KILL_SWITCH"

    counter = PassCounter(args.pass_seconds)
    ms.main = counter.main
    kalshi = FakeKalshi()

    vapid = w.b64u(ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big"))
    settings = Settings.from_env({
        "STORE": "memory", "RUNNER_ENABLED": "true", "ALLOWED_ORIGIN": "http://localhost:5173",
        "TICK_TOKEN": secrets.token_urlsafe(24), "PUBLIC_URL": f"http://127.0.0.1:{sock_port}",
        "VAPID_PRIVATE_KEY": vapid, "VAPID_SUBJECT": "mailto:loadtest@example.invalid",
    })
    alerts_routes.RATE_LIMITS["push_subscribe"] = (args.push_rate_limit, 60.0)   # see README: cap race, not limiter
    if args.fast_passes:
        Runner.next_due_utc = lambda self: None          # a pass whenever a tick finds none running
    store = MemoryStore()
    await seed(store)
    counts = await seed_bulk(store, rows=args.rows, days=180)

    users = {}
    for name in ("owner", "locktest"):
        pw, secret = secrets.token_urlsafe(12), pyotp.random_base32()
        await store.save_user({"username": name, "password_hash": sec.hash_password(pw),
                               "totp_secret_enc": sec.encrypt_totp_secret(settings, secret),
                               "last_totp_step": None, "failed_logins": 0})
        users[name] = {"password": pw, "totp_secret": secret}
    app = create_app(settings, store=store, kalshi_factory=lambda mode: kalshi,
                     public_factory=lambda env: NoPublicData())
    runner = app.state.runner
    runner.last_monitor_utc = datetime.now(timezone.utc)          # no weekly health check mid-test
    await runner.rehydrate()

    @app.get("/__loadtest/stats")
    async def stats() -> dict:
        return {"passes_started": counter.started, "passes_finished": counter.finished,
                "max_concurrent_passes": counter.max_active, "cancelled": list(kalshi.cancelled),
                "resting_left": [o["order_id"] for o in kalshi.get_orders()],
                "stop_flag_file": Path(ms.KILL).exists(), "rss_mb": _rss_mb(),
                "threads": threading.active_count()}

    @app.post("/__loadtest/reset")
    async def reset(request: Request) -> dict:
        kalshi.reset()
        await store.save_user({"username": "owner", "last_totp_step": None, "failed_logins": 0,
                               "locked_until": None})
        return {"ok": True}

    token = sec.create_token(settings, "owner", sec.TYP_ACCESS, 4 * 3600)[0]
    ready = {"ready": True, "pid": os.getpid(), "port": sock_port, "token": token, "seeded": counts,
             "tick_token": settings.tick_token, "users": users, "tmp": str(tmp)}
    return app, ready, tmp


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=20000, help="synthetic fills over 180 days (default: the 20,000 cap)")
    ap.add_argument("--pass-seconds", type=float, default=2.0, help="how long the fake bot pass takes")
    ap.add_argument("--no-fast-passes", dest="fast_passes", action="store_false",
                    help="keep the real poll_minutes schedule (default: a tick starts a pass whenever none runs)")
    ap.add_argument("--push-rate-limit", type=int, default=1000, help="push_subscribe requests/min (prod: 10)")
    ap.add_argument("--switch-interval", type=float, default=None,
                    help="diagnostic: sys.setswitchinterval(seconds) to test GIL starvation (default: Python's 0.005)")
    args = ap.parse_args()
    if args.switch_interval:
        sys.setswitchinterval(args.switch_interval)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))                          # port chosen by the OS: no fixed ports, parallel-safe
    port = sock.getsockname()[1]
    import uvicorn
    app, ready, tmp = asyncio.run(build(args, port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))

    def announce():                                      # after lifespan startup (runner.start) has run
        while not server.started:
            time.sleep(0.05)
        print(json.dumps(ready), flush=True)
    threading.Thread(target=announce, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

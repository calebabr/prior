"""Shared fixtures. No real network, Atlas, Kalshi or cron-job.org; bot files are redirected to tmp."""
from __future__ import annotations

import ipaddress
import shutil
import socket
from pathlib import Path

import httpx
import pyotp
import pytest
import pytest_asyncio

from control_api.auth import security as sec
from control_api.db import MemoryStore
from control_api.main import create_app
from control_api.services import botfiles
from control_api.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://app.example.com"
PASSWORD = "correct horse battery staple"
TICK = "tick-secret-token"
# argon2 hashing costs ~0.3 s; hash the fixed test password once per process instead of once per test.
# (Tests that exercise hash/verify themselves still call sec.hash_password directly.)
_PW_HASH = sec.hash_password(PASSWORD)


class FakeKalshi:
    def __init__(self, raise_on_orders=False):
        self.raise_on_orders = raise_on_orders
        self.cancelled = []
        self.orders = [
            {"order_id": "o1", "client_order_id": "mm-aaa", "ticker": "T1"},
            {"order_id": "o2", "client_order_id": "manual-1", "ticker": "T2"},
            {"order_id": "o3", "client_order_id": "mm-bbb", "ticker": "T3"},
        ]

    def get_orders(self, status=None):
        if self.raise_on_orders:
            raise RuntimeError("kalshi down")
        return list(self.orders)

    def cancel_order(self, order_id, market_ticker=None):
        self.cancelled.append(order_id)


class FakeCron:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def sync(self, job_id, **kw):
        self.calls.append((job_id, kw))
        if self.fail:
            raise RuntimeError("cron down")
        return 4242


class FakePublic:
    """Default unauthenticated Kalshi client for tests: every read is empty; no network."""
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: {}


class NetworkBlocked(RuntimeError):
    pass


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes more than ~1 s (real sleeps, load, 20k docs); excluded by -m \"not slow\"")
    config.addinivalue_line("markers", "allow_network: this test may open real outbound connections")


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host in ("localhost", "", None, "test", "testserver"):
        return True
    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """Fail any test that tries to reach a non-loopback host (real Kalshi/Atlas/ntfy/cron-job.org/Vercel)."""
    if request.node.get_closest_marker("allow_network"):
        return
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def check(addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if isinstance(addr, str) or _is_local(host):   # AF_UNIX paths are str
            return
        raise NetworkBlocked(f"real network connection attempted to {host!r} in a test")

    def connect(self, addr):
        check(addr)
        return real_connect(self, addr)

    def connect_ex(self, addr):
        check(addr)
        return real_connect_ex(self, addr)

    def getaddrinfo(host, *a, **k):
        if not _is_local(host):
            raise NetworkBlocked(f"real DNS lookup attempted for {host!r} in a test")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class Env:
    pass


DEMO_SERIES = ["KXDEMOA", "KXDEMOB", "KXDEMOC", "KXDEMOD", "KXDEMOE", "KXDEMOF"]
DEMO_ARB_SERIES = ["KXDEMOA", "KXDEMOB", "KXDEMOG", "KXDEMOH"]


def _pin_series_in(cfg: dict) -> dict:
    """Rewrite the series lists of a config dict to the generic test tickers (in place; returns it)."""
    if "series" in cfg:
        cfg["series"] = list(DEMO_SERIES)
    for holder in (cfg, cfg.get("arb") if isinstance(cfg.get("arb"), dict) else {}):
        if "arb_series" in holder:
            holder["arb_series"] = list(DEMO_ARB_SERIES)
    return cfg


@pytest.fixture(autouse=True)
def _pin_generic_series(monkeypatch):
    """Tests use the generic KXDEMO* tickers whichever strategy is installed (public stand-in or the overlaid private
    one, whose series lists are different). The API validates against these two lists at call time, so pin both."""
    from control_api.services import arb as arbsvc
    monkeypatch.setitem(botfiles.strategy().DEFAULTS, "series", list(DEMO_SERIES))
    monkeypatch.setattr(arbsvc.scanner(), "DEFAULT_SERIES", list(DEMO_ARB_SERIES))


FAKE_BASELINE = {"trade_through_share": 0.5, "avg_move_60m_c": -1.0}     # made-up numbers


def fake_expectations_doc() -> dict:
    """Fake benchmark file: one entry per INSTALLED config cell (stand-in example cells, or the overlaid real ones;
    generated at test time, never written into the repo) plus the synthetic test cells."""
    from control_api.services import analytics as an
    from control_api.tests.fake_cells import TEST_CELLS
    ids = list(dict.fromkeys(an.cell_id(c) for c in an.parse_cells(list(botfiles.strategy().DEFAULTS["cells"])
                                                                  + TEST_CELLS)))
    return {"source": "TEST FIXTURE - fake numbers, not research results",
            "default": {"win_rate": None, "roi": 0.05},
            "cells": {cid: {"win_rate": round(0.5 + i / 100, 2), "roi": round(0.1 + i / 1000, 3),
                            "n_ref": 100 + i} for i, cid in enumerate(ids)},
            "fill_quality_baseline": dict(FAKE_BASELINE)}


@pytest.fixture(scope="session")
def fake_expectations_path(tmp_path_factory) -> Path:
    import json
    p = tmp_path_factory.mktemp("bench") / "cell_expectations_fake.json"
    p.write_text(json.dumps(fake_expectations_doc(), indent=2), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _fake_cell_expectations(monkeypatch, fake_expectations_path):
    """Tests never read real (private) benchmark numbers: point the loader at fake ones (S-32)."""
    monkeypatch.setenv("CELL_EXPECTATIONS_PATH", str(fake_expectations_path))


@pytest.fixture
def real_strategy(monkeypatch):
    """Simulate the private strategy overlaid (IS_STANDIN false), to test the env gates for LIVE on their own."""
    monkeypatch.setattr(botfiles, "is_standin", lambda: False)


@pytest.fixture
def standin(monkeypatch):
    """Force the public stand-in (IS_STANDIN true), whatever kalshi_bot is installed."""
    monkeypatch.setattr(botfiles, "is_standin", lambda: True)


@pytest.fixture
def bot_paths(tmp_path, monkeypatch):
    ms = botfiles.strategy()
    cfg = tmp_path / "maker_config.json"
    import json
    shutil.copy(ROOT / "kalshi_bot" / "maker_config.json", cfg)
    doc = json.loads(cfg.read_text(encoding="utf-8"))      # same generic series as the pinned DEFAULTS
    cfg.write_text(json.dumps(_pin_series_in(doc), indent=2), encoding="utf-8")
    monkeypatch.setattr(ms, "KILL", str(tmp_path / "KILL"))
    monkeypatch.setattr(ms, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(ms, "LOG", str(tmp_path / "journal.csv"))
    from control_api.services import arb as arbsvc
    sc = arbsvc.scanner()
    monkeypatch.setattr(sc, "KILL_PATH", tmp_path / "ARB_KILL")
    monkeypatch.setattr(sc, "LOG_PATH", tmp_path / "arb_opportunities.csv")
    return tmp_path


@pytest.fixture
def settings():
    return Settings(store="memory", allowed_origins=[ORIGIN], tick_token=TICK, runner_enabled=True,
                    totp_enc_key=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
                    public_url="https://api.example.com", cronjob_api_key="cron-key")


@pytest.fixture
def kalshi():
    return FakeKalshi()


@pytest.fixture
def cron():
    return FakeCron()


@pytest_asyncio.fixture
async def env(settings, bot_paths, kalshi, cron, monkeypatch):
    for k in ("KALSHI_LIVE_CONFIRM", "KALSHI_PROD_API_KEY_ID", "KALSHI_PROD_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    store = MemoryStore()
    secret = pyotp.random_base32()
    await store.save_user({"username": "owner", "password_hash": _PW_HASH,
                           "totp_secret_enc": sec.encrypt_totp_secret(settings, secret),
                           "last_totp_step": None, "failed_logins": 0})
    e = Env()
    e.public = FakePublic()
    e.settings, e.store, e.secret, e.kalshi, e.cron, e.paths = settings, store, secret, kalshi, cron, bot_paths
    e.app = create_app(settings, store=store, kalshi_factory=lambda mode: e.kalshi,
                       cronjob_factory=lambda key: e.cron,
                       public_factory=lambda mode: e.public)
    e.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url="http://test")
    e.runner = e.app.state.runner
    await e.runner.rehydrate()
    _pin_series_in(e.runner.config)   # ASGITransport does not run lifespan, so no background loops start
    # runner_enabled=True here: since S-10, False is a hard stop (tick/run-once/start refused). Tests never call
    # runner.start(), so the timer/keep-awake/arb loops never run.
    e.token = sec.create_token(settings, "owner", sec.TYP_ACCESS, 3600)[0]
    e.auth = {"Authorization": f"Bearer {e.token}"}

    async def h2fa():
        """Auth headers + a fresh TOTP code (step counter reset: a step can't otherwise be reused)."""
        await store.save_user({"username": "owner", "last_totp_step": None, "failed_logins": 0,
                               "locked_until": None})
        return {**e.auth, "X-2FA-Code": pyotp.TOTP(secret).now()}
    e.h2fa = h2fa
    yield e
    await e.client.aclose()


class BrokenStore:
    """Every method raises, to prove Stop does not depend on the store."""
    def __getattr__(self, name):
        async def boom(*a, **k):
            raise RuntimeError("atlas down")
        return boom


async def ready_preflight(target, **kw):
    """A go-live preflight that is ready (S3 L-1 recheck at live mode / live Start / arbitrage live)."""
    return {"ready": True, "target": target, "ts": "test",
            "checks": [{"id": "keys", "label": "Keys", "status": "pass", "detail": ""}]}


def preflight_ready(monkeypatch):
    from control_api.services import preflight as pf
    monkeypatch.setattr(pf, "run_preflight", ready_preflight)

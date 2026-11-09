"""check_monitoring.py with a fake fetcher (no network)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_monitoring as cm  # noqa: E402

UUID = "12345678-1234-1234-1234-123456789abc"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
ENV = {"PUBLIC_URL": "https://svc.onrender.com", "HEALTHCHECKS_PING_URL": f"https://hc-ping.com/{UUID}",
       "NTFY_TOPIC": "supersecrettopicname"}


def fetcher(health=200, hc_status="up", last_ping="2026-11-09T11:55:00+00:00", api=200):
    calls = []

    def f(url, headers=None, timeout=0):
        calls.append(url)
        if url.endswith("/healthz"):
            return health, "ok"
        body = {"checks": [{"uuid": UUID, "ping_url": f"https://hc-ping.com/{UUID}", "status": hc_status,
                            "last_ping": last_ping}]}
        return api, json.dumps(body)
    f.calls = calls
    return f


def run(env=None, fet=None, argv=()):
    return cm.main(list(argv), dict(ENV if env is None else env), fet or fetcher(), NOW)


def test_all_ok(capsys):
    f = fetcher()
    assert run(fet=f) == 0
    assert not any("hc-ping" in c for c in f.calls)          # the dead-man URL is never pinged
    out = capsys.readouterr().out
    assert UUID not in out and "supersecrettopicname" not in out


def test_missing_or_bad_config_exits_2(capsys):
    assert run({}) == 2
    assert run({**ENV, "HEALTHCHECKS_PING_URL": "http://hc-ping.com/" + UUID}) == 2
    assert run({**ENV, "HEALTHCHECKS_PING_URL": "https://hc-ping.com/not-a-uuid"}) == 2
    assert run({k: v for k, v in ENV.items() if k != "PUBLIC_URL"}) == 2
    assert UUID not in capsys.readouterr().err


def test_api_down_exits_1():
    assert run(fet=fetcher(health=503)) == 1
    assert run(fet=fetcher(health=0)) == 1


def test_api_status_checked_only_with_key():
    assert run(fet=fetcher(hc_status="down")) == 0           # no key: status not read
    env = {**ENV, "HEALTHCHECKS_API_KEY": "readonly-key"}
    assert run(env, fetcher()) == 0
    assert run(env, fetcher(hc_status="down")) == 1
    assert run(env, fetcher(last_ping="2026-11-09T11:00:00+00:00")) == 1      # 60 min old
    assert run(env, fetcher(last_ping=None)) == 1
    assert run(env, fetcher(api=401)) == 1


def test_skip_health_and_key_not_printed(capsys):
    env = {k: v for k, v in ENV.items() if k != "PUBLIC_URL"}
    assert run({**env, "HEALTHCHECKS_API_KEY": "readonly-key"}, fetcher(), ["--skip-health"]) == 0
    assert "readonly-key" not in capsys.readouterr().out


def test_mask():
    assert cm.mask("") == "<unset>" and cm.mask("abc") == "***" and "secret" not in cm.mask("supersecrettopic")

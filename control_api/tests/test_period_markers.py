"""Release / behaviour-change markers on GET /api/periods (additive): order, cap, flags, no secrets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.services import markers as M
from control_api.services import periods as P

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _a(action, mins, *, ok=True, details=None):
    return {"ts": NOW - timedelta(minutes=mins), "user": "owner", "action": action, "ok": ok, "details": details,
            "ip": "203.0.113.9", "ua": "agent/1"}


def test_build_markers_kinds_order_flags_and_no_leaks():
    tl = P.timeline([])
    docs = [_a("bot.start", 50), _a("release.register", 40, details={"version": "1.2.3", "commit": "abcdef0123", "ci": "http://x"}),
            _a("config.update", 30, details={"update": {"run_budget": 99, "poll_minutes": 9}}),
            _a("config.update", 20, details={"update": {"poll_minutes": 9}}),
            _a("mode.set", 10, details={"mode": "demo"}), _a("bot.stop", 5), _a("login", 4),
            _a("bot.start", 3, ok=False)]
    out = M.build_markers(docs, tl)
    assert [m["kind"] for m in out] == ["stop", "mode", "config", "config", "release", "start"]
    rel = out[4]
    assert rel["detail"] == "1.2.3 (abcdef0)" and rel["changes_trading"] is False
    assert out[3]["changes_trading"] is True and out[2]["changes_trading"] is False
    assert out[1]["changes_trading"] is True
    flat = repr(out)
    assert "203.0.113.9" not in flat and "owner" not in flat and "http://x" not in flat and "99" not in flat
    assert all(m["period_id"] == "p1" for m in out)


def test_cap():
    docs = [_a("bot.stop", i) for i in range(250)]
    assert len(M.build_markers(docs, P.timeline([]))) == M.MAX_MARKERS


@pytest.mark.asyncio
async def test_route_includes_markers_and_keeps_old_fields(env):
    await env.store.add_audit(_a("release.approve", 2, details={"id": "r1", "version": "2.0.0", "commit": "1234567abc"}))
    body = (await env.client.get("/api/periods", headers=env.auth)).json()
    assert {"periods", "cash_flows", "markers"} <= set(body)
    assert any(m["kind"] == "release" and m["detail"] == "2.0.0 (1234567)" for m in body["markers"])
    ts = [m["ts"] for m in body["markers"]]
    assert ts == sorted(ts, reverse=True)

"""Contract check for GET /api/analytics/returns and /returns/fills: the REAL route (in-memory store, seeded fills,
settlements, snapshots and cash flows) must match the shared schema file the frontend fixture is also checked against
(control_app/src/test/contract/returns_response_schema.json). If this file changes, change both sides."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from control_api.tests.test_analytics import _put, fill, settle  # noqa: F401
from control_api.tests.test_analytics import env  # noqa: F401  (env fixture with synthetic cells)

UTC = timezone.utc
URL = "/api/analytics/returns"
SCHEMA_PATH = (Path(__file__).resolve().parents[2] / "control_app" / "src" / "test" / "contract"
               / "returns_response_schema.json")
TZ = "America/Chicago"

_PY = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is(value, name: str) -> bool:
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _PY[name])


def check(obj: dict, spec: dict, where: str) -> None:
    """Every key in `spec` is present with an allowed type, and no key is outside the spec."""
    assert isinstance(obj, dict), where
    missing = set(spec) - set(obj)
    extra = set(obj) - set(spec)
    assert not missing, f"{where}: missing keys {sorted(missing)}"
    assert not extra, f"{where}: keys not in the shared schema {sorted(extra)}"
    for k, types in spec.items():
        assert any(_is(obj[k], t) for t in types), f"{where}.{k}: {obj[k]!r} is not one of {types}"


@pytest.fixture(scope="module")
def schema():
    if not SCHEMA_PATH.exists():                                       # a copy without control_app/
        pytest.skip("shared schema file not present")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


async def _seed(env):
    """Bot maker + arb fills over ~10 days (one day with 6 settled fills, one with 1, one losing), a manual ('you')
    fill, daily snapshots for the last 10 days, one very old snapshot and a deposit."""
    now = datetime.now(UTC).replace(microsecond=0)
    fills, setts = [], []

    def add(fid, days_ago, strategy, *, result="no", source="bot", side="no", hour=15):
        t = f"KXDEMOB-26OCT-{fid}"
        settled = (now - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0)
        extra = {"source": source}
        if source == "bot":
            extra["strategy"] = strategy
        fills.append(fill(fid, ts=settled - timedelta(days=1), ticker=t, side=side, **extra))
        setts.append(settle(t, result, settled))

    for i in range(5):
        add(f"m{i}", 2, "maker")
    add("a1", 2, "arb")                                # day -2: 6 settled bot fills (not a small sample)
    add("m-single", 1, "maker")                        # a day with one fill (small sample)
    add("a2", 5, "arb")
    add("loss", 3, "maker", result="yes")              # a NO-side fill loses when the market resolves yes
    add("you1", 2, "maker", source="you")
    add("old", 9, "maker")
    await _put(env.store, fills, setts)
    await env.store.add_account_snapshot({"ts": now - timedelta(days=28), "cash": 500.0, "exposure": 0.0})
    for d in range(10, -1, -1):
        await env.store.add_account_snapshot({"ts": now - timedelta(days=d, hours=1), "cash": 500.0 + d,
                                              "exposure": 5.0})
    await env.store.add_cash_flow({"id": "cf1", "ts": now - timedelta(days=3), "kind": "deposit", "amount": 100.0,
                                   "created_by": "owner"})
    return now


async def _get(env, gran, days, **extra):
    r = await env.client.get(URL, params={"granularity": gran, "days": days, "tz": TZ, **extra}, headers=env.auth)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("gran,days", [("day", 30), ("week", 84), ("month", 180)])
async def test_real_response_matches_shared_schema(env, schema, gran, days):
    await _seed(env)
    j = await _get(env, gran, days)
    check(j, schema["top"], "response")
    check(j["totals"], schema["totals"], "totals")
    assert j["granularity"] == gran and j["days"] == days and j["tz_used"] == TZ
    assert j["periods"], "expected at least one whole period"
    for p in j["periods"]:
        check(p, schema["period"], f"period {p.get('start_local')}")
        check(p["flows_in_period"], schema["flows_in_period"], "flows_in_period")
        check(p["you"], schema["you"], "you")
        assert set(p["by_strategy"]) == set(p["fills_by_strategy"]) == set(schema["strategy_keys"]), p
        for v in list(p["by_strategy"].values()) + list(p["fills_by_strategy"].values()):
            assert isinstance(v, (int, float)) and not isinstance(v, bool)
        assert p["return_reason"] in schema["return_reasons"], p["return_reason"]


@pytest.mark.asyncio
async def test_null_return_rate_comes_with_a_reason_and_no_base(env, schema):
    await _seed(env)
    j = await _get(env, "day", 30)
    reasons = {p["return_reason"] for p in j["periods"]}
    assert "ok" in reasons and ({"no_start_snapshot", "stale_start_snapshot"} & reasons), reasons
    for p in j["periods"]:
        if p["return_reason"] == "ok":
            assert isinstance(p["return_rate"], float) and isinstance(p["base"], float), p
        else:
            assert p["return_rate"] is None and p["base"] is None, p
        assert isinstance(p["realized_pnl"], float)                    # dollars survive a null rate


@pytest.mark.asyncio
async def test_partial_flag_small_sample_and_strategy_split(env):
    await _seed(env)
    j = await _get(env, "day", 30)
    ps = j["periods"]
    assert [p["partial"] for p in ps].count(True) == 1 and ps[-1]["partial"] is True
    assert ps[-1]["elapsed_hours"] is not None and all(p["elapsed_hours"] is None for p in ps[:-1])
    big = max(ps, key=lambda p: p["settled_fills"])
    assert big["settled_fills"] >= 5 and big["small_sample"] is False and big["fills_by_strategy"]["arb"] == 1
    small = [p for p in ps if p["small_sample"]]
    assert small and all(0 < p["settled_fills"] < j["small_sample_threshold"] for p in small)
    assert all(p["empty"] is (p["settled_fills"] == 0) for p in ps) and any(p["empty"] for p in ps)
    assert any(p["you"]["settled_fills"] == 1 for p in ps)             # manual trade reported apart, never in bars
    assert sum(p["settled_fills"] for p in ps) == j["totals"]["settled_fills"]
    assert any("not included" in n for n in j["notes"]) and any("not additive" in n for n in j["notes"])
    assert all(isinstance(n, str) for n in j["notes"])


@pytest.mark.asyncio
async def test_negative_period_and_deposit_are_reported(env):
    await _seed(env)
    j = await _get(env, "day", 30)
    assert any(p["realized_pnl"] < 0 for p in j["periods"])
    dep = [p for p in j["periods"] if p["flows_in_period"]["count"]]
    assert dep and dep[0]["flows_in_period"]["deposits"] == 100.0 and dep[0]["flows_in_period"]["net"] == 100.0


@pytest.mark.asyncio
async def test_empty_store_returns_empty_periods_not_an_error(env, schema):
    j = await _get(env, "day", 7)
    check(j, schema["top"], "response")
    assert j["periods"]
    for p in j["periods"]:
        check(p, schema["period"], "period")
        assert p["empty"] and p["realized_pnl"] == 0.0 and p["settled_fills"] == 0


@pytest.mark.asyncio
async def test_fills_response_matches_shared_schema(env, schema):
    await _seed(env)
    j = await _get(env, "day", 30)
    p = next(x for x in j["periods"] if x["settled_fills"] >= 5)
    r = await env.client.get(URL + "/fills", params={"start": p["start_local"], "end": p["end_local"], "tz": TZ,
                                                     "granularity": "day", "days": 30}, headers=env.auth)
    assert r.status_code == 200, r.text
    f = r.json()
    check(f, schema["fills_top"], "fills")
    check(f["period"], schema["fills_period"], "fills.period")
    assert f["total"] == p["settled_fills"] and len(f["fills"]) == f["total"]
    for row in f["fills"]:
        check(row, schema["fill"], "fill")
    stamps = [x["settled_utc"] for x in f["fills"]]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_fills_route_requires_end_so_the_app_must_send_it(env):
    """The app's drill-down must send `end` (the period's end_local); `start` alone is a 422."""
    j = await _get(env, "day", 30)
    start = j["periods"][0]["start_local"]
    r = await env.client.get(URL + "/fills", params={"start": start, "tz": TZ}, headers=env.auth)
    assert r.status_code == 422

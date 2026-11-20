"""Hotfix (legacy shadow episodes): episodes stored before the 2A-3 caps (no `methodology`, read as 1) are kept out
of the headline and summarized apart under `legacy` with a banner; new episodes carry methodology 2.

Synthetic data only (KXDEMO* tickers)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from control_api.services import shadow as sh
from control_api.services import shadow_model as m
from control_api.tests.test_shadow_cycle import FakeMD, FakeStrategy, inp, yes_quote

UTC = timezone.utc
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_private_files(tmp_path, monkeypatch):
    monkeypatch.setenv(sh.FEE_SCHEDULE_ENV, str(tmp_path / "absent_fees.json"))
    monkeypatch.setenv(sh.SHADOW_LIMITS_ENV, str(tmp_path / "absent_limits.json"))
    # These tests use fixed October 2026 dates (the legacy/corrected boundary is a fixed date), while the summary only
    # reads the last SHADOW_SUMMARY_DAYS days of the real clock: without this the episodes age out and the counts drift.
    monkeypatch.setattr(sh, "SHADOW_SUMMARY_DAYS", 36500)


def _ep(i, *, day, methodology=None, result="no", price=0.9, fill=10.0, ticker=None):
    base = datetime(2026, 10, 18, 4, 0, tzinfo=UTC) + timedelta(days=day)
    e = {"ep_id": f"lg{i}", "env": "prod", "ticker": ticker or f"KXDEMOA-L{i}", "series": "KXDEMOA", "side": "no",
         "book_side": "bid", "price": price, "order_price": price, "count": 10.0, "cost": 10 * price, "cell": ["c"],
         "join_ts": base, "last_seen_ts": base, "end_ts": base + timedelta(hours=1), "end_reason": "cancel",
         "close_time": base + timedelta(hours=20), "settled_ts": base + timedelta(hours=20), "result": result,
         "trades_read": True, "fill": {"lo": fill, "mid": fill, "hi": fill}, "standin": False, "ts": base}
    if methodology is not None:
        e["methodology"] = methodology
    return e


def test_new_episodes_are_methodology_2_and_missing_reads_as_legacy():
    [ep] = sh.run_cycle(FakeMD(), FakeStrategy([yes_quote()]), inp()).upserts
    assert ep["methodology"] == 2
    assert sh.methodology_of({}) == 1 and sh.methodology_of({"methodology": 1}) == 1
    assert sh.methodology_of({"methodology": 2}) == 2
    for odd in (True, "2", 2.5, None, 3):
        assert sh.methodology_of({"methodology": odd}) == 1
    assert "methodology" in sh.SUMMARY_FIELDS


def test_no_stored_methodology_is_decided_by_the_2a3_deploy_time():
    since = sh.METHODOLOGY2_SINCE
    assert since == datetime(2026, 10, 22, 22, 33, 41, tzinfo=UTC)
    assert sh.methodology_of({"join_ts": since}) == 2                          # at the deploy time: capped code
    assert sh.methodology_of({"join_ts": since.isoformat()}) == 2
    assert sh.methodology_of({"join_ts": since - timedelta(seconds=1)}) == 1   # before it: legacy
    assert sh.methodology_of({"join_ts": since - timedelta(days=3), "methodology": 2}) == 2   # stored 2 wins
    # the store fills a missing field with 1 on read (and the code never writes 1): read as "not stored"
    assert sh.methodology_of({"join_ts": since + timedelta(hours=1), "methodology": 1}) == 2
    assert sh.methodology_of({"join_ts": since - timedelta(hours=1), "methodology": 1}) == 1
    after = [_ep(i, day=0) for i in range(3)]
    for e in after:
        e.update(join_ts=since + timedelta(minutes=5))                         # no field, after the deploy
    s = sh.build_summary([_ep(9, day=0)] + after, [], NOW)
    assert s["status"]["settled_episodes"] == 3 and s["legacy"]["episodes"] == 1


def test_headline_excludes_legacy_and_the_legacy_block_has_the_old_figures():
    legacy = [_ep(i, day=i % 3) for i in range(12)]                       # stacked, all wins
    corrected = [_ep(100 + i, day=i % 2, methodology=2, result="yes" if i == 0 else "no") for i in range(4)]
    s = sh.build_summary(legacy + corrected, [], NOW)
    assert s == {**s, "methodology": 2, "legacy_banner": "legacy, uncapped, not comparable"}
    want = m.summarize(corrected, NOW)
    assert s["headline"] == want["headline"] and s["status"]["settled_episodes"] == 4
    assert s["status"]["days_covered"] == 2 and s["by_series"] == want["by_series"]
    lg = s["legacy"]
    old = m.summarize(legacy, NOW, draws=0)
    assert lg["banner"] == m.LEGACY_BANNER and lg["episodes"] == 12
    assert lg["headline"]["pnl"] == {k: old["headline"]["pnl"][k] for k in ("lo", "mid", "hi")}
    assert lg["headline"]["peak_held_capital"] == old["headline"]["peak_held_capital"]
    assert "ci90" not in lg["headline"]["pnl"] and lg["status"]["settled_episodes"] == 12
    assert lg["from"] == legacy[0]["join_ts"].isoformat()


def test_only_legacy_episodes_report_no_corrected_data_with_null_numbers():
    s = sh.build_summary([_ep(i, day=i) for i in range(5)], [], NOW)
    for name in ("headline", "headline_zero_fee"):
        h = s[name]
        assert h["headline_status"] == "no_corrected_data" and h["ci_reliable"] is False
        assert h["pnl"] == {"lo": None, "mid": None, "hi": None, "ci90": None}
        assert h["roi"]["mid"] is None and h["fill_rate"]["mid"] is None and h["peak_capital"] is None
    assert s["status"]["headline_status"] == "no_corrected_data" and s["status"]["too_early"] is True
    assert s["status"]["settled_episodes"] == 0 and s["legacy"]["episodes"] == 5


def test_no_legacy_means_no_block_and_the_headline_is_unchanged():
    corrected = [_ep(i, day=i % 8, methodology=2, result="yes" if i % 6 == 0 else "no") for i in range(40)]
    s = sh.build_summary(corrected, [], NOW)
    assert s["legacy"] is None and s["legacy_banner"] is None
    assert s["headline"] == m.summarize(corrected, NOW)["headline"] and s["status"]["headline_status"] == "ok"
    assert sh.build_summary([], [], NOW)["status"]["headline_status"] == "too_early"     # nothing at all


def test_variants_use_corrected_episodes_only(monkeypatch):
    legacy = [_ep(i, day=0) for i in range(3)]
    corrected = [dict(_ep(10 + i, day=0, methodology=2), interval=5) for i in range(2)]
    s = sh.build_summary(legacy + corrected, [], NOW, intervals=[5])
    rows = {v["interval"]: v for v in s["variants"]}
    assert rows[10]["settled_episodes"] == 0 and rows[5]["settled_episodes"] == 2


@pytest.mark.asyncio
async def test_period_filter_applies_to_the_headline_and_the_legacy_block(env):
    # fixed dates, not "now minus N days": whether an episode without a stored methodology is legacy depends on
    # METHODOLOGY2_SINCE (2026-10-22), so a relative date would flip to corrected once N days have passed
    def at(e, j):
        return dict(e, join_ts=j, last_seen_ts=j, end_ts=j + timedelta(hours=1), close_time=j + timedelta(hours=10),
                    settled_ts=j + timedelta(hours=10), ts=j)
    eps = [at(_ep(1, day=0), datetime(2026, 10, 20, 12, 0, tzinfo=UTC)),
           at(_ep(2, day=0), datetime(2026, 10, 21, 12, 0, tzinfo=UTC)),
           at(_ep(3, day=0, methodology=2), datetime(2026, 10, 25, 12, 0, tzinfo=UTC))]
    await env.store.shadow_upsert_episodes(eps)
    await env.store.add_period({"id": "per2", "name": "Period 2", "started_at": datetime(2026, 10, 23, 0, 0, tzinfo=UTC)})
    p1 = (await env.client.get("/api/shadow/summary?period=p1", headers=env.auth)).json()
    p2 = (await env.client.get("/api/shadow/summary?period=per2", headers=env.auth)).json()
    assert p1["headline"]["headline_status"] == "no_corrected_data" and p1["legacy"]["episodes"] == 2
    assert p1["legacy_banner"] == "legacy, uncapped, not comparable"
    assert p2["legacy"] is None and p2["status"]["settled_episodes"] == 1
    assert p2["headline"]["headline_status"] == "too_early"

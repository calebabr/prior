"""pull_data --shadow: a read-only export of Shadow episodes and pass docs with scrubbed fields only."""
from __future__ import annotations

import asyncio
import csv
import re
from datetime import datetime, timedelta, timezone

from control_api.db import MemoryStore
from control_api.tools import pull_data

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


class ReadOnlyStore:
    """Allows only the read methods the pull may call (never a shadow write); records the calls."""
    ALLOWED = re.compile(r"^(get_\w+|last_\w+|list_\w+|shadow_recent|shadow_recent_passes|close)$")

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def __getattr__(self, name):
        if not self.ALLOWED.match(name):
            raise AssertionError(f"pull_data must not call store.{name}")
        self.calls.append(name)
        return getattr(self.inner, name)


async def _store():
    st = MemoryStore()
    close = NOW - timedelta(days=4)
    await st.shadow_upsert_episodes([
        {"ep_id": "ep1", "ticker": "KXDEMOA-26OCT22-B1", "series": "KXDEMOA", "side": "yes", "price": 0.4,
         "count": 10.0, "cost": 4.0, "methodology": 1, "capped": False, "cap_reason": None,
         "join_ts": NOW - timedelta(days=5), "end_ts": NOW - timedelta(days=5, hours=-1), "end_reason": "cancel",
         "close_time": close, "trades_read": True, "fill": {"lo": 0.0, "mid": 5.0, "hi": 10.0}, "result": None,
         "settle_checked_ts": NOW, "api_key": "k-should-not-appear", "tick_token": "tick-should-not-appear",
         "q0": 120.0, "level_min": 100.0, "interval": 7, "fee_mode": "std", "through": True,
         "through_vol": 3.5, "traded_vol": 9.0, "truncated": False, "n_obs": 4, "pnl": 0.6, "cell": "secret-cell"},
        {"ep_id": "ep2", "ticker": "KXDEMOA-26OCT27-B1", "series": "KXDEMOA", "side": "no", "price": 0.9,
         "count": 5.0, "cost": 4.5, "methodology": 2, "capped": True, "cap_reason": "open_cap",
         "join_ts": NOW - timedelta(hours=1), "end_ts": None, "close_time": NOW + timedelta(hours=5),
         "trades_read": True, "fill": {"lo": 0.0, "mid": 0.0, "hi": 0.0}, "result": "yes",
         "settled_ts": NOW}])
    await st.add_shadow_pass({"ts": NOW, "ok": True, "held_cost": 248.51, "ceiling": 75.0, "headroom": 0.0,
                              "capped_new": 3, "held_stale_ignored": 0, "errors": ["ConnectionError"]})
    return st


def _rows(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_shadow_export_is_read_only_scrubbed_and_documented(tmp_path):
    inner = asyncio.run(_store())
    store = ReadOnlyStore(inner)
    out = tmp_path / "ld"
    data = asyncio.run(pull_data.collect(store, 30, shadow=True))
    counts = pull_data.write_all(data, out, 30, "test")
    assert counts["shadow_episodes.csv"] == 2 and counts["shadow_passes.csv"] == 1
    assert {"shadow_recent", "shadow_recent_passes"} <= set(store.calls)

    eps = _rows(out / "shadow_episodes.csv")
    assert list(eps[0].keys()) == pull_data.SHADOW_EPISODE_KEYS            # scrubbed, fixed fields only
    by = {r["ep_id"]: r for r in eps}
    assert by["ep1"]["methodology"] == "1" and by["ep1"]["result"] == "" and by["ep1"]["capped"] == "False"
    assert by["ep1"]["settle_checked_ts"].startswith("2026-10-27T15:00") and "+00:00" in by["ep1"]["close_time"]
    assert by["ep2"]["cap_reason"] == "open_cap" and by["ep2"]["result"] == "yes"
    assert by["ep1"]["interval"] == "7" and by["ep1"]["through"] == "True" and by["ep1"]["through_vol"] == "3.5"
    assert by["ep1"]["n_obs"] == "4" and by["ep1"]["pnl"] == "0.6" and by["ep2"]["interval"] in ("", "10")
    doc = (out / "DATA_DICTIONARY.md").read_text(encoding="utf-8") if (out / "DATA_DICTIONARY.md").exists() else ""
    assert not doc or "through_vol" in doc
    assert "secret-cell" not in (out / "shadow_episodes.csv").read_text(encoding="utf-8")
    text = (out / "shadow_episodes.csv").read_text(encoding="utf-8")
    assert "k-should-not-appear" not in text and "tick-should-not-appear" not in text and "q0" not in text

    [p] = _rows(out / "shadow_passes.csv")
    assert p["held_cost"] == "248.51" and p["headroom"] == "0.0"

    dd = (out / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
    sections = dict(re.findall(r"^## (\S+)\n(.*?)(?=^## |\Z)", dd, flags=re.M | re.S))
    for name in ("shadow_episodes.csv", "shadow_passes.csv"):
        assert name in sections
        for col in eps[0].keys() if name == "shadow_episodes.csv" else p.keys():
            assert f"| `{col}` |" in sections[name], (name, col)
            line = next(x for x in sections[name].splitlines() if x.startswith(f"| `{col}` |"))
            assert "Extra field stored" not in line, (name, col)


def test_shadow_export_is_off_by_default(tmp_path):
    inner = asyncio.run(_store())
    store = ReadOnlyStore(inner)
    data = asyncio.run(pull_data.collect(store, 30))
    out = tmp_path / "ld"
    counts = pull_data.write_all(data, out, 30, "test")
    assert "shadow_episodes.csv" not in counts and not (out / "shadow_episodes.csv").exists()
    assert not {"shadow_recent", "shadow_recent_passes"} & set(store.calls)


def test_shadow_flag_via_main_with_demo_store(tmp_path):
    out = tmp_path / "ld"
    assert pull_data.main(["--seed-demo", "--shadow", "--out", str(out), "--days", "30"]) == 0
    assert (out / "shadow_episodes.csv").is_file() and (out / "shadow_passes.csv").is_file()

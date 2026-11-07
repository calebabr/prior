"""pull_data exports decision-recording rows (journal event "decision_pass") to decisions.csv and
history/decisions.csv, flattening the JSON note; the dictionary documents every column. Fake tickers only."""
import asyncio
import csv
import json
import re
from datetime import datetime, timedelta, timezone

from control_api.db import MemoryStore
from control_api.tools import pull_data

REASONS = ["quoted", "kept", "skipped:not_in_cell", "skipped:series_cap", "skipped:market_cap", "skipped:ceiling",
           "skipped:run_budget", "skipped:other"]


def _note(i, quoted=2, kept=1):
    reasons = {r: 0 for r in REASONS}
    reasons.update({"quoted": quoted, "kept": kept, "skipped:not_in_cell": 3})
    return {"v": 1, "t0": "2026-11-07T00:00:00+00:00", "live": True, "n_markets": 4, "n_cand": quoted + kept + 3,
            "reasons": reasons, "other": {"unread_markets": 1}, "cfg": {"hash": f"abc{i:09d}", "run_budget": 20},
            "caps": {"ceiling": 50.0, "series_cap": 17.5, "market_cap": 3.0}, "state": {"cash": 90.0, "equity": 100.0},
            "orders": {"kept": kept, "cancelled": 0, "placed": quoted}, "write_errors": {"cancel": 0},
            "series_failed": [], "rec_errors": 0, "ttc": {"24-48": [6, 3]}, "by_series": {"DEMO": [4, 6, 2, 1]},
            "n_items": 3, "dropped": 0, "items": [["DEMO-26NOV07-T60", "no", 0.5, 2.0, 0, "q"]]}


def _row(ts, event, note):
    return {"utc": ts.isoformat(), "event": event, "ticker": "", "side": "", "price": "", "count": "", "cost": "",
            "cell": "", "note": note}


def _store(now, n=3, bad=False):
    s = MemoryStore()
    rows = [_row(now - timedelta(minutes=10 * i), "decision_pass", json.dumps(_note(i), separators=(",", ":")))
            for i in range(n)]
    rows.append(_row(now - timedelta(minutes=5), "quote", "live order_id=x"))
    if bad:
        rows.append(_row(now - timedelta(minutes=1), "decision_pass", "not json"))
    asyncio.run(s.add_journal_rows(rows))
    return s


def _pull(store, out, days=90):
    data = asyncio.run(pull_data.collect(store, days))
    return pull_data.write_all(data, out, days, "test")


def _rows(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_decision_rows_flatten_the_note():
    now = datetime.now(timezone.utc)
    journal = [{**_row(now, "decision_pass", json.dumps(_note(0))), "row_hash": "h0", "ts": now},
               {**_row(now, "quote", "x"), "row_hash": "h1", "ts": now},
               {**_row(now, "decision_pass", "[1, 2]"), "row_hash": "h2", "ts": now}]
    rows = pull_data.decision_rows(journal)
    assert [r["row_hash"] for r in rows] == ["h0", "h2"]
    r = rows[0]
    assert r["reasons.quoted"] == 2 and r["reasons.skipped:not_in_cell"] == 3
    assert sum(r[f"reasons.{k}"] for k in REASONS) == r["n_cand"]
    assert r["caps.market_cap"] == 3.0 and r["state.equity"] == 100.0 and r["other.unread_markets"] == 1
    assert r["cfg.hash"] == "abc000000000" and r["live"] is True
    assert json.loads(r["items"])[0][0] == "DEMO-26NOV07-T60" and json.loads(r["by_series"]) == {"DEMO": [4, 6, 2, 1]}
    assert rows[1] == {"ts": now, "row_hash": "h2", "parse_error": True}


def test_pull_writes_decisions_and_history_and_dedupes(tmp_path):
    now = datetime.now(timezone.utc)
    out = tmp_path / "ld"
    counts = _pull(_store(now, bad=True), out)
    assert counts["decisions.csv"] == 4
    top = _rows(out / "decisions.csv")
    assert [r.get("parse_error") or "" for r in top] == ["", "", "True", ""]
    assert top[0]["reasons.kept"] == "1" and top[0]["ts"] < top[-1]["ts"]
    _pull(_store(now, bad=True), out)                                 # same rows again: no duplicates
    hist = _rows(out / "history" / "decisions.csv")
    assert len(hist) == 4 and len({r["row_hash"] for r in hist}) == 4
    _pull(MemoryStore(), out)                                         # aged out of the store: history keeps them
    assert len(_rows(out / "history" / "decisions.csv")) == 4
    assert (out / "decisions.csv").read_text(encoding="utf-8").strip() == "empty"


def test_no_decision_rows_means_no_history_file(tmp_path):
    out = tmp_path / "ld"
    counts = _pull(MemoryStore(), out)
    assert counts["decisions.csv"] == 0
    assert not (out / "history" / "decisions.csv").exists()


def test_dictionary_documents_every_decision_column(tmp_path):
    out = tmp_path / "ld"
    _pull(_store(datetime.now(timezone.utc), bad=True), out)
    dd = (out / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
    sections = dict(re.findall(r"^## (\S+)\n(.*?)(?=^## |\Z)", dd, flags=re.M | re.S))
    for name in ("decisions.csv", "history/decisions.csv"):
        header = next(csv.reader((out / name).open(encoding="utf-8")))
        for col in header:
            assert f"| `{col}` |" in sections[name], (name, col)
            assert "Extra field stored" not in sections[name].split(f"| `{col}` |")[1].split("\n")[0], (name, col)
    assert "decision_pass" in sections["journal.csv"]

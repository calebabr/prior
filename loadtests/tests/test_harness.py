"""The load-test harness itself: pure helpers (fast) and a tiny real run against a local server (slow).

    python -m pytest -q loadtests/tests                 # everything
    python -m pytest -q -m "not slow" loadtests/tests   # helpers only
"""
from __future__ import annotations

import json

import pytest

from loadtests import report, run
from loadtests.lib import MemSampler, Rec, Recorder, check_remote_target, pct


def test_percentile_is_nearest_rank():
    v = [float(i) for i in range(1, 101)]
    assert pct(v, 50) == 50 and pct(v, 99) == 99 and pct(v, 100) == 100
    assert pct([], 99) is None and pct([3.0], 99) == 3.0


def test_recorder_summary_counts_statuses_and_percentiles():
    r = Recorder()
    for s, t in [(200, .1), (200, .2), (429, .05), (503, .3), (0, 1.0)]:
        r.add(Rec("x", s, t, "analytics_busy" if s == 429 else ""))
    s = r.summary("x")
    assert (s["n"], s["n_429"], s["n_5xx"], s["n_errors"]) == (5, 1, 1, 1)
    assert s["detail"] == {"analytics_busy": 1} and s["max_ms"] == 1000.0


@pytest.mark.parametrize("url,confirm,why", [
    ("https://prior-live.onrender.com", True, "live"),
    ("https://kalshi-prod.example.com", True, "live"),
    ("http://some-demo.example.com", True, "https"),
    ("https://demo.example.com", False, "confirm-demo"),
    ("ftp://demo.example.com", True, "http"),
])
def test_remote_target_refusals(url, confirm, why):
    with pytest.raises(SystemExit) as e:
        check_remote_target(url, confirm)
    assert why in str(e.value)


def test_remote_target_accepts_a_demo_host():
    assert check_remote_target("https://prior-demo.onrender.com/api/x", True) == "https://prior-demo.onrender.com"


def test_scenario_selection_by_name_letter_and_remote_subset():
    assert run.pick("a,g", False) == ["polling", "contention"]
    assert run.pick("all", True) == ["polling", "analytics", "auth", "stop"]      # no fake pass on a remote deploy
    assert run.pick("all", False)[-1] == "soak"
    with pytest.raises(SystemExit):
        run.pick("nope", False)


def test_report_writer_refuses_a_secret_and_writes_clean_files(tmp_path):
    res = {"label": "local MemoryStore", "target": "local", "started_utc": "2026-01-02T03:04:05+00:00",
           "duration_s": 1, "environment": {"python": "3", "platform": "x", "git": "abc"},
           "params": {"rows": 1, "pass_seconds": 1}, "bars": [], "pass": True,
           "scenarios": [{"name": "s", "status": "pass", "checks": [{"name": "c", "ok": True, "detail": ""}],
                          "metrics": {}, "notes": []}]}
    j, m = report.write(res, tmp_path, ["super-secret-token-value"])
    assert json.loads(j.read_text())["pass"] is True and "PASS" in m.read_text()
    res["scenarios"][0]["notes"] = ["oops super-secret-token-value"]
    with pytest.raises(RuntimeError):
        report.write(res, tmp_path, ["super-secret-token-value"])


def test_memory_growth_compares_early_and_late_windows():
    s = MemSampler(None)
    s.samples = [(i, 100.0 + (0 if i < 20 else 10)) for i in range(40)]
    g = s.growth()
    assert g["growth_mb"] == 10.0 and s.peak() == 110.0


@pytest.mark.slow
def test_smoke_tiny_local_run_passes_and_writes_clean_results(tmp_path):
    """Starts the real local API (300 rows, 1 s pass) on a free port and runs the quick Stop, polling and auth bursts."""
    rc = run.main(["--scenario", "stop,polling,auth", "--target", "local", "--quick", "--rows", "100",
                   "--out-dir", str(tmp_path)])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert rc == 0, files
    assert len(files) == 2 and files[0].endswith("local-memorystore.json")
    res = json.loads((tmp_path / files[0]).read_text())
    assert res["pass"] and {s["name"] for s in res["scenarios"]} == {"polling", "auth_burst", "stop_storm"}
    assert "eyJ" not in (tmp_path / files[0]).read_text()                       # no JWT in the results

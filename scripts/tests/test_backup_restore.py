"""backup_export.py / restore_drill.py against the in-memory adapter (no network, no Atlas)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_export as be  # noqa: E402
import backup_lib as bl  # noqa: E402
import restore_drill as rd  # noqa: E402

T = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
URI = "mongodb+srv://user:hunter2secret@cluster0.example.mongodb.net/?retryWrites=true"


def live_db() -> bl.MemoryDb:
    return bl.MemoryDb("kalshi_control", {
        "fills": [{"fill_id": f"f{i}", "ts": T, "price": 0.9, "blob": b"\x00\x01"} for i in range(1200)],
        "heartbeats": [{"ts": T, "ok": True}],
        "users": [{"username": "owner", "password_hash": "argon2-hash"}],
        "runner_lease": [{"holder": "x"}],
        "state": [{"desired": "stopped", "nested": {"when": T, "xs": [1, {"d": T}]}}],
    })


def test_export_skips_sensitive_and_writes_manifest(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    m = rd.load_manifest(dest)                       # also verifies checksums
    assert set(m["collections"]) == {"fills", "heartbeats", "state"}
    assert set(m["skipped"]) == {"users", "runner_lease"}
    assert m["collections"]["fills"]["docs"] == 1200
    assert not (dest / "users.ndjson.gz").exists()


def test_export_include_sensitive(tmp_path):
    dest = be.export(live_db(), tmp_path, include_sensitive=True, now=T)
    assert (dest / "users.ndjson.gz").exists()


def test_export_is_deterministic(tmp_path):
    a = be.export(live_db(), tmp_path / "a", now=T)
    b = be.export(live_db(), tmp_path / "b", now=T)
    assert rd.load_manifest(a)["collections"] == rd.load_manifest(b)["collections"]


def test_roundtrip_preserves_datetimes_bytes_and_counts(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    scratch = bl.MemoryDb("kalshi_scratch_drill")
    rep = rd.restore(scratch, dest, live_db="kalshi_control", apply=True)
    assert rep["ok"] and not rep["dry_run"]
    assert scratch.count("fills") == 1200
    f0 = next(scratch.find_all("fills"))
    assert f0["ts"] == T and f0["blob"] == b"\x00\x01"
    st = next(scratch.find_all("state"))
    assert st["nested"]["xs"][1]["d"] == T


def test_dry_run_is_default_and_writes_nothing(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    scratch = bl.MemoryDb("kalshi_scratch_drill")
    rep = rd.restore(scratch, dest, live_db="kalshi_control")
    assert rep["dry_run"] and rep["ok"]
    assert scratch.data == {}
    assert rep["collections"]["fills"] == {"expected": 1200, "restored": 0}


@pytest.mark.parametrize("target", ["kalshi_control", "KALSHI_CONTROL", "prod", "", "kalshi_scratc"])
def test_refuses_live_or_unmarked_target(tmp_path, target):
    dest = be.export(live_db(), tmp_path, now=T)
    with pytest.raises(rd.Refused):
        rd.restore(bl.MemoryDb(target), dest, live_db="kalshi_control", apply=True)


def test_refuses_when_scratch_name_equals_live_name(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    with pytest.raises(rd.Refused):
        rd.restore(bl.MemoryDb("my_scratch"), dest, live_db="my_scratch", apply=True)


def test_refuses_nonempty_scratch_unless_replace(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    scratch = bl.MemoryDb("kalshi_scratch_drill", {"fills": [{"fill_id": "old"}]})
    with pytest.raises(rd.Refused):
        rd.restore(scratch, dest, live_db="kalshi_control", apply=True)
    assert scratch.count("fills") == 1
    rep = rd.restore(scratch, dest, live_db="kalshi_control", apply=True, replace=True)
    assert rep["ok"] and scratch.count("fills") == 1200


def test_refuses_tampered_backup(tmp_path):
    dest = be.export(live_db(), tmp_path, now=T)
    (dest / "fills.ndjson.gz").write_bytes(b"corrupt")
    with pytest.raises(rd.Refused, match="checksum"):
        rd.restore(bl.MemoryDb("kalshi_scratch_drill"), dest, live_db="kalshi_control", apply=True)


def test_main_dry_run_and_apply(tmp_path, capsys):
    dest = be.export(live_db(), tmp_path, now=T)
    env = {"MONGODB_DB": "kalshi_control", "MONGODB_URI": URI}
    scratch = bl.MemoryDb("kalshi_scratch_drill")
    assert rd.main(["--backup", str(dest), "--target-db", "kalshi_scratch_drill"], env, scratch) == 0
    assert scratch.data == {} and "DRY RUN" in capsys.readouterr().out
    assert rd.main(["--backup", str(dest), "--target-db", "kalshi_scratch_drill", "--apply"], env, scratch) == 0
    assert scratch.count("heartbeats") == 1


def test_main_refusals_exit_2_and_never_print_uri(tmp_path, capsys):
    dest = be.export(live_db(), tmp_path, now=T)
    env = {"MONGODB_DB": "kalshi_control", "MONGODB_URI": URI}
    for target in ("kalshi_control", "notmarked"):
        assert rd.main(["--backup", str(dest), "--target-db", target, "--apply"], env, bl.MemoryDb(target)) == 2
    assert rd.main(["--backup", str(tmp_path / "nope"), "--target-db", "x_scratch"], env) == 2
    out = capsys.readouterr()
    assert "hunter2secret" not in out.out + out.err and "REFUSED" in out.err


def test_main_missing_uri_exits_2(tmp_path, capsys):
    dest = be.export(live_db(), tmp_path, now=T)
    assert rd.main(["--backup", str(dest), "--target-db", "kalshi_scratch_drill"], {}) == 2
    assert "MONGODB_URI" in capsys.readouterr().err


def test_backup_main_exit_codes_and_no_uri(tmp_path, capsys):
    assert be.main(["--out", str(tmp_path)], {}) == 2
    assert be.main(["--out", str(tmp_path)], {"MONGODB_URI": URI}, db=live_db()) == 0
    out = capsys.readouterr()
    assert "fills" in out.out and "hunter2secret" not in out.out + out.err

    class Boom(bl.MemoryDb):
        def find_all(self, coll):
            raise RuntimeError(f"cannot reach {URI}")

    assert be.main(["--out", str(tmp_path / "x")], {"MONGODB_URI": URI}, db=Boom("kalshi_control", {"fills": [{}]})) == 1
    err = capsys.readouterr().err
    assert "hunter2secret" not in err and "redacted" in err


def test_scrub_removes_connection_strings():
    assert "hunter2" not in bl.scrub("fail: mongodb://u:hunter2@h/db and mongodb+srv://u:hunter2@h/")

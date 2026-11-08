"""release_ctl.py: polling rules, backend verification rules, URL safety, and the HTTP calls against a local fake API."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ctl as rc  # noqa: E402

SHA = "a" * 40


# ---- poll_approval: only "approved" ever deploys ------------------------------------------------------------------
def make_clock():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


@pytest.mark.parametrize("answers,expected", [
    (["pending", "pending", "approved"], "approved"),
    (["pending", "rejected"], "rejected"),
    (["pending", "expired"], "expired"),
    (["bogus", None, "APPROVED", "approve"], "timeout"),            # unknown answers never count as approval
])
def test_poll_outcomes(answers, expected):
    now, sleep = make_clock()
    it = iter(answers + [None] * 10_000)
    assert rc.poll_approval(lambda: next(it), timeout=100, interval=10, sleep=sleep, now=now) == expected


def test_poll_times_out_after_the_budget_and_survives_errors():
    now, sleep = make_clock()
    calls = []

    def boom():
        calls.append(now())
        raise OSError("network down")
    assert rc.poll_approval(boom, timeout=1800, interval=20, sleep=sleep, now=now) == "timeout"
    assert 85 <= len(calls) <= 92 and max(calls) <= 1800


# ---- verification rules -------------------------------------------------------------------------------------------
def good_status(**o):
    return {"version": "2026.10.16-2", "role": "leader", "healthy": True, "generation": 5, **o}


def test_backend_verified_when_new_version_new_leader_healthy_generation_advanced():
    assert rc.check_backend({"api_version": "2026.10.16-2"}, good_status(), version="2026.10.16-2", commit=SHA, prev_generation=4) == []


@pytest.mark.parametrize("health,status,prev,needle", [
    ({"api_version": "2026.10.16-1"}, good_status(), 4, "/healthz"),                 # old version still serving
    ({}, good_status(), 4, "/healthz"),
    (None, good_status(), 4, "/healthz"),
    ({"api_version": "2026.10.16-2"}, None, 4, "unreadable"),
    ({"api_version": "2026.10.16-2"}, good_status(role="standby"), 4, "not the leader"),
    ({"api_version": "2026.10.16-2"}, good_status(healthy=False), 4, "unhealthy"),
    ({"api_version": "2026.10.16-2"}, good_status(generation=4), 4, "generation"),
    ({"api_version": "2026.10.16-2"}, good_status(generation=True), 4, "generation"),
    ({"api_version": "2026.10.16-2"}, good_status(version="2026.10.16-1"), 4, "old version"),
])
def test_backend_not_verified(health, status, prev, needle):
    problems = rc.check_backend(health, status, version="2026.10.16-2", commit=SHA, prev_generation=prev)
    assert problems and any(needle in p for p in problems)


def test_version_may_also_be_reported_as_the_commit_prefix_and_generation_check_is_optional():
    h = {"api_version": SHA[:7]}
    s = good_status(version=SHA[:7], generation=1)
    assert rc.check_backend(h, s, version="2026.10.16-2", commit=SHA, prev_generation=None) == []
    assert rc.matches_release("v2026.10.16-2", "2026.10.16-2", SHA)
    assert not rc.matches_release("abc", "2026.10.16-2", SHA)          # too short to be a commit prefix
    assert not rc.matches_release("0.0.0-dev", "2026.10.16-2", SHA)


# ---- URL safety ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "ftp://x.example", "http://api.example.com", "https://u:p@api.example.com",
                                 "https://api.example.com?x=1", "https:///nohost", "javascript:alert(1)"])
def test_base_url_rejects_unsafe(raw):
    with pytest.raises(ValueError):
        rc.base_url(raw)


def test_base_url_accepts_https_and_localhost_http():
    assert rc.base_url("https://api.example.com/") == "https://api.example.com"
    assert rc.base_url("http://localhost:8000") == "http://localhost:8000"


# ---- HTTP against a local fake API --------------------------------------------------------------------------------
class Fake(BaseHTTPRequestHandler):
    seen: list = []
    approval = "pending"

    def log_message(self, *a):
        pass

    def _send(self, code, body=None, extra=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        Fake.seen.append(("GET", self.path, dict(self.headers)))
        if self.path == "/api/deploy/status":
            if self.headers.get("X-Deploy-Status-Key") != "k-status":
                return self._send(401, {"detail": "no"})
            return self._send(200, {"version": "1", "generation": 1})
        if self.path == "/api/release/pending/r1":
            if self.headers.get("X-Release-Token") != "k-rel":
                return self._send(401)
            return self._send(200, {"status": Fake.approval})
        if self.path == "/redir":
            return self._send(302, extra={"Location": "http://127.0.0.1:1/steal"})
        self._send(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Fake.seen.append(("POST", self.path, dict(self.headers), json.loads(self.rfile.read(n) or b"{}")))
        if self.path == "/api/release/pending" and self.headers.get("X-Release-Token") == "k-rel":
            return self._send(201, {"id": "r1", "status": "pending"})
        self._send(401)


@pytest.fixture
def api(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Fake.seen, Fake.approval = [], "pending"
    monkeypatch.setenv("API_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("DEPLOY_STATUS_KEY", "k-status")
    monkeypatch.setenv("RELEASE_TOKEN", "k-rel")
    yield srv
    srv.shutdown()


def test_status_writes_the_file_and_fails_with_3_on_a_wrong_key(api, tmp_path, monkeypatch, capsys):
    out = tmp_path / "s.json"
    assert rc.main(["status", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"version": "1", "generation": 1}
    monkeypatch.setenv("DEPLOY_STATUS_KEY", "wrong")
    assert rc.main(["status", "--out", str(out)]) == 3
    assert not out.exists()                                  # a stale file never survives a failed read
    assert "wrong" not in capsys.readouterr().err


def test_status_unreachable_is_exit_3(tmp_path, monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("DEPLOY_STATUS_KEY", "k")
    assert rc.main(["status", "--out", str(tmp_path / "s.json")]) == 3


def test_register_then_poll_until_approved(api, tmp_path, capsys):
    notes = tmp_path / "n.md"
    notes.write_text("- fix(api): thing\n" * 500, encoding="utf-8")
    assert rc.main(["register", "--version", "2026.10.16-1", "--commit", SHA, "--summary-file", str(notes), "--ci", "green"]) == 0
    assert capsys.readouterr().out.strip() == "r1"
    posted = [s for s in Fake.seen if s[0] == "POST"][0]
    assert posted[3]["version"] == "2026.10.16-1" and posted[3]["commit"] == SHA and len(posted[3]["summary"]) <= rc.MAX_SUMMARY
    assert rc.main(["poll", "--id", "r1", "--timeout", "0.3", "--interval", "0.05"]) == 1       # still pending -> no deploy
    Fake.approval = "approved"
    assert rc.main(["poll", "--id", "r1", "--timeout", "2", "--interval", "0.05"]) == 0
    Fake.approval = "rejected"
    assert rc.main(["poll", "--id", "r1", "--timeout", "2", "--interval", "0.05"]) == 1


def test_register_sends_trading_changes_flag(api, tmp_path):
    notes = tmp_path / "n.md"
    notes.write_text("- fix(api): thing", encoding="utf-8")
    base = ["register", "--version", "2026.11.8-1", "--commit", SHA, "--summary-file", str(notes)]
    assert rc.main(base) == 0
    assert [s for s in Fake.seen if s[0] == "POST"][-1][3]["trading_changes"] is False
    assert rc.main(base + ["--trading-changes"]) == 0
    assert [s for s in Fake.seen if s[0] == "POST"][-1][3]["trading_changes"] is True


def test_register_rejects_bad_input_and_bad_token(api, tmp_path, monkeypatch):
    notes = tmp_path / "n.md"
    notes.write_text("x", encoding="utf-8")
    assert rc.main(["register", "--version", "bad version!", "--commit", SHA, "--summary-file", str(notes)]) == 1
    assert rc.main(["register", "--version", "1", "--commit", "short", "--summary-file", str(notes)]) == 1
    monkeypatch.setenv("RELEASE_TOKEN", "wrong")
    assert rc.main(["register", "--version", "1", "--commit", SHA, "--summary-file", str(notes)]) == 1


def test_poll_refuses_ids_that_could_alter_the_path(api):
    assert rc.main(["poll", "--id", "../../admin", "--timeout", "1"]) == 1
    assert not [s for s in Fake.seen if "admin" in s[1]]


def test_redirects_are_not_followed_so_keys_cannot_be_forwarded(api):
    status, body = rc.request("GET", rc.base_url() + "/redir", headers={"X-Release-Token": "secret"})
    assert status == 302 and body is None


def test_missing_keys_fail_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com")
    monkeypatch.delenv("DEPLOY_STATUS_KEY", raising=False)
    assert rc.main(["status", "--out", str(tmp_path / "s.json")]) == 3

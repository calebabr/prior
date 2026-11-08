#!/usr/bin/env python3
"""HTTP helper for .github/workflows/release.yml: talks to the control API with release-scoped keys.

Why a script and not curl in the workflow: the keys never appear on a command line, redirects are never followed
(a redirect would forward the key header to another host), response bodies are never echoed (they could carry
anything), and the polling / verification logic is unit-tested.

    release_ctl.py status          --out status.json            GET  /api/deploy/status   (env DEPLOY_STATUS_KEY)
    release_ctl.py register        --version V --commit SHA --summary-file notes.md --ci TEXT [--trading-changes]   -> prints the id
                                                                POST /api/release/pending (env RELEASE_TOKEN)
    release_ctl.py poll            --id ID [--timeout 1800]     GET  /api/release/pending/{id}: exit 0 only on "approved"
    release_ctl.py verify-backend  --version V --commit SHA [--prev-generation N] [--timeout 600]
    release_ctl.py verify-frontend --url URL [--timeout 300]

Env: API_BASE_URL (https; http only for localhost), DEPLOY_STATUS_KEY, RELEASE_TOKEN. Exit 0 ok, 1 failed/denied,
3 unreachable (status). Anything that is not clearly "approved" / "healthy" is a failure (fail closed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_BODY = 256 * 1024
MAX_SUMMARY = 3000
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # a 3xx becomes an HTTPError instead of being followed
        return None


_opener = urllib.request.build_opener(NoRedirect)


def base_url(raw: str | None = None) -> str:
    raw = (raw if raw is not None else os.environ.get("API_BASE_URL", "")).strip().rstrip("/")
    p = urllib.parse.urlsplit(raw)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("API_BASE_URL must be a plain http(s) origin")
    if p.scheme == "http" and p.hostname not in LOCAL_HOSTS:
        raise ValueError("API_BASE_URL must be https (http is allowed for localhost only)")
    return f"{p.scheme}://{p.netloc}{p.path}"


def request(method: str, url: str, *, headers: dict | None = None, body: dict | None = None, timeout: float = 20.0):
    """-> (status, parsed_json_or_None). Network errors raise OSError. Never logs headers or bodies."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "User-Agent": "prior-release-workflow",
        **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, b""
    if len(raw) > MAX_BODY:
        return status, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _key(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise ValueError(f"{name} is not set")
    return val


# ---- commands --------------------------------------------------------------------------------------------------------
def cmd_status(a) -> int:
    out = Path(a.out)
    out.unlink(missing_ok=True)
    try:
        status, body = request("GET", base_url() + "/api/deploy/status", headers={"X-Deploy-Status-Key": _key("DEPLOY_STATUS_KEY")})
    except (OSError, ValueError) as exc:
        print(f"deploy status unreachable: {type(exc).__name__}", file=sys.stderr)
        return 3
    if status != 200 or not isinstance(body, dict):
        print(f"deploy status unavailable (HTTP {status})", file=sys.stderr)
        return 3
    out.write_text(json.dumps(body), encoding="utf-8")
    print("deploy status read")
    return 0


def cmd_register(a) -> int:
    if not VERSION_RE.match(a.version) or not SHA_RE.match(a.commit):
        print("register: bad version or commit", file=sys.stderr)
        return 1
    summary = Path(a.summary_file).read_text(encoding="utf-8", errors="replace")[:MAX_SUMMARY]
    try:
        status, body = request("POST", base_url() + "/api/release/pending", headers={"X-Release-Token": _key("RELEASE_TOKEN")},
                               body={"version": a.version, "commit": a.commit, "summary": summary, "ci": a.ci[:200],
                                     "trading_changes": bool(a.trading_changes)})
    except (OSError, ValueError) as exc:
        print(f"register failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    rid = body.get("id") if isinstance(body, dict) else None
    if status not in (200, 201) or not isinstance(rid, str) or not ID_RE.match(rid):
        print(f"register refused (HTTP {status})", file=sys.stderr)
        return 1
    print(rid)
    return 0


def poll_approval(fetch, *, timeout: float, interval: float, sleep=time.sleep, now=time.monotonic) -> str:
    """fetch() -> status string or None (error). Returns approved | rejected | expired | timeout. Only 'approved' deploys."""
    deadline = now() + timeout
    while True:
        try:
            st = fetch()
        except (OSError, ValueError):
            st = None
        if st in ("approved", "rejected", "expired"):
            return st
        if now() >= deadline:
            return "timeout"
        sleep(interval)


def cmd_poll(a) -> int:
    if not ID_RE.match(a.id):
        print("poll: bad id", file=sys.stderr)
        return 1
    try:
        url, hdr = base_url() + f"/api/release/pending/{a.id}", {"X-Release-Token": _key("RELEASE_TOKEN")}
    except ValueError as exc:
        print(f"poll: {exc}", file=sys.stderr)
        return 1

    def fetch():
        status, body = request("GET", url, headers=hdr)
        return body.get("status") if status == 200 and isinstance(body, dict) else None
    result = poll_approval(fetch, timeout=a.timeout, interval=a.interval)
    print(f"release approval: {result}")
    return 0 if result == "approved" else 1


def matches_release(api_version: object, version: str, commit: str) -> bool:
    """The running build is this release: APP_VERSION equals the release version, or equals/prefixes the commit sha."""
    if not isinstance(api_version, str) or not api_version:
        return False
    if api_version == version or api_version.lstrip("v") == version.lstrip("v"):
        return True
    return len(api_version) >= 7 and commit.startswith(api_version.lower())


def check_backend(health: object, status: object, *, version: str, commit: str, prev_generation: int | None) -> list[str]:
    """-> problems (empty = verified): new version live, this instance is the leader, healthy, generation moved on."""
    problems = []
    if not isinstance(health, dict) or not matches_release(health.get("api_version"), version, commit):
        problems.append("/healthz does not report the new version yet")
        return problems
    if not isinstance(status, dict):
        return ["deploy status unreadable"]
    if not matches_release(status.get("version"), version, commit):
        problems.append("deploy status still reports the old version")
    if status.get("role") != "leader":
        problems.append(f"new instance is not the leader (role={status.get('role')!r})")
    if status.get("healthy") is not True:
        problems.append("new instance reports unhealthy")
    gen = status.get("generation")
    if prev_generation is not None and not (isinstance(gen, int) and not isinstance(gen, bool) and gen > prev_generation):
        problems.append("lease generation did not advance (no new leader yet)")
    return problems


def cmd_verify_backend(a) -> int:
    try:
        base, key = base_url(), _key("DEPLOY_STATUS_KEY")
    except ValueError as exc:
        print(f"verify-backend: {exc}", file=sys.stderr)
        return 1
    deadline, problems = time.monotonic() + a.timeout, ["not checked yet"]
    while True:
        try:
            _, health = request("GET", base + "/healthz")
            s, status = request("GET", base + "/api/deploy/status", headers={"X-Deploy-Status-Key": key})
            problems = check_backend(health, status if s == 200 else None, version=a.version, commit=a.commit,
                                     prev_generation=a.prev_generation)
        except (OSError, ValueError) as exc:
            problems = [f"unreachable ({type(exc).__name__})"]
        if not problems:
            print("backend verified: new version live, new leader holds the lease, healthy")
            return 0
        if time.monotonic() >= deadline:
            print("backend verification FAILED: " + "; ".join(problems), file=sys.stderr)
            return 1
        time.sleep(a.interval)


def cmd_verify_frontend(a) -> int:
    p = urllib.parse.urlsplit(a.url)
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in LOCAL_HOSTS):
        print("verify-frontend: APP_URL must be https", file=sys.stderr)
        return 1
    deadline = time.monotonic() + a.timeout
    while True:
        try:
            req = urllib.request.Request(a.url, headers={"User-Agent": "prior-release-workflow"})
            with _opener.open(req, timeout=20) as resp:
                ctype, head = resp.headers.get("Content-Type", ""), resp.read(MAX_BODY)
            if resp.status == 200 and "text/html" in ctype and b'id="root"' in head:
                print("frontend verified: the app answers 200 and serves the shell")
                return 0
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            print("frontend verification FAILED: the app did not serve its shell in time", file=sys.stderr)
            return 1
        time.sleep(a.interval)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_status)
    r = sub.add_parser("register")
    r.add_argument("--version", required=True); r.add_argument("--commit", required=True)
    r.add_argument("--summary-file", required=True); r.add_argument("--ci", default="green")
    r.add_argument("--trading-changes", action="store_true", help="mark a release that changes trading behaviour (starts a new period)")
    r.set_defaults(fn=cmd_register)
    p = sub.add_parser("poll")
    p.add_argument("--id", required=True); p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--interval", type=float, default=20); p.set_defaults(fn=cmd_poll)
    b = sub.add_parser("verify-backend")
    b.add_argument("--version", required=True); b.add_argument("--commit", required=True)
    b.add_argument("--prev-generation", type=int); b.add_argument("--timeout", type=float, default=600)
    b.add_argument("--interval", type=float, default=15); b.set_defaults(fn=cmd_verify_backend)
    f = sub.add_parser("verify-frontend")
    f.add_argument("--url", required=True); f.add_argument("--timeout", type=float, default=300)
    f.add_argument("--interval", type=float, default=10); f.set_defaults(fn=cmd_verify_frontend)
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

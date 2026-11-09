"""Verify the external monitoring is wired up. Reads the environment; never prints a secret.

    PUBLIC_URL=https://<service>.onrender.com HEALTHCHECKS_PING_URL=https://hc-ping.com/<uuid> \
    [HEALTHCHECKS_API_KEY=<read-only key>] [NTFY_TOPIC=...] python scripts/check_monitoring.py

Checks:
  1. API health: GET <PUBLIC_URL>/healthz answers 200 (a cold Render service can need up to ~60 s).
  2. Dead-man URL: HEALTHCHECKS_PING_URL is https and shaped like <host>/<uuid>[/slug]. It is NOT pinged (a ping
     from here would hide a real outage). With a read-only HEALTHCHECKS_API_KEY the check's status and last-ping
     age are read from the healthchecks.io API and must be "up" with a ping newer than --max-age-min (default 20).
  3. NTFY_TOPIC is set (presence only; the topic is a secret-ish name and is printed masked).
Exit codes: 0 all ok, 1 a check failed, 2 required configuration missing or malformed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
API_BASE = "https://healthchecks.io/api/v3/checks/"


def mask(s: str) -> str:
    return "<unset>" if not s else (s[:2] + "..." + s[-2:] if len(s) > 8 else "***")


def fetch(url: str, headers: dict | None = None, timeout: float = 30.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "prior-check-monitoring", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https URLs only, validated by caller)
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return 0, type(e).__name__


def parse_ping_url(url: str) -> str:
    """Return the check uuid, or raise ValueError. Never echoes the url."""
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("HEALTHCHECKS_PING_URL must be an https URL")
    parts = [p for p in u.path.split("/") if p]
    if not parts or not UUID_RE.match(parts[0]):
        raise ValueError("HEALTHCHECKS_PING_URL path must start with the check uuid (https://hc-ping.com/<uuid>)")
    return parts[0]


def check_api_health(base: str, fetcher) -> tuple[bool, str]:
    u = urlparse(base)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "PUBLIC_URL is not a valid http(s) URL"
    status, _ = fetcher(base.rstrip("/") + "/healthz", None, 60.0)
    return status == 200, f"GET /healthz -> {status or 'no response'}"


def check_deadman_status(uuid: str, api_key: str, fetcher, max_age_min: float, now: datetime | None = None) -> tuple[bool, str]:
    status, body = fetcher(API_BASE, {"X-Api-Key": api_key}, 30.0)
    if status != 200:
        return False, f"healthchecks API answered {status or 'no response'} (is the key read-only and correct?)"
    try:
        checks = json.loads(body).get("checks", [])
    except ValueError:
        return False, "healthchecks API returned unreadable data"
    mine = next((c for c in checks if str(c.get("ping_url", "")).rstrip("/").endswith(uuid)
                 or c.get("uuid") == uuid), None)
    if not mine:
        return False, "no check with that uuid in this project (wrong key or uuid?)"
    if mine.get("status") != "up":
        return False, f"check status is {mine.get('status')!r}, expected 'up'"
    lp = mine.get("last_ping")
    if not lp:
        return False, "check has never been pinged"
    age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(lp.replace("Z", "+00:00"))).total_seconds() / 60
    if age > max_age_min:
        return False, f"last ping {age:.0f} min ago (limit {max_age_min:.0f})"
    return True, f"status up, last ping {age:.0f} min ago"


def main(argv: list[str] | None = None, env: dict | None = None, fetcher=fetch, now: datetime | None = None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(description="Verify the external monitoring configuration.")
    p.add_argument("--max-age-min", type=float, default=20.0, help="newest acceptable dead-man ping age")
    p.add_argument("--skip-health", action="store_true", help="do not call the API health URL")
    a = p.parse_args(argv)
    base, ping, key, topic = (env.get(k, "").strip() for k in
                              ("PUBLIC_URL", "HEALTHCHECKS_PING_URL", "HEALTHCHECKS_API_KEY", "NTFY_TOPIC"))
    problems, failed = [], []
    if not base and not a.skip_health:
        problems.append("PUBLIC_URL is not set")
    uuid = ""
    if not ping:
        problems.append("HEALTHCHECKS_PING_URL is not set")
    else:
        try:
            uuid = parse_ping_url(ping)
        except ValueError as e:
            problems.append(str(e))
    if problems:
        for m in problems:
            print(f"CONFIG: {m}", file=sys.stderr)
        return 2
    if not a.skip_health:
        ok, msg = check_api_health(base, fetcher)
        print(f"[{'ok' if ok else 'FAIL'}] api health: {msg}")
        if not ok:
            failed.append("api")
    print(f"[ok] dead-man URL well-formed (check {mask(uuid)}); not pinged")
    if key:
        ok, msg = check_deadman_status(uuid, key, fetcher, a.max_age_min, now)
        print(f"[{'ok' if ok else 'FAIL'}] dead-man check: {msg}")
        if not ok:
            failed.append("deadman")
    else:
        print("[skip] dead-man status: HEALTHCHECKS_API_KEY not set (optional read-only key)")
    print(f"[{'ok' if topic else 'warn'}] ntfy topic: {mask(topic)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

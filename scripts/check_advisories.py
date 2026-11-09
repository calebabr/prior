#!/usr/bin/env python3
"""Advisory gate for the pinned Python dependencies (SC-3). Stdlib only; talks to https://api.osv.dev.

Reads `name==version` pins from one or more requirements files, asks OSV (the data source pip-audit uses) for each
pin, and fails when an advisory has a FIXED version available and is not in the allowlist. Advisories with no fix
are listed but do not fail the run. A network or API failure prints a warning and exits 0 (a flaky third party must
not block a build); a malformed allowlist exits 2.

  python scripts/check_advisories.py control_api/requirements.txt [more files] [--allowlist FILE]

Allowlist (scripts/advisory_allowlist.txt): one advisory id (GHSA-/PYSEC-/CVE- ...) per line, `#` starts a comment.
Every entry needs a comment with the review reason, so an exception is never silent.
Exit codes: 0 clean / warned / only unfixable, 1 fixable advisories found, 2 usage or allowlist error.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

OSV = "https://api.osv.dev"
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)", re.M)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_ALLOWLIST = Path(__file__).with_name("advisory_allowlist.txt")

Http = Callable[[str, Optional[dict]], dict]


def http_json(url: str, body: Optional[dict] = None) -> dict:
    """GET (body None) or POST JSON, https only, 20 s timeout."""
    if not url.startswith("https://"):
        raise ValueError("https only")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - scheme checked above
        return json.loads(resp.read().decode("utf-8"))


def parse_pins(text: str) -> dict:
    return {n.lower().replace("_", "-"): v for n, v in PIN_RE.findall(text)}


def parse_allowlist(text: str) -> dict:
    out = {}
    for no, raw in enumerate(text.splitlines(), 1):
        line, _, comment = raw.partition("#")
        line = line.strip()
        if not line:
            continue
        if not ID_RE.match(line):
            raise ValueError(f"allowlist line {no}: not an advisory id")
        if not comment.strip():
            raise ValueError(f"allowlist line {no}: {line} needs a '# reason' comment")
        out[line] = comment.strip()
    return out


def fixed_versions(vuln: dict) -> list:
    fixes = []
    for aff in vuln.get("affected", []):
        for rng in aff.get("ranges", []):
            fixes += [e["fixed"] for e in rng.get("events", []) if "fixed" in e]
    return sorted(set(fixes))


def audit(pins: dict, allow: dict, http: Http = http_json) -> tuple:
    """Returns (fixable, unfixable, allowed) as lists of (package, version, id, fixed_versions)."""
    names = sorted(pins)
    res = http(f"{OSV}/v1/querybatch", {"queries": [
        {"package": {"name": n, "ecosystem": "PyPI"}, "version": pins[n]} for n in names]})
    results = res.get("results", [])
    if len(results) != len(names):
        raise ValueError("OSV returned an unexpected number of results")
    fixable, unfixable, allowed = [], [], []
    for name, r in zip(names, results):
        for v in r.get("vulns", []) or []:
            vid = v["id"]
            detail = http(f"{OSV}/v1/vulns/{vid}", None)
            ids = {vid, *detail.get("aliases", [])}
            fixes = fixed_versions(detail)
            row = (name, pins[name], vid, fixes)
            if ids & set(allow):
                allowed.append(row)
            elif fixes:
                fixable.append(row)
            else:
                unfixable.append(row)
    return fixable, unfixable, allowed


def main(argv: list, http: Http = http_json, out=sys.stdout) -> int:
    args, files, allow_path = list(argv), [], DEFAULT_ALLOWLIST
    while args:
        a = args.pop(0)
        if a == "--allowlist" and args:
            allow_path = Path(args.pop(0))
        else:
            files.append(Path(a))
    if not files:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        allow = parse_allowlist(allow_path.read_text(encoding="utf-8")) if allow_path.exists() else {}
        pins = {}
        for f in files:
            pins.update(parse_pins(f.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        fixable, unfixable, allowed = audit(pins, allow, http)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"WARNING: advisory check skipped, OSV unavailable or unreadable ({type(e).__name__}); not failing the build.", file=out)
        return 0
    print(f"checked {len(pins)} pinned packages against OSV", file=out)
    for n, v, vid, _ in allowed:
        print(f"allowed   {n}=={v} {vid}: {allow.get(vid, 'reviewed via alias')}", file=out)
    for n, v, vid, _ in unfixable:
        print(f"no fix    {n}=={v} {vid} (no fixed version published; not failing)", file=out)
    for n, v, vid, fx in fixable:
        print(f"FIXABLE   {n}=={v} {vid}: upgrade to {', '.join(fx)}", file=out)
    return 1 if fixable else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

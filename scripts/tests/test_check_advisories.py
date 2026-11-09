"""scripts/check_advisories.py with a fake HTTP layer (no network)."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_advisories as ca  # noqa: E402

PINS = "anyio==4.15.1 \\\n    --hash=sha256:aa\n    # via x\nCertifi==2026.7.22 \\\n    --hash=sha256:bb\n"


def fake(vulns_by_pkg, details):
    def http(url, body=None):
        if url.endswith("/v1/querybatch"):
            return {"results": [{"vulns": [{"id": i} for i in vulns_by_pkg.get(q["package"]["name"], [])]}
                                for q in body["queries"]]}
        return details[url.rsplit("/", 1)[1]]
    return http


FIXED = {"GHSA-1": {"id": "GHSA-1", "aliases": ["CVE-9"], "affected": [
    {"ranges": [{"events": [{"introduced": "0"}, {"fixed": "2026.10.23"}]}]}]}}
UNFIXED = {"GHSA-2": {"id": "GHSA-2", "affected": [{"ranges": [{"events": [{"introduced": "0"}]}]}]}}


def write(tmp_path, text=PINS):
    f = tmp_path / "req.txt"
    f.write_text(text, encoding="utf-8")
    return f


def test_parse_pins_normalises_names():
    assert ca.parse_pins(PINS) == {"anyio": "4.15.1", "certifi": "2026.7.22"}


def test_clean_run_passes(tmp_path):
    out = io.StringIO()
    assert ca.main([str(write(tmp_path))], http=fake({}, {}), out=out) == 0
    assert "checked 2" in out.getvalue()


def test_fixable_advisory_fails(tmp_path):
    out = io.StringIO()
    rc = ca.main([str(write(tmp_path))], http=fake({"certifi": ["GHSA-1"]}, FIXED), out=out)
    assert rc == 1 and "FIXABLE" in out.getvalue() and "2026.10.23" in out.getvalue()


def test_unfixable_advisory_does_not_fail(tmp_path):
    out = io.StringIO()
    assert ca.main([str(write(tmp_path))], http=fake({"anyio": ["GHSA-2"]}, UNFIXED), out=out) == 0
    assert "no fix" in out.getvalue()


@pytest.mark.parametrize("entry", ["GHSA-1", "CVE-9"])   # by id or by alias
def test_allowlist_suppresses(tmp_path, entry):
    al = tmp_path / "allow.txt"
    al.write_text(f"{entry}  # reviewed 2026-11-09: not reachable\n", encoding="utf-8")
    out = io.StringIO()
    rc = ca.main([str(write(tmp_path)), "--allowlist", str(al)], http=fake({"certifi": ["GHSA-1"]}, FIXED), out=out)
    assert rc == 0 and "allowed" in out.getvalue()


@pytest.mark.parametrize("bad", ["GHSA-1\n", "not an id!  # x\n"])
def test_allowlist_requires_reason_and_valid_id(tmp_path, bad):
    al = tmp_path / "a.txt"
    al.write_text(bad, encoding="utf-8")
    assert ca.main([str(write(tmp_path)), "--allowlist", str(al)], http=fake({}, {}), out=io.StringIO()) == 2


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), TimeoutError(), json.JSONDecodeError("x", "", 0)])
def test_network_failure_warns_not_fails(tmp_path, exc):
    def boom(url, body=None):
        raise exc
    out = io.StringIO()
    assert ca.main([str(write(tmp_path))], http=boom, out=out) == 0
    assert "WARNING" in out.getvalue()


def test_wrong_result_count_is_a_warning(tmp_path):
    out = io.StringIO()
    assert ca.main([str(write(tmp_path))], http=lambda u, b=None: {"results": []}, out=out) == 0
    assert "WARNING" in out.getvalue()


def test_no_files_is_usage_error():
    assert ca.main([], http=fake({}, {}), out=io.StringIO()) == 2


def test_http_json_refuses_plain_http():
    with pytest.raises(ValueError):
        ca.http_json("http://api.osv.dev/v1/querybatch", {})


def test_repo_allowlist_parses():
    assert ca.parse_allowlist(ca.DEFAULT_ALLOWLIST.read_text(encoding="utf-8")) == {}

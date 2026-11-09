"""S-44: the private-overlay GitHub token never reaches the log buffer (env value or any GitHub token shape)."""
from __future__ import annotations

import logging

import pytest

from control_api.services import logbuffer as lb

# Built at runtime so no token-shaped literal sits in the repo (secret scanners).
FAKE_PAT = "github" + "_pat_" + "11ABCDEFG0" + "a1b2c3d4e5" * 4
FAKE_GHP = "gh" + "p_" + "Zz9" * 12
FAKE_GHS = "gh" + "s_" + "Q1w2E3r4T5" * 3


@pytest.mark.parametrize("tok", [FAKE_PAT, FAKE_GHP, FAKE_GHS])
def test_github_token_shapes_are_redacted_anywhere(tok):
    out = lb.redact(f"git clone https://x-access-token:{tok}@github.com/owner/private.git failed")
    assert tok not in out and "[REDACTED]" in out


def test_private_repo_token_env_value_is_redacted(monkeypatch):
    value = "custom-" + "token-value-" * 3          # not GitHub-shaped: caught by the env-name list
    monkeypatch.setenv("PRIVATE_REPO_TOKEN", value)
    assert "PRIVATE_REPO_TOKEN" in lb._SECRET_ENVS
    out = lb.redact(f"overlay fetch with {value} returned 403")
    assert value not in out and "[REDACTED]" in out


def test_ring_buffer_never_stores_the_token(monkeypatch):
    monkeypatch.setenv("PRIVATE_REPO_TOKEN", FAKE_PAT)
    h = lb.RingBufferHandler(capacity=10)
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "auth failed for %s", (FAKE_PAT,), None)
    h.emit(rec)
    h.add(f"stdout line {FAKE_GHP}")
    joined = "\n".join(h.tail(10))
    assert FAKE_PAT not in joined and FAKE_GHP not in joined and joined.count("[REDACTED]") >= 2


def test_ordinary_text_is_untouched():
    s = "ghost_town ghp_short github_pattern KXDEMOA pass ok"
    assert lb.redact(s) == s


@pytest.mark.parametrize("name", ["HANDOVER_HMAC_KEY", "DEPLOY_STATUS_KEY", "RELEASE_TOKEN"])
def test_release_process_secrets_are_redacted(monkeypatch, name):
    """B-10: the release-process secrets are on the env-name list (defence in depth)."""
    value = "rel-" + name.lower().replace("_", "") + "-value-9z"
    monkeypatch.setenv(name, value)
    assert name in lb._SECRET_ENVS
    out = lb.redact(f"handover step saw {value} in a message")
    assert value not in out and "[REDACTED]" in out


def test_bearer_tokens_are_redacted_and_the_ping_url_rule_does_not_swallow_the_rule_after_it():
    """Regression: the hc-ping rule's trailing comment once absorbed the bearer rule that followed it on the same line."""
    out = lb.redact("Authorization: Bearer abc123.def_456-xyz and https://hc-ping.com/1234-abcd/fail")
    assert "abc123" not in out and "Bearer [REDACTED]" in out
    assert "1234-abcd" not in out and "hc-ping.com/[REDACTED]" in out
    for pattern, _rep in lb._SUBS:                  # every rule is a real, separate entry
        assert hasattr(pattern, "sub")

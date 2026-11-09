"""In-memory ring-buffer log handler with secret redaction."""
from __future__ import annotations

import collections
import logging
import os
import re

_SECRET_ENVS = ("KALSHI_DEMO_PRIVATE_KEY", "KALSHI_PROD_PRIVATE_KEY", "KALSHI_PRIVATE_KEY", "KALSHI_DEMO_API_KEY_ID",
                "KALSHI_PROD_API_KEY_ID", "KALSHI_API_KEY_ID", "JWT_SECRET", "TOTP_ENC_KEY", "TICK_TOKEN",
                "CRONJOB_API_KEY", "MONGODB_URI", "NTFY_TOPIC", "VAPID_PRIVATE_KEY",
                "PRIVATE_REPO_TOKEN",    # S-44: read-only GitHub token for the private strategy overlay
                "HANDOVER_HMAC_KEY", "DEPLOY_STATUS_KEY", "RELEASE_TOKEN",    # B-10: release-process secrets
                "HEALTHCHECKS_PING_URL")         # dead-man ping URL (anyone holding it can ping)
_SUBS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED]"),
    (re.compile(r"mongodb(\+srv)?://\S+"), "[REDACTED]"),
    # S-44: GitHub tokens (fine-grained github_pat_..., classic ghp_/gho_/ghu_/ghs_/ghr_...), wherever they appear
    (re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"), "[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"(?i)hc-ping\.com/\S+"), "hc-ping.com/[REDACTED]"),      # healthchecks.io ping URLs
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:password|secret|token|api[_-]?key|x-2fa-code|totp)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
     r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    text = str(text)
    for name in _SECRET_ENVS:
        v = (os.environ.get(name) or "").strip()
        if len(v) >= 6:
            text = text.replace(v, "[REDACTED]")
    for pat, rep in _SUBS:
        text = pat.sub(rep, text)
    return text


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(redact(self.format(record)))
        except Exception:  # noqa: BLE001 - never break logging
            pass

    def add(self, line: str) -> None:
        for part in str(line).splitlines():
            if part.strip():
                self.lines.append(redact(part))

    def tail(self, n: int = 200) -> list[str]:
        n = max(1, min(int(n), 2000))
        return list(self.lines)[-n:]


_HANDLER: RingBufferHandler | None = None


def install(capacity: int = 2000) -> RingBufferHandler:
    global _HANDLER
    # httpx/httpcore log every request URL at INFO. Outbound URLs can be capabilities (a web push endpoint, the ntfy
    # topic), and stdout is not redacted, so keep those loggers at WARNING.
    for name in ("httpx", "httpcore"):
        if logging.getLogger(name).getEffectiveLevel() < logging.WARNING:
            logging.getLogger(name).setLevel(logging.WARNING)
    if _HANDLER is None:
        _HANDLER = RingBufferHandler(capacity)
        root = logging.getLogger()
        root.addHandler(_HANDLER)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    return _HANDLER

"""Audit logging: ``await audit(request, action, ok=..., user=..., details=...)``. Never raises.

Writes ``{ts, user, action, ok, details, ip, ua}`` via ``request.app.state.store.add_audit``.
Callers must never put passwords, TOTP codes, tokens or secrets into ``details``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from .ratelimit import client_ip

log = logging.getLogger("control_api.audit")

_SENSITIVE_KEYS = {"password", "code", "token", "mfa_token", "access_token", "secret",
                   "totp", "authorization", "x-2fa-code", "jwt", "key", "private_key"}


def _username(user: Any) -> str | None:
    if user is None:
        return None
    if isinstance(user, dict):
        u = user.get("username")
        return str(u)[:64] if u is not None else None
    return str(user)[:64]


def _scrub(details: Any) -> Any:
    """Defense in depth: drop obviously sensitive keys from dict details."""
    if isinstance(details, dict):
        return {k: ("[redacted]" if str(k).lower() in _SENSITIVE_KEYS else _scrub(v))
                for k, v in details.items()}
    if isinstance(details, list):
        return [_scrub(v) for v in details]
    return details


async def audit(request: Request, action: str, *, ok: bool = True, user: Any = None,
                details: Any = None, timeout: float = 5.0) -> bool:
    """True when the audit doc was written, False when the write failed or timed out (already logged)."""
    try:
        if user is None:
            user = getattr(request.state, "user", None)
        doc = {
            "ts": datetime.now(timezone.utc),
            "user": _username(user),
            "action": str(action),
            "ok": bool(ok),
            "details": _scrub(details),
            "ip": client_ip(request),
            "ua": (request.headers.get("user-agent") or "")[:256],
        }
        # Bounded so a hung Atlas can never stall a request (Stop in particular).
        await asyncio.wait_for(request.app.state.store.add_audit(doc), timeout)
        return True
    except Exception as e:  # audit must never break the request (esp. Stop)
        try:
            log.warning("audit write failed for action=%s: %s", action, type(e).__name__)
        except Exception:
            pass
        return False

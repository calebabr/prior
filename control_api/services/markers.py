"""Read-only release / behaviour-change markers for the period timeline, built from the audit log.

A marker is {ts, kind: release|config|start|stop|mode, label, detail, changes_trading, period_id}. Only successful
audit rows count. Nothing from the audit row beyond a small allow-list of fields is copied out: no user names, IPs,
user agents, tokens or raw config values.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import periods as P

MAX_MARKERS = 100
AUDIT_SCAN = 1000
# Config keys whose change alters how much money is at risk or how orders are sized (name fragments only).
_TRADING_KEY = re.compile(r"budget|cap|max|size|sizing|stake|contracts|frac|limit", re.I)
_SAFE = re.compile(r"[^0-9A-Za-z._+-]")


def _clean(v: Any, n: int = 64) -> str:
    return _SAFE.sub("", str(v or ""))[:n]


def _marker(doc: dict) -> dict | None:
    if doc.get("ok") is False:
        return None
    act = str(doc.get("action") or "")
    d = doc.get("details") if isinstance(doc.get("details"), dict) else {}
    ts = P.iso(doc.get("ts"))
    if not ts:
        return None
    if act == "release.register":
        return {"ts": ts, "kind": "release", "label": "Release registered", "changes_trading": bool(d.get("trading_changes")),
                "detail": f"{_clean(d.get('version'))} ({_clean(d.get('commit'), 7)})"}
    if act == "release.approve":
        return {"ts": ts, "kind": "release", "label": "Release approved", "changes_trading": bool(d.get("trading_changes")),
                "detail": f"{_clean(d.get('version'))} ({_clean(d.get('commit'), 7)})"}
    if act == "config.update":
        upd = d.get("update") if isinstance(d.get("update"), dict) else {}
        keys = sorted(_clean(k, 32) for k in upd)[:6]
        return {"ts": ts, "kind": "config", "label": "Settings changed", "detail": ", ".join(keys),
                "changes_trading": any(_TRADING_KEY.search(k) for k in keys)}
    if act == "bot.start":
        return {"ts": ts, "kind": "start", "label": "Bot started", "detail": "", "changes_trading": False}
    if act == "bot.stop":
        return {"ts": ts, "kind": "stop", "label": "Bot stopped", "detail": "", "changes_trading": False}
    if act == "mode.set":
        return {"ts": ts, "kind": "mode", "label": "Mode switched", "detail": _clean(d.get("mode"), 16),
                "changes_trading": True}
    return None


def build_markers(audit_docs: list[dict], timeline: list[dict], limit: int = MAX_MARKERS) -> list[dict]:
    """Newest first, capped. `period_id` is the period containing the marker's time (None if unplaced)."""
    out = []
    for doc in audit_docs:
        m = _marker(doc)
        if m:
            out.append(m)
    out.sort(key=lambda m: m["ts"], reverse=True)
    out = out[:max(0, limit)]
    for m in out:
        t = P.utc(m["ts"])
        m["period_id"] = None
        for p in timeline:
            s, e = P.utc(p.get("started_at")), P.utc(p.get("ended_at"))
            if t and (s is None or t >= s) and (e is None or t < e):
                m["period_id"] = p["id"]
    return out

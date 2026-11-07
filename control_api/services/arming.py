"""Per-strategy live arming (Sprint 2B part 3, item 10).

The arming flags gate LIVE trading only. Each strategy (``maker``, ``arbitrage``) is armed separately and starts NOT
armed. Demo is unaffected. A live maker pass and a live arbitrage set each need their strategy armed AND the live
gates (``live_allowed``); the runner checks both right before it sends anything.

* Arming (``armed: true``) needs fresh 2FA (the route), the live gates, and the go-live preflight
  (``services/preflight.run_preflight("live")``) reporting ready. Otherwise it is refused with the failing checks.
* Disarming needs no 2FA. It takes effect in memory at once (the next pass and the next arbitrage set see it), then
  the maker's resting ``mm-`` orders in LIVE are cancelled through Stop's own cancel path, then the flag is saved.
  A disarm whose store write fails stays in force in this process and is re-applied over anything read back later.
* Stop is untouched: it never reads or writes these flags.

Persisted as the cloud-settings field ``arming`` = {maker: bool, arbitrage: bool, updated: ISO str, by: str}.
No secrets. Only a literal ``True`` counts as armed (fail closed on anything else).
"""
from __future__ import annotations

from typing import Any

STRATEGIES = ("maker", "arbitrage")
CLOUD_KEY = "arming"
BY_MAX = 64


def default() -> dict:
    return {"maker": False, "arbitrage": False, "updated": None, "by": None}


def normalize(raw: Any) -> dict:
    """Stored/unknown value -> {maker, arbitrage, updated, by}. Anything but a literal True is not armed."""
    out = default()
    if not isinstance(raw, dict):
        return out
    for s in STRATEGIES:
        out[s] = raw.get(s) is True
    u = raw.get("updated")
    if hasattr(u, "isoformat"):
        u = u.isoformat()
    out["updated"] = str(u)[:40] if u else None
    b = raw.get("by")
    out["by"] = str(b)[:BY_MAX] if b else None
    return out


def check_strategy(strategy: Any) -> str:
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {list(STRATEGIES)}")
    return str(strategy)


def view(runner: Any) -> dict:
    """GET /api/arming body: {maker: {armed, mode}, arbitrage: {armed, mode}, live_allowed, updated}."""
    a = normalize(getattr(runner, "arming", None))
    return {"maker": {"armed": a["maker"], "mode": getattr(runner, "mode", "demo")},
            "arbitrage": {"armed": a["arbitrage"], "mode": getattr(runner, "arb_mode", "demo")},
            "live_allowed": bool(runner.live_allowed()),
            "updated": a["updated"]}


def failing_checks(preflight: dict) -> list[dict]:
    """The failing preflight checks as a FastAPI-style detail list (the app shows `loc: msg` per line)."""
    out = []
    for c in (preflight or {}).get("checks") or []:
        if not isinstance(c, dict) or c.get("status") != "fail":
            continue
        cid = str(c.get("id") or "?")[:40]
        msg = str(c.get("label") or cid)[:120]
        if c.get("detail"):
            msg += f": {str(c['detail'])[:300]}"
        out.append({"loc": ["preflight", cid], "msg": msg, "id": cid, "status": "fail"})
    return out or [{"loc": ["preflight"], "msg": "the go-live checks are not ready", "id": "preflight",
                    "status": "fail"}]

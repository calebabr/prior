"""Config and markets routes. Writes: merge -> validate_config + API ceilings -> store -> atomic file write."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from ..auth.audit import audit
from ..auth.deps import current_user, require_fresh_2fa
from ..services import botfiles
from ..services import keepalive as ka

router = APIRouter(prefix="/api")
STORE_TIMEOUT_S = 5.0          # S-23: /markets never hangs on a slow store
MARKET_STAT_KEYS = ("fills", "cost", "pnl", "events", "roi", "ci_low", "ci_high")


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="allow")       # extra keys (e.g. cells) are rejected by merge_and_validate
    run_budget: float | None = None
    max_order: float | None = None
    max_open_frac: float | None = None
    poll_minutes: int | None = None
    series: list[str] | None = None
    # optional boolean, observation only. Typed loosely here so a wrong type is the same 400 list of messages as
    # every other field (merge_and_validate checks it strictly), not pydantic's 422.
    record_decisions: Any = None


async def save_config(request: Request, update: dict, user: dict, action: str) -> dict:
    r = request.app.state.runner
    store = request.app.state.store
    settings = request.app.state.settings
    before = dict(r.config) if r.config else botfiles.read_config_file(r.config_path)
    try:
        after = botfiles.merge_and_validate(before, update, settings)
    except ValueError as e:
        msgs = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
        await audit(request, action, ok=False, user=user, details={"update": update, "errors": msgs})
        raise HTTPException(400, msgs) from e
    try:
        version = await store.save_config(after, before=before, by=user.get("username", "user"))
    except Exception as e:  # noqa: BLE001
        await audit(request, action, ok=False, user=user, details={"error": "store"})
        raise HTTPException(503, "store_unavailable") from e
    await r.apply_config(after)
    changes = config_changes(before, after)
    await audit(request, action, user=user, details={"update": update, "version": version})
    return {"config": botfiles.config_view(after), "version": version, "changes": changes}


def config_changes(before: dict | None, after: dict | None) -> list[dict]:
    """[{key, from, to}] over the editable settings (item 39 toast / item 40 preview). `cells` and the arb block are
    never listed (cells are never writable; arb has its own routes)."""
    b, a = botfiles.config_view(before), botfiles.config_view(after)
    return [{"key": k, "from": b.get(k), "to": a.get(k)} for k in botfiles.EDITABLE if b.get(k) != a.get(k)]


async def _versions(request: Request, limit: int) -> list[dict]:
    try:
        return await asyncio.wait_for(request.app.state.store.list_config_versions(limit=limit), STORE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, "store_unavailable") from e


def _version_view(v: dict) -> dict:
    ts = v.get("ts")
    before, after = v.get("before"), v.get("after")
    return {"version": v.get("version"), "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "by": v.get("by"), "changes": config_changes(before, after) if before is not None else [],
            "cells_changed": before is not None and botfiles.public_config(before).get("cells")
            != botfiles.public_config(after).get("cells"),
            "undoable": before is not None and bool(config_changes(before, after))}


@router.get("/config/versions")
async def config_versions(request: Request, limit: int = Query(20, ge=1, le=50),
                          user: dict = Depends(current_user)) -> dict:
    """Item 40: the stored config history, newest first, as changes (no full configs, never cells)."""
    vs = await _versions(request, limit)
    return {"versions": [_version_view(v) for v in vs],
            "undo": _version_view(vs[0]) if vs and _version_view(vs[0])["undoable"] else None}


class UndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int | None = None          # the version the user saw as "last change"; 409 if it is no longer the last


@router.post("/config/undo")
async def config_undo(request: Request, body: UndoIn | None = None,
                      user: dict = Depends(require_fresh_2fa)) -> dict:
    """Item 40: re-apply the previous values of the settings the LAST version changed. Fresh 2FA; goes through the
    normal save path (merge -> validate_config + API ceilings -> store -> atomic file write). Never touches cells."""
    vs = await _versions(request, 1)
    if not vs or vs[0].get("before") is None:
        raise HTTPException(409, "nothing_to_undo")
    last = vs[0]
    asked = body.version if body is not None else None
    if asked is not None and asked != last.get("version"):
        raise HTTPException(409, "stale_version")
    if asked is None and last.get("by") != user.get("username"):
        # e.g. the weekly monitor paused a losing series after the owner's change: a blind undo would re-add that
        # risk. Undoing someone else's change needs the client to name that exact version.
        raise HTTPException(409, "last_change_not_yours")
    undo = config_changes(last.get("before"), last.get("after"))
    if not undo:
        raise HTTPException(409, "nothing_to_undo")
    update = {c["key"]: c["from"] for c in undo}            # EDITABLE keys only: never cells
    res = await save_config(request, update, user, "config.undo")
    return {**res, "undone_version": last.get("version")}


def shadow_capital_field(runner) -> dict:
    """The Shadow capital setting for the Settings page: value, bounds and help. It is stored with the cloud
    settings; write it with PUT /api/cloud {"shadow_capital": n} (fresh 2FA, audited)."""
    spec = ka.CLOUD_FIELDS["shadow_capital"]
    v = (runner.cloud or {}).get("shadow_capital")
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
    return {"value": v if ok else spec["default"], "default": spec["default"], "min": spec["min"],
            "max": spec["max"], "unit": spec["unit"], "help": spec["help"], "editable": True,
            "write_via": "PUT /api/cloud", "needs_fresh_2fa": True}


@router.get("/config")
async def get_config(request: Request, user: dict = Depends(current_user)) -> dict:
    r = request.app.state.runner
    s = request.app.state.settings
    cfg = r.config or botfiles.read_config_file(r.config_path) or botfiles.public_config(botfiles.strategy().DEFAULTS)
    return {"config": botfiles.config_view(cfg), "schema": botfiles.schema(s), "ceilings": botfiles.ceilings(s),
            "shadow_capital": shadow_capital_field(r)}


@router.put("/config")
async def put_config(body: ConfigIn, request: Request, user: dict = Depends(require_fresh_2fa)) -> dict:
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    return await save_config(request, update, user, "config.update")


@router.get("/markets")
async def markets(request: Request, user: dict = Depends(current_user)) -> dict:
    r = request.app.state.runner
    ms = botfiles.strategy()
    enabled = set((r.config or {}).get("series") or [])
    summary = None
    try:
        summary = await asyncio.wait_for(request.app.state.store.last_monitor_summary(), timeout=STORE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        pass
    # maker_monitor's summary: per_series[s] = roi_ci() -> {fills, cost, pnl, events, roi, ci_low, ci_high};
    # actions = {pause: [s], keep: [s], need_more: {s: n}, collateral_return: str}
    inner = (summary or {}).get("summary") or {}
    per = inner.get("per_series")
    stats = per if isinstance(per, dict) else {}
    actions = inner.get("actions") if isinstance(inner.get("actions"), dict) else {}
    pause, keep = set(actions.get("pause") or []), set(actions.get("keep") or [])
    need = actions.get("need_more") if isinstance(actions.get("need_more"), dict) else {}
    out = []
    for s in ms.DEFAULTS["series"]:
        row = {"series": s, "enabled": s in enabled, "fills": None, "cost": None, "pnl": None,
               "events": None, "roi": None, "ci_low": None, "ci_high": None}
        if isinstance(stats.get(s), dict):
            row.update({k: v for k, v in stats[s].items() if k in MARKET_STAT_KEYS})
        row["verdict"] = "pause" if s in pause else ("keep" if s in keep else None)
        n = need.get(s)
        row["need_more"] = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None
        out.append(row)
    for row in out:
        row["fill_quality"] = r.fill_quality(row["series"])    # S-23: cached once per pass
        row["queue_stats"] = r.queue_stats(row["series"])      # same cache pattern; null if unknown
    ts = (summary or {}).get("ts")
    return {"markets": out, "report_utc": ts.isoformat() if hasattr(ts, "isoformat") else ts,
            "fill_quality": r.fill_quality(), "queue_stats": r.queue_stats()}


class MarketIn(BaseModel):
    enabled: bool


@router.put("/markets/{series}")
async def put_market(series: str, body: MarketIn, request: Request,
                     user: dict = Depends(require_fresh_2fa)) -> dict:
    ms = botfiles.strategy()
    if series not in ms.DEFAULTS["series"]:
        raise HTTPException(400, [f"series: {series} is not a confirmed market"])
    current = list((request.app.state.runner.config or {}).get("series") or [])
    if body.enabled and series not in current:
        current.append(series)
    if not body.enabled:
        current = [s for s in current if s != series]
    current = [s for s in ms.DEFAULTS["series"] if s in current]
    return await save_config(request, {"series": current}, user, "market.resume" if body.enabled else "market.pause")

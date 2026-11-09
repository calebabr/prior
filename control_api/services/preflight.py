"""Go-live preflight (Sprint 2B part 3, item 9): one read-only checklist the owner runs before switching to LIVE.

``run_preflight(target, runner=..., store=..., settings=..., app_state=...)`` returns
``{ready, target, ts, checks: [{id, label, status, detail}]}`` with status ``pass`` | ``warn`` | ``fail`` and
``ready = no check is "fail"``.

Rules:
* **Fail closed.** A check that cannot run (exception, timeout, missing data) is ``fail`` with the reason, except the
  reconciliation check, which says ``warn`` "not checked" when no cheap copy of the ledger rows exists.
* **Read only.** Nothing here places, changes or cancels an order or writes the store. The one local write is the
  Stop-path probe: a temporary file created and removed next to the kill-switch files (never the kill file itself).
* **No secrets.** Key checks report presence as booleans only; Kalshi errors are reported by type name; nothing from
  the environment is echoed back.
* ``target="demo"``: the same checks, but the live-only ones (``LIVE_ONLY``) are downgraded from ``fail`` to ``warn``.
"""
from __future__ import annotations

import asyncio
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import version as ver
from . import arb as arbsvc
from . import botfiles
from . import ops_monitor as opsmon

TARGETS = ("live", "demo")
PASS, WARN, FAIL = "pass", "warn", "fail"
STORE_TIMEOUT_S = 5.0
KALSHI_TIMEOUT_S = 6.0
TIME_TIMEOUT_S = 3.0                            # the Kalshi time read (public exchange status, Date header)
SNAPSHOT_FRESH_S = 20 * 60
SKEW_MAX_S = opsmon.SKEW_WARN_S                 # 5 s
RECON_TOLERANCE = 0.10                          # dollars
RECON_DAYS = 7
RECON_REPORT_MAX_AGE_H = 36                     # a daily report older than this says nothing about today
AUDIT_SCAN = 500                                # newest audit rows searched for the last approved release
DETAIL_MAX = 300
# Checks that only gate real money: on target=demo a "fail" here is shown as "warn".
LIVE_ONLY = frozenset({"strategy", "live_gate", "release", "reconciliation"})

LABELS = {
    "keys": "Kalshi keys for the target environment",
    "mode": "Mode and bot state",
    "live_gate": "Live confirmation (env gate)",
    "strategy": "Real strategy installed",
    "config": "Settings pass validation and API ceilings",
    "caps": "Caps within API ceilings",
    "clock": "Clock skew within 5 s",
    "store": "Database reachable",
    "snapshot": "Account snapshot fresh with the right env tag",
    "kalshi": "Kalshi reachable (read-only)",
    "kill_switch": "No kill switch active",
    "stop_path": "Stop path writable",
    "arb_paused": "No series paused by arbitrage containment",
    "alerts": "Alert channel configured",
    "release": "Approved release is the running version",
    "reconciliation": "Reconciliation residual within $0.10",
}
ORDER = tuple(LABELS)


def _clip(s: Any) -> str:
    s = str(s)
    return s if len(s) <= DETAIL_MAX else s[:DETAIL_MAX - 3] + "..."


def _res(status: str, detail: str) -> tuple[str, str]:
    return status, _clip(detail)


def _aware(d: Any) -> datetime | None:
    if not isinstance(d, datetime):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


async def _bounded(coro: Awaitable[Any], timeout: float = STORE_TIMEOUT_S) -> Any:
    return await asyncio.wait_for(coro, timeout)


# ---------------------------------------------------------------- individual checks (each returns (status, detail))

def check_keys(target: str, settings: Any) -> tuple[str, str]:
    """Presence only (booleans). Live uses the startup snapshot the live gate itself uses (S-19)."""
    from ..runner import mode_keys
    kid, pk = mode_keys(target)
    has_id, has_pk = bool(kid), bool(pk)
    if target == "live":
        snap = bool(getattr(settings, "prod_keys_present", False))
        ok = snap and has_id and has_pk
        detail = (f"prod key id present: {has_id}; prod private key present: {has_pk}; "
                  f"present at startup: {snap}")
    else:
        ok = has_id and has_pk
        detail = f"demo key id present: {has_id}; demo private key present: {has_pk}"
    return _res(PASS if ok else FAIL, detail)


def check_mode(target: str, runner: Any, settings: Any) -> tuple[str, str]:
    mode, desired = getattr(runner, "mode", None), getattr(runner, "desired", None)
    state = f"current mode {mode}, bot {desired}"
    if not getattr(settings, "runner_enabled", False):
        return _res(FAIL, f"{state}; RUNNER_ENABLED is false (the runner refuses to trade)")
    if mode not in TARGETS:
        return _res(FAIL, f"{state}; unknown mode")
    if mode == target:
        return _res(PASS, f"{state}; already in the target mode")
    return _res(WARN, f"{state}; switch to {target} after the other checks pass")


def check_live_gate(settings: Any) -> tuple[str, str]:
    keys = bool(getattr(settings, "prod_keys_present", False))
    confirm = bool(getattr(settings, "live_confirm_ok", False))
    ok = keys and confirm
    return _res(PASS if ok else FAIL,
                f"prod keys present at startup: {keys}; KALSHI_LIVE_CONFIRM set correctly: {confirm}")


def check_strategy() -> tuple[str, str]:
    if botfiles.is_standin():
        return _res(FAIL, "the public stand-in strategy is installed (IS_STANDIN); LIVE is refused")
    return _res(PASS, "the private strategy overlay is installed")


def _config_of(runner: Any) -> dict | None:
    cfg = getattr(runner, "config", None)
    if isinstance(cfg, dict) and cfg:
        return cfg
    try:
        return botfiles.read_config_file(runner.config_path)
    except Exception:  # noqa: BLE001
        return None


def check_config(runner: Any, settings: Any) -> tuple[str, str]:
    cfg = _config_of(runner)
    try:
        botfiles.validate_stored(cfg, settings)
    except ValueError as e:
        msgs = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
        return _res(FAIL, "invalid: " + "; ".join(str(m) for m in msgs[:3]))
    return _res(PASS, "validate_config and the API ceilings pass")


def check_caps(runner: Any, settings: Any) -> tuple[str, str]:
    """Generic: every maker ceiling, every numeric arbitrage cap against its API ceiling, and every other numeric
    `*_frac` setting (per-market / per-series shares of the ceiling, whatever the installed strategy names them)
    inside (0, 1]. Names only in the detail, never values of the strategy."""
    cfg = _config_of(runner)
    if not isinstance(cfg, dict) or not cfg:
        return _res(FAIL, "no settings loaded")
    bad: list[str] = []
    checked = 0
    for key, ceiling in botfiles.ceilings(settings).items():
        v = _num(cfg.get(key))
        checked += 1
        if v is None or v <= 0 or v > float(ceiling):
            bad.append(key)
    arb = arbsvc.current(cfg)
    for key, ceiling in arbsvc.ceilings(settings).items():
        if arb.get(key) is None and key in arbsvc.FRAC_FIELDS:
            continue                                            # optional share: OFF is allowed
        v = _num(arb.get(key))
        checked += 1
        if v is None or v <= 0 or v > float(ceiling):
            bad.append(f"arb.{key}")
    for key, raw in cfg.items():
        if not str(key).endswith("_frac") or key in botfiles.ceilings(settings):
            continue
        if raw is None:
            continue
        v = _num(raw)
        checked += 1
        if v is None or not (0 < v <= 1):
            bad.append(str(key))
    if bad:
        return _res(FAIL, "outside the API ceilings or not a number: " + ", ".join(sorted(bad)))
    return _res(PASS, f"{checked} caps within the API ceilings")


def check_clock(kalshi_skew_local_minus_server: float | None, store_time: datetime | None,
                now: datetime, target: str = "live") -> tuple[str, str]:
    """Server clock against Kalshi's clock (HTTP Date header). Live fails closed when Kalshi's time cannot be read
    (the database clock is no substitute for money decisions) or the skew is over SKEW_MAX_S. Demo only warns, and
    may fall back to the database clock."""
    bad = FAIL if target == "live" else WARN
    if kalshi_skew_local_minus_server is not None:
        ref, source = now - timedelta(seconds=float(kalshi_skew_local_minus_server)), "Kalshi"
    elif target == "live":
        return _res(FAIL, "could not read Kalshi's time (no Date header within the timeout)")
    elif store_time is not None:
        ref, source = _aware(store_time), "database"
    else:
        ref, source = None, None
    rep = opsmon.clock_report(ref, now, source or "none")
    if not rep["available"]:
        return _res(bad, "no reference clock (Kalshi Date header and database clock unavailable)")
    skew = rep["skew_s"]
    if abs(skew) > SKEW_MAX_S:
        return _res(bad, f"skew {skew:+.1f} s against the {source} clock")
    return _res(PASS, f"skew {skew:+.1f} s against the {source} clock")


LIVE_SNAPSHOT_PENDING = "live-tagged snapshot appears after the first live pass; verify then"


def check_snapshot(snap: dict | None, expected_env: str, target: str, now: datetime) -> tuple[str, str]:
    if not snap:
        return _res(FAIL, "no account snapshot stored")
    ts = _aware(snap.get("ts"))
    if ts is None:
        return _res(FAIL, "latest snapshot has no timestamp")
    age = (now - ts).total_seconds()
    env = snap.get("env")
    parts = [f"latest snapshot {int(age // 60)} min old, env tag {env}"]
    if age > SNAPSHOT_FRESH_S:
        return _res(FAIL, parts[0] + "; older than 20 min (is the bot running?)")
    if env not in TARGETS:
        return _res(FAIL, parts[0] + "; the snapshot carries no env tag")
    if target == "live" and expected_env == "demo" and env != "live":
        # lead decision 2026-11-07: arming happens while the mode is still demo, so no live-tagged snapshot can exist
        # yet. A fresh, tagged snapshot is required; the live tag is verified once the mode is live (then: fail).
        return _res(WARN, LIVE_SNAPSHOT_PENDING + f" ({parts[0]})")
    if env != expected_env:
        return _res(FAIL, parts[0] + f"; expected {expected_env} (the current mode)")
    if expected_env != target:
        parts.append(f"a {target} snapshot appears after the first {target} pass")
    return _res(PASS, "; ".join(parts))


def check_kill_switch(runner: Any) -> tuple[str, str]:
    maker, arb = bool(runner.kill_switch()), bool(runner.arb_kill_switch())
    if maker or arb:
        return _res(FAIL, f"maker kill switch: {maker}; arbitrage kill switch: {arb}")
    return _res(PASS, "maker kill switch: False; arbitrage kill switch: False")


def _probe_dir(d: Path) -> bool:
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".preflight-", suffix=".tmp")
    try:
        os.write(fd, b"ok")
    finally:
        os.close(fd)
        os.unlink(tmp)
    return True


def check_stop_path(runner: Any) -> tuple[str, str]:
    """Stop writes the kill-switch files; prove both folders accept a new file (a temp file, removed at once)."""
    dirs = []
    for what, p in (("maker", runner.kill_path), ("arbitrage", runner.arb_kill_path)):
        dirs.append((what, Path(p).parent))
    bad = []
    for what, d in dirs:
        try:
            if not d.is_dir():
                bad.append(f"{what}: folder missing")
                continue
            _probe_dir(d)
        except OSError as e:
            bad.append(f"{what}: {type(e).__name__}")
    if bad:
        return _res(FAIL, "kill-switch folder not writable (" + "; ".join(bad) + ")")
    return _res(PASS, "maker and arbitrage kill-switch folders are writable")


def check_arb_paused(runner: Any) -> tuple[str, str]:
    st = runner.arb_contain_status()
    paused = [p for p in (st or {}).get("paused_series") or [] if isinstance(p, dict)]
    if paused:
        names = sorted({str(p.get("series")) for p in paused})
        return _res(FAIL, f"{len(names)} series paused after a contained partial: " + ", ".join(names[:10]))
    return _res(PASS, "no series paused (this process's memory, rebuilt from the store on the first scan)")


def check_alerts(settings: Any, push_count: int | None) -> tuple[str, str]:
    ntfy = bool(getattr(settings, "ntfy_topic", None))
    vapid = bool(getattr(settings, "vapid_private_key", None))
    subs = push_count if isinstance(push_count, int) else None
    push_ok = vapid and bool(subs)
    subs_txt = "unknown (store read failed)" if subs is None else str(subs)
    detail = f"ntfy topic set: {ntfy}; web push configured: {vapid}; push subscriptions: {subs_txt}"
    return _res(PASS if (ntfy or push_ok) else FAIL, detail)


def _version_match(running: str, version: Any, commit: Any) -> bool:
    if not running or running == "0.0.0-dev":
        return False
    if isinstance(version, str) and version == running:
        return True
    c = commit.lower() if isinstance(commit, str) else ""
    r = running.lower()
    return len(r) >= 7 and len(c) >= 7 and (c.startswith(r) or r.startswith(c))


def check_release(approved: dict | None, running: str) -> tuple[str, str]:
    if running == "0.0.0-dev":
        return _res(FAIL, "running a development build (no APP_VERSION or deploy commit)")
    if not approved:
        return _res(FAIL, f"running {running}; no approved release found")
    v, c = approved.get("version"), approved.get("commit")
    short = c[:7] if isinstance(c, str) else None
    if _version_match(running, v, c):
        return _res(PASS, f"running {running} = approved {v} ({short})")
    return _res(FAIL, f"running {running}, approved {v} ({short})")


def _recon_report_result(rep: dict, now: datetime) -> tuple[str, str] | None:
    """The daily Kalshi reconciliation report (services/kalshi_recon.py): pass when ok and recent, fail when it found
    a warn/error difference; None when it is too old to say anything (the caller falls back)."""
    try:
        checked = datetime.fromisoformat(str(rep.get("checked_at")))
    except (TypeError, ValueError):
        return None
    checked = _aware(checked)
    if checked is None or (now - checked) > timedelta(hours=RECON_REPORT_MAX_AGE_H):
        return None
    day = rep.get("day")
    diffs = [d for d in rep.get("differences") or [] if isinstance(d, dict)
             and d.get("severity") in ("warn", "error")]
    if rep.get("ok") is True and not diffs:
        return _res(PASS, f"daily Kalshi reconciliation for {day}: no difference beyond $0.10")
    kinds = sorted({str(d.get("kind")) for d in diffs})
    return _res(FAIL, f"daily Kalshi reconciliation for {day}: {len(diffs)} difference(s)"
                + (f" ({', '.join(kinds[:5])})" if kinds else ""))


def check_reconciliation(recon: dict | None) -> tuple[str, str]:
    if isinstance(recon, dict) and recon.get("_kind") == "kalshi_report":
        return recon["_result"]
    if recon is None:
        return _res(WARN, "not checked (no recent ledger copy; open Analytics once, then run preflight again)")
    res = _num(recon.get("residual"))
    if res is None:
        notes = recon.get("notes") or []
        return _res(WARN, "not checked: " + (str(notes[0]) if notes else "residual unavailable"))
    if abs(res) > RECON_TOLERANCE:
        return _res(FAIL, f"residual ${res:+.2f} over the last {RECON_DAYS} days (tolerance $0.10)")
    return _res(PASS, f"residual ${res:+.2f} over the last {RECON_DAYS} days")


# ---------------------------------------------------------------- data gathering (all bounded, never raise)

async def _store_ping(store: Any) -> bool:
    return bool(await _bounded(store.ping()))


async def _latest_snapshot(store: Any) -> dict | None:
    snaps = await _bounded(store.list_account_snapshots(limit=1))
    return snaps[0] if snaps else None


async def _push_count(store: Any) -> int:
    return len(await _bounded(store.list_push_subscriptions()))


async def _approved_release(store: Any) -> dict | None:
    """The newest approved release. A store method `latest_approved_release` is used when present; otherwise the
    newest successful `release.approve` audit row (written by routes/release.py with {id, version, commit})."""
    fn = getattr(store, "latest_approved_release", None)
    if callable(fn):
        return await _bounded(fn())
    rows = await _bounded(store.list_audit(limit=AUDIT_SCAN))
    for r in rows or []:
        if isinstance(r, dict) and r.get("action") == "release.approve" and r.get("ok"):
            d = r.get("details")
            if isinstance(d, dict):
                return {"version": d.get("version"), "commit": d.get("commit"), "id": d.get("id")}
    return None


def _kalshi_client(runner: Any, mode: str):
    from .. import runner as runner_mod
    f = getattr(runner, "kalshi_factory", None)
    if f is None or f is runner_mod.default_kalshi_factory:
        f = runner_mod.telemetry_kalshi_factory       # short timeout, one retry (read-only telemetry client)
    return f(mode)


async def _kalshi_probe(runner: Any, target: str, timeout: float) -> tuple[tuple[str, str], float | None]:
    """One authenticated read (balance; exchange status if the client has no balance call). The result's values
    are never returned; only reachability and the clock skew from the Date header."""
    def go() -> float | None:
        client = _kalshi_client(runner, target)
        if client is None:
            raise LookupError("no keys for this environment")
        call = getattr(client, "get_balance", None) or getattr(client, "get_exchange_status")
        call()
        skew = getattr(client, "clock_skew_seconds", None)
        return float(skew) if isinstance(skew, (int, float)) and not isinstance(skew, bool) else None
    try:
        skew = await asyncio.wait_for(asyncio.to_thread(go), timeout)
    except asyncio.TimeoutError:
        return _res(FAIL, f"no answer within {timeout:.0f} s"), None
    except LookupError as e:
        return _res(FAIL, str(e)), None
    except Exception as e:  # noqa: BLE001  (type only: a Kalshi error body is never echoed)
        status = getattr(e, "status", None) or getattr(e, "status_code", None)
        extra = f" (HTTP {status})" if isinstance(status, int) else ""
        return _res(FAIL, f"read failed: {type(e).__name__}{extra}"), None
    env = "prod" if target == "live" else "demo"
    return _res(PASS, f"authenticated read on the {env} exchange answered"), skew


async def _kalshi_time_probe(runner: Any, target: str, timeout: float = TIME_TIMEOUT_S) -> float | None:
    """Local minus Kalshi clock, from the Date header of the public exchange-status call (no auth, no orders).
    None when it cannot be read in `timeout` seconds. Never raises."""
    def go() -> float | None:
        client = _kalshi_client(runner, target)
        if client is None:
            return None
        client.get_exchange_status()
        skew = getattr(client, "clock_skew_seconds", None)
        return float(skew) if isinstance(skew, (int, float)) and not isinstance(skew, bool) else None
    try:
        return await asyncio.wait_for(asyncio.to_thread(go), timeout)
    except Exception:  # noqa: BLE001  (timeout included; the check reports "could not read")
        return None


async def _reconciliation(app_state: Any, store: Any, env: str, now: datetime) -> dict | None:
    """Cheap only. First the newest daily Kalshi reconciliation report for this env (one store read) when the store
    has them; else the ledger residual from the analytics row cache (built after each pass by analytics.warm) when a
    fresh copy for this env exists, plus two bounded store reads. Returns None (= not checked) otherwise."""
    fn = getattr(store, "latest_recon_report", None)
    if callable(fn):
        rep = await _bounded(fn(env))
        if isinstance(rep, dict):
            res = _recon_report_result(rep, now)
            if res is not None:
                return {"_kind": "kalshi_report", "_result": res}
    if app_state is None:
        return None
    from ..routes import analytics as anr
    from . import ledger as ledgersvc
    st = getattr(app_state, "analytics", None)
    if not isinstance(st, dict):
        return None
    hit = (st.get("rows") or {}).get((env, anr.WARM_DAYS, None))
    if not hit or not (0 <= anr._clock() - hit[0] < anr.CACHE_TTL_S):
        return None
    since = now - timedelta(days=RECON_DAYS)
    snaps = await _bounded(store.list_account_snapshots(since=since - anr.SNAP_BEFORE, limit=anr.SNAP_LIMIT, env=env))
    flows = await _bounded(store.list_cash_flows(since=since - anr.SNAP_BEFORE))
    return ledgersvc.reconciliation(hit[1], snaps or [], flows or [], since=since, now=now,
                                    tolerance=RECON_TOLERANCE, snapshot_env=env)


async def _soft(coro: Awaitable[Any]) -> tuple[bool, Any]:
    try:
        return True, await coro
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


# ---------------------------------------------------------------- the whole preflight

async def run_preflight(target: str, *, runner: Any, store: Any, settings: Any, app_state: Any = None,
                        now: Callable[[], datetime] | None = None,
                        kalshi_timeout: float = KALSHI_TIMEOUT_S) -> dict:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    t = (now or (lambda: datetime.now(timezone.utc)))()
    results: dict[str, tuple[str, str]] = {}

    def run(cid: str, fn: Callable[[], tuple[str, str]]) -> None:
        try:
            results[cid] = fn()
        except Exception as e:  # noqa: BLE001   (fail closed)
            results[cid] = _res(FAIL, f"check could not run: {type(e).__name__}")

    mode = getattr(runner, "mode", None)
    snap_env = mode if mode in TARGETS else "demo"

    # independent I/O first, in parallel, each bounded
    (ping_ok, ping), (snap_ok, snap), (push_ok, push), (rel_ok, rel), (rec_ok, rec), (_k, kal), \
        (st_ok, store_time), (_t, time_skew) = await asyncio.gather(
            _soft(_store_ping(store)), _soft(_latest_snapshot(store)), _soft(_push_count(store)),
            _soft(_approved_release(store)), _soft(_reconciliation(app_state, store, snap_env, t)),
            _soft(_kalshi_probe(runner, target, kalshi_timeout)),
            _soft(_bounded(store.server_time())), _soft(_kalshi_time_probe(runner, target)))

    run("keys", lambda: check_keys(target, settings))
    run("mode", lambda: check_mode(target, runner, settings))
    run("live_gate", lambda: check_live_gate(settings))
    run("strategy", check_strategy)
    run("config", lambda: check_config(runner, settings))
    run("caps", lambda: check_caps(runner, settings))
    kalshi_res, skew = kal if _k else (_res(FAIL, f"check could not run: {kal}"), None)
    results["kalshi"] = kalshi_res
    clock_skew = time_skew if _t and time_skew is not None else skew
    run("clock", lambda: check_clock(clock_skew, store_time if st_ok else None, t, target))
    if ping_ok and ping:
        results["store"] = _res(PASS, "the database answered a ping")
    else:
        results["store"] = _res(FAIL, "no answer to a ping" if ping_ok else f"ping failed: {ping}")
    run("snapshot", lambda: check_snapshot(snap, snap_env, target, t) if snap_ok
        else _res(FAIL, f"snapshot read failed: {snap}"))
    run("kill_switch", lambda: check_kill_switch(runner))
    run("stop_path", lambda: check_stop_path(runner))
    run("arb_paused", lambda: check_arb_paused(runner))
    run("alerts", lambda: check_alerts(settings, push if push_ok else None))
    run("release", lambda: check_release(rel, ver.API_VERSION) if rel_ok
        else _res(FAIL, f"release read failed: {rel}"))
    run("reconciliation", lambda: check_reconciliation(rec) if rec_ok
        else _res(WARN, f"not checked: read failed ({rec})"))

    checks = []
    for cid in ORDER:
        status, detail = results.get(cid, _res(FAIL, "check did not run"))
        if target == "demo" and cid in LIVE_ONLY and status == FAIL:
            status, detail = WARN, "live only: " + detail
        checks.append({"id": cid, "label": LABELS[cid], "status": status, "detail": _clip(detail)})
    return {"ready": not any(c["status"] == FAIL for c in checks), "target": target, "ts": t.isoformat(),
            "checks": checks}

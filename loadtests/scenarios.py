"""The scenarios. Each takes a Ctx and returns {name, status: pass|fail|skipped, checks, metrics, notes}.

Pass bars (docs/load-tests/README.md): Stop p99 < 1 s even under load; no 5xx and no transport errors; rate limits
answer 429 (never 5xx); no memory growth over the soak beyond the stated tolerance; peak memory under 512 MB.
Scenarios that need the fake bot pass or the local test users run on a local target only; on a remote demo deploy
they are skipped with a reason.
"""
from __future__ import annotations

import asyncio
import base64
import time
from datetime import date, timedelta
from dataclasses import dataclass, field

import pyotp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .lib import MEMORY_BUDGET_MB, STOP_P99_BAR_S, Api, MemSampler, Recorder

SOAK_GROWTH_MB = 25.0       # tolerated RSS growth over the soak: the larger of this and SOAK_GROWTH_PCT of the start
SOAK_GROWTH_PCT = 10.0
TZS = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London",
       "Europe/Paris", "Asia/Tokyo", "Australia/Sydney", "America/Phoenix", "Pacific/Auckland")


@dataclass
class Ctx:
    api: Api
    is_local: bool
    cfg: dict
    sampler: MemSampler
    info: dict = field(default_factory=dict)       # local server info (users, tick token, pid); empty when remote
    steps: dict = field(default_factory=dict)      # secret -> last TOTP step used (a step can never be reused)

    def new_api(self, token: str | None) -> Api:
        return Api(self.api.base, token, self.api.rec)


def chk(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def result(name: str, checks: list[dict], metrics: dict, notes: list[str] | None = None, skipped: str | None = None):
    if skipped:
        return {"name": name, "status": "skipped", "checks": [], "metrics": {}, "notes": [skipped]}
    return {"name": name, "status": "pass" if all(c["ok"] for c in checks) else "fail",
            "checks": checks, "metrics": metrics, "notes": notes or []}


def no_failures(summary: dict, what: str) -> dict:
    return chk(f"{what}: no 5xx and no transport errors", summary["n_5xx"] == 0 and summary["n_errors"] == 0,
               f"5xx={summary['n_5xx']} errors={summary['n_errors']} of {summary['n']}")


def code_for(secret: str) -> str:
    return pyotp.TOTP(secret).now()


async def fresh_code(ctx: Ctx, secret: str) -> str:
    """A TOTP code for a step newer than any this run used (the server accepts +-1 step; wait if we need more)."""
    now_step = int(time.time()) // 30
    want = max(now_step, ctx.steps.get(secret, 0) + 1)
    if want > now_step + 1:
        await asyncio.sleep((want - 1) * 30 - time.time() + 0.5)
    ctx.steps[secret] = want
    return pyotp.TOTP(secret).at(want * 30)


async def reset_local(ctx: Ctx) -> None:
    if ctx.is_local:
        await ctx.api.call("admin", "POST", "/__loadtest/reset", anon=True)


async def local_stats(ctx: Ctx) -> dict:
    _, body, _ = await ctx.api.call("admin", "GET", "/__loadtest/stats", anon=True)
    return body or {}


async def start_trading(ctx: Ctx) -> int:
    await reset_local(ctx)
    secret = ctx.info["users"]["owner"]["totp_secret"]
    st, _, _ = await ctx.api.call("start", "POST", "/api/bot/start",
                                  headers={"X-2FA-Code": await fresh_code(ctx, secret)})
    return st


async def stop_many(ctx: Ctx, n: int, conc: int, label: str = "stop") -> None:
    sem = asyncio.Semaphore(conc)

    async def one():
        async with sem:
            await ctx.api.call(label, "POST", "/api/bot/stop")
    await asyncio.gather(*(one() for _ in range(n)))


# ---------------------------------------------------------------- (a) normal phone use
async def polling(ctx: Ctx) -> dict:
    """Overview (status) and Activity polling like phones that stay open: a few phones, each polling on a cadence."""
    phones, secs, every = ctx.cfg["phones"], ctx.cfg["polling_seconds"], ctx.cfg["poll_every"]
    rec = ctx.api.rec
    start = len(rec.recs)

    async def phone(i: int):
        end = time.monotonic() + secs
        k = 0
        while time.monotonic() < end:
            await ctx.api.call("poll_status", "GET", "/api/status")
            if k % 2 == 0:
                await ctx.api.call("poll_activity", "GET", "/api/activity?limit=50")
            if k % 5 == 0:
                await ctx.api.call("poll_healthz", "GET", "/healthz", anon=True)
            k += 1
            await asyncio.sleep(every * (0.8 + 0.4 * ((i * 7 + k) % 5) / 4))    # jitter

    await asyncio.gather(*(phone(i) for i in range(phones)))
    sub = Recorder(rec.recs[start:])
    s = sub.summary("poll_status", "poll_activity", "poll_healthz")
    ok200 = s["status"].get("200", 0) == s["n"]
    return result("polling", [no_failures(s, "polling"), chk("every poll answered 200", ok200, str(s["status"]))],
                  {"phones": phones, "seconds": secs, "poll_every_s": every, **{k: sub.summary(k) for k in
                   ("poll_status", "poll_activity", "poll_healthz")}, "all": s})


# ---------------------------------------------------------------- (b) logins and bad 2FA codes
async def auth_burst(ctx: Ctx) -> dict:
    notes, checks, metrics = [], [], {}
    if ctx.is_local:
        lt = ctx.info["users"]["locktest"]
        anon = ctx.new_api(None)
        st, body, _ = await anon.call("login_ok", "POST", "/api/auth/login",
                                      json={"username": "locktest", "password": lt["password"]}, anon=True)
        mfa = (body or {}).get("mfa_token")
        checks.append(chk("login with the right password gives an mfa token", st == 200 and bool(mfa), f"status {st}"))
        seq = []
        for _ in range(12):
            s, _, r = await anon.call("bad_2fa", "POST", "/api/auth/2fa", json={"mfa_token": mfa, "code": "000000"},
                                      anon=True)
            seq.append((s, r.headers.get("retry-after") if r is not None else None))
        codes = [s for s, _ in seq]
        first429 = next((i + 1 for i, s in enumerate(codes) if s == 429), None)
        checks += [chk("bad 2FA codes: no 5xx", all(s < 500 and s != 0 for s in codes), str(codes)),
                   chk("lockout: 429 from the 5th wrong code", first429 == 5 and all(s == 429 for s in codes[4:]),
                       f"first 429 at attempt {first429}: {codes}"),
                   chk("429 carries Retry-After", all(h for s, h in seq if s == 429))]
        s, _, _ = await anon.call("2fa_valid_while_locked", "POST", "/api/auth/2fa",
                                  json={"mfa_token": mfa, "code": code_for(lt["totp_secret"])}, anon=True)
        checks.append(chk("a correct code is refused while locked (429)", s == 429, f"status {s}"))
        metrics["bad_2fa_sequence"] = codes
        user, pwd = "owner", ctx.info["users"]["owner"]["password"] + "-wrong"
    else:
        anon = ctx.new_api(None)
        user, pwd = "loadtest-nobody", "not-a-password"       # unknown user: counts only against the per-IP bucket
        notes.append("remote: only the bad-password burst runs (it cannot lock the demo owner's account)")
    n = ctx.cfg["login_burst"]
    await asyncio.gather(*(anon.call("bad_login", "POST", "/api/auth/login", json={"username": user, "password": pwd},
                                     anon=True) for _ in range(n)))
    s = ctx.api.rec.summary("bad_login")
    c401, c429 = s["status"].get("401", 0), s["status"].get("429", 0)
    checks += [no_failures(s, "login burst"),
               chk("per-IP limit: 429 after the allowance", c429 >= 1, f"401={c401} 429={c429} of {n}"),
               chk("allowance not exceeded (<= 10 wrong passwords answered 401)", c401 <= 10, f"401={c401}")]
    metrics["login_burst"] = s
    # while the login/2FA buckets are exhausted, the owner's session and Stop still work
    st_stop, _, _ = await ctx.api.call("stop_during_burst", "POST", "/api/bot/stop")
    st_stat, _, _ = await ctx.api.call("status_during_burst", "GET", "/api/status")
    checks.append(chk("Stop and status still work while auth is rate limited", st_stop == 200 and st_stat == 200,
                      f"stop={st_stop} status={st_stat}"))
    await anon.aclose()
    return result("auth_burst", checks, metrics, notes)


# ---------------------------------------------------------------- (c) Analytics at the 180-day size
async def analytics_180(ctx: Ctx) -> dict:
    api, rec = ctx.api, ctx.api.rec
    start, mem0 = len(rec.recs), ctx.sampler.mark()
    eps = [("summary", "/api/analytics/summary?days=180"), ("ledger", "/api/analytics/ledger?days=180"),
           ("returns_day", "/api/analytics/returns?days=180&granularity=day"),
           ("returns_week", "/api/analytics/returns?days=180&granularity=week"),
           ("returns_month", "/api/analytics/returns?days=180&granularity=month"),
           ("returns_fills", f"/api/analytics/returns/fills?start={(date.today() - timedelta(days=13)).isoformat()}"
                                  f"&end={date.today().isoformat()}&limit=50"),
           ("scorecard", "/api/analytics/scorecard?days=180")]
    cold, sizes = {}, {}
    for name, path in eps:                           # first touch of each: cold (or whatever the cache holds)
        t0 = time.perf_counter()
        st, body, r = await api.call(f"cold_{name}", "GET", path)
        cold[name] = {"status": st, "secs": round(time.perf_counter() - t0, 2)}
        sizes[name] = len(r.content) if r is not None else 0
    for name, path in eps:                           # warm: back to back
        for _ in range(3):
            await api.call(f"warm_{name}", "GET", path)
    # concurrent: many phones asking for different time zones (distinct cache keys) at once, plus the shared keys
    n_par = ctx.cfg["parallel_analytics"]
    paths = [f"/api/analytics/summary?days=180&tz={TZS[i % len(TZS)]}" for i in range(n_par)]
    paths += [p for _, p in eps] * 2
    await asyncio.gather(*(api.call("parallel", "GET", p) for p in paths))
    sub = Recorder(rec.recs[start:])
    s = sub.summary()
    par = sub.summary("parallel")
    detail = par["detail"]
    allowed = {"analytics_busy", "rate_limited"}
    bad_429 = [d for d in detail if d not in allowed and sub.summary("parallel")["status"].get("429")]
    only_ok = all(r.status in (200, 429) for r in sub.recs)
    checks = [no_failures(s, "analytics"),
              chk("every answer is 200 or 429", only_ok, str(s["status"])),
              chk("every 429 is rate_limited or analytics_busy", not bad_429, str(detail)),
              chk("cold 180-day summary and ledger returned 200",
                  cold["summary"]["status"] == 200 and cold["ledger"]["status"] == 200, str(cold)),
              chk("memory stays under the 512 MB budget", (ctx.sampler.peak(mem0) or 0) < MEMORY_BUDGET_MB,
                  f"peak {ctx.sampler.peak(mem0)} MB (n/a on a remote target)")]
    return result("analytics_180", checks, {"cold": cold, "response_bytes": sizes, "parallel": par,
                                            "all": s, "peak_rss_mb": ctx.sampler.peak(mem0)})


# ---------------------------------------------------------------- (d) many simultaneous Stops
async def stop_storm(ctx: Ctx) -> dict:
    n, conc = ctx.cfg["stop_calls"], ctx.cfg["stop_conc"]
    start = len(ctx.api.rec.recs)
    await stop_many(ctx, n, conc, "stop_storm")
    s = Recorder(ctx.api.rec.recs[start:]).summary("stop_storm")
    _, st, _ = await ctx.api.call("status_after_stop", "GET", "/api/status")
    stats = await local_stats(ctx) if ctx.is_local else {}
    checks = [no_failures(s, "stop"),
              chk("every Stop answered 200", s["status"].get("200", 0) == s["n"], str(s["status"])),
              chk(f"Stop p99 under {STOP_P99_BAR_S:.0f} s", (s["p99_ms"] or 1e9) < STOP_P99_BAR_S * 1000,
                  f"p99 {s['p99_ms']} ms"),
              chk("status reports stopped", (st or {}).get("desired") == "stopped", str((st or {}).get("desired")))]
    if ctx.is_local:
        checks.append(chk("kill-switch file exists", bool(stats.get("stop_flag_file"))))
    return result("stop_storm", checks, {"concurrency": conc, "stop": s})


# ---------------------------------------------------------------- (e) push sign-up races (10-subscription cap)
def _sub(i: int, p256dh: str, auth: str) -> dict:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/loadtest-{i:04d}", "keys": {"p256dh": p256dh, "auth": auth}}


async def push_race(ctx: Ctx) -> dict:
    if not ctx.is_local:
        return result("push_race", [], {}, skipped="local only: needs VAPID configured and would fill the demo's 10 slots")
    api = ctx.api
    pub = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()      # noqa: E731
    p256dh, auth = b64(pub), b64(b"0123456789abcdef")
    st, pk, _ = await api.call("push_key", "GET", "/api/push/public-key")
    if st != 200 or not (pk or {}).get("enabled"):
        return result("push_race", [], {}, skipped=f"push not configured on the server (status {st})")
    n, cap = ctx.cfg["push_attempts"], 10
    start = len(api.rec.recs)
    await asyncio.gather(*(api.call("push_new", "POST", "/api/push/subscribe", json=_sub(i, p256dh, auth))
                           for i in range(n)))
    s = Recorder(api.rec.recs[start:]).summary("push_new")
    ok, full = s["status"].get("200", 0), s["status"].get("409", 0)
    _, lst, _ = await api.call("push_list", "GET", "/api/push/subscriptions")
    # the same device signing up again (racing itself) is idempotent and never counts twice
    dup_start = len(api.rec.recs)
    await asyncio.gather(*(api.call("push_dup", "POST", "/api/push/subscribe", json=_sub(0, p256dh, auth))
                           for _ in range(10)))
    dup = Recorder(api.rec.recs[dup_start:]).summary("push_dup")
    _, lst2, _ = await api.call("push_list2", "GET", "/api/push/subscriptions")
    await api.call("push_clear", "DELETE", "/api/push/subscriptions")
    checks = [no_failures(s, "push sign-ups"),
              chk(f"exactly {cap} of {n} concurrent sign-ups accepted", ok == cap, f"200={ok} 409={full} {s['status']}"),
              chk("the rest answered 409 too_many_subscriptions", full == n - cap and
                  s["detail"].get("too_many_subscriptions", 0) == full, str(s["detail"])),
              chk(f"stored count is exactly {cap}", (lst or {}).get("count") == cap, str((lst or {}).get("count"))),
              chk("re-subscribing an existing device at the cap stays 200 and the count stays",
                  dup["status"].get("200", 0) == dup["n"] and (lst2 or {}).get("count") == cap,
                  f"{dup['status']} count={(lst2 or {}).get('count')}")]
    return result("push_race", checks, {"attempts": n, "signups": s, "duplicate_device": dup},
                  ["the server runs with the push_subscribe limiter raised so this tests the store cap; the limiter "
                   "itself (10/min) is covered by the control_api tests"])


# ---------------------------------------------------------------- (g) contention
async def contention(ctx: Ctx) -> dict:
    """A 180-day Analytics rebuild running while a bot pass is in flight, with Stop pressed during it."""
    if not ctx.is_local:
        return result("contention", [], {}, skipped="local only: needs the fake pass; run it locally")
    api, rec, rounds = ctx.api, ctx.api.rec, ctx.cfg["rounds"]
    tick_headers = {"X-Tick-Token": ctx.info["tick_token"]}
    mem0 = ctx.sampler.mark()
    start = len(rec.recs)
    checks, per_round = [], []
    days_cycle = (180, 90, 180)                  # alternating row sets so the 2-entry rows cache keeps rebuilding
    for r in range(rounds):
        round_start = len(rec.recs)
        st = await start_trading(ctx)
        checks.append(chk(f"round {r + 1}: Start (fresh 2FA) accepted", st == 200, f"status {st}"))
        tz = TZS[r % len(TZS)]
        d = days_cycle[r % len(days_cycle)]
        _, t, _ = await api.call("tick", "POST", "/api/tick", anon=True, headers=tick_headers)
        started = False
        for _ in range(60):                          # the pass first, so the rebuild starts while it is in flight
            _, s, _ = await api.call("poll_status", "GET", "/api/status")
            if (s or {}).get("running_pass"):
                started = True
                break
            await asyncio.sleep(0.05)
        heavy = [asyncio.create_task(api.call("rebuild", "GET", p)) for p in (
            f"/api/analytics/summary?days={d}&tz={tz}", f"/api/analytics/ledger?days={d}&tz={tz}",
            f"/api/analytics/returns?days={d}&granularity=week&tz={tz}")]
        await asyncio.sleep(0.1)
        in_flight = sum(1 for h in heavy if not h.done())
        pollers = [asyncio.create_task(api.call("poll_during", "GET", "/api/status")) for _ in range(6)]
        await stop_many(ctx, ctx.cfg["stop_calls_contention"], ctx.cfg["stop_conc"], "stop_contention")
        _, after, _ = await api.call("status_after_stop", "GET", "/api/status")
        await asyncio.gather(*heavy, *pollers)
        _, t2, _ = await api.call("tick_after_stop", "POST", "/api/tick", anon=True, headers=tick_headers)
        stats = await local_stats(ctx)
        for _ in range(int(ctx.cfg["pass_seconds"] * 20) + 100):      # let the in-flight pass finish
            _, s, _ = await api.call("poll_status", "GET", "/api/status")
            if not (s or {}).get("running_pass"):
                break
            await asyncio.sleep(0.1)
        stats = await local_stats(ctx)
        sub = Recorder(rec.recs[round_start:])
        per_round.append({"round": r + 1, "tz": tz, "days": d, "tick": (t or {}).get("reason"),
                          "pass_in_flight_before_stop": started, "analytics_calls_in_flight_at_stop": in_flight,
                          "rebuild": sub.summary("rebuild"), "stop": sub.summary("stop_contention"),
                          "cancelled": stats.get("cancelled"), "resting_left": stats.get("resting_left")})
        checks += [
            chk(f"round {r + 1}: the pass was in flight when Stop landed", started, f"pass={started}"),
            chk(f"round {r + 1}: Analytics was still building when Stop landed (needs the 20,000-row size)",
                in_flight > 0 or ctx.cfg["rows"] < 5000, f"analytics_in_flight={in_flight}"),
            chk(f"round {r + 1}: flags set (desired=stopped, kill_switch) when Stop returned",
                (after or {}).get("desired") == "stopped" and bool((after or {}).get("kill_switch")) and
                bool(stats.get("stop_flag_file")), f"{(after or {}).get('desired')} {(after or {}).get('kill_switch')}"),
            chk(f"round {r + 1}: only mm- orders cancelled, the manual order untouched",
                {"o0", "o1", "o2"} <= set(stats.get("cancelled") or []) and "manual-1" not in (stats.get("cancelled") or [])
                and "manual-1" in (stats.get("resting_left") or []), f"cancelled={stats.get('cancelled')}"),
            chk(f"round {r + 1}: no new pass after Stop", (t2 or {}).get("ran") is False, str(t2)),
            chk(f"round {r + 1}: Analytics answered 200 or 429 only",
                all(x.status in (200, 429) for x in sub.of("rebuild")), str(sub.summary("rebuild")["status"]))]
    allsub = Recorder(rec.recs[start:])
    stop_s = allsub.summary("stop_contention")
    s_all = allsub.summary()
    peak = ctx.sampler.peak(mem0)
    stats = await local_stats(ctx)
    checks += [no_failures(s_all, "contention"),
               chk("every Stop answered 200", stop_s["status"].get("200", 0) == stop_s["n"], str(stop_s["status"])),
               chk(f"Stop p99 under {STOP_P99_BAR_S:.0f} s", (stop_s["p99_ms"] or 1e9) < STOP_P99_BAR_S * 1000,
                   f"p99 {stop_s['p99_ms']} ms (max {stop_s['max_ms']})"),
               chk("never two passes at once", stats.get("max_concurrent_passes", 0) <= 1,
                   str(stats.get("max_concurrent_passes"))),
               chk(f"peak memory under {MEMORY_BUDGET_MB:.0f} MB", peak is not None and peak < MEMORY_BUDGET_MB,
                   f"peak {peak} MB" if peak is not None else "psutil missing: memory not measured")]
    return result("contention", checks, {"rounds": per_round, "stop": stop_s, "peak_rss_mb": peak,
                                         "passes_started": stats.get("passes_started")})


# ---------------------------------------------------------------- (f) soak of the runner loop
async def soak(ctx: Ctx) -> dict:
    if not ctx.is_local:
        return result("soak", [], {}, skipped="local only: needs the fake pass; run it locally")
    api, rec, secs = ctx.api, ctx.api.rec, ctx.cfg["soak_seconds"]
    tick_headers = {"X-Tick-Token": ctx.info["tick_token"]}
    start, mem0 = len(rec.recs), ctx.sampler.mark()
    st = await start_trading(ctx)
    end = time.monotonic() + secs
    k = 0
    while time.monotonic() < end:                   # a cron tick every second, a phone polling every 2 s
        await api.call("soak_tick", "POST", "/api/tick", anon=True, headers=tick_headers)
        if k % 2 == 0:
            await api.call("soak_status", "GET", "/api/status")
            await api.call("soak_activity", "GET", "/api/activity?limit=50")
        if k % 30 == 15:
            await api.call("soak_ledger", "GET", "/api/analytics/ledger?days=30")
        k += 1
        await asyncio.sleep(1.0)
    stop_status, _, _ = await api.call("soak_stop", "POST", "/api/bot/stop")
    stats = await local_stats(ctx)
    g = ctx.sampler.growth()
    s = Recorder(rec.recs[start:]).summary()
    tol = None if g is None else max(SOAK_GROWTH_MB, g["first_mb"] * SOAK_GROWTH_PCT / 100)
    checks = [chk("Start accepted", st == 200, f"status {st}"), no_failures(s, "soak"),
              chk("Stop at the end answered 200", stop_status == 200),
              chk("the runner kept running passes", stats.get("passes_finished", 0) >= max(1, int(secs / (ctx.cfg["pass_seconds"] + 2)) // 2),
                  f"passes finished: {stats.get('passes_finished')}"),
              chk("never two passes at once", stats.get("max_concurrent_passes", 0) <= 1),
              chk(f"memory growth within tolerance ({SOAK_GROWTH_MB:.0f} MB or {SOAK_GROWTH_PCT:.0f}%)",
                  g is not None and g["growth_mb"] <= (tol or 0),
                  f"{g} tolerance {tol and round(tol, 1)} MB" if g else "too few samples or psutil missing")]
    return result("soak", checks, {"seconds": secs, "passes_started": stats.get("passes_started"),
                                   "passes_finished": stats.get("passes_finished"), "growth": g,
                                   "tolerance_mb": tol and round(tol, 1), "peak_rss_mb": ctx.sampler.peak(mem0),
                                   "threads_at_end": stats.get("threads"), "requests": s})


REGISTRY = {"polling": polling, "analytics": analytics_180, "push": push_race, "auth": auth_burst,
            "stop": stop_storm, "contention": contention, "soak": soak}
LETTERS = {"a": "polling", "b": "auth", "c": "analytics", "d": "stop", "e": "push", "f": "soak", "g": "contention"}
ORDER = ["polling", "analytics", "push", "auth", "stop", "contention", "soak"]   # auth locks IP buckets, so late

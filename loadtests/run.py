"""CLI:  python -m loadtests.run --scenario all --target local

    --target local                      start the throwaway API (MemoryStore, fake Kalshi, fake pass) on a free port
    --target https://<demo> --confirm-demo   the Render DEMO deploy (needs LOADTEST_TOKEN = an access token for the
                                        demo owner; refuses live/prod-looking hosts and any deploy not in demo mode)
    --scenario all | polling,auth,analytics,stop,push,soak,contention (or the letters a-g)
    --quick                             small and fast (the smoke-test sizes)
Exit code 0 only if every scenario that ran passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from . import report
from .lib import ROOT, Api, LocalServer, MemSampler, Recorder, check_remote_target, remote_state_refusal
from .scenarios import LETTERS, ORDER, REGISTRY, Ctx

REMOTE_SCENARIOS = ["polling", "analytics", "auth", "stop"]       # the rest need the fake pass / local users

FULL = dict(rows=20000, pass_seconds=5.0, phones=3, polling_seconds=20, poll_every=2.0, login_burst=25,
            parallel_analytics=10, stop_calls=200, stop_conc=50, stop_calls_contention=20, push_attempts=30,
            rounds=3, soak_seconds=120)
QUICK = dict(rows=300, pass_seconds=1.0, phones=2, polling_seconds=3, poll_every=1.0, login_burst=14,
             parallel_analytics=3, stop_calls=30, stop_conc=15, stop_calls_contention=8, push_attempts=14,
             rounds=1, soak_seconds=10)
REMOTE_LIMITS = dict(stop_calls=30, stop_conc=5, phones=2, parallel_analytics=4, login_burst=14)   # gentle on Render free


def pick(arg: str, remote: bool) -> list[str]:
    if arg == "all":
        names = [n for n in ORDER if not remote or n in REMOTE_SCENARIOS]
    else:
        names = [LETTERS.get(x.strip().lower(), x.strip().lower()) for x in arg.split(",") if x.strip()]
        bad = [n for n in names if n not in REGISTRY]
        if bad:
            raise SystemExit(f"unknown scenario(s) {bad}; choose from {sorted(REGISTRY)} or a-g or 'all'")
    return sorted(names, key=ORDER.index)


def bars(scenarios: list[dict]) -> list[dict]:
    def collect(sub: str):
        return [(s["name"], c) for s in scenarios for c in s["checks"] if sub in c["name"]]

    def bar(name: str, sub: str) -> dict:
        cs = collect(sub)
        if not cs:
            return {"bar": name, "status": "not run", "evidence": "no scenario measured it in this run"}
        bad = [f"{n}: {c['name']} ({c['detail']})" for n, c in cs if not c["ok"]]
        return {"bar": name, "status": "fail" if bad else "pass",
                "evidence": "; ".join(bad) if bad else f"{len(cs)} check(s) in " + ", ".join(sorted({n for n, _ in cs}))}
    return [bar("Stop p99 under 1 s, also under load", "Stop p99"),
            bar("No 5xx and no transport errors", "no 5xx"),
            bar("Rate limits answer 429, never an error", "429"),
            bar("Flags set first; only mm- orders cancelled", "flags set"),
            bar("No memory growth over the soak (tolerance stated in the check)", "memory growth"),
            bar("Peak memory under the 512 MB budget", "peak memory")] + \
           [bar("Push cap holds under a sign-up race", "concurrent sign-ups accepted")]


async def amain(a) -> int:
    t0, started = time.monotonic(), report.now_iso()
    remote = a.target != "local"
    cfg = dict(QUICK if a.quick else FULL)
    for k in ("rows", "pass_seconds", "soak_seconds", "rounds"):
        if getattr(a, k) is not None:
            cfg[k] = getattr(a, k)
    names = pick(a.scenario, remote)
    secrets: list[str] = []
    server = None
    rec = Recorder()
    if remote:
        base = check_remote_target(a.target, a.confirm_demo)
        token = os.environ.get("LOADTEST_TOKEN")
        if not token:
            raise SystemExit("remote runs need LOADTEST_TOKEN (an access token for the demo owner) in the environment")
        secrets.append(token)
        cfg.update(REMOTE_LIMITS)
        api, info, pid = Api(base, token, rec), {}, None
        st, body, _ = await api.call("preflight", "GET", "/api/status")
        if st != 200:
            raise SystemExit(f"refusing: GET /api/status gave {st}; the target must answer 200 and be in demo mode")
        why = remote_state_refusal(body)
        if why:
            raise SystemExit(f"refusing: {why}")
        if (body or {}).get("desired") == "running" and not a.allow_running:
            raise SystemExit("refusing: the demo bot is running; the Stop scenarios would stop it. Pass --allow-running "
                             "if that is fine.")
        label, where = "demo deploy", base
    else:
        server = LocalServer(cfg["rows"], cfg["pass_seconds"],
                             extra_args=(["--switch-interval", str(a.switch_interval)] if a.switch_interval else None))
        print(f"starting the local API (seeding {cfg['rows']} fills)...", flush=True)
        info = await asyncio.to_thread(server.start)
        secrets += [info["token"], info["tick_token"]] + [v for u in info["users"].values() for v in u.values()]
        api, pid = Api(server.base, info["token"], rec), info["pid"]
        label, where = "local MemoryStore", "local"
    sampler = MemSampler(pid)
    sampler.start()
    ctx = Ctx(api=api, is_local=not remote, cfg=cfg, sampler=sampler, info=info)
    out = []
    try:
        for n in names:
            print(f"== {n} ...", flush=True)
            api.rec = rec = Recorder()                  # per-scenario latency records
            try:
                r = await REGISTRY[n](ctx)
            except Exception as e:  # noqa: BLE001      a harness failure is a failed scenario, not a crash
                r = {"name": n, "status": "fail", "checks": [{"name": "scenario ran", "ok": False,
                                                              "detail": f"{type(e).__name__}: {e}"}],
                     "metrics": {}, "notes": []}
            out.append(r)
            print(f"   {r['status'].upper()}" + "".join(f"\n   - FAIL {c['name']} ({c['detail']})"
                                                        for c in r["checks"] if not c["ok"]), flush=True)
    finally:
        await sampler.stop()
        await api.aclose()
        if server:
            server.stop()
    res = {"label": label, "target": where, "started_utc": started, "duration_s": round(time.monotonic() - t0, 1),
           "environment": report.environment(),
           "params": {k: cfg[k] for k in ("rows", "pass_seconds", "soak_seconds", "rounds", "stop_calls") if k in cfg},
           "scenarios": out, "bars": bars(out), "memory_samples": len(sampler.samples),
           "pass": all(s["status"] in ("pass", "skipped") for s in out) and any(s["status"] == "pass" for s in out)}
    j, m = report.write(res, Path(a.out_dir), secrets)
    print(f"\n{'PASS' if res['pass'] else 'FAIL'}: wrote {j} and {m}")
    return 0 if res["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--target", required=True, help="'local' or the https URL of the DEMO deploy")
    ap.add_argument("--confirm-demo", action="store_true", help="required with a URL target")
    ap.add_argument("--allow-running", action="store_true", help="remote: allow the Stop scenarios while the demo bot runs")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--rows", type=int)
    ap.add_argument("--pass-seconds", dest="pass_seconds", type=float)
    ap.add_argument("--soak-seconds", dest="soak_seconds", type=int)
    ap.add_argument("--rounds", type=int)
    ap.add_argument("--switch-interval", dest="switch_interval", type=float,
                    help="diagnostic: set the local server's sys.setswitchinterval (tests GIL starvation of Stop)")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "load-tests"))
    a = ap.parse_args(argv)
    return asyncio.run(amain(a))


if __name__ == "__main__":
    sys.exit(main())

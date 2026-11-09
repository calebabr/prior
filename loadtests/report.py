"""JSON + markdown result files under docs/load-tests/. No secrets: the writer refuses a result that contains any
token, password or TOTP secret the run used."""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .lib import ROOT


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def environment() -> dict:
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        ram = None
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine_ram_gb": ram, "git": git_sha()}


def secrets_in(text: str, secrets: list[str]) -> list[str]:
    return [s for s in secrets if s and len(s) >= 8 and s in text]


def markdown(res: dict) -> str:
    L = [f"# Load test results: {res['label']}", "",
         f"- Run: {res['started_utc']} ({res['duration_s']} s), target: **{res['target']}**",
         f"- Environment: Python {res['environment']['python']}, {res['environment']['platform']}, "
         f"git {res['environment']['git']}",
         f"- Store: {res['label']}; fake Kalshi and a fake {res['params'].get('pass_seconds')} s bot pass; "
         f"{res['params'].get('rows')} synthetic fills over 180 days" if res["target"] == "local" else
         f"- Demo deploy, read-mostly scenarios only",
         f"- Overall: **{'PASS' if res['pass'] else 'FAIL'}**", "",
         "| Scenario | Result | Checks passed |", "|---|---|---|"]
    for s in res["scenarios"]:
        n = len(s["checks"])
        L.append(f"| {s['name']} | {s['status'].upper()} | " + (f"{sum(c['ok'] for c in s['checks'])}/{n}" if n else "-") + " |")
    L += ["", "## Pass bars", "", "| Bar | Result | Evidence |", "|---|---|---|"]
    for b in res["bars"]:
        L.append(f"| {b['bar']} | {b['status'].upper()} | {b['evidence']} |")
    for s in res["scenarios"]:
        L += ["", f"## {s['name']} ({s['status']})", ""]
        for note in s["notes"]:
            L.append(f"_{note}_")
        L += [f"- [{'x' if c['ok'] else ' '}] {c['name']}" + (f" ({c['detail']})" if c["detail"] and not c["ok"] else "")
              for c in s["checks"]]
        m = s["metrics"]
        keys = [(k, v) for k, v in m.items() if isinstance(v, dict) and "p99_ms" in v]
        if keys:
            L += ["", "| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |", "|---|---|---|---|---|---|---|---|"]
            for k, v in keys:
                L.append(f"| {k} | {v['n']} | {v['p50_ms']} | {v['p95_ms']} | {v['p99_ms']} | {v['max_ms']} | "
                         f"{v['n_429']} | {v['n_5xx']} |")
        for k in ("peak_rss_mb", "growth", "tolerance_mb", "passes_finished"):
            if m.get(k) is not None:
                L.append(f"- {k}: {m[k]}")
    L += ["", "Peak memory is the server process RSS (local runs only). The Render instance limit is 512 MB; a local "
          "RSS is an estimate of that, not a measurement of it. Atlas and Render timings are measured only by "
          "running this against the demo deploy."]
    return "\n".join(L) + "\n"


def write(res: dict, out_dir: Path, secrets: list[str]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(res["started_utc"]).strftime("%Y%m%dT%H%M%SZ")
    slug = "local-memorystore" if res["target"] == "local" else "demo-deploy"
    j, m = out_dir / f"results-{stamp}-{slug}.json", out_dir / f"results-{stamp}-{slug}.md"
    body_j, body_m = json.dumps(res, indent=2, default=str), markdown(res)
    leak = secrets_in(body_j + body_m, secrets)
    if leak:
        raise RuntimeError("refusing to write results: they contain a secret used by this run")
    j.write_text(body_j + "\n", encoding="utf-8")
    m.write_text(body_m, encoding="utf-8")
    return j, m


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

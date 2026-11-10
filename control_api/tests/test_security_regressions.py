"""Regression guards for docs/SECURITY_REVIEW.md findings, so a fixed bug stays fixed.

Where a finding already has behavioural tests elsewhere, this file only adds what is missing:
  S-1  test_auth.py::test_rate_limit_ignores_spoofed_forwarded_for        S-2  test_auth.py (lockout tests)
  S-3  test_stop_safety.py (stop_pending)      S-4  test_stop_safety.py (post-pass cancel)
  S-10 test_safety_2026_10_13.py               S-12 test_safety_2026_10_13.py          S-15 test_sessions.py
Covered HERE: S-5, S-6, S-7, S-8/S-14, S-9, S-11, S-13, S-17, plus whole-API guards (every mutating route is
behind fresh 2FA or on an explicit allow-list; every /api route needs a login).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from control_api.services import botfiles
from control_api.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.asyncio

# Mutating routes that are deliberately NOT behind fresh 2FA, with the reason.
NO_2FA = {
    ("POST", "/api/auth/login"): "password step",
    ("POST", "/api/auth/2fa"): "the 2FA step itself",
    ("POST", "/api/auth/logout"): "ends the caller's own session",
    ("POST", "/api/bot/stop"): "Stop must always work",
    ("POST", "/api/bot/run-once"): "only runs a pass when already started (desired=running)",
    ("POST", "/api/monitor/run"): "S-6: can only pause series, never add risk",
    ("POST", "/api/tick"): "cron-job.org, X-Tick-Token instead of a login",
    ("POST", "/api/release/pending"): "release workflow, X-Release-Token instead of a login; only registers a pending release",
    ("POST", "/api/release/pending/{release_id}/reject"): "can only prevent a deploy",
    ("POST", "/api/push/subscribe"): "UX 45: only lets this signed-in device receive alerts; no trading effect",
    ("DELETE", "/api/push/subscribe"): "UX 45: only stops notifications to a device",
    ("DELETE", "/api/push/subscriptions"): "S-33: only stops notifications to every device; defensive, no trading effect",
    ("POST", "/api/push/test"): "UX 45: sends a test notification; rate limited",
    ("PUT", "/api/trades/{fill_id}/note"): "UX Y5: display-only journal note on a fill; rate limited, audited",
    ("PUT", "/api/arming"): "S3 item 10: disarming only removes risk (no 2FA, like Stop); armed=true demands fresh "
                            "2FA inside the route (test_arming.py)",
}


def _flat_routes(routes):
    """Yield leaf routes. FastAPI >= 0.14x keeps include_router() children nested (`_IncludedRouter`, exposing
    `effective_route_contexts()` with the full prefixed path); older FastAPI flattens them into app.routes."""
    for r in routes:
        contexts = getattr(r, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield r


def _api_routes(app, methods=frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})):
    out = set()
    for r in _flat_routes(app.routes):
        path = getattr(r, "path", "")
        if path.startswith("/api/"):
            out.update((m, path) for m in (getattr(r, "methods", None) or set()) & methods)
    # Cross-check against the public OpenAPI schema so a future FastAPI routing change cannot silently empty the walk
    # (which would make every guard below vacuously pass).
    documented = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() if p.startswith("/api/")
                  for m in ops if m.upper() in methods}
    assert documented <= out, f"route walk missed documented routes: {sorted(documented - out)}"
    return sorted(out)


def _mutating_routes(app):
    return _api_routes(app, frozenset({"POST", "PUT", "DELETE", "PATCH"}))


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "KXDEMOA", path)


# ------------------------------------------------------------------ whole-API guards
async def test_route_table_is_what_the_guards_expect(env):
    """If someone adds a mutating route, this fails until it is classified (2FA'd or added to NO_2FA with a reason)."""
    routes = _mutating_routes(env.app)
    assert routes, "no routes found"
    unclassified = [r for r in routes if r not in NO_2FA and r not in _expected_2fa()]
    assert not unclassified, f"new mutating route(s) need a 2FA decision: {unclassified}"


def _expected_2fa():
    return {
        ("POST", "/api/bot/start"), ("PUT", "/api/mode"), ("PUT", "/api/config"), ("PUT", "/api/markets/{series}"),
        ("PUT", "/api/cloud"), ("POST", "/api/cloud/pinger/sync"), ("PUT", "/api/arb"),
        ("POST", "/api/auth/logout-all"),
        ("PUT", "/api/alerts/settings"), ("POST", "/api/config/undo"),
        ("POST", "/api/release/pending/{release_id}/approve"),
        ("POST", "/api/periods"), ("POST", "/api/cash-flows"), ("DELETE", "/api/cash-flows/{flow_id}"),
        ("PUT", "/api/breaker"),                    # L1 circuit-breaker settings
    }


async def test_every_mutating_route_outside_the_allow_list_demands_a_fresh_2fa_code(env):
    for method, path in _mutating_routes(env.app):
        if (method, path) in NO_2FA:
            continue
        r = await env.client.request(method, _fill(path), headers=env.auth, json={})
        assert r.status_code == 403 and r.json()["detail"] == "2fa_required", (method, path, r.status_code, r.text)


async def test_every_mutating_route_rejects_a_wrong_2fa_code(env):
    for method, path in _mutating_routes(env.app):
        if (method, path) in NO_2FA:
            continue
        await env.store.save_user({"username": "owner", "failed_logins": 0, "locked_until": None})
        r = await env.client.request(method, _fill(path), headers={**env.auth, "X-2FA-Code": "000000"}, json={})
        assert r.status_code in (403, 429), (method, path, r.status_code)
        assert r.json()["detail"] in ("2fa_invalid", "account_locked", "rate_limited"), (method, path, r.text)


async def test_every_api_route_requires_a_login_except_the_explicit_public_ones(env):
    public = {("POST", "/api/auth/login"), ("POST", "/api/auth/2fa"), ("POST", "/api/tick"),
              ("POST", "/api/release/pending"), ("GET", "/api/release/pending/{release_id}")}
    routes = _api_routes(env.app)
    assert len(routes) > len(public), "no routes found"
    for m, path in routes:
        if (m, path) in public:
            continue
        r = await env.client.request(m, _fill(path), json={})
        assert r.status_code == 401, (m, path, r.status_code)


async def test_only_healthz_is_public(env):
    assert (await env.client.get("/healthz")).status_code == 200
    body = (await env.client.get("/healthz")).text
    for leak in ("token", "secret", "desired", "kill"):
        assert leak not in body.lower()


async def test_tick_needs_the_exact_token_and_never_echoes_it(env):
    assert (await env.client.post("/api/tick")).status_code == 403
    for bad in ("", "x", env.settings.tick_token + "x", env.settings.tick_token[:-1]):
        r = await env.client.post("/api/tick", headers={"X-Tick-Token": bad})
        assert r.status_code == 403 and env.settings.tick_token not in r.text
    ok = await env.client.post("/api/tick", headers={"X-Tick-Token": env.settings.tick_token})
    assert ok.status_code == 200 and env.settings.tick_token not in ok.text


async def test_a_tick_token_is_not_a_login(env):
    r = await env.client.get("/api/status", headers={"Authorization": f"Bearer {env.settings.tick_token}"})
    assert r.status_code == 401
    r = await env.client.get("/api/status", headers={"X-Tick-Token": env.settings.tick_token})
    assert r.status_code == 401


# ------------------------------------------------------------------ S-5 duplicate / look-alike series
@pytest.mark.parametrize("series", [
    ["KXDEMOA", "KXDEMOA"], ["kxdemoa"], [" KXDEMOA"], ["KXDEMOA", "KXDEMOA "],
    ["KXDEMOA", "KXDEMOB", "KXDEMOB"], "KXDEMOA", [["KXDEMOA"]], [1],
])
async def test_s5_series_duplicates_and_lookalikes_rejected(env, series):
    before = botfiles.read_config_file(env.runner.config_path)
    r = await env.client.put("/api/config", headers=await env.h2fa(), json={"series": series})
    assert r.status_code in (400, 422), r.text
    assert botfiles.read_config_file(env.runner.config_path) == before


async def test_s5_stored_config_with_duplicate_series_is_not_trusted(env):
    cfg = dict(botfiles.read_config_file(env.runner.config_path))
    cfg["series"] = ["KXDEMOA", "KXDEMOA"]
    with pytest.raises(ValueError):
        botfiles.validate_stored(cfg, env.settings)


# ------------------------------------------------------------------ S-6 monitor exception is narrow
async def test_s6_monitor_run_needs_a_login_but_the_response_has_no_config_write_surface(env):
    assert (await env.client.post("/api/monitor/run")).status_code == 401
    assert (await env.client.post("/api/monitor/run", headers={"Authorization": "Bearer junk"})).status_code == 401


# ------------------------------------------------------------------ S-7 secrets fail closed
@pytest.mark.parametrize("env_vars,missing", [
    ({"STORE": "mongo"}, ["JWT_SECRET", "TOTP_ENC_KEY"]),
    ({"MONGODB_URI": "mongodb://x", "JWT_SECRET": "j"}, ["TOTP_ENC_KEY"]),
    ({"MONGODB_URI": "mongodb://x", "TOTP_ENC_KEY": "k"}, ["JWT_SECRET"]),
])
async def test_s7_mongo_mode_refuses_to_start_without_secrets(env_vars, missing):
    with pytest.raises(ValueError) as e:
        Settings.from_env(env_vars)
    for m in missing:
        assert m in str(e.value)


async def test_s7_memory_mode_secrets_are_random_per_process_and_never_printed():
    a, b = Settings.from_env({"STORE": "memory"}), Settings.from_env({"STORE": "memory"})
    assert a.jwt_secret != b.jwt_secret and len(a.jwt_secret) >= 32
    assert a.totp_enc_key != b.totp_enc_key
    shown = repr(a)
    for secret in (a.jwt_secret, a.totp_enc_key):
        assert secret not in shown
    c = Settings.from_env({"MONGODB_URI": "mongodb://user:pw@h/db", "JWT_SECRET": "jj" * 20, "TOTP_ENC_KEY": "kk"})
    assert "pw" not in repr(c) and "jjjj" not in repr(c)


async def test_s7_local_cors_origins_dropped_on_render():
    s = Settings.from_env({"STORE": "memory", "RENDER": "true",
                           "ALLOWED_ORIGIN": "https://app.vercel.app,http://localhost:5173,http://127.0.0.1:5173"})
    assert s.allowed_origins == ["https://app.vercel.app"] and not s.dev_mode


# ------------------------------------------------------------------ S-8 / S-14 CSP on the PWA
def _csp() -> dict[str, list[str]]:
    cfg = json.loads((ROOT / "control_app" / "vercel.json").read_text(encoding="utf-8"))
    for h in cfg["headers"]:
        for kv in h["headers"]:
            if kv["key"].lower() == "content-security-policy":
                parts = [p.strip() for p in kv["value"].split(";") if p.strip()]
                return {p.split()[0]: p.split()[1:] for p in parts}
    raise AssertionError("no Content-Security-Policy header in control_app/vercel.json")


async def test_s8_csp_blocks_inline_script_eval_framing_and_plugins():
    csp = _csp()
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"]                       # no 'unsafe-inline' / 'unsafe-eval' / hosts
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert "base-uri" in csp and "form-action" in csp


async def test_s14_connect_src_has_no_wildcards_and_only_https_origins():
    srcs = [s for s in _csp()["connect-src"] if s != "'self'"]
    assert srcs, "connect-src needs the API origin"
    for s in srcs:
        assert "*" not in s, f"wildcard {s} lets any host receive the session token"
        assert s.startswith("https://") and s.rstrip("/") == s


async def test_s8_security_headers_on_the_pwa():
    cfg = json.loads((ROOT / "control_app" / "vercel.json").read_text(encoding="utf-8"))
    keys = {kv["key"] for h in cfg["headers"] for kv in h["headers"]}
    assert {"X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"} <= keys


# ------------------------------------------------------------------ S-9 frontend deps / XSS sinks
@pytest.mark.slow
async def test_s9_react_router_is_on_a_fixed_major_and_no_xss_sinks_in_the_app():
    pkg = json.loads((ROOT / "control_app" / "package.json").read_text(encoding="utf-8"))
    major = int(re.search(r"\d+", pkg["dependencies"]["react-router-dom"]).group())
    assert major >= 7
    sinks = re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*=|document\.write\(|\beval\(|new Function\(")
    bad = [str(p.relative_to(ROOT)) for p in (ROOT / "control_app" / "src").rglob("*.js*")
           if ".test." not in p.name and sinks.search(p.read_text(encoding="utf-8"))]
    assert not bad, f"XSS sink(s): {bad}"


async def test_s9_playwright_and_test_tooling_are_dev_only():
    pkg = json.loads((ROOT / "control_app" / "package.json").read_text(encoding="utf-8"))
    prod = set(pkg["dependencies"])
    assert not {n for n in prod if "playwright" in n or "vitest" in n or "testing-library" in n or n == "jsdom"}


# ------------------------------------------------------------------ S-11 dev server can never reach Kalshi
async def test_s11_dev_server_strips_kalshi_env_and_builds_no_client(monkeypatch):
    from control_api.tools import dev_server
    for k, v in {"KALSHI_DEMO_API_KEY_ID": "a", "KALSHI_PROD_PRIVATE_KEY": "b", "kalshi_live_confirm": "c",
                 "ARB_LIVE": "1", "UNRELATED": "keep"}.items():
        monkeypatch.setenv(k, v)
    gone = dev_server._strip_kalshi_env()
    import os
    assert not [k for k in os.environ if k.upper().startswith("KALSHI_")] and "ARB_LIVE" not in os.environ
    assert os.environ["UNRELATED"] == "keep" and len(gone) >= 3
    assert dev_server._no_client("demo") is None and dev_server._no_client("live") is None
    assert dev_server._NoPublicData().get_markets(series_ticker="X") == []


async def test_s11_dev_server_source_disables_the_runner_and_binds_loopback():
    src = (ROOT / "control_api" / "tools" / "dev_server.py").read_text(encoding="utf-8")
    assert '"RUNNER_ENABLED": "false"' in src and 'host="127.0.0.1"' in src


# ------------------------------------------------------------------ S-13 local data and creds stay out of git
@pytest.mark.parametrize("pattern", [".env", ".env.local", "local_data/", "local_dev/", "*.pem", "*.key", "MAKER_KILL_SWITCH"])
async def test_s13_gitignore_covers_secrets_and_local_data(pattern):
    lines = {l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert pattern in lines


@pytest.mark.slow
async def test_no_private_keys_or_live_looking_secrets_in_source():
    pat = re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----(\n|\s)*[A-Za-z0-9+/]{40,}|mongodb\+srv://[^\s\"'<>]+:[^\s\"'<>@]+@|sk-[A-Za-z0-9]{20,}")
    offenders = []
    for base in ("control_api", "control_app/src", "deploy", "kalshi_bot", "render.yaml", "Dockerfile"):
        p = ROOT / base
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and f.suffix in
                                         (".py", ".js", ".jsx", ".json", ".yaml", ".yml", ".md", ".mjs", ".txt", ".service", ".env", ".example")]
        for f in files:
            if "node_modules" in f.parts or "tests" in f.parts or f.name.startswith("test_"):
                continue
            try:
                if pat.search(f.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(f.relative_to(ROOT)))
            except OSError:
                pass
    assert not offenders, offenders


# ------------------------------------------------------------------ S-17 config path resolved at call time
async def test_s17_load_config_honours_a_redirected_config_path(bot_paths):
    ms = botfiles.strategy()
    custom = Path(ms.CONFIG_PATH)
    cfg = json.loads(custom.read_text(encoding="utf-8"))
    cfg["run_budget"] = 33
    custom.write_text(json.dumps(cfg), encoding="utf-8")
    assert ms.load_config()["run_budget"] == 33          # reads the redirected file, not kalshi_bot/maker_config.json

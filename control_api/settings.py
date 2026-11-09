"""Control API settings, read from the environment (empty strings count as unset)."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field


def _env(name: str, env: dict | None = None) -> str | None:
    v = (env if env is not None else os.environ).get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


log = logging.getLogger("control_api.settings")

# The Vite dev server. Allowed as a CORS origin only in dev: never on Render, and only when DEV_MODE is on or the
# store is the in-memory dev store. A local origin in ALLOWED_ORIGIN is dropped on Render.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_LOCAL_PREFIXES = ("http://localhost", "http://127.0.0.1", "http://[::1]")


LIVE_CONFIRM = "I_UNDERSTAND_REAL_MONEY"


def live_gates_from(env) -> tuple[bool, bool]:
    """(prod keys present, owner's KALSHI_LIVE_CONFIRM ok) from an env mapping. Called ONCE at startup by
    Settings.from_env (S-19): every pass mutates the process env (a demo pass sets the confirm value), so the
    live gate is never read from os.environ at runtime."""
    def g(n):
        return ((env.get(n) or "").strip()) or None
    return (bool(g("KALSHI_PROD_API_KEY_ID") and g("KALSHI_PROD_PRIVATE_KEY")),
            g("KALSHI_LIVE_CONFIRM") == LIVE_CONFIRM)


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _num(v: str | None, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v) if v is not None else default
    except ValueError:
        x = default
    if x != x:                     # NaN
        x = default
    return max(lo, min(hi, x))


def _ping_url(v: str | None) -> str | None:
    """HEALTHCHECKS_PING_URL: an https URL without spaces, trailing slash removed; anything else disables the ping
    with a warning that never includes the value (the URL is a secret)."""
    if not v:
        return None
    if not v.lower().startswith("https://") or any(c.isspace() for c in v) or len(v) > 512:
        log.warning("HEALTHCHECKS_PING_URL ignored: it must be an https:// URL (dead-man ping off)")
        return None
    return v.rstrip("/")


def release_timing_from(g) -> dict:
    """Lease / handover timing from env (LEASE_TTL_S, LEASE_RENEW_S, PASS_BUDGET_S, SHUTDOWN_DELAY_S,
    HANDOVER_MAX_AGE_S), clamped so a bad value can never make the lease unsafe: renew <= ttl/3 (two missed renews
    still leave the lease alive) and the pass budget < ttl/2 (a pass that starts on a fresh renew ends before the
    lease could expire)."""
    ttl = _num(g("LEASE_TTL_S"), 60.0, 15.0, 600.0)
    return {"lease_ttl_s": ttl,
            "lease_renew_s": _num(g("LEASE_RENEW_S"), 20.0, 1.0, ttl / 3),
            "pass_budget_s": _num(g("PASS_BUDGET_S"), 25.0, 1.0, ttl / 2 - 1),
            # Render's default SIGTERM-to-SIGKILL window is 30 s and the free plan cannot raise it (item 11). Set
            # SHUTDOWN_DELAY_S only on a paid plan whose maxShutdownDelaySeconds really is larger.
            "shutdown_delay_s": _num(g("SHUTDOWN_DELAY_S"), 30.0, 10.0, 300.0),
            "handover_max_age_s": _num(g("HANDOVER_MAX_AGE_S"), 300.0, 30.0, 900.0)}


@dataclass
class Settings:
    store: str = "memory"
    mongodb_uri: str | None = None
    mongodb_db: str = "kalshi_control"
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    totp_enc_key: str | None = None
    allowed_origins: list[str] = field(default_factory=list)
    tick_token: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    runner_enabled: bool = True
    max_run_budget: float = 500.0
    max_open_frac: float = 0.5
    max_poll_minutes: int = 14
    max_arb_contracts: int = 25
    dev_mode: bool = False
    # S-19: live env gates snapshotted ONCE at startup (never re-read from the mutable process env).
    prod_keys_present: bool = False
    live_confirm_ok: bool = False
    public_url: str | None = None
    cronjob_api_key: str | None = None
    # Web push (UX item 45). The VAPID private key is a secret: env only, never logged, stored or returned.
    vapid_private_key: str | None = None
    vapid_subject: str | None = None
    # Release process (docs/RELEASE_PROCESS.md section 4). Secrets: env only, never logged, stored or returned.
    lease_ttl_s: float = 60.0
    lease_renew_s: float = 20.0
    pass_budget_s: float = 25.0
    shutdown_delay_s: float = 30.0           # Render's shutdown window (default 30 s; free plan): sizes the runner's shutdown
    handover_max_age_s: float = 300.0
    handover_hmac_key: str | None = None     # < 32 bytes or missing: signed handover disabled (always demo)
    deploy_status_key: str | None = None     # GET /api/deploy/status; unset -> 401
    release_token: str | None = None         # /api/release/pending*; unset -> 404 (feature invisible)
    # Shadow mode (docs/SHADOW_CONTRACT.md): SHADOW_ENABLED env AND the cloud setting shadow_enabled. Read only.
    shadow_enabled: bool = False
    # Dead-man ping (docs/MONITORING.md): a healthchecks.io ping URL. Secret (anyone holding it can ping): env only,
    # never logged, stored, audited or returned. Unset (default) = off. Only https:// URLs are used.
    healthchecks_ping_url: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        g = lambda n: _env(n, env)  # noqa: E731
        uri = g("MONGODB_URI")
        store = (g("STORE") or ("mongo" if uri else "memory")).lower()
        jwt = g("JWT_SECRET")
        totp = g("TOTP_ENC_KEY")
        if store != "memory":
            missing = [n for n, v in (("JWT_SECRET", jwt), ("TOTP_ENC_KEY", totp)) if not v]
            if missing:
                raise ValueError(f"missing required env: {', '.join(missing)}")
        if totp is None and store == "memory":
            from cryptography.fernet import Fernet
            totp = Fernet.generate_key().decode()
        public = g("PUBLIC_URL") or g("RENDER_EXTERNAL_URL")
        on_render = bool(g("RENDER") or g("RENDER_SERVICE_ID") or g("RENDER_EXTERNAL_URL"))
        dev = not on_render and _bool(g("DEV_MODE"), store == "memory")
        origins = [o.strip().rstrip("/") for o in (g("ALLOWED_ORIGIN") or "").split(",") if o.strip()]
        if on_render:
            dropped = [o for o in origins if o.startswith(_LOCAL_PREFIXES)]
            if dropped:
                log.warning("dropping local CORS origins on Render: %s", dropped)
            origins = [o for o in origins if o not in dropped]
        elif dev:
            origins += [o for o in DEV_ORIGINS if o not in origins]
        kw = dict(
            store=store, mongodb_uri=uri, mongodb_db=g("MONGODB_DB") or "kalshi_control",
            totp_enc_key=totp,
            allowed_origins=origins, dev_mode=dev,
            tick_token=g("TICK_TOKEN"), ntfy_topic=g("NTFY_TOPIC"),
            ntfy_server=(g("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
            runner_enabled=_bool(g("RUNNER_ENABLED"), True),
            max_run_budget=float(g("MAX_RUN_BUDGET") or 500),
            max_open_frac=float(g("MAX_OPEN_FRAC") or 0.5),
            max_poll_minutes=min(14, int(g("MAX_POLL_MINUTES") or 14)),
            max_arb_contracts=max(1, min(100, int(g("MAX_ARB_CONTRACTS") or 25))),
            public_url=public.rstrip("/") if public else None,
            cronjob_api_key=g("CRONJOB_API_KEY"),
            vapid_private_key=g("VAPID_PRIVATE_KEY"),
            vapid_subject=g("VAPID_SUBJECT"),
            handover_hmac_key=g("HANDOVER_HMAC_KEY"),
            deploy_status_key=g("DEPLOY_STATUS_KEY"),
            release_token=g("RELEASE_TOKEN"),
            shadow_enabled=_bool(g("SHADOW_ENABLED"), False),
            healthchecks_ping_url=_ping_url(g("HEALTHCHECKS_PING_URL")),
        )
        kw.update(release_timing_from(g))
        kw["prod_keys_present"], kw["live_confirm_ok"] = live_gates_from(env if env is not None else os.environ)
        if jwt:
            kw["jwt_secret"] = jwt
        return cls(**kw)

    def __repr__(self) -> str:  # never print secrets
        return (f"Settings(store={self.store!r}, runner_enabled={self.runner_enabled}, "
                f"public_url={self.public_url!r}, origins={self.allowed_origins!r}, dev_mode={self.dev_mode}, live_gates={self.prod_keys_present and self.live_confirm_ok})")

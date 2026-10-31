"""FastAPI app factory. Run: uvicorn control_api.main:app --host 0.0.0.0 --port $PORT (one worker)."""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI

from .settings import Settings

log = logging.getLogger("control_api")


def create_app(settings: Settings | None = None, store: Any = None,
               kalshi_factory: Callable[[str], Any] | None = None,
               cronjob_factory: Callable[[str], Any] | None = None,
               public_factory: Callable[[str], Any] | None = None,
               shadow_factory: Callable[[], Any] | None = None) -> FastAPI:
    """kalshi_factory(mode) -> authenticated client or None; public_factory(env) -> unauthenticated client for
    arb scans; cronjob_factory(api_key) -> cron-job.org client; shadow_factory() -> read-only public market data
    for shadow mode (never authenticated). Tests inject fakes for all four."""
    from .auth.middleware import install_security
    from .auth.routes import router as auth_router
    from .db import get_store
    from . import version as ver
    from .routes import alerts, analytics, arb, bot, cloud, config, data, deploy_status, periods, release, shadow, ux
    from .runner import Runner
    from .services import keepalive as ka
    from .services.cronjob import CronJobClient
    from .services.logbuffer import install as install_logbuffer

    settings = settings or Settings.from_env()
    store = store if store is not None else get_store(settings)
    install_logbuffer()
    runner = Runner(settings, store, kalshi_factory, public_factory=public_factory, shadow_factory=shadow_factory)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await runner.start()
        if settings.runner_enabled and ka.pinger_status(settings, runner.cloud)["configured"] \
                and not runner.cloud.get("cronjob_job_id"):
            await ka.sync_pinger(runner, app.state.cronjob_factory, "startup")   # create once; never per tick
        log.info("control API started: %r", settings)
        try:
            yield
        finally:
            # R2: on SIGTERM uvicorn runs this; the runner finishes its order step, persists, hands over and
            # releases the lease well inside Render's 120 s shutdown delay. Never raises.
            await runner.shutdown()
            with contextlib.suppress(Exception):
                await store.close()

    app = FastAPI(title="Kalshi Maker Bot control API", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner
    # After each bot pass, pre-build the default analytics dataset in the background (read only; a failure only logs).
    runner.post_pass_hook = lambda: runner._spawn(analytics.warm(app))
    app.state.cronjob_factory = cronjob_factory or (lambda key: CronJobClient(key))
    app.state.last_inbound_utc = None

    @app.middleware("http")
    async def record_inbound(request, call_next):
        now = datetime.now(timezone.utc)
        request.app.state.last_inbound_utc = now
        runner.last_inbound_utc = now
        return await call_next(request)

    install_security(app, settings)

    @app.get("/healthz")
    async def healthz() -> dict:
        # unchanged {"ok": true} plus the R4 version handshake and the lease role (no store read, no secrets)
        return {"ok": True, **ver.handshake(), "role": runner.role(), "lease_generation": runner.lease.generation}

    app.include_router(auth_router)
    for mod in (bot, config, data, cloud, arb, analytics, ux, alerts, deploy_status, release, shadow, periods):
        app.include_router(mod.router)
    return app


def _lazy_app() -> FastAPI:
    return create_app()


class _LazyApp:
    """Builds the real app on first ASGI call, so importing this module does no env reads or network."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    def __getattr__(self, name: str):
        if self._app is None:
            self._app = _lazy_app()
        return getattr(self._app, name)

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = _lazy_app()
        await self._app(scope, receive, send)


app = _LazyApp()

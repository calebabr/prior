# syntax=docker/dockerfile:1.7
# Control API + in-process bot runner, as a container (for any VM later; Render uses render.yaml
# with the native Python runtime instead). Same app, same env vars as deploy/env.example.
#
#   docker build -t kalshi-control .
#   docker run --rm -p 8000:8000 --env-file /etc/kalshi/bot.env kalshi-control
#
# Optional private strategy overlay (BuildKit secret, never stored in a layer, git is not in the final image):
#   PRIVATE_REPO_TOKEN=<read-only token> docker build --secret id=private_repo_token,env=PRIVATE_REPO_TOKEN #       --build-arg PRIVATE_REPO_REF=main -t kalshi-control .
# Without the secret the image is a demo-only build (the public stand-in; live mode is refused).
# ---- stage 1: fetch + overlay the private strategy (needs git; discarded afterwards) ----
FROM python:3.11-slim AS overlay
ARG PRIVATE_REPO_REF=main
ENV PRIVATE_REPO_REF=${PRIVATE_REPO_REF}
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates     && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY scripts/ scripts/
COPY control_api/ control_api/
COPY kalshi_bot/ kalshi_bot/
RUN --mount=type=secret,id=private_repo_token,required=false     PRIVATE_REPO_TOKEN_FILE=/run/secrets/private_repo_token TARGET_DIR=/src bash scripts/fetch_private_overlay.sh

# ---- stage 2: the runtime image ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# Non-root user with no login shell.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

# Hash-pinned lock (control_api/requirements.txt, generated from requirements.in). --require-hashes
# makes pip refuse any file whose sha256 isn't in the lock; --only-binary refuses sdists (no
# build scripts run at install time); --no-deps because the lock already lists the full tree.
COPY control_api/requirements.txt control_api/requirements.txt
RUN pip install --require-hashes --only-binary :all: --no-deps -r control_api/requirements.txt \
    && pip check

COPY --from=overlay /src/control_api/ control_api/
COPY --from=overlay /src/kalshi_bot/ kalshi_bot/

# The bot writes logs/, MAKER_KILL_SWITCH and maker_config.json inside kalshi_bot/,
# so only that directory is writable by the app user. Code elsewhere stays root-owned.
RUN mkdir -p kalshi_bot/logs && chown -R app:app kalshi_bot

USER app
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)" || exit 1

# --workers 1: the runner lives in the process; more workers = more runners.
CMD ["sh", "-c", "exec uvicorn control_api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --timeout-graceful-shutdown 5"]

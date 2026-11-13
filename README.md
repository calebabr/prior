# Prior - Research-First Kalshi Trading Bot and Control App

![Python](https://img.shields.io/badge/-Python-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/-React-61DAFB?style=flat-square&logo=react&logoColor=white)
![Vite](https://img.shields.io/badge/-Vite-646CFF?style=flat-square&logo=vite&logoColor=white)
![MongoDB](https://img.shields.io/badge/-MongoDB-47A248?style=flat-square&logo=mongodb&logoColor=white)
![Vercel](https://img.shields.io/badge/-Vercel-000000?style=flat-square&logo=vercel&logoColor=white)
![Render](https://img.shields.io/badge/-Render-46E3B7?style=flat-square&logo=render&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)

This is my personal research project: a way to take trading strategies from the academic literature, implement and
backtest them, test them on Kalshi's demo exchange with mathematical analysis, and only then risk real money. A
FastAPI control API runs the bot in-process, and a React PWA lets me start, stop and supervise it from my phone. The
whole infrastructure runs on free tiers.

**Why "Prior"?** In Bayesian statistics a prior is what you believe before you see the evidence, which is how I try to approach every strategy here: write the belief down, then let the data update it.

**Project status:** running on Kalshi's demo exchange in named evaluation periods; live trading is not enabled.

## Overview

I built this because I'm interested in probabilistic modeling under uncertainty and in code, and I wanted to see how
far careful AI-assisted development could take a project like this. My workflow is deliberately slow and honest:

1. **Read and Implement.** Pick a strategy or an effect from the literature and write it down as code.
2. **Backtest.** Write down the hypotheses and pass/fail rules before looking at outcomes, split the data into
   discovery, holdout and forward sets, cluster by event day, correct for multiple tests, and report negative results
   plainly. An independent verifier recomputes the headline numbers.
3. **Run on Demo Data.** The bot trades Kalshi's demo exchange for weeks in named evaluation periods. Demo shows that
   the machinery works (orders, caps, fills, settlement, reconciliation); it does not prove returns, because demo
   fills are not real queue fills.
4. **Estimate Before Risking Real Money.** A Shadow mode records what the strategy would have quoted on real public
   market data, with no orders and no credentials, and scores it with real settlements under pessimistic and
   optimistic queue assumptions.
5. **Go Live.** Live mode needs fresh 2FA, a typed confirmation and a server-side gate. The first live phase is a
   small, supervised execution test with the expectations written down in advance.

## Features

- **Run and supervise from a phone** – Start, Stop, mode (demo or live), settings, logs, activity and account state in
  an installable PWA (`docs/PHONE.md`)
- **Stop always works** – One tap, no 2FA, and it does not depend on the database, the exchange or the network
- **Circuit breaker** – Stops everything on its own when equity falls past an owner-set drawdown or daily-loss limit (on by default in live, off in demo); restarting after a trip needs fresh 2FA and an explicit acknowledgement (`docs/API.md`)
- **Shadow mode** – Estimates what the strategy would have quoted on public market data without placing orders
  (`docs/SHADOW_CONTRACT.md`)
- **Demo periods** – Named evaluation windows with cash flows tracked per period, so results are never mixed across
  versions of the system
- **Analytics and health** – Fills, quote and fill quality, account snapshots, benchmark comparison and per-period
  results
- **Arbitrage scanner** – A log-only default and a gated demo-execute path behind a shared risk gate
- **Alerts** – Phone notifications through ntfy.sh and Web Push for fills, errors, restarts and safety events
- **Zero-downtime updates** – A manual release workflow I approve with fresh 2FA, with a lease handover so two
  instances never trade at once (`docs/RELEASE_PROCESS.md`)

## Architecture

```
 Phone (PWA) --static files--> Vercel (control_app)
     |
     | HTTPS + Bearer JWT (+ fresh 2FA code for sensitive actions)
     v
 Render free web service (native Python runtime)                GitHub Actions
   FastAPI control_api  +  in-process bot runner  <-- release -- (CI, manual
     |        |    |  ^            ^                              release workflow)
     |        |    |  | lease      | POST /api/tick (shared secret)
     |        |    |  | (Atlas)    |
     |        |    |  v            |
     |        |    +--> Kalshi API       cron-job.org + self-ping
     |        |        (demo or prod)         (keep-awake)
     |        v
     |   MongoDB Atlas M0: users, config versions, journal, audit, snapshots,
     |   desired state, shadow episodes, periods, lease
     v
 ntfy.sh / Web Push <-- alerts from the runner

 Oracle Always Free VM (separate, keyless): timer-driven recorder of public market data -> offline research
```

- **`control_app` (Vercel)** – A mobile-first React + Vite PWA that talks only to the control API. The header always
  shows the mode and the Stop button.
- **`control_api` (Render)** – A FastAPI service with single-user auth (password, TOTP 2FA, short-lived JWTs) and the
  bot runner in the same process. The runner executes a pass on a timer (default 10 minutes) and on external ticks,
  holds a single-leader lease, and writes every action to the journal and audit log.
- **`kalshi_bot`** – The trading code the runner calls. This repository ships a stand-in with the same interface; the
  real strategy is installed at deploy time.
- **Atlas** – Configuration versions (every settings change is versioned and undoable), the journal, audit trail,
  account snapshots, shadow episodes, periods and cash flows, the desired state and the lease document.
- **Keep-awake** – Render sleeps when idle, so a self-ping and a cron-job.org job call `POST /api/tick`. Both
  intervals stay under the 15-minute sleep limit.
- **Release path** – CI runs the tests and scanners; the release workflow is manual and hands leadership from the old
  instance to the new one with a signed token and fencing.

Step-by-step data flows (status, Stop, Start, config change, keep-awake, restart recovery) are in
`docs/ARCHITECTURE.md`.

## Tech Stack

- **Backend:** Python 3.11, FastAPI, Uvicorn, Pydantic, async PyMongo
- **Frontend:** React + Vite, installable PWA, hosted on Vercel
- **Database:** MongoDB Atlas M0, with an in-memory store for dev and tests
- **Hosting:** Render free web service (native Python runtime)
- **Scheduling and alerts:** self-ping, cron-job.org, ntfy.sh, Web Push
- **Auth:** single user, Argon2 password hash, TOTP 2FA, JWT sessions, rate limiting
- **Testing:** pytest, Vitest and Testing Library, Playwright
- **CI and security:** GitHub Actions, semgrep, gitleaks, a denylist scan, hash-pinned dependency locks
- **Research recorder:** systemd timer on an Oracle Always Free VM

## Built to Cost Nothing

My constraint was to spend as little as possible apart from funding the accounts. Every layer runs on a free tier,
and the design bends around the limits of each:

| Need | Free-tier choice | What the design does about its limits |
|---|---|---|
| API and bot runner | Render free web service | Sleeps after 15 minutes idle, so a self-ping and an external cron job keep it awake (every 14 minutes or less); the runner restores its state from the database after every restart |
| Phone app | Vercel Hobby | Static files only; the app never talks to Kalshi |
| Database | MongoDB Atlas M0 (512 MB) | TTL indexes expire old rows to stay under the size limit; Stop does not depend on it |
| Scheduler and keep-awake | cron-job.org | One job calls the tick endpoint with a shared secret |
| Alerts | ntfy.sh and Web Push | Fire-and-forget, never blocks a pass or Stop |
| CI and releases | GitHub Actions | A manual release workflow that needs my approval and a fresh 2FA code |
| Market-data recorder | Oracle Cloud Always Free VM (1 GB) | A small timer-driven recorder of public market data for offline research; it holds no trading keys |

The only money in the system is the funding of the Kalshi account.

## Project Structure

```
prior/
├── control_api/            # FastAPI control API + bot runner
│   ├── main.py             # app factory, /healthz
│   ├── runner.py           # in-process bot runner, passes, Stop, lease use
│   ├── routes/             # API endpoints (bot, config, analytics, arb, shadow, periods, release, ...)
│   ├── services/           # pnl, shadow, risk gate, arbitrage, lease and handover, alerts, analytics
│   ├── auth/               # login, TOTP 2FA, JWTs, rate limiting, audit, account bootstrap CLI
│   ├── db/                 # Store interface, MongoDB and in-memory implementations
│   ├── tools/              # read-only local data pull
│   └── tests/
├── control_app/            # React + Vite PWA (pages, components, services, e2e tests)
├── kalshi_bot/             # public stand-in: generic Kalshi client + demo strategy
├── deploy/                 # Render, Vercel, Atlas, cron-job.org notes, env template, optional VM files
├── scripts/                # overlay installer, release gate and control, hooks, repo scanners, recorder
├── docs/                   # architecture, API, deploy, release and phone guides, design contracts
├── .github/workflows/      # tests, semgrep, gitleaks, manual release
└── README.md
```

## Usage

The full deployment (Atlas, Render, Vercel, cron-job.org, ntfy) is in `docs/DEPLOY.md`; it starts in demo mode with
no real money. For local development no cloud accounts or real keys are needed.

**Backend** (in-memory store, no database)
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r control_api/requirements.txt
uvicorn control_api.main:app --reload
```

The API is then at `http://localhost:8000`, with `GET /healthz` as a liveness check.

**Frontend**
```bash
cd control_app
npm install
npm run dev
```

The app talks to the API at `VITE_API_BASE_URL` (see `control_app/.env.example`). Signing in needs an account, created
once with the bootstrap CLI against a real store (`python -m control_api.auth.bootstrap`, see `docs/DEPLOY.md`).

## API Endpoints

All routes live under `/api` and need a signed-in session (Bearer JWT) unless noted. **Fresh 2FA** means a TOTP code
verified within the last few minutes. Request and response details for the periods and cash-flow routes are in
`docs/API.md`; the others are defined in `control_api/routes/`.

### Auth
- `POST /api/auth/login`, `POST /api/auth/2fa` – Password step, then TOTP step, returning an access token
- `POST /api/auth/logout`, `POST /api/auth/logout-all` – End this session, or every session
- `GET /api/auth/me` – Current user and session info

### Bot control
- `GET /api/status` – Runner state, account, rolling and today's P&L, recent passes, limits
- `POST /api/bot/stop` – Stop the bot and the arbitrage scanner (no 2FA; always answers)
- `POST /api/bot/start` – Start trading (fresh 2FA; live is never restored after a restart)
- `POST /api/bot/run-once` – Run one pass now
- `PUT /api/mode` – Switch demo or live (fresh 2FA; live also needs a typed confirmation and a server gate)
- `POST /api/tick` – External keep-awake and scheduler tick (`X-Tick-Token` header, no session)
- `POST /api/monitor/run` – Run the periodic health check
- `GET /api/breaker`, `PUT /api/breaker` – Circuit breaker settings, ceilings and live state (write: fresh 2FA); `status.breaker` shows a trip, and Start or arbitrage enable then need `ack_breaker`
- `GET /api/arming`, `PUT /api/arming` – Live arming (arm: fresh 2FA; disarm: session only); `GET /api/preflight` – live readiness checks

### Settings
- `GET /api/config`, `PUT /api/config` – Read, or validate and atomically write, the bot settings (write: fresh 2FA)
- `GET /api/config/versions`, `POST /api/config/undo` – List past versions, restore the previous one (undo: fresh 2FA)
- `GET /api/markets`, `PUT /api/markets/{series}` – Per-series monitoring, pause or resume (write: fresh 2FA)
- `GET /api/cloud`, `PUT /api/cloud` – Cloud-side settings: keep-awake, Shadow, alert timing (write: fresh 2FA)
- `POST /api/cloud/pinger/sync` – Sync the cron-job.org job (fresh 2FA)
- `GET /api/alerts/settings`, `PUT /api/alerts/settings` – Alert policy (write: fresh 2FA)

### Arbitrage
- `GET /api/arb`, `PUT /api/arb` – Scanner state and settings: log-only, demo-execute, caps (write: fresh 2FA)
- `GET /api/arb/opportunities` – Recent opportunities and what the risk gate did with each

### Analytics, periods and money
- `GET /api/analytics/summary`, `/api/analytics/ledger`, `/api/analytics/scorecard`, `/api/analytics/trades`, `/api/analytics/my-trades`, `/api/analytics/trades.csv` –
  Results, fills and fill quality, with CSV export
- `GET`/`PUT /api/trades/{fill_id}/note` – Per-trade notes
- `GET /api/periods`, `POST /api/periods` (fresh 2FA), `GET /api/periods/{id}/summary` – Named evaluation periods
- `POST /api/cash-flows`, `DELETE /api/cash-flows/{id}` – Record deposits and withdrawals (fresh 2FA)
- `GET /api/pnl/by-source` – Realized P&L split by strategy
- `GET /api/account/positions` – Read-only view of open positions and resting orders

### Shadow, activity and health
- `GET /api/shadow/summary`, `GET /api/shadow/episodes` – Shadow estimate and its episodes, period-aware
- `GET /api/activity`, `/api/history`, `/api/since` – Activity feed, history, "since you last checked"
- `GET /api/health`, `/api/logs`, `/api/audit` – Health, redacted logs, audit trail
- `GET /api/strategies`, `/api/changelog` – Strategy registry for labels, release notes

### Alerts and push
- `GET /api/alerts` – Alert history
- `GET /api/push/public-key`, `POST`/`DELETE /api/push/subscribe`, `GET`/`DELETE /api/push/subscriptions`,
  `POST /api/push/test` – Web Push device management

### Release and system
- `POST /api/release/pending`, `GET /api/release/pending/{id}` – CI registers a pending release and polls it
  (`X-Release-Token`)
- `GET /api/release/pending` – I see the latest pending release
- `POST /api/release/pending/{id}/approve`, `/reject` – My approval for a release (fresh 2FA)
- `GET /api/deploy/status` – Running version and handover state (`X-Deploy-Status-Key`, used by the release workflow)
- `GET /healthz` – Liveness, version handshake and lease role; no secrets, no `/api` prefix

## How It Works

### A pass
1. The runner wakes on its timer or on an external tick and checks that it holds the leader lease and is not stopped.
2. It reads account state, calls the installed strategy for the quotes it wants, and applies the shared risk gate:
   caps on total exposure, per-category exposure, and per-scan and per-day spend, failing closed on unreadable inputs.
3. It places or cancels orders, writes the journal and a heartbeat, and sends alerts for anything notable. The order step
   has a hard time limit (25 s by default); a pass that hits it is recorded as `pass_timeout`, and the alert is sent only
   when two passes in a row time out.

### Stop
Stop sets in-memory kill flags first, then cancels resting orders, then persists the stopped state. It runs on its own
executor, so a slow database or exchange cannot delay it, and it works with no network. Its store reads and its response
wait are cut to about half a second, Analytics builds pause while it runs, and it is never rate limited.

### Release with a lease handover
The release workflow registers a pending release, waits for my approval with a fresh 2FA code, passes a safety gate,
then the new instance takes over leadership from the old one through a signed handover with fencing. The old instance
stops trading before the new one starts, so the two never trade together.

### Shadow
Each cycle Shadow asks the installed strategy what it would quote on public market data, tracks those hypothetical
bids as episodes, reads the public trades to estimate fills under pessimistic, middle and optimistic queue
assumptions, and scores them when the market settles.

## Safety Model

- **Stop** is a single authenticated tap with no 2FA. It cancels resting orders, sets kill switches and persists the
  stopped state; it keeps working if the database is down.
- **Start, live mode and settings changes** require a fresh TOTP 2FA code. Live mode also needs a typed confirmation
  and a server-side environment gate.
- **Live is never restored automatically** after a restart: the runner comes back in demo and alerts me.
- **Circuit breaker:** a drawdown or daily-loss limit runs the normal full Stop, persists the trip and sends an urgent
  alert; it survives restarts and needs fresh 2FA plus an acknowledgement to clear.
- **Rate limits** are shared through the store with keyed-hash counters, answer 429 and never block Stop.
- **Live refused without the real strategy:** while the public stand-in is installed, the API rejects live mode with
  `strategy_not_installed`.
- **Account-level risk gate** caps total exposure, per-category exposure and per-scan and per-day spend, and fails
  closed on missing or unreadable inputs.
- **Single leader lease** with signed handover and fencing, so an old and a new instance never trade together during
  a release.
- **Validated settings:** every write passes the strategy's validator plus API ceilings and is saved atomically.
- **Secrets** live only in Render environment variables, never in git, logs, the database or API responses.
- **Emergency path without the app:** suspend the Render service, or set `RUNNER_ENABLED=false`.

## The Strategy Is Private

The trading strategy is private and installed at deploy time. This repository ships a placeholder `kalshi_bot/` with
the same interface (a generic Kalshi client plus a demo strategy that places no orders), so the app, the API and every
test run without it. The build-time overlay mechanism is described in `kalshi_bot/README.md` and
`deploy/private_overlay.md`.

## Environment Variables

All variables are listed with placeholders in `deploy/env.example`; real values go only in Render's environment (or
`/etc/kalshi/bot.env` on the optional VM path). The main groups:

- **Storage:** `STORE`, `MONGODB_URI`, `MONGODB_DB`
- **Auth:** `JWT_SECRET`, `TOTP_ENC_KEY`, `ALLOWED_ORIGIN`, `DEV_MODE`
- **Scheduler and alerts:** `TICK_TOKEN`, `CRONJOB_API_KEY`, `PUBLIC_URL`, `NTFY_TOPIC`, `NTFY_SERVER`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (Web Push, see `docs/DEPLOY.md`)
- **Runner limits:** `RUNNER_ENABLED`, `MAX_RUN_BUDGET`, `MAX_OPEN_FRAC`, `MAX_POLL_MINUTES`, `MAX_ARB_CONTRACTS`, `PASS_BUDGET_S` (order-step time limit, default 25)
- **Kalshi:** `KALSHI_DEMO_*` and `KALSHI_PROD_*` key ids and private keys, `KALSHI_LIVE_CONFIRM`
- **Release:** `APP_VERSION`, `HANDOVER_HMAC_KEY`, `DEPLOY_STATUS_KEY`, `RELEASE_TOKEN`
- **Monitoring:** `HEALTHCHECKS_PING_URL` (optional dead-man switch, see `docs/MONITORING.md`)
- **Shadow and overlay:** `SHADOW_ENABLED`, `PRIVATE_REPO_TOKEN`, `PRIVATE_REPO_REF` (set to a commit SHA for each release)

Generate the secrets with `python -m control_api.auth.bootstrap --gen-keys`. Never commit real values.

## Tests

```bash
python -m pytest -q -n auto control_api/tests
cd kalshi_bot && python -m pytest -q tests
cd control_app && npm ci && npm test && npm run build && npm run e2e
```

Tests are parallel-safe, use fakes and mocks only, and never call Kalshi, Atlas, cron-job.org or ntfy. Slower tests are
marked `slow`; skip them while iterating with `-m "not slow"`, but run everything before pushing.

## CI and Releases

- **tests** workflow: Python and frontend suites, end-to-end tests and a commit-message check on every push and pull
  request. CI runs only against the stand-in.
- **semgrep** and **gitleaks** workflows scan for code and secret issues.
- **release** workflow: manual, needs my approval and a fresh 2FA code, passes a pre-deploy safety gate, then hands
  leadership from the old instance to the new one.

## Documentation

| Doc | What it covers |
|---|---|
| `docs/DEPLOY.md` | Step-by-step runbook: Atlas, secrets, Render, Vercel, cron-job.org, ntfy, first demo run, updating, rollback, troubleshooting |
| `docs/ARCHITECTURE.md` | System diagram and data flows, including Stop, the circuit breaker, the pass time limit and the caches |
| `docs/API.md` | Request and response details for the periods, cash-flow, circuit breaker and Stop/rate-limit/cache behaviour; the endpoint list above covers the rest |
| `docs/MONITORING.md`, `docs/BACKUP_RESTORE.md` | External monitoring setup, weekly backup, restore and rebuild drills |
| `docs/load-tests/README.md` | The load and contention test suite and how to run it |
| `docs/PHONE.md` | Installing the PWA and what each screen does |
| `docs/RELEASE_PROCESS.md` | CI/CD, release workflow (only the tip of `main` can be released), lease handover, compatibility rules |
| `docs/SHADOW_CONTRACT.md`, `docs/ANALYTICS_CONTRACT.md` | Design and interface of Shadow mode, and what the analytics endpoints return |
| `deploy/*.md` | Per-service setup notes (Atlas, Vercel, cron-job.org, release secrets, private overlay, optional VM) |
| `CONTRIBUTING.md`, `CHANGELOG.md` | Commit conventions and release notes |

## Contributing

Commits follow Conventional Commits, one logical change each, as described in `CONTRIBUTING.md`. A local git hook
checks the format; install it with `scripts/install_hooks.sh`. Never commit secrets or private strategy files.

## License

Released under the MIT License. Copyright (c) 2026 Caleb Abrantes. See `LICENSE`.

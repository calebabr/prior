# Architecture

```
 Phone (PWA) --static files--> Vercel (control_app)
     |
     | HTTPS + Bearer JWT (+ X-2FA-Code for sensitive actions)
     v
 Render free web service "kalshi-control"
   FastAPI control_api  +  runner (background loop, runs kalshi_bot/maker_strategy)
     |            |               ^                ^
     |            |               | POST /api/tick | self-ping /healthz
     |            v               |                |
     |        Kalshi API      cron-job.org     (runner itself)
     v        (demo or prod)
 MongoDB Atlas M0  (users, config, journal, audit, snapshots, desired state)
 ntfy.sh  <-- alerts from the runner
```

The Kalshi key lives only in Render's environment. The phone never talks to Kalshi.

## Flows

**Status.** The app polls `/api/status` every 30 s. The API combines in-memory runner state with Atlas (heartbeat, account snapshot, P&L); Atlas calls are time-limited. If Atlas is unreachable, every authenticated endpoint except Stop returns 503 `store_unavailable` (the app shows a database-down banner).

**Stop (maker and arbitrage).** `POST /api/bot/stop` needs only a valid token (no 2FA). Auth uses `current_user_stop`, which fails open if Atlas is down or slow: its two store reads (revocation, session epoch) are cut at 0.5 s, so a degraded Atlas delays the flags by at most that. Stop is never rate limited. The runner's sequence:
1. Bump the stop counter (`_stop_gen`) and hold `_stopping`; this is the first statement, so Stop wins any race with Start or an arb execution.
2. Set in-memory flags: maker `desired=stopped`, `arb_desired=stopped`, `arb_mode=demo`.
3. Write both kill switches (`MAKER_KILL_SWITCH`, `ARB_KILL_SWITCH`; an existing legging record is kept).
4. Cancel resting `mm-` orders on Kalshi (a second cancel runs after any pass that was in flight, S-4).
5. One atomic `set_state(desired="stopped", arb_desired="stopped", arb_mode="demo")` to Atlas, retried in the background until persisted (S-3).
It never raises. Start and arb-enable refuse with 409 `stop_in_progress` while Stop runs, and with `stopped_during_start` if a Stop landed during their store write (they re-assert stopped). Emergency path without the app: Suspend the Render service, or `RUNNER_ENABLED=false` (hard stop, S-10: Stop runs at startup and tick, run-once, Start and arb are refused).

**Start.** `POST /api/bot/start` needs a fresh 2FA code. The desired state is written to Atlas first; if that fails nothing starts (fail-safe). Then the kill switch is removed and desired=running. Live mode additionally needs the `LIVE` confirmation and the server-side env gate (`KALSHI_LIVE_CONFIRM` plus prod keys). Stop is always one tap away.

**Config change.** `PUT /api/config` (2FA) merges the update, runs `maker_strategy.validate_config` plus API ceilings (`MAX_RUN_BUDGET`, `MAX_OPEN_FRAC`, `MAX_POLL_MINUTES`), saves a versioned copy to Atlas, writes the config file atomically, and the runner applies it. `cells` can never be changed.

**Keep-awake.** Render free sleeps after 15 min without inbound requests. Two mechanisms: the runner pings its own `/healthz` every `keepalive_minutes`, and a cron-job.org job calls `POST /api/tick` every `pinger_minutes` (the backup that wakes a sleeping service). The job is created/updated through the cron-job.org API when Server settings change or Sync is pressed. `/api/tick` returns immediately and runs the pass in the background (cron-job.org times out at 30 s).

**Restart rehydration.** Render's disk is ephemeral and the service restarts. On startup the runner reloads config, desired state, journal and cloud settings from Atlas and only trades if the stored desired state is running. **LIVE is never restored (S-21):** if the stored `mode` or `arb_mode` is live, the runner comes back in demo, persists that, and sends an urgent ntfy alert ("LIVE not restored: re-arm from the phone"); resuming `running` in demo or arbitrage log-only also sends an urgent alert naming `updated_by`. I re-arm Live with 2FA and typed LIVE. See SECURITY_REVIEW S-3 for the residual risk (kill-switch file is lost on restart; Atlas state is the source of truth).

**Kill switches on Render's ephemeral disk.** `MAKER_KILL_SWITCH`, `ARB_KILL_SWITCH`, the logs and `maker_config.json` live on a disk wiped on every restart or deploy, so they are not durable. Durability comes from Atlas: Stop persists `desired`/`arb_desired` (retried until written), each arb scan that sees the arb kill file persists `arb_desired=stopped`, and a monitor-created kill switch triggers a persisted Stop. Config read back from Atlas at startup is re-validated (`validate_config` + API ceilings, S-12); if it fails, the runner stays stopped and alerts. The only remaining window: Atlas down at Stop plus a restart before the retry succeeds.

**Session epoch (S-15).** The owner's user doc can hold `sessions_valid_after`. A token is valid only if its `iat` is later. `POST /api/auth/logout-all` and bootstrap (create, `OVERWRITE`, `--reset-2fa`, `--revoke-sessions`) set it. Each process caches it for 10 s, so other instances pick it up within about 10 s; the cache never moves backwards.

**Arbitrage loop.** `Runner._arb_loop` calls `arb_scanner.scan_once` every `arb_poll_seconds` (5-60) with `client=None`, so the scanner can never trade by itself. Opportunities are logged to Atlas (`arb_opportunities`, repeats within 120 s folded, 45-day TTL). Log-only is the default. Live execution goes through the runner's gated path (calling the unchanged `arb_scanner.execute`), and every gate is re-checked just before each set:
- arb-live switch on (2FA + typed `LIVE`), prod keys, `KALSHI_LIVE_CONFIRM`;
- no Stop since the scan (stop counter), no kill switch, `RUNNER_ENABLED` true;
- set cost <= fresh cash x min(`max_open_frac`, `MAX_OPEN_FRAC`);
- `arb_max_contracts` <= `MAX_ARB_CONTRACTS`.
A partial fill or an order error mid-set trips `ARB_KILL_SWITCH` ("legging"); the loop persists `arb_desired=stopped`, sends an urgent alert, and re-enabling needs `ack_legging`. Stop cannot interrupt a set already executing (cutting it off would itself create legging risk). A NO-arb holds its legs until the event settles, so capital is locked until settlement, and arb positions reduce maker headroom.

**Account risk gate (arbitrage).** Every arbitrage execution, live or demo, passes `services/riskgate.check_arb` just before sending. `ceiling = max_open_frac x (cash + open exposure)`. A set is refused if: total exposure (maker + arbitrage) plus its cost exceeds the ceiling; the series' exposure plus its cost exceeds `arb_series_cap_frac` of the ceiling; this scan's spend exceeds `arb_scan_cap`; or today's (UTC) spend exceeds `arb_day_cap`. The gate fails closed: unreadable positions, unknown day spend, or any missing or non-finite input refuses the set. A refusal is logged as `executed = "skipped: gate <reason>"` and shown on the Arbitrage page. Day spend is kept in memory and, after a restart or leadership change, rebuilt from the store's executed arbitrage opportunities for the current UTC day (a server-side sum with no row limit); it never goes below what memory already holds. Repeated identical `skipped:` results are stored at most once per event and reason per hour. The gate also refuses a set when its event already holds an executed (FILLED, PARTIAL or ERROR) set of the opposite kind for the same execution kind (Kalshi nets opposite YES/NO positions into $1 pairs, so the second set would pay fees for positions that hedge each other): `skipped: gate opposite set held on event`. Always on; same-kind repeats stay under the caps. The held sets are kept in memory and rebuilt from the store's executed opportunities since 00:00 UTC yesterday after a restart or leadership change; an unreadable or truncated read refuses (`skipped: gate held sets unknown`). With `arb_complete_legs` on (default off, fresh 2FA, demo-execute only, never live), a partial set gets one attempt to buy its missing legs at the current price, only if the whole set still cannot lose money and the gate allows the extra spend (re-checked with fresh balance and positions, reserved on top of the set); otherwise the legging kill switch trips as before. A positions read the exchange marks `truncated` refuses the set (`skipped: gate positions truncated`) and any completion. A scan abandoned at its timeout starts no further set and no completion (`skipped: scan abandoned`). The strategy applies its own concentration limit on the maker side.

| Arb setting | Default | Range |
|---|---|---|
| `arb_scan_cap` ($ per scan) | 15 | 1-100 |
| `arb_day_cap` ($ per UTC day) | 45 | 1-500 |
| `arb_series_cap_frac` (share of the ceiling, arbitrage only) | 0.35 | 0.05-0.9 |
| `arb_demo_execute` | off | on/off |

All four change only with fresh 2FA.

**Arbitrage demo-execute.** With `arb_demo_execute` on, arbitrage running, `arb_mode` demo and the maker mode demo, sets that pass the gate are executed on the Kalshi DEMO exchange (client from `kalshi_factory("demo")`); scans read demo books. Opportunity docs carry `mode = "demo-execute"`, and the Arbitrage page and P&L label them demo (`pnl_demo_execute`), never mixed into live figures. Hard refusals, each re-checked immediately before sending and under the budget lock: never when the maker mode is live; never unless the client's base URL is a known demo URL (checked on the client, not only the mode name); never with live keys (a demo key id equal to the production key id is refused); never when stopped, stopping, shutting down, after a Stop since the scan, not the leader, or with the arb kill switch present. The legging kill switch is unchanged. The setting lives with the arb settings (versioned with the config), not in the state doc, because it never touches real money.

**Order-error alerts.** After each trading pass the runner reads the strategy's write report. If any cancel or place failed it sends ONE grouped alert ("Order errors: N cancel, M place", high priority), at most one per 30 minutes; suppressed repeats are counted into the next alert. A "trading paused" report from Kalshi gets its own alert at most once an hour. The pass itself still counts as ok. Alert text holds counts and mode only. The latest report is in `/api/status` as `write_report`.

**Start reloads config.** `POST /api/bot/start` re-reads and re-validates the stored config and clears a stale invalid-config error before setting desired=running. If the config is still invalid, Start returns 409 `invalid_config: <reasons>` and the bot stays stopped. Stop never reads config and is unaffected.

**Shadow retry.** If a shadow cycle is skipped for a transient reason (not leader yet, lock busy, upstream read error), the loop retries within 60 s instead of waiting a full interval; a completed cycle sleeps the full interval.

**Lease expiry.** The lease expiry shown by status follows each renew (it previously showed "due now").

**Periods and cash flows.** Collections `periods` and `cash_flows` (see `control_api/db/README.md`). Period 1 ("Demo Period 1", id `p1`) is implicit: it starts at the beginning of data and ends at the first marker; each marker starts the next period. Money in or out is recorded as a cash-flow event beside P&L, never netted into it. Attribution is by FILL date: a fill in Period 1 that settles in Period 2 counts toward Period 1, and open positions stay with the period of their fill. Period reads are filters only; Stop never touches them. Store methods: `add_period`, `list_periods`, `add_cash_flow`, `list_cash_flows(since, until)`, `delete_cash_flow`, plus `arb_spend_since` (server-side sum of executed arbitrage spend).

**Live gates snapshot (S-19).** Whether production keys and `KALSHI_LIVE_CONFIRM` are present is read once at startup into `Settings.prod_keys_present` / `live_confirm_ok`. `Runner.live_allowed()` reads only that snapshot, so a demo pass (which temporarily swaps env variables) can never make the gate look open. Arb execution re-checks it immediately before `arb_scanner.execute`. Changing the env on Render restarts the service, so the snapshot cannot go stale.

**Fill quality.** After each pass's fills sync, `Runner.evaluate_fill_quality()` scores up to 8 maker fills (65 minutes to 3 days old) using the public `GET /markets/trades` endpoint (no keys, host fixed by environment, at most 24 calls per pass, under a total time budget). Results (`trade_through`, `move_60m_c`) are stored on the fill doc via `set_fill_fields`, and the aggregate (cached per pass) is returned in `/api/status` and `/api/markets` next to the optional market baseline (from the private benchmark file; null without it). Read-only, never raises, and not on the Stop path.

**Fills sync and P&L.** After each pass that ran the strategy, the runner copies Kalshi fills and settlements into Atlas (`fills`, `settlements`; at most every 30 min; first sync covers 35 days, later ones 2 days; read-only, never raises). `status.pnl` = realised maker (non-taker) fill result vs settlement, windowed by settlement time: `count x (win - price) - fee`. Arb P&L is the sum of `profit` over FILLED arb records (guaranteed profit at execution, not settlement-reconciled).

**Analytics (read-only).** `GET /api/analytics/summary`, `/trades` and `/trades.csv` (`routes/analytics.py`, math in `services/analytics.py`) never write and never touch Kalshi.
- *One build at a time.* A process-wide gate lets one build run (a store read plus the summary and all trade rows for one `(env, days)`). Identical concurrent requests share the result (single flight); a request for another key waits up to 8 s, then gets 429 `analytics_busy`. This keeps the 512 MB Render instance and Atlas M0 safe (S-28).
- *Own thread.* Builds run on a dedicated one-thread executor, not asyncio's default pool, so Stop's cancel never queues behind them. Measured with 8 concurrent cap-sized requests: Stop about 1 s, memory +44 MB.
- *Caches.* Summary and trade rows are cached 60 s per `(env, days)` (rows: at most 2 sets), so paging and filtering `trades` never re-read Atlas. Failed builds are never cached. The journal is folded into a persistent `order_id -> cell` map, refreshed incrementally (newest seen minus 6 h) and rebuilt every 6 h (cap 60,000 ids).
- *Bounded reads with projections.* Each of fills, settlements, quote episodes, journal (`quote` rows only) and arbitrage opportunities is read at most 20,000 documents, newest first. `list_settlements` and `list_journal` accept an optional `fields=` projection (MemoryStore and MongoStore alike); the default is unchanged. No new collections or indexes.
- *Expectations loader.* `load_expectations()` reads the optional private per-cell benchmark from `CELL_EXPECTATIONS_PATH` if set, else `control_api/data/cell_expectations.json`. A missing or invalid file gives `expectations.loaded:false` and no benchmark (null ROI). Contents are never logged. The file is private: supplied as a Render secret file or by the overlay, never committed (S-32).
- *Statistics.* Wilson per-fill intervals plus ticker-clustered (cluster-robust, deterministic) intervals for win rate, edge and ROI; verdict rule needs 20+ markets.
- *Rate limits.* Per user, own buckets on the auth limiter: 30/min summary, 60/min trades and CSV.

**Rate limits (all buckets).** Two layers must both allow a request: an in-memory sliding window per process, and a store-backed counter in the `rate_limits` collection (hashed keys, fixed windows with a sliding estimate, TTL on `expires_at`), so a bucket survives a restart and is shared by every instance. Over the limit is always 429 `rate_limited` with `Retry-After`. If the counter store errors or takes over 0.5 s, the limiter falls back to the in-memory layer alone for 30 s (fail open, never weaker than before). Login, 2FA and step-up keep their closed behaviour: the account lockout is unchanged and those routes still return 503 without the store. Stop never touches the limiter.

**Close-time recording (`services/closetime.py`, runner).** The market's close time makes "time left" exact in Analytics. It is stored as an aware UTC `close_time` on quote episodes (set once at insert, immutable), fills (`set_fill_fields`) and settlements (`set_settlement_fields`). Sources, cheapest first: values already in hand (the doc, the store, the arbitrage scan's market rows via `RecordingPublic`), then one cached public `GET /markets/{ticker}` per `(env, ticker)`.
- *Cache:* thread-safe LRU of 2,048 entries; a known value lives 6 h, a failure 15 min (no retry every pass).
- *Budget:* at most 6 lookups (5 s timeout) when a new episode is inserted; backfill after each strategy pass is single-flight, at most 10 lookups 0.1 s apart, 200 writes and 20 s, covering open episodes and the last 7 days of fills and settlements. Typical steady state: 0 calls; worst case 16 per pass.
- *Isolation (S-31):* lookups run on their own 2-thread executor with an unauthenticated client (`max_retries=0`, 5 s timeout). Stop takes no lock these tasks hold and never waits for them (a test proves Stop returns under 1 s while lookups hang). Tickers must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` before use in a URL. Every failure is counted and cached as a miss; nothing raises.
- *Store methods added:* `set_quote_episode_close_time(order_id, dt)` (writes only while missing), `set_settlement_fields(ticker, fields)`, `list_monitor_summaries(since, limit)`.

**Local data, history and schedule.** `pull_data` (run on the PC, read-only toward Atlas) writes `local_data/` plus an append-only `local_data/history/<name>.csv` for fills, fill_pnl, settlements, journal, quote_episodes, arb_opportunities, account_snapshots and heartbeats (deduplicated by natural id, newest pull wins, never shortened), and dated snapshots of `SUMMARY.md` and `analytics_summary.json` in `local_data/archive/YYYY-MM-DD/` (newest 90 kept). It never reads `users` or `revoked_tokens` and scrubs secrets from every value. `scripts/schedule_pull_data.ps1` registers a daily Windows Task Scheduler job (current user, no stored password, only while logged in, catches up after a missed run).

**Phone app additions.** The Analytics page cancels superseded requests (AbortController), debounces filters 300 ms and loads trades only after the summary settles. A React error boundary wraps the page area only, so a crashing page shows "This page could not be shown" while the header Stop button and navigation keep working (S-29). CORS now exposes `X-Truncated`, `Content-Disposition` and `Retry-After` to the allowed origin.

**Frontend layout and charts (2026-10-13).** `PageTitle` renders a sticky bar as the first child of `<main>` (z-index 15: below the header 20, tab bar 30 and modals 50, so Stop is never covered). It publishes `--appbar-h` and `--sticky-top` (header + bar while shown, header alone when auto-hidden by `useScrollIdle`) on `:root`; sticky elements such as the Terminology filter use `top: var(--sticky-top)`. Titles come from `utils/routes.js` and also set `document.title`. Terminology search is `utils/search.js` `rankTerms` (pure, tiered, alias-aware). Every chart draws through `ChartFrame` (HTML tick labels and gridlines around a stretched SVG, `utils/chart.js` helpers, `styles/charts.css`); tick text is `aria-hidden` and the chart's `aria-label` carries units and ranges.

**Test infrastructure.** API tests run in parallel with pytest-xdist (`-n auto`); tests over about 1 s carry `@pytest.mark.slow`, so `-m "not slow"` is a roughly 12 s quick run and the full run (15-17 s on 22 cores) is the pre-commit and CI gate. Workers are separate processes with tmp-copied bot files and in-process ASGI transports, so there are no shared ports, paths or caches. Frontend: Vitest, then `npm run build`, then Playwright (`e2e` mock, `e2e:real` local).

# API additions (Sprint 2A-1)

All routes need a normal login; every POST/DELETE below needs fresh 2FA and is audited. Stop is unaffected by any of them.

## Periods and cash flows
| Route | Notes |
|---|---|
| `GET /api/periods` | `{periods:[{id,name,started_at,ended_at,note,current,implicit}], cash_flows:[{id,ts,kind,amount,note,created_by}]}`, oldest first. `p1` is implicit (`started_at` null). |
| `POST /api/periods` `{name (1-60), note? (<=200)}` | Marker starting now. 409 `period_too_soon` if the last marker is under 5 min old. Audit `period.create`. |
| `POST /api/cash-flows` `{kind: deposit or withdrawal, amount (0<x<=100000), ts, note?, confirm_duplicate?}` | `ts` is required and may not be in the future; validation errors are 422 (`detail` is a list of messages). Same kind, amount and UTC date as an existing flow gives 409 unless `confirm_duplicate:true`. Success adds `created` and non-blocking `warnings:[{code,message}]` (`date_in_current_period`, `no_balance_jump`). Fresh 2FA. Audit `cash_flow.create`. |
| `DELETE /api/cash-flows/{id}` | 404 `cash_flow_not_found`. Audit `cash_flow.delete`. |
| `GET /api/periods/{id or current}/summary?env=&source=` | Fills, settled, wins, P&L, money spent, `return_on_settled_cost`, `return_on_all_cost`, open at-risk, and a `cash_flows` block (deposits, withdrawals, equity start/end, `return_on_capital`; never netted into P&L). |
| `GET /api/pnl/by-source?period=&env=` | P&L per source for a period. |

The write routes return the same body as `GET /api/periods` plus `created` or `deleted`. Errors: 400 (validation messages), 409, 422 (unknown period), 503 `store_unavailable`.

## Period filter
`period=<id>|current|all` (default `all`, response unchanged) on `/api/analytics/summary`, `/trades`, `/trades.csv`, `/my-trades`, `/api/activity`, `/api/history`, `/api/pnl/by-source`. Attribution is by fill date. With a period, `days` is replaced by the smallest allowed value reaching the period start. `/api/status` `pnl` is period-aware only with `?period=`; `/api/since` is not.

## Arbitrage
`GET/PUT /api/arb`: settings gain `arb_scan_cap` (15), `arb_day_cap` (45), `arb_series_cap_frac` (0.35), `arb_demo_execute` (false, schema type `boolean`); `ceilings` carries their maxima; `status.demo_execute`. `pnl` is live only; `pnl_demo_execute` `{d7,d30,sets,demo:true}` is separate. Opportunity `mode` is `live`, `demo-execute` or `log-only`; gate refusals read `skipped: gate <reason>`. Changing any of these needs fresh 2FA.

## P&L scopes and Today's boundary (`GET /api/status`, additive)
- `tz=<IANA name>` (optional query): Today's P&L (`pnl_today`) counts markets settled since the viewer's local midnight. The name is checked against the tz database (letters, digits, `_+-/`, at most 64 characters); anything unknown, malformed or oversized silently means UTC (never an error). Storage, settlements, the journal and reconciliation stay UTC.
- `pnl_today_tz`: the zone actually used (`"UTC"` after a fallback). `pnl_today_since`: the UTC instant of that local midnight. `pnl_today` fields stay `null` when nothing settled in the window; a real net of zero is `0.0`. Cached 60 s per (mode, zone), at most 16 entries.
- `pnl_arb`: the arbitrage block in the same shape as `pnl` (`{d7,d30,settled_fills}` by settlement time; period-aware like `pnl`). `pnl_total`: `{d7,d30}` = maker + arbitrage, same date rule. Both `null` when nothing settled. `pnl` is unchanged (maker only).
- What each figure measures (the app prints these under the numbers): Overview Maker P&L = realized maker fills by settlement date (UTC); Today = realized, bot and manual, settled since local midnight; Analytics totals = maker + arbitrage by settlement date (UTC); period summary = by fill date (UTC); Arbitrage `pnl` = guaranteed profit of filled sets by execution time, not realized.

## Analytics additions (UI-1a)
- `GET /api/analytics/summary?tz=<IANA>` (also `/ledger`, `/my-trades`, `/api/arb`): `tz_used` is the validated zone or `UTC` (invalid names fall back, never an error). The P&L curve, arbitrage daily rows and "today" use local dates; windows stay UTC. Cache keys use the validated name only and are bounded (64, oldest evicted).
- `win_rate_by_strategy`: `maker` (unit `fill`) and `arb` (unit `set`: executed sets, all filled legs settled, win = set P&L > 0), each `{n, wins, losses, win_rate, ci_low, ci_high, method:"wilson95"}`. `totals.win_rate` pools both and is deprecated (listed under `deprecated`).
- `arb_summary` (summary and `GET /api/arb`): `today` and `period` blocks (`sets`, `events`, `sightings`, `by_outcome`, `skipped_by_rule`), `per_set` (null on `/api/arb` if the dataset is unavailable), `since_restart` (in-memory runner counters), `notes`. `/api/arb` may wait up to 3 s for the dataset.
- `arb_sets` (newest first, max 100) and `arb_sets_total`: one row per executed set with `status` PARTIAL, FILLED, OPEN or SETTLED, guaranteed and realized profit, `legs[]` and `notes`.
- `GET /api/analytics/ledger?days=&env=&period=&tz=`: signed in, no 2FA, read-only, 20/min/user. Per-strategy rows (`maker`, `arb`, `you`), `total`, `daily` (by fill day), `daily_by_settlement`, `settled_by_strategy` (list of `maker`, `arb`, `you`?, `total` with `settled_fills`, `wins`, `losses`, `realized_pnl`; every row settled inside the window whatever its fill date, not capped; `settled_attribution` names the rule), informational `netting`, and `reconciliation` (`residual`, `tolerance` 0.10, `flag` when the absolute residual exceeds it, equity start and end, net flows, notes). Fewer than 2 snapshots gives nulls plus notes.
- `GET /api/analytics/returns?granularity=day|week|month&days=1..180&tz=&env=`: signed in, no 2FA, read-only, 20/min/user (bucket shared with the drill-down). Realized P&L of bot fills by SETTLEMENT date, per local calendar day / Monday-start week / month in `tz_used`, with `by_strategy` (`maker`, `arb`), `settled_fills`, `small_sample` (1-4 settled fills), `return_rate` (a fraction, or null with `return_reason`: `no_start_snapshot`, `stale_start_snapshot`, `base_too_small` (base <= `min_base`), `env_not_current`), the modified-Dietz `base`, `flows_in_period`, `equity_change`, and `you` (counted, not included). Whole periods only: the one straddling the window start is dropped; no whole period gives `periods: []` plus a note. The current period has `partial: true`. 422 on a bad `granularity`, `days` (not 1-180) or `env`. Cached 60 s per (env, days, tz, granularity), at most 24 keys.
- `GET /api/analytics/returns/fills?start=YYYY-MM-DD&end=YYYY-MM-DD&tz=&env=&limit=(1-100)&offset=`: the bot fills settled in those local dates (a period's `start_local` / `end_local`, at most 31 days, within 180 days), newest settlement first.
- `GET /api/analytics/scorecard?days=(7|30|90|180)&period=&env=`: pass rate and durations, churn, fill-to-settlement rate, open past expected settlement, reconciliation, alerts by type, arbitrage per set, stop/start log, indicative return. Values not stored are `null` with a note.
- `GET /api/status` `pnl_today_state`: `settled`, `none_yet` (nothing settled yet, not $0.00) or `unavailable` (store read failed).
- `GET /api/config` `shadow_capital`: `{value, default:150, min:50, max:1000, help, editable, write_via:"PUT /api/cloud", needs_fresh_2fa:true}`. Written with `PUT /api/cloud {shadow_capital}` (fresh 2FA, audited with from/to, whole body validated before any write). Dollar P&L and the ceiling scale with it; rates and ROI do not.

## Other
- `POST /api/bot/start`: 409 `invalid_config: <reasons>` if the reloaded config fails validation (the bot stays stopped). To stop: `POST /api/bot/stop` or the app's Stop button (no 2FA).
- `GET /api/status`: new `write_report` `{cancel, place, paused, series_capped, utc, mode}` or null; `lease_expires_at` follows each renew.

Example: `curl -X POST "$API/api/periods" -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{"name":"Demo Period 2"}'` (include the fresh-2FA proof your session requires).

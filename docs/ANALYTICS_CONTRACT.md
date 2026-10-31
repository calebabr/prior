# Analytics and terminology

What the read-only analytics endpoints return and how the numbers are defined. The code is the reference
(`control_api/services/analytics.py` and `control_api/routes/analytics.py`); this page is the short version.

## Endpoints

All need a signed-in session and no 2FA. They take `days` (7, 30, 90 or 180), `env` (demo or live) and, where it
makes sense, filters for side, kind, status, result, price band, series and source. Bad values return 422.

| Route | Returns |
|---|---|
| `GET /api/analytics/summary` | Totals, the P&L curve with drawdown, results by cell, series, price band and time to close (with confidence intervals), weekly fill quality and the arbitrage summary |
| `GET /api/analytics/ledger` | Per-strategy fills, cost, fees, realized P&L and open cost, daily tables, and a reconciliation of equity change against flows and realized P&L (residual, 0.10 tolerance, flag) |
| `GET /api/analytics/scorecard` | Run health, churn, fill-to-settlement, reconciliation, arbitrage per set and an indicative return; unstored values are null with a note |
| `GET /api/analytics/trades` | A page of trade rows |
| `GET /api/analytics/my-trades` | The same rows for my own manual trades |
| `GET /api/analytics/trades.csv` | A CSV export, capped in size |

## Definitions

- **Settled fill:** a fill whose market has a settlement result of yes or no. P&L is `count x (win - price) - fee`.
- **Kind:** maker (not a taker fill) or taker.
- **Price band:** the price ranges of the installed strategy's cells, derived from the configuration and never
  hard-coded in this repository; anything else is "other".
- **Hours to close:** market close minus fill time. When the close time is only known from the settlement time, it is an
  upper bound.
- **Win rate interval:** Wilson score, 95%. **Edge** is win rate minus average price.
- **Drawdown:** the cumulative P&L minus its running maximum, with the maximum starting at zero.
- **Arbitrage** is judged per set, not per fill, because a set of hedged legs always has losing legs.

## UI-1a additions

- `tz` (IANA name) on the summary and ledger; response `tz_used` (UTC when invalid). Dates are local, windows are UTC. Details in `docs/API.md`.
- `win_rate_by_strategy`: maker per fill, arbitrage per set; the pooled `totals.win_rate` is deprecated.
- `arb_summary`, `arb_sets`, `arb_sets_total`: distinct sets and sightings, and one row per executed set.
- Two attribution rules in the ledger: `strategies`, `total` and `daily` count each fill on its FILL day (`attribution`); `settled_by_strategy` counts P&L on the SETTLEMENT day (`settled_attribution`). Home uses the settlement rule for its headline and its By Strategy rows, so they add up; a fill made before the window can settle inside it, so the two rules differ by design. Without `settled_by_strategy` Home shows a dash.
- Analytics (under Performance) tabs: Ledger (strategy table, totals, reconciliation) and Scorecard (null shows "not tracked yet"; return is labelled demo, indicative only).
- New glossary terms: `win-rate-by-strategy`, `arb-set`, `scans-since-restart`, `risk-rule-outcome`, `ledger`, `scorecard`, `shadow-capital`.
- Shadow: when `ceiling_full` is set, the banner says Shadow is not estimating until positions settle.

## Terminology and help icons

The plain-English definitions live in one place, `control_app/src/utils/glossary.js`, which exports `GLOSSARY` (an
array of `{id, term, short, body, group, related}` with kebab-case ids) and `getTerm(id)`. A help icon such as
`<HelpIcon term="fill-rate" />` links to `/terminology#fill-rate`, and a test fails if any help icon in `src/` names a
term that is missing from the glossary.

## Returns per day, week and month

`GET /api/analytics/returns` and `/returns/fills` (`control_api/services/returns.py`). Read only.

- **Return** = realized P&L of bot fills settled in the period (maker + arbitrage; `you` is excluded and counted in a note) divided by a modified Dietz base: `B = V0 + sum(F_i * w_i)`. `V0` is the equity (cash + exposure) of the last account snapshot at or before the period start; `F_i` is a deposit (+) or withdrawal (-) after that snapshot and up to the period end; `w_i = (end_eff - max(ts_i, start)) / (end_eff - start)` on real elapsed seconds (a 23 or 25 hour DST day is handled), `end_eff = min(period end, now)`. `return_rate` is a fraction (0.0265 = 2.65%).
- **Periods** are local calendar spans in `tz` (day, Monday-start week, month), bucketed by the local date of the settlement time. The current period is `partial` and is never projected. Empty periods are present with zero P&L.
- **Window:** `days` is any value from 1 to 180 (the app asks 7/30/90/180 for days, 84 for weeks, 180 for months). Whole periods only: a period starting before `now - days` is dropped; if none fits, `periods` is empty with a note.
- **Null rate** (`return_reason`): `no_start_snapshot`, `stale_start_snapshot` (the snapshot is over 24 h before the start), `base_too_small` (base <= 10.00), `env_not_current` (`env` is `all` or not the account the snapshots belong to). Dollars are still returned.
- **Small sample:** 1 to 4 settled fills (`small_sample_threshold` 5).
- **Account snapshots carry no env tag** (the runner stores `ts`, `cash`, `exposure`, `headroom`), so they are taken to belong to the account the runner is in now; any other `env` gets `env_not_current`. Snapshots written before a demo/live switch are not distinguishable, so a period straddling a switch can show a wrong base.
- Percentages are not additive across periods; the dollars are.

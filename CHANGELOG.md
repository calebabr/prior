# Changelog

## Unreleased - Ledger pair timing
- Fix: the Analytics reconciliation now books YES / NO pair P&L when the pair forms (as Kalshi credits it) instead of at settlement, so open pair markets no longer show a false residual; new `pair_timing` fields, `model: pair_time_v2`, and pair netting uses average cost. Tolerance unchanged; read-only.

## Unreleased - Returns per period (app)
- Shadow Estimate: when no market carries a maker fee, the page now says the after-fee and zero-fee estimates are the same instead of showing a fee toggle that changes nothing.
- Analytics > Summary: a Returns card with a Day | Week | Month control, stacked Maker and Arbitrage bars per period (dollars and percent, "so far" on the running period, faded bars under 5 fills), a table view and a tap-to-see-fills sheet. Week and Month always show their own window (12 weeks, about 6 months). Read-only; Stop is untouched.

## Unreleased - Returns per period (API)
- New read-only `GET /api/analytics/returns` (day, week, month; modified-Dietz percentage, whole periods only, `days` 1-180) and a per-period fills drill-down `GET /api/analytics/returns/fills`.

## Unreleased - Update prompt
- The app now shows a dismissible "A new version is ready" banner (tap to reload) when a new service worker or a newer deployed bundle is detected. The min_app_version "update available" message is unchanged. Stop and data are never blocked.

## Unreleased - Home rows by settlement date
- Fix: Home By Strategy rows now use settlement-date P&L like the Total P&L headline, so Maker + Arbitrage equal the total. New ledger field `settled_by_strategy`; the Ledger tab shows fill-day and settlement-day P&L side by side.

## Unreleased - UI-1a stage 2: new navigation and readability
- Five tabs on the phone: Home, Strategies, Performance, Activity, More. Desktop sidebar groups the same pages (Strategies: Maker, Arbitrage; Performance: All strategies, Analytics, Shadow Estimate, Periods) and More is grouped into System, You and Help.
- Old links still work: `/markets` opens Maker Controls and `/arb` opens the Arbitrage page (query and hash kept, so notification links keep working).
- Home: one total P&L (Maker + Arbitrage) with a 7 days / 30 days / This period control; your own manual trades are on a separate line and not in the total.
- Strategies hub: one card per strategy. Each strategy page has Overview | Results | Controls; Pause/Resume, settings, Go live and Turn off keep their 2FA and confirmation steps.
- Performance hub: All strategies table, Analytics (with Ledger and Scorecard), Shadow, and Periods (moved from Account, which keeps a link).
- Readability: six type sizes, 40 px minimum tap targets, one set of number and date formats, sentence-case labels, stronger control borders.
- Stop is unchanged: one tap, no 2FA, visible on every page and width.

## Unreleased - UI-1a stage 1: honest analytics
- Analytics takes the viewer's time zone (`tz`, UTC fallback, `tz_used` returned); win rate is shown per strategy (maker per fill, arbitrage per set) and the pooled rate is deprecated.
- Arbitrage: distinct sets and sightings, scans since restart, and an executed-sets list. New Ledger and Scorecard tabs in Analytics with a reconciliation residual (tolerance 0.10).
- Cash flows now require a date (422 on bad input) and refuse duplicates (409) unless confirmed. `pnl_today_state` separates "none yet" from "unavailable".
- Shadow capital is a setting (default 150, 50 to 1000, fresh 2FA); the Shadow banner says it is not estimating when the ceiling is full.
- API (additive): `tz_used`, `win_rate_by_strategy`, `arb_summary`, `arb_sets`, `arb_sets_total`, `/api/analytics/ledger`, `/api/analytics/scorecard`.

## Unreleased - rename to Prior
- The phone app is now called Prior, with a new logo (a ring holding a wide prior curve and a narrow posterior curve) in all three themes. Page titles, notifications and the install name say Prior.
- Saved theme, text size and last-visit preferences carry over from the old name.

## Unreleased - honest P&L labels
- Overview: the 30-day tile is now "Maker P&L (30 days)" with a combined Maker + Arbitrage total and a Maker / Arbitrage split; every P&L figure on Overview, Analytics, Account, Arbitrage and Shadow carries a one-line scope (what it measures, date rule) from one registry, `utils/pnlScopes.js`.
- Today's P&L starts at the viewer's local midnight (`GET /api/status?tz=`, UTC fallback for any unknown name) and says "No settlements yet today" instead of a dash; `$0.00` means settled and netted to zero.
- Arbitrage: realized profit beside guaranteed profit, with a note on why they differ. Shadow: "No estimate yet" instead of a zeroed headline.
- API (additive): `pnl_arb`, `pnl_total`, `pnl_today_tz`, `pnl_today_since` on `/api/status`.

## Unreleased - release pipeline and CI (Sprint 1)
- `release` workflow: manual, main only. Gate (CI green on the exact commit, running server healthy, no arbitrage set executing, handover-capable or stopped), pending release approved in the app with 2FA (30 minutes), backend deploy and verify, frontend deploy and verify, ntfy, tag and notes.
- Render: auto-deploy off, 120 s shutdown delay, new env names `APP_VERSION`, `HANDOVER_HMAC_KEY`, `DEPLOY_STATUS_KEY`, `RELEASE_TOKEN` (values only in the dashboard).
- CI: semgrep workflow with a reviewed baseline, Dependabot (monthly, grouped), commit-message check in the hook and in CI, PR template with a security checklist, CODEOWNERS, workflow safety tests.
- Scripts: `release_gate.py`, `release_ctl.py`, `changelog.py`, `commit_msg_check.py`, `semgrep_triage.py`, each with tests.

## 0.9.0 - 2026-10-15 (Summary-first UX, unreleased; not yet browser-verified)
- New name and logo: Keel; three dark themes and text size.
- Overview: headline, today's P&L, last 12 runs, Since You Last Checked, P&L and At Risk by source.
- Your own Kalshi trades labelled "You" and kept out of bot stats.
- Markets folded cards; Analytics tabs (Summary, Edge, Execution, Trades); My Trades and notes.
- Terminology search v2 and help popover.
- Alert history, alert settings (daily summary, mode, quiet hours), web push (VAPID, no new dependency).
- Settings change preview and Undo Last Change.
- API: new endpoints and env vars `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` are described in `docs/DEPLOY.md`.

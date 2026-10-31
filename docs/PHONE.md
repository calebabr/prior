# Using the app on your phone

## Install
- **iPhone (Safari):** open the app URL > Share > **Add to Home Screen** > Add.
- **Android (Chrome):** open the URL > menu (three dots) > **Install app** / Add to Home screen.

## Logging in
Username and password, then the 6-digit code from your authenticator app.

**2FA codes work once.** Login uses the current code. The first protected action afterwards (marked with a lock) needs the NEXT code, so wait up to 30 seconds. After 5 wrong codes the account locks for 15 minutes.

## Stop (always available)
The red **Stop** button is in the header on every page. One tap plus one confirmation, no 2FA. It stops the maker **and arbitrage**, cancels resting bot orders and blocks new ones. It works even when the database is down. If the app will not load, see "Emergency stop without the app" in `docs/DEPLOY.md`.

## Pages
Bottom tabs: **Home, Strategies, Performance, Activity, More**. On a wide screen the same pages are in a sidebar. Old links `/markets` (Maker Controls) and `/arb` (Arbitrage) still work.
- **Home:** status and Stop/Start (Start needs 2FA), one total P&L (Maker + Arbitrage) with a 7 days / 30 days / This period control, today's line, a row per strategy, and what changed since your last visit. Manual trades are on their own line and not in the total.
- **Strategies:** a card per strategy. Each strategy page has **Overview** (state, Stop, Turn on/off, latest activity), **Results** (P&L, fills, fill quality, executed sets) and **Controls** (pause or resume a market, mode, settings; all need 2FA, Go live also needs typing LIVE). The strategy cells are read-only.
- **Performance:** All strategies table, **Analytics** (Summary, Edge, Execution, Trades, Ledger, Scorecard), **Shadow Estimate** and **Periods**.
- **Activity:** recent orders and fills, filter chips (All, Maker, Arbitrage, You).
- **More:** System (Health, Server, Mode, Logs), You (Account, Settings, Alerts), Help (Terminology).
- Text uses six sizes, buttons are at least 40 px tall, and labels are sentence case.

Stop is the red button on every page.

If the app looks out of date, close and reopen it, or tap the update banner ("A new version is ready").

If you get "session expired" at login, all sessions were logged out; just log in again.

## Push notifications on iPhone (step by step)
Push only works from the Home Screen app, not from a normal browser tab. iOS 16.4 or newer is needed.
1. Open the app URL in Safari (or Chrome) on the iPhone.
2. Tap the **Share** button (square with an arrow), scroll down, tap **Add to Home Screen**, then **Add**.
3. Close the browser. Open the app from the new Prior icon on your Home Screen and log in.
4. Tap **More** > **Alerts** (or **Settings** > Notifications) and tap **Enable notifications**.
5. iOS asks "Allow notifications?". Tap **Allow**. If you tapped Don't Allow by mistake: iPhone Settings > Notifications > Prior > Allow Notifications, then try again.
6. Tap **Send test**. A notification should arrive within seconds.

**Push Devices card:** More > **Settings**, directly under the Notifications card. It lists every phone or browser registered for alerts (push service name and when it was added). **Clear All** asks you to confirm, then removes them all (login only, no 2FA). Re-enable on each phone afterwards. Use it after a lost phone or when re-testing.

Stop is the red button on every page; last resort, suspend the service in Render, or `stop_maker.ps1` on the desktop.

## Update 2026-10-15 (Prior)
- **Install for notifications:** push only works from the Home Screen app. iPhone: Share > **Add to Home Screen** (Chrome or Safari), open from the icon, then More > Alerts > **Enable notifications** > **Send test**. Android: Chrome menu > Install app, then the same.
- **Tabs (since stage 2):** see Pages above. Home shows the headline, Stop, today's P&L, recent runs and what changed since your last visit. Activity is grouped by day with Bot or You chips.
- **Settings > Display:** theme (three dark) and text size. **Settings:** saving shows what changed; **Undo Last Change** reverts it (needs a 2FA code).
- **Alerts:** history, daily summary time, every fill or problems only, quiet hours (Stop and breaker alerts always come through).
- **Stop** is on the status card on every state; last resort, suspend the service in Render, or `stop_maker.ps1` on the desktop.
- **Returns (Performance > Analytics > Summary):** choose Day, Week or Month under the Returns heading. Each bar is one period's profit (Maker and Arbitrage stacked, above or below the zero line); tap a bar to read it, then Details for the fills behind it. Faded bars have fewer than 5 fills, so treat them as noise. "Show as table" gives the same numbers as a list. A dash instead of a percent means there was no reliable starting balance; the dollars are still right.

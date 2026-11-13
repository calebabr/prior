# External monitoring (free)

The app already watches itself: the runner's silent-alert watchdog (`silent_alert_minutes`, default 30) sends a
"No heartbeat" ntfy alert when the bot is **running** but no pass has finished. That watchdog lives inside the
Render process, so it cannot tell you when the whole service is dead, suspended or crash-looping. Two free
outside watchers cover that gap. Both alert to the same ntfy topic (the topic name stays `keel`; yours is in
`NTFY_TOPIC`).

| Watcher | Free service | Detects | Checks every |
|---|---|---|---|
| Dead-man switch | healthchecks.io | the process stopped sending its "I am alive and passes are fresh" signal (crash, suspend, deploy stuck, Render outage) or sent an explicit fail (bot stalled) | the app pings it once per keep-awake cycle (<= 14 min) |
| Uptime monitor | UptimeRobot (free plan) | `GET /healthz` unreachable or not 200 from outside Render | 5 min |
| Keep-awake (already built) | cron-job.org | not a monitor; keeps Render awake with `POST /api/tick` | `pinger_minutes` (<= 14) |

## Thresholds (all consistent with the keep-awake rule: every interval <= 14 min)

| Setting | Value | Why |
|---|---|---|
| `keepalive_minutes` (self-ping) | 10 (max 14) | Render sleeps after 15 min idle |
| `pinger_minutes` (cron-job.org) | 10 (max 14) | same |
| healthchecks **Period** | 15 min | longer than the 14-min max ping interval, so a normal cycle never looks late |
| healthchecks **Grace** | 10 min | a slow cold start or deploy handover does not page you; alarm at about 25 min of silence |
| UptimeRobot interval | 5 min, alert after 2 failed checks (about 10 min) | one slow cold start (up to about 60 s) does not page you |
| Internal watchdog `silent_alert_minutes` | 30 (min 15) | bot running but passes stalled; independent of the above |

## What each alert means and what to do

| Alert (ntfy / email) | Meaning | What to do |
|---|---|---|
| healthchecks "kalshi-control is DOWN" (no ping for about 25 min) | The service or its keep-awake loop is not running at all. Resting orders already on Kalshi stay there. | Open the Render dashboard: is the service suspended, crashed or mid-deploy? Read the logs. Restart or redeploy. If you cannot fix it quickly, cancel resting orders in Kalshi's own site (see `docs/DEPLOY.md` > Emergency stop). |
| healthchecks "FAIL" signal | The service is alive but the bot is set to run and has produced no pass for longer than `silent_alert_minutes`. Same as the in-app "No heartbeat" alert. | Open the app > Server page: last pass time, runner errors, Kalshi connectivity. Press Stop if unsure. |
| UptimeRobot "api is DOWN" | `/healthz` is not answering from the internet (service asleep and not waking, crashed, DNS, Render incident). | Check <https://status.render.com>, then the Render logs. If only UptimeRobot alerts and healthchecks stays green, the service is up and the problem is the network path or the URL. |
| Both fire | Full outage. | Render dashboard first. |
| Recovery messages ("is UP") | Back to normal. | Confirm one pass in the app, then relax. |
| In-app "Self-ping failed" | The service could not reach its own URL. | Check `PUBLIC_URL` and the Render logs. |

Monitoring never trades and never stops the bot: it only tells you. Stop in the app always works on its own.

## Owner setup

Do these once, after the first deploy works (`docs/DEPLOY.md`). Nothing here costs money. Never paste the ping URL
into chat, git or a screenshot: anyone who has it can fake a healthy signal.

### A. healthchecks.io dead-man switch

1. Go to <https://healthchecks.io> and click **Sign Up**. Use your email, confirm it, turn on 2FA under
   Account Settings > Security.
2. In your project click **Add Check**. Name it `kalshi-control`.
3. Set **Period** = 15 minutes and **Grace Time** = 10 minutes (the Schedule panel, "Simple" tab; type the numbers
   into the boxes). Save.
4. Copy the check's **ping URL** (looks like `https://hc-ping.com/<uuid>`). Keep it in your password manager.
5. Add the ntfy alert: Project > **Integrations** > **Add Integration** > **ntfy**. Enter server `https://ntfy.sh`
   and your topic (the same value as `NTFY_TOPIC`). Tick "Notify when check goes down" and "up". Save, then press
   **Send test** and confirm your phone receives it. (If the ntfy option is not offered, use the **Email**
   integration instead and keep the ntfy app for the in-app alerts.)
6. Open the check > **Integrations** tab and make sure the new ntfy integration is switched on for this check.
7. Render dashboard > `kalshi-control` > **Environment** > add `HEALTHCHECKS_PING_URL` = the ping URL. Save (Render
   redeploys; wait for green). The app sends the pings itself, once after each keep-awake cycle: the plain URL when
   healthy, `<URL>/fail` when the bot is set to run but has produced no pass for `silent_alert_minutes`. A stopped bot
   sends the plain URL, so pressing Stop never raises a false alarm.
8. *Check:* within 15 minutes the check turns green ("up") and shows recent pings. To test the alarm: Render >
   Suspend service for 30 minutes; you must get a DOWN alert. Resume afterwards. (Do this when the bot is stopped.)

Optional read-only API key for the verification script: healthchecks > Project Settings > **API Keys** > create a
**read-only** key. Use it only on your own computer for `scripts/check_monitoring.py`; never put it on Render.

### B. UptimeRobot uptime monitor

1. Go to <https://uptimerobot.com> > **Register for FREE**; confirm your email.
2. **+ New monitor** > Monitor Type **HTTP(s)**. Friendly name `kalshi-control health`. URL
   `https://<your-service>.onrender.com/healthz`. Monitoring interval **5 minutes** (the free plan minimum).
3. Alert contacts: your email is added by default. To also reach ntfy, open **Integrations** (or My Settings >
   Alert Contacts) > Add > **Webhook**, URL `https://ntfy.sh/<your NTFY_TOPIC>`, method POST, body
   `api health: *monitorFriendlyName* is *alertTypeFriendlyName*`. Webhook availability on the free plan can change;
   if it is not offered, email is enough (set up the email to push to your phone).
4. Set "alert after" / confirmation to **2 failed checks** where offered. Create the monitor.
5. *Check:* the monitor shows Up. A cold Render wake-up can take about 60 s; one blip is normal, two in a row is not.

### C. Verify from your computer (optional)

```
set PUBLIC_URL=https://<your-service>.onrender.com
set HEALTHCHECKS_PING_URL=<the ping URL>
set NTFY_TOPIC=<your topic>
set HEALTHCHECKS_API_KEY=<optional read-only key>
python scripts/check_monitoring.py
```

Exit 0 = everything answers; 1 = a check failed; 2 = a variable is missing or malformed. It prints masked values only
and never pings the dead-man URL itself (that would hide a real outage).

## Environment variables

| Variable | Where | Meaning |
|---|---|---|
| `HEALTHCHECKS_PING_URL` | Render (secret, `sync: false`) | dead-man URL; unset = no dead-man pings |
| `HEALTHCHECKS_API_KEY` | your computer only | optional read-only key for `scripts/check_monitoring.py` |
| `PUBLIC_URL` | Render | already used by the self-ping |
| `NTFY_TOPIC` | Render | already used by in-app alerts |

## Limits and caveats

- Free plans change; re-check the limits when you sign up (healthchecks.io free tier: a handful of checks, enough
  for one; UptimeRobot free: 5-minute interval, non-commercial use).
- A secret ping URL means anyone who learns it can keep the dead-man green. Rotate it (edit the check, "Change
  ping URL") if it leaks.
- Monitoring depends on ntfy.sh and your phone: test it once a month (the healthchecks "Send test" button).

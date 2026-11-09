# Deploy runbook (owner)

## Demo-Day Checklist (do these in order)

Demo only: Kalshi **demo** keys, no real money. Keep every secret in a password manager; never paste one into chat, git or a screenshot. **Stop** (the red button in the app) always works; last resort is Render dashboard > `kalshi-control` > Suspend service (or `stop_maker.ps1` on the desktop).

**1. Accounts and 2FA.** Create accounts at GitHub, MongoDB Atlas, Render, Vercel, cron-job.org and ntfy.sh (the app only). Turn on 2FA for each that offers it (GitHub, Atlas, Render, Vercel: Account/Security settings). Save recovery codes in the password manager.
*Check:* you can log out and back in to each one with a 2FA code.

**2. MongoDB Atlas free M0** (details: `deploy/atlas.md`).
1. Atlas > Create > **M0 Free**, AWS, us-east-1, name `kalshi-control`.
2. Security > Database Access > Add user, password auth, role **readWrite** on one database. Use a long random password.
3. Security > Network Access > Add IP Address > **Allow access from anywhere** (`0.0.0.0/0`); Render's free IPs change.
4. Database > Connect > Drivers > copy the connection string; fill in the password (URL-encode special characters). This is `MONGODB_URI`. Save it in the password manager.
*Check:* the cluster shows green/Active and the string starts with `mongodb+srv://`.

**3. Generate secrets** (PC, repo root).
1. `python -m control_api.auth.bootstrap --gen-keys` prints `JWT_SECRET`, `TOTP_ENC_KEY`, `TICK_TOKEN`.
2. Make three more random values, using these commands: `python -c "import secrets; print(secrets.token_urlsafe(48))"` for `HANDOVER_HMAC_KEY` (must be at least 32 bytes; shorter or empty turns handover off, so every restart drops to demo), and `python -c "import secrets; print(secrets.token_urlsafe(32))"` for `DEPLOY_STATUS_KEY` and `RELEASE_TOKEN`. Use a different value for each.
3. `APP_VERSION` is optional: leave it empty and the API reports the first 7 characters of the Render commit as its version. Setting it restarts the service, so only set it if you want a fixed label.
4. Save all of them in the password manager.
*Check:* 6 distinct values saved (3 from `--gen-keys`, 3 you made).

**4. Kalshi DEMO keys only.** On Kalshi's demo site create an API key: you get a key ID (`KALSHI_DEMO_API_KEY_ID`) and a private key file (`KALSHI_DEMO_PRIVATE_KEY`). Do **not** create a production key now and leave `KALSHI_PROD_*` and `KALSHI_LIVE_CONFIRM` empty.
*Check:* the key shows in the demo site's API keys list.

**5. Render free web service from the Blueprint.**
1. Render > New > **Blueprint** > connect GitHub > pick this repo (service `kalshi-control`, free plan).
2. Fill the secret prompts from the table below. Auto-deploy is **OFF** in `render.yaml` (`autoDeployTrigger: off`): pushing to GitHub does not deploy; releases go through the release process.
3. Deploy. Open `https://<service>.onrender.com/healthz`.
*Check:* it shows `{"ok":true}`. Settings > Build & Deploy shows Auto-Deploy **Off**.

| Variable | Where it comes from | Secret? |
|---|---|---|
| `MONGODB_URI` | step 2 | yes |
| `JWT_SECRET`, `TOTP_ENC_KEY`, `TICK_TOKEN` | step 3 (`--gen-keys`) | yes |
| `HANDOVER_HMAC_KEY` | step 3, at least 32 bytes | yes |
| `DEPLOY_STATUS_KEY` | step 3; same value as GitHub secret `DEPLOY_STATUS_KEY` | yes |
| `RELEASE_TOKEN` | step 3; same value as GitHub secret `RELEASE_TOKEN` | yes |
| `APP_VERSION` | optional; empty = Render commit (7 chars) | no |
| `KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY` | step 4 | yes |
| `NTFY_TOPIC` | step 8 | yes (topic is the password) |
| `ALLOWED_ORIGIN` | step 7 (Vercel URL) | no |
| `CRONJOB_API_KEY` | step 8 | yes |
| `PRIVATE_REPO_TOKEN` | step 9 (build time) | yes |
| `PRIVATE_REPO_REF` | optional, default `main` | no |
| `KALSHI_PROD_*`, `KALSHI_LIVE_CONFIRM` | leave empty until going live | yes |
| `PUBLIC_URL` | leave empty (Render sets it) | no |

Never set `DEV_MODE=true` on Render.

**6. Owner account + 2FA (PC).**
1. In the terminal set `MONGODB_URI` and `TOTP_ENC_KEY` (same values as Render), then run `python -m control_api.auth.bootstrap --username <YOUR_USERNAME>`. Choose a password.
2. On your phone install **Duo Mobile** > **+ Add** > **Use QR code** and scan the QR code the CLI prints. (Duo works as a normal third-party TOTP app; no Duo account/organisation is needed.) If the QR does not scan, choose manual entry with the printed key.
3. The CLI prints the secret/URI **once**. Save the manual key in the password manager now; it is your recovery.
4. Type the current 6-digit code into the CLI to confirm.
*Check:* the CLI says `Saved user '<YOUR_USERNAME>'`. Then close the terminal so the variables are gone.

*One-time check after your first login (step 11):* open the app's Audit/Security log and confirm the IP shown for your login is your phone's or home network's public address, not a Render-internal one (10.x, 172.16-31.x). If it is an internal address, tell the assistant before going further: the login rate limiter would then count every visitor as one client.

**7. Vercel.** New Project > import the repo > Root Directory `control_app` > env `VITE_API_BASE_URL` = Render URL (no trailing slash; never set `VITE_MOCK`) > Deploy.
1. CSP step: edit `control_app/vercel.json`, replace `https://your-api-origin.invalid` in `connect-src` with your Render origin, run `node deploy/check_csp.mjs https://<service>.onrender.com`, commit and push.
2. Render > Environment > `ALLOWED_ORIGIN` = the exact Vercel URL (https, no trailing slash). Save (redeploys).
*Check:* the Vercel URL shows the login page and a login attempt does not show a network/CORS error.

**8. cron-job.org keep-awake and ntfy.**
1. cron-job.org > Settings > API > create an API key (unrestricted) > Render `CRONJOB_API_KEY`. In the app, Server page: keep-awake and pinger **at most 14 minutes** (10 and 5 recommended), save with a 2FA code. Details: `deploy/cronjob.md`.
2. ntfy app > subscribe to a long random topic name (`NTFY_TOPIC`; anyone who knows it can read alerts) > put it in Render.
*Check:* Server page shows a cron job ID and "synced"; cron-job.org execution history shows green runs; the phone gets an ntfy test message.

**9. `PRIVATE_REPO_TOKEN` (real strategy at build time).** Without it the build is **demo-only** and **LIVE is refused**. That is correct for now; you can do this step now or just before the live switch. Details: `deploy/private_overlay.md`.
1. github.com > profile picture > **Settings** > **Developer settings** (bottom of the left sidebar) > **Personal access tokens** > **Fine-grained tokens** > **Generate new token**.
2. Name `render-build-overlay`, expiration **90 days**. Resource owner: your account. **Renewal: the token created 2026-10-17 expires 2027-01-15.** Set a calendar reminder for 2027-01-08. To renew: generate a new token with the same settings, paste it into Render `PRIVATE_REPO_TOKEN`, redeploy, then delete the old token. If it lapses, the next build falls back to the stand-in and live mode is refused (safe).
3. Repository access: **Only select repositories** > the private strategy repo only.
4. Repository permissions > **Contents: Read-only**; everything else No access.
5. Generate, copy the `github_pat_...` value once, save it in the password manager.
6. Render > `kalshi-control` > Environment > add `PRIVATE_REPO_TOKEN` > Save > Manual Deploy > Deploy latest commit.
*Check:* the build log shows `private strategy installed` and `IS_STANDIN=False`. With no token it prints `demo-only build`.

**10. GitHub environment `production`** (release secrets; full text: `deploy/release_secrets.md`).
1. Repo > **Settings** > **Environments** > **New environment** > `production` > Configure environment.
2. **Deployment branches and tags** > Selected branches and tags > Add rule > Branch `main` > Add rule. Optional: tick **Required reviewers**, add yourself, Save protection rules.
3. **Environment secrets** > Add environment secret, one per row: `API_BASE_URL` (Render URL), `APP_URL` (Vercel URL), `DEPLOY_STATUS_KEY` and `RELEASE_TOKEN` (same as Render), `RENDER_DEPLOY_HOOK` (Render > service > Settings > Deploy Hook), `VERCEL_DEPLOY_HOOK` (Vercel > project > Settings > Git > Deploy Hooks, name `release`, branch `main`), `NTFY_TOPIC` (same as Render).
4. Do **not** add them as repository secrets (delete any you did).
*Check:* the `production` page lists `main` as the only branch and 7 secret names; Repository secrets has none of them.

**12. Vercel: deploy production only from the `release` branch (one time).** Until you do this, a push to `main` still updates the live app; nothing breaks if you wait. Do it after the first release run has created the branch (the `promote-frontend` job creates `release` on its first run; or in step 3 below create it from `main`).
1. GitHub > repo > **Branches** > check that `release` exists. If not: Code tab > branch dropdown > type `release` > **Create branch: release from main**.
2. Optional protection: Settings > Branches > Add branch ruleset > name `release` > target `release` > tick **Restrict deletions** and **Block force pushes** > Create. Do not require pull requests (the workflow pushes directly).
3. Vercel > your project > **Settings** > **Environments** > **Production** > **Branch Tracking**: change the branch name to `release` > **Save**. (Older dashboards show it under **Settings** > **Git** > **Production Branch**; if you cannot find it, use the search box at the top of the project settings and type "branch".)
4. Vercel > **Deployments**: the current production deployment stays live. Pushes to `main` now show as **Preview**.
5. Run one release (see "Releasing a change"). The `promote-frontend` job moves `release`; Vercel builds it as **Production**.
*Check:* after the release, Vercel > Deployments shows a Production deployment from branch `release` at the released commit, and a later push to `main` shows only a Preview.
*Rollback:* Vercel > your project > **Deployments** > the last good **Production** deployment > ... > **Instant Rollback**. This keeps the approval path intact. Setting Production Branch back to `main` re-opens the unapproved path (every push to `main` reaches your phone), so use it only as a last resort and set it back to `release` afterwards.
*Limit:* the `VERCEL_DEPLOY_HOOK` secret is no longer needed after this step (it would only build a preview); you may delete the hook and the secret together, but then also remove the deploy hook step from `release.yml` first.

**11. Phone and laptop.**
- **iPhone:** open the Vercel URL in Safari (or Chrome) > Share > **Add to Home Screen**. Push needs iOS 16.4 or newer and only works from the Home Screen icon. **Android:** browser menu > Install app.
- Open the app from the icon, log in with password + Duo code, More > Alerts > **Enable notifications** > Allow > **Send test**. Push also needs the VAPID keys (step 7b below).
- Laptop: open the same URL in a browser and log in.
*Check:* the test notification arrives within seconds.

**12. Demo acceptance: must work from a phone AND a laptop.**
- [ ] Login with password + 2FA code
- [ ] Overview loads with numbers (or clear empty states)
- [ ] **Stop** works (no 2FA) and shows the kill switch on
- [ ] **Start** asks for a fresh 2FA code and the bot starts in demo
- [ ] Settings change saves (fresh 2FA); bad values are refused
- [ ] Server page shows the leader, `api_version` and app version, keep-awake synced
- [ ] Push test arrives on the phone
- [ ] Heartbeat age stays under 15 min for 24 hours
- [ ] **Three-deploy rehearsal** (dress rehearsal; use dummy commits, e.g. a README edit): run the `release` workflow (Actions > release > Run workflow, from `main`) three times in a row with the bot running in demo.
  - *Watch:* the app shows a pending release for you to approve (fresh 2FA); the Server page shows the new version, still one leader, generation going up; ntfy sends a "release done" message.
  - *No double orders:* Activity/Logs show a single `mm-` order per market per pass, no duplicates around the restart, and the Kalshi demo site lists no doubled resting orders.
  - *Stop mid-deploy:* during one rehearsal press **Stop**. Stop wins; the deploy finishes with the bot stopped. If the app is unreachable, use Render > Suspend service.
  - A refusal code (for example `CI_NOT_GREEN`, `NOT_HANDOVER_CAPABLE`) is a safe stop, not a failure; read `docs/RELEASE_PROCESS.md`.
- [ ] Live stays off: switching to live is refused while `KALSHI_PROD_*` or the private strategy is missing.

---

## Releasing a change (every time you push)

Pushing to GitHub does **not** change your running app (Render's auto-deploy is off). Two things are yours to do:

1. **Wait for CI.** GitHub > repo > Actions (or the commit's checks): all 7 green. A red X means a failure; "cancelled" on an older commit just means a newer push replaced it. You get an email only on failures.
2. **Run workflow.** Actions > **release** > Run workflow > branch `main` > paste the commit SHA (the full 40 characters; Commits > copy icon) > Run. The gate checks that CI is green and that the commit is the tip of `main`.
3. **Approve in the app.** Open Prior > Server page > the pending release > approve with a fresh Duo code. Nothing deploys before this.
4. **Wait about 3 to 5 minutes.** The workflow deploys Render, moves the `release` branch (Vercel builds production from it, step 12), then reports. ntfy sends "release done".
5. **Check.** Server page: the new `api_version`, you are the single **Leader**, the lease **generation** went up by one. A notification "Trading resumed after restart" is normal: the bot was restored to demo/running as it was.

**If something looks wrong:** press **Stop** (always works, no 2FA). If the app is unreachable: Render > service > Suspend. A refusal code in the gate is a safe stop (see `docs/RELEASE_PROCESS.md`): fix the cause and start a new run; a failed run cannot be re-run, start a fresh one.

**App screens are gated too.** After step 12 below, Vercel deploys production only from the `release` branch, which the release workflow moves after your 2FA approval. A push to `main` makes a preview URL only and never reaches your phone.

## What each email means

Real alerts never ask for your password, 2FA code or key. If an email asks for one, do not click; go to the site yourself by typing the address.

| From | Meaning | Urgency | What to do |
|---|---|---|---|
| GitHub: workflow failed (`python`, `frontend`, `e2e`) | Tests failed on a push or PR | Medium | Open the run, read the first failing step. Nothing deploys automatically; fix before releasing. |
| GitHub: `gitleaks` failed | A secret-like string was found in a commit | **High** | Do not push further. Treat the value as leaked: rotate it (Render/Kalshi/Atlas), then remove it from history. |
| GitHub: `semgrep` failed | A new code-safety finding | Medium | Review it; fix, or add it to the baseline in `CONTRIBUTING.md` if a false positive. |
| GitHub: `commit-messages` failed | A commit subject is not `type(area): summary` | Low | Reword the commit/PR title. |
| GitHub: `release` failed or cancelled | A release stopped at a stage | Medium-High | Open the run; a gate refusal code is a safe stop. Check the Server page that exactly one leader is running; Stop if unsure. |
| Dependabot PR | A dependency update proposed (monthly) | Low | Wait for green tests, then merge or close. Retitle to `chore(deploy): ...`. |
| Dependabot alert / GitHub security advisory | A known vulnerability in a package | Medium-High | Open the alert; merge the fix PR if there is one. |
| GitHub secret scanning alert | GitHub found a key pattern in the repo | **High** | Revoke that key at once at its provider, then replace it. |
| Render: deploy failed | Build or start failed | Medium | Open the logs. The previous version keeps running. |
| Render: health check failing / service crashed | API not answering | **High** if the bot runs | Check the app; press Stop; look at Render logs. |
| Render: service suspended | Free hours or a manual suspend | **High** | Resume only after checking why, then Start from the app. |
| Atlas: cluster paused / storage near limit | Inactivity pause or 0.5 GB nearly used | Medium (paused: High, app fails closed) | Log in to Atlas and Resume; for storage, wait for data to expire or clear old logs. Stop still works. |
| Vercel: build failed | New app version did not build | Low-Medium | The old app stays live. Open the build log. |
| cron-job.org: job failed | Keep-awake ping failed | Medium | Check Render is up and the URL/`TICK_TOKEN`; repeat failures can let the service sleep. |
| healthchecks.io (later) | Dead-man alert: no heartbeat | **High** | Open the app; Stop if unsure. |
| ntfy (phone, not email) | Bot alerts: fills, Stop, breaker, LIVE not restored | Per message | Urgent ones: open the app. "LIVE not restored" means re-arm Live yourself. |
| Kalshi account/API-key notice | Key expiry, account or policy change | Medium | Log in on Kalshi's site directly; replace the key in Render if needed. |

---

 on free tiers. Follow the steps in order. Never paste a key into chat, git or a screenshot; keep keys in a password manager.

**Safety first.** Starting the bot or going live is always reversible with **Stop**:
1. Best: the app's red **Stop** button (one tap, no 2FA).
2. Last resort: see [Emergency stop without the app](#emergency-stop-without-the-app).
3. Desktop fallback only: `stop_maker.ps1`.

Start in **demo** mode. Do not create a production Kalshi key until step 15 is done.

## Steps

1. **Push to GitHub.** Push the repo and confirm the "tests" workflow is green (Actions tab).
2. **Generate secrets** on your PC (repo root; install with `pip install --require-hashes -r control_api/requirements-dev.txt`): `python -m control_api.auth.bootstrap --gen-keys`. It prints `JWT_SECRET`, `TOTP_ENC_KEY` and `TICK_TOKEN`. Save all three in your password manager.
3. **MongoDB Atlas M0** (see `deploy/atlas.md`): create a free M0 cluster (AWS us-east-1), create a database user with readWrite, allow network access `0.0.0.0/0` (Render's free IPs change), copy the connection string as `MONGODB_URI`. Also create a **second, read-only** user (only for the local data tool, see "Local data tool" below); never reuse the Render read/write URI on your PC.
4. **Create your owner account + 2FA** from your PC. Set `MONGODB_URI` and `TOTP_ENC_KEY` in the terminal session, then run `python -m control_api.auth.bootstrap --username <YOUR_USERNAME>`. Choose a password, scan the QR code with an authenticator app, and type one code to confirm.
5. **Kalshi demo key.** Create a key on Kalshi's demo site. You get a key ID (`KALSHI_DEMO_API_KEY_ID`) and a private key (`KALSHI_DEMO_PRIVATE_KEY`). Do NOT create a production key yet.
6. **ntfy.** Install the ntfy app, subscribe to a long random topic name (`NTFY_TOPIC`). Anyone who knows the name can read alerts, so make it unguessable.
7. **Render.** New > Blueprint > pick your GitHub repo (it reads `render.yaml`, service `kalshi-control`). Enter when asked: `MONGODB_URI`, `JWT_SECRET`, `TOTP_ENC_KEY`, `TICK_TOKEN`, `KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY`, `NTFY_TOPIC`. Leave empty: `PUBLIC_URL`, `CRONJOB_API_KEY`, `KALSHI_PROD_*`, `KALSHI_LIVE_CONFIRM`. Prefilled: `MAX_ARB_CONTRACTS=25` (API ceiling for contracts per arbitrage leg). **Never set `DEV_MODE=true` on Render** (it only enables local dev CORS and is ignored on Render, but leave it unset). The build installs from the hash-pinned lock (`--require-hashes`, wheels only). When deployed, open `https://<service>.onrender.com/healthz`; it should show `{"ok":true}`.
7b. **Push notifications (VAPID keys, optional).** Needed only for notifications on your phone.
    1. On your PC run `npx web-push generate-vapid-keys` (or any tool that makes a P-256 VAPID pair). Copy the **private** key.
    2. In Render > Environment add `VAPID_PRIVATE_KEY` (secret; PEM or the raw base64url key) and `VAPID_SUBJECT` (`mailto:<YOUR_EMAIL>` or your https URL; if empty, an https `PUBLIC_URL` is used). Save (redeploys). The public key is derived and served by the API; do not put either key in git or Vercel.
    3. **iPhone acceptance test (you do this during the demo weeks).** Push on iPhone only works from the Home Screen app. In **Chrome on iPhone** use Share > **Add to Home Screen** (Safari: Share > Add to Home Screen). Open the app from that icon, log in, go to More > Alerts (or Account), tap **Enable notifications** and allow, then tap **Send test**. A notification should arrive within seconds. Then check: a daily summary at your chosen time, and that Quiet Hours holds back everyday alerts but never Stop or breaker alerts.
    3b. **Check and clear registered devices.** In the app go to More > Settings and scroll to **Push Devices**, just under Notifications. Each phone or browser you enabled shows as a push service name (for example Apple or FCM) with the time it was added. Tap **Clear All**, confirm, and every device is removed (no 2FA needed); on each phone tap **Enable notifications** again to re-register. Do this if you re-test from scratch, lose a phone, or see a device you do not recognise. Up to 10 devices can be registered.
    4. No key set = push is simply off; ntfy keeps working. To stop everything, use the app's **Stop** button; last resort, suspend the service in Render, or `stop_maker.ps1` on the desktop.
8. **Vercel.** New Project > import the repo. Root Directory `control_app`. Environment variable `VITE_API_BASE_URL` = your Render URL (no trailing slash). Never set `VITE_MOCK`. Deploy and note the Vercel URL.
   - **Set the exact API origin in the CSP (S-14).** Edit `control_app/vercel.json` and replace `https://your-api-origin.invalid` in `connect-src` with your API origin (for example `https://<service>.onrender.com`; no wildcard, no path). Check it with `node deploy/check_csp.mjs https://<service>.onrender.com`, then commit and push. Until you do, the deployed app cannot reach the API (it fails closed). Details: `deploy/vercel.md`. Same edit for a VM or custom domain.
9. **ALLOWED_ORIGIN.** In Render > Environment set `ALLOWED_ORIGIN` = the Vercel URL exactly (https, no trailing slash). Save (redeploys). A mismatch locks the app out.
10. **cron-job.org** (see `deploy/cronjob.md`): sign up, Settings > API > create an API key (leave unrestricted). Set it in Render as `CRONJOB_API_KEY`. The app creates and manages the ping job itself.
11. **Phone.** Open the Vercel URL, Add to Home Screen, log in (see `docs/PHONE.md`).
12. **Server page.** Set keep-awake 10 min and pinger 5 min and save (needs a 2FA code). Confirm a cron job ID appears and it says synced.
13. **Demo run.** Press Start (2FA), then Run once. Check Activity and Logs. Press Stop and confirm the kill switch shows on. Check ntfy alerts arrive.
13b. **Arbitrage (optional, log-only first).** In the app open More > Arbitrage and turn it on in Log only mode (2FA). It only records opportunities. Going Live needs typing `LIVE`, a fresh code, prod keys and `KALSHI_LIVE_CONFIRM`; keep it log-only until you understand the legging risk. **Stop** stops the maker and arbitrage together.
14. **Verify on demo:** the Kalshi order, fill, settlement and trades field names look right (P&L and fill quality show numbers rather than dashes once fills settle) and "cash" excludes money tied up in resting bids. Fill quality on demo reflects demo liquidity and is not representative.
15. **Watch for 24 hours:** heartbeat age stays under 15 min, the service never sleeps, Atlas storage is far below 0.5 GB.
16. **Go live (only when happy).**
    1. Create a Kalshi production key.
    2. In Render set `KALSHI_PROD_API_KEY_ID`, `KALSHI_PROD_PRIVATE_KEY` and `KALSHI_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY`.
    3. In the app: Mode page > Live, enter a fresh 2FA code and type `LIVE`.
    4. **Stop** at any time with the red button. To back out: switch to demo and clear `KALSHI_LIVE_CONFIRM` in Render.
    5. **After every restart or redeploy you must re-arm Live** (S-21): the bot always comes back in demo and sends an urgent ntfy alert "LIVE not restored: re-arm from the phone". Go to Mode > Live again (2FA + type `LIVE`). Same for arbitrage live.

    **Preconditions from security review pass 4 (do these before step 16.2):**
    1. `control_app/vercel.json` `connect-src` is your exact Render origin and `node deploy/check_csp.mjs https://<service>.onrender.com` passes (step 8).
    2. `MONGODB_URI` has a strong, unique password (Atlas network access is `0.0.0.0/0`, so the URI is effectively a credential), and the data tool uses a separate **read-only** Atlas user (S-13).
    3. `KALSHI_PROD_*` and `KALSHI_LIVE_CONFIRM` are set in Render only when you mean to go live.
    4. ntfy alerts are on, and you tested both the Stop button and `RUNNER_ENABLED=false` on the deployed demo.
    5. You accept the residual risks: someone with Atlas write access could resume **demo** or log-only trading at a restart (you get an alert); a stolen login token can force extra passes (not more often than every 60 s), but only within the limits you armed.
    6. You have read your strategy's validation (kept in the private repo): run demo first, then live at a small size you can afford to lose entirely in a day, and judge by the **fill quality** block (Overview and Markets) before P&L. See `docs/FEATURES.md`, "Before you go live".
17. **Maintenance.**
    - Auto-deploy is off: ship with the `release` workflow (see `docs/RELEASE_PROCESS.md`). To roll back, redeploy an earlier deploy in Render (Events tab).
    - After rotating `TICK_TOKEN`, press Sync on the Server page.
    - **Adding or upgrading a Python package:** edit `control_api/requirements.in`, regenerate both locks with the command in the header of the `.in` files (needs `uv`; Python 3.11 target), commit `requirements.txt` and `requirements-dev.txt`. Never hand-edit them. Installs always use `pip install --require-hashes --only-binary :all: --no-deps -r ...`.
    - Atlas pauses after 30 days inactive: log in and Resume.

## Starting a new demo period

Use this for a clean performance window (for example Period 2).
1. App > Settings: set **Poll Minutes** to **10** and Save (fresh 2FA). It must stay 14 or lower.
2. App > Server (or Account) > **Mark new period**: enter a name and optional note, confirm with fresh 2FA. A second marker within 5 minutes is refused (typo guard).
3. Record any money you add or take out as a **cash-flow event** (deposit or withdrawal, amount, date, optional note) with fresh 2FA. A typo can be deleted and re-added. These show beside P&L, never inside it.
4. Make **no deposits or withdrawals during the period**, so returns stay comparable.
5. Use the period selector (All / each period) on P&L and By Source. Fills count in the period they were filled, even if they settle later.
6. Start the bot as usual (Start re-reads and validates the saved config). To stop at any time: the app's Stop button; last resort, suspend the service in Render (see below).

## Monitoring, backups and drills (after the first deploy works)

- **External monitoring** (free healthchecks.io dead-man switch plus UptimeRobot on `/healthz`, alerts to ntfy):
  click-by-click setup, thresholds and what each alert means are in `docs/MONITORING.md`. Verify from your computer
  with `python scripts/check_monitoring.py`. All intervals stay <= 14 min (the Render keep-awake rule).
- **Backups:** Atlas M0 has none, so run `python scripts/backup_export.py` weekly with a read-only Atlas user.
- **Restore drill** (into a database whose name contains `scratch`, never the live one) and the **rebuild-from-scratch
  drill** (new Render + Atlas + Vercel from the repo and your env list): checklists and a results template in
  `docs/BACKUP_RESTORE.md`. Do the restore drill before the live date and quarterly after it.
- New optional Render env var: `HEALTHCHECKS_PING_URL` (secret, see `deploy/env.example`).

## Emergency stop without the app

Use if the app, Vercel or your phone login is unavailable.
- **Render dashboard > `kalshi-control` > Suspend service.** Nothing runs, so no orders are placed. Resting orders already on Kalshi remain until you cancel them in Kalshi's own site; do that too.
- **Also available (S-10 fixed 2026-10-13):** set `RUNNER_ENABLED=false` in Render > Environment and save. It is now a hard stop: tick, run-once, Start and arbitrage are refused, and the runner runs Stop once at startup (kill switches plus cancelling its resting `mm-` orders). Best used together with Suspend service and cancelling on Kalshi.
- Resume later with Resume service (and set `RUNNER_ENABLED=true`), then Start from the app.
- Desktop fallback only: `stop_maker.ps1`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App says "Waking up the server" | Render free was asleep; wait up to a minute. If it happens often, check the Server page pinger/keep-awake and cron-job.org job. |
| Login fails with a network/CORS error | `ALLOWED_ORIGIN` does not exactly match the Vercel URL (step 9). |
| "2fa_invalid" right after login | Each code works once and login used the current one. Wait for the next code (up to 30 s). |
| Account locked (429) | 5 wrong codes lock for 15 min. Wait. Stop still works with a valid session. |
| Lost phone | End every session: from another logged-in device use **Server > Log out all sessions** (2FA; API `POST /api/auth/logout-all`), or from the PC with `MONGODB_URI` and `TOTP_ENC_KEY` set: `python -m control_api.auth.bootstrap --username <YOU> --revoke-sessions` (keeps password and 2FA; live within about 10 s). If the 2FA device is gone too: `--reset-2fa` (also logs out everything). Then press Stop if the bot should not run. |
| Login says "session expired" (401 `session_expired`) | All sessions were logged out or credentials were reset; log in again. |
| "Server can't reach the database" banner / 503 `store_unavailable` | Atlas paused or unreachable. Every endpoint except Stop fails closed. Resume it in Atlas. Stop still works. |
| Start/arbitrage refused with `runner_disabled` | `RUNNER_ENABLED=false` is set in Render; set it back to `true`. |
| Start refused with `stop_in_progress` / `stopped_during_start` | A Stop was running or happened during Start (Stop always wins); wait a few seconds and Start again. |
| App cannot reach the API after a fresh Vercel deploy | The CSP in `control_app/vercel.json` still has the placeholder origin (step 8). |
| Start refused | Check that the store is up (Start fails safe without it). |
| Live mode refused ("live_not_allowed") | Prod key or `KALSHI_LIVE_CONFIRM` missing in Render. |
| Heartbeat age grows | Service asleep or crashed; check Render logs and the cron-job.org execution history. |
| Render region has no free plan | Edit `region` in `render.yaml` to `ohio` or `oregon`. |

Not yet verified: whether cron-job.org or Atlas ask for a card at signup; Atlas M0 connection limits and backups.

## Optional: analytics edge benchmark file

The Analytics page can compare each cell with an optional private benchmark file (`cell_expectations.json`, also the source of `expected_roi` and the fill-quality baseline). Nothing to do for the demo. The file is never in this repo; supply it to Render as a **secret file**: in Render > Environment > Secret Files add `cell_expectations.json`, then set the optional env var `CELL_EXPECTATIONS_PATH=/etc/secrets/cell_expectations.json`. If the file is missing the page still works; there is simply no comparison ("expectations not loaded"). Never put this file in a public repo.

## Local data tool (read-only) and cloud-sync warning

`python -m control_api.tools.pull_data` copies data from Atlas into git-ignored `local_data/` for analysis (project skill `update-local-data`). It reads `MONGODB_URI` from `.env.local`.
- Use a **separate read-only Atlas user**, never the Render read/write URI (S-12/S-13).
- **If the repo sits in a cloud-synced folder such as OneDrive, every file is uploaded, `.env.local` and `local_data/` included.** Keep `.env.local` outside the synced folder (set `MONGODB_URI` in the terminal session instead), or make sure OneDrive excludes it; and move or exclude `local_data/` too.
- `--seed-demo` runs it with fake data and needs no Atlas.

### Scheduled "Update Local Data" (optional, Windows)

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule_pull_data.ps1 -Register` creates a daily Task Scheduler job (default 07:00, option `-Time 06:30`) that runs `python -m control_api.tools.pull_data --days 180` from the repo folder and appends output to `local_data\pull.log`. `-Status` shows state, last result and the log tail; `-Unregister` removes it. Details:
- It runs **only while you are logged in** (no password is stored) and catches up after a missed run at the next logon or wake.
- It is read-only toward Atlas and uses the read-only URI in `.env.local`.
- Each run keeps `local_data/history/` (append-only CSVs) and dated snapshots in `local_data/archive/` (newest 90).
- **Cloud-sync caveat (S-13):** the task runs code from wherever the repo sits and writes `local_data/` there. In a synced folder that uploads your data (including `state.json`, which holds your bot configuration) and `.env.local`. Recommended: clone the repo outside OneDrive (for example `C:\dev\trading`), keep `.env.local` there, and register the task from that copy.
- Not registered by default; registering it is your decision. Nothing here can start the bot; to stop trading use the app's Stop button (last resort: suspend the service in Render, or `stop_maker.ps1` for the desktop fallback).

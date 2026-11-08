# Release process (CI/CD): updating Prior while the bot runs

_Status: **design approved and built (2026-10-16); my decisions (A2, B2, C = 120 s) were made 2026-10-13.** The real first-run verification happens in the dress rehearsal (three dummy releases in demo, see the Demo-Day Checklist in DEPLOY.md). Written 2026-10-13._

Goal: ship backend or frontend updates **while the bot is running (demo or live)** without double trading, lost orders, a stuck Stop or a broken app. If any check fails, the system fails **safe** (no new orders) instead of failing open.

## 1. The problem this solves

Render redeploys with "zero downtime": it starts the **new** server while the **old** one is still serving, moves traffic over, then sends SIGTERM to the old one about **60 s** later. It waits a shutdown delay (default 30 s, max 300 s) before SIGKILL ([Render deploy docs](https://render.com/docs/deploys)).

Our bot runs **inside** the API process. So during every deploy there are briefly **two bots**. Without coordination both could place orders. That is the main thing to fix, and it matters even with today's "stop before pushing" rule.

Other risks during an update:
- A pass in progress is killed halfway.
- The new code reads data written by the old code, or the reverse.
- The installed phone app is older or newer than the server.
- A bad build reaches production.
- Live mode drops to demo on every restart (S-21).

## 2. Roles: who can do what

| Role | Can | Cannot |
|---|---|---|
| **Me** (`calebabr`) | Approve a release, trigger production deploys, roll back, go live, Stop | n/a |
| **AI coding agents** | Write code, tests, docs, open commits and PRs, run local and CI checks | Deploy, change Render/Vercel/Atlas settings, touch secrets, change `cells`/strategy without approval, push with failing checks |
| **CI** (GitHub Actions) | Run every check; be the only path that marks a commit releasable | Hold production secrets beyond a deploy-hook URL and a read-only status key |
| **Render / Vercel** | Build and run what CI approved | Auto-deploy unchecked commits |

## 3. Pipeline

1. **Work happens on branches.** Every change lands on `main` through a PR.
   - GitHub Free has no branch protection on private repos, so the CI gate below is what enforces this, not GitHub settings.
2. **CI (required, all must pass):**
   - **Bot and API tests,** plus the Stop-invariant fuzz and security regressions.
   - **App tests, build and `npm audit`.**
   - **Mock end-to-end checks,** plus the real-API end-to-end checks against the dev server.
   - **Dependency checks:** hash-locked installs and lock agreement, plus `pip-audit`.
   - **CSP check.**
   - **Cells unchanged:** fails if `cells` changed without a tag signed by me.
   - **Compatibility check:** the new API must still answer every request the currently deployed app makes (contract tests), and must read data written by the previous release (fixture of the previous schema).
3. **Render "Auto-Deploy: After CI Checks Pass"** on `main`. If zero checks are detected or any check fails, no deploy happens.
   - **Option:** set it to Off and deploy only through a release workflow I trigger by hand. See decision B.
4. **Vercel** production builds only from the `release` branch (Vercel Settings > Environments > Production > Branch Tracking = `release`; DEPLOY.md step 12). The release workflow's `promote-frontend` job fast-forwards `release` to the approved commit, only after the CI gate, my 2FA approval and the verified backend deploy; it never forces, and it is the only job besides the tag job with `contents: write`. Pushes to `main` make previews only. Until I flip the setting, a push to `release` just makes a preview. Rollback: Vercel Instant Rollback to the last good production deployment; setting Production Branch back to `main` re-opens the unapproved path and is a last resort.
5. **Order:** when a change touches both, the **backend deploys first**, then the frontend. The release workflow enforces this.
6. **Post-deploy verification (automated):**
   - `/healthz` reports the new version.
   - The new instance holds the bot lease (section 4).
   - Heartbeats resume.
   - No errors in the first passes.

   I get an ntfy alert "Release vX deployed, healthy". If verification fails: alert, and the bot stays safe (section 4).
7. **Rollback:** Render "rollback to previous deploy", using the same handover rules. Releases are tagged (`vYYYY.MM.DD-n`) so the running version is always known.

## 4. Running-bot handover (no double trading)

A **single-leader lease** in Atlas decides which server instance may trade.
- **The lease:** a `runner_lease` document `{holder: instance_id, version, expires_at, generation}`. Only the holder runs maker passes, arbitrage scans and order cancels other than Stop. It renews every 20 s. A lease expires 60 s after its last renewal.
- **Fencing:** every pass re-checks it still holds the lease (same generation) right before sending any order. If it doesn't, it sends nothing.
- **New instance starts in standby.** It serves the API and the app straight away but does not trade until it gets the lease.
- **Old instance on SIGTERM:**
  1. Stop starting new passes.
  2. Let the current pass finish its order step. A pass is never cut off mid-order; it has a hard 25 s budget.
  3. Save state.
  4. Release the lease with a handover record.
- **Handover:** the new instance takes the lease and continues with the same desired state (running or stopped, demo or live per decision A). Resting maker orders stay on Kalshi throughout; the next pass re-quotes them as normal. Arbitrage is never mid-set across a handover, because sets are immediate-or-cancel and the old instance finishes or skips a set before releasing.
- **Crash or kill without handover:** the lease expires after 60 s and the new instance takes it. No two holders can ever overlap, because of the fencing.
- **If Atlas is unreachable,** nobody can confirm the lease, so **no instance trades** (fail safe).
- **Stop is unchanged:**
  - It works on either instance, with no lease, no 2FA and no Atlas needed.
  - It sets the kill switches and the persisted stop.
  - The lease holder and any new holder must respect it.
  - Stop beats the lease: a stopped bot stays stopped across any number of deploys.
- **Render setting (changed 2026-11-07, Sprint 2B item 11, option b):** the free plan cannot raise the shutdown delay, so the whole shutdown is sized to Render's **default 30 s** window (`runner.RENDER_SHUTDOWN_WINDOW_S`). uvicorn drains in-flight requests for at most 5 s first (`--timeout-graceful-shutdown 5` in `render.yaml` = `runner.UVICORN_GRACEFUL_S`), then the runner has **22 s** (`shutdown_budget_s`, 3 s margin before SIGKILL):
  - it waits up to **12 s** (`shutdown_order_wait_s`) for a running order step or arbitrage set to finish (never cut by us);
  - every shutdown store call is bounded at 2 s (journal flush, Stop check, handover read, lease release: 8 s reserved), and a remote-Stop cancel gets only what is left before the release;
  - a pass still running after 12 s keeps the lease (it expires after the 60 s TTL), so no other instance trades beside it; the successor's takeover cancel covers a stopped bot.
  - The release gate still refuses to deploy while an arbitrage set is executing. A paid plan may set `SHUTDOWN_DELAY_S` (env) to its real `maxShutdownDelaySeconds`; the budget scales with it.

## 5. Compatibility rules (old and new code side by side)

- **Database changes are "expand, then contract".**
  - Add new fields or collections first, with the code tolerating both old and new shapes.
  - Remove old ones only in a later release.
  - Never rename in place.
- **API changes are additive.**
  - Never remove or rename a response field the app uses within one release.
  - New fields must be optional in the app.
  - The app already tolerates missing fields; this keeps it true.
- **Version handshake.**
  - `/healthz` and `/api/status` report `api_version` and the minimum app version they support.
  - The app shows "Update available, reopen the app" if it is too old. It keeps showing data, and Stop always works.
- **Config:** `maker_config.json` and the stored config carry a schema version. New code reads the old version, and validation (`validate_config` + ceilings) runs on every load, as today.

## 6. Pre-deploy safety gate (automated)

The release workflow refuses to deploy unless all of these hold:
- CI is green on the exact commit.
- The lease and handover code is present: a version check on the running server confirms it supports handover. **Before the first deploy that has handover,** the gate requires the bot to be **stopped**. That is the original rule, kept until handover is proven.
- No arbitrage set is executing.
- The running server is healthy. If it isn't, the deploy proceeds only with the bot stopped.
- I approved the run. A manual approval step in GitHub Actions records who and when.

The checks use a **read-only deploy-status key** with these properties:
- It's scoped to one endpoint that reports `{version, lease_holder, running, mode, arb_executing}`, plus nothing else.
- It's stored as a GitHub secret and in Render.
- It is never my login, and it is never public.

## 7. Live mode across planned deploys (decision A)

Today (S-21), any restart drops LIVE to demo, and you re-arm from the phone. With handover, a **planned** deploy could keep LIVE using a **signed handover**:
- The old instance writes a short-lived handover token, HMAC-signed with a secret that exists only in Render's environment (never in Atlas).
- The new instance keeps LIVE only if the token is valid, less than 5 minutes old, for the same config hash, and no Stop happened since.
- An attacker with only Atlas write access can't forge it, which is the case S-21 protects against.
- Crashes, unplanned restarts and rollbacks without a valid token still drop to demo.

## 8. Verification before this is trusted

- **Tests:**
  - **Lease and fencing:** two instances overlapping, one killed mid-pass, Atlas flapping, a clock jump.
  - **Handover:** handover while running in demo and in live.
  - **Signed-handover forgery** is refused.
  - **Stop during handover** always ends stopped.
  - **Compatibility:** old app against the new API, new app against the old API, and old data read by new code.
- **Dress rehearsal on Kalshi demo:** deploy three times while the bot runs in demo. Confirm the following, recorded in `docs/` as the go/no-go for using this with live:
  - no double orders (compare the journal and Kalshi order history);
  - no gaps longer than one pass;
  - Stop works mid-deploy.
- **Security review** of the lease, the handover token and the deploy-status key.

## 9. Decisions

- **A. Live across planned deploys:**
  - **(1) keep S-21:** every deploy drops to demo and you re-arm;
  - **(2) signed handover:** a planned deploy keeps LIVE, while crashes still drop to demo.
- **B. Who triggers production deploys:**
  - **(1) automatic after CI passes**, plus the safety gate;
  - **(2) a manual release workflow** you approve each time (recommended at first).
- **C. Shutdown delay:** 90 s (recommended), or longer.

## 10. My decisions (2026-10-13)

| Decision | Choice | What the team builds |
|---|---|---|
| **A** Live across planned deploys | **A2: signed handover** | The old instance writes an HMAC-signed handover token (secret only in Render env, never in Atlas). The new instance keeps LIVE only if the token is valid, < 5 min old, same config hash, no Stop since. Crashes, unplanned restarts and rollbacks without a valid token still drop to demo (S-21 remains the default). Forgery and replay tests, plus security review |
| **B** Who triggers deploys | **B2: manual release workflow** | Render auto-deploy **Off**. A GitHub Actions "release" workflow with an approval step by me (approvable from the phone) runs: CI green on the exact commit → safety gate → backend deploy hook → verify → frontend deploy → verify → ntfy "Release vX healthy" → tag. AI coding agents can open PRs but never run or approve it |
| **C** Shutdown delay | **120 s** | `render.yaml` shutdown delay 120 s; pass budget 25 s; handover must complete well inside it (tested) |

> **Resolved 2026-11-07 (option b, free):** the runner's shutdown now fits Render's default 30 s window (section 4, "Render setting"); decision C is replaced by that budget. The original note follows.
>
> **Free-plan limit found 2026-10-17:** Render rejects `maxShutdownDelaySeconds` on the free plan, so the 120 s window above needs a paid plan. On free, Render's default shutdown window applies; a shutdown that is cut short is covered by the successor's takeover cancel. This is fine for the demo (demo mode only). Before LIVE, either move to a paid plan and restore `maxShutdownDelaySeconds: 120`, or shorten the runner shutdown to fit the default window.

### B2 mechanics (checked 2026-10-13)

GitHub's native "required reviewers" on deployment environments is **not available for private repos on GitHub Free/Pro/Team** ([GitHub docs](https://docs.github.com/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment)). So B2 for the private repo works like this:
1. **Start:** I run the `release` workflow (`workflow_dispatch`) from **GitHub Mobile** (Repository → Actions → release → Run workflow), choosing the commit or tag. Only accounts with write access can run it.
2. **Gate:** the workflow checks that CI is green on that exact commit and runs the safety gate.
3. **Approve with 2FA in Prior:** the workflow posts a *pending release* (version, commit, change summary, CI result, and an optional `trading_changes` flag) to the API. The Server page shows it with **Approve** / **Reject**; Approve needs fresh 2FA. The workflow polls the API using a release-scoped token and proceeds only after approval; it times out (no deploy) after 30 min. AI coding agents can never approve (no 2FA), even though their commits appear under the owner's git name.
   - **`trading_changes`** (boolean, default `false`, added 2026-11-07): set it to `true` when the release changes trading behaviour, sizing, caps or the reliability profile (anything that starts a new period). It is stored with the pending release and written into the `release.register` and `release.approve` audit rows, which the Periods page markers read to show the release as a trading-behaviour change. App-only releases leave it `false`. The register body is `{version, commit, summary, ci, trading_changes}`; a non-boolean value is refused (422). Tick the **trading_changes** box when you run the release workflow; it passes `--trading-changes` to `scripts/release_ctl.py register`.
4. **Deploy:** backend deploy hook → verify → frontend → verify → ntfy "Release vX healthy" → tag.

After the public/private split, the public `tradingControlApp` repo could use GitHub's native required reviewers (free on public repos) as an extra layer.

## Branching rule (decided 2026-10-13)
- **Before the first deploy:** work on `main`, all tests green.
- **After the first deploy (demo or live):**
  - **Large features** → branch → PR → I merge.
  - **Always branch** for: strategy / cells / orders, Stop / auth / security, database schema, release / handover code.
  - **Small changes** (docs, copy, small UI, tests, contained fixes) may go to `main` with all tests passing locally.
- **New strategies or markets:** validated in the private repo → I approve → demo → small live. Strategy code and research stay in the private repo.
- AI coding agents never merge PRs or run releases.

**Releasing a change that only touches the private strategy.** The release builds the public tip and overlays the private repository's main at build time, so a private-only change needs a new public commit to give the release workflow a new SHA to approve. Make a small public commit (a docs line is enough), wait for CI, then release that SHA as usual.

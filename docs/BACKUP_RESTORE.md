# Backups, restore drill and rebuild drill

## What is (and is not) backed up

- **Data worth keeping** lives only in MongoDB Atlas: fills, settlements, journal rows, heartbeats, snapshots,
  config versions, periods, cash flows, reconciliation reports, alert history, shadow data, audit log.
- **Not in the database, so nothing to back up:** Kalshi keys and every other secret (Render environment only;
  keep them in your password manager), the code (GitHub), the app (Vercel).
- Atlas **M0 has no continuous or snapshot backups** (those start at M10). Your backup is the export below, which
  you run yourself. Some collections expire by TTL (heartbeats, journal rows, account snapshots: 180 days), so an
  old backup is also a longer history than Atlas keeps.
- By default the export skips `users`, `revoked_tokens`, `push_subscriptions`, `runner_lease`, `handover_nonces`
  (password hash, encrypted 2FA secret, sessions, push endpoints). After a rebuild you recreate the owner login with
  the bootstrap CLI and re-enable push in the app. `--include-sensitive` exists, but then the folder must be kept
  encrypted (for example inside your password manager's attachments or an encrypted drive).

## Weekly backup (owner, 5 minutes)

Use a **read-only** Atlas user for this (Atlas > Database Access > Add user, role **Read** on the bot database
only; a second user, so the app's read-write user is never on your laptop). Atlas > Connect > Drivers gives the
connection string; put the password in yourself.

```
set MONGODB_URI=mongodb+srv://<readonly-user>:<password>@<cluster-host>/
set MONGODB_DB=kalshi_control
python scripts/backup_export.py --out C:\path\to\backups
```

It prints per-collection counts and writes `<out>\<UTC stamp>\` with one `.ndjson.gz` per collection and
`manifest.json` (counts and SHA-256). Keep the last 8 weeks, in a folder that is **not** inside this repo
(`backups/` is git-ignored in case you do put it there). The existing `control_api.tools.pull_data` tool
(Update Local Data) is for analysis CSVs and is **not** a backup: it is scrubbed and lossy.

Optional automation: run it from the same Windows Task Scheduler pattern as `scripts/schedule_pull_data.ps1`
(weekly). The URI must come from a user environment variable, never from a file in the repo.

## Restore drill (owner, quarterly and before the live date)

Goal: prove a backup can be read back and that you know the steps, **without touching the live collections**.

The restore script refuses (exit 2, nothing written) unless the target database name contains `scratch`, differs
from `MONGODB_DB` and from the backup's source database, and the backup's checksums match. It is a dry run unless
you add `--apply`.

1. Pick the newest backup folder. *Check:* `manifest.json` exists and lists collections.
2. Atlas > Database Access: create a **temporary** user with a custom role that can write only to the scratch database
   (Add New Database User > Built-in Role: Custom Role > readWrite on `kalshi_scratch_drill` only). Do not use the app's
   read-write user for this drill; delete the temporary user when you finish. Set `MONGODB_URI` to it, and `MONGODB_DB=kalshi_control` (the LIVE name, so the
   script can refuse it).
3. Dry run:
   `python scripts/restore_drill.py --backup <folder> --target-db kalshi_scratch_drill`
   *Check:* prints `DRY RUN`, every collection with its expected count, `RESULT: ok`.
4. Apply: same command plus `--apply`.
   *Check:* `APPLIED`, `restored` equals `expected` for every collection, `RESULT: ok`.
5. Look at it: Atlas > Browse Collections > database `kalshi_scratch_drill`; open `fills` and `config_versions`,
   spot-check a few documents against the app.
6. Optional: run `python -m control_api.tools.pull_data --days 30` pointed at the scratch database (set
   `MONGODB_DB=kalshi_scratch_drill`) and compare the SUMMARY with the live one.
7. **Clean up:** Atlas > Browse Collections > `kalshi_scratch_drill` > drop the database. Remove any temporary
   user. The scratch database counts toward the 512 MB M0 limit while it exists.
8. Fill in the results template below.

A restore into the **live** database is not scripted on purpose. If the live data is ever lost, restore into a new
scratch name first, check it, and only then (with the bot stopped and Atlas as the single writer) point
`MONGODB_DB` at the restored database in Render, or ask for help; do not copy over live collections by hand.

## Rebuild-from-scratch drill (owner, once before the live date, then yearly)

Goal: recreate the whole system from the repo and your password manager if Render, Atlas or Vercel is lost. Use
**demo** Kalshi keys and fresh free-tier resources; do not touch the real services. Time-box: 2 hours.

1. **Inventory** (from `deploy/env.example` and `render.yaml`): list every environment variable and where its
   value lives in your password manager. Anything not there is a gap: write it down.
2. **Atlas:** create a second free project/cluster `kalshi-drill` (or a new database user and database
   `kalshi_scratch_rebuild` on the existing cluster, if the one-M0-per-project limit blocks you). Follow
   `deploy/atlas.md`. *Check:* connection string works from `python scripts/restore_drill.py ... ` dry run.
3. **Restore data:** run the restore drill above into `kalshi_scratch_rebuild` (it satisfies the scratch rule).
4. **Render:** New > Blueprint > this repo (`render.yaml`), service name `kalshi-drill`, then fill the secret env
   values (`sync: false` ones) from the password manager; `MONGODB_DB=kalshi_scratch_rebuild`, **demo keys only**,
   `RUNNER_ENABLED=false`. *Check:* `/healthz` answers 200 and the deploy log is clean.
5. **Owner login:** run the bootstrap CLI (see `docs/DEPLOY.md` step on creating the owner) against the new
   service's database. *Check:* log in with a fresh 2FA code.
6. **Vercel:** import `control_app` as a new project `prior-drill`, set `VITE_API_BASE_URL` to the new Render URL,
   and add the new Vercel URL to `ALLOWED_ORIGIN` on Render. *Check:* the app loads, shows data from the restored
   fills, Stop works.
7. **Time it** and note every step that was unclear or needed a value you did not have.
8. **Tear down:** delete the drill Render service, Vercel project and scratch database. Remove demo keys.

## Results template (copy into your notes; one per drill)

```
Drill: restore | rebuild          Date (UTC):            Done by:
Backup used (folder stamp):       Backup age (days):     Atlas used MB / 512:
Dry run result:  ok / refused (reason)
Counts (collection: expected -> restored):
  fills:            heartbeats:         journal_rows:      config_versions:
Mismatches / errors:
Spot-check of documents (what compared, result):
Rebuild only: minutes per step  Atlas __  Restore __  Render __  Login __  Vercel __  Total __
Rebuild only: values missing from the password manager:
Unclear steps / doc fixes needed:
Cleanup done (scratch DB dropped, temp user removed, drill services deleted):  yes / no
Overall: PASS / FAIL            Next drill due:
```

## Maintenance rules

- Backups of an old schema restore fine: collections are plain documents and the schema changes are additive
  (`SCHEMA_VERSION`). Restoring does not recreate indexes; the app creates them on start (`ensure_indexes`).
- Never run `restore_drill.py` with `--target-db` set to the live name: it will refuse, and that refusal is a test.

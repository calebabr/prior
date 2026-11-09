# Load and contention tests

A pure-Python suite (asyncio + httpx) in `loadtests/`. There is one user, one bot pass about every 10 minutes and a
phone polling, so the risk on the free tiers is **resource contention**, not user count. The suite checks that Stop
stays fast, nothing returns a 5xx, the rate limits answer 429, and memory stays flat.

## Run it

```
pip install -r control_api/requirements-dev.txt -r loadtests/requirements.txt   # psutil is the only addition (optional)
python -m loadtests.run --scenario all --target local             # about 4 minutes at the default size
python -m loadtests.run --scenario contention,soak --target local # a subset (names or letters a-g)
python -m loadtests.run --scenario all --target local --quick      # about 30 s, small sizes
python -m pytest -q loadtests/tests                                # the harness's own tests (one slow smoke run)
```

`--target local` starts a throwaway API (`python -m loadtests.server`) on a free port: the real FastAPI app and
runner with a `MemoryStore`, seeded with 20,000 synthetic fills over 180 days (the largest Analytics dataset), a
fake Kalshi client that records cancels (3 resting `mm-` orders and 1 manual order), and a fake "bot pass" that
sleeps (default 5 s). Nothing trades and nothing leaves the machine; bot files live in a temp directory.
Credentials are generated per run, held in memory only, and the writer refuses any result that contains one.

Options: `--rows`, `--pass-seconds`, `--soak-seconds` (default 120), `--rounds` (contention, default 3),
`--out-dir` (default `docs/load-tests/`).

### Against the Render demo deploy

```
set LOADTEST_TOKEN=<an access token for the demo owner, from a normal login>      (one hour lifetime)
python -m loadtests.run --scenario all --target https://<your-demo>.onrender.com --confirm-demo
```

- Demo only, never live. A host containing `live` or `prod` is refused, `http` is refused for remote hosts, and the
  run aborts unless `GET /api/status` answers 200 with `mode == "demo"`. `--confirm-demo` is required, and the Stop
  scenarios refuse to run while the demo bot is running unless you add `--allow-running`.
- Only `polling`, `analytics`, `auth` (bad-password burst only; it cannot lock the owner) and `stop` run remotely, at
  gentle sizes (Render free sleeps and Atlas M0 has connection limits). `push`, `contention` and `soak` need the fake
  pass or the local test users and are skipped. Memory is not measured remotely. No real credentials are typed or
  stored anywhere; the token comes from the environment and never reaches the result files.
- This is where real Atlas and Render timings get measured. The result is labelled "demo deploy".

## Scenarios

| Letter | Name | What it does |
|---|---|---|
| a | `polling` | 3 phones polling Overview (`/api/status`) and Activity on a cadence with jitter, plus `/healthz` |
| b | `auth` | 12 wrong 2FA codes (lockout at the 5th, a correct code refused while locked), then a burst of wrong-password logins (per-IP limit); Stop and status still work during it |
| c | `analytics` | summary, ledger, returns (day/week/month), returns fills, scorecard at 180 days: cold, warm, then concurrent with distinct time zones (cache keys) |
| d | `stop` | 200 Stop calls, 50 at a time |
| e | `push` | 30 concurrent sign-ups against the 10-subscription cap, then 10 concurrent re-sign-ups of one device |
| f | `soak` | the runner loop for `--soak-seconds`: a cron tick each second (a fake pass runs whenever none is running), a phone polling, memory sampled twice a second |
| g | `contention` | per round: Start (fresh 2FA), a 180-day Analytics rebuild (new time zone and day count so the cache misses), a tick starts a pass, then 20 concurrent Stops while the pass and the rebuild are in flight and phones poll; then checks the flags, the cancels and that no new pass starts |

`all` runs them in the order polling, analytics, push, auth, stop, contention, soak (auth goes late because it
exhausts the per-IP login buckets).

## Pass bars

| Bar | How it is checked |
|---|---|
| Stop p99 under 1 s, also under load | `stop` (200 calls) and `contention` (all Stops pressed during a pass plus a rebuild) |
| No 5xx and no transport errors | every scenario |
| Rate limits answer 429, never an error | `auth`: lockout at the 5th bad code, per-IP login limit; Analytics 429s must be `rate_limited` or `analytics_busy` |
| Flags set first; only `mm-` orders cancelled | `contention`: `desired=stopped`, kill switch present when Stop returned; the manual order is untouched; no new pass after Stop; never two passes at once |
| No memory growth over the soak | mean RSS of the first quarter after warm-up vs the last quarter; tolerance the larger of 25 MB or 10 % of the start |
| Peak memory under 512 MB | peak RSS of the server process across `analytics` and `contention` |
| Push cap | exactly 10 of 30 concurrent sign-ups accepted, the rest `409 too_many_subscriptions` |

### Limits of what this proves

- Memory is the server process's resident size on the machine you ran it on (Windows working set or Linux RSS), not a
  Render container. To enforce 512 MB for real, run the server in Docker (`docker run --memory=512m python -m loadtests.server ...`) and watch whether the container is OOM-killed while the
  suite runs (the harness does not drive a containerised server yet: an open item).
- The local server has a `MemoryStore`: no network hop and no Atlas limits. `MemoryStore` reads are faster than
  Atlas, so cold-build times here are a floor.
- The push limiter (10 per minute) is raised on the local server so the test exercises the store cap; the limiter is
  covered by the `control_api` tests.
- The fake pass sleeps; it does not exercise the strategy code. The Stop path, the runner locks, the executors, the
  analytics build gate and the post-pass warm-up are the real code.
- Not covered yet (the T1 list): the single build gate's 8 s busy wait under a long build, a release restart or Render
  sleep in the middle of a pass, the stale-app banner fetch, and Atlas M0 connection limits. The last three need the
  demo deploy or a container.

## Results

Each run writes `results-<UTC timestamp>-<local-memorystore|demo-deploy>.json` and `.md` here. They hold timings,
counts and pass/fail only (no tokens, keys or codes; the writer refuses a result that contains one). Commit them with
the release they measured. Run before the demo deploy and again after each large release (T2).

`load-test.yml.proposed` is a manual (`workflow_dispatch`) GitHub Actions job for the local suite. It is a proposal:
the owner or lead copies it to `.github/workflows/` when wanted.

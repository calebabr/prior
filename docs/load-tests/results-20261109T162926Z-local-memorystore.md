# Load test results: local MemoryStore

- Run: 2026-11-09T16:29:26+00:00 (215.8 s), target: **local**
- Environment: Python 3.11.9, Windows-10-10.0.26300-SP0, git 8985e4c
- Store: local MemoryStore; fake Kalshi and a fake 5.0 s bot pass; 20000 synthetic fills over 180 days
- Overall: **FAIL**

| Scenario | Result | Checks passed |
|---|---|---|
| polling | PASS | 2/2 |
| analytics_180 | PASS | 5/5 |
| push_race | PASS | 5/5 |
| auth_burst | PASS | 9/9 |
| stop_storm | PASS | 5/5 |
| contention | FAIL | 25/26 |
| soak | PASS | 6/6 |

## Pass bars

| Bar | Result | Evidence |
|---|---|---|
| Stop p99 under 1 s, also under load | FAIL | contention: Stop p99 under 1 s (p99 5201.6 ms (max 5201.6)) |
| No 5xx and no transport errors | PASS | 8 check(s) in analytics_180, auth_burst, contention, polling, push_race, soak, stop_storm |
| Rate limits answer 429, never an error | PASS | 9 check(s) in analytics_180, auth_burst, contention |
| Flags set first; only mm- orders cancelled | PASS | 3 check(s) in contention |
| No memory growth over the soak (tolerance stated in the check) | PASS | 1 check(s) in soak |
| Peak memory under the 512 MB budget | PASS | 1 check(s) in contention |
| Push cap holds under a sign-up race | PASS | 1 check(s) in push_race |

## polling (pass)

- [x] polling: no 5xx and no transport errors
- [x] every poll answered 200

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| poll_status | 16 | 796.5 | 3529.1 | 3529.1 | 3529.1 | 0 | 0 |
| poll_activity | 9 | 939.7 | 1362.8 | 1362.8 | 1362.8 | 0 | 0 |
| poll_healthz | 4 | 5.5 | 11.6 | 11.6 | 11.6 | 0 | 0 |
| all | 29 | 738.4 | 3520.0 | 3529.1 | 3529.1 | 0 | 0 |

## analytics_180 (pass)

- [x] analytics: no 5xx and no transport errors
- [x] every answer is 200 or 429
- [x] every 429 is rate_limited or analytics_busy
- [x] cold 180-day summary and ledger returned 200
- [x] memory stays under the 512 MB budget

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| parallel | 24 | 4201.7 | 11608.8 | 12180.3 | 12180.3 | 8 | 0 |
| all | 52 | 740.8 | 11607.9 | 12180.3 | 12180.3 | 8 | 0 |
- peak_rss_mb: 189.8

## push_race (pass)

_the server runs with the push_subscribe limiter raised so this tests the store cap; the limiter itself (10/min) is covered by the control_api tests_
- [x] push sign-ups: no 5xx and no transport errors
- [x] exactly 10 of 30 concurrent sign-ups accepted
- [x] the rest answered 409 too_many_subscriptions
- [x] stored count is exactly 10
- [x] re-subscribing an existing device at the cap stays 200 and the count stays

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| signups | 30 | 70.9 | 81.8 | 84.1 | 84.1 | 0 | 0 |
| duplicate_device | 10 | 26.0 | 37.5 | 37.5 | 37.5 | 0 | 0 |

## auth_burst (pass)

- [x] login with the right password gives an mfa token
- [x] bad 2FA codes: no 5xx
- [x] lockout: 429 from the 5th wrong code
- [x] 429 carries Retry-After
- [x] a correct code is refused while locked (429)
- [x] login burst: no 5xx and no transport errors
- [x] per-IP limit: 429 after the allowance
- [x] allowance not exceeded (<= 10 wrong passwords answered 401)
- [x] Stop and status still work while auth is rate limited

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| login_burst | 25 | 76.9 | 537.7 | 606.1 | 606.1 | 16 | 0 |

## stop_storm (pass)

- [x] stop: no 5xx and no transport errors
- [x] every Stop answered 200
- [x] Stop p99 under 1 s
- [x] status reports stopped
- [x] kill-switch file exists

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| stop | 200 | 256.4 | 418.5 | 636.2 | 720.8 | 0 | 0 |

## contention (fail)

- [x] round 1: Start (fresh 2FA) accepted
- [x] round 1: the pass was in flight when Stop landed
- [x] round 1: Analytics was still building when Stop landed (needs the 20,000-row size)
- [x] round 1: flags set (desired=stopped, kill_switch) when Stop returned
- [x] round 1: only mm- orders cancelled, the manual order untouched
- [x] round 1: no new pass after Stop
- [x] round 1: Analytics answered 200 or 429 only
- [x] round 2: Start (fresh 2FA) accepted
- [x] round 2: the pass was in flight when Stop landed
- [x] round 2: Analytics was still building when Stop landed (needs the 20,000-row size)
- [x] round 2: flags set (desired=stopped, kill_switch) when Stop returned
- [x] round 2: only mm- orders cancelled, the manual order untouched
- [x] round 2: no new pass after Stop
- [x] round 2: Analytics answered 200 or 429 only
- [x] round 3: Start (fresh 2FA) accepted
- [x] round 3: the pass was in flight when Stop landed
- [x] round 3: Analytics was still building when Stop landed (needs the 20,000-row size)
- [x] round 3: flags set (desired=stopped, kill_switch) when Stop returned
- [x] round 3: only mm- orders cancelled, the manual order untouched
- [x] round 3: no new pass after Stop
- [x] round 3: Analytics answered 200 or 429 only
- [x] contention: no 5xx and no transport errors
- [x] every Stop answered 200
- [ ] Stop p99 under 1 s (p99 5201.6 ms (max 5201.6))
- [x] never two passes at once
- [x] peak memory under 512 MB

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| stop | 60 | 3073.4 | 5195.3 | 5201.6 | 5201.6 | 0 | 0 |
- peak_rss_mb: 187.4

## soak (pass)

- [x] Start accepted
- [x] soak: no 5xx and no transport errors
- [x] Stop at the end answered 200
- [x] the runner kept running passes
- [x] never two passes at once
- [x] memory growth within tolerance (25 MB or 10%)

| Calls | n | p50 ms | p95 ms | p99 ms | max ms | 429 | 5xx |
|---|---|---|---|---|---|---|---|
| requests | 186 | 132.3 | 436.3 | 731.4 | 1805.1 | 0 | 0 |
- peak_rss_mb: 184.6
- growth: {'first_mb': 179.9, 'last_mb': 173.6, 'growth_mb': -6.3, 'growth_pct': -3.5, 'samples': 391}
- tolerance_mb: 25.0
- passes_finished: 25

Peak memory is the server process RSS (local runs only). The Render instance limit is 512 MB; a local RSS is an estimate of that, not a measurement of it. Atlas and Render timings are measured only by running this against the demo deploy.

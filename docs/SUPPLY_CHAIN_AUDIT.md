# Supply-chain audit

- **Date:** 2026-11-09
- **Scope:** `control_api/requirements*.txt` (runtime and dev locks), `.github/semgrep/requirements.txt`,
  `control_app/package.json` + `package-lock.json`, `Dockerfile`, `render.yaml`, `scripts/render_build.sh`,
  `scripts/fetch_private_overlay.sh`, `control_app/vercel.json`, `.github/workflows/*.yml`, `.github/dependabot.yml`,
  `.githooks/*`, `scripts/install_hooks.sh`, `deploy/vm/*` (install instructions only).
- **Commit:** `f73b83d` on `feat/sprint-3-wave-a` (working tree, read-only review).
- **Context used for severity:** single owner, Render free web service (one worker, 0.1 CPU), Vercel Hobby, Atlas M0,
  demo money only until the live date 2026-11-24.

## Method

| Step | Tool | Result |
|---|---|---|
| npm advisories, full tree | `npm audit --package-lock-only` (npm 11.6.2, already installed; reads the lockfile, installs nothing) | ran |
| npm advisories, production only | `npm audit --package-lock-only --omit=dev` | ran: 0 vulnerabilities |
| npm install scripts, non-registry sources, missing integrity | lockfile walk with `node` (read-only) | ran |
| PyPI advisories for every pinned version | OSV `querybatch` API with a stdlib script (same data source `pip-audit` uses by default) | ran over all 35 runtime, 43 dev and 66 semgrep-CI pins |
| Direct-dependency risk signals (maintainers, staleness, archived repos) | `supply-chain-risk-auditor` collector (stdlib only) | see "Collector" below |
| Actions pinning, permissions, triggers | manual review of `.github/workflows` | ran |
| Hooks and tool pinning | manual review of `.githooks`, workflows | ran |
| `pip-audit` | not installed; not installed for this audit (no new installs allowed) | replaced by the OSV query above |
| `gh` CLI | not installed; GitHub repository criteria in the collector ran unauthenticated | partial |
| Container image scan (Trivy/Grype) | not installed | not run |

## Findings

| ID | Severity | Confidence | File | Finding | Exploitability here | Recommended fix |
|---|---|---|---|---|---|---|
| SC-1 | **High** | High | `control_api/requirements.txt` (`certifi==2026.7.22`, via httpx, httpcore, requests) | GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320 (rated critical, published 2026-10-20, fixed in 2026.10.18): the bundled CA store trusts the `emSign ECC Root CA - G3` root, under which eight intermediates have compromised private keys. Anyone holding one of those keys can mint a certificate for any hostname. The same pin is in `requirements-dev.txt` and `.github/semgrep/requirements.txt`. | The runtime reaches Kalshi through `requests` and cron-job.org / ntfy / self-ping through `httpx`; both trust `certifi`. An attacker who is on-path between Render and Kalshi **and** holds a compromised key can read and alter API traffic. A Kalshi request signature covers timestamp, method and path, not the body, so an on-path attacker could alter an order body inside the signature's time window. Likelihood is low (needs network position plus a leaked key), impact from 11/24 is real money. | Recompile both locks with `--exclude-newer` on or after 2026-11-01 (the fix is older than the 14-day window) so `certifi>=2026.10.18` is pinned (latest seen: 2026.10.23). Same bump in `.github/semgrep/requirements.txt`. Owner: infra. **Must land before the live gate.** |
| SC-2 | Medium | Medium (the call path was not verified) | `control_api/requirements.txt` (`opentelemetry-api==1.45.0`, a hard dependency of `fastapi==0.143.0`) | GHSA-xcq7-p3pf-p88h (moderate, fixed in 1.45.1): `TraceState.from_header` has quadratic regex cost; a ~100 KB `tracestate` header holds a worker for ~6-12 s. | Only exploitable if FastAPI or the app parses inbound W3C trace headers. The app does not import `opentelemetry`; whether FastAPI 0.143 extracts trace context on every request was **not verified** (its source was not downloaded). If it does, one unauthenticated request blocks the single Render worker (0.1 CPU, event loop shared with the runner), which would delay a Stop request. | Bump to `opentelemetry-api>=1.45.1` (latest 1.45.2) in the same recompile as SC-1. Separately, check FastAPI 0.143 for trace-context extraction and, if present, have uvicorn/proxy cap header size. Owner: infra (lock), backend (verification). |
| SC-3 | Medium | High | `.github/workflows/tests.yml` (python job), `.github/dependabot.yml` | Python dependencies have no advisory gate in CI. The frontend job runs `npm audit --omit=dev --audit-level=low`; the python job installs the hash lock but never checks it against an advisory database. Dependabot version updates are monthly, so a critical advisory (SC-1, 20 days old) can sit unnoticed until the next monthly run. | Process gap, not an exploit by itself; it is the reason SC-1 and SC-2 are still pinned. | Add a `pip-audit` step (hash-locked install in `.github/semgrep/requirements.txt` style, `pip-audit -r control_api/requirements.txt --require-hashes --disable-pip`, or an OSV query against the lock). Turn on Dependabot **security** updates in repo settings (separate from the monthly version updates). Owner: infra. |
| SC-4 | Low | High | `control_api/requirements.txt` (`dnspython==2.8.0`, via pymongo) | GHSA-45c3-73f7-pv4m (high, fixed in 2.9.0): crafted DNS wire messages cause excessive CPU in name decompression. | pymongo uses dnspython only to resolve the `mongodb+srv` SRV/TXT records at connect time. Triggering it requires forging DNS answers to Render's resolver (on-path) or controlling Atlas's DNS. Low likelihood; impact is a slow connect. | Bump to `dnspython>=2.9.0` (latest 2.9.2) in the SC-1 recompile. Owner: infra. |
| SC-5 | Low | High | `render.yaml` (`PYTHON_VERSION: 3.11.9`) | The production interpreter is pinned to CPython 3.11.9 (April 2024). Later 3.11 security releases fix stdlib issues (for example email header injection, `http.cookies` and `urllib.parse` parsing, `tarfile`/`zipfile` handling). | The app does not unpack archives or parse untrusted cookies on the hot path; the exposed surface is small. Pinning is correct, the pin is just stale. | Move to the newest 3.11.x security release Render supports, run the full suite, release as an app-only change. Owner: infra. |
| SC-6 | Low | High | `control_app/vercel.json` (no `installCommand`), `.github/workflows/tests.yml` (`npm ci` without `--ignore-scripts`) | Vercel's default install for npm projects runs install scripts and is not guaranteed to be `npm ci`; CI runs `npm ci` with scripts enabled. The lockfile has exactly one package with an install script, `fsevents@2.3.3` (dev, macOS-only optional; skipped on Linux). | Today no install script executes on Linux, so nothing runs. A future dependency that adds a postinstall would run on Vercel's build with access to the build environment (no secrets are stored in Vercel for this app). | Set `"installCommand": "npm ci --ignore-scripts"` in `vercel.json` and use `npm ci --ignore-scripts --no-audit --no-fund` in CI; this is viable because no Linux package needs an install script (Vite 8's native bindings arrive as optional platform packages). Re-check `npm run build` and `npx playwright install` after the change. Owner: infra. |
| SC-7 | Low | High | `render.yaml` (`PRIVATE_REPO_REF: main`), `scripts/fetch_private_overlay.sh` | The private strategy overlay is fetched from a mutable branch at build time, and the script does not print the private commit it built. The release workflow approves an exact public SHA, but the private half of the build is whatever is on the private `main` when Render builds. | Single owner, read-only token, so this is an integrity/traceability gap rather than an external attack: an unreviewed push to the private `main` between approval and build would ship unapproved strategy code. | Set `PRIVATE_REPO_REF` to a tag or full SHA per release (the script already accepts both), and print the resolved private commit SHA (a hash only, nothing private) in the build log. Owner: infra. |
| SC-8 | Low | High | `control_app/package-lock.json` (dev tree) | `npm audit` reports 9 advisories, all in dev/build dependencies: `undici` 7.30.0 (high, via jsdom; tests only), `fast-uri` 3.1.8 (moderate, via ajv), `glob` 11.1.0 and `minimatch` 10.2.6 / 5.1.9 (moderate), `workbox-build` 7.4.1 and `vite-plugin-pwa` (moderate, inherited), `serialize-javascript` 7.1.2, `deepmerge` 4.3.1, `@rollup/plugin-node-resolve` (low). Production tree (`react`, `react-dom`, `react-router`, 9 packages): 0 advisories. | None of these ship to the phone. `undici` runs only under Vitest. `workbox-build` / `serialize-javascript` run at build time on the project's own service-worker config, not on attacker input. | `npm audit fix` (non-breaking) clears `undici`, `fast-uri` and `serialize-javascript` in the lock. Ignore npm's suggested `vite-plugin-pwa@1.1.0` "fix": it is a downgrade; wait for a workbox release that moves off the flagged `glob`. Owner: frontend. |
| SC-9 | Low | High | `.github/semgrep/requirements.txt` (`mcp==1.29.0`, `opentelemetry-api==1.37.0`, `certifi`) | The CI-only semgrep install carries `mcp` 1.29.0 (4 moderate advisories, fixed in 1.30.0), the SC-2 opentelemetry advisory and the SC-1 certifi advisory. | CI job has `contents: read`, no secrets, scans repository code only; semgrep does not use its MCP client in `semgrep scan`. | Bump semgrep and regenerate the hash lock (Dependabot group `semgrep-ci`). Owner: infra. |
| SC-10 | Info | High | `Dockerfile` (`FROM python:3.11-slim`, twice) | Base image is a floating tag, not a digest; `apt-get install git` in the overlay stage is unpinned. | The Dockerfile is the VM fallback; production builds on Render's native runtime and never uses it. The apt stage is discarded. | Pin `python:3.11-slim@sha256:<digest>` when the Docker path is used; Dependabot can then track the `docker` ecosystem. Owner: infra. |
| SC-11 | Info | High | `.githooks/pre-commit`, `.githooks/pre-push` | Local hooks run whatever `gitleaks` is on PATH (no version check; the pre-commit falls back to the older `protect` subcommand). The pre-push can be bypassed with `SKIP_GITLEAKS_LOCAL=1`. CI pins gitleaks 8.30.1 with a sha256 check, so the server-side scan is pinned. | Local-only; CI is the enforcing gate. | Optional: have the hooks warn when `gitleaks version` is not 8.30.1. Owner: infra. |
| SC-12 | Info | High | `.github/workflows/semgrep.yml`, `tests.yml` (e2e) | Semgrep rule packs (`p/python` etc.) and Playwright's Chromium are fetched at run time without a pinned digest. | Both run in jobs with `contents: read` and no secrets. Rule drift is documented in the workflow header. | Accept; documented. |

Severity counts: Critical 0, High 1, Medium 2, Low 6, Info 3.

## Verified safe

- **GitHub Actions:** every `uses:` across the four workflows is pinned to a full 40-character SHA (`actions/checkout`,
  `actions/setup-python`, `actions/setup-node`; no other third-party actions). Top-level permissions are `contents: read`
  (`tests`, `semgrep`, `gitleaks`) or `{}` (`release`); write scopes exist only in the two release jobs that need them.
  No `pull_request_target`, no `schedule`, every checkout uses `persist-credentials: false`. Release secrets sit behind
  the `production` environment and reach scripts only through `env:`.
- **gitleaks in CI:** version 8.30.1 downloaded from the official release and checked against a pinned sha256 before use.
- **semgrep in CI:** installed from a hash-locked file, wheels only, `--no-deps`; all 66 pins carry hashes. Metrics off.
- **Python runtime install:** `control_api/requirements.txt` is a `uv`-compiled universal lock with hashes for every
  one of the 35 pins; Render (`scripts/render_build.sh`), Docker and CI all install with `--require-hashes
  --only-binary :all: --no-deps` (no sdist build scripts run, no unlocked transitive packages), followed by `pip check`.
  The dev lock is constrained to the runtime lock and CI fails if their pins diverge. `--exclude-newer` keeps releases
  younger than 14 days out (fresh-compromise window).
- **npm lockfile:** lockfileVersion 3, 481 packages, every one resolved from `https://registry.npmjs.org/` with an
  `integrity` hash; no git, tarball-URL or `file:` sources. Production tree is 9 packages with 0 advisories. One install
  script in the whole tree (`fsevents`, dev, macOS-only).
- **Dependabot:** configured for pip (runtime and semgrep), npm and github-actions, grouped, nothing auto-merges.
- **Private overlay fetch:** token passed through a throwaway `GIT_ASKPASS` helper, never on a command line or URL;
  ref validated against a character allow-list; global and system git config disabled for the fetch; the clone is
  deleted on exit; a token that fails to produce an overlay fails the build instead of falling back silently.
- **Docker runtime:** non-root `app` user, git absent from the final image, `.dockerignore` is an allow-list that
  excludes keys, env files, state and tests.

## Collector

The `supply-chain-risk-auditor` collector read `control_api/requirements.txt` and `requirements-dev.txt` (44 packages,
all pins, 36 reaching production). Results it measured:

- Known advisories: assessed 44/44, flagged 3 (`certifi` 2026.7.22, `dnspython` 2.8.0, `opentelemetry-api` 1.45.0),
  matching the OSV query above.
- Deprecated or yanked: assessed 44/44, flagged 0.
- Repository archived / maintenance activity: assessed 19/44, flagged 0; 25 not assessable (unauthenticated GitHub API
  rate limit).
- OpenSSF Scorecard: assessed 13/44; one flag, `cffi` Binary-Artifacts 9/10 (upstream repository hygiene, no action).
- Publisher concentration, install-time scripts, provenance, download volume: not assessable for PyPI (no upload ACL,
  counters disabled; install-time execution is closed off here by `--only-binary :all:`).

For `control_app` (13 direct npm dependencies, 3 in production; 466 further lockfile packages swept for advisories):

- Known advisories on direct dependencies: assessed 13/13, flagged 0. Transitive: 6 packages flagged, all labelled
  build-time only (`deepmerge`, `fast-uri`, `glob`, `minimatch`, `serialize-javascript`, `undici`), matching SC-8.
- Deprecated or yanked: 13/13 assessed, 0 flagged. Repository archived / maintenance: 9/13 assessed, 0 flagged.
- Install-time script execution on direct dependencies: 13/13 assessed, 0 flagged.
- Publish provenance: 13 of 13 publish with build provenance. Publisher concentration: 0/13 assessable (CI publishing
  with provenance; the effective publisher set is not externally observable).
- Lowest weekly download volume among direct dependencies: `vite-plugin-pwa` (7,395,066/wk).
- Scorecard: `@playwright/test` Binary-Artifacts 6/10 (upstream hygiene, dev only).

`kalshi_bot/` (public stand-in) has no manifest of its own; it runs on the `control_api` lock, so the collector
refused to report on it (no dependencies to assess).

## Not checked

- Contents of any dependency's source code (no wheel or tarball was downloaded or unpacked). In particular, whether
  FastAPI 0.143 parses inbound `tracestate` headers (SC-2) is unverified.
- Container or OS-package vulnerabilities of Render's native Python image and of `python:3.11-slim` (no Trivy/Grype).
- Vercel's actual install command and Node version (not visible from the repo; `package.json` has no `engines`).
- GitHub repository settings: branch protection, required checks, Dependabot security updates, secret scanning, the
  `production` environment's branch rule. These are dashboard settings and could not be read without `gh`.
- npm publisher/maintainer data beyond what the collector measured; PyPI publishes no maintainer ACL.
- License compliance.
- The private repository's dependencies (it adds no packages to the runtime lock; not re-verified here).

## Go / no-go (supply-chain dimension of the live gate)

**Conditional GO.** The pipeline itself is sound: hash-locked wheels-only installs, SHA-pinned actions, least-privilege
workflows, a clean production npm tree. One item blocks: **SC-1 (certifi trusting compromised CA intermediates on the
path that signs and sends Kalshi orders) must be fixed by recompiling the locks before 2026-11-24**, ideally together
with SC-2 and SC-4 in one `chore(deploy)` lock refresh. SC-3 (a Python advisory gate in CI) is recommended before live
so the next advisory of this kind is caught within days instead of a month. Everything else can follow the release.

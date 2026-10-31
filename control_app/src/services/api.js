// The single HTTP client for the control API (see DASHBOARD_PLAN.md "REST endpoints").
//
// - Base URL: import.meta.env.VITE_API_BASE_URL (the Render service).
// - Adds `Authorization: Bearer <token>` and, for 2FA-protected calls, `X-2FA-Code`.
// - On 401 (for an authenticated call) it clears the token and calls the
//   unauthorized handler (AuthContext sends the user to /login).
// - If any request takes longer than 5 s, slow-listeners are told so the app can
//   show "Waking up the server (free tier, up to a minute)…".
// - GET responses are cached in sessionStorage (services/cache.js) for offline display.
// - VITE_MOCK=1 routes every request to services/mock.js instead of the network.

import { writeCache, clearCache } from './cache.js';
import { detailList } from '../utils/format.js';

export const SLOW_MS = 5000;
export const TIMEOUT_MS = 90000; // a cold Render start can take ~30–60 s
const TOKEN_KEY = 'km.token';

// ---------------------------------------------------------------- errors

// S-15: with MongoDB Atlas unreachable every authenticated endpoint except Stop
// returns 503 {detail: "store_unavailable"}.
export const STORE_DOWN = 'store_unavailable';
export const STORE_DOWN_TEXT = "The server can't reach its database. Stop still works.";
// Caller-cancelled request (opts.signal aborted). Not a failure; never shown to the user.
export const ABORTED = 'aborted';
export const RATE_LIMITED = 'rate_limited';
// Server cap on /api/analytics/trades.csv rows (docs/ANALYTICS_CONTRACT.md item 7).
export const CSV_ROW_CAP = 5000;

// 429 rate_limited -> "Too many requests — try again in N s" (N from Retry-After when given).
export function rateLimitText(retryAfter) {
  return typeof retryAfter === 'number' && retryAfter > 0
    ? `Too many requests — try again in ${retryAfter} s.`
    : 'Too many requests — try again shortly.';
}

function retryAfterSeconds(res) {
  try {
    const v = res && res.headers && typeof res.headers.get === 'function' ? res.headers.get('Retry-After') : null;
    if (v == null || v === '') return null;
    const n = Number(v);
    if (Number.isFinite(n)) return Math.max(0, Math.ceil(n));
    const at = Date.parse(v); // HTTP-date form
    return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - Date.now()) / 1000));
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  constructor(status, detail, message) {
    super(
      message ||
        (status === 503 && detail === STORE_DOWN ? STORE_DOWN_TEXT : '') ||
        detailList(detail).join('; ') ||
        (status ? `HTTP ${status}` : 'Network error'),
    );
    this.name = 'ApiError';
    this.status = status; // 0 = network error / timeout
    this.detail = detail;
  }
  get details() {
    return detailList(this.detail);
  }
  get isNetwork() {
    return this.status === 0;
  }
  get aborted() {
    return this.detail === ABORTED;
  }
  get is2fa() {
    return this.status === 403 && (this.detail === '2fa_required' || this.detail === '2fa_invalid');
  }
}

// ---------------------------------------------------------------- token

let token = null;
let tokenLoaded = false;

function session() {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
  } catch {
    return null;
  }
}

export function getToken() {
  if (!tokenLoaded) {
    tokenLoaded = true;
    const s = session();
    token = s ? s.getItem(TOKEN_KEY) : null;
  }
  return token;
}

export function setToken(t) {
  token = t || null;
  tokenLoaded = true;
  const s = session();
  if (!s) return;
  if (token) s.setItem(TOKEN_KEY, token);
  else s.removeItem(TOKEN_KEY);
}

export function clearToken() {
  setToken(null);
  clearCache();
  setStoreDown(false);
}

let unauthorizedHandler = null;
export function onUnauthorized(fn) {
  unauthorizedHandler = fn;
  return () => {
    if (unauthorizedHandler === fn) unauthorizedHandler = null;
  };
}

// ---------------------------------------------------------------- slow indicator

const slowListeners = new Set();
let slowCount = 0;

function setSlow(delta) {
  slowCount = Math.max(0, slowCount + delta);
  const slow = slowCount > 0;
  slowListeners.forEach((fn) => fn(slow));
}

export function subscribeSlow(fn) {
  slowListeners.add(fn);
  fn(slowCount > 0);
  return () => slowListeners.delete(fn);
}

// ---------------------------------------------------------------- database-down indicator
// Set by any 503 store_unavailable; cleared by the next successful response from an
// endpoint that needs the store (i.e. anything except Stop, which works without it).

const storeListeners = new Set();
let storeDown = false;

function setStoreDown(v) {
  if (storeDown === v) return;
  storeDown = v;
  storeListeners.forEach((fn) => fn(v));
}

export function subscribeStoreDown(fn) {
  storeListeners.add(fn);
  fn(storeDown);
  return () => storeListeners.delete(fn);
}

export function isStoreDown() {
  return storeDown;
}

// ---------------------------------------------------------------- transport

export function isMockMode() {
  return import.meta.env.VITE_MOCK === '1';
}

export function baseUrl() {
  return String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
}

async function transport(method, path, init) {
  // The literal env check (not isMockMode()) lets the production build drop the mock chunk entirely (S-49):
  // Vite inlines VITE_MOCK at build time, so in a normal build this branch is dead code.
  if (import.meta.env.VITE_MOCK === '1') {
    const mock = await import('./mock.js');
    return mock.mockFetch(method, path, init);
  }
  return fetch(baseUrl() + path, {
    method,
    headers: init.headers,
    body: init.body,
    signal: init.signal,
    credentials: 'omit',
    cache: 'no-store',
  });
}

/**
 * request(method, path, { body, code, auth, cache, signal })
 *  - path: starts with "/api/..." or "/healthz"
 *  - code: 6-digit TOTP code -> X-2FA-Code header
 *  - auth: send the Bearer token (default true)
 *  - cache: write the response to sessionStorage (default: GET only); cacheKey: store it under this key, not the path
 *  - raw: return {text, header(name)} instead of parsed JSON (non-JSON downloads, never cached)
 *  - signal: optional AbortSignal from the caller. Aborting it cancels the request and
 *    rejects with an ApiError whose `aborted` is true (callers should ignore it).
 *    Without a signal the behaviour is unchanged.
 */
export async function request(method, path, opts = {}) {
  const { body, code, auth = true, signal: callerSignal } = opts;
  if (callerSignal && callerSignal.aborted) throw new ApiError(0, ABORTED, 'Request cancelled.');
  const cache = opts.cache ?? method === 'GET';
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const t = auth ? getToken() : null;
  if (t) headers.Authorization = `Bearer ${t}`;
  if (code) headers['X-2FA-Code'] = String(code);

  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  let slowMarked = false;
  const slowTimer = setTimeout(() => {
    slowMarked = true;
    setSlow(+1);
  }, SLOW_MS);
  const abortTimer = controller ? setTimeout(() => controller.abort(), TIMEOUT_MS) : null;

  // Caller cancellation: abort the transport and reject at once (even if the
  // transport ignores the signal).
  let cancelled = false;
  let onCallerAbort = null;
  const cancelledP = callerSignal
    ? new Promise((_, reject) => {
        onCallerAbort = () => {
          cancelled = true;
          if (controller) controller.abort();
          reject(new ApiError(0, ABORTED, 'Request cancelled.'));
        };
        callerSignal.addEventListener('abort', onCallerAbort, { once: true });
      })
    : null;
  if (cancelledP) cancelledP.catch(() => {});

  let res;
  try {
    const p = transport(method, path, {
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller ? controller.signal : undefined,
    });
    res = cancelledP ? await Promise.race([p, cancelledP]) : await p;
  } catch (e) {
    if (cancelled) throw new ApiError(0, ABORTED, 'Request cancelled.');
    const timedOut = e && e.name === 'AbortError';
    throw new ApiError(
      0,
      null,
      timedOut ? 'The server did not answer in time. Try again.' : 'Cannot reach the server. Check your connection.',
    );
  } finally {
    clearTimeout(slowTimer);
    if (abortTimer) clearTimeout(abortTimer);
    if (slowMarked) setSlow(-1);
    if (onCallerAbort) callerSignal.removeEventListener('abort', onCallerAbort);
  }

  let data = null;
  let rawText = '';
  try {
    rawText = (await res.text()) || '';
    data = rawText ? JSON.parse(rawText) : null;
  } catch {
    data = null;
  }

  const detail = data && typeof data === 'object' ? data.detail : null;
  if (res.status === 401 && auth) {
    // Any 401 on a signed-in call (including S-15 "session_expired" after
    // "Log out all sessions") ends the session locally and returns to login.
    clearToken();
    if (unauthorizedHandler) unauthorizedHandler(detail);
    throw new ApiError(401, detail, 'Session expired. Please sign in again.');
  }
  if (cancelled) throw new ApiError(0, ABORTED, 'Request cancelled.');
  if (!res.ok) {
    if (res.status === 503 && detail === STORE_DOWN) setStoreDown(true);
    if (res.status === 429 && detail === RATE_LIMITED) {
      const err = new ApiError(429, detail, rateLimitText(retryAfterSeconds(res)));
      err.retryAfter = retryAfterSeconds(res);
      throw err;
    }
    const err = new ApiError(res.status, detail);
    if (res.status === 429 || res.status === 503) err.retryAfter = retryAfterSeconds(res);
    throw err;
  }
  if (auth && path !== '/api/bot/stop') setStoreDown(false);
  if (opts.raw) {
    // Non-JSON success body (e.g. CSV): text + the headers the caller needs. Never cached.
    const header = (name) => {
      try {
        return res.headers && typeof res.headers.get === 'function' ? res.headers.get(name) : null;
      } catch {
        return null;
      }
    };
    return { text: rawText, header };
  }
  if (cache) writeCache(opts.cacheKey || path, data);
  return data;
}

// Automatic retry for the two "try again in a moment" answers of the read-only analytics path: 429 analytics_busy
// (another build holds the gate) and 503 store_unavailable. Honors Retry-After (default 2 s, capped), adds jitter so
// several screens never retry in step, and gives up after `tries` retries by throwing the LAST error (the caller
// then shows its manual Retry). Every other error, including every other 4xx, is thrown at once.
export const RETRY_POLICY = { tries: 2, defaultS: 2, maxS: 15, jitterMs: 500 };
export const RETRYABLE = [
  { status: 429, detail: 'analytics_busy' },
  { status: 503, detail: STORE_DOWN },
];

export function isRetryable(err) {
  return !!err && RETRYABLE.some((r) => r.status === err.status && r.detail === err.detail);
}

function retryDelayMs(err) {
  const s = typeof err.retryAfter === 'number' && err.retryAfter >= 0 ? err.retryAfter : RETRY_POLICY.defaultS;
  return Math.min(s, RETRY_POLICY.maxS) * 1000 + Math.random() * RETRY_POLICY.jitterMs;
}

/** run() is called again after a retryable error. onRetry(attempt, err) fires before each wait. */
export async function withRetry(run, { signal, onRetry } = {}) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await run();
    } catch (err) {
      if (attempt >= RETRY_POLICY.tries || !isRetryable(err) || (signal && signal.aborted)) throw err;
      if (onRetry) onRetry(attempt + 1, err);
      await new Promise((resolve) => setTimeout(resolve, retryDelayMs(err)));
      if (signal && signal.aborted) throw err;
    }
  }
}

// 'attachment; filename="x.csv"' -> 'x.csv' (plain names only; no paths).
export function filenameFrom(disposition, fallback) {
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition || '');
  let name = '';
  if (m) {
    try {
      name = decodeURIComponent(m[1]);
    } catch {
      name = m[1];
    }
    name = name.replace(/[\\/:*?"<>|]+/g, '_').trim();
  }
  return name || fallback;
}

// ---------------------------------------------------------------- endpoints

const enc = encodeURIComponent;

export const api = {
  // Auth
  login: (username, password) =>
    request('POST', '/api/auth/login', { body: { username, password }, auth: false }),
  verify2fa: (mfa_token, code) =>
    request('POST', '/api/auth/2fa', { body: { mfa_token, code }, auth: false }),
  logout: () => request('POST', '/api/auth/logout'),
  // S-15: invalidates every token issued so far (all devices). Fresh 2FA.
  logoutAll: (code) => request('POST', '/api/auth/logout-all', { code, cache: false }),
  me: () => request('GET', '/api/auth/me', { cache: false }),

  // Status & control
  healthz: () => request('GET', '/healthz', { auth: false, cache: false }),
  // The plain call also sends the viewer's IANA time zone so Today's P&L starts at local midnight (the API ignores
  // an unknown name and uses UTC). Cached under the plain path so the offline fallback still finds it.
  status: ({ period } = {}) =>
    period
      ? request('GET', `/api/status${query({ period })}`, { cache: false })
      : request('GET', `/api/status${query({ tz: viewerTimeZone() })}`, { cacheKey: '/api/status' }),
  stop: () => request('POST', '/api/bot/stop'), // never needs 2FA
  start: (code) => request('POST', '/api/bot/start', { code }),
  runOnce: () => request('POST', '/api/bot/run-once'),
  setMode: (mode, confirm, code) => request('PUT', '/api/mode', { body: { mode, confirm }, code }),

  // Settings
  getConfig: () => request('GET', '/api/config'),
  putConfig: (patch, code) => request('PUT', '/api/config', { body: patch, code }),

  // Markets
  getMarkets: () => request('GET', '/api/markets'),
  setMarket: (series, enabled, code) =>
    request('PUT', `/api/markets/${enc(series)}`, { body: { enabled }, code }),

  // Data
  getActivity: (limit = 100) => request('GET', `/api/activity?limit=${enc(limit)}`),
  getHistory: (days = 30) => request('GET', `/api/history?days=${enc(days)}`),
  getHealth: () => request('GET', '/api/health'),
  runMonitor: () => request('POST', '/api/monitor/run'),
  getLogs: (lines = 200) => request('GET', `/api/logs?lines=${enc(lines)}`),
  getAudit: (limit = 100) => request('GET', `/api/audit?limit=${enc(limit)}`),

  // Shell (UX contract section 5): strategy registry (names + colours) and the What's New feed.
  // Both return a plain array (a {strategies: [...]} / {entries: [...]} wrapper is tolerated).
  getStrategies: () => request('GET', '/api/strategies').then((d) => (Array.isArray(d) ? d : (d && d.strategies) || [])),
  getChangelog: () => request('GET', '/api/changelog').then((d) => (Array.isArray(d) ? d : (d && d.entries) || [])),

  // FE-Settings (UX contract section 5)
  getConfigVersions: (limit = 5) => request('GET', `/api/config/versions?limit=${enc(limit)}`),
  undoConfig: (version, code) => request('POST', '/api/config/undo', { body: version != null ? { version } : {}, code }),
  getAlerts: ({ type, limit = 50, before } = {}) => request('GET', `/api/alerts${query({ type, limit, before })}`),
  getAlertSettings: () => request('GET', '/api/alerts/settings'),
  putAlertSettings: (patch, code) => request('PUT', '/api/alerts/settings', { body: patch, code }),
  pushPublicKey: () => request('GET', '/api/push/public-key', { cache: false }),
  pushSubscribe: (subscription) => request('POST', '/api/push/subscribe', { body: subscription, cache: false }),
  pushUnsubscribe: (endpoint) => request('DELETE', '/api/push/subscribe', { body: { endpoint }, cache: false }),
  pushSubscriptions: () => request('GET', '/api/push/subscriptions', { cache: false }),
  pushClearAll: () => request('DELETE', '/api/push/subscriptions', { cache: false }),
  pushTest: () => request('POST', '/api/push/test', { cache: false }),
  getAccountPositions: () => request('GET', '/api/account/positions').then((d) => (Array.isArray(d) ? d : (d && d.positions) || [])),

  // Server / keep-awake
  getCloud: () => request('GET', '/api/cloud'),
  putCloud: (patch, code) => request('PUT', '/api/cloud', { body: patch, code }),
  // Shadow estimate (docs/SHADOW_CONTRACT.md): simulated, read only, no orders. Never 2FA.
  getShadowSummary: ({ period } = {}) => request('GET', `/api/shadow/summary${query({ period })}`),
  getShadowEpisodes: ({ limit = 50, state } = {}) =>
    request('GET', `/api/shadow/episodes?limit=${encodeURIComponent(limit)}${state ? `&state=${encodeURIComponent(state)}` : ''}`),
  syncPinger: (code) => request('POST', '/api/cloud/pinger/sync', { code }),

  // Release approval (docs/RELEASE_PROCESS.md section 3). Never cached: a stale approval card would mislead.
  getReleasePending: () => request('GET', '/api/release/pending', { cache: false }),
  approveRelease: (id, code) => request('POST', `/api/release/pending/${enc(id)}/approve`, { code, cache: false }),
  rejectRelease: (id) => request('POST', `/api/release/pending/${enc(id)}/reject`, { cache: false }),

  // Bracket arbitrage (DASHBOARD_PLAN.md "Strategies run by the app")
  getArb: () => request('GET', '/api/arb'),
  // patch: any of {enabled, live, confirm:"LIVE", ack_legging, arb_max_contracts, arb_min_profit, arb_series, arb_poll_seconds, arb_scan_cap, arb_day_cap, arb_series_cap_frac, arb_demo_execute}
  putArb: (patch, code) => request('PUT', '/api/arb', { body: patch, code }),
  getArbOpportunities: (limit = 100) => request('GET', `/api/arb/opportunities?limit=${enc(limit)}`),

  // Periods and cash flows (Sprint 2A-1). Reads need a login; every write needs fresh 2FA.
  // Period ids are passed verbatim ("p1" is the implicit first period).
  getPeriods: () => request('GET', '/api/periods'),
  createPeriod: (name, note, code) =>
    request('POST', '/api/periods', { body: note ? { name, note } : { name }, code, cache: false }),
  addCashFlow: (flow, code) => request('POST', '/api/cash-flows', { body: flow, code, cache: false }),
  deleteCashFlow: (id, code) => request('DELETE', `/api/cash-flows/${enc(id)}`, { code, cache: false }),
  getPeriodSummary: (id, { env, source } = {}) =>
    request('GET', `/api/periods/${enc(id)}/summary${query({ env, source })}`, { cache: false }),
  getPnlBySource: ({ period, env } = {}) => request('GET', `/api/pnl/by-source${query({ period, env })}`, { cache: false }),

  // Analytics (docs/ANALYTICS_CONTRACT.md). Read-only, normal session, no 2FA.
  // env omitted -> the server uses the current bot mode; 'all' = demo + live.
  // Both take an optional AbortSignal (S-28: cancel superseded requests).
  analyticsSummary: ({ days = 30, env } = {}, { signal } = {}) =>
    request('GET', `/api/analytics/summary${query({ days, env, tz: viewerTimeZone() })}`, { signal }),
  // UI-1a: the honest ledger (per-strategy rows + reconciliation) and the scorecard. Read-only, no 2FA.
  analyticsLedger: ({ days = 30, env } = {}, { signal } = {}) =>
    request('GET', `/api/analytics/ledger${query({ days, env, tz: viewerTimeZone() })}`, { signal }),
  analyticsScorecard: ({ days = 7, env } = {}, { signal } = {}) =>
    request('GET', `/api/analytics/scorecard${query({ days, env })}`, { signal }),
  // Returns per day / week / month (docs/RETURNS_FEATURE_CONTRACT.md 4.1, 4.2). Read-only, no 2FA.
  analyticsReturns: ({ granularity = 'day', days = 30, env } = {}, { signal } = {}) =>
    request('GET', `/api/analytics/returns${query({ granularity, days, env, tz: viewerTimeZone() })}`, { signal }),
  analyticsReturnsFills: ({ granularity = 'day', start, end, days = 30, env, limit = 50, offset = 0 } = {}, { signal } = {}) =>
    request('GET', `/api/analytics/returns/fills${query({ granularity, start, end, days, env, limit, offset, tz: viewerTimeZone() })}`, { signal }),
  // params: days, env, series, side, kind, status, band, cell, result, limit, offset
  analyticsTrades: (params = {}, { signal } = {}) =>
    request('GET', `/api/analytics/trades${query(params)}`, { signal }),
  // CSV of all matching trades (server caps at 5,000; X-Truncated: true if more).
  // Fetched with the auth header (the token is not a cookie) -> {blob, filename, truncated}.
  analyticsTradesCsv: async (params = {}, { signal, mode } = {}) => {
    const { limit, offset, ...rest } = params; // eslint-disable-line no-unused-vars
    const r = await request('GET', `/api/analytics/trades.csv${query(rest)}`, { raw: true, cache: false, signal });
    // The server exposes Content-Disposition / X-Truncated via CORS expose_headers, so they are
    // used when readable. Fallback (older server / headers stripped): build the same name here,
    // and infer truncation from the row count reaching the server cap.
    const day = new Date().toISOString().slice(0, 10);
    const fallback = `trades-${day}.csv`;
    const flag = r.header('X-Truncated');
    const dataRows = r.text ? r.text.split(/\r?\n/).filter(Boolean).length - 1 : 0;
    return {
      blob: new Blob([r.text], { type: r.header('Content-Type') || 'text/csv;charset=utf-8' }),
      filename: filenameFrom(r.header('Content-Disposition'), fallback),
      truncated: flag != null ? String(flag).toLowerCase() === 'true' : dataRows >= CSV_ROW_CAP,
    };
  },
};

// {a: 1, b: '', c: null} -> "?a=1" (empty / null / undefined values are left out).
/** The device's IANA time zone name (e.g. "America/Chicago"), or undefined when it cannot be read. */
export function viewerTimeZone() {
  try {
    const z = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof z === 'string' && z.length > 0 && z.length <= 64 ? z : undefined;
  } catch {
    return undefined;
  }
}

export function query(params) {
  const parts = Object.entries(params || {})
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${enc(k)}=${enc(v)}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

export default api;

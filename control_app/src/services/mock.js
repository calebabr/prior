// Dev-only fake backend. Active when VITE_MOCK=1 (see api.js `transport`).
// Implements the DASHBOARD_PLAN.md v3 REST contract in memory so the UI can be
// previewed with no server. Nothing here talks to the network.
//
// Mock rules (to exercise error paths):
//   - login password "wrong"            -> 401
//   - 2FA code "000000"                 -> invalid
//   - reusing the previous 2FA code     -> 403 2fa_invalid (codes are single-use)
//   - any other 6-digit code            -> accepted

import { MOCK_CELLS, mockLedger, mockScorecard, mockArbSummary, analyticsSummary, analyticsTrades, analyticsTradesCsv, mockReturns, mockReturnsFills, mockMyTrades, mockTradeNote } from './mockAnalytics.js';
import { decorateActivity, resetMockUx, uxRoute } from './mockUx.js';

const SERIES = ['KXDEMOA', 'KXDEMOB', 'KXDEMOC', 'KXDEMOD', 'KXDEMOE', 'KXDEMOF'];

// The public stand-in's EXAMPLE cells (made up; read-only). Same as services/mockAnalytics.js.
const CELLS = MOCK_CELLS;

const ARB_SERIES = ['KXDEMOA', 'KXDEMOB', 'KXDEMOG', 'KXDEMOH'];

const ARB_SCHEMA = {
  arb_max_contracts: { type: 'number', unit: 'contracts per leg', min: 1, max: 100, help: 'Largest set the scanner will size an opportunity to. Each leg is a taker order.' },
  arb_min_profit: { type: 'number', unit: 'USD per set, after fees', min: 0.01, max: 10, step: 0.01, help: 'Ignore opportunities that clear less than this after taker fees.' },
  arb_poll_seconds: { type: 'integer', unit: 'seconds', min: 5, max: 60, help: 'Scan interval. Faster catches more short windows but uses more of the read rate limit.' },
  arb_series: { type: 'list', unit: 'series tickers', options: ARB_SERIES, help: 'Bracket events to scan.' },
  arb_scan_cap: { type: 'number', unit: 'USD per scan', min: 1, max: 100, help: 'Most money one scan may spend in demo-execute.' },
  arb_day_cap: { type: 'number', unit: 'USD per day', min: 1, max: 500, help: 'Most money arbitrage may spend per UTC day in demo-execute.' },
  arb_series_cap_frac: { type: 'number', unit: 'fraction of the open-exposure ceiling', min: 0.05, max: 0.9, step: 0.01, help: 'Most one series may hold in open exposure.' },
  arb_demo_execute: { type: 'bool', label: 'Demo Execute', help: 'Place found sets on the Kalshi demo account. Never live.' },
};

const CONFIG_SCHEMA = {
  run_budget: {
    type: 'number', unit: 'USD per run', min: 6, max: 500, editable: true,
    help: 'Max total $ of new bids per run, split evenly across series. Main profit lever. Keep <= ~50% of your balance.',
  },
  max_order: {
    type: 'number', unit: 'USD per order', min: 1, max: 1000, editable: true,
    help: 'Max $ per single order (the bot uses the smaller of this and run_budget / number of series). Recommended 5-25.',
  },
  max_open_frac: {
    type: 'number', unit: 'fraction of balance', min: 0.05, max: 0.5, step: 0.01, editable: true,
    help: 'Hard cap: $ at risk can never exceed this fraction of (balance + open positions). Recommended 0.2-0.5.',
  },
  poll_minutes: {
    type: 'integer', unit: 'minutes', min: 2, max: 14, editable: true,
    help: 'How often a pass runs. Capped at 14 on the cloud so a pass is always due before the server could sleep.',
  },
  series: {
    type: 'list', unit: 'series tickers', options: SERIES, editable: true,
    help: 'Series the strategy may quote (allowed series only).',
  },
  cells: {
    type: 'table', unit: '[min price, max price, min h, max h, side]', editable: false,
    help: 'THE VALIDATED STRATEGY - DO NOT EDIT. Read-only.',
  },
};

const CLOUD_SCHEMA = {
  keepalive_minutes: { type: 'integer', unit: 'minutes', min: 1, max: 14, help: 'Self-ping interval (service calls its own /healthz).' },
  pinger_minutes: { type: 'integer', unit: 'minutes', min: 1, max: 14, help: 'External cron-job.org job calling POST /api/tick.' },
  pinger_enabled: { type: 'bool', help: 'Enable the external cron-job.org pinger.' },
  shadow_capital: { type: 'number', unit: 'USD', min: 50, max: 1000, help: 'Size of the simulated account. Dollar P&L and the ceiling scale with it; rates and ROI do not.' },
  shadow_enabled: { type: 'bool', label: 'Shadow Estimate', help: 'Simulate the strategy from public market data. No orders are placed.' },
  silent_alert_minutes: { type: 'integer', unit: 'minutes', min: 15, max: 240, help: 'Send an ntfy alert if no heartbeat for this long.' },
};

const MIN = 60 * 1000;
const now = () => Date.now();
const iso = (t) => new Date(t).toISOString();

// ------------------------------------------------------------------ state

function seedJournal() {
  const rows = [];
  const tickers = {
    KXDEMOA: ['KXDEMOA-26OCT13-T83', 'KXDEMOA-26OCT13-B78.5'],
    KXDEMOB: ['KXDEMOB-26OCT13-B67.5'],
    KXDEMOC: ['KXDEMOC-26OCT13-T90'],
    KXDEMOD: ['KXDEMOD-26OCT13-B88.5'],
    KXDEMOE: ['KXDEMOE-26OCT13-T72'],
    KXDEMOF: ['KXDEMOF-26OCT13-B40.5'],
  };
  let t = now() - 6 * 60 * MIN;
  const events = ['quote', 'quote', 'quote', 'fill', 'cancel', 'quote', 'settle'];
  let i = 0;
  while (t < now() - 5 * MIN) {
    for (const s of SERIES) {
      const ticker = tickers[s][i % tickers[s].length];
      const ev = events[(i + s.length) % events.length];
      const cell = CELLS[(i + s.length) % CELLS.length];
      const price = +(cell[0] + ((i * 7 + s.length) % Math.max(1, Math.round((cell[1] - cell[0]) * 100))) / 100).toFixed(2);
      const cost = +(6.66 - (i % 3) * 0.5).toFixed(2);
      rows.push({
        utc: iso(t + rows.length * 900),
        event: ev,
        ticker,
        side: cell[4],
        price,
        count: +(cost / price).toFixed(2),
        cost,
        cell: JSON.stringify(cell),
        note: ev === 'settle' ? (i % 9 === 0 ? 'lost' : 'won') : ev === 'cancel' ? 'requote' : '',
      });
    }
    rows.push({
      utc: iso(t + 5000), event: 'account', ticker: '', side: '', price: null, count: null,
      cost: null, cell: '', note: 'cash=212.40 exposure=37.60',
    });
    t += 10 * MIN;
    i++;
  }
  return rows.reverse(); // newest first
}

function seedOpps() {
  const out = [];
  const evs = ['KXDEMOA-26OCT13', 'KXDEMOB-26OCT13', 'KXDEMOC-26OCT13', 'KXDEMOG-26OCT12', 'KXDEMOH-26OCT12'];
  for (let i = 0; i < 9; i++) {
    const contracts = [100, 40, 100, 12, 75, 100, 30, 100, 55][i];
    const profit = +[1.26, 0.31, 0.88, 0.07, 0.52, 2.04, 0.12, 0.64, 0.4][i].toFixed(2);
    out.push({
      ts: iso(now() - (i * 97 + 6) * MIN),
      event: evs[i % evs.length],
      kind: i % 4 === 3 ? 'YES' : 'NO',
      contracts,
      profit,
      cost: +(contracts * 5.1 - profit).toFixed(2),
      sum_best: +(1.0 + profit / contracts + 0.02).toFixed(4),
      legs: [],
      executed: 'log-only',
      seen_count: (i % 3) + 1,
    });
  }
  return out;
}

function arbState() {
  const a = S.arb;
  if (!a.settings.enabled || a.kill_switch) return 'stopped';
  return a.settings.live ? 'live' : 'log-only';
}

function arbResponse() {
  const a = S.arb;
  return {
    settings: { ...a.settings, arb_series: [...a.settings.arb_series] },
    schema: ARB_SCHEMA,
    ceilings: { arb_max_contracts: 100 },
    status: {
      state: arbState(),
      running: arbState() !== 'stopped',
      kill_switch: a.kill_switch,
      kill_reason: a.kill_reason,
      last_scan_utc: arbState() !== 'stopped' ? iso(now() - 9 * 1000) : null,
      last_error: a.last_error,
      live_allowed: S.live_allowed,
      scans: a.scans,
      found: a.opps.length,
      since_restart_utc: iso(now() - 3 * 60 * MIN),
    },
    pnl: null,
    arb_summary: mockArbSummary([], now()),
    expectation: 'Bracket arbitrage is rare and small at a small balance. Opportunities last seconds, so a 15-second scan catches only some of them. The maker strategy is the main earner.',
  };
}

function seriesStats(i) {
  const fills = [41, 37, 29, 33, 22, 58][i];
  const cost = +(fills * 8.7).toFixed(2);
  const roi = [0.018, 0.009, 0.021, -0.004, 0.013, 0.006][i];
  const half = 0.06 / Math.sqrt(fills / 10);
  return {
    fills, cost, pnl: +(cost * roi).toFixed(2), events: Math.round(fills * 0.7), roi,
    ci_low: +(roi - half).toFixed(4), ci_high: +(roi + half).toFixed(4),
  };
}

function buildSummary() {
  const per_series = {};
  SERIES.forEach((s, i) => (per_series[s] = seriesStats(i)));
  const all = Object.values(per_series);
  const fills = all.reduce((a, b) => a + b.fills, 0);
  const cost = +all.reduce((a, b) => a + b.cost, 0).toFixed(2);
  const pnl = +all.reduce((a, b) => a + b.pnl, 0).toFixed(2);
  const need = {};
  SERIES.forEach((s) => (need[s] = Math.max(0, 100 - per_series[s].fills)));
  return {
    generated_utc: iso(now() - 3 * 60 * MIN),
    window_days: 30,
    expected_roi: null,
    quoted_dollars: 4120.5,
    filled_dollars: cost,
    fill_rate: +(cost / 4120.5).toFixed(3),
    overall: { fills, cost, pnl, events: Math.round(fills * 0.7), roi: +(pnl / cost).toFixed(4), ci_low: 0.001, ci_high: 0.019 },
    per_series,
    actions: { pause: [], keep: [...SERIES], need_more: need, collateral_return: 'not yet: 200 more settled fills needed' },
    last_account: { cash: 212.4, exposure: 37.6 },
    note: 'mock data',
  };
}

function reportText(summary) {
  const o = summary.overall;
  const lines = [
    `Maker monitor  ${summary.generated_utc.slice(0, 16)} UTC   window: last ${summary.window_days} days`,
    `Quoted $${summary.quoted_dollars.toFixed(2)}   filled $${summary.filled_dollars.toFixed(2)}   fill rate ${(summary.fill_rate * 100).toFixed(1)}%`,
    `OVERALL  fills ${o.fills}  P&L $${o.pnl >= 0 ? '+' : ''}${o.pnl.toFixed(2)}  return ${(o.roi * 100).toFixed(2)}%  95% CI [${(o.ci_low * 100).toFixed(2)}%, ${(o.ci_high * 100).toFixed(2)}%]`,
    '',
  ];
  for (const [s, v] of Object.entries(summary.per_series)) {
    lines.push(`  ${s.padEnd(12)} fills ${String(v.fills).padStart(4)}  P&L $${v.pnl.toFixed(2).padStart(7)}  return ${(v.roi * 100).toFixed(2).padStart(6)}%`);
  }
  lines.push('', 'PAUSE:              none', `Collateral return:  ${summary.actions.collateral_return}`);
  return lines.join('\n');
}

function readScenario() {
  try {
    const raw = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('keel.mock') : '';
    return new Set(String(raw || '').split(',').map((x) => x.trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

// One pending release, requested 6 min ago with a 30 min window; an expired one lapsed 3 min ago.
function seedRelease(sc) {
  if (sc.has('release-none') || sc.has('release-off')) return null;
  const expired = sc.has('release-expired');
  const created = now() - (expired ? 33 : 6) * MIN;
  return {
    id: 'c0ffee00c0ffee00c0ffee00c0ffee00',
    version: '2026.10.16-2',
    commit: '9f3a1c7d2b8e4a6f0c5d7e1b3a9f2c4d6e8b0a1c',
    summary: 'Release gate and lease handover; phone app shows server role. No settings changes.',
    ci: 'passed (4 of 4 checks)',
    status: 'pending',
    created_at: created,
    expires_at: created + 30 * MIN,
  };
}

/** Test/dev helper: replace the scenario flags and rebuild the release/lease state. */
export function setMockScenario(...names) {
  S.scenario = new Set(names.flat());
  S.release = seedRelease(S.scenario);
}

function initialState() {
  const summary = buildSummary();
  return {
    desired: 'running',
    mode: 'demo',
    kill_switch: false,
    live_allowed: true,
    last_run: now() - 4 * MIN,
    last_error: null,
    config: { run_budget: 15, max_order: 3, max_open_frac: 0.3, series: [...SERIES], poll_minutes: 10, cells: CELLS },
    version: 7,
    journal: seedJournal(),
    summary,
    report: reportText(summary),
    cloud: { keepalive_minutes: 10, pinger_minutes: 5, pinger_enabled: true, silent_alert_minutes: 30, shadow_enabled: true, shadow_capital: 150 },
    periods: [],
    cashFlows: [],
    pinger: { configured: true, job_id: 5813377, synced: true, last_sync_utc: iso(now() - 2 * 60 * MIN), error: null },
    keepalive: { last_ping_utc: iso(now() - 3 * MIN), ok: true, error: null },
    // Release process (docs/RELEASE_PROCESS.md section 3). Scenario names: standby, release-expired,
    // release-none, release-off (404), old-app. Set via sessionStorage 'keel.mock' (comma list) or setMockScenario().
    scenario: readScenario(),
    lease: { holder: 'a3f9c2d41b7e4c0a9d6e5f8b12c34d56', generation: 14, expires_at: now() + 45 * 1000, instance: 'a3f9c2d41b7e4c0a9d6e5f8b12c34d56' },
    release: seedRelease(readScenario()),
    audit: [],
    logs: [],
    lastCode: null,
    resting: 9,
    arb: {
      settings: { enabled: true, live: false, arb_max_contracts: 100, arb_min_profit: 0.05, arb_series: [...ARB_SERIES], arb_poll_seconds: 15, arb_scan_cap: 15, arb_day_cap: 45, arb_series_cap_frac: 0.35, arb_demo_execute: false },
      kill_switch: false,
      kill_reason: null,
      last_error: null,
      scans: 1342,
      opps: seedOpps(),
    },
  };
}

let S = initialState();
export function resetMock() {
  resetMockUx();
  S = initialState();
}

function log(msg) {
  S.logs.push(`${iso(now())} INFO ${msg}`);
  if (S.logs.length > 500) S.logs.shift();
}
function audit(action, ok = true, details = null) {
  S.audit.unshift({ ts: iso(now()), user: 'owner', action, ok, details, ip: '203.0.113.7', ua: 'mock' });
}
for (let i = 40; i > 0; i--) {
  S.logs.push(`${iso(now() - i * 10 * MIN)} INFO runner: tick reason=${i % 2 ? 'self' : 'external'} -> pass ran (quoted 6 series, 12 orders)`);
}
audit('login', true, null);

// ------------------------------------------------------------------ helpers

function reply(status, data) {
  const body = data === undefined ? '' : JSON.stringify(data);
  return { ok: status >= 200 && status < 300, status, text: async () => body, json: async () => data };
}

// Non-JSON body (CSV) with response headers.
function replyText(status, text, headers = {}) {
  const h = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
    headers: { get: (name) => h[String(name).toLowerCase()] ?? null },
  };
}

function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener?.('abort', () => {
        clearTimeout(t);
        const e = new Error('aborted');
        e.name = 'AbortError';
        reject(e);
      });
    }
  });
}

function authed(headers) {
  return headers && /^Bearer mock-/.test(headers.Authorization || '');
}

// -> null if ok, else a reply
function check2fa(headers) {
  const code = headers && headers['X-2FA-Code'];
  if (!code) return reply(403, { detail: '2fa_required' });
  if (!/^\d{6}$/.test(code) || code === '000000' || code === S.lastCode) {
    return reply(403, { detail: '2fa_invalid' });
  }
  S.lastCode = code;
  return null;
}

// Queue position at quote time (display only). Deterministic per series.
function queueStats(seed = 0) {
  const k = 1 + (seed % 5) * 0.15;
  const by_queue = [
    { bucket: '0', episodes: Math.round(140 / k), fills: Math.round(46 / k), trade_through_share: 0.31 },
    { bucket: '1-50', episodes: Math.round(210 / k), fills: Math.round(38 / k), trade_through_share: 0.36 },
    { bucket: '51-200', episodes: Math.round(260 / k), fills: Math.round(21 / k), trade_through_share: 0.44 },
    { bucket: '201-1000', episodes: Math.round(180 / k), fills: Math.round(7 / k), trade_through_share: 0.58 },
    { bucket: '>1000', episodes: Math.round(60 / k), fills: Math.round(1 / k), trade_through_share: null },
  ].map((b) => ({ ...b, fill_rate: b.episodes ? +(b.fills / b.episodes).toFixed(3) : 0 }));
  const episodes = by_queue.reduce((a, b) => a + b.episodes, 0);
  const fills = by_queue.reduce((a, b) => a + b.fills, 0);
  return {
    episodes,
    fills,
    median_queue_ahead_at_join: Math.round(64 * k),
    requote_reset_share: +(0.22 * k).toFixed(2),
    front_fill_share: fills ? +(by_queue[0].fills / fills).toFixed(2) : null,
    by_queue,
    updated_utc: iso(now() - 40 * MIN),
  };
}

// Like the real runner: while the bot is running, every poll interval that has elapsed is a pass that ran, so
// the heartbeat stays fresh and the next run is always in the future. A stopped bot does not run, so its
// last run keeps ageing.
function passRow(ts, note) {
  return {
    utc: iso(ts), event: 'quote', ticker: 'KXDEMOA-26OCT13-T83', side: 'no', price: 0.5,
    count: 13.32, cost: 6.66, cell: '[0.45, 0.6, 6, 12, "no"]', note,
  };
}

function tickBot() {
  if (S.desired !== 'running' || S.kill_switch) return;
  const gap = S.config.poll_minutes * MIN;
  const passes = Math.floor((now() - S.last_run) / gap);
  if (passes > 0) {
    // Each pass that elapsed leaves a journal row (the newest two), so Activity does not go stale while the bot runs.
    for (let k = Math.min(passes, 2) - 1; k >= 0; k--) S.journal.unshift(passRow(S.last_run + (passes - k) * gap, 'scheduled pass'));
    if (S.journal.length > 600) S.journal.length = 600;
    S.last_run += passes * gap;
  }
}

// The keep-awake self-ping fires every keepalive_minutes whether or not the bot is running, so its "last ping"
// moves forward in steps instead of ageing for ever.
function tickKeepalive() {
  const gap = S.cloud.keepalive_minutes * MIN;
  const last = Date.parse(S.keepalive.last_ping_utc);
  if (!(gap > 0) || !Number.isFinite(last)) return;
  const n = Math.floor((now() - last) / gap);
  if (n > 0) S.keepalive = { ...S.keepalive, last_ping_utc: iso(last + n * gap) };
}

// The leader renews every 20 s with a 60 s TTL, so the expiry always sits 40-60 s ahead (never frozen in the past).
function leaseFields() {
  const standby = S.scenario.has('standby');
  const expires = now() + (60 - ((now() / 1000) % 20)) * 1000;
  return {
    role: standby ? 'standby' : 'leader',
    lease_generation: S.lease.generation,
    lease_expires_at: iso(expires),
    lease_holder: standby ? 'e71b04aa5c2d49f08b3a6d7c91e0f245' : S.lease.holder,
    instance_id: standby ? '5d2c8e90b1a74f36a0c9e4d8b7f61a23' : S.lease.instance,
  };
}

function releaseView() {
  const r = S.release;
  if (!r) return null;
  const status = r.status === 'pending' && r.expires_at <= now() ? 'expired' : r.status;
  return { id: r.id, version: r.version, commit: r.commit, summary: r.summary, ci: r.ci, status, created_at: iso(r.created_at), expires_at: iso(r.expires_at), by: r.by || null, at: r.at ? iso(r.at) : null };
}

function status() {
  tickBot();
  const exposure = 37.6;
  const cash = 212.4;
  const nextDue = S.last_run + S.config.poll_minutes * MIN;
  return {
    desired: S.desired,
    mode: S.mode,
    kill_switch: S.kill_switch,
    running_pass: false,
    last_run_utc: iso(S.last_run),
    last_error: S.last_error,
    next_due_utc: S.desired === 'running' && !S.kill_switch ? iso(nextDue) : null,
    heartbeat_age_s: Math.round((now() - S.last_run) / 1000),
    live_allowed: S.live_allowed,
    account: { cash, exposure, headroom: +(S.config.max_open_frac * (cash + exposure) - exposure).toFixed(2) },
    limits: { run_budget: S.config.run_budget, max_open_frac: S.config.max_open_frac },
    pnl: { d7: 4.83, d30: 17.12 },
    pnl_arb: { d7: 0.9, d30: 2.4 },
    pnl_total: { d7: 5.73, d30: 19.52 },
    expected_roi: null,
    queue_stats: queueStats(),
    arb: { state: arbState(), last_scan_utc: arbState() !== 'stopped' ? iso(now() - 9 * 1000) : null },
    // UX contract section 5 additions (read-only).
    pnl_today: { maker: 1.4, arb: 0.35, you: -0.8, bot: 1.75, total: 0.95 },
    pnl_today_state: 'settled',
    pnl_today_tz: 'UTC',
    pnl_today_since: iso(new Date(new Date(now()).setUTCHours(0, 0, 0, 0))),
    recent_passes: recentPasses(),
    // Release handshake + lease (real field names from the API).
    api_version: '2026.10.16-1',
    min_app_version: S.scenario.has('old-app') ? '99.0.0' : '0.1.0',
    schema_version: 1,
    ...leaseFields(),
    last_run_at: iso(S.last_run),
    next_run_at: S.desired === 'running' && !S.kill_switch ? iso(nextDue) : null,
  };
}

// Last 12 passes, newest last: mostly ok, one late, one with errors.
function recentPasses() {
  const gap = S.config.poll_minutes * MIN;
  return Array.from({ length: 12 }, (_, i) => ({
    ts: iso(S.last_run - (11 - i) * gap),
    ok: i !== 4,
    late: i === 8,
    errors: i === 4 ? 2 : 0,
  }));
}

// Strategy registry (UX contract section 5): one entry per strategy plus "you" for manual trades.
const STRATEGY_LIST = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)', prefix: 'mm-' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)', prefix: 'arb-' },
  { id: 'you', source: 'you', name: 'You', color: 'var(--warn)', prefix: null },
];

// What's New feed (GET /api/changelog), newest first.
const CHANGELOG = [
  {
    version: '0.4.0',
    date: '2026-10-15',
    items: [
      'The app is now called Prior, with a new logo and three colour themes.',
      'The bottom tabs are Overview, Analytics, Activity, Markets and More.',
      'Times show in your own time zone; hover one for the exact UTC time.',
      'Pick Small, Normal or Large text in Settings.',
      'A friendlier screen while the free server wakes up.',
      'Wide screens get a sidebar.',
    ],
  },
  { version: '0.3.0', date: '2026-10-13', items: ['Analytics and the Terminology glossary.'] },
];

function markets() {
  return {
    markets: SERIES.map((s) => {
      const st = S.summary.per_series[s];
      const pause = S.summary.actions.pause.includes(s);
      return {
        series: s,
        enabled: S.config.series.includes(s),
        ...st,
        need_more: S.summary.actions.need_more[s],
        verdict: pause ? 'pause' : st.fills >= 100 ? 'keep' : 'collecting data',
        queue_stats: s === 'KXDEMOF' ? { ...queueStats(), episodes: 0, fills: 0, by_queue: [] } : queueStats(SERIES.indexOf(s) + 1),
      };
    }),
    queue_stats: queueStats(),
    report_utc: S.summary.generated_utc,
  };
}

function publicConfig() {
  return { ...S.config };
}

function validateConfig(next) {
  const errs = [];
  const n = (v) => typeof v === 'number' && Number.isFinite(v);
  if (!n(next.run_budget) || next.run_budget < next.series.length || next.run_budget > 500) {
    errs.push(`run_budget must be between ${next.series.length} and 500 (API ceiling)`);
  }
  if (!n(next.max_order) || next.max_order < 1 || next.max_order > 1000) errs.push('max_order must be between 1 and 1000');
  if (!n(next.max_open_frac) || next.max_open_frac < 0.05 || next.max_open_frac > 0.5) {
    errs.push('max_open_frac must be between 0.05 and 0.5 (API ceiling)');
  }
  if (!Number.isInteger(next.poll_minutes) || next.poll_minutes < 2 || next.poll_minutes > 14) {
    errs.push('poll_minutes must be an integer between 2 and 14 (MAX_POLL_MINUTES)');
  }
  if (!Array.isArray(next.series) || next.series.length < 1) errs.push('series must contain at least one series');
  else {
    const bad = next.series.filter((s) => !SERIES.includes(s));
    if (bad.length) errs.push(`unknown series: ${bad.join(', ')}`);
  }
  return errs;
}

const SHADOW_NOTES = [
  'Our own orders would move the queue; the model ignores that impact.',
  'Queue changes between checks are hidden, so fills are shown as a range.',
  'Cash limits are not modelled in v1.',
  'Outages and rejected orders are not modelled.',
  'Small sample: the range is wide until many episodes settle.',
];

function shadowSummary() {
  if (!S.cloud.shadow_enabled) {
    return { enabled: false, standin: true, status: { days_covered: 0, settled_episodes: 0, central_fills: 0, too_early: true, last_pass_ts: null, pass_ok_pct: null, truncated: false }, headline: null, by_series: [], by_cell: [], daily: [], fee_mode: 'maker', caveats: SHADOW_NOTES };
  }
  const day0 = Date.UTC(2026, 9, 1);
  const daily = [];
  let lo = 0, mid = 0, hi = 0;
  for (let i = 0; i < 14; i++) {
    lo += 0.2 + (i % 3) * 0.1; mid += 0.5 + (i % 4) * 0.15; hi += 0.9 + (i % 3) * 0.2;
    daily.push({ day: new Date(day0 + i * 86400000).toISOString().slice(0, 10), cum_lo: +lo.toFixed(2), cum_mid: +mid.toFixed(2), cum_hi: +hi.toFixed(2) });
  }
  const zero = { pnl: { lo: 3.6, mid: 9.2, hi: 16.8, ci90: [2.1, 17.4] }, roi: { lo: 0.006, mid: 0.012, hi: 0.019, ci90: [0.004, 0.02] }, fill_rate: { lo: 0.18, mid: 0.31, hi: 0.44 }, peak_capital: 412 };
  return {
    enabled: true,
    standin: true,
    status: { days_covered: 14, settled_episodes: 58, central_fills: 41.5, too_early: false, last_pass_ts: iso(now() - 4 * MIN), pass_ok_pct: 0.98, truncated: false },
    headline: { pnl: { lo: 3.1, mid: 8.4, hi: 15.9, ci90: [1.6, 16.5] }, roi: { lo: 0.005, mid: 0.011, hi: 0.018, ci90: [0.003, 0.019] }, fill_rate: zero.fill_rate, peak_capital: zero.peak_capital },
    headline_zero_fee: zero,
    by_series: SERIES.slice(0, 3).map((s, i) => ({ series: s, episodes: 20 - i * 4, fills_mid: 14 - i * 3, pnl: { lo: 1 + i, mid: 3 + i, hi: 6 + i } })),
    by_cell: [
      { cell: ['DEMO-CELL-1'], episodes: 30, fills_mid: 20, pnl: { lo: 1.2, mid: 4.1, hi: 8 } },
      { cell: ['DEMO-CELL-2'], episodes: 28, fills_mid: 21.5, pnl: { lo: 1.9, mid: 4.3, hi: 7.9 } },
    ],
    daily,
    fee_mode: 'maker',
    caveats: SHADOW_NOTES,
  };
}

function shadowEpisodes() {
  return Array.from({ length: 8 }, (_, i) => ({
    id: `ep-${i + 1}`, ticker: `${SERIES[0]}-26OCT${10 + i}-DEMO`, state: i < 6 ? 'settled' : 'open', price: 0.4, count: 5, fills_mid: 3, pnl_mid: i < 6 ? 0.3 : null,
  }));
}

// Period 1 is implicit ("p1", starts at the beginning of data); each marker starts the next period.
function periodsResponse() {
  const marks = S.periods;
  const periods = [{ id: 'p1', name: 'Demo Period 1', started_at: null, ended_at: marks[0] ? marks[0].started_at : null, note: null, implicit: true }];
  marks.forEach((m, i) => periods.push({ ...m, ended_at: marks[i + 1] ? marks[i + 1].started_at : null, implicit: false }));
  periods.forEach((p, i) => { p.current = i === periods.length - 1; });
  return { periods, cash_flows: S.cashFlows };
}

function cloudResponse() {
  tickKeepalive();
  return {
    settings: { ...S.cloud },
    schema: CLOUD_SCHEMA,
    keepalive: { ...S.keepalive },
    pinger: { ...S.pinger, enabled: S.cloud.pinger_enabled, schedule: `every ${S.cloud.pinger_minutes} min` },
    last_inbound_utc: iso(now() - 40 * 1000),
    sleep_timeout_minutes: 15,
  };
}

// ------------------------------------------------------------------ router

async function route(method, url, headers, body) {
  tickBot(); // the runner keeps running between requests, whichever page asks
  const p = url.pathname;
  const q = url.searchParams;

  if (method === 'GET' && p === '/healthz') {
    return reply(200, { ok: true, api_version: '2026.10.16-1', min_app_version: S.scenario.has('old-app') ? '99.0.0' : '0.1.0', schema_version: 1, role: leaseFields().role, lease_generation: S.lease.generation });
  }

  if (method === 'POST' && p === '/api/auth/login') {
    if (!body || !body.username || !body.password) return reply(422, { detail: 'username and password required' });
    if (body.password === 'wrong') {
      audit('login', false, { reason: 'bad password' });
      return reply(401, { detail: 'invalid_credentials' });
    }
    return reply(200, { mfa_token: 'mock-mfa-' + Date.now(), expires_in: 300 });
  }
  if (method === 'POST' && p === '/api/auth/2fa') {
    if (!body || !/^\d{6}$/.test(body.code || '') || body.code === '000000') {
      return reply(401, { detail: 'invalid_code' });
    }
    S.lastCode = body.code;
    audit('login_2fa');
    return reply(200, { access_token: 'mock-access-' + Date.now(), token_type: 'bearer', expires_in: 3600 });
  }

  if (!authed(headers)) return reply(401, { detail: 'not_authenticated' });

  if (method === 'POST' && p === '/api/auth/logout') {
    audit('logout');
    return reply(200, { ok: true });
  }
  if (method === 'GET' && p === '/api/auth/me') return reply(200, { username: 'owner' });
  if (method === 'POST' && p === '/api/auth/logout-all') {
    const bad = check2fa(headers);
    if (bad) return bad;
    audit('auth.logout_all');
    return reply(200, { ok: true, sessions_valid_after: iso(now()) });
  }

  if (method === 'GET' && p === '/api/status') return reply(200, status());
  if (method === 'GET' && p === '/api/strategies') return reply(200, STRATEGY_LIST);
  // --- FE-Overview mocks (items 30, Y2): since-last-visit summary and open positions by source ---
  if (method === 'GET' && p === '/api/since') {
    return reply(200, { fills: 3, settled: 2, pnl_change: 1.4, since: q.get('ts') });
  }
  if (method === 'GET' && p === '/api/account/positions') {
    return reply(200, [
      { kind: 'position', ticker: 'KXDEMOA-26OCT13-T83', title: 'Demo A above 83', side: 'yes', count: 20, price: 0.41, value: 8.2, payout_if_wins: 20, source: 'bot', strategy: 'maker' },
      { kind: 'position', ticker: 'KXDEMOB-26OCT13-T61', title: 'Demo B above 61', side: 'yes', count: 12, price: 0.4, value: 4.8, payout_if_wins: 12, source: 'bot', strategy: 'maker' },
      { kind: 'position', ticker: 'KXFED-26OCT-T4', title: 'Fed holds rates', side: 'yes', count: 10, price: 0.66, value: 6.6, payout_if_wins: 10, source: 'you', strategy: null },
      { kind: 'order', ticker: 'KXDEMOD-26OCT13-B88.5', title: 'Demo D 88 to 89', side: 'yes', action: 'buy', count: 9, price: 0.4, value: 3.6, payout_if_wins: 9, source: 'bot', strategy: 'maker' },
    ]);
  }
  if (method === 'GET' && p === '/api/changelog') return reply(200, CHANGELOG);

  if (method === 'POST' && p === '/api/bot/stop') {
    const cancelled = S.resting;
    S.resting = 0;
    S.desired = 'stopped';
    S.kill_switch = true;
    S.arb = { ...S.arb, settings: { ...S.arb.settings, enabled: false }, kill_switch: true, kill_reason: 'stop' };
    audit('bot_stop', true, { cancelled });
    log(`stop: kill switch set, cancelled ${cancelled} resting mm- orders`);
    // stopped_at / orders_cancelled: UX contract item 39 (toasts); the older keys stay.
    return reply(200, {
      desired: 'stopped', kill_switch: true, arb_kill_switch: true, cancelled, errors: [],
      stopped_at: iso(now()), orders_cancelled: cancelled,
    });
  }
  if (method === 'POST' && p === '/api/bot/start') {
    const bad = check2fa(headers);
    if (bad) return bad;
    S.desired = 'running';
    S.kill_switch = false;
    S.last_error = null;
    // Like the real runner, a start runs a pass straight away: the heartbeat is fresh and the next run is one
    // interval away (without this a long-stopped bot would look overdue right after Start).
    S.last_run = now();
    S.resting = Math.max(S.resting, 9);
    S.journal.unshift(passRow(now(), 'start'));
    audit('bot_start');
    log('start: kill switch removed, desired=running');
    return reply(200, { desired: 'running' });
  }
  if (method === 'POST' && p === '/api/bot/run-once') {
    if (S.desired !== 'running' || S.kill_switch) {
      audit('bot_run_once', false, { reason: 'stopped' });
      return reply(200, { ran: false, reason: 'desired=stopped (start the bot first)' });
    }
    S.last_run = now();
    S.resting = 12;
    S.journal.unshift(passRow(now(), 'run-once'));
    audit('bot_run_once');
    log('run-once: pass ran (quoted 6 series, 12 orders)');
    return reply(200, { ran: true, reason: 'forced' });
  }

  if (method === 'PUT' && p === '/api/mode') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const mode = body && body.mode;
    if (mode !== 'demo' && mode !== 'live') return reply(400, { detail: ['mode must be "demo" or "live"'] });
    if (mode === 'live') {
      if (body.confirm !== 'LIVE') return reply(400, { detail: ['type LIVE to confirm live mode'] });
      if (!S.live_allowed) {
        return reply(409, { detail: 'live_not_allowed: prod key or KALSHI_LIVE_CONFIRM missing in the server environment' });
      }
    }
    S.mode = mode;
    audit('mode', true, { mode });
    log(`mode -> ${mode}`);
    return reply(200, { mode });
  }

  if (method === 'GET' && p === '/api/config') {
    return reply(200, {
      config: publicConfig(),
      schema: CONFIG_SCHEMA,
      ceilings: { run_budget: 500, max_open_frac: 0.5, poll_minutes: 14 },
      shadow_capital: {
        value: S.cloud.shadow_capital, default: 150, min: 50, max: 1000, unit: 'USD', editable: true,
        help: CLOUD_SCHEMA.shadow_capital.help, write_via: 'PUT /api/cloud', needs_fresh_2fa: true,
      },
    });
  }
  if (method === 'PUT' && p === '/api/config') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const patch = body || {};
    if ('cells' in patch) return reply(400, { detail: ['cells is read-only (validated strategy)'] });
    const allowed = ['run_budget', 'max_order', 'max_open_frac', 'poll_minutes', 'series'];
    const unknown = Object.keys(patch).filter((k) => !allowed.includes(k));
    if (unknown.length) return reply(400, { detail: [`unknown or read-only field(s): ${unknown.join(', ')}`] });
    const next = { ...S.config, ...patch };
    const errs = validateConfig(next);
    if (errs.length) {
      audit('config_save', false, { errors: errs });
      return reply(400, { detail: errs });
    }
    const before = { ...S.config };
    S.config = next;
    S.version += 1;
    audit('config_save', true, { version: S.version, changed: Object.keys(patch), before: Object.fromEntries(Object.keys(patch).map((k) => [k, before[k]])) });
    log(`config saved as version ${S.version}`);
    return reply(200, { config: publicConfig(), version: S.version });
  }

  const ux = uxRoute(method, p, q, headers, body, { reply, check2fa, state: () => S, now, iso, audit, validateConfig });
  if (ux) return ux;

  if (method === 'GET' && p === '/api/markets') return reply(200, markets());
  const m = p.match(/^\/api\/markets\/([^/]+)$/);
  if (method === 'PUT' && m) {
    const bad = check2fa(headers);
    if (bad) return bad;
    const series = decodeURIComponent(m[1]);
    if (!SERIES.includes(series)) return reply(404, { detail: `unknown series ${series}` });
    const enabled = !!(body && body.enabled);
    const set = new Set(S.config.series);
    if (enabled) set.add(series);
    else set.delete(series);
    const next = { ...S.config, series: SERIES.filter((s) => set.has(s)) };
    const errs = validateConfig(next);
    if (errs.length) return reply(400, { detail: errs });
    S.config = next;
    S.version += 1;
    audit('market', true, { series, enabled });
    return reply(200, { config: publicConfig(), version: S.version });
  }

  if (method === 'GET' && p === '/api/activity') {
    const limit = Math.min(500, parseInt(q.get('limit') || '100', 10) || 100);
    return reply(200, { items: decorateActivity(S.journal.slice(0, limit), now(), iso).slice(0, limit) });
  }
  if (method === 'GET' && p === '/api/history') {
    const days = Math.min(365, parseInt(q.get('days') || '30', 10) || 30);
    const snaps = [];
    let cash = 200;
    for (let d = days; d >= 0; d--) {
      cash += Math.sin(d / 2.3) * 1.4 + 0.55;
      const exposure = 25 + Math.abs(Math.cos(d / 1.7)) * 30;
      snaps.push({
        ts: iso(now() - d * 24 * 60 * MIN),
        cash: +cash.toFixed(2),
        exposure: +exposure.toFixed(2),
        headroom: +(0.5 * (cash + exposure) - exposure).toFixed(2),
      });
    }
    return reply(200, { snapshots: snaps });
  }
  // Analytics (docs/ANALYTICS_CONTRACT.md): read-only, normal session, no 2FA.
  if (method === 'GET' && p === '/api/analytics/summary') {
    const [st, data] = analyticsSummary(q, { mode: S.mode });
    return reply(st, data);
  }
  if (method === 'GET' && p === '/api/analytics/returns') {
    const [st, data] = mockReturns(q, { mode: S.mode });
    return reply(st, data);
  }
  if (method === 'GET' && p === '/api/analytics/returns/fills') {
    const [st, data] = mockReturnsFills(q, { mode: S.mode });
    return reply(st, data);
  }
  if (method === 'GET' && p === '/api/analytics/ledger') return reply(200, mockLedger(q, S.mode));
  if (method === 'GET' && p === '/api/analytics/scorecard') return reply(200, mockScorecard(q, S.mode));
  if (method === 'GET' && p === '/api/analytics/trades.csv') {
    const [st, body, hdrs] = analyticsTradesCsv(q, { mode: S.mode });
    return st === 200 ? replyText(st, body, hdrs) : reply(st, body);
  }
  if (method === 'GET' && p === '/api/analytics/my-trades') {
    const [st, data] = mockMyTrades(q);
    return reply(st, data);
  }
  const noteM = p.match(/^\/api\/trades\/([^/]+)\/note$/);
  if (noteM && (method === 'GET' || method === 'PUT')) {
    const [st, data] = mockTradeNote(method, decodeURIComponent(noteM[1]), body);
    return reply(st, data);
  }
  if (method === 'GET' && p === '/api/analytics/trades') {
    const [st, data] = analyticsTrades(q, { mode: S.mode });
    return reply(st, data);
  }
  if (method === 'GET' && p === '/api/health') {
    return reply(200, { summary: S.summary, report_text: S.report });
  }
  if (method === 'POST' && p === '/api/monitor/run') {
    S.summary = { ...buildSummary(), generated_utc: iso(now()) };
    S.report = reportText(S.summary);
    audit('monitor_run');
    log('monitor: health check ran');
    return reply(200, { summary: S.summary });
  }
  if (method === 'GET' && p === '/api/logs') {
    const lines = Math.min(1000, parseInt(q.get('lines') || '200', 10) || 200);
    return reply(200, { lines: S.logs.slice(-lines) });
  }
  if (method === 'GET' && p === '/api/audit') {
    const limit = Math.min(500, parseInt(q.get('limit') || '100', 10) || 100);
    return reply(200, { items: S.audit.slice(0, limit) });
  }

  if (p === '/api/release/pending' || p.startsWith('/api/release/pending/')) {
    if (S.scenario.has('release-off')) return reply(404, { detail: 'Not Found' });
    if (method === 'GET' && p === '/api/release/pending') {
      const v = releaseView();
      return reply(200, { release: v && v.status === 'pending' ? v : null });
    }
    const m = /^\/api\/release\/pending\/([0-9a-f]{32})\/(approve|reject)$/.exec(p);
    if (method === 'POST' && m) {
      if (m[2] === 'approve') {
        const bad = check2fa(headers);
        if (bad) return bad;
      }
      const v = releaseView();
      if (!v || v.id !== m[1]) return reply(404, { detail: 'release_not_found' });
      if (v.status !== 'pending') return reply(409, { detail: 'release_not_pending' });
      S.release.status = m[2] === 'approve' ? 'approved' : 'rejected';
      S.release.by = 'owner';
      S.release.at = now();
      audit(`release.${m[2]}`, true, { id: v.id, version: v.version });
      return reply(200, { id: v.id, status: S.release.status });
    }
  }

  if (method === 'GET' && p === '/api/cloud') return reply(200, cloudResponse());
  if (method === 'PUT' && p === '/api/cloud') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const patch = body || {};
    const errs = [];
    for (const [k, v] of Object.entries(patch)) {
      const sc = CLOUD_SCHEMA[k];
      if (!sc) errs.push(`unknown field ${k}`);
      else if (sc.type === 'bool') {
        if (typeof v !== 'boolean') errs.push(`${k} must be true/false`);
      } else if (k === 'shadow_capital') {
        if (typeof v !== 'number' || !Number.isFinite(v) || v < sc.min || v > sc.max) errs.push(`${k} must be a number between ${sc.min} and ${sc.max}`);
      } else if (!Number.isInteger(v) || v < sc.min || v > sc.max) {
        errs.push(`${k} must be an integer between ${sc.min} and ${sc.max}`);
      }
    }
    if (errs.length) return reply(400, { detail: errs });
    S.cloud = { ...S.cloud, ...patch };
    S.pinger = { ...S.pinger, synced: true, last_sync_utc: iso(now()), error: null };
    audit('cloud_save', true, patch);
    log(`cloud settings saved; cron-job.org job ${S.pinger.job_id} synced`);
    return reply(200, cloudResponse());
  }
  if (method === 'POST' && p === '/api/cloud/pinger/sync') {
    const bad = check2fa(headers);
    if (bad) return bad;
    S.pinger = { ...S.pinger, synced: true, last_sync_utc: iso(now()), error: null };
    audit('pinger_sync');
    return reply(200, cloudResponse());
  }

  if (method === 'GET' && p === '/api/shadow/summary') return reply(200, shadowSummary());
  if (method === 'GET' && p === '/api/shadow/episodes') {
    const limit = Math.min(500, parseInt(q.get('limit') || '50', 10) || 50);
    return reply(200, { episodes: shadowEpisodes().slice(0, limit) });
  }
  if (method === 'GET' && p === '/api/periods') return reply(200, periodsResponse());
  if (method === 'POST' && p === '/api/periods') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const name = String((body && body.name) || '').trim();
    if (!name || name.length > 60) return reply(400, { detail: ['name must be 1-60 characters'] });
    const id = `mp${S.periods.length + 1}`;
    S.periods.push({ id, name, started_at: iso(now()), note: (body && body.note) || null });
    audit('period.create', true, { name });
    return reply(200, { ...periodsResponse(), created: id });
  }
  if (method === 'POST' && p === '/api/cash-flows') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const b = body || {};
    if (!['deposit', 'withdrawal'].includes(b.kind) || !(b.amount > 0 && b.amount <= 100000)) {
      return reply(422, { detail: ['kind must be deposit or withdrawal and amount between 0 and 100000'] });
    }
    if (!b.ts || Number.isNaN(Date.parse(b.ts))) return reply(422, { detail: ['ts (the date it happened) is required'] });
    if (Date.parse(b.ts) > now() + MIN) return reply(422, { detail: ['ts must not be in the future'] });
    const day = String(b.ts).slice(0, 10);
    if (!b.confirm_duplicate && S.cashFlows.some((f) => f.kind === b.kind && f.amount === b.amount && String(f.ts).slice(0, 10) === day)) {
      return reply(409, { detail: ['duplicate: same type, amount and date already recorded'] });
    }
    const id = `cf${S.cashFlows.length + 1}`;
    S.cashFlows.push({ id, ts: iso(Date.parse(b.ts)), kind: b.kind, amount: b.amount, note: b.note || null, created_by: 'owner' });
    audit('cash_flow.create', true, { kind: b.kind, amount: b.amount });
    return reply(200, { ...periodsResponse(), created: id, warnings: [] });
  }
  if (method === 'DELETE' && p.startsWith('/api/cash-flows/')) {
    const bad = check2fa(headers);
    if (bad) return bad;
    const id = decodeURIComponent(p.slice('/api/cash-flows/'.length));
    const n = S.cashFlows.length;
    S.cashFlows = S.cashFlows.filter((f) => f.id !== id);
    if (S.cashFlows.length === n) return reply(404, { detail: 'cash_flow_not_found' });
    audit('cash_flow.delete', true, { id });
    return reply(200, { ...periodsResponse(), deleted: id });
  }
  if (method === 'GET' && /^\/api\/periods\/[^/]+\/summary$/.test(p)) {
    return reply(200, {
      fills: 24, settled: 18, wins: 16, losses: 2, pnl: 6.4, money_spent: 96.0, settled_cost: 72.0,
      return_on_settled_cost: 0.0889, return_on_all_cost: 0.0667, open_at_risk: 24.0,
      cash_flows: { deposits: 0, withdrawals: 0, net: 0, items: [] },
    });
  }
  if (method === 'GET' && p === '/api/pnl/by-source') {
    return reply(200, { period: null, env: 'demo', by_source: { maker: 5.1, arb: 0.9, you: 0.4, bot: 6.0, total: 6.4, settled_fills: 18 } });
  }
  if (method === 'GET' && p === '/api/arb') return reply(200, arbResponse());
  if (method === 'GET' && p === '/api/arb/opportunities') {
    const limit = Math.min(1000, parseInt(q.get('limit') || '100', 10) || 100);
    return reply(200, { items: S.arb.opps.slice(0, limit) });
  }
  if (method === 'PUT' && p === '/api/arb') {
    const bad = check2fa(headers);
    if (bad) return bad;
    const patch = { ...(body || {}) };
    const confirm = patch.confirm;
    const ack = patch.ack_legging;
    delete patch.confirm;
    delete patch.ack_legging;
    const errs = [];
    for (const [k, v] of Object.entries(patch)) {
      if (k === 'enabled' || k === 'live') {
        if (typeof v !== 'boolean') errs.push(`${k} must be true/false`);
        continue;
      }
      const sc = ARB_SCHEMA[k];
      if (!sc) errs.push(`unknown field ${k}`);
      else if (sc.type === 'list') {
        if (!Array.isArray(v) || !v.length) errs.push(`${k} must list at least one series`);
        else if (v.some((x) => !ARB_SERIES.includes(x))) errs.push(`${k}: unknown series`);
      } else if (sc.type === 'bool') {
        if (typeof v !== 'boolean') errs.push(`${k} must be true/false`);
      } else if (typeof v !== 'number' || v < sc.min || v > sc.max || (sc.type === 'integer' && !Number.isInteger(v))) {
        errs.push(`${k} must be between ${sc.min} and ${sc.max}`);
      }
    }
    if (errs.length) return reply(400, { detail: errs });
    if (patch.live === true) {
      if (confirm !== 'LIVE') return reply(400, { detail: ['type LIVE to confirm arbitrage live'] });
      if (!S.live_allowed) return reply(409, { detail: 'live_not_allowed' });
    }
    if (patch.enabled === true && S.arb.kill_reason === 'legging' && !ack) {
      return reply(409, { detail: 'arb_legging_kill' });
    }
    const a = S.arb;
    a.settings = { ...a.settings, ...patch };
    if (patch.enabled === true) {
      a.kill_switch = false;
      a.kill_reason = null;
    }
    audit('arb.update', true, patch);
    log(`arb settings saved: ${Object.keys(patch).join(', ')}`);
    return reply(200, arbResponse());
  }

  return reply(404, { detail: 'Not Found' });
}

export async function mockFetch(method, path, init = {}) {
  const ms = parseInt(import.meta.env.VITE_MOCK_DELAY_MS || '250', 10);
  await delay(Number.isFinite(ms) ? ms : 250, init.signal);
  const url = new URL(path, 'http://mock.local');
  let body;
  try {
    body = init.body ? JSON.parse(init.body) : undefined;
  } catch {
    return reply(422, { detail: 'invalid JSON' });
  }
  return route(method, url, init.headers || {}, body);
}

// Dev-only synthetic analytics for the fake backend (services/mock.js).
// Produces GET /api/analytics/summary and /api/analytics/trades in the exact
// shapes of docs/ANALYTICS_CONTRACT.md. Deterministic (seeded PRNG) so screenshots
// and e2e runs are stable. Nothing here talks to the network.

const DAY = 24 * 60 * 60 * 1000;
const HOUR = 60 * 60 * 1000;

export const A_SERIES = ['KXDEMOA', 'KXDEMOB', 'KXDEMOC', 'KXDEMOD', 'KXDEMOE', 'KXDEMOF'];
export const DAYS = [7, 30, 90, 180];

// Mirrors the mock config's cells: the public stand-in's EXAMPLE cells (made up; read-only, never edited).
export const MOCK_CELLS = [
  [0.35, 0.5, 3, 9, 'yes'], [0.5, 0.62, 9, 30, 'no'],
];
const CELLS = MOCK_CELLS;
const LO = Math.min(...CELLS.map((c) => c[0]));
const TOP = Math.max(...CELLS.map((c) => c[1]));
// One band per distinct cell price range [lo, hi) (the topmost includes hi), as the backend derives them.
const RANGES = [...new Map(CELLS.map((c) => [`${c[0]}-${c[1]}`, [c[0], c[1]]])).values()].sort((a, b) => a[0] - b[0]);
const BAND_DEFS = RANGES.map(([lo, hi]) => {
  const last = hi === TOP ? hi : Math.round(hi * 100 - 1) / 100;
  return { lo, last, key: `${lo.toFixed(2)}-${last.toFixed(2)}` };
});
export const A_BANDS = [...BAND_DEFS.map((b) => b.key), 'other'];
// Time-to-close buckets from the cells' hour edges (0 always included), as the backend derives them.
const TTC_EDGES = [...new Set([0, ...CELLS.flatMap((c) => [c[2], c[3]]).filter((h) => h > 0)])].sort((a, b) => a - b);
export const TTC_BANDS = [...TTC_EDGES.slice(1).map((e, i) => `${TTC_EDGES[i]}-${e}h`), `>${TTC_EDGES[TTC_EDGES.length - 1]}h`];

const r2 = (v) => Math.round(v * 100) / 100;
const r4 = (v) => Math.round(v * 10000) / 10000;
const iso = (t) => new Date(t).toISOString();
const dateKey = (t) => iso(t).slice(0, 10);

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function cellId(c) {
  return `${c[0].toFixed(2)}-${c[1].toFixed(2)}|${c[2]}-${c[3]}h|${c[4]}`;
}

export function priceBand(p) {
  const c = Math.round(p * 100);
  const b = BAND_DEFS.find((x) => c >= Math.round(x.lo * 100) && c <= Math.round(x.last * 100));
  return b ? b.key : 'other';
}

export function ttcBand(h) {
  const i = TTC_EDGES.slice(1).findIndex((e) => h < e);
  return TTC_BANDS[i < 0 ? TTC_BANDS.length - 1 : i];
}

function matchCell(price, ttc, side) {
  const c = CELLS.find((x) => price >= x[0] && price < x[1] + (x[1] === TOP ? 0.001 : 0) && ttc >= x[2] && ttc < x[3] && side === x[4]);
  return c ? cellId(c) : null;
}

export function wilson(wins, n, z = 1.96) {
  if (!n) return [null, null];
  const p = wins / n;
  const d = 1 + (z * z) / n;
  const c = p + (z * z) / (2 * n);
  const m = z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n));
  return [r4((c - m) / d), r4((c + m) / d)];
}

function isoWeek(t) {
  const d = new Date(t);
  const day = (d.getUTCDay() + 6) % 7; // Mon=0
  const th = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - day + 3));
  const y = th.getUTCFullYear();
  const jan4 = new Date(Date.UTC(y, 0, 4));
  const wk = 1 + Math.round(((th - jan4) / DAY - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
  return `${y}-W${String(wk).padStart(2, '0')}`;
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
function ticker(series, t, rand) {
  const d = new Date(t);
  const ds = `${String(d.getUTCFullYear()).slice(2)}${MONTHS[d.getUTCMonth()]}${String(d.getUTCDate()).padStart(2, '0')}`;
  return `${series}-${ds}-${rand() < 0.5 ? 'T' : 'B'}${60 + Math.floor(rand() * 35)}${rand() < 0.5 ? '.5' : ''}`;
}

const takerFee = (count, price) => Math.ceil(0.07 * count * price * (1 - price) * 100) / 100;

/** Several hundred demo fills over 180 days (+ a handful on live), arb days, a visible drawdown. */
function generate(nowMs) {
  const rand = rng(20261013);
  const fills = [];
  const arbDays = [];
  const end = nowMs;
  let id = 0;
  for (let d = 180; d >= 0; d--) {
    const dayStart = end - d * DAY;
    // A bad stretch ~2 months ago: more losses -> a visible drawdown.
    const rough = d <= 66 && d >= 54;
    const perDay = 2 + Math.floor(rand() * 5);
    let last = null;
    for (let k = 0; k < perDay; k++) {
      const ts = dayStart - rand() * 20 * HOUR;
      if (ts > nowMs - 5 * 60 * 1000) continue;
      // ~40% of fills land in a market we already filled in (same ticker, same outcome):
      // that correlation is what the clustered CIs account for.
      if (last && rand() < 0.4) {
        const ts2 = last.tsMs + rand() * HOUR;
        const ttc2 = (last.settleMs0 - ts2) / HOUR - 0.5;
        if (ttc2 > 0.2 && ts2 < nowMs - 5 * 60 * 1000) {
          const r = rand();
          const fq = r < 0.62 ? 'front' : r < 0.86 ? 'trade_through' : null;
          fills.push({
            ...last,
            fill_id: `mf-${String(++id).padStart(5, '0')}`,
            tsMs: ts2,
            price: Math.min(TOP, Math.max(LO, r2(last.price + (rand() < 0.5 ? -0.01 : 0.01)))),
            count: 3 + Math.floor(rand() * 12),
            ttc_hours: r2(ttc2),
            fill_quality: fq,
            queue_ahead: fq === 'front' ? Math.floor(rand() * 40) : Math.floor(30 + rand() * 600),
            requote: rand() < 0.35,
          });
          continue;
        }
      }
      const series = A_SERIES[Math.floor(rand() * A_SERIES.length)];
      const cell = CELLS[Math.floor(rand() * CELLS.length)];
      const offCell = rand() < 0.08;
      const side = offCell ? (rand() < 0.5 ? 'yes' : 'no') : cell[4];
      let price = r2(cell[0] + rand() * (cell[1] - cell[0] - 0.005));
      if (offCell) price = r2(LO + rand() * (TOP - LO));
      price = Math.min(TOP, Math.max(LO, price));
      const ttc = offCell ? 1 + rand() * 70 : cell[2] + rand() * (cell[3] - cell[2]) * 0.98;
      const count = 3 + Math.floor(rand() * 12);
      // Synthetic win probability: a little above the price, except in the rough patch and in one weaker cell.
      const weak = cell === CELLS[1];
      const winP = rough ? price - (1 - price) * 0.9 : weak ? price - (1 - price) * 0.4 : price + (1 - price) * 0.4;
      const settleTs = ts + ttc * HOUR + 0.5 * HOUR;
      const settled = settleTs < nowMs;
      const win = rand() < winP;
      const r = rand();
      const fq = r < 0.62 ? 'front' : r < 0.86 ? 'trade_through' : null;
      fills.push({
        env: 'demo',
        fill_id: `mf-${String(++id).padStart(5, '0')}`,
        tsMs: ts,
        ticker: ticker(series, ts + ttc * HOUR, rand),
        series,
        side,
        kind: 'maker',
        price,
        count,
        fee: 0,
        ttc_hours: r2(ttc),
        settleMs: settled ? settleTs : null,
        result: settled ? (win ? 'win' : 'loss') : null,
        fill_quality: fq,
        queue_ahead: fq === 'front' ? Math.floor(rand() * 40) : Math.floor(30 + rand() * 600),
        requote: rand() < 0.35,
        settleMs0: settleTs,
      });
      last = fills[fills.length - 1];
    }
    // Bracket arb: some days see opportunities, few are executed (taker legs).
    const opps = rand() < 0.45 ? 1 + Math.floor(rand() * 6) : 0;
    let executed = 0;
    for (let k = 0; k < opps; k++) {
      if (rand() < 0.3) {
        executed++;
        const ts = dayStart - rand() * 10 * HOUR;
        if (ts > nowMs - 5 * 60 * 1000) continue;
        const series = A_SERIES[Math.floor(rand() * 5)];
        // A NO set: buy NO on every bracket; exactly one bracket resolves YES.
        // Sum of NO prices = legs - 1 - margin, so the set clears ~margin per contract.
        const legs = 2 + Math.floor(rand() * 3);
        const margin = 0.05 + rand() * 0.05; // before taker fees
        const w = Array.from({ length: legs }, () => 0.2 + rand());
        const wSum = w.reduce((a, b) => a + b, 0);
        const yes = w.map((x) => (x / wSum) * (1 + margin));
        let winner = 0;
        let acc = rand();
        for (let l = 0; l < legs; l++) {
          acc -= yes[l] / (1 + margin);
          if (acc <= 0) { winner = l; break; }
        }
        const count = 10 + Math.floor(rand() * 30);
        const ttc = 4 + rand() * 14;
        for (let l = 0; l < legs; l++) {
          const price = Math.min(0.99, Math.max(0.01, r2(1 - yes[l])));
          const settleTs = ts + ttc * HOUR;
          const settled = settleTs < nowMs;
          fills.push({
            env: 'demo',
            fill_id: `tf-${String(++id).padStart(5, '0')}`,
            tsMs: ts,
            ticker: ticker(series, settleTs, rand),
            series,
            side: 'no',
            kind: 'taker',
            price,
            count,
            fee: takerFee(count, price),
            ttc_hours: r2(ttc),
            settleMs: settled ? settleTs : null,
            result: settled ? (l === winner ? 'loss' : 'win') : null,
            fill_quality: null,
            queue_ahead: null,
            requote: false,
          });
        }
      }
    }
    arbDays.push({ dayMs: dayStart, opportunities: opps, executed });
  }
  // A few live fills so env=all differs from demo.
  for (let k = 0; k < 6; k++) {
    const ts = nowMs - (2 + k * 3) * DAY;
    fills.push({
      env: 'live', fill_id: `lf-${k}`, tsMs: ts, ticker: ticker('KXDEMOB', ts + 8 * HOUR, rand), series: 'KXDEMOB',
      side: CELLS[1][4], kind: 'maker', price: r2((CELLS[1][0] + CELLS[1][1]) / 2), count: 2, fee: 0, ttc_hours: (CELLS[1][2] + CELLS[1][3]) / 2, settleMs: ts + 9 * HOUR,
      result: k === 3 ? 'loss' : 'win', fill_quality: 'front', queue_ahead: 4, requote: false,
    });
  }
  for (const f of fills) {
    f.ts = iso(f.tsMs);
    f.cost = r2(f.count * f.price);
    f.band = priceBand(f.price);
    f.ttc_band = ttcBand(f.ttc_hours);
    f.cell = f.kind === 'maker' ? matchCell(f.price, f.ttc_hours, f.side) : null;
    f.status = f.result ? 'settled' : 'open';
    f.settled_utc = f.settleMs ? iso(f.settleMs) : null;
    f.pnl = f.result ? r2(f.count * ((f.result === 'win' ? 1 : 0) - f.price) - f.fee) : null;
  }
  fills.sort((a, b) => b.tsMs - a.tsMs);

  // Quote episodes (for fill rate): one per maker fill, plus bids that rested and never filled.
  const er = rng(7);
  const episodes = [];
  for (const f of fills) {
    if (f.kind !== 'maker') continue;
    const ttf = 3 + er() * er() * 400;
    episodes.push({ env: f.env, tsMs: f.tsMs, cell: f.cell, series: f.series, filled: true, partial: er() < 0.15, rest: ttf + er() * 20, ttf });
    const missed = Math.floor(er() * (f.price >= CELLS[CELLS.length - 1][0] ? 4 : 2.5));
    for (let k = 0; k < missed; k++) {
      episodes.push({ env: f.env, tsMs: f.tsMs - er() * 6 * HOUR, cell: f.cell, series: f.series, filled: false, partial: false, rest: 10 + er() * 500, ttf: null });
    }
  }
  return { fills, arbDays, episodes };
}

// Synthetic research benchmark per cell (mock only; the real one is server-side and private).
export const MOCK_EXPECTATIONS = {
  source: 'mock: synthetic benchmark',
  default: { win_rate: null, roi: 0.01 },
  cells: Object.fromEntries(
    CELLS.map((c, i) => {
      const mid = (c[0] + c[1]) / 2;
      return [cellId(c), { win_rate: r4(mid + (1 - mid) * (0.2 + (i % 3) * 0.05)), roi: r4(0.008 + (i % 4) * 0.004), n_ref: 400 + i * 37 }];
    }),
  ),
};

// Cluster-robust ratio estimate: clusters = tickers; y = numerator, m = denominator per row.
export function clusterCi(rows, y, m, z = 1.96) {
  const by = new Map();
  for (const f of rows) {
    const e = by.get(f.ticker) || { y: 0, m: 0 };
    e.y += y(f);
    e.m += m(f);
    by.set(f.ticker, e);
  }
  const G = by.size;
  const vals = [...by.values()];
  const Y = vals.reduce((t, e) => t + e.y, 0);
  const M = vals.reduce((t, e) => t + e.m, 0);
  if (G < 2 || !M) return [null, null];
  const r = Y / M;
  const ss = vals.reduce((t, e) => t + (e.y - r * e.m) ** 2, 0);
  const se = Math.sqrt((G / (G - 1)) * ss) / M;
  return [r - z * se, r + z * se];
}

const clip01 = (v) => (v == null ? null : r4(Math.max(0, Math.min(1, v))));

function median(xs) {
  if (!xs.length) return null;
  const a = xs.slice().sort((p, q) => p - q);
  const mid = Math.floor(a.length / 2);
  return Math.round((a.length % 2 ? a[mid] : (a[mid - 1] + a[mid]) / 2) * 10) / 10;
}

function fillRate(eps) {
  const filled = eps.filter((e) => e.filled).length;
  return {
    quotes: eps.length,
    filled,
    partial: eps.filter((e) => e.partial).length,
    unfilled: eps.length - filled,
    fill_rate: eps.length ? r4(filled / eps.length) : null,
    median_rest_minutes: median(eps.map((e) => e.rest)),
    median_minutes_to_fill: median(eps.filter((e) => e.filled).map((e) => e.ttf)),
  };
}

function verdictFor(g, exp) {
  const fmt = (v) => `${(v * 100).toFixed(1)}%`;
  if (g.markets < 20) {
    return ['too_early', `Only ${g.markets} market${g.markets === 1 ? '' : 's'} so far; at least 20 are needed before comparing with the research.`];
  }
  if (exp.win_rate != null && g.cluster_ci_low != null) {
    if (g.cluster_ci_high < exp.win_rate) return ['below', `Won ${fmt(g.win_rate)}; even the top of the range (${fmt(g.cluster_ci_high)}) is under the expected ${fmt(exp.win_rate)}.`];
    if (g.cluster_ci_low > exp.win_rate) return ['above', `Won ${fmt(g.win_rate)}; even the bottom of the range (${fmt(g.cluster_ci_low)}) beats the expected ${fmt(exp.win_rate)}.`];
    return ['on_track', `Won ${fmt(g.win_rate)} against an expected ${fmt(exp.win_rate)}; the difference is within normal noise.`];
  }
  if (exp.roi != null && g.roi_ci_low != null) {
    if (g.roi_ci_high < exp.roi) return ['below', `Return ${fmt(g.roi)}; even the top of the range is under the expected ${fmt(exp.roi)}.`];
    if (g.roi_ci_low > exp.roi) return ['above', `Return ${fmt(g.roi)}; even the bottom of the range beats the expected ${fmt(exp.roi)}.`];
    return ['on_track', `Return ${fmt(g.roi)} against an expected ${fmt(exp.roi)}; the difference is within normal noise.`];
  }
  return ['on_track', 'No benchmark to compare with yet.'];
}

let CACHE = null;
function data(nowMs) {
  // Regenerate at most hourly so "now" stays roughly current in a long dev session.
  if (!CACHE || nowMs - CACHE.at > HOUR) CACHE = { at: nowMs, ...generate(nowMs) };
  return CACHE;
}

function group(key, rows) {
  const n = rows.length;
  const wins = rows.filter((f) => f.result === 'win').length;
  const contracts = rows.reduce((s, f) => s + f.count, 0);
  const cost = rows.reduce((s, f) => s + f.cost, 0);
  const pnl = rows.reduce((s, f) => s + f.pnl, 0);
  const avgPrice = contracts ? rows.reduce((s, f) => s + f.price * f.count, 0) / contracts : null;
  const winRate = n ? wins / n : null;
  const [lo, hi] = wilson(wins, n);
  return {
    key, n, wins, contracts, cost: r2(cost), pnl: r2(pnl),
    roi: cost ? r4(pnl / cost) : null,
    avg_price: avgPrice == null ? null : r4(avgPrice),
    win_rate: winRate == null ? null : r4(winRate),
    win_ci_low: lo, win_ci_high: hi,
    edge: winRate == null ? null : r4(winRate - avgPrice),
    edge_ci_low: lo == null ? null : r4(lo - avgPrice),
    edge_ci_high: hi == null ? null : r4(hi - avgPrice),
    ...clustered(rows, avgPrice),
  };
}

// Never narrower than the per-fill Wilson interval: with every fill won (or lost) the
// cluster variance is 0 and a pure normal approximation would collapse to [1, 1].
function widen([cl, ch], [wl, wh]) {
  if (cl == null) return [cl, ch];
  return [wl == null ? cl : Math.min(cl, wl), wh == null ? ch : Math.max(ch, wh)];
}

function clustered(rows, avgPrice) {
  const wins = rows.filter((f) => f.result === 'win').length;
  const [cl, ch] = widen(clusterCi(rows, (f) => (f.result === 'win' ? 1 : 0), () => 1), wilson(wins, rows.length));
  const [rl, rh] = clusterCi(rows, (f) => f.pnl, (f) => f.cost);
  const lo = clip01(cl);
  const hi = clip01(ch);
  return {
    cluster_ci_low: lo,
    cluster_ci_high: hi,
    edge_cluster_ci_low: lo == null ? null : r4(lo - avgPrice),
    edge_cluster_ci_high: hi == null ? null : r4(hi - avgPrice),
    roi_ci_low: rl == null ? null : r4(rl),
    roi_ci_high: rh == null ? null : r4(rh),
  };
}

function groupBy(rows, fn, order) {
  const m = new Map();
  for (const f of rows) {
    const k = fn(f);
    if (k == null) continue;
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(f);
  }
  const keys = [...m.keys()];
  if (order) keys.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  else keys.sort();
  return keys.map((k) => group(k, m.get(k)));
}

function envOf(q, mode) {
  const e = q.get('env');
  if (e == null || e === '') return mode;
  return e;
}

function daysOf(q) {
  const raw = q.get('days');
  if (raw == null || raw === '') return 30;
  const d = Number(raw);
  return DAYS.includes(d) ? d : NaN;
}

const inEnv = (env) => (f) => env === 'all' || f.env === env;

const ARB_OUTCOMES0 = () => ({ filled: 0, partial: 0, error: 0, skipped: 0, log_only: 0 });
export function mockArbSummary(arbIn, nowMs) {
  const sets = arbIn.reduce((a, d) => a + d.executed, 0);
  const sightings = arbIn.reduce((a, d) => a + d.opportunities, 0);
  const win = (n) => ({
    since: iso(nowMs - n * DAY), sets: n === 1 ? 0 : sets, events: n === 1 ? 0 : sets, sightings: n === 1 ? 0 : sightings,
    by_outcome: { ...ARB_OUTCOMES0(), filled: n === 1 ? 0 : sets, log_only: n === 1 ? 0 : Math.max(0, sightings - sets) },
    skipped_by_rule: {},
  });
  return {
    tz: 'UTC', source: 'log',
    today: win(1), period: { label: 'period', ...win(30), until: iso(nowMs) },
    per_set: null,
    since_restart: { label: 'Since restart', since_utc: iso(nowMs - 3 * 3600 * 1000), scans: 0, sightings: 0 },
  };
}

export function analyticsSummary(q, { mode = 'demo', nowMs = Date.now() } = {}) {
  const days = daysOf(q);
  const env = envOf(q, mode);
  if (Number.isNaN(days)) return [422, { detail: 'days must be one of 7, 30, 90, 180' }];
  if (!['demo', 'live', 'all'].includes(env)) return [422, { detail: 'env must be demo, live or all' }];
  const { fills, arbDays } = data(nowMs);
  const since = nowMs - days * DAY;
  const pool = fills.filter(inEnv(env));
  const settled = pool.filter((f) => f.settleMs && f.settleMs >= since);
  const maker = settled.filter((f) => f.kind === 'maker');
  const open = pool.filter((f) => f.status === 'open');

  // P&L curve by settlement date (every day in the window).
  const byDay = new Map();
  for (const f of settled) {
    const k = dateKey(f.settleMs);
    const e = byDay.get(k) || { pnl: 0, maker: 0, taker: 0 };
    e.pnl += f.pnl;
    e[f.kind] += f.pnl;
    byDay.set(k, e);
  }
  const curve = [];
  let cum = 0;
  let peak = 0;
  let maxDd = 0;
  for (let t = since; t <= nowMs + 1; t += DAY) {
    const k = dateKey(t);
    if (curve.length && curve[curve.length - 1].date === k) continue;
    const e = byDay.get(k) || { pnl: 0, maker: 0, taker: 0 };
    cum += e.pnl;
    peak = Math.max(peak, cum);
    const dd = cum - peak;
    maxDd = Math.min(maxDd, dd);
    curve.push({ date: k, pnl: r2(e.pnl), cum_pnl: r2(cum), drawdown: r2(dd), maker_pnl: r2(e.maker), taker_pnl: r2(e.taker) });
  }

  const cost = settled.reduce((s, f) => s + f.cost, 0);
  const pnl = settled.reduce((s, f) => s + f.pnl, 0);
  const wins = settled.filter((f) => f.result === 'win').length;

  const cellKeys = new Set(CELLS.map(cellId));
  const byCell = groupBy(maker, (f) => f.cell, CELLS.map(cellId)).map((g) => {
    const markets = new Set(maker.filter((f) => f.cell === g.key).map((f) => f.ticker)).size;
    const exp = MOCK_EXPECTATIONS.cells[g.key] || MOCK_EXPECTATIONS.default;
    const row = {
      ...g, cell: g.key, in_config: cellKeys.has(g.key), markets,
      expected_win_rate: exp.win_rate ?? null, expected_roi: exp.roi ?? null,
    };
    const [verdict, reason] = verdictFor(row, { win_rate: row.expected_win_rate, roi: row.expected_roi });
    return { ...row, verdict, verdict_reason: reason };
  });

  // Calibration: 2-cent buckets over the cells' price span (the last one includes the top price), as the backend.
  const calibration = [];
  const loC = Math.round(LO * 100);
  const topC = Math.round(TOP * 100);
  const lastC = loC + Math.max(0, Math.ceil((topC - loC) / 2) - 1) * 2;
  for (let c = loC; c <= lastC; c += 2) {
    const lo = c / 100;
    const last = c === lastC;
    const hi = (c + 2) / 100;
    const rows = maker.filter((f) => f.price >= lo - 1e-9 && (last ? f.price <= TOP + 1e-9 : f.price < hi - 1e-9));
    const n = rows.length;
    const wins = rows.filter((f) => f.result === 'win').length;
    const [cl, ch] = widen(clusterCi(rows, (f) => (f.result === 'win' ? 1 : 0), () => 1), wilson(wins, n));
    const contracts = rows.reduce((t, f) => t + f.count, 0);
    calibration.push({
      bucket: `${lo.toFixed(2)}-${((c + 1) / 100).toFixed(2)}`,
      lo, hi, n,
      markets: new Set(rows.map((f) => f.ticker)).size,
      wins,
      win_rate: n ? r4(wins / n) : null,
      avg_price: contracts ? r4(rows.reduce((t, f) => t + f.price * f.count, 0) / contracts) : null,
      ci_low: clip01(cl),
      ci_high: clip01(ch),
    });
  }

  // Fill rate from quote episodes (by time in window).
  const eps = data(nowMs).episodes.filter((e) => inEnv(env)(e) && e.tsMs >= since);
  const frGroup = (fn, order) => {
    const m = new Map();
    for (const e of eps) {
      const k = fn(e);
      if (k == null) continue;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(e);
    }
    const keys = [...m.keys()];
    if (order) keys.sort((a, b) => order.indexOf(a) - order.indexOf(b));
    else keys.sort();
    return keys.map((k) => ({ key: k, ...fillRate(m.get(k)) }));
  };

  // Fill quality by ISO week of fill time (maker fills only).
  const fq = new Map();
  for (const f of pool.filter((x) => x.kind === 'maker' && x.tsMs >= since)) {
    const wk = isoWeek(f.tsMs);
    const e = fq.get(wk) || { week: wk, fills: 0, front: 0, trade_through: 0, q: [], requotes: 0 };
    e.fills += 1;
    if (f.fill_quality === 'front') e.front += 1;
    if (f.fill_quality === 'trade_through') e.trade_through += 1;
    if (f.queue_ahead != null) e.q.push(f.queue_ahead);
    if (f.requote) e.requotes += 1;
    fq.set(wk, e);
  }
  const fillQuality = [...fq.values()]
    .sort((a, b) => (a.week < b.week ? -1 : 1))
    .map(({ q, ...e }) => {
      const labelled = e.front + e.trade_through;
      return {
        ...e,
        front_rate: labelled ? r4(e.front / labelled) : null,
        avg_queue_ahead: q.length ? Math.round((q.reduce((s, v) => s + v, 0) / q.length) * 10) / 10 : null,
      };
    });

  const arbIn = env === 'live' ? [] : arbDays.filter((a) => a.dayMs >= since);
  const takerPnl = settled.filter((f) => f.kind === 'taker').reduce((s, f) => s + f.pnl, 0);
  const executed = arbIn.reduce((s, a) => s + a.executed, 0);

  const tzUsed = typeof q.get === 'function' && q.get('tz') ? q.get('tz') : 'UTC';
  const makerSettled = maker.length;
  const makerWins = maker.filter((f) => f.pnl > 0).length;
  const mw = wilson(makerWins, makerSettled);
  return [200, {
    env, days, generated_utc: iso(nowMs), truncated: false,
    tz_used: tzUsed,
    win_rate_by_strategy: {
      maker: {
        unit: 'fill', n: makerSettled, wins: makerWins, losses: makerSettled - makerWins,
        win_rate: makerSettled ? r4(makerWins / makerSettled) : null,
        ci_low: mw[0], ci_high: mw[1], method: 'wilson95',
      },
      arb: { unit: 'set', n: 0, wins: 0, losses: 0, win_rate: null, ci_low: null, ci_high: null, method: 'wilson95' },
    },
    deprecated: { 'totals.win_rate': 'pools maker fills and arbitrage legs; use win_rate_by_strategy' },
    arb_summary: mockArbSummary(arbIn, nowMs),
    arb_sets: [],
    arb_sets_total: 0,
    totals: {
      settled_fills: settled.length,
      contracts: settled.reduce((s, f) => s + f.count, 0),
      cost: r2(cost),
      pnl: r2(pnl),
      roi: cost ? r4(pnl / cost) : null,
      win_rate: settled.length ? r4(wins / settled.length) : null,
      open_fills: open.length,
      max_drawdown: r2(maxDd),
    },
    pnl_curve: settled.length ? curve : [],
    by_cell: byCell,
    by_series: groupBy(maker, (f) => f.series),
    by_band: groupBy(maker, (f) => f.band, A_BANDS),
    by_ttc: groupBy(maker, (f) => f.ttc_band, TTC_BANDS),
    fill_quality: fillQuality,
    expectations: { source: MOCK_EXPECTATIONS.source, loaded: true },
    calibration,
    fill_rate: {
      totals: fillRate(eps),
      by_cell: frGroup((e) => e.cell, CELLS.map(cellId)),
      by_series: frGroup((e) => e.series),
    },
    arb: {
      opportunities: arbIn.reduce((s, a) => s + a.opportunities, 0),
      executed,
      pnl: executed ? r2(takerPnl) : null,
      daily: arbIn.map((a) => ({ date: dateKey(a.dayMs), opportunities: a.opportunities, executed: a.executed })),
    },
  }];
}

const ENUMS = {
  side: ['yes', 'no'],
  kind: ['maker', 'taker'],
  status: ['settled', 'open'],
  result: ['win', 'loss'],
  band: A_BANDS,
  source: ['bot', 'you', 'all'],
};

export const TRADE_COLUMNS = ['fill_id', 'ts', 'ticker', 'series', 'side', 'kind', 'price', 'count', 'fee', 'cost', 'band',
  'ttc_hours', 'ttc_band', 'cell', 'status', 'result', 'settled_utc', 'pnl', 'fill_quality'];
export const CSV_MAX_ROWS = 5000;

export function csvCell(v) {
  if (v == null) return '';
  let s = String(v);
  if (typeof v === 'string' && /^[=+\-@]/.test(s)) s = `'${s}`; // CSV-injection safe
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** GET /api/analytics/trades.csv -> [status, text | {detail}, headers] */
export function analyticsTradesCsv(q, opts = {}) {
  const q2 = new URLSearchParams(q);
  q2.delete('offset');
  q2.set('limit', '200');
  const [st, first] = analyticsTrades(q2, opts);
  if (st !== 200) return [st, first, {}];
  const rows = [];
  for (let off = 0; off < Math.min(first.total, CSV_MAX_ROWS); off += 200) {
    q2.set('offset', String(off));
    rows.push(...analyticsTrades(q2, opts)[1].trades);
  }
  const d = new Date(opts.nowMs || Date.now()).toISOString().slice(0, 10);
  const lines = [TRADE_COLUMNS.join(','), ...rows.slice(0, CSV_MAX_ROWS).map((t) => TRADE_COLUMNS.map((k) => csvCell(t[k])).join(','))];
  const headers = {
    'Content-Type': 'text/csv; charset=utf-8',
    'Content-Disposition': `attachment; filename="trades-${d}.csv"`,
  };
  if (first.total > CSV_MAX_ROWS) headers['X-Truncated'] = 'true';
  return [200, lines.join('\r\n') + '\r\n', headers];
}

export function analyticsTrades(q, { mode = 'demo', nowMs = Date.now() } = {}) {
  const days = daysOf(q);
  const env = envOf(q, mode);
  const errs = [];
  if (Number.isNaN(days)) errs.push('days must be one of 7, 30, 90, 180');
  if (!['demo', 'live', 'all'].includes(env)) errs.push('env must be demo, live or all');
  for (const [k, allowed] of Object.entries(ENUMS)) {
    const v = q.get(k);
    if (v != null && v !== '' && !allowed.includes(v)) errs.push(`${k} must be one of ${allowed.join(', ')}`);
  }
  const series = q.get('series');
  if (series && !/^[A-Z0-9]{2,20}$/.test(series)) errs.push('series is not a valid series ticker');
  const limit = q.get('limit') == null ? 50 : Number(q.get('limit'));
  const offset = q.get('offset') == null ? 0 : Number(q.get('offset'));
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) errs.push('limit must be 1-200');
  if (!Number.isInteger(offset) || offset < 0) errs.push('offset must be >= 0');
  if (errs.length) return [422, { detail: errs }];

  const since = nowMs - days * DAY;
  const want = (k) => {
    const v = q.get(k);
    return v == null || v === '' ? null : v;
  };
  // Y1/Y5: bot-only by default; source=all adds manual trades, source=you shows only those.
  const source = want('source') || 'bot';
  const manual = source === 'bot' ? [] : mockManualTradeRows(nowMs).filter((f) => f.tsMs >= since);
  const botRows = source === 'you' ? [] : data(nowMs).fills.map((f) => ({ ...f, source: 'bot', strategy: f.kind === 'taker' ? 'arb' : 'maker' }));
  const rows = [...botRows, ...manual].sort((a, b) => b.tsMs - a.tsMs).filter((f) =>
    (f.source === 'you' || inEnv(env)(f)) &&
    f.tsMs >= since &&
    (!want('series') || f.series === want('series')) &&
    (!want('side') || f.side === want('side')) &&
    (!want('kind') || f.kind === want('kind')) &&
    (!want('status') || f.status === want('status')) &&
    (!want('band') || f.band === want('band')) &&
    (!want('cell') || f.cell === want('cell')) &&
    (!want('result') || f.result === want('result')));
  const page = rows.slice(offset, offset + limit).map((f) => ({
    fill_id: f.fill_id, ts: f.ts, ticker: f.ticker, series: f.series, side: f.side, kind: f.kind,
    price: f.price, count: f.count, fee: f.fee, cost: f.cost, band: f.band, ttc_hours: f.ttc_hours,
    ttc_band: f.ttc_band, cell: f.cell, status: f.status, result: f.result, settled_utc: f.settled_utc,
    pnl: f.pnl, fill_quality: f.fill_quality, source: f.source, strategy: f.strategy,
    ...(f.source === 'you' ? { title: f.title, note: f.note } : {}),
  }));
  return [200, { total: rows.length, offset, limit, trades: page }];
}

// ---------------------------------------------------------------- My Trades (Y5)
// GET /api/analytics/my-trades?days&env&limit -> {env, days, generated_utc, totals, bot, pnl_curve, trades, total_trades}
// GET/PUT /api/trades/{fill_id}/note (plain text, <= 200 chars). Manual trades (source "you") never feed
// analyticsSummary: the bot statistics above stay bot-only.
const MY_NOTE_MAX = 200;
const MY_SPEC = [
  // [daysAgo, ticker, title, side, price, count, result|null]
  [2, 'KXFEDDECISION-26OCT-HOLD', 'Fed holds rates in October', 'yes', 0.62, 20, null],
  [4, 'KXNBAGAME-26OCT12-BOS', 'Celtics beat the Knicks', 'yes', 0.58, 15, 'loss'],
  [7, 'KXCPI-26SEP-T0.3', 'CPI above 0.3% in September', 'no', 0.41, 25, 'win'],
  [11, 'KXNFLGAME-26OCT05-KC', 'Chiefs beat the Raiders', 'yes', 0.71, 10, 'win'],
  [15, 'KXDEMOB-26OCT01-T70', 'Demo B above 70', 'yes', 0.35, 12, 'loss'],
  [19, 'KXFEDDECISION-26SEP-CUT', 'Fed cuts rates in September', 'yes', 0.48, 18, 'win'],
  [24, 'KXNBAGAME-26SEP26-LAL', 'Lakers beat the Suns', 'no', 0.44, 14, 'loss'],
  [33, 'KXGDP-26Q2-T3', 'GDP growth above 3%', 'yes', 0.52, 16, 'win'],
  [47, 'KXSNOW-26SEP-NYC', 'Example event in September', 'no', 0.63, 30, 'win'],
];
const MY_NOTES = new Map([['my-003', 'Fed hunch after the speech']]);
let MY_CACHE = null;

function myRows(nowMs) {
  if (!MY_CACHE || nowMs - MY_CACHE.at > HOUR) {
    const rows = MY_SPEC.map(([ago, tk, title, side, price, count, result], i) => {
      const tsMs = nowMs - ago * DAY - 3 * HOUR;
      const settled = result != null;
      const cost = r2(price * count);
      const pnl = settled ? r2(result === 'win' ? (1 - price) * count : -price * count) : null;
      return {
        fill_id: `my-${String(MY_SPEC.length - 1 - i).padStart(3, '0')}`,
        ts: iso(tsMs), tsMs, ticker: tk, title, series: tk.split('-')[0], side, kind: 'maker', price, count,
        fee: 0, cost, status: settled ? 'settled' : 'open', result,
        settled_utc: settled ? iso(tsMs + 2 * DAY) : null, pnl, source: 'you', strategy: null, band: priceBand(price),
        cell: null, fill_quality: null,
      };
    });
    MY_CACHE = { at: nowMs, rows };
  }
  return MY_CACHE.rows;
}

function totalsOf(rows, open) {
  const s = rows.filter((r) => r.status === 'settled');
  const cost = s.reduce((a, r) => a + r.cost, 0);
  const pnl = s.reduce((a, r) => a + r.pnl, 0);
  let peak = 0;
  let cum = 0;
  let dd = 0;
  for (const r of [...s].sort((a, b) => a.tsMs - b.tsMs)) {
    cum += r.pnl;
    peak = Math.max(peak, cum);
    dd = Math.min(dd, cum - peak);
  }
  return {
    settled_fills: s.length, contracts: s.reduce((a, r) => a + r.count, 0), cost: r2(cost), pnl: r2(pnl),
    roi: cost ? r4(pnl / cost) : null, win_rate: s.length ? r4(s.filter((r) => r.result === 'win').length / s.length) : null,
    open_fills: open, max_drawdown: r2(dd),
  };
}

export function mockMyTrades(q, { nowMs = Date.now(), botPnl = 16.4 } = {}) {
  const days = daysOf(q);
  if (Number.isNaN(days)) return [422, { detail: ['days must be one of 7, 30, 90, 180'] }];
  const lo = nowMs - days * DAY;
  const rows = myRows(nowMs).filter((r) => r.tsMs >= lo);
  const limit = q.get('limit') == null ? 100 : Number(q.get('limit'));
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) return [422, { detail: ['limit must be 1-200'] }];
  const settled = rows.filter((r) => r.status === 'settled').sort((a, b) => a.tsMs - b.tsMs);
  let cum = 0;
  let peak = 0;
  const byDay = new Map();
  for (const r of settled) {
    cum = r2(cum + r.pnl);
    peak = Math.max(peak, cum);
    byDay.set(dateKey(r.tsMs + 2 * DAY), { date: dateKey(r.tsMs + 2 * DAY), pnl: r2((byDay.get(dateKey(r.tsMs + 2 * DAY)) || { pnl: 0 }).pnl + r.pnl), cum_pnl: cum, drawdown: r2(cum - peak) });
  }
  const trades = rows.slice(0, limit).map((r) => {
    const { tsMs, ...pub } = r; // eslint-disable-line no-unused-vars
    return { ...pub, note: MY_NOTES.get(r.fill_id) || null };
  });
  return [200, {
    env: envOf(q, 'demo'), days, generated_utc: iso(nowMs),
    totals: totalsOf(rows, rows.filter((r) => r.status === 'open').length),
    bot: { settled_fills: 140, contracts: 900, cost: 820, pnl: botPnl, roi: r4(botPnl / 820), win_rate: 0.63, open_fills: 12, max_drawdown: -4.1 },
    pnl_curve: [...byDay.values()], trades, total_trades: rows.length,
  }];
}

export function mockTradeNote(method, fillId, body) {
  if (!/^[A-Za-z0-9_.:-]{1,80}$/.test(fillId || '')) return [400, { detail: ['fill_id: invalid'] }];
  if (!myRows(Date.now()).some((r) => r.fill_id === fillId)) return [404, { detail: 'fill_not_found' }];
  if (method === 'GET') return [200, { fill_id: fillId, note: MY_NOTES.get(fillId) || '' }];
  const raw = body && body.note;
  if (raw != null && typeof raw !== 'string') return [400, { detail: ['note: must be a string'] }];
  const note = String(raw || '').replace(/\s+/g, ' ').trim();
  if (note.length > MY_NOTE_MAX) return [400, { detail: [`note: at most ${MY_NOTE_MAX} characters`] }];
  if (note) MY_NOTES.set(fillId, note);
  else MY_NOTES.delete(fillId);
  return [200, { fill_id: fillId, note }];
}

/** Rows for GET /api/analytics/trades?source=all|you (bot rows get source "bot"). */
export function mockManualTradeRows(nowMs = Date.now()) {
  return myRows(nowMs).map((r) => ({ ...r, note: MY_NOTES.get(r.fill_id) || null }));
}

const ZERO = { fills: 0, contracts: 0, cost: 0, fees: 0, settled_fills: 0, wins: 0, losses: 0, settled_cost: 0, realized_pnl: 0, open_fills: 0, open_cost: 0, roi_on_settled: null };

/** GET /api/analytics/ledger (UI-1a shape). Small fixed numbers: the mock is for the UI, not the maths. */
export function mockLedger(q, mode = 'demo') {
  const row = (strategy, extra) => ({ strategy, unit: strategy === 'arb' ? 'set' : 'fill', ...ZERO, ...extra });
  const maker = row('maker', { fills: 12, contracts: 60, cost: 49.2, fees: 0.8, settled_fills: 10, wins: 9, losses: 1, settled_cost: 41, realized_pnl: 1.9, open_fills: 2, open_cost: 8.2, roi_on_settled: 0.046 });
  const arb = row('arb', { fills: 2, contracts: 10, cost: 9.5, fees: 0.3, settled_fills: 2, wins: 2, settled_cost: 9.5, realized_pnl: 0.35 });
  const you = row('you');
  const sum = (k) => Math.round((maker[k] + arb[k] + you[k]) * 100) / 100;
  return {
    tz_used: q.get('tz') || 'UTC', attribution: 'fill date (local day in tz_used); realized P&L follows the fill',
    settled_attribution: 'settlement date (local day in tz_used)',
    settled_by_strategy: [
      { strategy: 'maker', settled_fills: 10, wins: 9, losses: 1, realized_pnl: 1.9 },
      { strategy: 'arb', settled_fills: 2, wins: 2, losses: 0, realized_pnl: 0.35 },
      { strategy: 'you', settled_fills: 0, wins: 0, losses: 0, realized_pnl: 0 },
      { strategy: 'total', settled_fills: 12, wins: 11, losses: 1, realized_pnl: 2.25 },
    ],
    env: q.get('env') || mode,
    days: 30, strategies: [maker, arb, you],
    total: { ...ZERO, fills: sum('fills'), cost: sum('cost'), fees: sum('fees'), realized_pnl: sum('realized_pnl'), open_cost: sum('open_cost') },
    reconciliation: {
      tolerance: 0.1, flag: false, residual: 0.02, equity_start: 100, equity_end: 102.37, equity_change: 2.37, net_flows: 0,
      realized_pnl: 2.25, fee_timing: 0.1, expected_equity_change: 2.35, start_exact: true, notes: [],
    },
    netting: { tickers: 0, pairs: 0, pair_cost: 0, pair_value: 0, pair_gain: 0, verified: false },
    notes: [],
  };
}

/** GET /api/analytics/scorecard (UI-1a shape). */
export function mockScorecard(q, mode = 'demo') {
  return {
    env: q.get('env') || mode, label: 'last 7 days',
    passes: { n: 480, ok: 476, ok_pct: 0.9917, errors: 4, median_duration_s: 3.2, max_duration_s: 21 },
    order_errors: { value: null, error_alerts: 1 },
    churn: { quotes: 300, cancels: 120, cancels_per_quote: 0.4 },
    cap_bindings: { maker: null, arbitrage_gate: {} },
    fill_to_settlement: { due_fills: 10, settled: 9, unsettled: 1, rate: 0.9, grace_hours: 24 },
    open_past_expected_settlement: { count: 0, cost: 0, grace_hours: 24, close_unknown: 0 },
    reconciliation: { residual: 0.02, tolerance: 0.1, flag: false },
    alerts_by_type: {},
    arbitrage_per_set: { legged: 0, complete: 2, skipped_by_gate: 0, skipped_by_rule: {}, log_only: 5, settled_sets: 2, set_wins: 2, distinct_sets_logged: 7 },
    stop_start_log: { stops: 1, stop_failures: 0, starts: 2 },
    return: { label: 'demo, indicative only', sample_settled_fills: 12, realized_pnl: 2.25, cost: 50.5, roi: 0.0446 },
    notes: ['Order-error counters and maker cap bindings are not stored yet.'],
  };
}

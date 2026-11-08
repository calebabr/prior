// Dev-only synthetic Day view for the fake backend (services/mock.js): GET /api/analytics/day. Deterministic per date.
// Demo tickers only. Nothing here talks to the network.

const get = (q, k) => (q && typeof q.get === 'function' ? q.get(k) : q ? q[k] : null);

function seedOf(date) {
  let h = 7;
  for (const c of String(date)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
}

export function mockDay(q, { mode = 'demo', nowMs = Date.now() } = {}) {
  const date = get(q, 'date');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '')) return [422, { detail: [{ loc: ['query', 'date'], msg: 'must be a date YYYY-MM-DD' }] }];
  const strategy = get(q, 'strategy') || null;
  if (strategy && !['maker', 'arb'].includes(strategy)) return [422, { detail: [{ loc: ['query', 'strategy'], msg: 'must be one of' }] }];
  const seed = seedOf(date);
  const today = new Date(nowMs);
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  if (date > todayStr) return [422, { detail: [{ loc: ['query', 'date'], msg: 'must not be in the future' }] }];
  const isToday = date === todayStr;
  const n = 4 + (seed % 5);
  const rows = [];
  for (let i = 0; i < n; i += 1) {
    const strat = i % 4 === 3 ? 'arb' : 'maker';
    const price = +(0.82 + ((seed + i) % 8) * 0.02).toFixed(2);
    const count = 2 + ((seed + i) % 6);
    const open = isToday && i % 3 === 2;
    const lost = !open && (seed + i) % 7 === 0;
    const cost = +(price * count).toFixed(2);
    rows.push({
      fill_id: `${date}-${i}`,
      label: `KXDEMOA-${date.replace(/-/g, '').slice(2)}-T${60 + i}`,
      ticker: `KXDEMOA-${date.replace(/-/g, '').slice(2)}-T${60 + i}`,
      series: 'KXDEMOA', strategy: strat, side: i % 2 ? 'no' : 'yes', count, price, cost,
      ts: `${date}T${String(8 + i).padStart(2, '0')}:10:00Z`,
      status: open ? 'open' : lost ? 'lost' : 'won',
      settles_at: open ? `${date}T23:00:00Z` : null,
      pnl: open ? null : lost ? -cost : +((1 - price) * count).toFixed(2),
    });
  }
  const kept = strategy ? rows.filter((r) => r.strategy === strategy) : rows;
  const settled = kept.filter((r) => r.status !== 'open').map((r) => ({ ...r, settled_utc: `${date}T${String(18 + (r.fill_id.length % 4)).padStart(2, '0')}:00:00Z` }));
  const wins = settled.filter((r) => r.status === 'won').length;
  const filled = kept.map(({ pnl, ...r }) => r); // eslint-disable-line no-unused-vars
  const frac = (x) => +x.toFixed(2);
  return [
    200,
    {
      date, tz_used: get(q, 'tz') || 'UTC', env: get(q, 'env') || mode, generated_utc: new Date(nowMs).toISOString(), strategy,
      summary: {
        filled: { count: filled.length, cost: +filled.reduce((a, r) => a + r.cost, 0).toFixed(2) },
        settled: { count: settled.length, wins, losses: settled.length - wins },
        realized: +settled.reduce((a, r) => a + r.pnl, 0).toFixed(2),
        settles_later: filled.filter((r) => r.status === 'open').length,
      },
      filled, settled,
      analytics: [
        { key: 'fill_rate', label: 'Fill rate', today: frac(0.3 + (seed % 20) / 100), avg7: 0.38 },
        { key: 'maker_share', label: 'Maker share of fills', today: frac(0.7), avg7: 0.72 },
        { key: 'win_rate', label: 'Win rate', today: settled.length ? frac(wins / settled.length) : null, avg7: 0.9 },
      ],
      notes: [],
    },
  ];
}

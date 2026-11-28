// When a 95% range is worth showing. Display only: the numbers themselves come from the server unchanged.
// With only a few markets, or no loss yet, the computed range comes out narrow and above zero and looks certain
// when it is not.

/** The health check's own minimum of distinct markets before it judges anything. */
export const MIN_RANGE_MARKETS = 20;
export const TOO_FEW_TEXT = 'Too few results for a range.';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * stats: {events, fills, ...} from /api/markets or the health summary. record: {n, wins} for the same market from
 * /api/analytics/summary `by_series` when it has been read (null otherwise).
 */
export function tooFewForRange(stats, record = null) {
  const s = stats || {};
  const markets = isNum(s.events) ? s.events : s.fills; // distinct markets never outnumber fills
  if (!isNum(markets) || markets < MIN_RANGE_MARKETS) return true;
  return !!record && isNum(record.n) && record.n > 0 && record.wins === record.n;
}

/** by_series -> {series: {n, wins}}, plus `all` for the whole window. Empty when the summary was not read. */
export function winRecords(summary) {
  const rows = summary && Array.isArray(summary.by_series) ? summary.by_series.filter((g) => g && isNum(g.n) && isNum(g.wins)) : [];
  const out = {};
  for (const g of rows) out[g.key] = { n: g.n, wins: g.wins };
  if (rows.length) {
    out.all = rows.reduce((a, g) => ({ n: a.n + g.n, wins: a.wins + g.wins }), { n: 0, wins: 0 });
  }
  return out;
}

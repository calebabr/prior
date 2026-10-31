// Helpers for the Returns bars, table and sheet (docs/RETURNS_FEATURE_CONTRACT.md). Pure functions.
import { DASH, NA_TITLE, formatMoney, formatPeriodReturn } from './format.js';

export { NA_TITLE };
export const MIN_SLOT_PX = 9;
export const SMALL_SAMPLE_DEFAULT = 5;

const NAMES = { maker: 'Maker', arb: 'Arbitrage', you: 'You' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** "maker" -> "Maker", "arb" -> "Arbitrage"; a new strategy key is capitalised, never dropped. */
export function strategyLabel(key) {
  return NAMES[key] || (key ? String(key).charAt(0).toUpperCase() + String(key).slice(1) : DASH);
}

/** Series colour by position in the server's order: accent, second chart colour, then neutral. */
export function strategyColor(index) {
  return ['var(--accent)', 'var(--chart2)', 'var(--off)'][Math.min(index, 2)];
}

function parts(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  return m ? { y: +m[1], m: +m[2] - 1, d: +m[3] } : null;
}

/** "2026-10-26" -> "Oct 26" (month granularity: "Oct"). */
export function shortLabel(period, granularity) {
  const p = parts(period && period.start_local);
  if (!p) return (period && period.label) || DASH;
  return granularity === 'month' ? MONTHS[p.m] : `${MONTHS[p.m]} ${p.d}`;
}

/** Long label with the "so far" mark on a partial period: "Today so far", "This week so far", "October so far". */
export function periodTitle(period, granularity) {
  if (!period) return DASH;
  const p = parts(period.start_local);
  const base = granularity === 'month' && p ? MONTHS_LONG[p.m] : period.label || shortLabel(period, granularity);
  if (!period.partial) return base;
  if (granularity === 'day') return 'Today so far';
  if (granularity === 'week') return 'This week so far';
  return `${base} so far`;
}

/** Positive and negative stacks of a period, for the bar geometry. */
export function stacks(period) {
  const vals = Object.values((period && period.by_strategy) || {}).filter(isNum);
  return { pos: vals.filter((v) => v > 0).reduce((a, b) => a + b, 0), neg: vals.filter((v) => v < 0).reduce((a, b) => a + b, 0) };
}

/** Value range for the y scale; always includes zero. */
export function scaleOf(periods) {
  let hi = 0;
  let lo = 0;
  for (const p of periods) {
    const s = stacks(p);
    hi = Math.max(hi, s.pos);
    lo = Math.min(lo, s.neg);
  }
  if (hi === 0 && lo === 0) hi = 1;
  const pad = (hi - lo) * 0.06;
  return { hi: hi > 0 ? hi + pad : 0, lo: lo < 0 ? lo - pad : 0 };
}

export function isSmall(period, threshold = SMALL_SAMPLE_DEFAULT) {
  if (!period) return false;
  if (typeof period.small_sample === 'boolean') return period.small_sample;
  return period.settled_fills > 0 && period.settled_fills < threshold;
}

/** One sentence per bar: the aria-label and the table's row summary. */
export function barLabel(period, granularity) {
  const bits = [
    `${periodTitle(period, granularity)}: ${formatMoney(period.realized_pnl, { sign: true })}`,
    isNum(period.return_rate) ? formatPeriodReturn(period.return_rate) : 'return not available',
    period.settled_fills ? `${period.settled_fills} settled fill${period.settled_fills === 1 ? '' : 's'}` : 'no settlements',
  ];
  if (isSmall(period)) bits.push('small sample');
  return bits.join(', ');
}

/** Reason text for a null return, shown as the dash's tooltip. */
export function reasonText(period) {
  const r = period && period.return_reason;
  if (r === 'no_start_snapshot') return 'No starting balance was stored for this period.';
  if (r === 'stale_start_snapshot') return 'The last stored balance was too old to use as the start.';
  if (r === 'base_too_small') return 'The starting balance was too small for a meaningful percent.';
  if (r === 'env_not_current') return 'Percent is only available for the current account.';
  return NA_TITLE;
}

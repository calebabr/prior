// Helpers for the Day view (pages/Day.jsx, components/day/*). Pure functions.
import { DASH } from './format.js';

export const DAY_PATH = '/performance/day';
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

export const isDateString = (s) => DATE_RE.test(String(s || ''));

/** Today's local date in the viewer's zone, "YYYY-MM-DD" (the same zone the API is asked for). */
export function todayString(now = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
}

export const dayPath = (date) => `${DAY_PATH}/${date}`;

/** "Fri Nov 6" for a local date string; the string itself when it is not a date. */
export function dayLabel(date) {
  if (!isDateString(date)) return String(date || DASH);
  const [y, m, d] = date.split('-').map(Number);
  const dow = new Date(y, m - 1, d).getDay();
  return `${DOW[dow]} ${MONTHS[m - 1]} ${d}`;
}

/** "6 fills, 4 settled, +$1.20" (+ ", 2 settle later"). `fmt` formats signed dollars. */
export function verdict(summary, fmt) {
  if (!summary) return '';
  const f = summary.filled ? summary.filled.count : 0;
  const s = summary.settled ? summary.settled.count : 0;
  const later = isNum(summary.settles_later) ? summary.settles_later : 0;
  let out = `${f} fill${f === 1 ? '' : 's'}, ${s} settled, ${fmt(summary.realized)}`;
  if (later > 0) out += `, ${later} settle${later === 1 ? 's' : ''} later`;
  return out;
}

/** The market event a ticker belongs to: the ticker without its last segment (KXA-26OCT10-B70 -> KXA-26OCT10). */
export function eventOf(ticker) {
  const t = String(ticker || '');
  const i = t.lastIndexOf('-');
  return i > 0 ? t.slice(0, i) : t;
}

/** Rows grouped by event (first appearance order), keeping their order inside an event: [{event, rows}]. */
export function groupByEvent(rows) {
  const order = [];
  const map = new Map();
  for (const r of rows || []) {
    const e = eventOf(r.ticker);
    if (!map.has(e)) {
      map.set(e, []);
      order.push(e);
    }
    map.get(e).push(r);
  }
  return order.map((event) => ({ event, rows: map.get(event) }));
}

export const STATUS_WORD = { open: 'Open', won: 'Won', lost: 'Lost' };
export const STATUS_TONE = { open: 'muted', won: 'ok', lost: 'danger' };

export const DAY_CHIPS = [
  { id: 'all', label: 'All' },
  { id: 'maker', label: 'Maker' },
  { id: 'arb', label: 'Arbitrage' },
];

/** A fraction as a whole percent: 0.5 -> "50%"; null -> the dash. */
export const pctWhole = (v) => (isNum(v) ? `${Math.round(v * 100)}%` : DASH);

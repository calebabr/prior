// Small display helpers. All tolerate null/undefined and return '—'.

export const DASH = '—';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

const MINUS = '−'; // typographic minus, used by the contract formatters

// One implementation behind formatMoney (typographic minus) and the legacy money (ASCII hyphen).
function fmtMoney(v, { sign = false, compact = false, digits = 2 } = {}, minus = '-') {
  if (!isNum(v)) return DASH;
  let body;
  if (compact && Math.abs(v) >= 1000) {
    const k = Math.abs(v) / 1000;
    body = k >= 1000 ? `${(k / 1000).toFixed(1)}M` : `${k.toFixed(1)}k`;
    body = body.replace(/\.0(?=[kM]$)/, '');
  } else {
    body = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  // A value that rounds to zero shows as $0.00 with no sign.
  const zero = /^[0.,]+$/.test(body);
  if (v < 0 && !zero) return `${minus}$${body}`;
  return `${sign && v > 0 && !zero ? '+' : ''}$${body}`;
}

/**
 * Money: "$12.30" (2 decimals, thousands separators). `sign` adds "+" / "−" (P&L always passes it);
 * `compact` gives "$1.2k" for values >= 1000 where a phone is tight; null/NaN -> "—".
 */
export function formatMoney(v, opts) {
  return fmtMoney(v, opts, MINUS);
}

// Legacy name kept so existing pages and tests keep working: same output but an ASCII "-".
export function money(v, opts) {
  return fmtMoney(v, opts, '-');
}

function fmtPct(v, { sign = true, digits = 2 } = {}, minus = '-') {
  if (!isNum(v)) return DASH;
  const n = v * 100;
  const body = Math.abs(n).toFixed(digits);
  const zero = Number(body) === 0;
  if (n < 0 && !zero) return `${minus}${body}%`;
  return `${sign && n > 0 && !zero ? '+' : ''}${body}%`;
}

/** Rates as a percent: 0.015 -> "+1.50%". Never "pts" in the main view. */
export function formatPct(v, opts) {
  return fmtPct(v, opts, MINUS);
}

// Legacy name (ASCII hyphen for negatives).
export function pct(v, opts) {
  return fmtPct(v, opts, '-');
}

export function num(v, digits = 0) {
  if (!isNum(v)) return DASH;
  return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function parseDate(v) {
  if (!v) return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

const TIME_FMT = { hour: 'numeric', minute: '2-digit' };
const DATE_TIME_FMT = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };

// Exact instant in UTC for tooltips: "2026-10-15 02:41:07 UTC".
export function utcTitle(v) {
  const d = parseDate(v);
  if (!d) return '';
  return `${d.toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

/**
 * Device-time-zone time as {text, title}: text "9:41 PM" (or "Oct 15, 9:41 PM" with `withDate`),
 * title = the exact UTC instant for a tooltip / long-press. Export CSV stays UTC.
 */
export function formatTimeParts(v, { withDate = false } = {}) {
  const d = parseDate(v);
  if (!d) return { text: DASH, title: '' };
  return { text: d.toLocaleString('en-US', withDate ? DATE_TIME_FMT : TIME_FMT), title: utcTitle(d) };
}

export function formatTime(v, opts) {
  return formatTimeParts(v, opts).text;
}

// Legacy names.
export function dateTime(v) {
  return formatTime(v, { withDate: true });
}

export function time(v) {
  return formatTime(v);
}

// Seconds -> "45s", "12 min", "3 h 5 min", "2 d 4 h"
export function duration(seconds) {
  if (!isNum(seconds)) return DASH;
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

export function ago(v, now = Date.now()) {
  const d = parseDate(v);
  if (!d) return DASH;
  const diff = (now - d.getTime()) / 1000;
  if (diff < 0) return 'in ' + duration(-diff);
  return duration(diff) + ' ago';
}

export function until(v, now = Date.now()) {
  const d = parseDate(v);
  if (!d) return DASH;
  const diff = (d.getTime() - now) / 1000;
  if (diff <= 0) return 'due now';
  return 'in ' + duration(diff);
}

/** "4 min ago" (a future time reads "in 5 min"). */
export const formatAgo = ago;
/** "in 5 min" ("due now" once past). */
export const formatUntil = until;

// "keepalive_minutes" -> "Keepalive minutes"
export function humanize(key) {
  if (!key) return '';
  const s = String(key).replace(/_/g, ' ').replace(/\butc\b/i, 'UTC').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Normalise a FastAPI-style `detail` (string | string[] | [{msg, loc}] | object) to string[].
export function detailList(detail) {
  if (detail == null) return [];
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (typeof d === 'string') return d;
      if (d && typeof d === 'object') {
        const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== 'body').join('.') : '';
        return (loc ? loc + ': ' : '') + (d.msg || JSON.stringify(d));
      }
      return String(d);
    });
  }
  if (typeof detail === 'object') return [detail.msg || JSON.stringify(detail)];
  return [String(detail)];
}

// ---------------------------------------------------------------- Title Case (item 21)

const SMALL_WORDS = new Set(['a', 'an', 'the', 'and', 'or', 'of', 'by', 'at', 'to', 'in', 'on', 'for']);
const ACRONYMS = { 'p&l': 'P&L', pnl: 'P&L', roi: 'ROI', ci: 'CI', utc: 'UTC', '2fa': '2FA', api: 'API' };
const TICKER = /^KX[A-Z0-9-]+$/;

function capFirst(w) {
  const m = /^([^A-Za-z]*)([a-z])(.*)$/.exec(w);
  return m ? m[1] + m[2].toUpperCase() + m[3] : w;
}

function caseWord(word, first) {
  // Peel leading/trailing punctuation: "(demo" "live)" "fills:" ...
  const m = /^([^A-Za-z0-9&$%]*)(.*?)([^A-Za-z0-9&$%]*)$/.exec(word);
  const [, lead, core, trail] = m;
  if (!core) return word;
  if (TICKER.test(core)) return word;
  const lower = core.toLowerCase();
  if (ACRONYMS[lower] && core === lower) return lead + ACRONYMS[lower] + trail;
  if (!first && SMALL_WORDS.has(lower) && core !== core.toUpperCase()) return lead + lower + trail;
  // Already has a capital after the first letter (P&L, iPhone, ROI): leave it alone.
  if (/[A-Z]/.test(core.slice(1))) return word;
  const parts = core.split(/([-/])/).map((p) => {
    if (p === '-' || p === '/' || !p) return p;
    if (/\d/.test(p)) return p; // "30", "24h", "$1.20" stay as written
    if (ACRONYMS[p.toLowerCase()] && p === p.toLowerCase()) return ACRONYMS[p.toLowerCase()];
    return capFirst(p);
  });
  return lead + parts.join('') + trail;
}

/**
 * Title Case for titles and short labels (never for sentences). Small words (a an the and or of by at to
 * in on for) stay lowercase unless first; ticker codes (KXDEMOD) and tokens with digits are unchanged;
 * hyphen parts are each capitalised ("Re-Quoting"); acronyms (P&L, ROI, CI, UTC) are kept.
 */
export function titleCase(s) {
  if (s == null) return '';
  return String(s)
    .split(/(\s+)/)
    .map((tok, i, arr) => {
      if (!tok || /^\s+$/.test(tok)) return tok;
      return caseWord(tok, arr.slice(0, i).every((t) => !t || /^\s+$/.test(t)));
    })
    .join('');
}

// ---------------------------------------------------------------- UI-1a stage 2 rules (sections 5.3 and 5.5)

/** Tooltip for a missing value: render DASH with title={NA_TITLE}; never 0 or blank. */
export const NA_TITLE = 'Not available yet';

/** Returns as one-decimal percent, always signed: 0.046 -> "+4.6%", -0.004 -> "−0.4%", ~0 -> "0.0%". */
export function formatReturn(v, { sign = true } = {}) {
  return fmtPct(v, { sign, digits: 1 }, MINUS);
}

/** Return for one period: two decimals under 1%, one decimal from 1% up, signed, U+2212 minus, dash for null. */
export function formatPeriodReturn(v) {
  if (!isNum(v)) return DASH;
  return fmtPct(v, { sign: true, digits: Math.abs(v) < 0.01 ? 2 : 1 }, MINUS);
}

/** Shares and rates, zero decimals, no sign: 0.914 -> "91%". */
export function formatShare(v) {
  return fmtPct(v, { sign: false, digits: 0 }, MINUS);
}

/** Confidence interval for a return: (−0.4% to +5.1%). Missing bound -> "". */
export function formatInterval(lo, hi) {
  if (!isNum(lo) || !isNum(hi)) return '';
  return `(${formatReturn(lo)} to ${formatReturn(hi)})`;
}

const MONTH_DAY = { month: 'short', day: 'numeric' };
const MONTH_DAY_YEAR = { month: 'short', day: 'numeric', year: 'numeric' };
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/**
 * Absolute date for tables and headings, in the device time zone (UTC only in tooltips and CSV):
 * today "5:19 PM", yesterday "Yesterday 5:19 PM", this year "Oct 30", another year "Oct 30, 2025".
 * Feeds and anything under 24 h use formatAgo ("4 min ago") instead.
 */
export function formatWhen(v, now = Date.now()) {
  const d = parseDate(v);
  if (!d) return DASH;
  const n = new Date(now);
  const days = Math.round((startOfDay(n) - startOfDay(d)) / 86400000);
  const t = d.toLocaleTimeString('en-US', TIME_FMT);
  if (days === 0) return t;
  if (days === 1) return `Yesterday ${t}`;
  return d.toLocaleDateString('en-US', d.getFullYear() === n.getFullYear() ? MONTH_DAY : MONTH_DAY_YEAR);
}

/** Date range with no dash: "Oct 1 to Oct 30". An open end reads "Oct 1 to now". */
export function formatRange(from, to, now = Date.now()) {
  const a = parseDate(from);
  if (!a) return DASH;
  const day = (d) => {
    const sameYear = d.getFullYear() === new Date(now).getFullYear();
    return d.toLocaleDateString('en-US', sameYear ? MONTH_DAY : MONTH_DAY_YEAR);
  };
  const b = parseDate(to);
  return `${day(a)} to ${b ? day(b) : 'now'}`;
}

/** Sentence case for buttons, chips, headlines, help text and notices: only the first letter is raised. */
export function sentenceCase(s) {
  if (s == null) return '';
  const t = String(s);
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** Strategy name for chrome: the registry's "Bot · Maker" -> "Maker". Analytics keeps the full name. */
export function strategyShortName(name) {
  if (name == null) return '';
  return String(name).replace(/^\s*bot\s*[·•:-]\s*/i, '').trim();
}

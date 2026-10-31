// Pure helpers for the Overview page: headline, abnormal-state detection, ring progress, the
// "Since You Last Checked" bookkeeping and the specific toast texts (items 1, 3, 25, 30, 39).
import { readWithLegacy } from './theme.js';
import { badgeKind, HEARTBEAT_STALE_S } from './status.js';
import { formatMoney, formatTimeParts, parseDate, titleCase } from './format.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

export const LAST_VISIT_KEY = 'prior.lastVisit';

export function readLastVisit() {
  try {
    const v = readWithLegacy(window.localStorage, LAST_VISIT_KEY);
    return v && parseDate(v) ? v : null;
  } catch {
    return null;
  }
}

export function writeLastVisit(iso = new Date().toISOString()) {
  try {
    window.localStorage.setItem(LAST_VISIT_KEY, iso);
  } catch {
    /* storage blocked: the card simply never shows */
  }
}

/** The status fields with the contract names first and the older names as a fallback. */
export function runTimes(status) {
  return {
    last: (status && (status.last_run_at || status.last_run_utc)) || null,
    next: (status && (status.next_run_at || status.next_due_utc)) || null,
  };
}

export function isRunning(status) {
  return !!status && status.desired === 'running' && !status.kill_switch;
}

/**
 * Everything that is wrong right now, as short strings the UI shows in red.
 * Empty array = all fine. Each item: {key, text}.
 */
export function abnormalities(status, now = Date.now()) {
  if (!status) return [];
  const out = [];
  const running = isRunning(status);
  if (status.last_error) out.push({ key: 'error', text: `Last error: ${String(status.last_error)}` });
  const age = status.heartbeat_age_s;
  if (running && isNum(age) && age > HEARTBEAT_STALE_S) {
    out.push({ key: 'heartbeat', text: `No heartbeat for ${Math.round(age / 60)} min` });
  }
  if (status.desired === 'running' && status.kill_switch) out.push({ key: 'kill', text: 'Kill switch is set' });
  const { next } = runTimes(status);
  const nextMs = next ? Date.parse(next) : NaN;
  if (running && isNum(nextMs) && now - nextMs > 2 * 60 * 1000) out.push({ key: 'overdue', text: 'Next run is overdue' });
  const passes = Array.isArray(status.recent_passes) ? status.recent_passes : [];
  const lastPass = passes[passes.length - 1];
  if (lastPass && lastPass.ok === false) out.push({ key: 'pass', text: 'Last run had errors' });
  if (status.fills_sync && status.fills_sync.error) out.push({ key: 'sync', text: 'Fill sync failed' });
  return out;
}

/** Overall state for the card, widget and headline. */
export function overallState(status, now = Date.now()) {
  if (!status) return { key: 'unknown', label: 'Loading', tone: 'muted' };
  const bad = abnormalities(status, now);
  const { kind } = badgeKind(status);
  const hard = bad.some((b) => b.key === 'error' || b.key === 'heartbeat' || b.key === 'overdue');
  if (kind === 'error' || hard) return { key: 'error', label: 'Needs Attention', tone: 'danger' };
  if (kind === 'stopped') return { key: 'stopped', label: 'Stopped', tone: 'muted' };
  return { key: kind, label: 'Running', tone: kind === 'live' ? 'ok' : 'warn' };
}

/** 0..1: how far we are from the last run to the next one (the ring fills as the next run approaches). */
export function ringProgress(status, now = Date.now()) {
  if (!isRunning(status)) return 0;
  const { last, next } = runTimes(status);
  const a = last ? Date.parse(last) : NaN;
  const b = next ? Date.parse(next) : NaN;
  if (!isNum(a) || !isNum(b) || b <= a) return 0;
  return Math.max(0, Math.min(1, (now - a) / (b - a)));
}

/** Edge verdict across cells from GET /api/analytics/summary -> {text, tone} | null. */
export function edgeSummary(summary) {
  const cells = ((summary && summary.by_cell) || []).filter((c) => c && c.verdict);
  if (!cells.length) return null;
  if (cells.some((c) => c.verdict === 'below')) return { text: 'Edge Below Expected', tone: 'warn' };
  if (cells.every((c) => c.verdict === 'too_early')) return { text: 'Edge Too Early', tone: 'muted' };
  return { text: 'Edge On Track', tone: 'ok' };
}

/** Headline parts for "Demo Running · +$17.12 Over 30 Days · Edge On Track". */
export function headlineParts(status, edge, now = Date.now()) {
  const st = overallState(status, now);
  const mode = status && status.mode === 'live' ? 'Live' : 'Demo';
  const head =
    st.key === 'error'
      ? { text: 'Needs Attention', tone: 'danger' }
      : st.key === 'stopped'
        ? { text: `${mode} Stopped`, tone: 'muted' }
        : st.key === 'unknown'
          ? { text: 'Loading Status', tone: 'muted' }
          : { text: `${mode} Running`, tone: st.tone };
  const parts = [head];
  const d30 = status && status.pnl && status.pnl.d30;
  if (isNum(d30)) {
    parts.push({ text: `${formatMoney(d30, { sign: true })} Over 30 Days`, tone: d30 < 0 ? 'danger' : d30 > 0 ? 'ok' : 'muted' });
  }
  if (edge) parts.push(edge);
  return parts;
}

/** "9:12 PM" today, "Yesterday 9:12 PM", else "Oct 12, 9:12 PM". */
export function sinceLabel(iso, now = Date.now()) {
  const d = parseDate(iso);
  if (!d) return '';
  const t = formatTimeParts(iso).text;
  const day = (x) => new Date(x).setHours(0, 0, 0, 0);
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff <= 0) return t;
  if (diff === 1) return `Yesterday ${t}`;
  return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}, ${t}`;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** Parts of the Since You Last Checked line, skipping zeros. */
export function sinceParts(since, label) {
  const parts = [];
  if (since && since.fills > 0) parts.push(titleCase(plural(since.fills, 'new fill', 'new fills')));
  if (since && since.settled > 0) parts.push(titleCase(plural(since.settled, 'market settled', 'markets settled')));
  if (since && isNum(since.pnl_change) && since.pnl_change !== 0) {
    parts.push(`P&L ${formatMoney(since.pnl_change, { sign: true })} Since ${label}`);
  }
  return parts;
}

/** Item 39: "Stopped at 9:41 PM · 3 Orders Cancelled" (null when the API sent no stopped_at). */
export function stopToastTitle(r) {
  if (!r || !r.stopped_at) return null;
  const t = formatTimeParts(r.stopped_at).text;
  const n = isNum(r.orders_cancelled) ? r.orders_cancelled : isNum(r.cancelled) ? r.cancelled : null;
  return n == null ? `Stopped at ${t}` : `Stopped at ${t} · ${titleCase(plural(n, 'order cancelled', 'orders cancelled'))}`;
}

const SETTING_LABELS = { run_budget: 'Run Budget', poll_minutes: 'Poll Minutes', max_open_frac: 'Max Open Fraction' };

/**
 * Item 39: "Settings Saved · Run Budget $40 → $50" from the PUT /api/config `changes: [{key, from, to}]`.
 * Exported for the Settings page (FE-Settings): `notify({kind: 'ok', title: settingsToastTitle(r)})`.
 * Null when there are no changes to describe.
 */
export function settingsToastTitle(r) {
  const ch = r && Array.isArray(r.changes) ? r.changes : [];
  if (!ch.length) return null;
  const fmt = (k, v) => (k === 'run_budget' && isNum(v) ? formatMoney(v, { digits: Number.isInteger(v) ? 0 : 2 }) : String(v));
  const first = ch
    .slice(0, 2)
    .map((c) => `${SETTING_LABELS[c.key] || titleCase(String(c.key).replace(/_/g, ' '))} ${fmt(c.key, c.from)} → ${fmt(c.key, c.to)}`);
  const more = ch.length > 2 ? ` · +${ch.length - 2} More` : '';
  return `Settings Saved · ${first.join(' · ')}${more}`;
}

/**
 * The headline total. 7 and 30 days come from the cheap /api/status `pnl_total` (Maker + Arbitrage, the same
 * settlement-date windows the old Home used), so they show at once and never wait for the ledger. 'This period' (or a
 * status without the figure) falls back to the ledger total. source: 'status' | 'ledger' | null.
 */
export function headlineTotal(status, range, ledgerPnl) {
  const quick = status && status.pnl_total;
  const q = quick ? (range === '7d' ? quick.d7 : range === '30d' ? quick.d30 : null) : null;
  if (isNum(q)) return { pnl: q, source: 'status' };
  return isNum(ledgerPnl) ? { pnl: ledgerPnl, source: 'ledger' } : { pnl: null, source: null };
}

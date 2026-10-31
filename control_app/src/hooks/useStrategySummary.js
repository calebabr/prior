import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, query, request, viewerTimeZone, withRetry } from '../services/api.js';
import { registerRefresh } from '../services/refreshBus.js';
import { DEFAULT_RANGE, rangeParams } from '../utils/range.js';
import { STRATEGY_PAGES, strategyPath } from '../utils/routes.js';
import { strategyShortName } from '../utils/format.js';

/**
 * Client-side composition of the Strategies hub and the Home totals from existing endpoints (no backend change):
 *   GET /api/strategies            names, colours      (fallback: Maker and Arbitrage, built in)
 *   GET /api/status                on/off, mode, last run (skipped when the caller passes `status`)
 *   GET /api/analytics/ledger      P&L, return, open cost, fills per strategy (range: days= or period=)
 *   GET /api/account/positions     exposure per strategy (live)
 *   GET /api/markets               Maker series enabled / paused
 * Each source degrades on its own: a failed source leaves its fields null (render "—" with title "Not available
 * yet") and is named in `errors`; the rest still render. `GET /api/analytics/summary` is not called: it has no
 * strategy filter, so per-strategy drawdown stays null (`capabilities.strategyDrawdown` is false).
 *
 * Sources fill in as they arrive (the slow ledger no longer holds the others back): `loading` is true until all
 * have settled, `ledgerLoading` only while the ledger (including its automatic retries) is pending.
 *
 *   const { strategies, total, manual, errors, partial, loading, ledgerLoading, reload } =
 *     useStrategySummary({ range: '30d', status });   // pass useStatus().status to avoid a second status poll
 *
 * strategies[]: { id, name, shortName, color, to, state: 'running'|'stopped'|'error'|'unknown', mode, stateText,
 *   enabled, lastRunAt, nextRunAt, lastAction: {ts, text}|null, pnl, roi, openCost, fills, settledFills,
 *   exposure, resting, drawdown (always null), series: {enabled, total, paused}|null (Maker only) }
 * total: Maker + Arbitrage only: { pnl, roi, openCost, fills, exposure } (null fields when no source has them)
 * manual: the owner's own trades, shown on a separate "not in the total" line: { pnl, fills, openCost, exposure }|null
 */

export const SUMMARY_CAPABILITIES = {
  allRange: false, // ledger period=all is "no period filter", not all time
  strategyDrawdown: false, // /api/analytics/summary has no strategy filter
};

const FALLBACK_REGISTRY = [
  { id: 'maker', name: 'Bot · Maker', color: 'var(--accent)', source: 'bot' },
  { id: 'arb', name: 'Bot · Arb', color: 'var(--chart2)', source: 'bot' },
];

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const sum = (vals) => {
  const n = vals.filter(isNum);
  return n.length ? Math.round(n.reduce((a, b) => a + b, 0) * 100) / 100 : null;
};

// The ledger is the slow call (a cold server build). 429 analytics_busy and 503 store_unavailable are retried twice
// by themselves (Retry-After honored, jitter); the section stays in its loading state meanwhile (see withRetry).
function fetchLedger(range, env) {
  return withRetry(() => request('GET', `/api/analytics/ledger${query({ ...rangeParams(range), env, tz: viewerTimeZone() })}`));
}

/** Maker / Arbitrage run state from GET /api/status (null status -> unknown). */
export function strategyState(id, status) {
  if (!status) return { state: 'unknown', mode: null, lastRunAt: null, nextRunAt: null, enabled: null };
  if (id === 'arb') {
    const a = status.arb || {};
    const s = a.state;
    if (!s) return { state: 'unknown', mode: null, lastRunAt: null, nextRunAt: null, enabled: null };
    const stopped = s === 'stopped';
    return {
      state: stopped ? 'stopped' : 'running',
      mode: stopped ? null : s === 'live' ? 'live' : 'demo',
      lastRunAt: a.last_scan_utc || null,
      nextRunAt: null,
      enabled: !stopped,
    };
  }
  const running = status.desired === 'running' && !status.kill_switch;
  return {
    state: status.last_error ? 'error' : running ? 'running' : 'stopped',
    mode: running ? status.mode || null : null,
    lastRunAt: status.last_run_at || null,
    nextRunAt: running ? status.next_run_at || null : null,
    enabled: running,
  };
}

function stateText(state, mode) {
  if (state === 'running') return mode === 'live' ? 'Live running' : 'Demo running';
  if (state === 'error') return 'Needs attention';
  if (state === 'stopped') return 'Stopped';
  return 'Unknown';
}

export default function useStrategySummary({ range = DEFAULT_RANGE, env, status: statusProp } = {}) {
  const ownStatus = statusProp === undefined;
  const [raw, setRaw] = useState({ registry: null, status: null, ledger: null, ledgerFor: null, positions: null, markets: null });
  const [errors, setErrors] = useState({ strategies: null, status: null, ledger: null, positions: null, markets: null });
  const [loading, setLoading] = useState(true);
  const [ledgerLoading, setLedgerLoading] = useState(true);
  const seq = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const ledgerKey = `${range}|${env ?? ''}`;

  const reload = useCallback(async () => {
    const mine = ++seq.current;
    const live = () => alive.current && mine === seq.current;
    setLoading(true);
    setLedgerLoading(true);
    setErrors((e) => ({ ...e, ledger: null }));
    const jobs = {
      strategies: api.getStrategies(),
      ledger: fetchLedger(range, env),
      positions: api.getAccountPositions(),
      markets: api.getMarkets(),
    };
    if (ownStatus) jobs.status = api.status();
    const tracked = Object.entries(jobs).map(([k, p]) =>
      Promise.resolve(p).then(
        (value) => {
          if (!live()) return;
          const field = k === 'strategies' ? 'registry' : k;
          setRaw((prev) => ({ ...prev, [field]: value, ...(k === 'ledger' ? { ledgerFor: ledgerKey } : {}) }));
          setErrors((e) => ({ ...e, [k]: null }));
          if (k === 'ledger') setLedgerLoading(false);
        },
        (reason) => {
          if (!live()) return;
          // A failed source keeps its last good value (except the ledger: it belongs to the chosen range).
          if (k === 'ledger') {
            setRaw((prev) => ({ ...prev, ledger: null, ledgerFor: null }));
            setLedgerLoading(false);
          }
          setErrors((e) => ({ ...e, [k]: reason || new Error('Request failed') }));
        },
      ),
    );
    await Promise.all(tracked);
    if (live()) setLoading(false);
  }, [range, env, ownStatus, ledgerKey]);

  useEffect(() => {
    reload();
  }, [reload]);
  useEffect(() => registerRefresh(reload), [reload]);

  const status = ownStatus ? raw.status : statusProp;

  return useMemo(() => {
    const registry = (Array.isArray(raw.registry) && raw.registry.length ? raw.registry : FALLBACK_REGISTRY).filter(
      (s) => s && s.id !== 'you' && s.source !== 'you',
    );
    const ledger = raw.ledger && raw.ledgerFor === ledgerKey ? raw.ledger : null; // never another range's figures
    const rows = {};
    for (const r of (ledger && ledger.strategies) || []) rows[r.strategy] = r;
    const positions = Array.isArray(raw.positions) ? raw.positions : null;
    const posSum = (pred, kind) =>
      positions ? sum(positions.filter((p) => (p.kind || 'position') === kind && pred(p)).map((p) => p.value)) ?? 0 : null;
    const marketRows = raw.markets && Array.isArray(raw.markets.markets) ? raw.markets.markets : null;

    const strategies = registry.map((s) => {
      const st = strategyState(s.id, status);
      const row = rows[s.id] || null;
      const ledgerOk = !!ledger;
      const shortName = STRATEGY_PAGES[s.id] || strategyShortName(s.name);
      const lastAction = st.lastRunAt ? { ts: st.lastRunAt, text: s.id === 'arb' ? 'Last scan' : 'Last pass' } : null;
      return {
        id: s.id,
        name: s.name,
        shortName,
        color: s.color,
        to: strategyPath(s.id),
        state: st.state,
        mode: st.mode,
        stateText: stateText(st.state, st.mode),
        enabled: st.enabled,
        lastRunAt: st.lastRunAt,
        nextRunAt: st.nextRunAt,
        lastAction,
        pnl: row ? row.realized_pnl : ledgerOk ? 0 : null,
        roi: row && isNum(row.roi_on_settled) ? row.roi_on_settled : null,
        openCost: row ? row.open_cost : ledgerOk ? 0 : null,
        fills: row ? row.fills : ledgerOk ? 0 : null,
        settledFills: row ? row.settled_fills : ledgerOk ? 0 : null,
        exposure: posSum((p) => p.source !== 'you' && p.strategy === s.id, 'position'),
        resting: posSum((p) => p.source !== 'you' && p.strategy === s.id, 'order'),
        drawdown: null,
        series:
          s.id === 'maker' && marketRows
            ? {
                enabled: marketRows.filter((m) => m.enabled).length,
                total: marketRows.length,
                paused: marketRows.filter((m) => m.verdict === 'pause').length,
              }
            : null,
      };
    });

    // Home total = Maker + Arbitrage only. The owner's own trades are a separate line.
    const inTotal = strategies.filter((s) => s.id === 'maker' || s.id === 'arb');
    const settledCost = sum(inTotal.map((s) => (rows[s.id] ? rows[s.id].settled_cost : null)));
    const totalPnl = sum(inTotal.map((s) => s.pnl));
    const total = {
      pnl: totalPnl,
      roi: isNum(totalPnl) && isNum(settledCost) && settledCost > 0 ? Math.round((totalPnl / settledCost) * 10000) / 10000 : null,
      openCost: sum(inTotal.map((s) => s.openCost)),
      fills: sum(inTotal.map((s) => s.fills)),
      exposure: sum(inTotal.map((s) => s.exposure)),
    };
    const you = rows.you;
    const manual =
      ledger || positions
        ? {
            pnl: you ? you.realized_pnl : ledger ? 0 : null,
            fills: you ? you.fills : ledger ? 0 : null,
            openCost: you ? you.open_cost : ledger ? 0 : null,
            exposure: posSum((p) => p.source === 'you', 'position'),
          }
        : null;

    const partial = Object.values(errors).some(Boolean);
    return {
      strategies,
      total,
      manual,
      errors,
      partial,
      loading,
      ledgerLoading,
      reload,
      capabilities: SUMMARY_CAPABILITIES,
      env: ledger ? ledger.env : null,
    };
  }, [raw, status, errors, loading, ledgerLoading, ledgerKey, reload]);
}

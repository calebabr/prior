import { useCallback, useEffect, useRef, useState } from 'react';
import { api, query, viewerTimeZone, withRetry } from '../services/api.js';
import { registerRefresh } from '../services/refreshBus.js';

// Granularity -> the window the app asks for. Day follows the page's Window; Week and Month always ask for their own
// (12 weeks, about 6 months) so they always have bars. The server returns whole periods only.
export const WEEK_DAYS = 84;
export const MONTH_DAYS = 180;
export const GRANULARITIES = [
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
];

export function daysFor(granularity, pageDays) {
  if (granularity === 'week') return WEEK_DAYS;
  if (granularity === 'month') return MONTH_DAYS;
  return pageDays;
}

// Results kept per (granularity, days, tz, env) for a minute, so flipping Day / Week / Month does not refetch
// (the server caches for 60 s too). Module level on purpose; bounded.
const TTL_MS = 60 * 1000;
const MAX_KEYS = 24;
const memo = new Map();
export function clearReturnsMemo() {
  memo.clear();
}
function remember(key, data) {
  memo.delete(key);
  memo.set(key, { data, at: Date.now() });
  while (memo.size > MAX_KEYS) memo.delete(memo.keys().next().value);
}

/**
 * GET /api/analytics/returns for one granularity. Retries 429 analytics_busy / 503 store_unavailable by itself,
 * aborts a superseded request, and on a failure keeps the last good data for the same key (staleAt set).
 *   const { data, error, loading, staleAt, reload } = useReturns({ granularity, days, env });
 */
export function useReturns({ granularity = 'day', days = 30, env, enabled = true } = {}) {
  const tz = viewerTimeZone() || '';
  const key = `${granularity}|${days}|${tz}|${env || ''}`;
  const path = `/api/analytics/returns${query({ granularity, days, env })}`;
  const fresh = () => {
    const m = memo.get(key);
    return m && Date.now() - m.at < TTL_MS ? m.data : null;
  };
  const [state, setState] = useState(() => ({ key, data: fresh(), error: null, loading: enabled && !fresh(), staleAt: null }));
  const ctrl = useRef(null);
  const keyRef = useRef(key);
  keyRef.current = key;

  const load = useCallback(
    async (force) => {
      if (!enabled) return null;
      if (!force) {
        const hit = memo.get(key);
        if (hit && Date.now() - hit.at < TTL_MS) {
          setState({ key, data: hit.data, error: null, loading: false, staleAt: null });
          return hit.data;
        }
      }
      if (ctrl.current) ctrl.current.abort();
      const mine = typeof AbortController !== 'undefined' ? new AbortController() : null;
      ctrl.current = mine;
      setState((s) => ({ key, data: s.key === key ? s.data : null, error: null, loading: true, staleAt: s.key === key ? s.staleAt : null }));
      try {
        const data = await withRetry(() => api.analyticsReturns({ granularity, days, env }, { signal: mine ? mine.signal : undefined }), {
          signal: mine ? mine.signal : undefined,
        });
        if (ctrl.current !== mine) return null;
        remember(key, data);
        setState({ key, data, error: null, loading: false, staleAt: null });
        return data;
      } catch (error) {
        if ((error && error.aborted) || ctrl.current !== mine) return null;
        const old = memo.get(key);
        setState((s) => ({
          key,
          data: old ? old.data : s.key === key ? s.data : null,
          error,
          loading: false,
          staleAt: old ? old.at : null,
        }));
        return null;
      }
    },
    [key, enabled, granularity, days, env],
  );

  useEffect(() => {
    load(false);
  }, [load]);
  useEffect(() => (enabled ? registerRefresh(() => load(true)) : undefined), [enabled, load]);
  useEffect(
    () => () => {
      if (ctrl.current) ctrl.current.abort();
      ctrl.current = null;
    },
    [],
  );

  const reload = useCallback(() => load(true), [load]);
  // Never hand back data that belongs to another key.
  const mine = state.key === key;
  return { data: mine ? state.data : null, error: mine ? state.error : null, loading: mine ? state.loading : enabled, staleAt: mine ? state.staleAt : null, path, reload };
}

/** GET /api/analytics/returns/fills for one period (the sheet). Pages of 50; `more()` appends the next page. */
export function useReturnsFills({ granularity, start, end, days, env, enabled = true }) {
  const [state, setState] = useState({ fills: [], total: 0, period: null, loading: enabled, error: null });
  const seq = useRef(0);
  const fetchPage = useCallback(
    async (offset) => {
      const mine = ++seq.current;
      setState((s) => ({ ...s, loading: true, error: null }));
      try {
        const r = await withRetry(() => api.analyticsReturnsFills({ granularity, start, end, days, env, offset, limit: 50 }));
        if (mine !== seq.current) return;
        setState((s) => ({
          fills: offset ? [...s.fills, ...(r.fills || [])] : r.fills || [],
          total: r.total || 0,
          period: r.period || null,
          loading: false,
          error: null,
        }));
      } catch (error) {
        if (mine !== seq.current) return;
        setState((s) => ({ ...s, loading: false, error }));
      }
    },
    [granularity, start, end, days, env],
  );
  useEffect(() => {
    if (enabled) fetchPage(0);
    return () => {
      seq.current += 1;
    };
  }, [enabled, fetchPage]);
  const more = useCallback(() => fetchPage(state.fills.length), [fetchPage, state.fills.length]);
  const retry = useCallback(() => fetchPage(state.fills.length), [fetchPage, state.fills.length]);
  return { ...state, more, retry, hasMore: state.fills.length < state.total };
}

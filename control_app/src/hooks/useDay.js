import { useCallback, useEffect, useRef, useState } from 'react';
import { api, viewerTimeZone, withRetry } from '../services/api.js';
import { registerRefresh } from '../services/refreshBus.js';

// Results kept per (date, strategy, tz, env) for 30 s (the server reads cached rows too). Module level; bounded.
const TTL_MS = 30 * 1000;
const MAX_KEYS = 16;
const memo = new Map();
export function clearDayMemo() {
  memo.clear();
}
function remember(key, data) {
  memo.delete(key);
  memo.set(key, { data, at: Date.now() });
  while (memo.size > MAX_KEYS) memo.delete(memo.keys().next().value);
}

/**
 * GET /api/analytics/day for one local date. Retries 429 analytics_busy / 503 by itself, aborts a superseded request
 * and, on a failure, keeps the last good data for the same key (`staleAt` set).
 *   const { data, error, loading, staleAt, reload } = useDay({ date, strategy });
 */
export function useDay({ date, strategy, env, enabled = true } = {}) {
  const tz = viewerTimeZone() || '';
  const key = `${date}|${strategy || ''}|${tz}|${env || ''}`;
  const fresh = () => {
    const m = memo.get(key);
    return m && Date.now() - m.at < TTL_MS ? m.data : null;
  };
  const [state, setState] = useState(() => ({ key, data: fresh(), error: null, loading: enabled && !fresh(), staleAt: null }));
  const ctrl = useRef(null);

  const load = useCallback(
    async (force) => {
      if (!enabled || !date) return null;
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
        const signal = mine ? mine.signal : undefined;
        const data = await withRetry(() => api.analyticsDay({ date, strategy, env }, { signal }), { signal });
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
          staleAt: old ? old.at : s.key === key ? s.staleAt : null,
        }));
        return null;
      }
    },
    [key, enabled, date, strategy, env],
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
  const mine = state.key === key;
  return { data: mine ? state.data : null, error: mine ? state.error : null, loading: mine ? state.loading : enabled, staleAt: mine ? state.staleAt : null, reload };
}

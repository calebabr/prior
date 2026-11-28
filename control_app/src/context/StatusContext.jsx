import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../services/api.js';
import { registerRefresh } from '../services/refreshBus.js';
import { readCache } from '../services/cache.js';
import { usePageVisible } from '../hooks/usePageVisible.js';
import { STOPPED_EVENT, clearStopSuccessIfRestarted } from '../services/stopResult.js';
import { isRunning } from '../utils/overview.js';

// App-wide GET /api/status, polled every 30 s while the page is visible.
// Shared by the header badge, the Overview page and the Mode page.
// `unconfirmed`: there is a status but it may no longer be true (the last read failed, it is the saved copy from an
// earlier visit, or nothing newer arrived for STALE_AFTER_MS). Screens then say "Last known" / "Not confirmed".

export const POLL_MS = 30000;
export const STALE_AFTER_MS = 75000;
const StatusContext = createContext(null);

export function StatusProvider({ enabled = true, children }) {
  const visible = usePageVisible();
  const [state, setState] = useState(() => {
    const c = readCache('/api/status');
    return { status: c ? c.data : null, error: null, loading: true, updatedAt: c ? c.ts : null, stale: !!c, saved: !!c, requestedAt: null };
  });
  const inFlight = useRef(false);
  const again = useRef(false);
  const [, setAged] = useState(0);

  // `force` (after a Stop): if a read is already under way, which may predate the Stop, read once more after it.
  const refresh = useCallback(async (opts) => {
    if (inFlight.current) {
      if (opts && opts.force === true) again.current = true;
      return;
    }
    inFlight.current = true;
    try {
      do {
        again.current = false;
        const requestedAt = Date.now();
        try {
          const status = await api.status();
          setState({ status, error: null, loading: false, updatedAt: Date.now(), stale: false, saved: false, requestedAt });
        } catch (error) {
          setState((s) => ({ ...s, error, loading: false, stale: !!s.status }));
        }
      } while (again.current);
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    const onStop = () => refresh({ force: true });
    window.addEventListener(STOPPED_EVENT, onStop);
    return () => window.removeEventListener(STOPPED_EVENT, onStop);
  }, [enabled, refresh]);

  const { status, error, saved, updatedAt, requestedAt } = state;
  const unconfirmed = !!status && (!!error || saved || (updatedAt != null && Date.now() - updatedAt > STALE_AFTER_MS));

  // Re-render when the current status grows too old.
  useEffect(() => {
    if (!status || unconfirmed || updatedAt == null) return undefined;
    const id = setTimeout(() => setAged((n) => n + 1), Math.max(0, updatedAt + STALE_AFTER_MS - Date.now()) + 50);
    return () => clearTimeout(id);
  }, [status, unconfirmed, updatedAt]);

  // A newer status that says "running" ends the "Stopped at" line (Start pressed on another device).
  useEffect(() => {
    if (!saved && requestedAt && isRunning(status)) clearStopSuccessIfRestarted(requestedAt);
  }, [status, saved, requestedAt]);

  useEffect(() => {
    if (!enabled || !visible) return undefined;
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [enabled, visible, refresh]);

  useEffect(() => (enabled ? registerRefresh(refresh) : undefined), [enabled, refresh]);

  const value = useMemo(() => ({ ...state, unconfirmed, refresh }), [state, unconfirmed, refresh]);
  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>;
}

export function useStatus() {
  const ctx = useContext(StatusContext);
  if (!ctx) throw new Error('useStatus must be used inside <StatusProvider>');
  return ctx;
}

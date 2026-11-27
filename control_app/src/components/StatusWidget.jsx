import { useEffect, useState } from 'react';
import Chip from './Chip.jsx';
import StopButton from './StopButton.jsx';
import { StatusRing, pnlClass, useNow } from './StatusCard.jsx';
import { useMediaQuery } from '../hooks/useMediaQuery.js';
import { formatMoney, formatUntil } from '../utils/format.js';
import { isRunning, overallState, ringProgress, runTimes } from '../utils/overview.js';
import { todayFigure, todayKind } from '../utils/pnlScopes.js';
import '../styles/overview.css';

/** Tablets and desktops only. Phones keep the card plus the header Stop (no floating widget). */
export const WIDGET_QUERY = '(min-width: 768px)';

/**
 * True once the element behind `cardRef` has scrolled up out of view (past the sticky header).
 * IntersectionObserver where it exists, a scroll listener otherwise.
 */
export function useCardOffscreen(cardRef, enabled) {
  const [off, setOff] = useState(false);
  useEffect(() => {
    const el = cardRef.current;
    if (!enabled || !el) {
      setOff(false);
      return undefined;
    }
    if (typeof IntersectionObserver === 'function') {
      const io = new IntersectionObserver(
        ([entry]) => setOff(!entry.isIntersecting && entry.boundingClientRect.top < 0),
        { rootMargin: '-60px 0px 0px 0px', threshold: 0 },
      );
      io.observe(el);
      return () => io.disconnect();
    }
    const check = () => setOff(el.getBoundingClientRect().bottom < 60);
    check();
    window.addEventListener('scroll', check, { passive: true });
    return () => window.removeEventListener('scroll', check);
  }, [cardRef, enabled]);
  return off;
}

/**
 * Compact status widget pinned at the right once the status card scrolls away (item 25).
 * Shows dot, state, mode, today's P&L and the SAME Stop button as everywhere else (one tap + the same
 * confirm, no 2FA). Tap the body to expand; "Back to Status" scrolls up. Never rendered on phones.
 */
export default function StatusWidget({ status, cardRef, unconfirmed = false }) {
  const wide = useMediaQuery(WIDGET_QUERY);
  const off = useCardOffscreen(cardRef, wide);
  const [open, setOpen] = useState(false);
  const now = useNow();
  if (!wide || !off || !status) return null;

  const st = overallState(status, now);
  const running = isRunning(status);
  const mode = status.mode === 'live' ? 'live' : 'demo';
  const { value: today, withYou } = todayFigure(status);
  const { next } = runTimes(status);

  function toTop() {
    const el = cardRef.current;
    const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    else window.scrollTo(0, 0);
    setOpen(false);
  }

  return (
    <aside className={`status-widget state-${unconfirmed ? 'unknown' : st.key}`} aria-label="Bot status" data-testid="status-widget">
      <button type="button" className="sw-body" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <StatusRing state={unconfirmed ? 'unknown' : st.key} progress={unconfirmed ? 0 : ringProgress(status, now)} size={30} />
        <span className="sw-text">
          <span className="sw-state">{unconfirmed ? `Last known: ${st.label}` : st.label}</span>
          <Chip tone={mode === 'live' ? 'danger' : 'warn'}>{mode === 'live' ? 'Live' : 'Demo'}</Chip>
        </span>
        <span className={`sw-pnl ${pnlClass(today)}`}>
          <span className="sw-pnl-label">{withYou ? 'Today, incl. you' : 'Today'}</span>
          {todayKind(status) === 'unavailable' ? 'Unavailable' : typeof today === 'number' && todayKind(status) === 'value' ? formatMoney(today, { sign: true }) : 'None yet'}
        </span>
      </button>
      {open && (
        <div className="sw-more">
          {running && !unconfirmed && next && <p className="hint">Next {formatUntil(next)}</p>}
          <button type="button" className="btn" onClick={toTop}>Back to Status</button>
        </div>
      )}
      <StopButton compact />
    </aside>
  );
}

import { forwardRef, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import Chip from './Chip.jsx';
import HelpIcon from './HelpIcon.jsx';
import StopButton from './StopButton.jsx';
import { useStopRecord } from '../services/stopResult.js';
import { STOP_COPY } from '../utils/stopCopy.js';
import { IconLock } from './Icons.jsx';
import { formatAgo, formatUntil } from '../utils/format.js';
import { abnormalities, isRunning, overallState, ringProgress, runTimes, stoppedLine } from '../utils/overview.js';
import '../styles/overview.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Re-render every `ms` so "next in 5 min" and the ring stay fresh between status polls. */
export function useNow(ms = 30000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

export function pnlClass(v) {
  return isNum(v) ? (v < 0 ? 'neg' : v > 0 ? 'pos' : '') : 'muted';
}

/** Status ring: a dot in the middle, the arc fills as the next run approaches. */
export function StatusRing({ state, progress, size = 44 }) {
  const r = 18;
  const c = 2 * Math.PI * r;
  return (
    <svg className={`ov-ring ov-ring-${state}`} width={size} height={size} viewBox="0 0 44 44" aria-hidden="true">
      <circle className="ov-ring-track" cx="22" cy="22" r={r} fill="none" strokeWidth="3" />
      <circle
        className="ov-ring-arc"
        cx="22"
        cy="22"
        r={r}
        fill="none"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={`${(c * progress).toFixed(1)} ${c.toFixed(1)}`}
        transform="rotate(-90 22 22)"
      />
      <circle className="ov-ring-dot" cx="22" cy="22" r="6" />
    </svg>
  );
}

function ActionsMenu({ onRunOnce, disabled }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const esc = (e) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('pointerdown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('pointerdown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open]);
  return (
    <div className="ov-menu" ref={ref}>
      <button
        type="button"
        className="ov-menu-btn"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open && (
        <div className="ov-menu-list" role="menu">
          <button
            type="button"
            role="menuitem"
            disabled={disabled}
            onClick={() => {
              setOpen(false);
              onRunOnce();
            }}
          >
            Run once
          </button>
          <Link role="menuitem" to="/mode" onClick={() => setOpen(false)}>
            Mode and live access
          </Link>
        </div>
      )}
    </div>
  );
}

/**
 * The compact status and control card (stage 2): state ring + label + mode chip, one timing line and a
 * context button (Stop while running, Start otherwise; Stop is ALWAYS present and identical to the header one).
 * Money figures live in the Total P&L section of Home; the heartbeat strip moved to the Maker page. Abnormal facts show in red on their own.
 */
const StatusCard = forwardRef(function StatusCard({ status, busy, onStart, onRunOnce, unconfirmed = false, failed = false }, ref) {
  const now = useNow();
  const stop = useStopRecord();
  const running = isRunning(status);
  const st = overallState(status, now);
  // "Overdue" is judged from the clock, so it says nothing about an old reading.
  const bad = abnormalities(status, now).filter((b) => !(unconfirmed && b.key === 'overdue'));
  const { last, next } = runTimes(status);
  const overdue = bad.some((b) => b.key === 'overdue');
  const mode = status && status.mode === 'live' ? 'live' : 'demo';
  const label = !status && failed ? 'Status unknown' : unconfirmed ? `Last known: ${st.label}` : st.label;
  const shown = unconfirmed ? 'unknown' : st.key;
  // Only what the server reported: shown until a newer status says the bot runs again.
  const stopLine = stop && stop.kind === 'ok' && (!running || unconfirmed || !status) ? stoppedLine(stop) : null;
  const justStopped = stop && stop.kind === 'ok';
  const alreadyStopped = !!status && !unconfirmed && !running && (!stop || justStopped);

  return (
    <section className={`card hero status-card state-${shown}`} ref={ref} data-testid="status-card" data-state={shown}>
      <div className="ov-top">
        <StatusRing state={shown} progress={unconfirmed ? 0 : ringProgress(status, now)} />
        <div className="ov-state">
          <div className="ov-state-label" data-testid="state-label">{label}</div>
          <div className="ov-chips">
            {status && <Chip tone={mode === 'live' ? 'danger' : 'warn'}>{mode === 'live' ? 'Live' : 'Demo'}</Chip>}
            <HelpIcon term="stop" />
          </div>
        </div>
        <ActionsMenu onRunOnce={onRunOnce} disabled={!!busy || !status} />
      </div>

      {bad.length > 0 && (
        <ul className="ov-bad" data-testid="abnormal">
          {bad.map((b) => (
            <li key={b.key} className="error-text">{b.text}</li>
          ))}
        </ul>
      )}

      {stopLine && (
        <p className="ov-stopped" data-testid="stopped-line" role="status">
          {stopLine}
        </p>
      )}

      <p className="ov-timing" data-testid="timing-line">
        <span>Last run {last ? formatAgo(last) : '—'}</span>
        {running && !unconfirmed && next && (
          <>
            {' · '}
            <span className={overdue ? 'error-text' : undefined}>Next {formatUntil(next)}</span>
          </>
        )}
      </p>

      <div className="ov-actions">
        {running || st.key === 'error' ? (
          <>
            <StopButton />
            {!running && (
              <button type="button" className="btn" onClick={onStart} disabled={!!busy || !status}>
                {busy === 'start' ? 'Starting…' : <><IconLock /> Start</>}
              </button>
            )}
          </>
        ) : (
          <>
            <button type="button" className="btn primary ov-start" onClick={onStart} disabled={!!busy || !status}>
              {busy === 'start' ? 'Starting…' : <><IconLock /> Start</>}
            </button>
            <StopButton />
          </>
        )}
      </div>
      {alreadyStopped && (
        <p className="hint" data-testid="already-stopped">
          {STOP_COPY.alreadyStopped(mode)}
        </p>
      )}
      {busy === 'run' && <p className="hint">Running one pass…</p>}
    </section>
  );
});

export default StatusCard;

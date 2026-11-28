import { Link } from 'react-router-dom';
import Chip from './Chip.jsx';
import { IconChevron } from './Icons.jsx';
import { DASH, NA_TITLE, formatAgo, formatMoney, formatReturn } from '../utils/format.js';
import '../styles/nav.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

// State is never colour-only: each state has its own shape next to the word.
function StateIcon({ state }) {
  const p = { width: 10, height: 10, viewBox: '0 0 10 10', fill: 'currentColor', 'aria-hidden': true };
  if (state === 'running') return <svg {...p}><circle cx="5" cy="5" r="4" /></svg>;
  if (state === 'error') return <svg {...p}><path d="M5 .5 9.5 9H.5z" /></svg>;
  if (state === 'stopped') return <svg {...p}><rect x="1" y="1" width="8" height="8" rx="1" /></svg>;
  return <svg {...p}><circle cx="5" cy="5" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>;
}

const TONE = { running: 'ok', error: 'danger', stopped: 'muted', unknown: 'muted' };

/** A figure that is missing renders the placeholder with the "Not available yet" tooltip, never 0 or blank. */
function Fig({ value, fmt, signed = false }) {
  if (!isNum(value)) {
    return (
      <span className="num" title={NA_TITLE}>
        {DASH}
      </span>
    );
  }
  const cls = signed ? (value > 0 ? 'pos' : value < 0 ? 'neg' : '') : '';
  return <span className={`num ${cls}`.trim()}>{fmt(value)}</span>;
}

/**
 * One row of the Strategies hub (and the Home strategy list): a strategy's colour dot, short name, run state,
 * P&L and return for the chosen range, open cost, and its last action. The whole card is one link to the
 * strategy page. `strategy` is an item of useStrategySummary().strategies.
 * Only P&L is coloured green / red; open cost and exposure are always plain text. The strategy colour is the
 * 8 px dot only, never text.
 *   <StrategyCard strategy={s} rangeText="30 days" />
 */
export default function StrategyCard({ strategy: s, rangeText, unconfirmed = false }) {
  const old = unconfirmed && s.state !== 'unknown'; // the run state may no longer be true
  const running = s.mode === 'log-only' ? 'accent' : s.mode !== 'live' ? 'warn' : 'ok';
  const tone = old ? 'muted' : s.state === 'running' ? running : TONE[s.state] || 'muted';
  return (
    <Link to={s.to} className="strategy-card" data-testid={`strategy-card-${s.id}`} style={{ '--strategy-color': s.color }}>
      <div className="strategy-card-head">
        <span className="strategy-dot" aria-hidden="true" />
        <span className="strategy-name">{s.shortName}</span>
        <Chip tone={tone} title={s.mode ? `Mode: ${s.mode}` : undefined}>
          <span className="strategy-state">
            <StateIcon state={old ? 'unknown' : s.state} />
            {old ? `Last known: ${s.stateText.toLowerCase()}` : s.stateText}
          </span>
        </Chip>
        <span className="strategy-chevron" aria-hidden="true">
          <IconChevron />
        </span>
      </div>
      <dl className="strategy-figures">
        <div className="strategy-figure">
          <dt>{rangeText ? `Settled P&L, ${rangeText}` : 'Settled P&L'}</dt>
          <dd>
            <Fig value={s.pnl} fmt={(v) => formatMoney(v, { sign: true })} signed />
          </dd>
        </div>
        <div className="strategy-figure">
          <dt>Return</dt>
          <dd>
            <Fig value={s.roi} fmt={formatReturn} signed />
          </dd>
        </div>
        <div className="strategy-figure">
          <dt>Open, paid</dt>
          <dd>
            <Fig value={s.openCost} fmt={(v) => formatMoney(v)} />
          </dd>
        </div>
        <div className="strategy-figure">
          <dt>Fills</dt>
          <dd>
            <Fig value={s.fills} fmt={(v) => String(v)} />
          </dd>
        </div>
      </dl>
      <p className="strategy-last">{s.lastAction ? `${s.lastAction.text} ${formatAgo(s.lastAction.ts)}` : 'No activity yet'}</p>
    </Link>
  );
}

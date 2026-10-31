import { useId } from 'react';
import Modal from '../Modal.jsx';
import { ErrorNotice, Loading } from '../Notices.jsx';
import { useReturnsFills } from '../../hooks/useReturns.js';
import { signCls } from '../../utils/analytics.js';
import { DASH, formatMoney, formatPeriodReturn, formatTime, num } from '../../utils/format.js';
import { periodTitle, reasonText, strategyLabel } from '../../utils/returns.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function FillRow({ f }) {
  const t = formatTime(f.settled_utc, { withDate: true });
  return (
    <li className="rt-fill">
      <span className="rt-fill-t">{f.ticker}</span>
      <span className="rt-fill-m">
        {String(f.side || '').toUpperCase()} · {num(f.count)} · {isNum(f.price) ? `${Math.round(f.price * 100)}¢` : DASH}
      </span>
      <span className={`rt-fill-p num ${signCls(f.pnl)}`}>{formatMoney(f.pnl, { sign: true })}</span>
      <span className="rt-fill-d muted">{t}</span>
    </li>
  );
}

/**
 * The fills behind one bar (GET /api/analytics/returns/fills), 50 at a time, grouped by strategy. Focus is trapped
 * and Escape closes (shared Modal). Manual trades are never listed.
 */
export default function ReturnsSheet({ period, granularity, days, env, onClose }) {
  const titleId = useId();
  const { fills, total, loading, error, more, retry, hasMore } = useReturnsFills({ granularity, start: period.start_local, end: period.end_local, days, env });
  const flows = period.flows_in_period || {};
  const keys = Object.keys(period.by_strategy || {});
  const groups = keys.map((k) => ({ k, rows: fills.filter((f) => f.strategy === k) }));
  const known = new Set(keys);
  const other = fills.filter((f) => !known.has(f.strategy));
  return (
    <Modal onClose={onClose} labelledBy={titleId}>
      <div className="rt-sheet" data-testid="returns-sheet">
        <h2 id={titleId} className="modal-title">
          {periodTitle(period, granularity)}
        </h2>
        <p className="rt-sheet-fig">
          <b className={signCls(period.realized_pnl)}>{formatMoney(period.realized_pnl, { sign: true })}</b>{' '}
          {isNum(period.return_rate) ? (
            <span>
              {formatPeriodReturn(period.return_rate)} on {formatMoney(period.base)} base
            </span>
          ) : (
            <span className="muted" title={reasonText(period)}>
              {DASH}
              <span className="sr-only"> percent not available</span> · {reasonText(period)}
            </span>
          )}
        </p>
        {flows.count > 0 && (
          <p className="hint">
            {flows.count} deposit or withdrawal counted (net {formatMoney(flows.net, { sign: true })}), weighted by the time it was in the account.
          </p>
        )}
        {isNum(period.equity_change) && (
          <p className="hint">Equity change {formatMoney(period.equity_change, { sign: true })}{flows.count > 0 ? ' (includes the money moved)' : ''}.</p>
        )}
        {loading && fills.length === 0 && <Loading text="Loading fills…" />}
        <ErrorNotice error={error} onRetry={retry} />
        {groups.map(({ k, rows }) => (
          <section key={k} className="rt-group" aria-label={strategyLabel(k)}>
            <h3 className="subhead">
              {strategyLabel(k)}{' '}
              <span className={signCls(period.by_strategy[k])}>{formatMoney(period.by_strategy[k], { sign: true })}</span>{' '}
              <span className="muted">
                {num((period.fills_by_strategy || {})[k])} fill{(period.fills_by_strategy || {})[k] === 1 ? '' : 's'}
              </span>
            </h3>
            {rows.length > 0 && (
              <ul className="rt-fills">
                {rows.map((f) => (
                  <FillRow key={f.fill_id} f={f} />
                ))}
              </ul>
            )}
          </section>
        ))}
        {other.length > 0 && (
          <ul className="rt-fills">
            {other.map((f) => (
              <FillRow key={f.fill_id} f={f} />
            ))}
          </ul>
        )}
        {!loading && !error && total === 0 && <p className="muted">No settled fills in this period.</p>}
        <div className="rt-sheet-actions">
          {hasMore && (
            <button type="button" className="btn" onClick={more} disabled={loading}>
              {loading ? 'Loading…' : 'Show more'}
            </button>
          )}
          <span className="hint">Your own trades are not included.</span>
          <button type="button" className="btn primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}

import RangeSelect from '../components/RangeSelect.jsx';
import StrategyCard from '../components/StrategyCard.jsx';
import Skeleton from '../components/Skeleton.jsx';
import { ErrorNotice } from '../components/Notices.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import useRange from '../hooks/useRange.js';
import useStrategySummary from '../hooks/useStrategySummary.js';
import { DASH, NA_TITLE, formatMoney } from '../utils/format.js';
import { rangeLabel } from '../utils/range.js';
import '../styles/overview.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const SOURCE_LABEL = {
  status: 'run state',
  ledger: 'P&L',
  positions: 'open exposure',
  markets: 'series',
};

/**
 * Strategies hub: one read-only card per strategy (run state, P&L for the range, open cost, last action),
 * composed client-side by useStrategySummary. Each source degrades on its own: a failed source shows a dash
 * in its figures and one "Couldn't load" line with Retry; the other figures still render. No switches here.
 */
export default function Strategies() {
  const { status, unconfirmed } = useStatus();
  const [range, setRange] = useRange();
  const summary = useStrategySummary({ range, status });
  const { strategies, total, manual, errors } = summary;
  const failed = Object.keys(errors).filter((k) => errors[k] && SOURCE_LABEL[k]);
  const first = summary.loading && strategies.every((s) => s.pnl == null) && failed.length === 0;

  return (
    <div className="page">
      <div className="page-head hub-head">
        <h1>Strategies</h1>
        <RangeSelect value={range} onChange={setRange} label="Range" />
      </div>
      {failed.length > 0 && (
        <p className="hint error-text" data-testid="hub-degraded">
          Couldn't load {failed.map((k) => SOURCE_LABEL[k]).join(', ')}.{' '}
          <button type="button" className="btn small" onClick={summary.reload}>
            Retry
          </button>
        </p>
      )}
      <ErrorNotice error={errors.strategies && !strategies.length ? errors.strategies : null} onRetry={summary.reload} />
      {first ? (
        <Skeleton rows={2} label="Loading strategies" />
      ) : (
        <div className="hub-cards" data-testid="strategy-cards">
          {strategies.map((s) => (
            <StrategyCard key={s.id} strategy={s} rangeText={rangeLabel(range).toLowerCase()} unconfirmed={unconfirmed} />
          ))}
        </div>
      )}
      <p className="home-scope" data-testid="hub-total">
        Total, maker and arbitrage:{' '}
        <span className="num" title={isNum(total.pnl) ? undefined : NA_TITLE}>
          {isNum(total.pnl) ? formatMoney(total.pnl, { sign: true }) : DASH}
        </span>
        {manual && (
          <>
            {' '}
            · You (manual, not in the total):{' '}
            <span className="num" title={isNum(manual.pnl) ? undefined : NA_TITLE}>
              {isNum(manual.pnl) ? formatMoney(manual.pnl, { sign: true }) : DASH}
            </span>
          </>
        )}
      </p>
    </div>
  );
}

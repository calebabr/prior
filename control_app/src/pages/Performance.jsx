import { Link } from 'react-router-dom';
import RangeSelect from '../components/RangeSelect.jsx';
import Skeleton from '../components/Skeleton.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import { IconChevron } from '../components/Icons.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import useRange from '../hooks/useRange.js';
import useStrategySummary from '../hooks/useStrategySummary.js';
import { DASH, NA_TITLE, formatMoney, formatReturn } from '../utils/format.js';
import { rangeLabel } from '../utils/range.js';
import { PERFORMANCE_ANALYSIS_PATH } from '../utils/routes.js';
import { dayPath, todayString } from '../utils/day.js';
import '../styles/overview.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const tone = (v) => (isNum(v) ? (v < 0 ? 'neg' : v > 0 ? 'pos' : '') : '');

function Cell({ v, fmt, signed = false }) {
  if (!isNum(v)) {
    return (
      <span className="num" title={NA_TITLE}>
        {DASH}
      </span>
    );
  }
  return <span className={`num ${signed ? tone(v) : ''}`.trim()}>{fmt(v)}</span>;
}
const pnl = (v) => formatMoney(v, { sign: true });
const plain = (v) => formatMoney(v);

// Links to the other views. Analytics holds the Ledger and Scorecard tabs.
const LINKS = [
  { to: null, label: 'Today', blurb: 'Fills, settlements and realized P&L for today', today: true },
  { to: '/analytics', label: 'Analytics', blurb: 'Edge, execution, trades, ledger, scorecard' },
  { to: '/shadow', label: 'Shadow Estimate', blurb: 'Simulated results, no orders' },
  { to: '/performance/periods', label: 'Periods', blurb: 'Periods and cash flows' },
];
// The Analysis page does not exist yet: its slot (PERFORMANCE_ANALYSIS_PATH) is reserved and hidden until it ships.
const SHOW_ANALYSIS = false;

/**
 * Performance hub: "All Strategies" compares P&L, return, open cost, exposure and drawdown per strategy for the
 * shared range (Maker + Arbitrage total, manual trades on a separate line), then links to the deeper views.
 */
export default function Performance() {
  const { status } = useStatus();
  const [range, setRange] = useRange();
  const summary = useStrategySummary({ range, status });
  const { strategies, total, manual, errors } = summary;
  const first = summary.loading && strategies.every((s) => s.pnl == null) && !errors.ledger;
  const links = (SHOW_ANALYSIS ? [...LINKS, { to: PERFORMANCE_ANALYSIS_PATH, label: 'Analysis', blurb: '' }] : LINKS).map((l) =>
    l.today ? { ...l, to: dayPath(todayString()) } : l,
  );

  return (
    <div className="page">
      <div className="page-head hub-head">
        <h1>Performance</h1>
        <RangeSelect value={range} onChange={setRange} label="Range" />
      </div>
      <section className="home-section" aria-labelledby="perf-all-h">
        <h2 className="home-label" id="perf-all-h">All Strategies</h2>
        {errors.ledger && (
          <p className="hint error-text" data-testid="perf-degraded">
            Couldn't load P&amp;L.{' '}
            <button type="button" className="btn small" onClick={summary.reload}>
              Retry
            </button>
          </p>
        )}
        {first ? (
          <Skeleton rows={3} label="Loading strategies" />
        ) : (
          <div className="perf-scroll">
            <table className="perf-table" data-testid="perf-table">
              <caption className="sr-only">Strategy comparison, {rangeLabel(range)}</caption>
              <thead>
                <tr>
                  <th scope="col">Strategy</th>
                  <th scope="col">P&amp;L</th>
                  <th scope="col">Return</th>
                  <th scope="col">Open cost</th>
                  <th scope="col">Exposure</th>
                  <th scope="col">Drawdown</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s) => (
                  <tr key={s.id} data-testid={`perf-row-${s.id}`}>
                    <th scope="row">
                      <Link to={s.to}>{s.shortName}</Link>
                    </th>
                    <td><Cell v={s.pnl} fmt={pnl} signed /></td>
                    <td><Cell v={s.roi} fmt={formatReturn} signed /></td>
                    <td><Cell v={s.openCost} fmt={plain} /></td>
                    <td><Cell v={s.exposure} fmt={plain} /></td>
                    <td><Cell v={s.drawdown} fmt={plain} /></td>
                  </tr>
                ))}
                {manual && (
                  <tr className="perf-manual" data-testid="perf-row-you">
                    <th scope="row">You (manual, not in the total)</th>
                    <td><Cell v={manual.pnl} fmt={pnl} signed /></td>
                    <td><Cell v={null} fmt={formatReturn} /></td>
                    <td><Cell v={manual.openCost} fmt={plain} /></td>
                    <td><Cell v={manual.exposure} fmt={plain} /></td>
                    <td><Cell v={null} fmt={plain} /></td>
                  </tr>
                )}
              </tbody>
              <tfoot>
                <tr data-testid="perf-total">
                  <th scope="row">Total (bot)</th>
                  <td><Cell v={total.pnl} fmt={pnl} signed /></td>
                  <td><Cell v={total.roi} fmt={formatReturn} signed /></td>
                  <td><Cell v={total.openCost} fmt={plain} /></td>
                  <td><Cell v={total.exposure} fmt={plain} /></td>
                  <td><Cell v={null} fmt={plain} /></td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
        <p className="home-scope">
          Settled P&amp;L, by settlement date, your time zone.<HelpIcon term="realised-pnl" />
        </p>
      </section>
      <section className="home-section" aria-labelledby="perf-more-h">
        <h2 className="home-label" id="perf-more-h">More Views</h2>
        <ul className="perf-links" data-testid="perf-links">
          {links.map((l) => (
            <li key={l.to}>
              <Link to={l.to}>
                <span>
                  {l.label}
                  <span className="perf-link-blurb">{l.blurb}</span>
                </span>
                <span className="home-row-chev" aria-hidden="true"><IconChevron /></span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

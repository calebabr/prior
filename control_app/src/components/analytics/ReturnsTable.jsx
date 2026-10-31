import { DASH, formatMoney, formatPeriodReturn, num } from '../../utils/format.js';
import { signCls } from '../../utils/analytics.js';
import { isSmall, periodTitle, reasonText, strategyLabel } from '../../utils/returns.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** The percent cell: the figure, or a dash with the reason as its tooltip and a spoken "not available". */
export function ReturnCell({ period }) {
  if (isNum(period.return_rate)) return <span className={signCls(period.return_rate)}>{formatPeriodReturn(period.return_rate)}</span>;
  return (
    <span className="muted" title={reasonText(period)}>
      {DASH}
      <span className="sr-only"> not available</span>
    </span>
  );
}

/**
 * Accessible table alternative to the bars (also the screen-reader and print view): one row per period, newest
 * first, one column per strategy the server sent, and a Fills button that opens the same sheet as a bar tap.
 */
export default function ReturnsTable({ periods, granularity, onOpen }) {
  const keys = [];
  for (const p of periods) for (const k of Object.keys(p.by_strategy || {})) if (!keys.includes(k)) keys.push(k);
  const rows = periods.map((p, i) => ({ p, i })).reverse();
  return (
    <div className="rt-tablewrap" data-testid="returns-table">
      <table className="rt-table">
        <caption className="sr-only">Realized P&amp;L and return for each {granularity}, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Period</th>
            {keys.map((k) => (
              <th scope="col" key={k} className="num">
                {strategyLabel(k)}
              </th>
            ))}
            <th scope="col" className="num">Total</th>
            <th scope="col" className="num">Fills</th>
            <th scope="col" className="num">Return</th>
            <th scope="col">
              <span className="sr-only">Details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ p, i }) => (
            <tr key={p.start_local} className={isSmall(p) ? 'is-small' : ''}>
              <th scope="row">
                {periodTitle(p, granularity)}
                {isSmall(p) && <span className="rt-flag"> n&lt;5</span>}
              </th>
              {keys.map((k) => {
                const v = (p.by_strategy || {})[k];
                return (
                  <td key={k} className={`num ${signCls(v)}`}>
                    {formatMoney(v, { sign: true })}
                  </td>
                );
              })}
              <td className={`num ${signCls(p.realized_pnl)}`}>{formatMoney(p.realized_pnl, { sign: true })}</td>
              <td className="num">{num(p.settled_fills)}</td>
              <td className="num">
                <ReturnCell period={p} />
              </td>
              <td>
                <button type="button" className="btn small" onClick={() => onOpen(i)} aria-label={`Fills for ${periodTitle(p, granularity)}`}>
                  Fills
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import Card from '../Card.jsx';
import HelpIcon from '../HelpIcon.jsx';
import { ErrorNotice, Loading } from '../Notices.jsx';
import { useCancellableResource } from '../../hooks/useCancellableResource.js';
import { api, query } from '../../services/api.js';
import { isNum, signCls } from '../../utils/analytics.js';
import { formatMoney, num } from '../../utils/format.js';

export const NOT_AVAILABLE = 'not available';
const STRATEGY_NAMES = { maker: 'Maker', arb: 'Arbitrage', you: 'You (manual)' };

/** A money cell; null/undefined is "not available", never $0.00. */
export function Money({ v, sign = false }) {
  if (!isNum(v)) return <span className="na">{NOT_AVAILABLE}</span>;
  return <span className={sign ? signCls(v) : ''}>{formatMoney(v, { sign })}</span>;
}

export function Reconciliation({ rec }) {
  const r = rec || {};
  const notes = Array.isArray(r.notes) ? r.notes : [];
  const flagged = r.flag === true;
  return (
    <div className={`recon ${flagged ? 'flag' : ''}`} data-testid="reconciliation">
      <h3 className="subhead">Reconciliation</h3>
      {isNum(r.residual) ? (
        <p data-testid="recon-residual">
          Unexplained difference <b className={flagged ? 'neg' : ''}>{formatMoney(r.residual, { sign: true })}</b> (tolerance{' '}
          {formatMoney(isNum(r.tolerance) ? r.tolerance : 0.1)}).{' '}
          {flagged ? (
            <strong className="neg" data-testid="recon-flag">
              Over tolerance: do not trust the totals until this is explained.
            </strong>
          ) : (
            <span data-testid="recon-ok">Within tolerance.</span>
          )}
        </p>
      ) : (
        <p data-testid="recon-residual">
          Unexplained difference <span className="na">{NOT_AVAILABLE}</span>
        </p>
      )}
      <dl className="stats">
        <div>
          <dt>Balance change</dt>
          <dd><Money v={r.equity_change} sign /></dd>
        </div>
        <div>
          <dt>Net deposits</dt>
          <dd><Money v={r.net_flows} sign /></dd>
        </div>
        <div>
          <dt>Realized P&amp;L</dt>
          <dd><Money v={r.realized_pnl} sign /></dd>
        </div>
        <div>
          <dt>Expected change</dt>
          <dd><Money v={r.expected_equity_change} sign /></dd>
        </div>
      </dl>
      {r.start_exact === false && <p className="hint">The window start used the first stored balance snapshot, not an exact one.</p>}
      {notes.map((n, i) => (
        <p className="hint" key={i}>{n}</p>
      ))}
    </div>
  );
}

/** GET /api/analytics/ledger: P&L by strategy plus the reconciliation against the account balance. */
export default function LedgerView({ days, env }) {
  const path = `/api/analytics/ledger${query({ days, env })}`;
  const { data, error, loading, reload } = useCancellableResource((signal) => api.analyticsLedger({ days, env }, { signal }), path);
  const rows = data && Array.isArray(data.strategies) ? data.strategies : [];
  const settledList = data && Array.isArray(data.settled_by_strategy) ? data.settled_by_strategy : null;
  const settledFor = (id) => {
    if (!settledList) return null;
    const r = settledList.find((x) => x.strategy === id);
    return r && isNum(r.realized_pnl) ? r.realized_pnl : 0;
  };
  const notes = data && Array.isArray(data.notes) ? data.notes : [];
  return (
    <Card
      title={<>Honest Ledger<HelpIcon term="ledger" /></>}
      subtitle="Counts are by fill day; the last P&L column is by settlement day. Anything the server cannot know says not available."
    >
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading text="Loading ledger…" />}
      {data && (
        <>
          <div className="tbl-wrap" data-testid="ledger-table">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Fills</th>
                  <th>Cost</th>
                  <th>Fees</th>
                  <th>Settled P&amp;L (by fill day)</th>
                  <th>Settled P&amp;L (by settlement day)</th>
                  <th>Open cost</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.strategy}>
                    <td>{STRATEGY_NAMES[r.strategy] || r.strategy}</td>
                    <td>{num(r.fills)}</td>
                    <td><Money v={r.cost} /></td>
                    <td><Money v={r.fees} /></td>
                    <td><Money v={r.realized_pnl} sign /></td>
                    <td data-testid={`ledger-settled-${r.strategy}`}><Money v={settledFor(r.strategy)} sign /></td>
                    <td><Money v={r.open_cost} /></td>
                  </tr>
                ))}
                {data.total && (
                  <tr>
                    <td><b>Total</b></td>
                    <td>{num(data.total.fills)}</td>
                    <td><Money v={data.total.cost} /></td>
                    <td><Money v={data.total.fees} /></td>
                    <td><Money v={data.total.realized_pnl} sign /></td>
                    <td data-testid="ledger-settled-total"><Money v={settledFor('total')} sign /></td>
                    <td><Money v={data.total.open_cost} /></td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="hint" data-testid="ledger-attribution">
            Fills, cost, fees and the first P&amp;L column count each fill on the day it was filled. The last column counts
            P&amp;L on the day the market settled, which is the rule Home uses. A fill made before this window can settle inside
            it, so the two P&amp;L columns can differ.
          </p>
          {data.tz_used && <p className="hint">Days in {String(data.tz_used).replace(/_/g, ' ')}.</p>}
          <Reconciliation rec={data.reconciliation} />
          {notes.map((n, i) => (
            <p className="hint" key={i}>{n}</p>
          ))}
        </>
      )}
    </Card>
  );
}

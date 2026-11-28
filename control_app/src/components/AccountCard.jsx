import HelpIcon from './HelpIcon.jsx';
import Time from './Time.jsx';
import { useResource } from '../hooks/useResource.js';
import usePollWhileVisible from '../hooks/usePollWhileVisible.js';
import { api } from '../services/api.js';
import { DASH, NA_TITLE, formatMoney, parseDate } from '../utils/format.js';

// The server caches the Kalshi reads for 30 s, so asking once a minute is enough.
export const ACCOUNT_POLL_MS = 60000;

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function Row({ id, label, help = null, value, signed = false, note, total = false }) {
  const tone = signed && isNum(value) ? (value < 0 ? 'neg' : value > 0 ? 'pos' : '') : '';
  return (
    <div className={`acct-row ${total ? 'acct-total' : ''}`.trim()} data-testid={`acct-${id}`}>
      <dt>
        {label}
        {help}
        {note && <span className="acct-note">{note}</span>}
      </dt>
      <dd className={`num ${tone}`.trim()} title={isNum(value) ? undefined : NA_TITLE}>
        {isNum(value) ? formatMoney(value, signed ? { sign: true } : undefined) : DASH}
      </dd>
    </div>
  );
}

/**
 * Home account card. Live figures come from GET /api/account/summary (the same total Kalshi shows: cash plus open
 * positions at market value). When that read fails, or the server is older and has no such route, the card falls
 * back to cash and the amount paid from the last account snapshot in /api/status. A null figure is a dash; nothing
 * is estimated here.
 */
export default function AccountCard({ account }) {
  const { data, error, loading, reload } = useResource(() => api.getAccountSummary(), null);
  usePollWhileVisible(reload, ACCOUNT_POLL_MS);
  // After a failed read the older live figures are dropped, so a stale total never looks current.
  const live = !error && data && typeof data === 'object' ? data : null;
  const missing = live && [live.total_value, live.cash, live.positions_value, live.positions_cost, live.unrealized].some((v) => !isNum(v));
  const envName = live && (live.env === 'live' ? 'live' : live.env === 'demo' ? 'demo' : null);
  return (
    <section className="home-section" aria-labelledby="home-account-h" data-testid="home-account">
      <h2 className="home-label" id="home-account-h">Account</h2>
      {live ? (
        <>
          <dl className="acct-rows">
            <Row id="total" label="Total value" value={live.total_value} total />
            <Row id="cash" label="Cash" help={<HelpIcon term="balance" />} value={live.cash} />
            <Row id="value" label="Positions at market value" help={<HelpIcon term="market-value" />} value={live.positions_value} />
            <Row id="paid" label="Paid for positions" help={<HelpIcon term="paid" />} value={live.positions_cost} />
            <Row id="unrealised" label="Unrealized P&L" help={<HelpIcon term="unrealised-pnl" />} value={live.unrealized} signed note="not realized until settlement" />
          </dl>
          <p className="hint acct-source">
            From Kalshi{envName ? ` (${envName})` : ''}
            {parseDate(live.as_of) && (
              <>
                {' '}
                at <Time value={live.as_of} />
              </>
            )}
            .{missing ? ' A dash means not available yet.' : ''}
          </p>
        </>
      ) : account || error ? (
        <>
          <dl className="acct-rows">
            <Row id="cash" label="Cash" help={<HelpIcon term="balance" />} value={account ? account.cash : null} />
            <Row id="paid" label="Paid for positions" help={<HelpIcon term="paid" />} value={account ? account.exposure : null} />
          </dl>
          <p className="hint acct-source">
            {loading && !error
              ? 'Checking Kalshi for the live total…'
              : `${account ? 'From the last account snapshot. ' : 'No account snapshot yet. '}Total and market value are not available right now.`}
          </p>
        </>
      ) : (
        <p className="muted">{loading ? 'Checking Kalshi for the live total…' : 'No account snapshot yet.'}</p>
      )}
    </section>
  );
}

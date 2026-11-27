import Card from '../Card.jsx';
import HelpIcon from '../HelpIcon.jsx';
import { ErrorNotice, Loading } from '../Notices.jsx';
import { useCancellableResource } from '../../hooks/useCancellableResource.js';
import { api, query } from '../../services/api.js';
import { isNum, rate } from '../../utils/analytics.js';
import { duration, formatMoney, num } from '../../utils/format.js';
import { useStatus } from '../../context/StatusContext.jsx';

export const NOT_TRACKED = 'not tracked yet';

// What the money behind the return is. Unknown environment: no claim either way.
const RETURN_HINT = { demo: 'Play money and a small sample.', live: 'Real money, but a small sample.', all: 'Demo and live mixed, small sample.' };
export const returnHint = (env) => `${RETURN_HINT[env] || 'A small sample.'} Do not read this as a forecast.`;

function Val({ v, children }) {
  return v === null || v === undefined ? <span className="na">{NOT_TRACKED}</span> : <>{children ?? String(v)}</>;
}

/** GET /api/analytics/scorecard: bot health over the chosen window. Null metrics say "not tracked yet". */
export default function ScorecardView({ days, env }) {
  const path = `/api/analytics/scorecard${query({ days, env })}`;
  const { data, error, loading, reload } = useCancellableResource((signal) => api.analyticsScorecard({ days, env }, { signal }), path);
  const d = data || {};
  const { status } = useStatus();
  const shownEnv = d.env || env || (status && status.mode);
  const notes = Array.isArray(d.notes) ? d.notes : [];
  const passes = d.passes || {};
  const ret = d.return || {};
  const oe = d.order_errors || {};
  const ch = d.churn || {};
  const caps = d.cap_bindings || {};
  const f2s = d.fill_to_settlement || {};
  const past = d.open_past_expected_settlement || {};
  const ssl = d.stop_start_log || {};
  return (
    <Card title={<>Scorecard<HelpIcon term="scorecard" /></>} subtitle={`The last ${days} days.`}>
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading text="Loading scorecard…" />}
      {data && (
        <>
          <dl className="stats" data-testid="scorecard">
            <div>
              <dt>Passes ok</dt>
              <dd>
                <Val v={passes.ok_pct}>
                  {rate(passes.ok_pct)} ({num(passes.ok)} of {num(passes.n)})
                </Val>
              </dd>
            </div>
            <div>
              <dt>Median pass time</dt>
              <dd>
                <Val v={passes.median_duration_s}>{isNum(passes.median_duration_s) ? duration(passes.median_duration_s) : ''}</Val>
              </dd>
            </div>
            <div>
              <dt>Order errors</dt>
              <dd data-testid="sc-order-errors">
                <Val v={oe.value}>{num(oe.value)}</Val>
              </dd>
              <dd className="hint">Error alerts: {num(oe.error_alerts)}</dd>
            </div>
            <div>
              <dt>Cancels per quote</dt>
              <dd>
                <Val v={ch.cancels_per_quote}>{isNum(ch.cancels_per_quote) ? ch.cancels_per_quote.toFixed(2) : ''}</Val>
              </dd>
            </div>
            <div>
              <dt>Maker cap bindings</dt>
              <dd data-testid="sc-cap-maker">
                <Val v={caps.maker}>{String(caps.maker)}</Val>
              </dd>
            </div>
            <div>
              <dt>Fills settled on time</dt>
              <dd>
                <Val v={f2s.rate}>
                  {rate(f2s.rate)} ({num(f2s.settled)} of {num(f2s.due_fills)})
                </Val>
              </dd>
            </div>
            <div>
              <dt>Open past expected settlement</dt>
              <dd>
                <Val v={past.count}>
                  {num(past.count)} ({formatMoney(past.cost)})
                </Val>
              </dd>
            </div>
            <div>
              <dt>Stops / starts</dt>
              <dd>
                <Val v={ssl.stops}>
                  {num(ssl.stops)} / {num(ssl.starts)}
                </Val>
              </dd>
            </div>
          </dl>
          <div className="recon" data-testid="sc-return">
            <h3 className="subhead">Return</h3>
            <p>
              <b data-testid="sc-return-label">{ret.label || 'indicative only'}</b>
              {': '}
              <Val v={ret.roi}>
                {rate(ret.roi)} on {formatMoney(ret.cost)}
              </Val>{' '}
              from {num(ret.sample_settled_fills)} settled fills.
            </p>
            <p className="hint" data-testid="sc-return-hint">{returnHint(shownEnv)}</p>
          </div>
          {notes.map((n, i) => (
            <p className="hint" key={i}>{n}</p>
          ))}
        </>
      )}
    </Card>
  );
}

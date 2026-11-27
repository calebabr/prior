import { Link } from 'react-router-dom';
import { IconLock } from '../../components/Icons.jsx';
import Card from '../../components/Card.jsx';
import HelpIcon from '../../components/HelpIcon.jsx';
import PnlStats from '../../components/PnlStats.jsx';
import StopButton from '../../components/StopButton.jsx';
import CollapsibleList from '../../components/CollapsibleList.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../../components/Notices.jsx';
import { ArbCounters, ArbSetsTable } from '../../components/analytics/ArbSets.jsx';
import { useResource } from '../../hooks/useResource.js';
import { api } from '../../services/api.js';
import { ago, dateTime, money, num } from '../../utils/format.js';
import { strategyPath } from '../../utils/routes.js';
import { StateTrack, executedPill, gateReason, isNum, useArbController, useArbOpportunities } from './arbShared.jsx';

// The three Arbitrage views (Overview | Results | Controls). Extracted from the old single page: same cards,
// same text, same handlers (all in useArbController). Every write keeps its confirm and fresh-2FA path.

function Refresh({ onClick, disabled }) {
  return (
    <div className="strat-toolbar">
      <button type="button" className="btn small" onClick={onClick} disabled={disabled}>
        Refresh
      </button>
    </div>
  );
}

function ArbLoadState({ arb, reload }) {
  return (
    <>
      <StaleNotice staleAt={arb.error ? arb.staleAt : null} />
      <ErrorNotice error={arb.error} onRetry={reload} />
      {arb.loading && !arb.data && <Loading />}
    </>
  );
}

// Turn on / off and Go live / Log only: each one confirms and asks for a fresh 2FA code (live also asks for LIVE).
function ArbModeButtons({ c }) {
  const { enabled, live, state, liveAllowed, busy, setEnabled, setLive } = c;
  return (
    <>
      {/* `live` is the configured mode; say so whenever it is not what the scanner is doing right now. */}
      {live && state !== 'live' && (
        <p className="hint error-text" data-testid="arb-set-live">
          {state === 'stopped'
            ? 'Set to LIVE. Turning arbitrage on will trade real money.'
            : 'Set to LIVE, but it is not placing live orders right now. Check Live Arming.'}
        </p>
      )}
      <div className="btn-row">
        <button type="button" className={`btn ${enabled ? '' : live ? 'danger-outline' : 'primary'}`} onClick={() => setEnabled(!enabled)} disabled={!!busy}>
          {busy === 'enabled' ? 'Working…' : <><IconLock /> {enabled ? 'Turn off' : 'Turn on'}</>}
        </button>
        {live ? (
          <button type="button" className="btn" onClick={() => setLive(false)} disabled={!!busy}>
            {busy === 'live' ? 'Working…' : <><IconLock /> Log only</>}
          </button>
        ) : (
          <button type="button" className="btn danger-outline" onClick={() => setLive(true)} disabled={!!busy || !liveAllowed}>
            {busy === 'live' ? 'Working…' : <><IconLock /> Go live</>}
          </button>
        )}
      </div>
      {!live && !liveAllowed && <p className="hint">Live Is Disabled: The server environment does not allow real money.</p>}
      {live && <p className="hint">A server restart never restores LIVE; you re-arm it here (2FA + typing LIVE).</p>}
    </>
  );
}

function OppRows({ items }) {
  return (
    <ul className="rows">
      {items.map((o, i) => {
        const ex = executedPill(o.executed);
        const when = o.ts || o.utc;
        const demoExec = o.mode === 'demo-execute';
        const why = gateReason(o.executed);
        return (
          <li key={`${when}-${o.event}-${i}`} className={`row-item ${demoExec ? 'row-demo' : ''}`} data-mode={o.mode || undefined}>
            <div className="row-top">
              <span className="mono">{o.event || '?'}</span>
              <span className="muted small">{dateTime(when)}</span>
            </div>
            <div className="row-meta">
              <span>{o.kind === 'YES' ? 'Buy every YES' : o.kind === 'NO' ? 'Buy every NO' : String(o.kind || '')}</span>
              <span>× {num(Number(o.contracts), 0)}</span>
              <span className={Number(o.profit) > 0 ? 'pos' : ''}>{money(Number(o.profit), { sign: true })}</span>
              <span className="muted">cost {money(Number(o.cost))}</span>
              {o.seen_count > 1 && <span className="muted">seen {o.seen_count}×</span>}
              {demoExec && <span className="pill pill-demo" data-testid="demo-exec-badge">Demo order</span>}
              <span className={`pill ${ex.cls}`}>{ex.text}</span>
            </div>
            {why && <div className="muted small" data-testid="gate-reason">Not placed: {why}</div>}
          </li>
        );
      })}
    </ul>
  );
}

export function ArbOverview() {
  const c = useArbController();
  const opps = useArbOpportunities();
  const { arb, data, st, state, legging, liveAllowed, settings, sinceRestart, restartUtc, arbSummary, reload } = c;
  const items = (opps.data && opps.data.items) || [];
  const top = items.slice(0, 3);
  return (
    <div className="strat-view">
      <Refresh
        onClick={() => {
          reload();
          opps.reload();
        }}
        disabled={arb.loading}
      />
      <ArbLoadState arb={arb} reload={reload} />
      {data && (
        <>
          <Card className="hero arb-hero">
            <StateTrack state={state} />
            <p className="muted arb-explain">
              {state === 'live'
                ? 'Scanning and trading real money: Every leg of a set is sent as a taker order.'
                : state === 'log-only'
                  ? 'Scanning and recording opportunities. No orders are placed.'
                  : 'Not scanning.'}
            </p>
            {st.kill_switch && (
              <div className="notice notice-error" role="alert">
                <strong>Kill switch set</strong>
                <HelpIcon term={legging ? 'legging' : 'kill-switch'} />
                {legging ? ': a partial fill left an unmatched leg. Check Kalshi positions.' : st.kill_reason === 'stop' ? ' by Stop.' : '.'}
              </div>
            )}
            <StopButton />
            <p className="hint">
              Stop turns arbitrage off too, no code needed.<HelpIcon term="stop" />
            </p>
            <ArbModeButtons c={c} />
          </Card>

          <Card title="Scanner">
            <dl className="stats">
              <div>
                <dt>Last scan</dt>
                <dd>{ago(st.last_scan_utc)}</dd>
              </div>
              <div>
                <dt>Interval</dt>
                <dd>{settings && settings.arb_poll_seconds != null ? `${settings.arb_poll_seconds}s` : '—'}</dd>
              </div>
              <div>
                <dt>Scans since restart<HelpIcon term="scans-since-restart" /></dt>
                <dd data-testid="scans-since-restart">{num(sinceRestart.scans != null ? sinceRestart.scans : st.scans)}</dd>
                {restartUtc && <dd className="hint" data-testid="restart-time">since {dateTime(restartUtc)}</dd>}
              </div>
              <div>
                <dt>Sightings since restart<HelpIcon term="arb-set" /></dt>
                <dd>{num(sinceRestart.sightings != null ? sinceRestart.sightings : st.found)}</dd>
              </div>
              <div>
                <dt>Live allowed<HelpIcon term="demo-vs-live" /></dt>
                <dd>{liveAllowed ? 'yes' : 'no'}</dd>
              </div>
              <div>
                <dt>Demo orders</dt>
                <dd data-testid="demo-exec-state">{st.demo_execute ? 'on' : 'off'}</dd>
              </div>
              <div>
                <dt>Kill switch<HelpIcon term="kill-switch" /></dt>
                <dd className={st.kill_switch ? 'neg' : ''}>{st.kill_switch ? 'SET' : 'off'}</dd>
              </div>
            </dl>
            {st.last_error && (
              <div className="notice notice-error">
                <strong>Last error:</strong> {String(st.last_error)}
              </div>
            )}
          </Card>
        </>
      )}

      {arbSummary && (
        <Card title={<>Sets found<HelpIcon term="arb-set" /></>} subtitle="Counted once each from the stored log, not per scan.">
          <ArbCounters summary={arbSummary} />
        </Card>
      )}

      <Card
        title={<>Top opportunities<HelpIcon term="fee" /></>}
        actions={<Link className="btn small" to={strategyPath('arb', 'results')}>See all in Results</Link>}
      >
        <StaleNotice staleAt={opps.error ? opps.staleAt : null} />
        <ErrorNotice error={opps.error} onRetry={opps.reload} />
        {opps.loading && !opps.data && <Loading />}
        {opps.data && !items.length && <p className="muted">None found yet.</p>}
        {top.length > 0 && <OppRows items={top} />}
      </Card>
    </div>
  );
}

export function ArbResults() {
  const c = useArbController();
  const opps = useArbOpportunities();
  // Grouped sets come from the analytics summary (one row per set). A failure here only hides that card.
  const sets = useResource(() => api.analyticsSummary({ days: 30, env: 'all' }), '/api/analytics/summary?arb-sets');
  const { arb, data, appStatus, expectation, reload } = c;
  const items = (opps.data && opps.data.items) || [];
  const loggedProfit = items.reduce((a, o) => a + (Number(o.profit) || 0), 0);
  return (
    <div className="strat-view">
      <Refresh
        onClick={() => {
          reload();
          opps.reload();
        }}
        disabled={arb.loading}
      />
      <ArbLoadState arb={arb} reload={reload} />
      {data && (
        <Card title={<>Profit &amp; loss<HelpIcon term="realised-pnl" /></>} subtitle="Executed arbitrage sets only.">
          <h3 className="subhead">Guaranteed Profit</h3>
          <PnlStats pnl={data.pnl} big={false} emptyText="No executed sets yet." scope="arbGuaranteed" />
          <h3 className="subhead">Realized Profit</h3>
          <div data-testid="arb-realized">
            <PnlStats pnl={appStatus && appStatus.pnl_arb} big={false} emptyText="No arbitrage fills have settled yet." scope="arbRealized" />
            <p className="hint">
              Guaranteed is what the scanner promised for sets that filled, counted when they were executed. Realized is
              what settled fills actually paid, counted when the market settled, so the two cover different sets and
              dates until every executed set has settled.
              {appStatus && appStatus.mode !== 'live' ? ' The app is in demo mode, so this is play money.' : ''}
            </p>
          </div>
          {data.pnl_demo_execute && (
            <div data-testid="demo-pnl">
              <h3 className="subhead">Demo Orders (Play Money)</h3>
              <PnlStats pnl={data.pnl_demo_execute} big={false} emptyText="No demo sets yet." />
              {isNum(data.pnl_demo_execute.sets) && <p className="hint">{data.pnl_demo_execute.sets} demo sets. Never part of the live figure above.</p>}
            </div>
          )}
          <p className="hint">{expectation}</p>
        </Card>
      )}

      {sets.data && Array.isArray(sets.data.arb_sets) && (
        <Card title="Executed sets" subtitle="One row per set. Tap a row for its legs.">
          <ArbSetsTable sets={sets.data.arb_sets} total={sets.data.arb_sets_total} />
        </Card>
      )}

      <Card
        title={<>All opportunities<HelpIcon term="fee" /></>}
        subtitle={items.length ? `${items.length} shown, ${money(loggedProfit, { sign: true })} total after fees` : undefined}
      >
        <StaleNotice staleAt={opps.error ? opps.staleAt : null} />
        <ErrorNotice error={opps.error} onRetry={opps.reload} />
        {opps.loading && !opps.data && <Loading />}
        {opps.data && !items.length && <p className="muted">None found yet.</p>}
        <CollapsibleList items={items} pageKey="arbitrage-opps" label="opportunities">
          {(vis) => <OppRows items={vis} />}
        </CollapsibleList>
      </Card>
    </div>
  );
}

export function ArbControls() {
  const c = useArbController();
  const { arb, data, settings, fields, renderField, save, saveError, dirty, hasErrors, busy, resetDraft, reload } = c;
  return (
    <div className="strat-view">
      <Refresh onClick={reload} disabled={arb.loading} />
      <ArbLoadState arb={arb} reload={reload} />
      {data && (
        <Card title="Mode">
          <p className="hint">
            Stop turns arbitrage off too, no code needed.<HelpIcon term="stop" />
          </p>
          <ArbModeButtons c={c} />
        </Card>
      )}
      {settings && fields.length > 0 && (
        <form onSubmit={save}>
          <Card title="Arbitrage settings" subtitle="Applies from the next scan.">
            <div className="stack">{fields.map(renderField)}</div>
          </Card>
          {saveError && (
            <div className="notice notice-error" role="alert">
              <strong>Not saved.</strong>
              {saveError.details && saveError.details.length ? (
                <ul className="detail-list">
                  {saveError.details.map((d, i) => (
                    <li key={i}>{d}</li>
                  ))}
                </ul>
              ) : (
                <div>{saveError.message}</div>
              )}
            </div>
          )}
          <div className="sticky-actions">
            <button type="button" className="btn" disabled={!dirty || !!busy} onClick={resetDraft}>
              Undo changes
            </button>
            <button type="submit" className="btn primary" disabled={!dirty || hasErrors || !!busy}>
              {busy === 'save' ? 'Saving…' : <><IconLock /> Save</>}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

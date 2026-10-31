import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import FoldCard from '../components/FoldCard.jsx';
import StatusCard from '../components/StatusCard.jsx';
import StatusWidget from '../components/StatusWidget.jsx';
import SinceCard from '../components/SinceCard.jsx';
import RangeSelect from '../components/RangeSelect.jsx';
import Skeleton from '../components/Skeleton.jsx';
import Sparkline from '../components/Sparkline.jsx';
import FillQuality from '../components/FillQuality.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import { IconChevron } from '../components/Icons.jsx';
import { ErrorNotice, Loading } from '../components/Notices.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useResource } from '../hooks/useResource.js';
import useRange from '../hooks/useRange.js';
import useStrategySummary from '../hooks/useStrategySummary.js';
import { api } from '../services/api.js';
import { riskLimit } from '../utils/status.js';
import { edgeSummary, headlineParts, headlineTotal } from '../utils/overview.js';
import { rangeLabel } from '../utils/range.js';
import { TODAY_EMPTY, TODAY_UNAVAILABLE, todayKind } from '../utils/pnlScopes.js';
import { DASH, NA_TITLE, dateTime, formatMoney, money, sentenceCase } from '../utils/format.js';
import { shortDate } from '../utils/analytics.js';
import { moneyDigits } from '../utils/chart.js';
import '../styles/overview.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const tone = (v) => (isNum(v) ? (v < 0 ? 'neg' : v > 0 ? 'pos' : '') : '');

// POST /api/bot/start 409 details (Start/Stop race guard, runner off).
const START_409 = {
  stop_in_progress: 'A Stop is still being saved. Try Start again in a few seconds.',
  stopped_during_start: 'A Stop arrived while starting, so Stop won. The bot is stopped.',
  runner_disabled: 'The runner is disabled on this server (RUNNER_ENABLED=false).',
};

// One line and one bar: "$37.60 at risk of $125". Exposure is plain text, never green or red.
function RiskBar({ exposure, limit }) {
  if (!isNum(exposure) || !isNum(limit) || limit <= 0) return null;
  const frac = Math.min(1, exposure / limit);
  const level = frac > 0.9 ? 'danger' : frac > 0.7 ? 'warn' : 'ok';
  return (
    <>
      <p className={`ov-risk-line ${level === 'danger' ? 'error-text' : ''}`} data-testid="risk-line">
        {formatMoney(exposure)} at risk of {formatMoney(limit, { digits: Number.isInteger(limit) ? 0 : 2 })}
        <HelpIcon term="exposure" />
      </p>
      <div className="meter" role="img" aria-label={`At risk ${Math.round(frac * 100)}% of limit`}>
        <div className={`meter-fill meter-${level}`} style={{ width: `${(frac * 100).toFixed(1)}%` }} />
      </div>
    </>
  );
}

/** A figure, or the dash with the "Not available yet" tooltip. Only P&L (signed) is coloured. */
function Fig({ value, signed = false }) {
  if (!isNum(value)) {
    return (
      <span className="num" title={NA_TITLE}>
        {DASH}
      </span>
    );
  }
  return <span className={`num ${signed ? tone(value) : ''}`.trim()}>{formatMoney(value, signed ? { sign: true } : undefined)}</span>;
}

// One sentence-case line: "Demo running. +$17.12 over 30 days. Edge below expected."
// Text colour; only the figure is coloured, and a problem state is red.
function Headline({ state, pnl, range, edge }) {
  return (
    <p className="ov-headline" data-testid="headline">
      <span className={`ov-hl ov-hl-${state.tone === 'danger' ? 'danger' : 'muted'}`}>{sentenceCase(state.text.toLowerCase())}.</span>
      {isNum(pnl) && (
        <>
          {' '}
          <span className="ov-hl">
            <span className={tone(pnl)}>{formatMoney(pnl, { sign: true })}</span> over {rangeLabel(range).toLowerCase()}.
          </span>
        </>
      )}
      {edge && (
        <>
          {' '}
          <span className="ov-hl">{sentenceCase(edge.text.toLowerCase())}.</span>
        </>
      )}
    </p>
  );
}

function StrategyRow({ s }) {
  return (
    <li>
      <Link to={s.to} className="home-row" data-testid={`home-row-${s.id}`} style={{ '--strategy-color': s.color }}>
        <span className="home-dot" aria-hidden="true" />
        <span className="home-row-name">{s.shortName}</span>
        <span className="home-row-fig">
          <Fig value={s.pnl} signed />
          <span className="home-row-sub">
            Open <Fig value={s.openCost} />
          </span>
        </span>
        <span className="home-row-chev" aria-hidden="true">
          <IconChevron />
        </span>
      </Link>
    </li>
  );
}

export default function Overview() {
  const { status, error, loading, updatedAt, stale, refresh } = useStatus();
  const { confirm, twoFactor, notify } = useDialogs();
  const [range, setRange] = useRange();
  const summary = useStrategySummary({ range, status });
  const history = useResource(() => api.getHistory(30), '/api/history?days=30');
  const edge = useResource(() => api.analyticsSummary({ days: 30 }), '/api/analytics/summary?days=30');
  const [busy, setBusy] = useState(null);
  const cardRef = useRef(null);

  const a = status && status.account;
  const limit = riskLimit(status);
  const snaps = (history.data && history.data.snapshots) || [];
  const equity = snaps.map((s) => (s.cash || 0) + (s.exposure || 0));
  const equityDates = snaps.map((s) => shortDate(s.ts));
  const { strategies, total, manual, errors } = summary;
  const todayTotal = status && status.pnl_today && status.pnl_today.total;
  const head = headlineTotal(status, range, total.pnl);
  const totalLoading = summary.ledgerLoading && !isNum(head.pnl); // only a ledger-fed headline ever waits
  const ledgerError = errors.ledger;
  const ledgerRetry = (
    <p className="hint error-text" data-testid="home-ledger-error">
      Couldn't load P&amp;L{head.source === 'status' ? ' by strategy' : ''}.{' '}
      <button type="button" className="btn small" onClick={summary.reload}>
        Retry
      </button>
    </p>
  );

  async function onStart() {
    const mode = (status && status.mode) || 'demo';
    setBusy('start');
    try {
      const r = await twoFactor({
        title: 'Start trading?',
        message: (
          <>
            <p>
              Removes the kill switch and starts the bot in <strong>{mode.toUpperCase()}</strong> mode.
            </p>
            {mode === 'live' && <p className="error-text">LIVE mode trades real money.</p>}
          </>
        ),
        confirmLabel: 'Start',
        action: (code) => api.start(code),
      });
      if (r) notify({ kind: 'ok', title: 'Started', text: `Bot is ${r.desired || 'running'}.` });
    } catch (err) {
      const why = START_409[err.detail];
      notify({ kind: 'error', title: 'Start failed', text: why || err.message, details: why ? undefined : err.details });
    } finally {
      setBusy(null);
      refresh();
    }
  }

  async function onRunOnce() {
    const ok = await confirm({
      title: 'Run one pass now?',
      message: 'Re-quotes all enabled markets once, using the current mode and settings.',
      confirmLabel: 'Run once',
    });
    if (!ok) return;
    setBusy('run');
    try {
      const r = await api.runOnce();
      notify({
        kind: r && r.ran ? 'ok' : 'info',
        title: r && r.ran ? 'Pass ran' : 'Pass did not run',
        text: r && r.reason ? `Reason: ${r.reason}` : undefined,
      });
    } catch (err) {
      notify({ kind: 'error', title: 'Run once failed', text: err.message, details: err.details });
    } finally {
      setBusy(null);
      refresh();
    }
  }

  return (
    <div className="page overview-page">
      {/* Visual title is the sticky PageTitle bar (aria-hidden); this is the page's one accessible heading. */}
      <h1>Home</h1>
      <SinceCard />
      {status && <Headline state={headlineParts(status)[0]} pnl={head.pnl} range={range} edge={edgeSummary(edge.data)} />}
      <StatusCard ref={cardRef} status={status} busy={busy} onStart={onStart} onRunOnce={onRunOnce} />
      <StatusWidget status={status} cardRef={cardRef} />
      {loading && !status && <Loading text="Loading status…" />}
      <ErrorNotice error={error} onRetry={refresh} />
      {stale && updatedAt && <p className="hint">Last update {dateTime(updatedAt)} (could not refresh).</p>}

      {status && (
        <div className="home-grid">
          <div className="home-main">
            <section className="home-total" aria-labelledby="home-total-h" data-testid="home-total">
              <h2 className="home-label" id="home-total-h">
                Total P&amp;L<HelpIcon term="realised-pnl" />
              </h2>
              {totalLoading ? (
                <Skeleton rows={0} figure label="Loading total P&L" />
              ) : (
                <p className={`home-figure ${tone(head.pnl)}`} data-testid="home-total-figure" title={isNum(head.pnl) ? undefined : NA_TITLE}>
                  {isNum(head.pnl) ? formatMoney(head.pnl, { sign: true }) : DASH}
                </p>
              )}
              <RangeSelect value={range} onChange={setRange} label="P&L range" />
              <p className="home-scope">Settled P&amp;L, by settlement date, your time zone. Maker and arbitrage.</p>
              {todayKind(status) === 'unavailable' ? (
                <p className="home-today muted" data-testid="today-unavailable">Today: {TODAY_UNAVAILABLE}</p>
              ) : todayKind(status) === 'none' || !isNum(todayTotal) ? (
                <p className="home-today muted" data-testid="today-empty">{TODAY_EMPTY}</p>
              ) : (
                <p className="home-today" data-testid="home-today">
                  Today <span className={tone(todayTotal)}>{formatMoney(todayTotal, { sign: true })}</span>
                </p>
              )}
              {ledgerError && head.source !== 'status' && ledgerRetry}
            </section>

            <section className="home-section" aria-labelledby="home-by-h" data-testid="by-strategy">
              <h2 className="home-label" id="home-by-h">By Strategy</h2>
              {summary.ledgerLoading && !ledgerError ? (
                <Skeleton rows={2} label="Loading P&L by strategy" />
              ) : (
                <ul className="home-rows">
                  {strategies.map((s) => (
                    <StrategyRow key={s.id} s={s} />
                  ))}
                </ul>
              )}
              {ledgerError && head.source === 'status' && ledgerRetry}
              {manual && (
                <div className="home-manual" data-testid="manual-row">
                  <div className="home-row">
                    <span className="home-dot" aria-hidden="true" style={{ background: 'transparent', border: '1px solid currentColor' }} />
                    <span className="home-row-name">
                      You <span className="home-row-sub">manual, not in the total</span>
                    </span>
                    <span className="home-row-fig">
                      <Fig value={manual.pnl} signed />
                    </span>
                  </div>
                </div>
              )}
            </section>
          </div>

          <div className="home-side">
            <section className="home-section" aria-labelledby="home-risk-h">
              <h2 className="home-label" id="home-risk-h">Risk</h2>
              {a ? (
                <>
                  <RiskBar exposure={a.exposure} limit={limit} />
                  <p className="home-cash" data-testid="home-cash">
                    Cash<HelpIcon term="balance" /> {formatMoney(a.cash)}
                  </p>
                </>
              ) : (
                <p className="muted">No account snapshot yet.</p>
              )}
              {status.fills_sync && status.fills_sync.error && <p className="hint error-text">Fill sync failed: {String(status.fills_sync.error)}</p>}
            </section>

            {equity.length > 1 && (
              <FoldCard title="Balance, 30 Days" id="balance-fold" summary={<span className="num">{money(equity[equity.length - 1])}</span>}>
                <Sparkline
                  values={equity}
                  labels={equityDates}
                  yFormat={(v) => money(v, { digits: moneyDigits(Math.min(...equity), Math.max(...equity)) })}
                  yTitle="Balance + positions ($)"
                  xTitle="Date"
                  testId="equity-chart"
                  label={`Balance + positions in dollars, last 30 days, ${equityDates[0]} to ${equityDates[equityDates.length - 1]}: ${money(equity[0])} to ${money(equity[equity.length - 1])}; low ${money(Math.min(...equity))}, high ${money(Math.max(...equity))}`}
                />
                <p className="hint">Balance + positions, 30 days: {money(equity[0])} → {money(equity[equity.length - 1])}</p>
              </FoldCard>
            )}
            {'fill_quality' in status && (
              <FoldCard title={<>Fill Quality<HelpIcon term="fill-quality" /></>} id="fill-quality-fold">
                <FillQuality fq={status.fill_quality} />
              </FoldCard>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

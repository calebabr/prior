import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Card from '../components/Card.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import QueuePosition from '../components/QueuePosition.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import WinRateByStrategy from '../components/analytics/WinRateByStrategy.jsx';
import LedgerView from '../components/analytics/LedgerView.jsx';
import ScorecardView from '../components/analytics/ScorecardView.jsx';
import PnlCurve from '../components/analytics/PnlCurve.jsx';
import ReturnsCard from '../components/analytics/ReturnsCard.jsx';
import EdgeBreakdown from '../components/analytics/EdgeBreakdown.jsx';
import FillQualityTrend from '../components/analytics/FillQualityTrend.jsx';
import ArbHistory from '../components/analytics/ArbHistory.jsx';
import TradeHistory from '../components/analytics/TradeHistory.jsx';
import EdgeHolding from '../components/analytics/EdgeHolding.jsx';
import Calibration from '../components/analytics/Calibration.jsx';
import FillRate from '../components/analytics/FillRate.jsx';
import MyTrades, { BotVsYou, periodLabel } from '../components/analytics/MyTrades.jsx';
import { useMyTrades } from '../components/analytics/useMyTrades.js';
import { useStatus } from '../context/StatusContext.jsx';
import { useCancellableResource } from '../hooks/useCancellableResource.js';
import { useResource } from '../hooks/useResource.js';
import Empty from '../components/Empty.jsx';
import Skeleton from '../components/Skeleton.jsx';
import RangeSelect from '../components/RangeSelect.jsx';
import TabScroller from '../components/analytics/TabScroller.jsx';
import PnlScope from '../components/PnlScope.jsx';
import { api, query } from '../services/api.js';
import { ago, formatMoney, formatPct, num } from '../utils/format.js';
import { DAY_OPTIONS, rate, signCls } from '../utils/analytics.js';
import '../styles/analytics.css';

// Read-only analytics (docs/ANALYTICS_CONTRACT.md):
//   GET /api/analytics/summary?days&env -> totals, pnl_curve, by_cell/series/band/ttc, fill_quality, arb  (bot only)
//   GET /api/analytics/trades?days&env&source&...                                                        (bot only by default)
//   GET /api/analytics/my-trades?days&env -> the owner's manual trades beside the bot (Y5)
// Tabs: Summary, Edge, Execution, Trades. Manual trades never enter a bot statistic; "Bot + You" only adds
// the combined line and the manual rows in the trade list.

export const TABS = [
  { key: 'summary', label: 'Summary' },
  { key: 'edge', label: 'Edge' },
  { key: 'execution', label: 'Execution' },
  { key: 'trades', label: 'Trades' },
  { key: 'ledger', label: 'Ledger' },
  { key: 'scorecard', label: 'Scorecard' },
];

// The summary honours a rolling `days` window only (no `period=current`), so the shared RangeSelect gets the
// four day windows. It has no `strategy` filter either, so there is no strategy chip on this page.
const RANGE_CHOICES = DAY_OPTIONS.map((d) => ({ id: String(d), label: `${d} days` }));

const KINDS = [
  { key: 'all', label: 'All' },
  { key: 'maker', label: 'Maker' },
  { key: 'taker', label: 'Taker' },
];

function cap(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

function Totals({ t, pooled = true }) {
  return (
    <dl className="stats an-totals" data-testid="totals">
      <div>
        <dt>
          P&amp;L
          <HelpIcon term="pnl" />
        </dt>
        <dd className={signCls(t.pnl)}>{formatMoney(t.pnl, { sign: true })}</dd>
      </div>
      <div>
        <dt>
          Profit per $1 Spent
          <HelpIcon term="roi" />
        </dt>
        <dd className={signCls(t.roi)}>{formatPct(t.roi, { digits: 1 })}</dd>
      </div>
      {pooled && (
        <div>
          <dt>
            Win Rate
            <HelpIcon term="win-rate" />
          </dt>
          <dd>{rate(t.win_rate)}</dd>
        </div>
      )}
      <div>
        <dt>
          Max Drawdown
          <HelpIcon term="drawdown" />
        </dt>
        <dd className={t.max_drawdown < 0 ? 'neg' : ''}>{formatMoney(t.max_drawdown)}</dd>
      </div>
    </dl>
  );
}

function SummaryTab({ s, totals, empty, curve, hasSplit, my, view, days, env, onUseDays }) {
  const [kind, setKind] = useState('all');
  const combined = view === 'combined' && my.data && totals;
  return (
    <>
      {!empty && <EdgeHolding summary={s} />}
      <Card title="Totals" subtitle="Bot trades only.">
        <Totals t={totals} pooled={!s.win_rate_by_strategy} />
        <WinRateByStrategy wr={s.win_rate_by_strategy} />
        <PnlScope id="analytics" ctx={{ tz: s.tz_used }} />
        <p className="hint">
          {num(totals.settled_fills)} settled fills
          <HelpIcon term="settlement" /> · {num(totals.open_fills)} still open
          <HelpIcon term="position" />
        </p>
        {empty && (
          <Empty
            compact
            what="No settled trades yet."
            why={totals.open_fills ? `${num(totals.open_fills)} fills are still waiting to settle.` : 'Fills settle after their market closes.'}
          />
        )}
        {my.data && <BotVsYou bot={my.data.bot || totals} you={my.data.totals} days={days} />}
        {combined && (
          <p className="bvy" data-testid="combined-line">
            <span>
              Bot + You{' '}
              <b className={signCls(totals.pnl + (my.data.totals.pnl || 0))}>
                {formatMoney(totals.pnl + (my.data.totals.pnl || 0), { sign: true })}
              </b>
            </span>
            <span className="muted">{periodLabel(days)}</span>
          </p>
        )}
      </Card>
      {!empty && (
        <Card
          title={
            <>
              Cumulative P&amp;L
              <HelpIcon term="pnl" />
            </>
          }
        >
          <div className="segmented seg-3 compact" role="group" aria-label="Fills to include">
            {KINDS.map((k) => (
              <button
                key={k.key}
                type="button"
                className={kind === k.key ? 'active' : ''}
                aria-pressed={kind === k.key}
                disabled={k.key !== 'all' && !hasSplit}
                onClick={() => setKind(k.key)}
              >
                {k.label}
              </button>
            ))}
          </div>
          <PnlCurve points={curve} kind={hasSplit ? kind : 'all'} />
          <PnlScope id="analytics" ctx={{ tz: s.tz_used }} />
          <p className="hint">
            Maker = resting bids that got filled; taker = arbitrage legs
            <HelpIcon term="maker-taker" />.
          </p>
        </Card>
      )}
      <ReturnsCard days={days} env={env} onUseDays={onUseDays} />
    </>
  );
}

function EdgeTab({ s }) {
  return (
    <>
      <Card
        title={
          <>
            Edge: Win Rate vs Price Paid
            <HelpIcon term="edge" />
          </>
        }
      >
        <EdgeBreakdown summary={s} />
      </Card>
      {Array.isArray(s.calibration) && (
        <Card
          title={
            <>
              Calibration: Win Rate vs Price
              <HelpIcon term="calibration" />
            </>
          }
          subtitle="Does each price level win more often than it costs?"
        >
          <Calibration buckets={s.calibration} />
        </Card>
      )}
    </>
  );
}

// The queue-position table moved here from Markets (item 8).
function ExecutionTab({ s }) {
  const { data, error, reload } = useResource(() => api.getMarkets(), '/api/markets');
  return (
    <>
      <Card
        title={
          <>
            Queue Position, All Series
            <HelpIcon term="queue-position" />
          </>
        }
        subtitle="Where our bids sat in line when they joined the book."
      >
        <ErrorNotice error={error} onRetry={reload} />
        {data ? <QueuePosition qs={data.queue_stats} /> : !error && <Loading />}
      </Card>
      <Card
        title={
          <>
            Fill Quality and Queue
            <HelpIcon term="fill-quality" />
          </>
        }
        subtitle={
          <>
            By week. Front of line
            <HelpIcon term="front-of-line" /> vs traded through.
            <HelpIcon term="trade-through" />
          </>
        }
      >
        <FillQualityTrend weeks={s.fill_quality} />
      </Card>
      {s.fill_rate && (
        <Card
          title={
            <>
              Fill Rate
              <HelpIcon term="fill-rate" />
            </>
          }
          subtitle="How many resting bids got filled, and how long they waited."
        >
          <FillRate fr={s.fill_rate} />
        </Card>
      )}
      <Card
        title={
          <>
            Arbitrage
            <HelpIcon term="arbitrage" />
          </>
        }
      >
        <ArbHistory arb={s.arb} />
      </Card>
    </>
  );
}

export default function Analytics() {
  const [days, setDays] = useState(30);
  const [allEnv, setAllEnv] = useState(false);
  const [view, setView] = useState('bot'); // 'bot' (default) | 'combined'
  const [params, setParams] = useSearchParams();
  const tab = TABS.some((t) => t.key === params.get('tab')) ? params.get('tab') : 'summary';
  const { status } = useStatus();
  const mode = status && status.mode ? String(status.mode) : null;
  const env = allEnv ? 'all' : undefined;

  const path = `/api/analytics/summary${query({ days, env })}`;
  // S-28: a window/account change or leaving the page aborts the summary in flight.
  const { data, error, loading, staleAt, reload, settledKey } = useCancellableResource(
    (signal) => api.analyticsSummary({ days, env }, { signal }),
    path,
  );
  // Load the trade list and my trades only after this window's summary has finished (data or error),
  // so one page load or window change never runs two heavy builds at once.
  const summaryDone = settledKey === path;
  const my = useMyTrades({ days, env, enabled: summaryDone });

  // Only trust data for the window that is selected (the cache may hold another one).
  const matches = !!data && data.days === days && (allEnv ? data.env === 'all' : data.env !== 'all');
  const s = matches || (data && !loading) ? data : null;
  const totals = (s && s.totals) || null;
  const empty = !!totals && !totals.settled_fills;
  const curve = (s && s.pnl_curve) || [];
  const hasSplit = curve.some((p) => p && ('maker_pnl' in p || 'taker_pnl' in p));

  const seriesOptions = useMemo(() => ((s && s.by_series) || []).map((g) => g.key).filter(Boolean), [s]);
  const cellOptions = useMemo(() => ((s && s.by_cell) || []).map((g) => g.cell || g.key).filter(Boolean), [s]);
  const bandOptions = useMemo(() => ((s && s.by_band) || []).map((g) => g.key).filter(Boolean), [s]);

  function pick(key) {
    setParams(key === 'summary' ? {} : { tab: key }, { replace: true });
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Analytics</h1>
        <button type="button" className="btn small" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      <div className="an-controls">
        <RangeSelect label="Window" value={String(days)} options={RANGE_CHOICES} onChange={(id) => setDays(Number(id))} />
        <div className="segmented compact" role="group" aria-label="Account">
          <button type="button" className={!allEnv ? 'active' : ''} aria-pressed={!allEnv} onClick={() => setAllEnv(false)}>
            {mode ? `${cap(mode)} (Current)` : 'Current Mode'}
          </button>
          <button type="button" className={allEnv ? 'active' : ''} aria-pressed={allEnv} onClick={() => setAllEnv(true)}>
            Demo + Live
          </button>
        </div>
        <div className="segmented compact" role="group" aria-label="Whose trades">
          <button type="button" className={view === 'bot' ? 'active' : ''} aria-pressed={view === 'bot'} onClick={() => setView('bot')}>
            Bot Only
          </button>
          <button type="button" className={view === 'combined' ? 'active' : ''} aria-pressed={view === 'combined'} onClick={() => setView('combined')}>
            Bot + You
          </button>
        </div>
        {s && (
          <p className="scope-chip" data-testid="scope-chip">
            <span>
              {s.env === 'all' ? 'Demo and live accounts' : `${cap(s.env)} account`}, settled in the last {s.days} days
            </span>
            <span className="muted">Updated {ago(s.generated_utc)}</span>
          </p>
        )}
      </div>

      <TabScroller tabs={TABS} value={tab} onChange={pick} label="Analytics sections" />

      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !s && <Skeleton rows={3} figure label="Loading analytics" />}
      {s && s.truncated && (
        <div className="notice notice-info">Large Window: Only the most recent fills were counted. Pick a shorter window for exact totals.</div>
      )}

      {s && tab === 'summary' && totals && (
        <SummaryTab s={s} totals={totals} empty={empty} curve={curve} hasSplit={hasSplit} my={my} view={view} days={days} env={env} onUseDays={setDays} />
      )}
      {s && tab === 'edge' && (empty ? <Empty what="No settled trades yet." why="Edge needs settled fills." /> : <EdgeTab s={s} />)}
      {s && tab === 'execution' && <ExecutionTab s={s} />}
      {tab === 'ledger' && <LedgerView days={days} env={env} />}
      {tab === 'scorecard' && <ScorecardView days={days} env={env} />}
      {tab === 'trades' && (
        <>
          <Card
            title={
              <>
                My Trades
                <HelpIcon term="position" />
              </>
            }
            subtitle="Your own trades, kept apart from the bot."
          >
            <MyTrades days={days} resource={my} />
          </Card>
          <Card
            title={view === 'combined' ? 'Bot and Your Trades' : 'Bot Trades'}
            subtitle="Newest first, by fill time in the selected window."
          >
            <TradeHistory
              days={days}
              env={env}
              mode={mode}
              source={view === 'combined' ? 'all' : undefined}
              enabled={summaryDone}
              seriesOptions={seriesOptions}
              cellOptions={cellOptions}
              bandOptions={bandOptions}
            />
          </Card>
        </>
      )}
    </div>
  );
}

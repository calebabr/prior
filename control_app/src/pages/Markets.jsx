import { useState } from 'react';
import { Link } from 'react-router-dom';
import { IconLock } from '../components/Icons.jsx';
import Card from '../components/Card.jsx';
import Chip from '../components/Chip.jsx';
import FoldCard from '../components/FoldCard.jsx';
import KeyValue from '../components/KeyValue.jsx';
import CiBar, { CiSummary } from '../components/CiBar.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import FillQuality from '../components/FillQuality.jsx';
import RangeSelect from '../components/RangeSelect.jsx';
import StopButton from '../components/StopButton.jsx';
import HeartbeatStrip from '../components/HeartbeatStrip.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { useResource } from '../hooks/useResource.js';
import useStrategySummary from '../hooks/useStrategySummary.js';
import { api } from '../services/api.js';
import { ago, countWord, formatMoney, formatPct, formatReturn, num, titleCase } from '../utils/format.js';
import { TOO_FEW_TEXT, tooFewForRange, winRecords } from '../utils/ci.js';
import { DEFAULT_RANGE, rangeLabel } from '../utils/range.js';
import { badgeKind, riskLimit } from '../utils/status.js';
import { isMakerKey } from '../utils/strategyConfig.js';
import { ConfigEditor } from './Settings.jsx';
import '../styles/analytics.css';

// The Maker strategy's three views live under /strategies/maker (pages/strategy/*): Overview (MakerOverview),
// Results (MarketsResults, read-only) and Controls (MarketsControls: Pause / Resume + the Maker settings).
// The old /markets route redirects to Controls.

// queue_stats is not shown here any more (item 8): the queue-position table lives in Analytics > Execution.
const KNOWN = ['series', 'enabled', 'fills', 'cost', 'pnl', 'events', 'roi', 'ci_low', 'ci_high', 'verdict', 'need_more', 'fill_quality', 'queue_stats'];

const NAMES = {
  KXDEMOA: 'Demo series A',
  KXDEMOB: 'Demo series B',
  KXDEMOC: 'Demo series C',
  KXDEMOD: 'Demo series D',
  KXDEMOE: 'Demo series E',
  KXDEMOF: 'Demo series F',
};

export function marketName(series) {
  return NAMES[series] ? titleCase(NAMES[series]) : series;
}

// Plain-words meaning of each chip (item 23), shown as the chip tooltip.
const VERDICT_TEXT = {
  pause: 'The research says this series is clearly losing. Pausing is suggested.',
  keep: 'Results are in line with what the research expected. Keep it running.',
  collecting: 'Not enough fills yet to judge this series.',
};

// /api/markets rows: {series, enabled, ...maker_monitor per_series stats (fills, cost, pnl, events, roi,
// ci_low, ci_high), verdict: "pause"|"keep"|null, need_more: int|null} (from summary.actions).
export function verdictInfo(m) {
  const more = typeof m.need_more === 'number' && m.need_more > 0 ? m.need_more : 0;
  const v = String(m.verdict || m.action || '').toLowerCase();
  if (v.includes('pause') || v.includes('losing')) return { label: 'Pause Suggested', tone: 'danger', title: VERDICT_TEXT.pause };
  const moreFills = `${more} More ${countWord(more, 'Fill')}`;
  if (v === 'keep' && more) return { label: `Keep (${moreFills} to Judge)`, tone: 'muted', title: VERDICT_TEXT.collecting };
  if (v === 'keep' || v === 'ok') return { label: 'Keep', tone: 'ok', title: VERDICT_TEXT.keep };
  if (v || more) return { label: more ? `Collecting Data (${moreFills})` : titleCase(v), tone: 'muted', title: VERDICT_TEXT.collecting };
  return null;
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function Toolbar({ onRefresh, loading }) {
  return (
    <div className="strat-toolbar">
      <button type="button" className="btn small" onClick={onRefresh} disabled={loading}>
        Refresh
      </button>
    </div>
  );
}

/** Maker, Overview: state, Stop, last passes, exposure, fill quality. */
export function MakerOverview() {
  const { status, unconfirmed } = useStatus();
  const { data, error, loading, staleAt, reload } = useResource(() => api.getMarkets(), '/api/markets');
  const badge = badgeKind(status);
  const running = badge.kind === 'live' || badge.kind === 'demo';
  const stateWords = running ? `Running, ${badge.kind === 'live' ? 'live' : 'demo'}` : badge.kind === 'error' ? 'Needs attention' : 'Stopped';
  const a = status && status.account;
  const limit = riskLimit(status);
  const frac = isNum(a && a.exposure) && isNum(limit) && limit > 0 ? Math.min(1, a.exposure / limit) : null;
  const level = frac == null ? 'ok' : frac > 0.9 ? 'danger' : frac > 0.7 ? 'warn' : 'ok';
  return (
    <div className="strat-view">
      <Toolbar onRefresh={reload} loading={loading} />
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      <Card title="State">
        <p data-testid="maker-state">
          {unconfirmed ? `Last known: ${stateWords.toLowerCase()}` : stateWords}
          {badge.reason ? <span className="muted"> ({badge.reason})</span> : null}
        </p>
        <p className="hint">
          Last run {status && status.last_run_at ? ago(status.last_run_at) : '—'}
          {!unconfirmed && status && status.next_run_at ? ` · next due ${ago(status.next_run_at)}` : ''}
        </p>
        <StopButton />
      </Card>
      <Card title="Recent passes">
        <HeartbeatStrip passes={status && status.recent_passes} />
      </Card>
      <Card title="Exposure">
        {frac == null ? (
          <p className="muted" title="Not available yet">—</p>
        ) : (
          <>
            <p>
              {formatMoney(a.exposure)} at risk of {formatMoney(limit, { digits: Number.isInteger(limit) ? 0 : 2 })}
              <HelpIcon term="exposure" />
            </p>
            <div className="meter" role="img" aria-label={`At risk ${Math.round(frac * 100)}% of limit`}>
              <div className={`meter-fill meter-${level}`} style={{ width: `${(frac * 100).toFixed(1)}%` }} />
            </div>
          </>
        )}
      </Card>
      {data && 'fill_quality' in data && (
        <Card title={<>Fill Quality, All Series<HelpIcon term="fill-quality" /></>} subtitle="Our fills vs the market tape.">
          <FillQuality fq={data.fill_quality} />
        </Card>
      )}
    </div>
  );
}

/** Maker, Results: settled P&L for the range, then one read-only card per series (return and likely range). */
export function MarketsResults() {
  const { data, error, loading, staleAt, reload } = useResource(() => api.getMarkets(), '/api/markets');
  const { status } = useStatus();
  const [range, setRange] = useState(DEFAULT_RANGE);
  const sum = useStrategySummary({ range, status });
  const maker = sum.strategies.find((s) => s.id === 'maker');
  const markets = (data && data.markets) || [];
  // Only to learn whether a market has had a loss yet: without it the range rule still uses the market count.
  const wins = useResource(() => api.analyticsSummary({ days: 30 }), '/api/analytics/summary?days=30');
  const records = winRecords(wins.data);
  return (
    <div className="strat-view">
      <Toolbar
        onRefresh={() => {
          reload();
          sum.reload();
        }}
        loading={loading}
      />
      <Card title="Settled P&L" subtitle="By settlement date, your time zone." actions={<RangeSelect value={range} onChange={setRange} label="Range" />}>
        <p className="strat-figure num" data-testid="maker-pnl" title={maker && isNum(maker.pnl) ? undefined : 'Not available yet'}>
          {maker && isNum(maker.pnl) ? formatMoney(maker.pnl, { sign: true }) : '—'}
        </p>
        <p className="hint">
          {rangeLabel(range)}
          {maker && isNum(maker.roi) ? ` · return ${formatReturn(maker.roi)}` : ''}
        </p>
      </Card>
      {data && data.report_utc && <p className="hint">Stats from the health report {ago(data.report_utc)}.</p>}
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading />}
      {data && !markets.length && <p className="muted">No markets reported.</p>}

      {markets.length > 0 && (
        <p className="hint mk-legend" data-testid="ci-explainer">
          Tap a market for its chart. The dot is the return so far and the bar is the likely range
          <HelpIcon term="ci-bars" />. If the bar crosses zero, or there is no bar yet, it is too soon to say.
        </p>
      )}

      <div className="mk-list">
        {markets.map((m) => {
          const v = verdictInfo(m);
          const extra = Object.fromEntries(Object.entries(m).filter(([k]) => !KNOWN.includes(k)));
          const hasCi = typeof m.ci_low === 'number' && typeof m.ci_high === 'number';
          return (
            <FoldCard
              key={m.series}
              className="mk-card"
              title={
                <>
                  {marketName(m.series)} <span className="mono muted small">{m.series}</span>
                </>
              }
              summary={
                <>
                  <span>
                    <b className="num">{num(m.fills)}</b> {countWord(m.fills, 'Fill')}
                  </span>
                  <span className={m.pnl < 0 ? 'neg' : m.pnl > 0 ? 'pos' : ''}>{formatMoney(m.pnl, { sign: true })}</span>
                  {!m.enabled && <Chip tone="muted">Not Trading</Chip>}
                  {v && (
                    <Chip tone={v.tone} title={v.title}>
                      {v.label}
                    </Chip>
                  )}
                </>
              }
            >
              <dl className="stats">
                <div>
                  <dt>Fills<HelpIcon term="fill" /></dt>
                  <dd>{num(m.fills)}</dd>
                </div>
                <div>
                  <dt>P&amp;L<HelpIcon term="pnl" /></dt>
                  <dd className={m.pnl < 0 ? 'neg' : ''}>{formatMoney(m.pnl, { sign: true })}</dd>
                </div>
                <div>
                  <dt>Return<HelpIcon term="roi" /></dt>
                  <dd className={m.roi < 0 ? 'neg' : ''}>{formatPct(m.roi, { digits: 1 })}</dd>
                </div>
                <div>
                  <dt>Spent</dt>
                  <dd>{formatMoney(m.cost)}</dd>
                </div>
              </dl>
              {hasCi && tooFewForRange(m, records[m.series]) ? (
                <p className="hint" data-testid="range-too-few">
                  {TOO_FEW_TEXT}
                </p>
              ) : hasCi ? (
                <>
                  <CiSummary roi={m.roi} low={m.ci_low} high={m.ci_high} />
                  <CiBar roi={m.roi} low={m.ci_low} high={m.ci_high} />
                </>
              ) : (
                <p className="hint">Not enough fills yet for a likely range.</p>
              )}
              {m.fill_quality && <FillQuality fq={m.fill_quality} compact />}
              {Object.keys(extra).length > 0 && (
                <details className="more-details">
                  <summary>More Stats</summary>
                  <KeyValue data={extra} />
                </details>
              )}
            </FoldCard>
          );
        })}
      </div>
    </div>
  );
}

/** Maker, Controls: pause / resume each series (fresh 2FA), then the Maker settings (same form as Settings). */
export function MarketsControls() {
  const { data, error, loading, staleAt, reload } = useResource(() => api.getMarkets(), '/api/markets');
  const config = useResource(() => api.getConfig(), '/api/config');
  const { twoFactor, notify } = useDialogs();
  const [busy, setBusy] = useState(null);
  const markets = (data && data.markets) || [];

  async function toggle(m) {
    const enable = !m.enabled;
    setBusy(m.series);
    try {
      const r = await twoFactor({
        title: enable ? `Resume ${m.series}?` : `Pause ${m.series}?`,
        message: enable
          ? 'The bot will quote this series again from the next pass.'
          : 'The bot stops placing new bids on this series. Existing fills are held to settlement.',
        confirmLabel: enable ? 'Resume' : 'Pause',
        danger: !enable,
        action: (code) => api.setMarket(m.series, enable, code),
      });
      if (r) {
        notify({
          kind: 'ok',
          title: `${m.series} ${enable ? 'resumed' : 'paused'}`,
          text: r.version != null ? `Settings saved as version ${r.version}.` : undefined,
        });
        reload();
      }
    } catch (err) {
      notify({ kind: 'error', title: 'Change not saved', text: err.message, details: err.details });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="strat-view">
      <Toolbar
        onRefresh={() => {
          reload();
          config.reload();
        }}
        loading={loading}
      />
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading />}
      {data && !markets.length && <p className="muted">No markets reported.</p>}
      {markets.length > 0 && (
        <Card title="Series" subtitle="Pause or resume a series. Each change needs your code.">
          <ul className="rows strat-series">
            {markets.map((m) => (
              <li key={m.series} className="row-item strat-series-row">
                <div className="strat-series-name">
                  <span>{marketName(m.series)}</span> <span className="mono muted small">{m.series}</span>
                  <div className="muted small">{m.enabled ? 'On' : 'Not trading'}</div>
                </div>
                <button
                  type="button"
                  className={`btn strat-pause ${m.enabled ? '' : 'primary'}`}
                  onClick={() => toggle(m)}
                  disabled={!!busy}
                  aria-label={`${m.enabled ? 'Pause' : 'Resume'} ${marketName(m.series)}`}
                >
                  {busy === m.series ? 'Saving…' : m.enabled ? <><IconLock /> Pause</> : <><IconLock /> Resume</>}
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <StaleNotice staleAt={config.error ? config.staleAt : null} />
      <ErrorNotice error={config.error} onRetry={config.reload} />
      {config.data && config.data.config && <ConfigEditor resource={config} filter={isMakerKey} title="Maker settings" />}
      <p className="hint">
        Global limits and polling live in <Link to="/settings">Settings</Link>.
      </p>
    </div>
  );
}

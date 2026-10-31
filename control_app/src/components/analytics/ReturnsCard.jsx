import { useEffect, useMemo, useState } from 'react';
import Card from '../Card.jsx';
import Empty from '../Empty.jsx';
import HelpIcon from '../HelpIcon.jsx';
import PnlScope from '../PnlScope.jsx';
import RangeSelect from '../RangeSelect.jsx';
import Skeleton from '../Skeleton.jsx';
import { ErrorNotice } from '../Notices.jsx';
import ReturnsBars from './ReturnsBars.jsx';
import ReturnsSheet from './ReturnsSheet.jsx';
import ReturnsTable, { ReturnCell } from './ReturnsTable.jsx';
import { GRANULARITIES, daysFor, useReturns } from '../../hooks/useReturns.js';
import { signCls } from '../../utils/analytics.js';
import { formatMoney, num } from '../../utils/format.js';
import { SMALL_SAMPLE_DEFAULT, isSmall, periodTitle, strategyColor, strategyLabel } from '../../utils/returns.js';
import '../../styles/returns.css';

const RANGE_OPTS = GRANULARITIES;
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function Readout({ period, granularity, onOpen }) {
  const keys = Object.keys(period.by_strategy || {});
  return (
    <div className="rt-readout" data-testid="returns-readout" aria-live="polite">
      <p className="rt-fig">
        <b className={`num ${signCls(period.realized_pnl)}`} data-testid="returns-dollar">
          {formatMoney(period.realized_pnl, { sign: true })}
        </b>
        <span className="rt-pct num" data-testid="returns-pct">
          <ReturnCell period={period} />
        </span>
        <span className="rt-when muted">{periodTitle(period, granularity)}</span>
      </p>
      <p className="hint rt-meta">
        {period.settled_fills ? (
          <>
            {num(period.settled_fills)} settled fill{period.settled_fills === 1 ? '' : 's'}
            {keys.map((k) => (
              <span key={k}>
                {' · '}
                {strategyLabel(k)} {formatMoney(period.by_strategy[k], { sign: true })}
              </span>
            ))}
          </>
        ) : (
          'No settlements'
        )}
        {period.partial && period.elapsed_hours != null && <> · {(period.elapsed_hours / 24).toFixed(1)} days elapsed</>}
      </p>
      {isSmall(period) && <p className="hint rt-smallnote">Small sample: fewer than {SMALL_SAMPLE_DEFAULT} settled fills.</p>}
      {period.settled_fills > 0 && (
        <button type="button" className="btn small rt-details" onClick={onOpen}>
          Details
        </button>
      )}
    </div>
  );
}

/**
 * Returns per day, week or month (docs/RETURNS_FEATURE_CONTRACT.md): the Day | Week | Month control picks the
 * granularity and this card requests its own window (Day follows the page's Window, Week asks for 84 days, Month for
 * 180). Stacked bars, a readout that follows the selected bar, a table alternative and a fills sheet.
 */
export default function ReturnsCard({ days, env, onUseDays }) {
  const [granularity, setGranularity] = useState('day');
  const [selected, setSelected] = useState(null);
  const [asTable, setAsTable] = useState(false);
  const [open, setOpen] = useState(null); // period index
  const reqDays = daysFor(granularity, days);
  const { data, error, loading, staleAt, reload } = useReturns({ granularity, days: reqDays, env });

  useEffect(() => {
    setSelected(null);
    setOpen(null);
  }, [granularity, reqDays, env]);

  const periods = useMemo(() => (data && Array.isArray(data.periods) ? data.periods : []), [data]);
  const idx = selected == null || selected >= periods.length ? periods.length - 1 : selected;
  const current = periods[idx];
  const emptyAll = !!data && (!periods.length || (data.totals ? !data.totals.settled_fills : periods.every((p) => !p.settled_fills)));
  const anySmall = periods.some((p) => isSmall(p));
  const youSettled = periods.reduce((a, p) => a + ((p.you && p.you.settled_fills) || 0), 0);
  const youPnl = periods.reduce((a, p) => a + ((p.you && p.you.realized_pnl) || 0), 0);
  const keys = [];
  for (const p of periods) for (const k of Object.keys(p.by_strategy || {})) if (!keys.includes(k)) keys.push(k);

  return (
    <Card
      title={
        <>
          Returns
          <HelpIcon term="period-return" />
        </>
      }
    >
      <RangeSelect label="Return period" value={granularity} options={RANGE_OPTS} onChange={setGranularity} className="rt-gran" />

      {loading && !data && <Skeleton rows={1} figure label="Loading returns" className="rt-skel" />}
      <ErrorNotice error={error} onRetry={reload} staleAt={error ? staleAt : null} />

      {data && (
        <div className={error ? 'rt-dim' : undefined} data-testid="returns-body">
          {data.truncated && <div className="notice notice-info">Large Window: Only the most recent fills were counted.</div>}
          {emptyAll ? (
            <Empty
              compact
              what="No settled fills in this window."
              why="Fills settle after their market closes."
              next={granularity === 'day' && days < 90 && onUseDays ? { label: 'Use 90 days', onClick: () => onUseDays(90) } : undefined}
            />
          ) : (
            <>
              {current && <Readout period={current} granularity={granularity} onOpen={() => setOpen(idx)} />}
              {asTable ? (
                <ReturnsTable periods={periods} granularity={granularity} onOpen={setOpen} />
              ) : (
                <ReturnsBars periods={periods} granularity={granularity} selected={idx} onSelect={setSelected} onOpen={setOpen} />
              )}
              {!asTable && (
                <ul className="rt-legend" aria-label="Chart key">
                  {keys.map((k, i) => (
                    <li key={k}>
                      <i className="rt-dot" style={{ background: strategyColor(i) }} />
                      {strategyLabel(k)}
                    </li>
                  ))}
                  <li>
                    <i className="rt-ticksw" />
                    Total
                  </li>
                  {anySmall && (
                    <li>
                      <i className="rt-fadesw" />
                      Fewer than {data.small_sample_threshold || SMALL_SAMPLE_DEFAULT} fills
                    </li>
                  )}
                </ul>
              )}
              {anySmall && !asTable && (
                <p className="hint">Faded bars have fewer than {data.small_sample_threshold || SMALL_SAMPLE_DEFAULT} settled fills. One fill can swing a period, so read these as noise.</p>
              )}
              <button type="button" className="btn small rt-toggle" aria-pressed={asTable} onClick={() => setAsTable((v) => !v)}>
                {asTable ? 'Show as bars' : 'Show as table'}
              </button>
            </>
          )}
          <PnlScope id="returns" />
          <p className="hint">
            Percentages are not additive: each period has its own base.
            <HelpIcon term="return-base" />
            {granularity !== 'day' && (
              <> {granularity === 'week' ? 'Week shows the last 12 weeks' : 'Month shows about the last 6 months'}, whole periods only.</>
            )}
            {periods.some((p) => p.partial) && (
              <>
                {' '}
                The latest period is still running (so far)
                <HelpIcon term="partial-period" />.
              </>
            )}
          </p>
          {youSettled > 0 && (
            <p className="hint" data-testid="returns-you-note">
              Your own trades are not included ({num(youSettled)} settled, {formatMoney(youPnl, { sign: true })}).
            </p>
          )}
          {data.label && (
            <p className="hint" data-testid="returns-label">
              {data.label}
              <HelpIcon term="indicative" />
            </p>
          )}
          {(data.notes || []).map((n, i) => (
            <p className="hint" key={i}>
              {n}
            </p>
          ))}
        </div>
      )}

      {open != null && periods[open] && (
        <ReturnsSheet period={periods[open]} granularity={granularity} days={reqDays} env={env} onClose={() => setOpen(null)} />
      )}
    </Card>
  );
}

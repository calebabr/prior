import { useMemo, useState } from 'react';
import { ErrorNotice, StaleNotice } from '../components/Notices.jsx';
import Empty from '../components/Empty.jsx';
import Skeleton from '../components/Skeleton.jsx';
import PnlStats from '../components/PnlStats.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import SourceBadge from '../components/SourceBadge.jsx';
import KalshiLink from '../components/KalshiLink.jsx';
import Time from '../components/Time.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import { formatMoney } from '../utils/format.js';
import { EVENT_GROUPS, eventGroupOf, eventLabel, groupByDay, matchesSource, sourceChips } from '../utils/activity.js';
import '../styles/settings.css';
import CollapsibleList from '../components/CollapsibleList.jsx';

// Journal rows: {utc|ts, event, ticker, side, price, count, cost, cell, note, source: "bot"|"you", strategy}
const LIMIT = 100;

function rowTime(r) {
  return r.utc || r.ts || r.time || null;
}

function Chips({ label, options, value, onChange }) {
  return (
    <div className="chips chips-lg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" className={`chip ${value === o.id ? 'active' : ''}`} aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export default function Activity() {
  const { data, error, loading, staleAt, reload } = useResource(() => api.getActivity(LIMIT), `/api/activity?limit=${LIMIT}`);
  // The strategy registry names and colours the source badges; if it is unavailable the defaults are used.
  const { data: strategies } = useResource(() => api.getStrategies(), '/api/strategies');
  const { status } = useStatus();
  const [source, setSource] = useState('all');
  const [kind, setKind] = useState('all');
  const items = useMemo(() => (data && data.items) || [], [data]);

  const srcChips = useMemo(() => sourceChips(strategies), [strategies]);
  const kindChips = useMemo(() => {
    const present = new Set(items.map((r) => eventGroupOf(r.event)));
    return [{ id: 'all', label: 'All' }, ...EVENT_GROUPS.filter((g) => present.has(g.id)).map((g) => ({ id: g.id, label: g.label }))];
  }, [items]);

  const shown = useMemo(
    () => items.filter((r) => matchesSource(r, source) && (kind === 'all' || eventGroupOf(r.event) === kind)),
    [items, source, kind],
  );
  const days = useMemo(() => groupByDay(shown, rowTime), [shown]);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Activity</h1>
        <button type="button" className="btn small" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>
      <p className="hint">
        Quotes<HelpIcon term="quote" />, fills<HelpIcon term="fill" />, cancels / re-quotes
        <HelpIcon term="re-quote" /> and settlements, from the bot and from your own trades.
      </p>
      {status && (
        <section className="activity-pnl" aria-label="Realized profit and loss">
          <PnlStats pnl={status.pnl} big={false} emptyText="P&L appears once fills settle." scope="maker" />
        </section>
      )}
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Skeleton rows={4} label="Loading activity" />}

      {items.length > 0 && (
        <>
          <p className="chip-label">Source</p>
          <Chips label="Filter by source" options={srcChips} value={source} onChange={setSource} />
          {kindChips.length > 2 && (
            <>
              <p className="chip-label">Type</p>
              <Chips label="Filter by type" options={kindChips} value={kind} onChange={setKind} />
            </>
          )}
        </>
      )}

      {data && !shown.length && (
        items.length ? (
          <Empty
            what="Nothing matches these filters."
            why="Try a different source or type."
            next={{ label: 'Show all', onClick: () => { setSource('all'); setKind('all'); } }}
          />
        ) : (
          <Empty what="No activity yet." why="Quotes, fills and settlements appear here once the bot runs." />
        )
      )}
      {days.map((d) => (
        <section key={d.key} aria-label={d.label}>
          <div className="day-head">
            <h2>{d.label}</h2>
            <span className="muted small num">{d.items.length}</span>
          </div>
          <CollapsibleList items={d.items} pageKey={`activity-${d.key}`} label="rows">
            {(vis) => (
              <ul className="rows">
                {vis.map((r, i) => (
                  <li key={r.row_hash || `${rowTime(r)}-${i}`} className="row-item" data-source={r.source === 'you' ? 'you' : 'bot'}>
                    <div className="row-top">
                      <span className="row-tags">
                        <span className={`pill pill-event pill-${String(r.event || '').replace(/[^a-z_]/gi, '')}`}>{eventLabel(r.event)}</span>
                        <SourceBadge row={r} strategies={strategies} />
                      </span>
                      <Time className="muted small" value={rowTime(r)} />
                    </div>
                    {r.ticker && <div className="mono row-ticker">{r.ticker}</div>}
                    <div className="row-meta num">
                      {r.side && <span>{String(r.side).toUpperCase()}</span>}
                      {r.price != null && r.price !== '' && <span>@ {Math.round(Number(r.price) * 100)}¢</span>}
                      {r.count != null && r.count !== '' && <span>× {Number(r.count).toFixed(2)}</span>}
                      {r.cost != null && r.cost !== '' && <span>{formatMoney(Number(r.cost))}</span>}
                    </div>
                    {r.note && <div className="muted small">{r.note}</div>}
                    {r.ticker && (
                      <div className="row-links">
                        <KalshiLink ticker={r.ticker} />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CollapsibleList>
        </section>
      ))}
    </div>
  );
}

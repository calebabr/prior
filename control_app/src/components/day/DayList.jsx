import Chip from '../Chip.jsx';
import CollapsibleList from '../CollapsibleList.jsx';
import KalshiLink from '../KalshiLink.jsx';
import SourceBadge from '../SourceBadge.jsx';
import { DASH, formatMoney, formatTime, num } from '../../utils/format.js';
import { STATUS_TONE, STATUS_WORD, groupByEvent } from '../../utils/day.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const cents = (p) => (isNum(p) ? `${Math.round(p * 100)}¢` : DASH);

/** One fill or settlement. The status is a word in a chip; the sign of a loss is in the figure, never colour alone. */
function Row({ r, strategies, settled }) {
  const pnl = isNum(r.pnl) ? r.pnl : null;
  return (
    <li className="day-row" data-testid="day-row" data-status={r.status}>
      <div className="day-row-main">
        <SourceBadge row={{ source: 'bot', strategy: r.strategy }} strategies={strategies} />
        <span className="day-row-what num">
          {String(r.side || '').toUpperCase()} {num(r.count)} × {cents(r.price)}
        </span>
        <span className="day-row-cost num">{formatMoney(r.cost)}</span>
        <Chip tone={STATUS_TONE[r.status] || 'muted'}>{STATUS_WORD[r.status] || r.status}</Chip>
      </div>
      <div className="day-row-sub">
        {settled ? (
          <span className={`num day-row-pnl ${pnl != null && pnl < 0 ? 'neg' : pnl > 0 ? 'pos' : ''}`.trim()}>
            {pnl != null && pnl < 0 ? 'Lost ' : pnl != null && pnl > 0 ? 'Won ' : ''}
            {formatMoney(pnl, { sign: true })}
          </span>
        ) : r.status === 'open' ? (
          <span className="muted">{r.settles_at ? `Settles by ${formatTime(r.settles_at, { withDate: true })}` : 'Settlement time not known'}</span>
        ) : (
          <span className="muted">Filled {formatTime(r.ts)}</span>
        )}
        <KalshiLink ticker={r.ticker} />
      </div>
    </li>
  );
}

/**
 * A list of day rows grouped by market event, collapsed at 25 (CollapsibleList). `settled` shows P&L per row.
 */
export default function DayList({ rows, strategies, settled = false, pageKey, label }) {
  return (
    <CollapsibleList items={rows} limit={25} pageKey={pageKey} label={label}>
      {(visible) =>
        groupByEvent(visible).map((g) => (
          <div key={g.event} className="day-group" role="group" aria-label={g.event}>
            <h4 className="day-event">{g.event}</h4>
            <ul className="day-rows">
              {g.rows.map((r) => (
                <Row key={r.fill_id} r={r} strategies={strategies} settled={settled} />
              ))}
            </ul>
          </div>
        ))
      }
    </CollapsibleList>
  );
}

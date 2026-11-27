import { num, pct } from '../utils/format.js';
import HelpIcon from './HelpIcon.jsx';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
// Below this many fills a 0.10 share gap is ~1 standard error (noise), so don't flag.
export const MIN_FILLS_TO_COMPARE = 50;

/**
 * Fill quality vs the market tape (GET /api/status and /api/markets `fill_quality`):
 *   {n, trade_through_share, avg_move_60m_c, baseline: {trade_through_share, avg_move_60m_c}} | null
 * Worse than baseline = a share well above it and/or a more negative 60-min move
 * (our fills come from the back of the queue). Null or n = 0 renders a short note.
 */
export default function FillQuality({ fq, compact = false }) {
  if (!fq) return compact ? null : <p className="hint">Fill quality unavailable.</p>;
  const n = isNum(fq.n) ? fq.n : 0;
  if (!n || (!isNum(fq.trade_through_share) && !isNum(fq.avg_move_60m_c))) {
    return <p className="hint">Fill Quality: No fills measured yet.</p>;
  }
  const b = fq.baseline || {};
  const enough = n >= MIN_FILLS_TO_COMPARE;
  const shareWorse = enough && isNum(fq.trade_through_share) && isNum(b.trade_through_share) && fq.trade_through_share > b.trade_through_share + 0.1;
  const moveWorse = enough && isNum(fq.avg_move_60m_c) && isNum(b.avg_move_60m_c) && fq.avg_move_60m_c < b.avg_move_60m_c - 0.5;
  const cents = (v) => (isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}¢` : '—');
  return (
    <div className="fill-quality" data-testid="fill-quality">
      <dl className="stats">
        <div>
          <dt>Traded Through<HelpIcon term="trade-through" /></dt>
          <dd className={shareWorse ? 'neg' : ''}>
            {pct(fq.trade_through_share, { sign: false, digits: 0 })}
            <span className="muted small"> vs {pct(b.trade_through_share, { sign: false, digits: 0 })}</span>
          </dd>
        </div>
        <div>
          <dt>Move 60 Min After Fill</dt>
          <dd className={moveWorse ? 'neg' : ''}>
            {cents(fq.avg_move_60m_c)}
            <span className="muted small"> vs {cents(b.avg_move_60m_c)}</span>
          </dd>
        </div>
      </dl>
      <p className="hint">
        {num(n)} {n === 1 ? 'fill' : 'fills'} measured against the market tape.
        {!enough
          ? ` Too few fills to compare (${n}/${MIN_FILLS_TO_COMPARE}).`
          : shareWorse || moveWorse
            ? ' Worse Than the Tape: Fills look like the back of the queue.'
            : ' In line with the tape.'}
      </p>
    </div>
  );
}

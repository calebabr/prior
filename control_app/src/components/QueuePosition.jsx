import { num, pct } from '../utils/format.js';
import HelpIcon from './HelpIcon.jsx';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
// Same bar as fill quality: under 50 fills the shares are mostly noise.
export const MIN_QUEUE_FILLS = 50;
export const BUCKETS = ['0', '1-50', '51-200', '201-1000', '>1000'];

export const QUEUE_EXPLAINER =
  "You're filled only after the contracts ahead of you trade or cancel; front-of-line fills are the ones the historical edge came from.";
export const DEMO_NOTE = "Numbers measured on demo aren't representative of live queues.";

const share = (v) => pct(v, { sign: false, digits: 0 });

function sortedBuckets(rows) {
  if (!Array.isArray(rows)) return [];
  const rank = (b) => {
    const i = BUCKETS.indexOf(String(b));
    return i === -1 ? BUCKETS.length : i;
  };
  return rows.filter((r) => r && r.bucket != null).slice().sort((a, b) => rank(a.bucket) - rank(b.bucket));
}

function BucketTable({ rows }) {
  const list = sortedBuckets(rows);
  if (!list.length) return null;
  const max = Math.max(0, ...list.map((r) => (isNum(r.fill_rate) ? r.fill_rate : 0)));
  return (
    <table className="queue-table" aria-label="Fill rate by contracts ahead when joining">
      <thead>
        <tr>
          <th scope="col">Ahead of Us<HelpIcon term="queue-position" /></th>
          <th scope="col">Quotes</th>
          <th scope="col">Fills</th>
          <th scope="col">Fill Rate<HelpIcon term="fill-rate" /></th>
        </tr>
      </thead>
      <tbody>
        {list.map((r) => {
          const w = isNum(r.fill_rate) && max > 0 ? Math.max(2, (r.fill_rate / max) * 100) : 0;
          return (
            <tr key={r.bucket}>
              <th scope="row">{r.bucket}</th>
              <td>{num(r.episodes)}</td>
              <td>{num(r.fills)}</td>
              <td className="queue-rate">
                <span className="queue-bar" aria-hidden="true">
                  <span style={{ width: `${w}%` }} />
                </span>
                <span>{share(r.fill_rate)}</span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Queue position at quote time (GET /api/markets and /api/status `queue_stats`):
 *   {episodes, fills, median_queue_ahead_at_join, requote_reset_share, front_fill_share,
 *    by_queue: [{bucket, episodes, fills, fill_rate, trade_through_share}], updated_utc} | null
 * Display only. variant: 'full' (default), 'compact' (market rows), 'summary' (one line, Overview). `live`: the
 * numbers are from live mode, so the note about demo queues is left out.
 */
export default function QueuePosition({ qs, variant = 'full', demo = false, live = false }) {
  if (!qs) {
    return variant === 'compact' ? null : <p className="hint">Queue data unavailable.</p>;
  }
  const episodes = isNum(qs.episodes) ? qs.episodes : 0;
  const fills = isNum(qs.fills) ? qs.fills : 0;
  if (!episodes) return <p className="hint">Queue Position: No quotes measured yet.</p>;
  const enough = fills >= MIN_QUEUE_FILLS;
  const fewNote = enough ? '' : ` Too few fills to judge (${fills}/${MIN_QUEUE_FILLS}).`;
  const demoNote = variant === 'summary' && demo ? ' Demo numbers are not representative.' : '';

  if (variant === 'summary') {
    return (
      <p className="hint" data-testid="queue-summary">
        Queue<HelpIcon term="queue-position" />: typically {num(qs.median_queue_ahead_at_join)} ahead of us, {share(qs.front_fill_share)} of fills from the
        front, {share(qs.requote_reset_share)} lost our spot when we moved our price.{fewNote}{demoNote}
      </p>
    );
  }

  return (
    <div className={`queue-position${variant === 'compact' ? ' compact' : ''}`} data-testid="queue-position">
      {variant === 'compact' && <h3 className="subhead">Queue position<HelpIcon term="queue-position" /></h3>}
      <dl className="stats">
        <div>
          <dt>Typical Place in Line<HelpIcon term="queue-position" /></dt>
          <dd>{num(qs.median_queue_ahead_at_join)}</dd>
        </div>
        <div>
          <dt>Fills From the Front<HelpIcon term="front-of-line" /></dt>
          <dd>{share(qs.front_fill_share)}</dd>
        </div>
        <div>
          <dt>Lost Our Spot When We Moved Our Price<HelpIcon term="re-quote" /></dt>
          <dd>{share(qs.requote_reset_share)}</dd>
        </div>
        <div>
          <dt>Quotes / Fills</dt>
          <dd>
            {num(episodes)} / {num(fills)}
          </dd>
        </div>
      </dl>
      {variant === 'full' && <BucketTable rows={qs.by_queue} />}
      {variant === 'full' && <p className="hint">{QUEUE_EXPLAINER}</p>}
      {!enough && <p className="hint">Too few fills to judge ({fills}/{MIN_QUEUE_FILLS}).</p>}
      {variant === 'full' && !live && <p className="hint">{DEMO_NOTE}</p>}
    </div>
  );
}

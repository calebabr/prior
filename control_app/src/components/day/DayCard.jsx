import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { IconChevron } from '../Icons.jsx';
import Skeleton from '../Skeleton.jsx';
import { useDay } from '../../hooks/useDay.js';
import { DASH, formatMoney, num } from '../../utils/format.js';
import { dayPath, todayString } from '../../utils/day.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const tone = (v) => (isNum(v) ? (v < 0 ? 'neg' : v > 0 ? 'pos' : '') : '');

/** Home: today's three numbers (filled, settled, realized) as one link to the Day page. */
export default function DayCard() {
  const date = useMemo(() => todayString(), []);
  const { data, error, loading, reload } = useDay({ date });
  const s = data && data.summary;
  const empty = s && s.filled.count === 0 && s.settled.count === 0;
  return (
    <section className="home-section" aria-labelledby="home-day-h" data-testid="home-day">
      <h2 className="home-label" id="home-day-h">Today</h2>
      {loading && !data ? (
        <Skeleton rows={1} label="Loading today" />
      ) : s ? (
        <Link to={dayPath(date)} className="home-row day-card" data-testid="home-day-link">
          {empty ? (
            <span className="day-card-empty muted">No fills yet today.</span>
          ) : (
            <dl className="day-card-figs">
              <div>
                <dt>Filled</dt>
                <dd className="num">{num(s.filled.count)}</dd>
              </div>
              <div>
                <dt>Settled</dt>
                <dd className="num">{num(s.settled.count)}</dd>
              </div>
              <div>
                <dt>Realized</dt>
                <dd className={`num ${tone(s.realized)}`.trim()}>{formatMoney(s.realized, { sign: true })}</dd>
              </div>
            </dl>
          )}
          <span className="home-row-chev" aria-hidden="true">
            <IconChevron />
          </span>
          <span className="sr-only">Open today's fills</span>
        </Link>
      ) : (
        <p className="hint" data-testid="home-day-error">
          Today: {DASH}{' '}
          {error && (
            <button type="button" className="btn small" onClick={reload}>
              Retry
            </button>
          )}
        </p>
      )}
    </section>
  );
}

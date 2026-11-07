import { useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import Card from '../components/Card.jsx';
import Empty from '../components/Empty.jsx';
import Skeleton from '../components/Skeleton.jsx';
import DayList from '../components/day/DayList.jsx';
import { ErrorNotice } from '../components/Notices.jsx';
import { useDay } from '../hooks/useDay.js';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import { DAY_CHIPS, dayLabel, dayPath, isDateString, pctWhole, todayString, verdict } from '../utils/day.js';
import { DASH, formatMoney, formatTime, num } from '../utils/format.js';
import '../styles/day.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const tone = (v) => (isNum(v) ? (v < 0 ? 'neg' : v > 0 ? 'pos' : '') : '');
const signed = (v) => formatMoney(v, { sign: true });

function Figure({ label, value, sub, cls = '', testId }) {
  return (
    <div className="day-fig" data-testid={testId}>
      <dt>{label}</dt>
      <dd className={`num ${cls}`.trim()}>{value}</dd>
      <dd className="day-fig-sub">{sub}</dd>
    </div>
  );
}

/**
 * Day view (/performance/day/:date): what the bot did on one local day. Filled, settled and realized only (no resting
 * orders, no future days). Opens on today from Performance and Home, or on a past day from the daily Returns chart.
 */
export default function Day() {
  const { date } = useParams();
  const valid = isDateString(date);
  const today = todayString();
  const [chip, setChip] = useState('all');
  const strategies = useResource(() => api.getStrategies(), '/api/strategies');
  const { data, error, loading, staleAt, reload } = useDay({ date: valid ? date : null, strategy: chip, enabled: valid });
  if (!valid || date > today) return <Navigate to={dayPath(today)} replace />;

  const s = data && data.summary;
  const isToday = date === today;
  const label = `${isToday ? 'Today, ' : ''}${dayLabel(date)}`;
  const emptyDay = s && s.filled.count === 0 && s.settled.count === 0;
  const list = strategies.data || undefined;

  return (
    <div className="page day-page">
      <h1>Day</h1>
      <p className="day-date" data-testid="day-date">{label}</p>

      <div className="chips chips-lg" role="group" aria-label="Filter by strategy">
        {DAY_CHIPS.map((c) => (
          <button key={c.id} type="button" className={`chip ${chip === c.id ? 'active' : ''}`} aria-pressed={chip === c.id} onClick={() => setChip(c.id)}>
            {c.label}
          </button>
        ))}
      </div>

      {loading && !data && <Skeleton rows={4} figure label="Loading the day" />}
      <ErrorNotice error={error} onRetry={reload} staleAt={error ? staleAt : null} />
      {error && staleAt && data && <p className="hint" data-testid="day-stale">Last updated {formatTime(staleAt)}. This may be out of date.</p>}

      {data && (
        <div className={error ? 'day-dim' : undefined} data-testid="day-body">
          <Card className="day-summary">
            <dl className="day-figs">
              <Figure label="Filled" testId="day-filled" value={num(s.filled.count)} sub={`${formatMoney(s.filled.cost)} at cost`} />
              <Figure
                label="Settled"
                testId="day-settled"
                value={num(s.settled.count)}
                sub={`${num(s.settled.wins)} won, ${num(s.settled.losses)} lost`}
              />
              <Figure label="Realized" testId="day-realized" value={signed(s.realized)} cls={tone(s.realized)} sub={s.settled.count ? 'of what settled' : DASH} />
            </dl>
            <p className="day-verdict" data-testid="day-verdict">{verdict(s, signed)}</p>
          </Card>

          {emptyDay ? (
            <Empty compact what={isToday ? 'No fills yet today.' : 'No fills on this day.'} why="Fills show up after the bot's resting bids are taken." />
          ) : (
            <>
              <Card title="Filled" subtitle={s.settles_later > 0 ? `${num(s.settles_later)} settle later` : undefined}>
                {data.filled.length === 0 ? (
                  <p className="muted">Nothing filled {isToday ? 'yet today' : 'on this day'}.</p>
                ) : (
                  <DayList rows={data.filled} strategies={list} pageKey="day-filled" label="fills" />
                )}
              </Card>
              <Card title="Settled" subtitle={s.settled.count ? `${signed(s.realized)} realized` : undefined}>
                {data.settled.length === 0 ? (
                  <p className="muted">Nothing settled {isToday ? 'yet today' : 'on this day'}.</p>
                ) : (
                  <DayList rows={data.settled} strategies={list} settled pageKey="day-settled" label="settlements" />
                )}
              </Card>
            </>
          )}

          <Card title="Day analytics" subtitle="Against the average of the 7 days before">
            <table className="day-an" data-testid="day-analytics">
              <thead>
                <tr>
                  <th scope="col"><span className="sr-only">Measure</span></th>
                  <th scope="col" className="num">{isToday ? 'Today' : 'This day'}</th>
                  <th scope="col" className="num">7-day avg</th>
                </tr>
              </thead>
              <tbody>
                {(data.analytics || []).map((a) => (
                  <tr key={a.key}>
                    <th scope="row">{a.label}</th>
                    <td className="num">{pctWhole(a.today)}</td>
                    <td className="num">{pctWhole(a.avg7)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <p className="hint">Bot fills only, in your time zone; settled by settlement date, like the Returns bars. Your own trades are not included.</p>
          {(data.notes || []).map((n, i) => (
            <p className="hint" key={i}>{n}</p>
          ))}
        </div>
      )}
    </div>
  );
}

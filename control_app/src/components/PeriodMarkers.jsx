import Card from './Card.jsx';
import Chip from './Chip.jsx';
import CollapsibleList from './CollapsibleList.jsx';
import Time from './Time.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import '../styles/markers.css';

const KIND = { release: 'Release', config: 'Settings', start: 'Start', stop: 'Stop', mode: 'Mode' };

/** Release and behaviour-change markers (newest first), grouped under the period they fall in. Presentational. */
export function MarkerList({ markers, periods = [] }) {
  const list = Array.isArray(markers) ? markers : [];
  const names = Object.fromEntries((periods || []).map((p) => [p.id, p.name]));
  if (!list.length) {
    return <p className="hint" data-testid="markers-empty">No releases or changes recorded yet.</p>;
  }
  return (
    <CollapsibleList items={list} limit={8} pageKey="period-markers" label="markers">
      {(visible) => (
        <ol className="marker-list" data-testid="marker-list">
          {visible.map((m, i) => (
            <li key={`${m.ts}-${m.kind}-${i}`} className="marker-row" data-kind={m.kind}>
              <div className="marker-head">
                <span className="marker-kind">{KIND[m.kind] || m.kind}</span>
                <Time value={m.ts} withDate className="marker-when" />
              </div>
              <div className="marker-body">
                <span className="marker-label">{m.label}</span>
                {m.detail ? <span className="marker-detail">{m.detail}</span> : null}
              </div>
              <div className="marker-tags">
                {m.changes_trading ? <Chip tone="warn">Trading behaviour changed</Chip> : null}
                {m.period_id && names[m.period_id] ? <Chip tone="muted">{names[m.period_id]}</Chip> : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </CollapsibleList>
  );
}

export default function PeriodMarkers() {
  const periods = useResource(() => api.getPeriods(), '/api/periods');
  const d = periods.data || {};
  return (
    <Card title="Releases And Changes">
      <MarkerList markers={d.markers} periods={d.periods} />
      <p className="hint">Markers come from the audit log. Mark a new period yourself when trading behaviour changes.</p>
    </Card>
  );
}

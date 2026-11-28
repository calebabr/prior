import { titleCase } from '../utils/format.js';

/** Period choices for a period-aware view, newest first (the API lists oldest first); the current one is marked. */
export function periodOptions(periods) {
  const list = Array.isArray(periods) ? [...periods].reverse() : [];
  return list.map((p) => ({ id: p.id, label: p.current ? `${p.name} (current)` : p.name }));
}

export default function PeriodSelect({ periods, value, onChange, includeAll = true, label = 'Period', id = 'period-select' }) {
  const opts = periodOptions(periods);
  return (
    <div className="period-select">
      <label htmlFor={id} className="period-select-label">{label}</label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        {includeAll && <option value="all">All</option>}
        {opts.map((o) => (
          <option key={o.id} value={o.id}>{titleCase(o.label)}</option>
        ))}
      </select>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from 'react';
import Card from '../components/Card.jsx';
import ChangeDiff from '../components/ChangeDiff.jsx';
import Chip from '../components/Chip.jsx';
import FoldCard from '../components/FoldCard.jsx';
import { IconLock } from '../components/Icons.jsx';
import Time from '../components/Time.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import '../styles/settings.css';
import CollapsibleList from '../components/CollapsibleList.jsx';

const PAGE = 50;

export const ALERT_TYPES = [
  { id: '', label: 'All' },
  { id: 'fill', label: 'Fills' },
  { id: 'settled', label: 'Settled' },
  { id: 'daily', label: 'Daily' },
  { id: 'weekly', label: 'Weekly' },
  { id: 'error', label: 'Errors' },
  { id: 'stop', label: 'Stop' },
  { id: 'breaker', label: 'Breaker' },
  { id: 'arb_legging', label: 'Legging' },
];

const TYPE_TONE = { fill: 'accent', settled: 'ok', daily: 'muted', weekly: 'muted', error: 'danger', stop: 'danger', breaker: 'danger', arb_legging: 'danger', test: 'muted', system: 'muted' };
const TYPE_LABEL = { fill: 'Fill', settled: 'Settled', daily: 'Daily', weekly: 'Weekly', error: 'Error', stop: 'Stop', breaker: 'Breaker', arb_legging: 'Arbitrage legging', test: 'Test', system: 'System' };

// ---------------------------------------------------------------- history (item 37)

function AlertHistory() {
  const [type, setType] = useState('');
  const [items, setItems] = useState(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (t) => {
    setBusy(true);
    setError(null);
    try {
      const rows = await api.getAlerts({ type: t || undefined, limit: PAGE });
      const list = Array.isArray(rows) ? rows : [];
      setItems(list);
      setMore(list.length >= PAGE);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load(type);
  }, [type, load]);

  async function loadMore() {
    if (!items || !items.length) return;
    setBusy(true);
    try {
      const rows = await api.getAlerts({ type: type || undefined, limit: PAGE, before: items[items.length - 1].ts });
      const list = Array.isArray(rows) ? rows : [];
      setItems([...items, ...list]);
      setMore(list.length >= PAGE);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="History"
      actions={
        <button type="button" className="btn small" onClick={() => load(type)} disabled={busy}>
          Refresh
        </button>
      }
    >
      <div className="chips" role="group" aria-label="Filter by alert type">
        {ALERT_TYPES.map((t) => (
          <button key={t.id || 'all'} type="button" className={`chip ${type === t.id ? 'active' : ''}`} aria-pressed={type === t.id} onClick={() => setType(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      <ErrorNotice error={error} onRetry={() => load(type)} />
      {busy && !items && <Loading />}
      {items && !items.length && !error && <p className="muted">No alerts yet.</p>}
      {items && items.length > 0 && (
        <CollapsibleList items={items} pageKey="alerts-history" label="alerts" pinned={(a) => ['error', 'stop', 'breaker', 'arb_legging'].includes(a.type)}>
          {(vis) => (
            <ul className="rows">
              {vis.map((a) => (
                <li key={a.id || `${a.ts}-${a.title}`} className="alert-row" data-type={a.type}>
                  <div className="alert-row-top">
                    <span>
                      <Chip tone={TYPE_TONE[a.type] || 'muted'}>{TYPE_LABEL[a.type] || a.type}</Chip>{' '}
                      <span className="alert-title">{a.title}</span>
                    </span>
                    <Time className="muted small" value={a.ts} withDate />
                  </div>
                  {a.body && <div className="alert-body">{a.body}</div>}
                </li>
              ))}
            </ul>
          )}
        </CollapsibleList>
      )}
      {more && (
        <button type="button" className="btn block" onClick={loadMore} disabled={busy} style={{ marginTop: 10 }}>
          {busy ? 'Loading…' : 'Load More'}
        </button>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- settings (item 38)

export function hhmmLabel(v) {
  const m = /^(\d{2}):(\d{2})$/.exec(String(v || ''));
  if (!m) return '—';
  const h = Number(m[1]);
  return `${h % 12 === 0 ? 12 : h % 12}:${m[2]} ${h < 12 ? 'AM' : 'PM'}`;
}

const MODE_LABELS = { problems: 'Only Problems', every_fill: 'Every Fill' };
const FIELD_LABELS = {
  daily_summary_time: 'Daily Summary Time',
  mode: 'Notify Me About',
  'quiet_hours.enabled': 'Quiet Hours',
  'quiet_hours.start': 'Quiet Hours Start',
  'quiet_hours.end': 'Quiet Hours End',
};
const alertLabel = (k) => FIELD_LABELS[k] || k;
function alertValue(k, v) {
  if (k === 'mode') return MODE_LABELS[v] || String(v);
  if (k === 'quiet_hours.enabled') return v ? 'On' : 'Off';
  if (k.endsWith('time') || k.endsWith('start') || k.endsWith('end')) return hhmmLabel(v);
  return String(v);
}

function flatten(s) {
  const q = (s && s.quiet_hours) || {};
  return {
    daily_summary_time: s && s.daily_summary_time,
    mode: s && s.mode,
    'quiet_hours.enabled': !!q.enabled,
    'quiet_hours.start': q.start,
    'quiet_hours.end': q.end,
  };
}

function AlertSettings() {
  const { data, error, loading, staleAt, reload, setData } = useResource(() => api.getAlertSettings(), '/api/alerts/settings');
  const { twoFactor, notify } = useDialogs();
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  useEffect(() => {
    if (data) setDraft(flatten(data));
  }, [data]);

  const saved = useMemo(() => (data ? flatten(data) : null), [data]);
  const changes = useMemo(
    () => (draft && saved ? Object.keys(draft).filter((k) => draft[k] !== saved[k]).map((k) => ({ key: k, from: saved[k], to: draft[k] })) : []),
    [draft, saved],
  );
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const dirty = changes.length > 0;

  async function save(e) {
    e.preventDefault();
    if (!dirty || saving) return;
    const patch = {};
    for (const c of changes) {
      if (c.key.startsWith('quiet_hours.')) {
        patch.quiet_hours = { ...(patch.quiet_hours || {}), [c.key.slice(12)]: c.to };
      } else patch[c.key] = c.to;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const r = await twoFactor({
        title: 'Save Alert Settings?',
        message: <ChangeDiff changes={changes} format={alertValue} label={alertLabel} />,
        confirmLabel: 'Save',
        action: (code) => api.putAlertSettings(patch, code),
      });
      if (r) {
        const next = r.settings || r;
        setData({ ...(data || {}), ...next });
        notify({ kind: 'ok', title: 'Alert Settings Saved', text: changes.map((c) => `${alertLabel(c.key)} ${alertValue(c.key, c.from)} → ${alertValue(c.key, c.to)}`).join(' · ') });
      }
    } catch (err) {
      setSaveError(err);
    } finally {
      setSaving(false);
    }
  }

  return (
    <FoldCard title="Alert Settings" summary={data ? <span>{MODE_LABELS[data.mode] || data.mode}</span> : null}>
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading />}
      {draft && (
        <form onSubmit={save} className="stack">
          <fieldset className="field">
            <legend>
              <span className="field-label">Notify Me About</span>
            </legend>
            <div className="seg" role="group" aria-label="Notify me about">
              {Object.entries(MODE_LABELS).map(([id, label]) => (
                <button key={id} type="button" className="seg-btn" aria-pressed={draft.mode === id} onClick={() => set('mode', id)}>
                  {label}
                </button>
              ))}
            </div>
            <span className="field-help">Stop and critical alerts are always sent.</span>
          </fieldset>

          <div className="field">
            <label className="field-label" htmlFor="alert-daily-time">
              Daily Summary Time
            </label>
            <input id="alert-daily-time" type="time" value={draft.daily_summary_time || ''} onChange={(e) => set('daily_summary_time', e.target.value)} />
            {data && data.tz && <span className="field-help">Times use {data.tz}.</span>}
          </div>

          <div className="field">
            <label className="ux-check">
              <input type="checkbox" checked={!!draft['quiet_hours.enabled']} onChange={(e) => set('quiet_hours.enabled', e.target.checked)} />
              <span className="field-label">Quiet Hours</span>
            </label>
            <span className="field-help">No everyday alerts during these hours. Stop and critical alerts still come through.</span>
          </div>
          {draft['quiet_hours.enabled'] && (
            <div className="two-col">
              <div className="field">
                <label className="field-label" htmlFor="alert-quiet-start">
                  Start
                </label>
                <input id="alert-quiet-start" type="time" value={draft['quiet_hours.start'] || ''} onChange={(e) => set('quiet_hours.start', e.target.value)} />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="alert-quiet-end">
                  End
                </label>
                <input id="alert-quiet-end" type="time" value={draft['quiet_hours.end'] || ''} onChange={(e) => set('quiet_hours.end', e.target.value)} />
              </div>
            </div>
          )}

          {dirty && (
            <div>
              <p className="chip-label">Changes To Review</p>
              <ChangeDiff changes={changes} format={alertValue} label={alertLabel} />
            </div>
          )}
          {saveError && (
            <div className="notice notice-error" role="alert">
              <strong>Not saved.</strong>
              {saveError.details && saveError.details.length ? (
                <ul className="detail-list">
                  {saveError.details.map((d, i) => (
                    <li key={i}>{d}</li>
                  ))}
                </ul>
              ) : (
                <div>{saveError.message}</div>
              )}
            </div>
          )}
          <button type="submit" className="btn primary block" disabled={!dirty || saving}>
            {saving ? 'Saving…' : <><IconLock /> Save Alert Settings</>}
          </button>
        </form>
      )}
    </FoldCard>
  );
}

export default function Alerts() {
  return (
    <div className="page">
      <div className="page-head">
        <h1>Alerts</h1>
      </div>
      <p className="hint">Everything Prior has sent you, newest first. Choose what gets sent below.</p>
      <AlertSettings />
      <AlertHistory />
    </div>
  );
}

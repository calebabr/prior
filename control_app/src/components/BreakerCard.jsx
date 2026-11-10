import { useMemo, useState } from 'react';
import Card from './Card.jsx';
import { IconLock } from './Icons.jsx';
import { ErrorNotice, Loading } from './Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import { DASH, dateTime, formatMoney, formatPct } from '../utils/format.js';
import { breakerReasonLabel, percentToShare, shareToPercent } from '../utils/breaker.js';
import '../styles/breaker.css';

const SHARES = [
  { key: 'drawdown_frac', label: 'Drawdown limit', help: 'Stop when equity falls this far below its high-water mark.' },
  { key: 'day_loss_frac', label: 'Daily loss limit', help: "Stop when the day's loss (since midnight New York time) reaches this share of the day's starting equity." },
];
const TOGGLES = [
  { key: 'live_enabled', label: 'Protect live mode' },
  { key: 'demo_enabled', label: 'Protect demo mode' },
];
const META = [...SHARES, ...TOGGLES];
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const round = (n) => Math.round(n * 1e4) / 1e4;

function toDraft(settings) {
  const d = {};
  for (const s of SHARES) d[s.key] = shareToPercent(settings && settings[s.key]);
  for (const t of TOGGLES) d[t.key] = !!(settings && settings[t.key]);
  return d;
}

function describe(v) {
  if (typeof v === 'boolean') return v ? 'On' : 'Off';
  return formatPct(v, { sign: false, digits: 1 });
}

/**
 * Loss / drawdown circuit breaker settings (Settings page). GET /api/breaker, PUT /api/breaker with fresh 2FA and only
 * the fields that changed. Shares are shown as percents (0.15 is 15%). The readings block shows the current mode.
 * Stop is never affected by this card.
 */
export default function BreakerCard() {
  const { twoFactor, notify } = useDialogs();
  const { data, error, loading, reload, setData } = useResource(() => api.getBreaker(), '/api/breaker');
  const [draft, setDraft] = useState({});
  const [seen, setSeen] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  const settings = data && data.settings;
  const ceilings = (data && data.ceilings) || {};
  const state = (data && data.state) || null;

  // Reset the draft in the same render that brings new settings.
  if (settings && settings !== seen) {
    setSeen(settings);
    setDraft(toDraft(settings));
  }

  const { patch, errors } = useMemo(() => {
    const p = {};
    const errs = {};
    if (!settings) return { patch: p, errors: errs };
    for (const s of SHARES) {
      const text = draft[s.key];
      if (text === undefined) continue;
      const c = ceilings[s.key] || {};
      const lo = isNum(c.min) ? c.min * 100 : undefined;
      const hi = isNum(c.max) ? c.max * 100 : undefined;
      const n = Number(text);
      if (String(text).trim() === '') errs[s.key] = 'Required';
      else if (!Number.isFinite(n)) errs[s.key] = 'Not a number';
      else if (lo !== undefined && n < lo - 1e-9) errs[s.key] = `Minimum ${round(lo)}%`;
      else if (hi !== undefined && n > hi + 1e-9) errs[s.key] = `Maximum ${round(hi)}%`;
      else {
        const share = percentToShare(text);
        if (share !== settings[s.key]) p[s.key] = share;
      }
    }
    for (const t of TOGGLES) {
      if (t.key in draft && draft[t.key] !== !!settings[t.key]) p[t.key] = draft[t.key];
    }
    return { patch: p, errors: errs };
  }, [draft, settings, ceilings]);

  const dirty = Object.keys(patch).length > 0;
  const hasErrors = Object.keys(errors).length > 0;

  async function save(e) {
    e.preventDefault();
    if (!dirty || hasErrors || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const r = await twoFactor({
        title: 'Save circuit breaker?',
        message: (
          <ul className="change-list">
            {Object.entries(patch).map(([k, v]) => {
              const meta = META.find((m) => m.key === k);
              return (
                <li key={k}>
                  <strong>{meta ? meta.label : k}</strong>: {describe(settings[k])} → {describe(v)}
                </li>
              );
            })}
          </ul>
        ),
        confirmLabel: 'Save',
        action: (code) => api.putBreaker(patch, code),
      });
      if (r) {
        if (r.settings) setData(r);
        else reload();
        notify({ kind: 'ok', title: 'Circuit breaker saved' });
      }
    } catch (err) {
      setSaveError(err);
    } finally {
      setSaving(false);
    }
  }

  const env = state && state.env;
  const reading = state && state.envs && env ? state.envs[env] : null;
  const unreadable = !!state && state.readable === false;

  return (
    <Card title="Circuit Breaker" subtitle="Stops trading and blocks a restart when losses reach a limit you set.">
      {loading && !data && <Loading />}
      <ErrorNotice error={error} onRetry={reload} />
      {data && settings && (
        <form onSubmit={save}>
          {unreadable && (
            <div className="notice notice-error" role="alert" data-testid="breaker-unreadable">
              The breaker state cannot be read right now, so loss checks are being skipped and starting is blocked until it can be read.
            </div>
          )}
          {!unreadable && state && state.warning && (
            <div className="notice notice-stale" role="status" data-testid="breaker-card-warning">
              {String(state.warning)}
            </div>
          )}
          {state && state.tripped && state.trip && (
            <div className="notice notice-error" role="status" data-testid="breaker-card-tripped">
              Tripped: {breakerReasonLabel(state.trip.reason)} at {dateTime(state.trip.utc)}. Start again from Home to clear it.
            </div>
          )}

          {reading && (
            <ul className="breaker-state" data-testid="breaker-state" aria-label={`Current readings, ${env} mode`}>
              <li>
                <span>Readings for</span>
                <span>
                  {String(env).charAt(0).toUpperCase() + String(env).slice(1)} mode{reading.enabled ? '' : ' (protection off)'}
                </span>
              </li>
              <li>
                <span>High-water mark</span>
                <span className="num">{isNum(reading.hwm) ? formatMoney(reading.hwm) : DASH}</span>
              </li>
              <li>
                <span>Drawdown so far</span>
                <span className="num">
                  {isNum(reading.drawdown) ? formatMoney(reading.drawdown) : DASH}
                  {isNum(reading.drawdown_share) ? ` (${formatPct(reading.drawdown_share, { sign: false, digits: 1 })})` : ''}
                </span>
              </li>
              <li>
                <span>Loss today</span>
                <span className="num">
                  {isNum(reading.day_loss) ? formatMoney(reading.day_loss) : DASH}
                  {isNum(reading.day_loss_share) ? ` (${formatPct(reading.day_loss_share, { sign: false, digits: 1 })})` : ''}
                </span>
              </li>
              <li>
                <span>Last reading</span>
                <span>{reading.last_utc ? dateTime(reading.last_utc) : DASH}</span>
              </li>
            </ul>
          )}

          <div className="stack">
            {SHARES.map((s) => {
              const c = ceilings[s.key] || {};
              const id = `breaker-${s.key}`;
              return (
                <div className="field" key={s.key}>
                  <span className="field-label">
                    <label htmlFor={id}>
                      {s.label} <span className="unit">(% of equity)</span>
                    </label>
                  </span>
                  <input
                    id={id}
                    type="number"
                    inputMode="decimal"
                    step="any"
                    value={draft[s.key] ?? ''}
                    disabled={saving}
                    onChange={(ev) => {
                      setDraft((d) => ({ ...d, [s.key]: ev.target.value }));
                      setSaveError(null);
                    }}
                    aria-invalid={!!errors[s.key]}
                  />
                  {(isNum(c.min) || isNum(c.max)) && (
                    <span className="field-range">
                      Range {isNum(c.min) ? round(c.min * 100) : '…'}% – {isNum(c.max) ? round(c.max * 100) : '…'}%
                    </span>
                  )}
                  <span className="field-help">{s.help}</span>
                  {errors[s.key] && <span className="error-text">{errors[s.key]}</span>}
                </div>
              );
            })}
            {TOGGLES.map((t) => (
              <div className="field" key={t.key}>
                <label className="ux-check">
                  <input
                    type="checkbox"
                    checked={!!draft[t.key]}
                    disabled={saving}
                    onChange={(ev) => setDraft((d) => ({ ...d, [t.key]: ev.target.checked }))}
                  />
                  <span className="field-label">{t.label}</span>
                </label>
              </div>
            ))}
          </div>

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
                <div>{saveError.status === 503 ? 'The settings store is unavailable. Nothing was changed.' : saveError.message}</div>
              )}
            </div>
          )}

          <button type="submit" className="btn block" disabled={!dirty || hasErrors || saving}>
            {saving ? 'Saving…' : <><IconLock /> Save Circuit Breaker</>}
          </button>
          {settings.updated && (
            <p className="hint">
              Last changed {dateTime(settings.updated)}
              {settings.by ? ` by ${settings.by}` : ''}.
            </p>
          )}
        </form>
      )}
    </Card>
  );
}

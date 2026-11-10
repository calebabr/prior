import { useCallback, useEffect, useMemo, useState } from 'react';
import { IconLock } from '../components/Icons.jsx';
import Card from '../components/Card.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import ChangeDiff, { formatSettingValue, plainText, settingLabel } from '../components/ChangeDiff.jsx';
import DisplayCard from '../components/DisplayCard.jsx';
import ShadowCapitalCard from '../components/ShadowCapitalCard.jsx';
import BreakerCard from '../components/BreakerCard.jsx';
import PushCard from '../components/PushCard.jsx';
import PushDevicesCard from '../components/PushDevicesCard.jsx';
import { humanize, titleCase } from '../utils/format.js';
import { settingsToastTitle } from '../utils/overview.js';
import '../styles/settings.css';

// Settings keys that have a glossary entry (shown as a (?) after the label).
const FIELD_TERMS = { run_budget: 'budget', max_order: 'budget', max_open_frac: 'open-fraction', series: 'series', cells: 'cell' };

// Form generated from GET /api/config `schema` ({field: {type, unit, min, max, help, editable}}).
// `cells` (the validated strategy) is always read-only and never sent.

export const MAX_POLL_MINUTES = 14;
const READ_ONLY = ['cells'];
// Series options come from the API's config schema (the installed strategy); none are built into the app.
const DEFAULT_SERIES = [];

// A nested object (or a list of objects) has no single text box: it is shown read-only, never edited as text.
const isObj = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
export function isComplex(v) {
  return isObj(v) || (Array.isArray(v) && v.some(isObj));
}

function kindOf(spec, value) {
  const t = String((spec && spec.type) || '').toLowerCase();
  if (['int', 'integer'].includes(t)) return 'int';
  if (['number', 'float', 'decimal'].includes(t)) return 'number';
  if (['bool', 'boolean'].includes(t)) return 'bool';
  if (['list', 'array', 'list[str]', 'series', 'multiselect'].includes(t)) return 'list';
  if (['table', 'cells'].includes(t)) return 'table';
  if (Array.isArray(value)) return 'list';
  if (typeof value === 'boolean') return 'bool';
  if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'number';
  return 'text';
}

function effectiveMax(key, spec, ceilings) {
  const vals = [];
  if (spec && typeof spec.max === 'number') vals.push(spec.max);
  if (ceilings && typeof ceilings[key] === 'number') vals.push(ceilings[key]);
  if (key === 'poll_minutes') vals.push(MAX_POLL_MINUTES);
  return vals.length ? Math.min(...vals) : undefined;
}

function toDraft(config) {
  const d = {};
  for (const [k, v] of Object.entries(config || {})) {
    if (k.startsWith('_') || READ_ONLY.includes(k)) continue;
    d[k] = Array.isArray(v) ? [...v] : typeof v === 'number' ? String(v) : v;
  }
  return d;
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The settings form (extracted so Maker Controls reuses it). `resource` is the useResource result for
 * api.getConfig(); `filter(key)` limits which keys are editable here (default: all), so the Maker Controls view
 * shows only the Maker keys while Settings shows every key. One PUT /api/config, always with a fresh 2FA code.
 * `readOnlyCard` shows the read-only Strategy card (cells); `compact` drops the title copy for embedded use.
 */
export function ConfigEditor({ resource, filter = null, title = 'Bot Settings', readOnlyCard = true, undoCard = true }) {
  const { data, reload, setData } = resource;
  const { twoFactor, notify } = useDialogs();
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [savedVersion, setSavedVersion] = useState(null);
  const [undo, setUndo] = useState(null); // {version, changes:[{key, from, to}]} for the last saved change, or null
  const [undoing, setUndoing] = useState(false);
  // True from a successful save until the form and change history have been re-read: edits typed in that
  // window would be wiped by the reload, so the fields are locked (Z1).
  const [syncing, setSyncing] = useState(false);

  // Item 40: what "Undo Last Change" would revert. A missing endpoint or an error just hides the card.
  const loadUndo = useCallback(async () => {
    try {
      const r = await api.getConfigVersions(5);
      const u = r && r.undo && r.undo.undoable !== false && Array.isArray(r.undo.changes) && r.undo.changes.length ? r.undo : null;
      setUndo(u);
    } catch {
      setUndo(null);
    }
  }, []);
  useEffect(() => {
    loadUndo();
  }, [loadUndo]);

  const config = (data && data.config) || null;
  const schema = (data && data.schema) || {};
  const ceilings = (data && data.ceilings) || {};

  // Reset the draft in the same render that brings new config (not in an effect), so the form is never
  // unlocked for a moment with the old draft still in it (Z1).
  const [seenConfig, setSeenConfig] = useState(null);
  if (config !== seenConfig) {
    setSeenConfig(config);
    if (config) setDraft(toDraft(config));
  }

  // Field order: schema order, then any other config keys.
  const fields = useMemo(() => {
    const keys = Object.keys(schema);
    for (const k of Object.keys(config || {})) if (!k.startsWith('_') && !keys.includes(k)) keys.push(k);
    return keys;
  }, [schema, config]);

  const inScope = (k) => READ_ONLY.includes(k) || !filter || filter(k);
  const editable = (k) => inScope(k) && !READ_ONLY.includes(k) && !(schema[k] && schema[k].editable === false) && !(config && isComplex(config[k]));

  // Parse + validate the draft -> { patch, errors }
  const { patch, errors } = useMemo(() => {
    const p = {};
    const errs = {};
    if (!config) return { patch: p, errors: errs };
    for (const k of fields) {
      if (!editable(k) || !(k in draft)) continue;
      const spec = schema[k] || {};
      const kind = kindOf(spec, config[k]);
      let v = draft[k];
      if (kind === 'int' || kind === 'number') {
        if (v === '' || v == null) {
          errs[k] = 'Required';
          continue;
        }
        v = Number(v);
        if (!Number.isFinite(v)) errs[k] = 'Not a number';
        else if (kind === 'int' && !Number.isInteger(v)) errs[k] = 'Whole number only';
        else if (typeof spec.min === 'number' && v < spec.min) errs[k] = `Minimum ${spec.min}`;
        else {
          const max = effectiveMax(k, spec, ceilings);
          if (typeof max === 'number' && v > max) errs[k] = `Maximum ${max}`;
        }
      } else if (kind === 'list') {
        if (!Array.isArray(v) || v.length === 0) errs[k] = 'Pick at least one';
      }
      if (!errs[k] && !same(v, config[k])) p[k] = v;
    }
    return { patch: p, errors: errs };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, config, schema, ceilings, fields]);

  const dirty = Object.keys(patch).length > 0;
  const changes = useMemo(() => Object.entries(patch).map(([key, to]) => ({ key, from: config ? config[key] : undefined, to })), [patch, config]);
  const hasErrors = Object.keys(errors).length > 0;

  async function save(e) {
    e.preventDefault();
    if (!dirty || hasErrors || saving) return;
    setSaving(true);
    setSaveError(null);
    setSavedVersion(null);
    try {
      const r = await twoFactor({
        title: 'Save settings?',
        message: <ChangeDiff changes={changes} />,
        confirmLabel: 'Save',
        action: async (code) => {
          const res = await api.putConfig(patch, code);
          setSyncing(true); // lock the form the moment the save lands, before the prompt closes
          return res;
        },
      });
      if (r) {
        setData({ ...data, config: r.config || { ...config, ...patch } });
        setSavedVersion(r.version ?? null);
        const fallback = changes.map((c) => `${settingLabel(c.key)} ${formatSettingValue(c.key, c.from)} → ${formatSettingValue(c.key, c.to)}`).join(' · ');
        notify({ kind: 'ok', title: settingsToastTitle(r) || (fallback ? `Settings Saved · ${fallback}` : 'Settings Saved') });
        try {
          await Promise.all([reload(), loadUndo()]);
        } finally {
          setSyncing(false);
        }
      }
    } catch (err) {
      setSaveError(err);
      setSyncing(false);
    } finally {
      setSaving(false);
    }
  }

  async function undoLast() {
    if (!undo || undoing) return;
    setUndoing(true);
    setSaveError(null);
    try {
      // Showing the reverse of the last change: current value -> value it goes back to.
      const reverse = undo.changes.map((c) => ({ key: c.key, from: c.to, to: c.from }));
      const r = await twoFactor({
        title: 'Undo Last Change?',
        message: <ChangeDiff changes={reverse} />,
        confirmLabel: 'Undo',
        action: (code) => api.undoConfig(undo.version, code),
      });
      if (r) {
        notify({ kind: 'ok', title: 'Change Undone', text: reverse.map((c) => `${settingLabel(c.key)} → ${formatSettingValue(c.key, c.to)}`).join(' · ') });
        setSavedVersion(null);
        await reload();
        loadUndo();
      }
    } catch (err) {
      const stale = err && err.status === 409;
      notify({
        kind: 'error',
        title: 'Not Undone',
        text: stale ? 'The settings changed again. Reload and review the latest change.' : err.message,
        details: err.details,
      });
      if (stale) loadUndo();
    } finally {
      setUndoing(false);
    }
  }

  function setField(k, v) {
    setDraft((d) => ({ ...d, [k]: v }));
    setSavedVersion(null);
  }

  function renderField(k) {
    const spec = schema[k] || {};
    const value = config[k];
    const kind = kindOf(spec, value);
    const label = (
      <span className="field-label">
        {titleCase(spec.label || humanize(k))}
        {spec.unit && <span className="unit"> ({spec.unit})</span>}
        {FIELD_TERMS[k] && <HelpIcon term={FIELD_TERMS[k]} />}
      </span>
    );
    const help = spec.help && <span className="field-help">{spec.help}</span>;

    if (!editable(k)) {
      return (
        <div className="field" key={k}>
          {label}
          <span className="pill pill-muted readonly-tag">Read-Only</span>
          {kind === 'table' || (Array.isArray(value) && Array.isArray(value[0])) ? (
            <div className="table-wrap">
              <table className="table mono small">
                <tbody>
                  {(value || []).map((row, i) => (
                    <tr key={i}>
                      {(Array.isArray(row) ? row : [row]).map((c, j) => (
                        <td key={j}>{plainText(c)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="readonly-value">{plainText(value)}</div>
          )}
          {help}
        </div>
      );
    }

    if (kind === 'list') {
      const options = spec.options || spec.choices || spec.enum || Array.from(new Set([...DEFAULT_SERIES, ...(value || [])]));
      const cur = draft[k] || [];
      return (
        <fieldset className="field" key={k}>
          <legend>{label}</legend>
          <div className="checks">
            {options.map((o) => (
              <label key={o} className="ux-check">
                <input
                  type="checkbox"
                  checked={cur.includes(o)}
                  onChange={(e) =>
                    setField(k, e.target.checked ? options.filter((x) => x === o || cur.includes(x)) : cur.filter((x) => x !== o))
                  }
                />
                <span className="mono">{o}</span>
              </label>
            ))}
          </div>
          {help}
          {errors[k] && <span className="error-text">{errors[k]}</span>}
        </fieldset>
      );
    }

    if (kind === 'bool') {
      return (
        <div className="field" key={k}>
          <label className="ux-check">
            <input type="checkbox" checked={!!draft[k]} onChange={(e) => setField(k, e.target.checked)} />
            {label}
          </label>
          {help}
        </div>
      );
    }

    const numeric = kind === 'int' || kind === 'number';
    const max = numeric ? effectiveMax(k, spec, ceilings) : undefined;
    // The (?) is a button, so it must sit outside the <label>: a label's control is its first labelable child.
    const inputId = `cfg-${k}`;
    return (
      <div className="field" key={k}>
        <span className="field-label">
          <label htmlFor={inputId}>
            {titleCase(spec.label || humanize(k))}
            {spec.unit && <span className="unit"> ({spec.unit})</span>}
          </label>
          {FIELD_TERMS[k] && <HelpIcon term={FIELD_TERMS[k]} />}
        </span>
        <input
          id={inputId}
          type={numeric ? 'number' : 'text'}
          inputMode={kind === 'int' ? 'numeric' : numeric ? 'decimal' : undefined}
          min={numeric ? spec.min : undefined}
          max={max}
          step={kind === 'int' ? 1 : numeric ? spec.step || 'any' : undefined}
          value={draft[k] ?? ''}
          onChange={(e) => setField(k, e.target.value)}
          aria-invalid={!!errors[k]}
        />
        {numeric && (spec.min != null || max != null) && (
          <span className="field-range">
            Range {spec.min ?? '…'} – {max ?? '…'}
            {ceilings[k] != null || k === 'poll_minutes' ? ' (cloud ceiling)' : ''}
          </span>
        )}
        {help}
        {errors[k] && <span className="error-text">{errors[k]}</span>}
      </div>
    );
  }

  if (!config) return null;
  const roKeys = fields.filter((k) => inScope(k) && !editable(k) && k in config);
  return (
    <form onSubmit={save}>
      <Card title={title} subtitle="Changes apply from the next pass.">
        <fieldset className="sync-lock" disabled={syncing} aria-busy={syncing} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <div className="stack">{fields.filter((k) => editable(k) && k in config).map(renderField)}</div>
        </fieldset>
        {syncing && <p className="hint" role="status">Refreshing settings…</p>}
      </Card>

      {dirty && (
        <Card title="Changes To Review" subtitle="Nothing is saved until you confirm with your 2FA code.">
          <ChangeDiff changes={changes} />
        </Card>
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
      {savedVersion != null && <div className="notice notice-ok">Saved as version {savedVersion}.</div>}

      <div className="sticky-actions">
        <button type="button" className="btn" disabled={!dirty || saving} onClick={() => setDraft(toDraft(config))}>
          Discard Edits
        </button>
        <button type="submit" className="btn primary" disabled={!dirty || hasErrors || saving || syncing}>
          {saving ? 'Saving…' : <><IconLock /> Save</>}
        </button>
      </div>

      {undoCard && undo && !dirty && (
        <Card title="Last Change" subtitle={`Version ${undo.version}`}>
          <ChangeDiff changes={undo.changes} />
          <button type="button" className="btn block" onClick={undoLast} disabled={undoing} style={{ marginTop: 10 }}>
            {undoing ? 'Undoing…' : <><IconLock /> Undo Last Change</>}
          </button>
        </Card>
      )}

      {readOnlyCard && roKeys.length > 0 && (
        <Card title={<>Strategy (Read-Only)<HelpIcon term="cell" /></>} subtitle="The validated strategy cannot be changed from the app.">
          <div className="stack">{roKeys.map(renderField)}</div>
        </Card>
      )}
    </form>
  );
}

export default function Settings() {
  const resource = useResource(() => api.getConfig(), '/api/config');
  const { data, error, loading, staleAt, reload } = resource;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Settings</h1>
        <button type="button" className="btn small" onClick={reload} disabled={loading}>
          Reload
        </button>
      </div>
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading />}

      {data && data.config && <ConfigEditor resource={resource} />}

      {data && data.shadow_capital && <ShadowCapitalCard info={data.shadow_capital} onSaved={reload} />}

      <BreakerCard />

      <DisplayCard />
      <PushCard />
      <PushDevicesCard />
    </div>
  );
}

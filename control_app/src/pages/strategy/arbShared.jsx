import { useEffect, useMemo, useState } from 'react';
import { useStatus } from '../../context/StatusContext.jsx';
import { STOPPED_EVENT } from '../../components/StopButton.jsx';
import { useDialogs } from '../../context/DialogContext.jsx';
import { useResource } from '../../hooks/useResource.js';
import { usePageVisible } from '../../hooks/usePageVisible.js';
import { api } from '../../services/api.js';
import { humanize } from '../../utils/format.js';
import { BREAKER_ACK_LABEL, breaker409, isTripped } from '../../utils/breaker.js';

// Bracket arbitrage (DASHBOARD_PLAN.md "Strategies run by the app").
//   GET  /api/arb               -> {settings, schema?, ceilings?, status, pnl, expectation?}
//   PUT  /api/arb  (2FA)        -> same shape; body: any settings field, plus confirm:"LIVE", ack_legging
//   GET  /api/arb/opportunities -> {items:[{ts, event, kind, contracts, profit, cost, sum_best, legs, executed, ...}]}
// Log-only by default. Live needs its own switch (typed LIVE + 2FA) plus server env gates.

export const POLL_MS = 30000;
export const OPP_LIMIT = 50;
// Series options come from the API's arb schema (the installed scanner); no ticker list is built into the app.
export const ARB_SERIES = [];
const FIELDS = [
  'arb_max_contracts', 'arb_min_profit', 'arb_poll_seconds', 'arb_series',
  'arb_scan_cap', 'arb_day_cap', 'arb_series_cap_frac', 'arb_demo_execute',
  'arb_set_loss_frac', 'arb_scan_cap_frac', 'arb_day_cap_frac', 'max_event_frac',
];
const DEFAULT_SCHEMA = {
  arb_max_contracts: {
    type: 'number', unit: 'contracts per leg', min: 1, max: 100,
    help: 'Largest set the scanner will size an opportunity to. Each leg is a taker order.',
  },
  arb_min_profit: {
    type: 'number', unit: 'USD per set, after fees', min: 0.01, max: 10, step: 0.01,
    help: 'Ignore opportunities that clear less than this after taker fees.',
  },
  arb_poll_seconds: {
    type: 'integer', unit: 'seconds', min: 5, max: 60,
    help: 'Scan interval. Faster catches more short windows but uses more of the read rate limit and the 0.1 CPU.',
  },
  arb_series: {
    type: 'list', unit: 'series tickers',
    help: 'Bracket events to scan. Each event has mutually exclusive brackets.',
  },
  arb_scan_cap: {
    type: 'number', unit: 'USD per scan', min: 1, max: 100,
    help: 'Most money one scan may spend on sets in demo-execute. Sets over this are skipped.',
  },
  arb_day_cap: {
    type: 'number', unit: 'USD per day', min: 1, max: 500,
    help: 'Most money arbitrage may spend per UTC day in demo-execute.',
  },
  arb_series_cap_frac: {
    type: 'number', unit: 'fraction of the open-exposure ceiling', min: 0.05, max: 0.9, step: 0.01,
    help: 'Most one series may hold in open exposure, as a share of the account ceiling.',
  },
  arb_set_loss_frac: {
    type: 'number', nullable: true, unit: 'share of equity', min: 0.001, max: 0.5, step: 0.001,
    help: 'Most one partial set may lose before containment gives up and trips the kill switch. Empty = off.',
  },
  arb_scan_cap_frac: {
    type: 'number', nullable: true, unit: 'share of equity per scan', min: 0.001, max: 1, step: 0.001,
    help: 'Percentage cap per scan; the dollar scan cap stays the maximum. Empty = off.',
  },
  arb_day_cap_frac: {
    type: 'number', nullable: true, unit: 'share of equity per day', min: 0.001, max: 1, step: 0.001,
    help: 'Percentage cap per UTC day; the dollar day cap stays the maximum. Empty = off.',
  },
  max_event_frac: {
    type: 'number', nullable: true, unit: 'share of equity per event', min: 0.001, max: 1, step: 0.001,
    help: 'Most one event may hold, maker and arbitrage together. Empty = off.',
  },
  arb_demo_execute: {
    type: 'bool', label: 'Demo Execute',
    help: 'Place the sets it finds as real orders on the Kalshi demo account (play money). Never on live.',
  },
};

const STATES = [
  { key: 'stopped', label: 'Off' },
  { key: 'log-only', label: 'Log only' },
  { key: 'live', label: 'Live' },
];

function arbState(settings, status) {
  if (status && status.state) return String(status.state);
  if (!settings || !settings.enabled) return 'stopped';
  return settings.live ? 'live' : 'log-only';
}

export function ArbStateText({ state }) {
  const s = STATES.find((x) => x.key === state);
  return <span className={`arb-state arb-state-${state || 'unknown'}`}>{s ? s.label : state || 'Unknown'}</span>;
}

// Three-position readout, like a selector switch on an instrument panel.
export function StateTrack({ state }) {
  return (
    <div className="arb-track" role="img" aria-label={`Arbitrage is ${(STATES.find((s) => s.key === state) || {}).label || state}`}>
      {STATES.map((s) => (
        <div key={s.key} className={`arb-pos arb-pos-${s.key} ${state === s.key ? 'on' : ''}`}>
          <span className="arb-pos-mark" aria-hidden="true" />
          {s.label}
        </div>
      ))}
    </div>
  );
}

function kindOf(spec) {
  const t = String((spec && spec.type) || '').toLowerCase();
  if (['int', 'integer'].includes(t)) return 'int';
  if (['list', 'array', 'multiselect'].includes(t)) return 'list';
  if (['bool', 'boolean'].includes(t)) return 'bool';
  return 'number';
}

function toDraft(settings) {
  const d = {};
  for (const k of FIELDS) {
    if (!(k in (settings || {}))) continue;
    const v = settings[k];
    d[k] = Array.isArray(v) ? [...v] : typeof v === 'boolean' ? v : v == null ? '' : String(v);
  }
  return d;
}

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const showVal = (v) => (Array.isArray(v) ? v.join(', ') : typeof v === 'boolean' ? (v ? 'On' : 'Off') : String(v));

/** "skipped: gate total exposure over the limit" -> the plain reason, or null when this is not a gate skip. */
export function gateReason(executed) {
  const m = /^skipped:\s*gate\s*(.*)$/i.exec(String(executed || ''));
  return m ? m[1].trim() || 'risk limit' : null;
}

export function executedPill(executed) {
  const e = String(executed || 'log-only');
  if (e.startsWith('FILLED')) return { cls: 'pill-ok', text: 'Filled' };
  if (e.startsWith('PARTIAL')) return { cls: 'pill-danger', text: 'Partial fill' };
  if (e === 'log-only') return { cls: 'pill-muted', text: 'Logged only' };
  if (gateReason(e) !== null) return { cls: 'pill-warn', text: 'Skipped by risk limit' };
  if (e.startsWith('ERROR')) return { cls: 'pill-danger', text: 'Error' };
  return { cls: 'pill-muted', text: e };
}

/** GET /api/arb/opportunities with the same poll and reload-after-Stop the old page had. */
export function useArbOpportunities(limit = OPP_LIMIT) {
  const visible = usePageVisible();
  const opps = useResource(() => api.getArbOpportunities(limit), `/api/arb/opportunities?limit=${limit}`);
  const reloadOpps = opps.reload;
  useEffect(() => {
    if (!visible) return undefined;
    const id = setInterval(reloadOpps, POLL_MS);
    return () => clearInterval(id);
  }, [visible, reloadOpps]);
  useEffect(() => {
    window.addEventListener(STOPPED_EVENT, reloadOpps);
    return () => window.removeEventListener(STOPPED_EVENT, reloadOpps);
  }, [reloadOpps]);
  return opps;
}

/**
 * THE one arbitrage controller: GET /api/arb, its poll, every confirm / fresh-2FA path (turn on or off, go
 * live with typed LIVE, save settings) and the settings draft and validation. Overview, Results and Controls
 * all call this, so there is a single set of PUT payloads and rules. Extracted from the old Arbitrage page;
 * no logic changed.
 */
export function useArbController() {
  const visible = usePageVisible();
  const arb = useResource(() => api.getArb(), '/api/arb');
  const { confirm, twoFactor, notify } = useDialogs();
  const { status: appStatus } = useStatus();
  const [busy, setBusy] = useState(null);
  const [draft, setDraft] = useState({});
  const [saveError, setSaveError] = useState(null);

  const { data, setData } = arb;
  const reloadArb = arb.reload;
  const reloadAll = reloadArb;

  // Poll while visible; reload right after a Stop from anywhere in the app.
  useEffect(() => {
    if (!visible) return undefined;
    const id = setInterval(reloadAll, POLL_MS);
    return () => clearInterval(id);
  }, [visible, reloadAll]);
  useEffect(() => {
    window.addEventListener(STOPPED_EVENT, reloadAll);
    return () => window.removeEventListener(STOPPED_EVENT, reloadAll);
  }, [reloadAll]);

  const settings = (data && data.settings) || null;
  const st = (data && data.status) || {};
  const schema = { ...DEFAULT_SCHEMA, ...((data && data.schema) || {}) };
  const ceilings = (data && data.ceilings) || {};
  const arbSummary = (data && data.arb_summary) || null;
  const sinceRestart = (arbSummary && arbSummary.since_restart) || {};
  const restartUtc = sinceRestart.since_utc || st.since_restart_utc || null;
  const state = arbState(settings, data && data.status);
  const enabled = (settings && 'enabled' in settings ? !!settings.enabled : state !== 'stopped') && !st.kill_switch;
  const live = state === 'live';
  const liveAllowed = !!st.live_allowed;
  const legging = st.kill_reason === 'legging';

  useEffect(() => {
    if (settings) setDraft(toDraft(settings));
  }, [settings]);

  const fields = settings ? FIELDS.filter((k) => k in settings) : [];

  const { patch, errors } = useMemo(() => {
    const p = {};
    const errs = {};
    if (!settings) return { patch: p, errors: errs };
    for (const k of fields) {
      const spec = schema[k] || {};
      const kind = kindOf(spec);
      let v = draft[k];
      if (kind === 'bool') {
        v = !!v;
      } else if (kind === 'list') {
        if (!Array.isArray(v) || !v.length) errs[k] = 'Pick at least one';
      } else {
        if (v === '' || v == null) {
          if (spec.nullable) {
            if (settings[k] != null) p[k] = null;
          } else {
            errs[k] = 'Required';
          }
          continue;
        }
        v = Number(v);
        const max = [spec.max, ceilings[k]].filter((x) => typeof x === 'number');
        const hi = max.length ? Math.min(...max) : undefined;
        if (!Number.isFinite(v)) errs[k] = 'Not a number';
        else if (kind === 'int' && !Number.isInteger(v)) errs[k] = 'Whole number only';
        else if (typeof spec.min === 'number' && v < spec.min) errs[k] = `Minimum ${spec.min}`;
        else if (typeof hi === 'number' && v > hi) errs[k] = `Maximum ${hi}`;
      }
      if (!errs[k] && JSON.stringify(v) !== JSON.stringify(settings[k])) p[k] = v;
    }
    return { patch: p, errors: errs };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, settings, data]);

  const dirty = Object.keys(patch).length > 0;
  const hasErrors = Object.keys(errors).length > 0;

  function applyResult(r) {
    if (r && (r.settings || r.status)) setData({ ...(data || {}), ...r });
    else reloadArb();
  }

  function errorToast(err, fallbackTitle) {
    if (err.status === 409 && String(err.detail).startsWith('live_not_allowed')) {
      notify({
        kind: 'error',
        title: 'Live arbitrage is not allowed by the server',
        text: 'The Render environment needs the prod key and KALSHI_LIVE_CONFIRM. The app cannot enable real money on its own.',
      });
    } else if (breaker409(err)) {
      notify({ kind: 'error', title: 'Circuit breaker is tripped', text: breaker409(err) });
    } else if (err.status === 409 && String(err.detail).startsWith('arb_legging_kill')) {
      notify({
        kind: 'error',
        title: 'Legging kill switch is set',
        text: 'A partial fill tripped the arbitrage kill switch. Check Kalshi for the unmatched leg, then turn arbitrage on again and acknowledge it.',
      });
    } else {
      notify({ kind: 'error', title: fallbackTitle, text: err.message, details: err.details });
    }
  }

  async function setEnabled(on) {
    if (busy) return;
    const body = { enabled: on };
    if (on && isTripped(appStatus)) {
      const ok = await confirm({
        title: 'Turn arbitrage on after the breaker tripped?',
        message: (
          <>
            <p>The loss circuit breaker stopped trading. Turning arbitrage on clears the trip.</p>
            <p className="hint">Stop still works at any time. You will need a 2FA code next.</p>
          </>
        ),
        confirmLabel: 'Continue',
        requireCheck: BREAKER_ACK_LABEL,
        danger: true,
      });
      if (!ok) return;
      body.ack_breaker = true;
    }
    if (on && legging) {
      const ok = await confirm({
        title: 'Clear the legging kill switch?',
        message: (
          <>
            <p>A partial fill left one or more legs unmatched and stopped arbitrage.</p>
            <p className="hint">Check your Kalshi positions before turning it back on.</p>
          </>
        ),
        confirmLabel: 'I have checked',
        danger: true,
      });
      if (!ok) return;
      body.ack_legging = true;
    }
    setBusy('enabled');
    try {
      const r = await twoFactor({
        title: on ? 'Turn arbitrage on?' : 'Turn arbitrage off?',
        message: on
          ? live
            ? 'The scanner starts and trades LIVE when it finds a set.'
            : 'The scanner starts in log-only mode: it records opportunities and places no orders.'
          : 'The scanner stops. Stop (no code needed) also turns it off.',
        confirmLabel: on ? 'Turn on' : 'Turn off',
        danger: on && live,
        action: (code) => api.putArb(body, code),
      });
      if (r) {
        applyResult(r);
        notify({ kind: 'ok', title: on ? 'Arbitrage is on' : 'Arbitrage is off' });
      }
    } catch (err) {
      errorToast(err, 'Arbitrage not changed');
    } finally {
      setBusy(null);
    }
  }

  async function setLive(on) {
    if (busy) return;
    if (on) {
      const ok = await confirm({
        title: 'Arbitrage LIVE (real money)?',
        message: (
          <>
            <p>The scanner will send taker orders for every leg of a set it finds, with real money.</p>
            <p>A partial fill leaves an unhedged leg; it trips the arbitrage kill switch and sends an alert.</p>
            <p className="hint">Independent of the maker&apos;s mode. You will need a code next.</p>
          </>
        ),
        confirmLabel: 'Continue',
        danger: true,
        requireText: 'LIVE',
      });
      if (!ok) return;
    }
    setBusy('live');
    try {
      const r = await twoFactor({
        title: on ? 'Confirm arbitrage LIVE' : 'Back to log-only?',
        message: on ? 'Final Step: Enter your code to trade arbitrage live.' : 'Opportunities will be recorded only. No orders.',
        confirmLabel: on ? 'Go LIVE' : 'Log only',
        danger: on,
        action: (code) => api.putArb(on ? { live: true, confirm: 'LIVE' } : { live: false }, code),
      });
      if (r) {
        applyResult(r);
        notify({ kind: 'ok', title: on ? 'Arbitrage is LIVE' : 'Arbitrage is log-only' });
      }
    } catch (err) {
      errorToast(err, 'Arbitrage mode not changed');
    } finally {
      setBusy(null);
    }
  }

  async function save(e) {
    e.preventDefault();
    if (!dirty || hasErrors || busy) return;
    setBusy('save');
    setSaveError(null);
    try {
      const r = await twoFactor({
        title: 'Save arbitrage settings?',
        message: (
          <ul className="change-list">
            {Object.entries(patch).map(([k, v]) => (
              <li key={k}>
                <strong>{humanize(k.replace(/^arb_/, ''))}</strong>:{' '}
                {showVal(settings[k])} → {showVal(v)}
              </li>
            ))}
          </ul>
        ),
        confirmLabel: 'Save',
        action: (code) => api.putArb(patch, code),
      });
      if (r) {
        applyResult(r);
        notify({ kind: 'ok', title: 'Arbitrage settings saved' });
      }
    } catch (err) {
      setSaveError(err);
    } finally {
      setBusy(null);
    }
  }

  const setField = (k, v) => setDraft((d) => ({ ...d, [k]: v }));

  function renderField(k) {
    const spec = schema[k] || {};
    const kind = kindOf(spec);
    const label = (
      <span className="field-label">
        {spec.label || humanize(k.replace(/^arb_/, ''))}
        {spec.unit && <span className="unit"> ({spec.unit})</span>}
      </span>
    );
    const help = spec.help && <span className="field-help">{spec.help}</span>;
    if (kind === 'list') {
      const options = spec.options && spec.options.length ? spec.options : Array.from(new Set([...ARB_SERIES, ...(settings[k] || [])]));
      const cur = draft[k] || [];
      return (
        <fieldset className="field" key={k}>
          <legend>{label}</legend>
          <div className="checks">
            {options.map((o) => (
              <label key={o} className="check">
                <input
                  type="checkbox"
                  checked={cur.includes(o)}
                  onChange={(ev) =>
                    setField(k, ev.target.checked ? options.filter((x) => x === o || cur.includes(x)) : cur.filter((x) => x !== o))
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
          <label className="check">
            <input type="checkbox" checked={!!draft[k]} onChange={(ev) => setField(k, ev.target.checked)} />
            {label}
          </label>
          {help}
        </div>
      );
    }
    const hiList = [spec.max, ceilings[k]].filter((x) => typeof x === 'number');
    const max = hiList.length ? Math.min(...hiList) : undefined;
    return (
      <label className="field" key={k}>
        {label}
        <input
          type="number"
          inputMode={kind === 'int' ? 'numeric' : 'decimal'}
          min={spec.min}
          max={max}
          step={kind === 'int' ? 1 : spec.step || 'any'}
          value={draft[k] ?? ''}
          onChange={(ev) => setField(k, ev.target.value)}
          aria-invalid={!!errors[k]}
          aria-label={spec.label || humanize(k.replace(/^arb_/, ''))}
        />
        {(spec.min != null || max != null) && (
          <span className="field-range">
            Range {spec.min ?? '…'} – {max ?? '…'}
          </span>
        )}
        {help}
        {errors[k] && <span className="error-text">{errors[k]}</span>}
      </label>
    );
  }

  const expectation =
    (data && data.expectation) ||
    'Bracket arbitrage is rare and small at a small balance. Opportunities last seconds, so a scan catches only some of them. The maker strategy is the main earner.';

  return {
    arb, data, appStatus, settings, st, state, enabled, live, liveAllowed, legging, arbSummary, sinceRestart, restartUtc,
    busy, setEnabled, setLive, save, saveError, dirty, hasErrors, draft, setDraft, fields, renderField, expectation,
    reload: reloadAll, resetDraft: () => setDraft(toDraft(settings)),
  };
}

import { useCallback, useMemo, useState } from 'react';
import Card from '../components/Card.jsx';
import Chip from '../components/Chip.jsx';
import CheckStatusIcon from '../components/CheckStatusIcon.jsx';
import CollapsibleList from '../components/CollapsibleList.jsx';
import Time from '../components/Time.jsx';
import Empty from '../components/Empty.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import '../styles/arming.css';

const STRATEGIES = [
  { id: 'maker', name: 'Maker' },
  { id: 'arbitrage', name: 'Arbitrage' },
];
const GROUPS = [
  { status: 'fail', title: 'Needs fixing', tone: 'danger' },
  { status: 'warn', title: 'Worth a look', tone: 'warn' },
  { status: 'pass', title: 'All good', tone: 'ok' },
];

/** Why arming is not possible right now, or null. Disarming is never blocked by these. */
export function armBlockReason({ liveAllowed, ready, hasPreflight }) {
  if (!liveAllowed) return 'The server does not allow live trading.';
  if (!hasPreflight) return 'Run the checks first.';
  if (!ready) return 'Not ready: fix the items under "Needs fixing" first.';
  return null;
}

export default function Arming() {
  const { status } = useStatus();
  const { confirm, twoFactor, notify } = useDialogs();
  const demo = !(status && status.mode === 'live');

  // Arming only gates live trading, so the checks are always the live ones (also while the bot is in demo).
  const pre = useResource(() => api.getPreflight('live'), '/api/preflight?target=live');
  const arm = useResource(() => api.getArming(), '/api/arming');
  const [busy, setBusy] = useState(null);
  const { reload: reloadPre } = pre;
  const { reload: reloadArm } = arm;

  const reloadAll = useCallback(() => {
    reloadPre();
    reloadArm();
  }, [reloadPre, reloadArm]);

  const checks = useMemo(() => (pre.data && Array.isArray(pre.data.checks) ? pre.data.checks : []), [pre.data]);
  const ready = !!(pre.data && pre.data.ready);
  const hasPreflight = !!pre.data;
  const liveAllowed = !!(arm.data && arm.data.live_allowed);
  const block = armBlockReason({ liveAllowed, ready, hasPreflight });

  async function change(s, next) {
    if (busy) return;
    const name = s.name;
    if (!next) {
      // Disarming is the safe direction: one tap, no confirm, no 2FA.
      setBusy(s.id);
      try {
        await api.setArming(s.id, false);
        notify({ kind: 'ok', title: `${name} is now disarmed` });
      } catch (err) {
        notify({ kind: 'error', title: `${name} not changed`, text: err.message, details: err.details });
      } finally {
        setBusy(null);
        reloadArm();
        reloadPre();
      }
      return;
    }
    const ok = await confirm({
      title: `Arm ${name}?`,
      message: (
        <>
          <p>Arming lets this strategy trade in live mode.</p>
          {demo && <p>This arms the strategy for live. It does not start live trading.</p>}
          <p className="hint">Stop still works at any time. You will need a 2FA code next.</p>
        </>
      ),
      confirmLabel: 'Continue',
      danger: true,
    });
    if (!ok) return;
    setBusy(s.id);
    try {
      const r = await twoFactor({
        title: `Arm ${name}`,
        message: 'Enter your code to let this strategy trade in live mode.',
        confirmLabel: 'Arm',
        danger: true,
        action: (code) => api.setArming(s.id, next, code),
      });
      if (r) notify({ kind: 'ok', title: `${name} is now ${next ? 'armed' : 'disarmed'}` });
    } catch (err) {
      notify({ kind: 'error', title: `${name} not changed`, text: err.message, details: err.details });
    } finally {
      setBusy(null);
      reloadArm();
      reloadPre();
    }
  }

  const loading = (pre.loading && !pre.data) || (arm.loading && !arm.data);
  const stale = (pre.error && pre.staleAt) || (arm.error && arm.staleAt) || null;

  return (
    <div className="page">
      <div className="page-head">
        <h1>Arming</h1>
        <button type="button" className="btn small" onClick={reloadAll} disabled={pre.loading || arm.loading}>
          Refresh
        </button>
      </div>
      <StaleNotice staleAt={stale} />
      <ErrorNotice error={pre.error} onRetry={reloadPre} />
      <ErrorNotice error={arm.error} onRetry={reloadArm} />
      {loading && <Loading text="Running checks…" />}

      {hasPreflight && (
        <>
          <div
            className={`arm-banner ${ready ? 'is-ready' : 'is-notready'}`}
            role="status"
            data-testid="arm-banner"
          >
            <CheckStatusIcon status={ready ? 'pass' : 'fail'} />
            <div>
              <h2 className="arm-banner-title">{ready ? 'READY' : 'NOT READY'}</h2>
              <p className="arm-banner-sub">
                {ready
                  ? 'Every check passed. You can arm a strategy below.'
                  : 'Fix the items below before arming a strategy.'}
              </p>
            </div>
          </div>
          <p className="hint" data-testid="arm-note">
            Arming only takes effect when live mode is on. You can arm now while in demo mode, then switch to live.
            {demo ? ' Checks are for live.' : ''}
          </p>
          <div className="arm-meta">
            <span className="hint">
              Last checked <Time value={pre.data.ts} withDate />
            </span>
            <button type="button" className="btn small" onClick={reloadPre} disabled={pre.loading}>
              {pre.loading ? 'Checking…' : 'Run checks again'}
            </button>
          </div>

          {checks.length === 0 && (
            <Empty what="No checks were returned." why="The server sent an empty checklist, so readiness cannot be judged." />
          )}
          {GROUPS.map((g) => {
            const rows = checks.filter((c) => c.status === g.status);
            if (!rows.length) return null;
            return (
              <Card
                key={g.status}
                title={
                  <span className="arm-group-title">
                    {g.title} <Chip tone={g.tone}>{rows.length}</Chip>
                  </span>
                }
              >
                <CollapsibleList items={rows} limit={g.status === 'pass' ? 3 : 20} pageKey={`arming-${g.status}`} label="checks">
                  {(visible) => (
                    <ul className="arm-checks">
                      {visible.map((c) => (
                        <li key={c.id} className="arm-check">
                          <CheckStatusIcon status={c.status} />
                          <div className="arm-check-body">
                            <div className="arm-check-label">{c.label}</div>
                            {c.detail && <div className="arm-check-detail">{c.detail}</div>}
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
                </CollapsibleList>
              </Card>
            );
          })}
        </>
      )}

      {(pre.data || arm.data) && (
        <Card title="Arm for live trading">
          <p className="hint">
            Arming lets a strategy trade in live mode. Each change needs a 2FA code. Stop is never affected.
          </p>
          {block && (
            <p className="hint" data-testid="arm-block">
              {block}
            </p>
          )}
          {STRATEGIES.map((s) => {
            const armed = !!(arm.data && arm.data[s.id] && arm.data[s.id].armed);
            const disabled = !arm.data || busy !== null || (!armed && !!block);
            const reason = !armed && block ? block : null;
            return (
              <div className="arm-switch-row" key={s.id}>
                <div className="arm-switch-text">
                  <div className="arm-switch-name" id={`arm-name-${s.id}`}>
                    {s.name}
                  </div>
                  <div className="arm-switch-reason" id={`arm-reason-${s.id}`}>
                    {reason || (armed ? 'Armed: may trade in live mode.' : 'Not armed.')}
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={armed}
                  aria-labelledby={`arm-name-${s.id}`}
                  aria-describedby={`arm-reason-${s.id}`}
                  className="arm-switch"
                  disabled={disabled}
                  onClick={() => change(s, !armed)}
                >
                  <span className="arm-switch-dot" aria-hidden="true" />
                  {busy === s.id ? 'Working…' : armed ? 'Armed' : 'Off'}
                </button>
              </div>
            );
          })}
          {arm.data && arm.data.updated && (
            <p className="hint">
              Changed <Time value={arm.data.updated} withDate />
            </p>
          )}
        </Card>
      )}
    </div>
  );
}

import { useState } from 'react';
import { api } from '../services/api.js';
import { useDialogs } from '../context/DialogContext.jsx';
import { IconStop } from './Icons.jsx';
import { STOPPED_EVENT, beginStop, describeFailure, describeStop, resolveStop, useStopPending } from '../services/stopResult.js';

export { STOPPED_EVENT };

/** Run bookkeeping around a Stop: a failure here must never keep the Stop from being sent or reported. */
function attempt(fn) {
  try {
    return fn();
  } catch {
    return undefined;
  }
}

/**
 * STOP: one tap + one confirm, no 2FA. Stops BOTH strategies (maker + arbitrage).
 * Calls POST /api/bot/stop and reports exactly what the server said (kill switch, cancelled orders, errors).
 * Never assumes success: a failed or partial Stop is recorded (services/stopResult.js) and stays on screen until a
 * later Stop succeeds or the owner dismisses it. Needs nothing but the dialog provider, so it also works inside a
 * dialog and while nothing else has loaded.
 */
export default function StopButton({ compact = false }) {
  const { confirmStop, dismissDialogs, notify } = useDialogs();
  const [busy, setBusy] = useState(false);
  const pending = useStopPending();

  async function onStop() {
    if (busy) return;
    const ok = await confirmStop({
      title: 'Stop all trading?',
      message: (
        <>
          <p>Stops the maker and the arbitrage scanner: sets both kill switches, cancels resting bot orders and marks the bot stopped.</p>
          <p className="hint">Filled positions stay open until settlement.</p>
        </>
      ),
      confirmLabel: 'Stop now',
      danger: true,
    });
    if (!ok) return;
    // Any dialog underneath (Start, arm, mode...) is closed, so nothing else can be sent without a new tap.
    attempt(dismissDialogs);
    const token = attempt(beginStop);
    setBusy(true);
    let outcome;
    try {
      outcome = describeStop(await api.stop());
    } catch (err) {
      outcome = describeFailure(err);
    }
    const recorded = !!token && attempt(() => (resolveStop(token, outcome), true)) === true;
    // A success is announced; anything else stays in the banner, or in a notice if it could not be recorded.
    if (outcome.kind === 'ok' || !recorded) {
      notify({ kind: outcome.kind === 'ok' ? 'ok' : 'error', title: outcome.title, text: outcome.text });
    }
    setBusy(false);
    attempt(() => window.dispatchEvent(new Event(STOPPED_EVENT)));
  }

  return (
    <button
      type="button"
      className={`btn danger stop-btn ${compact ? 'stop-compact' : 'stop-big'}`}
      onClick={onStop}
      disabled={busy}
      aria-label="Stop the bot (maker and arbitrage)"
    >
      <IconStop />
      <span>{busy || pending ? 'Stopping…' : compact ? 'Stop' : 'Stop bot'}</span>
    </button>
  );
}

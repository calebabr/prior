import { useState } from 'react';
import { IconLock } from '../components/Icons.jsx';
import Card from '../components/Card.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import { ErrorNotice, Loading } from '../components/Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { api } from '../services/api.js';
import { TO_DEMO_ARB, TO_DEMO_ARB_LIVE, TO_DEMO_WARNING } from '../utils/stopCopy.js';

export default function Mode() {
  const { status, error, loading, refresh } = useStatus();
  const { confirm, twoFactor, notify } = useDialogs();
  const [busy, setBusy] = useState(false);
  const mode = status && status.mode;
  const liveAllowed = !!(status && status.live_allowed);
  const arbLive = !!(status && status.arb && status.arb.state === 'live');
  const toDemo = (
    <>
      <p>The bot will trade on the Kalshi demo exchange (play money).</p>
      <div className="mode-warning" data-testid="to-demo-warning">
        {TO_DEMO_WARNING.map((line) => (
          <p key={line}>{line}</p>
        ))}
        <p>{arbLive ? TO_DEMO_ARB_LIVE : TO_DEMO_ARB}</p>
      </div>
    </>
  );

  async function switchTo(target) {
    if (busy) return;
    if (target === 'live') {
      const ok = await confirm({
        title: 'Switch to LIVE (real money)?',
        message: (
          <>
            <p>The bot will place real orders with real money on the Kalshi production exchange.</p>
            <p className="hint">You will need a 2FA code next.</p>
          </>
        ),
        confirmLabel: 'Continue',
        danger: true,
        requireText: 'LIVE',
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const r = await twoFactor({
        title: target === 'live' ? 'Confirm LIVE mode' : 'Switch to DEMO?',
        message: target === 'live' ? 'Final Step: Enter your code to switch to LIVE.' : toDemo,
        confirmLabel: target === 'live' ? 'Go LIVE' : 'Switch to demo',
        danger: target === 'live',
        action: (code) => api.setMode(target, target === 'live' ? 'LIVE' : undefined, code),
      });
      if (r) notify({ kind: 'ok', title: `Mode is now ${String(r.mode || target).toUpperCase()}` });
    } catch (err) {
      notify({
        kind: 'error',
        title: err.status === 409 ? 'Live trading is not allowed by the server' : 'Mode not changed',
        text:
          err.status === 409
            ? 'The server environment is missing the prod key or KALSHI_LIVE_CONFIRM. Set them in the Render dashboard.'
            : err.message,
        details: err.details,
      });
    } finally {
      setBusy(false);
      refresh();
    }
  }

  return (
    <div className="page">
      <h1>Mode</h1>
      <ErrorNotice error={error} onRetry={refresh} />
      {loading && !status && <Loading />}
      {status && (
        <>
          <Card title={<>Current mode<HelpIcon term="demo-vs-live" /></>}>
            <div className={`mode-big mode-${mode}`}>{String(mode || '?').toUpperCase()}</div>
            <p className="muted">
              {mode === 'live'
                ? 'Trading real money on the Kalshi production exchange.'
                : 'Trading play money on the Kalshi demo exchange.'}
            </p>
          </Card>

          <Card title={<>Live trading<HelpIcon term="demo-vs-live" /></>}>
            <p>
              Live allowed by server: <strong className={liveAllowed ? 'pos' : 'neg'}>{liveAllowed ? 'yes' : 'no'}</strong>
            </p>
            <p className="hint">
              Real money needs all three: LIVE selected here (2FA<HelpIcon term="two-factor" /> + typing LIVE), a prod API key in the Render environment,
              and <span className="mono">KALSHI_LIVE_CONFIRM</span> set by the owner in Render. The app alone can never
              enable real money.
            </p>
            <p className="hint">
              After a server restart LIVE is never restored: the maker and arbitrage come back in demo and you get an
              urgent alert to re-arm LIVE here.
            </p>
          </Card>

          <div className="stack">
            {mode !== 'demo' && (
              <button type="button" className="btn primary block" onClick={() => switchTo('demo')} disabled={busy}>
                {busy ? 'Working…' : <><IconLock /> Switch to DEMO</>}
              </button>
            )}
            {mode !== 'live' && (
              <button
                type="button"
                className="btn danger-outline block"
                onClick={() => switchTo('live')}
                disabled={busy || !liveAllowed}
              >
                {busy ? 'Working…' : <><IconLock /> Switch to LIVE</>}
              </button>
            )}
            {mode !== 'live' && !liveAllowed && (
              <p className="hint center">Disabled: The server does not allow live trading.</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

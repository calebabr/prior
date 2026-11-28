import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

// SAFETY: the "Turn on" dialog, its danger style and the mode buttons follow the CONFIGURED arbitrage mode
// (settings.live). While arbitrage is off the state is "stopped" even when the saved mode is live, and the dialog
// must never then say that no orders are placed.

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const NO_ORDERS = /places no orders/i;
const REAL_MONEY = /set to LIVE\. The scanner starts and trades real money/i;

/** A small stand-in for the API: the state follows enabled + live, and turning off drops live. */
function server({ enabled = false, live = false, armed = true } = {}) {
  const s = { enabled, live };
  const view = () => ({
    settings: { enabled: s.enabled, live: s.live, arb_max_contracts: 50, arb_series: ['KXDEMOA'], arb_poll_seconds: 15 },
    schema: {},
    ceilings: {},
    status: {
      state: !s.enabled ? 'stopped' : s.live && armed ? 'live' : 'log-only',
      kill_switch: false, kill_reason: null, live_allowed: true, last_scan_utc: null, last_error: null,
    },
    pnl: null,
  });
  const calls = stubApi({
    'GET /api/status': { ...STATUS, live_allowed: true },
    'GET /api/arb': () => view(),
    'GET /api/arb/opportunities': { items: [] },
    'PUT /api/arb': (c) => {
      if ('enabled' in c.body) s.enabled = c.body.enabled;
      if ('live' in c.body) s.live = c.body.live;
      if (c.body.enabled === false && !('live' in c.body)) s.live = false;
      return view();
    },
  });
  return { calls, puts: () => calls.filter((c) => c.key === 'PUT /api/arb').map((c) => c.body) };
}

let code = 200000;
async function submitCode(user, button) {
  await user.type(await screen.findByLabelText('6-digit code'), String(++code));
  await user.click(screen.getByRole('button', { name: button, exact: true }));
}
async function goLive(user) {
  await user.click(await screen.findByRole('button', { name: /go live/i }));
  await user.type(await screen.findByLabelText('Type LIVE to confirm'), 'LIVE');
  await user.click(screen.getByRole('button', { name: 'Continue' }));
  await submitCode(user, 'Go LIVE');
}
async function openTurnOn(user) {
  await user.click(await screen.findByRole('button', { name: /turn on/i }));
  return screen.findByRole('dialog', { name: 'Turn arbitrage on?' });
}
const confirmButton = (dlg) => within(dlg).getByRole('button', { name: 'Turn on', exact: true });

describe.each(['/strategies/arb/controls', '/arb'])('arbitrage configured mode on %s', (path) => {
  it('off but saved as live: the dialog warns of real money in the danger style; buttons show the live mode', async () => {
    const user = userEvent.setup();
    server({ enabled: false, live: true });
    renderApp(path);
    expect(await screen.findByTestId('arb-set-live')).toHaveTextContent('Turning arbitrage on will trade real money.');
    expect(screen.getByRole('button', { name: /log only/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /go live/i })).toBeNull();
    expect(screen.getByRole('button', { name: /turn on/i })).toHaveClass('danger-outline');
    const dlg = await openTurnOn(user);
    expect(dlg).toHaveTextContent(REAL_MONEY);
    expect(dlg).not.toHaveTextContent(NO_ORDERS);
    expect(confirmButton(dlg)).toHaveClass('danger');
  });

  it('order 1: Go live while off, then Turn on', async () => {
    const user = userEvent.setup();
    const api = server({ enabled: false, live: false });
    renderApp(path);
    await goLive(user);
    expect(await screen.findByText('Arbitrage is set to LIVE')).toBeInTheDocument();
    expect(screen.queryByText('Arbitrage is LIVE')).toBeNull();
    expect(await screen.findByTestId('arb-set-live')).toBeInTheDocument();
    const dlg = await openTurnOn(user);
    expect(dlg).toHaveTextContent(REAL_MONEY);
    expect(dlg).not.toHaveTextContent(NO_ORDERS);
    expect(confirmButton(dlg)).toHaveClass('danger');
    await submitCode(user, 'Turn on');
    await screen.findByText('Arbitrage is on');
    expect(screen.queryByTestId('arb-set-live')).toBeNull();
    expect(api.puts()).toEqual([{ live: true, confirm: 'LIVE' }, { enabled: true }]);
  });

  it('order 2: Turn on, then Go live; turning off drops back to the log-only dialog', async () => {
    const user = userEvent.setup();
    const api = server({ enabled: false, live: false });
    renderApp(path);
    expect(await screen.findByRole('button', { name: /turn on/i })).toHaveClass('primary');
    const first = await openTurnOn(user);
    expect(first).toHaveTextContent(NO_ORDERS);
    expect(first).not.toHaveTextContent(/real money/i);
    expect(confirmButton(first)).not.toHaveClass('danger');
    await submitCode(user, 'Turn on');
    await screen.findByText('Arbitrage is on');
    await goLive(user);
    expect(await screen.findByText('Arbitrage is LIVE')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /log only/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /go live/i })).toBeNull();
    expect(screen.queryByTestId('arb-set-live')).toBeNull();
    await user.click(screen.getByRole('button', { name: /turn off/i }));
    await submitCode(user, 'Turn off');
    expect(await screen.findByRole('button', { name: /go live/i })).toBeInTheDocument();
    expect(await openTurnOn(user)).toHaveTextContent(NO_ORDERS);
    expect(api.puts()).toEqual([{ enabled: true }, { live: true, confirm: 'LIVE' }, { enabled: false }]);
  });

  it('on and saved as live but only logging (not armed): says so and offers Log only', async () => {
    server({ enabled: true, live: true, armed: false });
    renderApp(path);
    expect(await screen.findByTestId('arb-set-live')).toHaveTextContent('not placing live orders right now');
    expect(screen.getByRole('button', { name: /log only/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /go live/i })).toBeNull();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { armBlockReason } from '../pages/Arming.jsx';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const LIVE_STATUS = { ...STATUS, mode: 'live', live_allowed: true };
const CHECKS = [
  { id: 'a', label: 'Exchange key is installed', status: 'pass', detail: 'Present.' },
  { id: 'b', label: 'Phone alerts are set up', status: 'warn', detail: 'No phone registered.' },
  { id: 'c', label: 'Database is reachable', status: 'fail', detail: 'Timed out.' },
];
const PRE = (over = {}) => ({ ready: false, target: 'live', ts: '2026-11-06T15:00:00Z', checks: CHECKS, ...over });
const ARM = (over = {}) => ({
  maker: { armed: false, mode: 'live' },
  arbitrage: { armed: false, mode: 'live' },
  live_allowed: true,
  updated: '2026-11-06T14:00:00Z',
  ...over,
});

function base(extra = {}) {
  return stubApi({
    'GET /api/status': LIVE_STATUS,
    'GET /api/preflight': PRE(),
    'GET /api/arming': ARM(),
    ...extra,
  });
}

describe('armBlockReason', () => {
  it('orders demo, live-not-allowed, no-data, not-ready, ok', () => {
    const ok = { demo: false, liveAllowed: true, ready: true, hasPreflight: true };
    expect(armBlockReason(ok)).toBeNull();
    expect(armBlockReason({ ...ok, demo: true })).toMatch(/demo mode: checks only/i);
    expect(armBlockReason({ ...ok, liveAllowed: false })).toMatch(/does not allow live/i);
    expect(armBlockReason({ ...ok, hasPreflight: false })).toMatch(/run the checks/i);
    expect(armBlockReason({ ...ok, ready: false })).toMatch(/not ready/i);
  });
});

describe('Arming page', () => {
  it('not ready: banner text, grouped checks, switches disabled with the reason', async () => {
    base();
    renderApp('/arming');
    const banner = await screen.findByTestId('arm-banner');
    expect(banner).toHaveTextContent('NOT READY');
    expect(screen.getByText('Needs fixing')).toBeInTheDocument();
    expect(screen.getByText('Worth a look')).toBeInTheDocument();
    expect(screen.getByText('All good')).toBeInTheDocument();
    expect(screen.getByText('Database is reachable')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /maker/i })).toBeDisabled();
    expect(screen.getByRole('switch', { name: /arbitrage/i })).toBeDisabled();
    expect(screen.getByTestId('arm-block')).toHaveTextContent(/not ready/i);
    // Stop stays in the header.
    expect(within(document.querySelector('header.appbar')).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
  });

  it('ready: banner says READY and switches are enabled', async () => {
    base({ 'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }) });
    renderApp('/arming');
    expect(await screen.findByTestId('arm-banner')).toHaveTextContent('READY');
    expect(screen.getByTestId('arm-banner')).not.toHaveTextContent('NOT READY');
    expect(screen.getByRole('switch', { name: /maker/i })).toBeEnabled();
    expect(screen.getByRole('switch', { name: /arbitrage/i })).toBeEnabled();
  });

  it('live not allowed disables both switches with that reason', async () => {
    base({
      'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }),
      'GET /api/arming': ARM({ live_allowed: false }),
    });
    renderApp('/arming');
    expect(await screen.findByTestId('arm-block')).toHaveTextContent(/does not allow live/i);
    expect(screen.getByRole('switch', { name: /maker/i })).toBeDisabled();
  });

  it('demo mode is labelled checks only and cannot arm', async () => {
    base({ 'GET /api/status': STATUS, 'GET /api/preflight': PRE({ ready: true, target: 'demo', checks: [CHECKS[0]] }) });
    renderApp('/arming');
    expect((await screen.findByTestId('arm-banner')).textContent).toMatch(/demo mode: checks only/i);
    expect(screen.getByRole('switch', { name: /maker/i })).toBeDisabled();
  });

  it('arming Maker confirms, asks 2FA, then sends the change', async () => {
    const user = userEvent.setup();
    const calls = base({
      'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }),
      'PUT /api/arming': ARM({ maker: { armed: true, mode: 'live' } }),
    });
    renderApp('/arming');
    await user.click(await screen.findByRole('switch', { name: /maker/i }));
    expect(await screen.findByText(/arming lets this strategy trade in live mode/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Arm' }));
    expect(await screen.findByText('Maker is now armed')).toBeInTheDocument();
    const put = calls.find((c) => c.key === 'PUT /api/arming');
    expect(put.body).toEqual({ strategy: 'maker', armed: true });
    expect(put.headers['X-2FA-Code']).toBe('123456');
  });

  it('arbitrage switch is separate and sends its own strategy', async () => {
    const user = userEvent.setup();
    const calls = base({
      'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }),
      'PUT /api/arming': ARM({ arbitrage: { armed: true, mode: 'live' } }),
    });
    renderApp('/arming');
    await user.click(await screen.findByRole('switch', { name: /arbitrage/i }));
    await user.click(await screen.findByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '654321');
    await user.click(screen.getByRole('button', { name: 'Arm' }));
    await screen.findByText('Arbitrage is now armed');
    expect(calls.find((c) => c.key === 'PUT /api/arming').body).toEqual({ strategy: 'arbitrage', armed: true });
  });

  it('cancelling the confirm sends nothing', async () => {
    const user = userEvent.setup();
    const calls = base({ 'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }) });
    renderApp('/arming');
    await user.click(await screen.findByRole('switch', { name: /maker/i }));
    await user.click(await screen.findByRole('button', { name: /cancel/i }));
    expect(calls.some((c) => c.key === 'PUT /api/arming')).toBe(false);
  });

  it('an armed strategy can be disarmed (2FA) even when not ready', async () => {
    const user = userEvent.setup();
    const calls = base({
      'GET /api/arming': ARM({ maker: { armed: true, mode: 'live' } }),
      'PUT /api/arming': ARM(),
    });
    renderApp('/arming');
    const sw = await screen.findByRole('switch', { name: /maker/i });
    expect(sw).toBeEnabled();
    expect(sw).toHaveAttribute('aria-checked', 'true');
    await user.click(sw);
    await user.click(await screen.findByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '111222');
    await user.click(screen.getByRole('button', { name: 'Disarm' }));
    await screen.findByText('Maker is now disarmed');
    expect(calls.find((c) => c.key === 'PUT /api/arming').body).toEqual({ strategy: 'maker', armed: false });
  });

  it('a server rejection of the change is shown and nothing is assumed', async () => {
    const user = userEvent.setup();
    base({
      'GET /api/preflight': PRE({ ready: true, checks: [CHECKS[0]] }),
      'PUT /api/arming': [409, { detail: 'preflight_not_ready' }],
    });
    renderApp('/arming');
    await user.click(await screen.findByRole('switch', { name: /maker/i }));
    await user.click(await screen.findByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Arm' }));
    expect(await screen.findByText('Maker not changed')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: /maker/i })).toHaveAttribute('aria-checked', 'false');
  });

  it('Run checks again re-fetches preflight', async () => {
    const user = userEvent.setup();
    const calls = base();
    renderApp('/arming');
    await user.click(await screen.findByRole('button', { name: 'Run checks again' }));
    await waitFor(() => expect(calls.filter((c) => c.key === 'GET /api/preflight').length).toBeGreaterThanOrEqual(2));
  });

  it('error state shows a message with Retry that recovers', async () => {
    const user = userEvent.setup();
    let n = 0;
    base({
      'GET /api/preflight': () => (++n === 1 ? [500, { detail: 'boom' }] : PRE({ ready: true, checks: [CHECKS[0]] })),
    });
    renderApp('/arming');
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.length).toBeGreaterThan(0);
    await user.click(within(alerts[0]).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('arm-banner')).toHaveTextContent('READY');
  });

  it('empty checklist is explained and is not READY', async () => {
    base({ 'GET /api/preflight': PRE({ ready: false, checks: [] }) });
    renderApp('/arming');
    expect(await screen.findByText('No checks were returned.')).toBeInTheDocument();
    expect(screen.getByTestId('arm-banner')).toHaveTextContent('NOT READY');
  });

  it('is linked from the More page', async () => {
    base();
    renderApp('/more');
    expect(await screen.findByRole('link', { name: /arming/i })).toHaveAttribute('href', '/arming');
  });
});

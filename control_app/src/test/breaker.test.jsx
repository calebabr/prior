import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { percentToShare, shareToPercent } from '../utils/breaker.js';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const ENV = {
  enabled: true, hwm: 250, hwm_utc: '2026-11-05T15:00:00Z', drawdown: 12.5, drawdown_share: 0.05,
  day: '2026-11-09', day_start_equity: 245, day_loss: 7.5, day_loss_share: 0.0306,
  last_equity: 237.5, last_utc: '2026-11-09T14:00:00Z',
};
const BREAKER = (over = {}) => ({
  settings: { live_enabled: true, demo_enabled: false, drawdown_frac: 0.15, day_loss_frac: 0.1, updated: '2026-11-06T12:00:00Z', by: 'owner', ...(over.settings || {}) },
  schema: {},
  ceilings: { drawdown_frac: { min: 0.02, max: 0.3 }, day_loss_frac: { min: 0.01, max: 0.25 } },
  defaults: { live_enabled: true, demo_enabled: false, drawdown_frac: 0.15, day_loss_frac: 0.1 },
  state: { readable: true, tripped: false, warning: null, env: 'demo', trip: null, envs: { live: ENV, demo: { ...ENV, enabled: false } }, ...(over.state || {}) },
});
const TRIPPED = { tripped: true, reason: 'drawdown', utc: '2026-11-09T13:00:00Z', enabled: true, readable: true, warning: null };
const CLEAR = { tripped: false, reason: null, utc: null, enabled: true, readable: true, warning: null };

function base(breakerStatus = CLEAR, extra = {}) {
  return stubApi({
    'GET /api/status': { ...STATUS, breaker: breakerStatus },
    'GET /api/history': { snapshots: [] },
    'GET /api/config': { config: { run_budget: 15 }, schema: { run_budget: { type: 'number', min: 1, max: 100 } }, ceilings: {} },
    'GET /api/breaker': BREAKER(),
    ...extra,
  });
}

describe('percent helpers', () => {
  it('shows shares as percents and converts back without float noise', () => {
    expect(shareToPercent(0.15)).toBe('15');
    expect(shareToPercent(0.025)).toBe('2.5');
    expect(percentToShare('15')).toBe(0.15);
    expect(percentToShare('7.1')).toBe(0.071);
  });
});

describe('Settings: Circuit Breaker card', () => {
  it('shows the four settings as percents/toggles and the current readings', async () => {
    base();
    renderApp('/settings');
    expect(await screen.findByText('Circuit Breaker')).toBeInTheDocument();
    expect(await screen.findByLabelText(/drawdown limit/i)).toHaveValue(15);
    expect(screen.getByLabelText(/daily loss limit/i)).toHaveValue(10);
    expect(screen.getByLabelText('Protect live mode')).toBeChecked();
    expect(screen.getByLabelText('Protect demo mode')).not.toBeChecked();
    expect(screen.getByText(/Range 2% – 30%/)).toBeInTheDocument();
    const state = screen.getByTestId('breaker-state');
    expect(state).toHaveTextContent('$250.00');
    expect(state).toHaveTextContent(/Demo mode/);
    expect(state).toHaveTextContent(/Last reading/);
  });

  it('validates required, not-a-number, minimum and maximum, and blocks Save', async () => {
    const user = userEvent.setup();
    base();
    renderApp('/settings');
    const dd = await screen.findByLabelText(/drawdown limit/i);
    const save = screen.getByRole('button', { name: /save circuit breaker/i });
    await user.clear(dd);
    expect(screen.getByText('Required')).toBeInTheDocument();
    expect(save).toBeDisabled();
    await user.type(dd, '1');
    expect(screen.getByText('Minimum 2%')).toBeInTheDocument();
    await user.clear(dd);
    await user.type(dd, '31');
    expect(screen.getByText('Maximum 30%')).toBeInTheDocument();
    expect(save).toBeDisabled();
    await user.clear(dd);
    await user.type(dd, '20');
    expect(save).toBeEnabled();
  });

  it('Save sends only the changed fields (as shares) with the 2FA code', async () => {
    const user = userEvent.setup();
    const calls = base(CLEAR, { 'PUT /api/breaker': BREAKER({ settings: { drawdown_frac: 0.2, demo_enabled: true } }) });
    renderApp('/settings');
    const dd = await screen.findByLabelText(/drawdown limit/i);
    await user.clear(dd);
    await user.type(dd, '20');
    await user.click(screen.getByLabelText('Protect demo mode'));
    await user.click(screen.getByRole('button', { name: /save circuit breaker/i }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getAllByRole('button', { name: 'Save' }).pop());
    await screen.findByText('Circuit breaker saved');
    const put = calls.find((c) => c.key === 'PUT /api/breaker');
    expect(put.body).toEqual({ drawdown_frac: 0.2, demo_enabled: true });
    expect(put.headers['X-2FA-Code']).toBe('123456');
  });

  it('shows the 400 detail list and a plain warning when state is unreadable', async () => {
    const user = userEvent.setup();
    base(CLEAR, {
      'GET /api/breaker': BREAKER({ state: { readable: false, warning: 'store down', envs: { live: null, demo: null } } }),
      'PUT /api/breaker': [400, { detail: ['drawdown_frac above ceiling'] }],
    });
    renderApp('/settings');
    expect(await screen.findByTestId('breaker-unreadable')).toHaveTextContent(/cannot be read/i);
    const dd = screen.getByLabelText(/drawdown limit/i);
    await user.clear(dd);
    await user.type(dd, '25');
    await user.click(screen.getByRole('button', { name: /save circuit breaker/i }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getAllByRole('button', { name: 'Save' }).pop());
    expect(await screen.findByText('drawdown_frac above ceiling')).toBeInTheDocument();
  });

  it('shows the state warning when readable', async () => {
    base(CLEAR, { 'GET /api/breaker': BREAKER({ state: { warning: 'Equity reading was missing.' } }) });
    renderApp('/settings');
    expect(await screen.findByTestId('breaker-card-warning')).toHaveTextContent('Equity reading was missing.');
  });
});

describe('Breaker banner', () => {
  it('shows red on Home when tripped, with the limit and a restart button', async () => {
    base(TRIPPED);
    renderApp('/');
    const banner = await screen.findByTestId('breaker-banner');
    expect(banner).toHaveTextContent(/circuit breaker tripped/i);
    expect(banner).toHaveTextContent(/drawdown limit/i);
    expect(within(banner).getByRole('button', { name: /review and restart/i })).toBeInTheDocument();
    // Stop stays in the header.
    expect(within(document.querySelector('header.appbar')).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
  });

  it('is compact with a link to Home on other pages', async () => {
    base(TRIPPED);
    renderApp('/activity', {});
    const banner = await screen.findByTestId('breaker-banner');
    expect(banner).toHaveClass('is-compact');
    expect(within(banner).getByTestId('breaker-link')).toHaveAttribute('href', '/');
  });

  it('is hidden when not tripped, and an amber note shows for a warning', async () => {
    base(CLEAR);
    const { unmount } = renderApp('/');
    await screen.findByRole('button', { name: /start/i });
    expect(screen.queryByTestId('breaker-banner')).toBeNull();
    expect(screen.queryByTestId('breaker-warning')).toBeNull();
    unmount();
    base({ ...CLEAR, warning: 'Equity reading was missing.' });
    renderApp('/');
    expect(await screen.findByTestId('breaker-warning')).toHaveTextContent('Equity reading was missing.');
  });
});

describe('Start after a trip', () => {
  it('needs the acknowledgement checkbox and sends ack_breaker', async () => {
    const user = userEvent.setup();
    const calls = base(TRIPPED, { 'POST /api/bot/start': { desired: 'running', breaker_cleared: true } });
    renderApp('/');
    const start = await screen.findByRole('button', { name: /^start/i });
    await waitFor(() => expect(start).toBeEnabled());
    await user.click(start);
    const go = await screen.findByRole('button', { name: 'Continue' });
    expect(go).toBeDisabled();
    await user.click(screen.getByLabelText(/I have checked the account and want to restart/i));
    expect(go).toBeEnabled();
    await user.click(go);
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByText('Started');
    expect(calls.find((c) => c.key === 'POST /api/bot/start').body).toEqual({ ack_breaker: true });
  });

  it('Review and restart opens the same acknowledgement', async () => {
    const user = userEvent.setup();
    base(TRIPPED);
    renderApp('/');
    await user.click(await screen.findByRole('button', { name: /review and restart/i }));
    expect(await screen.findByLabelText(/I have checked the account and want to restart/i)).toBeInTheDocument();
  });

  it('a 409 breaker_tripped gives a clear message', async () => {
    const user = userEvent.setup();
    base(TRIPPED, { 'POST /api/bot/start': [409, { detail: 'breaker_tripped' }] });
    renderApp('/');
    await user.click(await screen.findByRole('button', { name: /review and restart/i }));
    await user.click(await screen.findByLabelText(/I have checked the account/i));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    expect(await screen.findByText(/circuit breaker is tripped/i)).toBeInTheDocument();
  });

  it('when not tripped, Start has no checkbox and sends no body', async () => {
    const user = userEvent.setup();
    const calls = base(CLEAR, { 'POST /api/bot/start': { desired: 'running' } });
    renderApp('/');
    const start = await screen.findByRole('button', { name: /^start/i });
    await waitFor(() => expect(start).toBeEnabled());
    await user.click(start);
    expect(screen.queryByLabelText(/I have checked the account/i)).toBeNull();
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByText('Started');
    expect(calls.find((c) => c.key === 'POST /api/bot/start').body).toBeUndefined();
  });
});

describe('Arbitrage enable after a trip', () => {
  it('asks for the acknowledgement and sends ack_breaker', async () => {
    const user = userEvent.setup();
    const arb = (enabled) => ({
      settings: { enabled, live: false, arb_max_contracts: 100, arb_min_profit: 0.05, arb_series: ['KXDEMOA'], arb_poll_seconds: 15 },
      schema: {},
      status: { state: enabled ? 'log-only' : 'stopped', running: enabled, kill_switch: !enabled, kill_reason: null, live_allowed: true },
      pnl: null,
    });
    const calls = base(TRIPPED, { 'GET /api/arb': arb(false), 'GET /api/arb/opportunities': { items: [] }, 'PUT /api/arb': arb(true) });
    renderApp('/arb');
    await user.click(await screen.findByRole('button', { name: /turn on/i }));
    await user.click(await screen.findByLabelText(/I have checked the account/i));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Turn on' }));
    await screen.findByText('Arbitrage is on');
    expect(calls.find((c) => c.key === 'PUT /api/arb').body).toEqual({ enabled: true, ack_breaker: true });
  });
});

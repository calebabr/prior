import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import {
  STOP_KEY,
  beginStop,
  clearStopSuccess,
  describeStop,
  dismissStopRecord,
  getStopRecord,
  noteCrossedStop,
  resolveStop,
} from '../services/stopResult.js';
import { STALE_AFTER_MS } from '../context/StatusContext.jsx';

// Design audit A1-A5, A13, A17, A19: what the screen says about Stop and about an old status. All figures made up.

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.removeItem(STOP_KEY);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const RUNNING = { ...STATUS, desired: 'running', kill_switch: false, mode: 'live', live_allowed: true };
const STOPPED = { ...STATUS, desired: 'stopped', kill_switch: true, mode: 'live', live_allowed: true };
const OK = { desired: 'stopped', kill_switch: true, arb_kill_switch: true, cancelled: 3, orders_cancelled: 3, errors: [], stopped_at: new Date().toISOString() };
const PARTIAL = { ...OK, cancelled: 1, orders_cancelled: 1, errors: ['cancel: TimeoutError'] };

/** A stateful fake: Stop flips the status to stopped unless `stop` says otherwise. */
function fake({ stop = [200, OK], status = RUNNING, extra = {} } = {}) {
  const state = { status, stop };
  const calls = stubApi({
    'GET /api/status': () => state.status,
    'GET /api/history': { snapshots: [] },
    'POST /api/bot/stop': () => {
      const res = typeof state.stop === 'function' ? state.stop() : state.stop;
      if (Array.isArray(res) && res[0] === 200 && res[1].kill_switch) state.status = { ...state.status, desired: 'stopped', kill_switch: true };
      return res;
    },
    ...extra,
  });
  return { calls, state };
}

const header = () => document.querySelector('header.appbar');
const headerStop = () => within(header()).getByRole('button', { name: /stop the bot/i });
async function tapStop(user, button = headerStop()) {
  await user.click(button);
  await user.click(await screen.findByRole('button', { name: 'Stop now' }));
}
const alert = () => screen.getByTestId('stop-alert');
const stopCalls = (calls) => calls.filter((c) => c.key === 'POST /api/bot/stop');

describe('A1: a failed Stop stays on screen', () => {
  it('stays long after any toast, on every page, until a later Stop succeeds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { calls, state } = fake({ stop: [500, { detail: 'boom' }] });
    renderApp('/');
    await tapStop(user);
    expect(await screen.findByTestId('stop-alert')).toHaveTextContent('Stop did NOT reach the server');
    expect(alert()).toHaveTextContent(/cancel resting orders in Kalshi/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });
    expect(alert()).toHaveTextContent('Stop did NOT reach the server');
    expect(header()).toContainElement(alert());
    // another page keeps it (it is part of the header)
    await user.click(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Activity' }));
    expect(alert()).toHaveTextContent('Stop did NOT reach the server');
    expect(screen.queryByTestId('stopped-line')).toBeNull();
    state.stop = [200, OK];
    await tapStop(user);
    await waitFor(() => expect(screen.queryByTestId('stop-alert')).toBeNull());
    expect(stopCalls(calls)).toHaveLength(2);
    for (const c of stopCalls(calls)) expect(c.headers['X-2FA-Code']).toBeUndefined();
  });

  it('survives a reload (a new render reads it back) and Dismiss removes it', async () => {
    const user = userEvent.setup();
    fake({ stop: [500, { detail: 'boom' }] });
    const first = renderApp('/');
    await tapStop(user);
    await screen.findByTestId('stop-alert');
    first.unmount();
    renderApp('/settings');
    expect(alert()).toHaveTextContent('Stop did NOT reach the server');
    await user.click(within(alert()).getByRole('button', { name: 'Dismiss Stop warning' }));
    expect(screen.queryByTestId('stop-alert')).toBeNull();
    expect(getStopRecord()).toBeNull();
  });

  it('a refused Stop (expired session) is still reported on the login screen', async () => {
    const user = userEvent.setup();
    fake({ stop: [401, { detail: 'expired' }] });
    renderApp('/');
    await tapStop(user);
    expect(await screen.findByLabelText('Username')).toBeInTheDocument();
    expect(alert()).toHaveTextContent('Stop was refused: sign in again');
  });

  it('a Stop whose answer was never seen (reload while in flight) shows as unconfirmed', () => {
    window.localStorage.setItem(STOP_KEY, JSON.stringify({ result: null, pending: [{ id: 'old:1', sentAt: 1000, session: 'old' }] }));
    fake();
    renderApp('/');
    expect(alert()).toHaveTextContent('Stop was sent, but no answer was seen');
    fireEvent.click(within(alert()).getByRole('button', { name: 'Dismiss Stop warning' }));
    expect(screen.queryByTestId('stop-alert')).toBeNull();
  });

  it('an older Stop answering late never hides a newer failure, and an older failure never replaces a newer success', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1000);
    const older = beginStop();
    vi.setSystemTime(2000);
    const newer = beginStop();
    resolveStop(newer, { kind: 'failed', title: 'newer failed' });
    resolveStop(older, { kind: 'ok', title: 'older ok' });
    expect(getStopRecord().title).toBe('newer failed');
    vi.setSystemTime(3000);
    const a = beginStop();
    vi.setSystemTime(4000);
    const b = beginStop();
    resolveStop(b, { kind: 'ok', title: 'later ok' });
    resolveStop(a, { kind: 'failed', title: 'earlier failed' });
    expect(getStopRecord().title).toBe('later ok');
    clearStopSuccess();
    expect(getStopRecord()).toBeNull();
    noteCrossedStop('Start trading?');
    clearStopSuccess(); // a Start never clears a warning
    expect(getStopRecord().kind).toBe('unconfirmed');
    dismissStopRecord();
  });
});

describe('A2: a partial Stop is not shown as a success', () => {
  it('is a persistent error with the reason and a Kalshi check, with no success toast or Stopped line', async () => {
    const user = userEvent.setup();
    fake({ stop: [200, PARTIAL] });
    renderApp('/');
    await tapStop(user);
    const box = await screen.findByTestId('stop-alert');
    expect(box).toHaveAttribute('role', 'alert');
    expect(box).toHaveTextContent('Stopped, but not everything was cancelled');
    expect(box).toHaveTextContent('Not done: cancel: TimeoutError');
    expect(box).toHaveTextContent('Check Kalshi for resting orders');
    expect(box).toHaveTextContent('cancelled 1 resting order(s)');
    expect(screen.queryByTestId('stopped-line')).toBeNull();
    expect(screen.queryByText(/^Stopped at /)).toBeNull();
    expect(screen.queryByTestId('already-stopped')).toBeNull();
  });

  it('reads the server answer strictly', () => {
    expect(describeStop(OK).kind).toBe('ok');
    expect(describeStop(PARTIAL).title).toBe('Stopped, but not everything was cancelled');
    expect(describeStop({ ...OK, errors: ['store: slow (still writing in background)'] })).toMatchObject({
      kind: 'partial',
      title: 'Stopped, but the server reported a problem',
    });
    expect(describeStop({ ...OK, errors: ['kill_switch: OSError'] }).title).toBe('Stopped, but a kill switch was not confirmed');
    expect(describeStop({ ...OK, kill_switch: false }).title).toBe('Stopped, but a kill switch was not confirmed');
    expect(describeStop({ ...OK, arb_kill_switch: false })).toMatchObject({ kind: 'partial', text: expect.stringMatching(/Arbitrage kill switch NOT confirmed/) });
    for (const odd of [null, {}, { desired: 'stopped' }, { ...OK, desired: 'running' }]) {
      expect(describeStop(odd).kind).toBe('unconfirmed');
    }
  });
});

describe('A3 and A19: after a Stop that worked', () => {
  it('keeps "Stopped at ..., 3 orders cancelled" on the status card long after the toast, across a reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    fake();
    const first = renderApp('/');
    await screen.findByTestId('status-card');
    await tapStop(user);
    const line = await screen.findByTestId('stopped-line');
    expect(line).toHaveTextContent(/^Stopped at .+, 3 orders cancelled$/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(screen.getByTestId('stopped-line')).toBeInTheDocument();
    expect(screen.queryByTestId('stop-alert')).toBeNull();
    // A19: the card says so and Stop is still offered, with the same confirm.
    expect(await screen.findByTestId('already-stopped')).toHaveTextContent('Already stopped. Stop again cancels any leftover bot orders.');
    const card = screen.getByTestId('status-card');
    await user.click(within(card).getByRole('button', { name: /stop the bot/i }));
    expect(screen.getByRole('dialog', { name: 'Stop all trading?' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    first.unmount();
    renderApp('/');
    expect(await screen.findByTestId('stopped-line')).toHaveTextContent('3 orders cancelled');
  });

  it('names only the current mode, and says nothing about orders while running', async () => {
    const { state } = fake({ status: { ...STOPPED, mode: 'demo' } });
    const first = renderApp('/');
    expect(await screen.findByTestId('already-stopped')).toHaveTextContent('leftover bot orders');
    expect(screen.queryByTestId('stopped-line')).toBeNull(); // no Stop from this phone: nothing claimed
    first.unmount();
    state.status = RUNNING;
    window.sessionStorage.clear();
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('state-label')).toHaveTextContent('Running'));
    expect(screen.queryByTestId('already-stopped')).toBeNull();
  });

  it('a Start from here clears the line', async () => {
    const user = userEvent.setup();
    const { state } = fake({
      extra: {
        'POST /api/bot/start': () => {
          state.status = RUNNING;
          return { desired: 'running' };
        },
      },
    });
    renderApp('/');
    await screen.findByTestId('status-card');
    await tapStop(user);
    await screen.findByTestId('stopped-line');
    await user.click(await within(screen.getByTestId('status-card')).findByRole('button', { name: /^Start$/ }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByText('Started');
    expect(screen.queryByTestId('stopped-line')).toBeNull();
    expect(getStopRecord()).toBeNull();
  });

  it('a newer status saying "running" (started elsewhere) ends the line', async () => {
    const user = userEvent.setup();
    const { state } = fake();
    renderApp('/');
    await screen.findByTestId('status-card');
    await tapStop(user);
    await screen.findByTestId('stopped-line');
    await waitFor(() => expect(screen.getByTestId('state-label')).toHaveTextContent('Stopped'));
    state.status = RUNNING;
    await new Promise((r) => setTimeout(r, 5));
    act(() => {
      window.dispatchEvent(new Event('km:stopped')); // the same forced re-read the app does
    });
    await waitFor(() => expect(screen.queryByTestId('stopped-line')).toBeNull());
  });
});

describe('A4 and A17: an old or missing status is never shown as current', () => {
  it('says Not Confirmed in the header and Last known on Home once a read fails', async () => {
    let up = true;
    stubApi({
      'GET /api/status': () => (up ? { ...RUNNING, next_run_at: new Date(Date.now() + 300000).toISOString() } : [500, { detail: 'down' }]),
      'GET /api/history': { snapshots: [] },
    });
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('headline')).toHaveTextContent('Live running.'));
    expect(within(header()).getByText('Running')).toBeInTheDocument();
    expect(screen.getByTestId('timing-line')).toHaveTextContent(/Next/);
    up = false;
    act(() => {
      window.dispatchEvent(new Event('km:stopped'));
    });
    await waitFor(() => expect(screen.getByTestId('headline')).toHaveTextContent('Last known: live running.'));
    expect(within(header()).getByText('Not Confirmed')).toBeInTheDocument();
    expect(header().querySelector('.badge')).toHaveAttribute('data-kind', 'stale');
    expect(screen.getByTestId('state-label')).toHaveTextContent('Last known: Running');
    expect(screen.getByTestId('timing-line')).not.toHaveTextContent(/Next/);
    expect(within(header()).getByTestId('live-tag')).toBeInTheDocument(); // still flagged as real money
    expect(headerStop()).toBeEnabled();
  });

  it('a status that simply grows old is marked too, and the strategy cards say Last known', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let hang = false;
    vi.stubGlobal(
      'fetch',
      vi.fn((url) => {
        if (!String(url).includes('/api/status')) return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('{}') });
        if (hang) return new Promise(() => {});
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(RUNNING)) });
      }),
    );
    renderApp('/strategies');
    await waitFor(() => expect(screen.getByTestId('strategy-card-maker')).toHaveTextContent('Live running'));
    hang = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STALE_AFTER_MS + 2000);
    });
    expect(screen.getByTestId('strategy-card-maker')).toHaveTextContent('Last known: live running');
    expect(within(header()).getByText('Not Confirmed')).toBeInTheDocument();
  });

  it('A17: a first read that fails says "Status unknown", and Stop still works', async () => {
    const user = userEvent.setup();
    const calls = stubApi({ 'GET /api/status': [500, { detail: 'down' }], 'POST /api/bot/stop': OK });
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('state-label')).toHaveTextContent('Status unknown'));
    expect(screen.queryByTestId('already-stopped')).toBeNull();
    await tapStop(user);
    expect(await screen.findByTestId('stopped-line')).toHaveTextContent('3 orders cancelled');
    expect(stopCalls(calls)).toHaveLength(1);
  });
});

describe('A13: the LIVE tag', () => {
  it.each([
    ['running', RUNNING],
    ['stopped', STOPPED],
  ])('shows beside the badge when live and %s', async (_name, status) => {
    stubApi({ 'GET /api/status': status });
    renderApp('/more');
    expect(await within(header()).findByTestId('live-tag')).toHaveTextContent('LIVE');
  });

  it('is absent in demo', async () => {
    stubApi({ 'GET /api/status': { ...RUNNING, mode: 'demo' } });
    renderApp('/more');
    await within(header()).findByText('Demo Running');
    expect(within(header()).queryByTestId('live-tag')).toBeNull();
  });
});

describe('A5: Stop is reachable above every dialog', () => {
  const modeRoutes = (extra) => ({ stop: [200, OK], status: RUNNING, extra });
  const dialogStop = () => within(screen.getByTestId('modal-stop')).getByRole('button', { name: /stop the bot/i });

  async function openModeDialog(user) {
    renderApp('/mode');
    await user.click(await screen.findByRole('button', { name: /switch to demo/i }));
    return screen.getByRole('dialog', { name: 'Switch to DEMO?' });
  }

  it('stacks the Stop confirm on top; closing it returns to the dialog and sends nothing', async () => {
    const user = userEvent.setup();
    const { calls } = fake(modeRoutes({ 'PUT /api/mode': { mode: 'demo' } }));
    const dlg = await openModeDialog(user);
    await user.type(screen.getByLabelText('6-digit code'), '123456');
    const button = dialogStop();
    expect(dlg).toContainElement(button);
    await user.click(button);
    expect(screen.getByRole('dialog', { name: 'Stop all trading?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop now' })).toHaveFocus();
    expect(screen.getAllByTestId('modal-stop')).toHaveLength(1); // the Stop confirm itself has none
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Stop all trading?' })).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Switch to DEMO?' })).toBeInTheDocument();
    expect(screen.getByLabelText('6-digit code')).toHaveValue('123456');
    expect(button).toHaveFocus();
    expect(calls.some((c) => c.method !== 'GET')).toBe(false);
  });

  it('keeps Tab inside the Stop confirm', async () => {
    const user = userEvent.setup();
    fake(modeRoutes({}));
    await openModeDialog(user);
    await user.click(dialogStop());
    const top = screen.getByRole('dialog', { name: 'Stop all trading?' });
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(top).toContainElement(document.activeElement);
    }
  });

  it('closes the dialog underneath once Stop is confirmed, so it cannot be sent without a new tap', async () => {
    const user = userEvent.setup();
    const { calls } = fake(modeRoutes({ 'PUT /api/mode': { mode: 'demo' } }));
    await openModeDialog(user);
    await user.type(screen.getByLabelText('6-digit code'), '123456');
    await tapStop(user, dialogStop());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(stopCalls(calls)).toHaveLength(1);
    expect(stopCalls(calls)[0].headers['X-2FA-Code']).toBeUndefined();
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
  });

  it('closes a confirm dialog (arm) too', async () => {
    const user = userEvent.setup();
    const { calls } = fake(
      modeRoutes({
        'GET /api/preflight': { ready: true, checks: [{ id: 'a', label: 'ok', status: 'pass' }] },
        'GET /api/arming': { maker: { armed: false, mode: 'live' }, arbitrage: { armed: false, mode: 'demo' }, live_allowed: true },
      }),
    );
    renderApp('/arming');
    await user.click(await screen.findByRole('switch', { name: /maker/i }));
    await screen.findByRole('dialog', { name: 'Arm Maker?' });
    await tapStop(user, dialogStop());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
  });

  /** A Start whose request is held open until `release(result)`. */
  async function startInFlight(user) {
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const calls = [];
    let status = STOPPED;
    vi.stubGlobal(
      'fetch',
      vi.fn((url, init = {}) => {
        const key = `${init.method || 'GET'} ${String(url).replace('http://api.test', '').split('?')[0]}`;
        calls.push({ key, headers: init.headers || {} });
        const res = (s, body) => ({ ok: s < 300, status: s, text: () => Promise.resolve(JSON.stringify(body)) });
        if (key === 'GET /api/status') return Promise.resolve(res(200, status));
        if (key === 'POST /api/bot/stop') return Promise.resolve(res(200, OK));
        if (key === 'POST /api/bot/start') return gate.then(([s, body]) => res(s, body));
        return Promise.resolve(res(404, {}));
      }),
    );
    renderApp('/');
    await user.click(await within(await screen.findByTestId('status-card')).findByRole('button', { name: /^Start$/ }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByRole('button', { name: 'Working…' });
    return { calls, release: (s, body) => act(async () => release([s, body])) };
  }

  it('Stop works while a 2FA request is in flight; a Start that still lands raises a warning, not "Started"', async () => {
    const user = userEvent.setup();
    const { calls, release } = await startInFlight(user);
    expect(screen.queryByRole('button', { name: 'Dismiss dialog' })).toBeNull(); // cannot be closed...
    expect(dialogStop()).toBeEnabled(); // ...but Stop is still there
    await tapStop(user, dialogStop());
    await waitFor(() => expect(stopCalls(calls)).toHaveLength(1));
    expect(await screen.findByText(/This request had already left/)).toBeInTheDocument();
    await release(200, { desired: 'running' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText('Started')).toBeNull();
    expect(alert()).toHaveTextContent('Check that the bot is stopped');
    expect(alert()).toHaveTextContent('"Start trading?" was already sent');
    expect(screen.queryByTestId('stopped-line')).toBeNull();
    expect(calls.filter((c) => c.key === 'POST /api/bot/start')).toHaveLength(1);
    // a new Stop clears it
    await tapStop(user);
    await waitFor(() => expect(screen.queryByTestId('stop-alert')).toBeNull());
  });

  it('a rejected code after Stop closes the prompt instead of offering to send Start again', async () => {
    const user = userEvent.setup();
    const { release } = await startInFlight(user);
    await tapStop(user, dialogStop());
    await release(403, { detail: '2fa_invalid' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByTestId('stop-alert')).toBeNull();
    expect(screen.getByTestId('stopped-line')).toBeInTheDocument();
  });

  it('a Start the server refused raises no warning', async () => {
    const user = userEvent.setup();
    const { release } = await startInFlight(user);
    await tapStop(user, dialogStop());
    await release(409, { detail: 'stopped_during_start' });
    expect(await screen.findByText(/Stop won/)).toBeInTheDocument();
    expect(screen.queryByTestId('stop-alert')).toBeNull();
  });

  it('a Start with no clear answer after Stop raises the warning', async () => {
    const user = userEvent.setup();
    const { release } = await startInFlight(user);
    await tapStop(user, dialogStop());
    await release(500, { detail: 'boom' });
    await waitFor(() => expect(alert()).toHaveTextContent('Check that the bot is stopped'));
  });

  it('turning arbitrage on, sent before Stop and accepted after it, raises the warning', async () => {
    const user = userEvent.setup();
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const arb = { settings: { enabled: false, live: false }, status: { state: 'stopped', live_allowed: true } };
    stubApi({ 'GET /api/status': STOPPED, 'GET /api/arb': arb, 'GET /api/arb/opportunities': { items: [] }, 'POST /api/bot/stop': OK });
    const base = globalThis.fetch;
    vi.stubGlobal(
      'fetch',
      vi.fn((url, init = {}) =>
        init.method === 'PUT'
          ? gate.then(() => ({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ settings: { enabled: true, live: false }, status: { state: 'log-only' } })) }))
          : base(url, init),
      ),
    );
    renderApp('/strategies/arb/controls');
    await user.click(await screen.findByRole('button', { name: /turn on/i }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Turn on' }));
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Working…' })).toBeDisabled());
    await tapStop(user, dialogStop());
    await act(async () => release());
    await waitFor(() => expect(alert()).toHaveTextContent('"Turn arbitrage on?" was already sent'));
    expect(screen.queryByText('Arbitrage is on')).toBeNull();
  });

  it('a failed Stop from inside a dialog is reported on the page', async () => {
    const user = userEvent.setup();
    fake({ stop: [500, { detail: 'boom' }], status: RUNNING });
    await openModeDialog(user);
    await tapStop(user, dialogStop());
    expect(await screen.findByTestId('stop-alert')).toHaveTextContent('Stop did NOT reach the server');
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('P3-L1: a damaged stored record never blocks Stop', () => {
  it.each([
    ['null and non-object pending entries', JSON.stringify({ pending: [null, 5, 'x'], result: null })],
    ['wrong field types', JSON.stringify({ pending: 'no', result: { kind: 'failed', title: { a: 1 }, details: 'no' } })],
    ['not JSON', '{oops'],
    ['not an object', '7'],
  ])('%s', async (_name, stored) => {
    window.localStorage.setItem(STOP_KEY, stored);
    const user = userEvent.setup();
    const { calls } = fake();
    renderApp('/');
    await screen.findByTestId('status-card');
    await tapStop(user);
    await waitFor(() => expect(stopCalls(calls)).toHaveLength(1));
    await waitFor(() => expect(getStopRecord().kind).toBe('ok'));
  });
});

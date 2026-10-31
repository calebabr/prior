import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { StatusProvider } from '../context/StatusContext.jsx';
import UpdateBanner from '../components/UpdateBanner.jsx';
import { isOlder, shortId } from '../utils/version.js';
import { resetForTests, markReady, getReady, setApplyServiceWorker, checkDeployedBundle } from '../utils/updateSignal.js';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const LEADER = {
  ...STATUS,
  role: 'leader',
  lease_holder: 'a3f9c2d41b7e4c0a9d6e5f8b12c34d56',
  instance_id: 'a3f9c2d41b7e4c0a9d6e5f8b12c34d56',
  lease_generation: 14,
  lease_expires_at: new Date(Date.now() + 45000).toISOString(),
  api_version: '2026.10.16-1',
  schema_version: 1,
  min_app_version: '0.1.0',
};
const CLOUD = { settings: { keepalive_minutes: 10, pinger_minutes: 5, pinger_enabled: true }, sleep_timeout_minutes: 15 };
const REL = {
  id: 'c0ffee00c0ffee00c0ffee00c0ffee00',
  version: '2026.10.16-2',
  commit: '9f3a1c7d2b8e4a6f0c5d7e1b3a9f2c4d6e8b0a1c',
  summary: 'Lease handover.',
  ci: 'passed',
  status: 'pending',
  expires_at: new Date(Date.now() + 20 * 60000).toISOString(),
};

describe('version helpers', () => {
  it('compares versions and ignores unreadable ones', () => {
    expect(isOlder('0.1.0', '0.2.0')).toBe(true);
    expect(isOlder('0.1.0', '0.1.0')).toBe(false);
    expect(isOlder('1.0.0', '0.9.9')).toBe(false);
    expect(isOlder('0.1.0', undefined)).toBe(false);
    expect(isOlder('0.1.0', 'abc')).toBe(false);
    expect(shortId('a3f9c2d41b7e')).toBe('a3f9c2d4');
  });
});

describe('UpdateBanner', () => {
  function show(appVersion, min) {
    stubApi({ 'GET /api/status': { ...STATUS, min_app_version: min } });
    setToken('tok');
    render(
      <StatusProvider>
        <UpdateBanner appVersion={appVersion} />
      </StatusProvider>,
    );
  }
  it('shows when the app is older than min_app_version', async () => {
    show('0.1.0', '0.2.0');
    expect(await screen.findByText(/update available, reopen the app/i)).toBeInTheDocument();
  });
  it('stays hidden when the app is current', async () => {
    show('0.2.0', '0.2.0');
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText(/update available/i)).toBeNull();
  });
  it('never blocks Stop and the page still renders', async () => {
    stubApi({ 'GET /api/status': { ...STATUS, min_app_version: '99.0.0' } });
    renderApp('/');
    expect(await screen.findByText(/update available, reopen the app/i)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /stop the bot/i })[0]).toBeEnabled();
  });
});

describe('UpdateBanner new bundle prompt', () => {
  const READY = /a new version is ready/i;
  function mount(extra = {}) {
    stubApi({ 'GET /api/status': { ...STATUS, ...extra } });
    setToken('tok');
    render(
      <StatusProvider>
        <UpdateBanner appVersion="0.2.0" />
      </StatusProvider>,
    );
  }
  beforeEach(() => {
    resetForTests();
    sessionStorage.clear();
  });
  it('is hidden by default and when status has no version', async () => {
    mount({ min_app_version: undefined, api_version: undefined });
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText(READY)).toBeNull();
  });
  it('shows when a new bundle is signalled and the app is current', async () => {
    mount({ min_app_version: '0.1.0' });
    act(() => markReady());
    expect(await screen.findByText(READY)).toBeInTheDocument();
  });
  it('keeps the stronger min_app_version message when both apply', async () => {
    mount({ min_app_version: '9.0.0' });
    act(() => markReady());
    expect(await screen.findByText(/update available, reopen the app/i)).toBeInTheDocument();
    expect(screen.queryByText(READY)).toBeNull();
  });
  it('dismisses for the session', async () => {
    mount();
    act(() => markReady());
    await userEvent.click(await screen.findByRole('button', { name: /dismiss update notice/i }));
    expect(screen.queryByText(READY)).toBeNull();
  });
  it('tap asks the worker to update then reloads once', async () => {
    const reload = vi.fn();
    vi.stubGlobal('location', { ...window.location, reload });
    const sw = vi.fn().mockResolvedValue();
    setApplyServiceWorker(sw);
    mount();
    act(() => markReady());
    await userEvent.click(await screen.findByRole('button', { name: READY }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(sw).toHaveBeenCalled();
  });
  it('does not re-signal right after a reload (no loop)', () => {
    sessionStorage.setItem('prior_update_reloaded_at', String(Date.now()));
    markReady();
    expect(getReady()).toBe(false);
  });
  it('bundle check signals only when the deployed entry script differs', async () => {
    document.head.innerHTML = '<script type="module" src="/assets/index-AAAA.js"></script>';
    const same = vi.fn().mockResolvedValue({ ok: true, text: async () => '<script src="/assets/index-AAAA.js">' });
    await checkDeployedBundle(same);
    expect(getReady()).toBe(false);
    resetForTests();
    const newer = vi.fn().mockResolvedValue({ ok: true, text: async () => '<script src="/assets/index-BBBB.js">' });
    await checkDeployedBundle(newer);
    expect(getReady()).toBe(true);
    document.head.innerHTML = '';
  });
});

describe('Lease card', () => {
  it('says the server is trading when leader', async () => {
    stubApi({ 'GET /api/status': LEADER, 'GET /api/cloud': CLOUD });
    renderApp('/server');
    expect(await screen.findByText('This server is trading')).toBeInTheDocument();
    expect(screen.getByText('Leader')).toBeInTheDocument();
    expect(screen.getByText(/a3f9c2d4 \(this server\)/)).toBeInTheDocument();
    expect(screen.getByText('14')).toBeInTheDocument();
    expect(screen.getByText('2026.10.16-1')).toBeInTheDocument();
  });
  it('says standing by on a standby, and Stop stays enabled', async () => {
    stubApi({ 'GET /api/status': { ...LEADER, role: 'standby', lease_holder: 'e71b04aa5c2d' }, 'GET /api/cloud': CLOUD });
    renderApp('/server');
    expect(await screen.findByText('This server is standing by')).toBeInTheDocument();
    expect(screen.getByText('Standby')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /stop the bot/i })[0]).toBeEnabled();
  });
});

describe('Pending release card', () => {
  const base = { 'GET /api/status': LEADER, 'GET /api/cloud': CLOUD };

  it('is hidden when the endpoint answers 404', async () => {
    stubApi({ ...base, 'GET /api/release/pending': [404, { detail: 'Not Found' }] });
    renderApp('/server');
    await screen.findByText('This server is trading');
    expect(screen.queryByText('Pending Release')).toBeNull();
  });

  it('is hidden when nothing is pending', async () => {
    stubApi({ ...base, 'GET /api/release/pending': { release: null } });
    renderApp('/server');
    await screen.findByText('This server is trading');
    expect(screen.queryByText('Pending Release')).toBeNull();
  });

  it('shows the release and Approve asks for a 2FA code, then sends it', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      ...base,
      'GET /api/release/pending': { release: REL },
      [`POST /api/release/pending/${REL.id}/approve`]: { id: REL.id, status: 'approved' },
    });
    renderApp('/server');
    expect(await screen.findByText('Pending Release')).toBeInTheDocument();
    expect(screen.getByText('2026.10.16-2')).toBeInTheDocument();
    expect(screen.getByText('9f3a1c7')).toBeInTheDocument();
    expect(screen.getByText('Lease handover.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /approve/i }));
    const code = await screen.findByLabelText(/code from your authenticator/i);
    expect(calls.some((c) => c.key.endsWith('/approve'))).toBe(false);
    await user.type(code, '123456');
    await user.click(screen.getAllByRole('button', { name: /^approve$/i }).pop());
    await waitFor(() => expect(calls.some((c) => c.key.endsWith('/approve'))).toBe(true));
    expect(calls.find((c) => c.key.endsWith('/approve')).headers['X-2FA-Code']).toBe('123456');
    expect(await screen.findByText(/approved\. the deploy continues/i)).toBeInTheDocument();
  });

  it('Reject confirms first and needs no code', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      ...base,
      'GET /api/release/pending': { release: REL },
      [`POST /api/release/pending/${REL.id}/reject`]: { id: REL.id, status: 'rejected' },
    });
    renderApp('/server');
    await user.click(await screen.findByRole('button', { name: 'Reject' }));
    expect(calls.some((c) => c.key.endsWith('/reject'))).toBe(false);
    await screen.findByText('Reject this release?');
    await user.click(screen.getAllByRole('button', { name: 'Reject' }).pop());
    await waitFor(() => expect(calls.some((c) => c.key.endsWith('/reject'))).toBe(true));
    expect(calls.find((c) => c.key.endsWith('/reject')).headers['X-2FA-Code']).toBeUndefined();
    expect(await screen.findByText(/rejected\. nothing was deployed/i)).toBeInTheDocument();
  });

  it('shows an already-resolved message on 409', async () => {
    const user = userEvent.setup();
    stubApi({
      ...base,
      'GET /api/release/pending': { release: REL },
      [`POST /api/release/pending/${REL.id}/approve`]: [409, { detail: 'release_not_pending' }],
    });
    renderApp('/server');
    await user.click(await screen.findByRole('button', { name: /approve/i }));
    await user.type(await screen.findByLabelText(/code from your authenticator/i), '123456');
    await user.click(screen.getAllByRole('button', { name: /^approve$/i }).pop());
    expect(await screen.findByText(/already decided or has expired/i)).toBeInTheDocument();
  });

  it('an expired release cannot be approved', async () => {
    stubApi({
      ...base,
      'GET /api/release/pending': { release: { ...REL, expires_at: new Date(Date.now() - 60000).toISOString() } },
    });
    renderApp('/server');
    expect(await screen.findByText(/release request has expired/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /approve/i })).toBeDisabled();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { LAST_VISIT_KEY, abnormalities, edgeSummary, headlineParts, settingsToastTitle, sinceParts, stopToastTitle } from '../utils/overview.js';

const iso = (msAgo) => new Date(Date.now() - msAgo).toISOString();
const MIN = 60 * 1000;

const RUNNING = {
  ...STATUS,
  desired: 'running',
  kill_switch: false,
  mode: 'demo',
  heartbeat_age_s: 60,
  last_run_at: iso(4 * MIN),
  next_run_at: new Date(Date.now() + 5 * MIN).toISOString(),
  account: { cash: 212.4, exposure: 37.6, headroom: 87.4 },
  limits: { run_budget: 25, max_open_frac: 0.4 },
  pnl: { d7: 4.83, d30: 17.12 },
  pnl_today: { maker: 1.4, arb: 0.35, you: -0.8, total: 0.95 },
  recent_passes: Array.from({ length: 12 }, (_, i) => ({ ts: iso((12 - i) * 5 * MIN), ok: true, late: i === 8, errors: 0 })),
};

const STRATS = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)' },
  { id: 'you', source: 'you', name: 'You', color: 'var(--warn)' },
];

const LEDGER = {
  env: 'demo',
  strategies: [
    { strategy: 'maker', realized_pnl: 17.12, roi_on_settled: 0.046, open_cost: 8.2, fills: 9, settled_fills: 6, settled_cost: 372 },
    { strategy: 'arb', realized_pnl: 2.4, roi_on_settled: 0.011, open_cost: 0, fills: 2, settled_fills: 2, settled_cost: 218 },
    { strategy: 'you', realized_pnl: -0.8, roi_on_settled: null, open_cost: 4, fills: 1, settled_fills: 1, settled_cost: 10 },
  ],
  settled_by_strategy: [
    { strategy: 'maker', realized_pnl: 17.12 },
    { strategy: 'arb', realized_pnl: 2.4 },
    { strategy: 'you', realized_pnl: -0.8 },
  ],
};

const base = (extra = {}) => ({
  'GET /api/status': RUNNING,
  'GET /api/history': { snapshots: [] },
  'GET /api/strategies': [200, STRATS],
  'GET /api/analytics/ledger': LEDGER,
  'GET /api/account/positions': [200, []],
  'GET /api/markets': { markets: [] },
  ...extra,
});

function mediaQuery(wide) {
  window.matchMedia = vi.fn((q) => ({
    matches: /min-width:\s*768px/.test(q) ? wide : false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  }));
}

// IntersectionObserver stub: tests call fireOffscreen() to say the status card scrolled away.
let observers = [];
function stubObserver() {
  observers = [];
  window.IntersectionObserver = class {
    constructor(cb) {
      this.cb = cb;
      observers.push(this);
    }
    observe() {}
    disconnect() {}
  };
}
function fireOffscreen(off = true) {
  act(() => {
    observers.forEach((o) => o.cb([{ isIntersecting: !off, boundingClientRect: { top: off ? -200 : 100 } }]));
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete window.matchMedia;
  delete window.IntersectionObserver;
});

describe('Home headline and status card', () => {
  it('running in demo: one headline line with the 30-day P&L', async () => {
    stubApi(base());
    renderApp('/');
    const hl = await screen.findByTestId('headline');
    expect(hl).toHaveTextContent('Demo running.');
    await waitFor(() => expect(hl).toHaveTextContent('+$19.52 over 30 days.'));
    expect(screen.getByTestId('state-label')).toHaveTextContent('Running');
    expect(screen.getByTestId('status-card')).toHaveAttribute('data-state', 'demo');
  });

  it('stopped and live states read correctly', async () => {
    stubApi(base({ 'GET /api/status': { ...STATUS, mode: 'live' } }));
    const { unmount } = renderApp('/');
    expect(await screen.findByTestId('headline')).toHaveTextContent('Live stopped.');
    unmount();
    vi.unstubAllGlobals();
    stubApi(base({ 'GET /api/status': { ...RUNNING, mode: 'live' } }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('headline')).toHaveTextContent('Live running.'));
    expect(screen.getByTestId('status-card')).toHaveAttribute('data-state', 'live');
  });

  it('an error turns the headline and card to Needs Attention and lists the problem in red', async () => {
    stubApi(base({ 'GET /api/status': { ...RUNNING, last_error: 'kalshi 500 on order' } }));
    renderApp('/');
    const hl = await screen.findByTestId('headline');
    await waitFor(() => expect(hl).toHaveTextContent('Needs attention.'));
    expect(hl.querySelector('.ov-hl-danger')).not.toBeNull();
    const bad = screen.getByTestId('abnormal');
    expect(within(bad).getByText(/kalshi 500 on order/)).toHaveClass('error-text');
    expect(screen.getByTestId('status-card')).toHaveAttribute('data-state', 'error');
  });

  it('a stale heartbeat and an overdue run show in red; a healthy status shows none', async () => {
    stubApi(base({ 'GET /api/status': { ...RUNNING, heartbeat_age_s: 45 * 60, next_run_at: iso(10 * MIN) } }));
    const { unmount } = renderApp('/');
    const bad = await screen.findByTestId('abnormal');
    expect(within(bad).getByText(/No heartbeat for 45 min/)).toHaveClass('error-text');
    expect(within(bad).getByText(/Next run is overdue/)).toHaveClass('error-text');
    expect(screen.getByTestId('timing-line').querySelector('.error-text')).not.toBeNull();
    unmount();
    vi.unstubAllGlobals();
    stubApi(base());
    renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.queryByTestId('abnormal')).toBeNull();
  });

  it('the status card is compact: timing line, Stop, no tiles or heartbeat strip', async () => {
    stubApi(base());
    renderApp('/');
    const card = await screen.findByTestId('status-card');
    expect(screen.queryByTestId('status-tiles')).toBeNull();
    expect(screen.queryByTestId('heartbeat-strip')).toBeNull();
    expect(screen.queryByTestId('pnl-combined')).toBeNull();
    expect(screen.getByTestId('timing-line')).toHaveTextContent(/Last run 4 min ago · Next in \d+ min/);
    expect(within(card).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
    const text = document.querySelector('main').textContent;
    for (const gone of ['Heartbeat', 'Kill switch', 'Live allowed', 'Pass running', 'Expected return', 'Run budget', 'headroom']) {
      expect(text).not.toContain(gone);
    }
  });

  it('Run once moves into the more-actions menu and still confirms first', async () => {
    const user = userEvent.setup();
    const calls = stubApi(base({ 'POST /api/bot/run-once': { ran: true } }));
    renderApp('/');
    await user.click(await screen.findByRole('button', { name: 'More actions' }));
    await user.click(screen.getByRole('menuitem', { name: 'Run once' }));
    await user.click(await screen.findByRole('button', { name: 'Run once' }));
    await waitFor(() => expect(calls.some((c) => c.key === 'POST /api/bot/run-once')).toBe(true));
  });

  it('stopped shows Start (with the lock) as the primary action and Stop is still on the card', async () => {
    stubApi(base({ 'GET /api/status': STATUS }));
    renderApp('/');
    await screen.findByTestId('status-card');
    const card = screen.getByTestId('status-card');
    expect(within(card).getByRole('button', { name: /Start/ })).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
  });

  it('shows one bar and one line: "$37.60 at risk of $100" (limit = max_open_frac of balance + positions)', async () => {
    stubApi(base({ 'GET /api/status': { ...RUNNING, account: { cash: 212.4, exposure: 37.6 }, limits: { max_open_frac: 0.4 } } }));
    renderApp('/');
    const line = await screen.findByTestId('risk-line');
    expect(line).toHaveTextContent('$37.60 at risk of $100');
    expect(document.querySelector('main').textContent).not.toMatch(/headroom|limit \(/);
  });

  it('no demo banner: the mode is the chip on the status card', async () => {
    stubApi(base());
    renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.queryByTestId('demo-banner')).toBeNull();
    expect(within(screen.getByTestId('status-card')).getByText('Demo')).toBeInTheDocument();
  });

  it('Home total is Maker + Arbitrage only; the owner trades sit on their own line', async () => {
    stubApi(base());
    renderApp('/');
    const fig = await screen.findByTestId('home-total-figure');
    await waitFor(() => expect(fig).toHaveTextContent('+$19.52'));
    expect(within(screen.getByTestId('home-row-maker')).getByText('+$17.12')).toBeInTheDocument();
    expect(within(screen.getByTestId('home-row-arb')).getByText('+$2.40')).toBeInTheDocument();
    const manual = screen.getByTestId('manual-row');
    expect(manual).toHaveTextContent('manual, not in the total');
    expect(manual).toHaveTextContent('−$0.80');
    expect(screen.getByTestId('by-strategy').querySelectorAll('.home-rows > li')).toHaveLength(2);
  });

  it('a failed ledger shows dashes and a retry, never $0.00', async () => {
    stubApi(base({ 'GET /api/analytics/ledger': [500, { detail: 'boom' }] }));
    renderApp('/');
    expect(await screen.findByTestId('home-ledger-error')).toHaveTextContent("Couldn't load P&L");
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('—');
    expect(within(screen.getByTestId('home-row-maker')).queryByText('$0.00')).toBeNull();
  });

  it('the page has one h1, Home', async () => {
    stubApi(base());
    renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Home']);
  });

  it('edge verdict joins the headline when analytics has cells', async () => {
    stubApi(base({ 'GET /api/analytics/summary': { by_cell: [{ cell: 'a', verdict: 'on_track' }, { cell: 'b', verdict: 'too_early' }] } }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('headline')).toHaveTextContent('Edge on track.'));
  });
});

describe('Overview helpers', () => {
  it('edge summary: below wins, all too early, on track', () => {
    expect(edgeSummary({ by_cell: [{ verdict: 'below' }, { verdict: 'on_track' }] }).text).toBe('Edge Below Expected');
    expect(edgeSummary({ by_cell: [{ verdict: 'too_early' }] }).text).toBe('Edge Too Early');
    expect(edgeSummary({ by_cell: [{ verdict: 'above' }] }).text).toBe('Edge On Track');
    expect(edgeSummary({ by_cell: [] })).toBeNull();
    expect(edgeSummary(null)).toBeNull();
  });

  it('headline omits the P&L part when there is none', () => {
    expect(headlineParts(RUNNING, null).map((p) => p.text)).toEqual(['Demo Running', '+$17.12 Over 30 Days']);
    expect(headlineParts({ ...STATUS, pnl: null }, null).map((p) => p.text)).toEqual(['Demo Stopped']);
  });

  it('abnormalities: kill switch while desired running, failed last pass, fill sync error', () => {
    const keys = abnormalities({ ...RUNNING, kill_switch: true, recent_passes: [{ ts: 'x', ok: false, errors: 1 }], fills_sync: { error: 'x' } }).map((b) => b.key);
    expect(keys).toEqual(expect.arrayContaining(['kill', 'pass', 'sync']));
    expect(abnormalities(null)).toEqual([]);
  });

  it('toast texts are specific, and absent fields fall back', () => {
    expect(stopToastTitle({ stopped_at: '2026-10-15T21:41:00Z', orders_cancelled: 3 })).toMatch(/^Stopped at .* · 3 Orders Cancelled$/);
    expect(stopToastTitle({ stopped_at: '2026-10-15T21:41:00Z', orders_cancelled: 1 })).toMatch(/1 Order Cancelled$/);
    expect(stopToastTitle({ cancelled: 3 })).toBeNull();
    expect(settingsToastTitle({ changes: [{ key: 'run_budget', from: 25, to: 50 }] })).toBe('Settings Saved · Run Budget $25 → $50');
    expect(settingsToastTitle({ changes: [] })).toBeNull();
    expect(settingsToastTitle({})).toBeNull();
    expect(sinceParts({ fills: 0, settled: 0, pnl_change: 0 }, 'x')).toEqual([]);
  });
});

describe('Since You Last Checked', () => {
  it('first visit shows nothing and records the visit', async () => {
    const calls = stubApi(base());
    renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.queryByTestId('since-card')).toBeNull();
    expect(calls.some((c) => c.key === 'GET /api/since')).toBe(false);
    expect(window.localStorage.getItem(LAST_VISIT_KEY)).not.toBeNull();
  });

  it('a returning visit shows fills, settled markets and P&L change from /api/since', async () => {
    window.localStorage.setItem(LAST_VISIT_KEY, iso(3 * 60 * MIN));
    const calls = stubApi(base({ 'GET /api/since': { fills: 3, settled: 2, pnl_change: 1.4 } }));
    renderApp('/');
    const card = await screen.findByTestId('since-card');
    expect(card).toHaveTextContent('Since you last checked:');
    expect(card).toHaveTextContent(/3 new fills, 2 markets settled, P&L \+\$1\.40 since /);
    expect(calls.find((c) => c.key === 'GET /api/since').path).toMatch(/ts=/);
    await userEvent.setup().click(within(card).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('since-card')).toBeNull();
  });

  it('is hidden when nothing changed or the request fails', async () => {
    window.localStorage.setItem(LAST_VISIT_KEY, iso(60 * MIN));
    stubApi(base({ 'GET /api/since': { fills: 0, settled: 0, pnl_change: 0 } }));
    const { unmount } = renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.queryByTestId('since-card')).toBeNull();
    unmount();
    vi.unstubAllGlobals();
    stubApi(base({ 'GET /api/since': [500, { detail: 'boom' }] }));
    renderApp('/');
    await screen.findByTestId('headline');
    expect(screen.queryByTestId('since-card')).toBeNull();
  });
});

describe('Floating status widget', () => {
  async function setupWide(extra = {}) {
    mediaQuery(true);
    stubObserver();
    const calls = stubApi(base(extra));
    renderApp('/');
    await screen.findByTestId('status-card');
    return calls;
  }

  it('appears at 768 px and up only after the status card scrolls away, and goes back when it returns', async () => {
    await setupWide();
    expect(screen.queryByTestId('status-widget')).toBeNull();
    fireOffscreen(true);
    const w = await screen.findByTestId('status-widget');
    expect(w).toHaveTextContent('Running');
    expect(w).toHaveTextContent('Demo');
    expect(w).toHaveTextContent('+$0.95');
    fireOffscreen(false);
    await waitFor(() => expect(screen.queryByTestId('status-widget')).toBeNull());
  });

  it('never appears on phone widths, even when the card is off screen', async () => {
    mediaQuery(false);
    stubObserver();
    stubApi(base());
    renderApp('/');
    await screen.findByTestId('status-card');
    fireOffscreen(true);
    expect(screen.queryByTestId('status-widget')).toBeNull();
    expect(observers).toHaveLength(0);
  });

  it('Stop from the widget: one confirm, no 2FA, specific toast', async () => {
    const user = userEvent.setup();
    const calls = await setupWide({
      'POST /api/bot/stop': { desired: 'stopped', kill_switch: true, cancelled: 3, errors: [], stopped_at: '2026-10-15T21:41:00Z', orders_cancelled: 3 },
    });
    fireOffscreen(true);
    const w = await screen.findByTestId('status-widget');
    await user.click(within(w).getByRole('button', { name: /stop the bot/i }));
    expect(screen.getByRole('button', { name: 'Stop now' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Stop now' }));
    expect(await screen.findByText(/^Stopped at .* · 3 Orders Cancelled$/)).toBeInTheDocument();
    const stop = calls.find((c) => c.key === 'POST /api/bot/stop');
    expect(stop.headers['X-2FA-Code']).toBeUndefined();
    expect(screen.queryByLabelText('6-digit code')).toBeNull();
  });

  it('the widget does not replace the header Stop or the card Stop', async () => {
    await setupWide();
    fireOffscreen(true);
    await screen.findByTestId('status-widget');
    const header = document.querySelector('header.appbar');
    expect(within(header).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
    expect(within(screen.getByTestId('status-card')).getByRole('button', { name: /stop the bot/i })).toBeInTheDocument();
    expect(document.querySelector('nav')).not.toBeNull();
    expect(screen.getByTestId('status-widget').closest('header, nav')).toBeNull();
  });

  it('tap expands it with a Back to Status button', async () => {
    const user = userEvent.setup();
    await setupWide();
    fireOffscreen(true);
    const w = await screen.findByTestId('status-widget');
    const scroll = vi.fn();
    screen.getByTestId('status-card').scrollIntoView = scroll;
    await user.click(within(w).getByRole('button', { name: /Running/ }));
    await user.click(within(w).getByRole('button', { name: 'Back to Status' }));
    expect(scroll).toHaveBeenCalled();
  });
});

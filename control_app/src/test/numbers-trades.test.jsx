import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { NA_TITLE } from '../utils/format.js';
import { ACCOUNT_POLL_MS } from '../components/AccountCard.jsx';

// Home account card, the owner's own open positions, the Account page and the balance chart label. All figures here
// are made up.

beforeEach(() => window.sessionStorage.clear());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const RUNNING = {
  ...STATUS,
  desired: 'running',
  kill_switch: false,
  mode: 'live',
  last_run_at: new Date(Date.now() - 4 * 60000).toISOString(),
  account: { cash: 101.5, exposure: 39.5 },
  limits: { max_open_frac: 0.4 },
};
const SUMMARY = {
  env: 'live',
  as_of: '2026-01-01T00:00:00+00:00',
  cash: 100,
  positions_cost: 40,
  positions_value: 37,
  total_value: 137,
  total_at_cost: 140,
  unrealized: -3,
};
const LEDGER = {
  env: 'live',
  strategies: [
    { strategy: 'maker', realized_pnl: 2, open_cost: 8.2, fills: 3, settled_fills: 2, settled_cost: 20 },
    // The range-filtered ledger figure for "you" must NOT be what the You row calls open.
    { strategy: 'you', realized_pnl: -4, open_cost: 99, fills: 3, settled_fills: 1, settled_cost: 10 },
  ],
  settled_by_strategy: [
    { strategy: 'maker', realized_pnl: 2 },
    { strategy: 'you', realized_pnl: -4 },
  ],
};
const COMBO_TICKER = 'KXMVEDEMOMULTIGAMEEXTENDED-S2026DEMO000000000-DEMO00000000000';
const row = (over) => ({
  kind: 'position', ticker: 'KXDEMOA-26JAN01-T1', title: 'Demo A', side: 'yes', count: 10, price: 0.4,
  cost: 4, legs: null, value: 4.5, payout_if_wins: 10, source: 'bot', strategy: 'maker', ...over,
});
const POSITIONS = [
  row({}),
  row({ ticker: 'KXDEMOB-26JAN01-T2', title: 'Demo B', cost: 6.25, value: 5, source: 'you', strategy: null }),
  row({ ticker: COMBO_TICKER, title: '10-leg combo', legs: 10, count: 100, price: 0.05, cost: 5, value: 3.5, payout_if_wins: 100, source: 'you', strategy: null }),
  row({ kind: 'order', ticker: 'KXDEMOC-26JAN01-T3', title: 'Demo C', cost: null, value: 3.6, source: 'you', strategy: null }),
];

const routes = (extra = {}) => ({
  'GET /api/status': RUNNING,
  'GET /api/history': { snapshots: [] },
  'GET /api/strategies': [200, []],
  'GET /api/analytics/ledger': LEDGER,
  'GET /api/account/summary': SUMMARY,
  'GET /api/account/positions': [200, POSITIONS],
  'GET /api/markets': { markets: [] },
  ...extra,
});

const acct = (id) => screen.getByTestId(`acct-${id}`);
const figure = (id) => acct(id).querySelector('dd');

describe('Home account card (/api/account/summary)', () => {
  it('shows total value, cash, market value, paid and unrealised P&L with the settlement words', async () => {
    stubApi(routes());
    renderApp('/');
    await waitFor(() => expect(figure('total')).toHaveTextContent('$137.00'));
    expect(figure('cash')).toHaveTextContent('$100.00');
    expect(figure('value')).toHaveTextContent('$37.00');
    expect(figure('paid')).toHaveTextContent('$40.00');
    expect(figure('unrealised')).toHaveTextContent('−$3.00');
    expect(figure('unrealised')).toHaveClass('neg');
    expect(acct('unrealised')).toHaveTextContent('not realised until settlement');
    expect(screen.getByTestId('home-account')).toHaveTextContent('From Kalshi (live)');
    for (const name of ['What is Market Value?', 'What is Paid for Positions?', 'What is Unrealised P&L?']) {
      expect(within(screen.getByTestId('home-account')).getByRole('button', { name })).toBeInTheDocument();
    }
    // The realised headline is still the ledger's settled figure for the bot (your own trades are not in it).
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('+$2.00');
  });

  it('null figures are dashes with "Not available yet", never zero or an estimate', async () => {
    stubApi(routes({ 'GET /api/account/summary': { ...SUMMARY, positions_value: null, total_value: null, unrealized: null } }));
    renderApp('/');
    await waitFor(() => expect(figure('cash')).toHaveTextContent('$100.00'));
    for (const id of ['total', 'value', 'unrealised']) {
      expect(figure(id)).toHaveTextContent('—');
      expect(figure(id)).toHaveAttribute('title', NA_TITLE);
      expect(figure(id)).not.toHaveTextContent('$');
    }
    expect(figure('paid')).toHaveTextContent('$40.00');
    expect(screen.getByTestId('home-account')).toHaveTextContent('A dash means not available yet.');
  });

  it.each([
    ['an older API without the route (404)', [404, { detail: 'Not Found' }]],
    ['Kalshi unavailable (503)', [503, { detail: 'kalshi_unavailable' }]],
    ['no keys (409)', [409, { detail: 'kalshi_not_configured' }]],
  ])('%s falls back to the snapshot cash and paid figures', async (_name, reply) => {
    stubApi(routes({ 'GET /api/account/summary': reply }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('home-account')).toHaveTextContent('From the last account snapshot.'));
    expect(figure('cash')).toHaveTextContent('$101.50');
    expect(figure('paid')).toHaveTextContent('$39.50');
    for (const id of ['total', 'value', 'unrealised']) expect(screen.queryByTestId(`acct-${id}`)).toBeNull();
    expect(screen.getByTestId('risk-line')).toHaveTextContent('$39.50 at risk');
  });

  it('with neither a live read nor a snapshot, shows dashes', async () => {
    stubApi(routes({ 'GET /api/status': { ...RUNNING, account: null }, 'GET /api/account/summary': [404, { detail: 'Not Found' }] }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('home-account')).toHaveTextContent('No account snapshot yet.'));
    expect(figure('cash')).toHaveTextContent('—');
    expect(figure('paid')).toHaveTextContent('—');
  });

  it('drops the live total when a later poll fails (an old total never looks current)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    stubApi(routes({ 'GET /api/account/summary': () => (++n === 1 ? SUMMARY : [503, { detail: 'kalshi_unavailable' }]) }));
    renderApp('/');
    await waitFor(() => expect(figure('total')).toHaveTextContent('$137.00'));
    await act(() => vi.advanceTimersByTimeAsync(ACCOUNT_POLL_MS));
    await waitFor(() => expect(screen.queryByTestId('acct-total')).toBeNull());
    expect(figure('cash')).toHaveTextContent('$101.50');
  });
});

describe('Home You row (live positions)', () => {
  it('counts your open positions (not orders) with paid and value now, keeps settled P&L and links to Account', async () => {
    stubApi(routes());
    renderApp('/');
    const line = await screen.findByTestId('manual-open');
    await waitFor(() => expect(line).toHaveTextContent('2 open positions'));
    expect(line).toHaveTextContent('Paid $11.25');
    expect(line).toHaveTextContent('Value now $8.50');
    expect(line).not.toHaveTextContent('$99.00');
    const manual = screen.getByTestId('manual-row');
    await waitFor(() => expect(manual).toHaveTextContent('−$4.00'));
    expect(manual).toHaveTextContent('Settled');
    expect(manual).toHaveTextContent('manual, not in the total');
    expect(within(manual).getByRole('link')).toHaveAttribute('href', '/account');
  });

  it('bot rows say the open figure is what was paid', async () => {
    stubApi(routes());
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('home-row-maker')).toHaveTextContent('Open, paid $8.20'));
  });

  it('a row without a paid figure (older API) gives a dash, not a smaller total', async () => {
    const old = POSITIONS.map(({ cost, legs, ...p }, i) => (i === 1 ? p : { ...p, cost }));
    stubApi(routes({ 'GET /api/account/positions': [200, old] }));
    renderApp('/');
    const line = await screen.findByTestId('manual-open');
    await waitFor(() => expect(line).toHaveTextContent('Paid —'));
    expect(line).toHaveTextContent('Value now $8.50');
  });

  it('says there are none when you hold nothing', async () => {
    stubApi(routes({ 'GET /api/account/positions': [200, [POSITIONS[0]]] }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('manual-open')).toHaveTextContent('No open positions'));
  });

  it('unreadable positions show a dash, never "none"', async () => {
    stubApi(routes({ 'GET /api/account/positions': [503, { detail: 'kalshi_unavailable' }] }));
    renderApp('/');
    await waitFor(() => expect(screen.getByTestId('manual-row')).toHaveTextContent('−$4.00'));
    expect(screen.getByTestId('manual-open')).toHaveTextContent('Open positions —');
    expect(screen.getByTestId('manual-open')).not.toHaveTextContent('No open positions');
  });
});

describe('Balance chart and the mode default', () => {
  const snapshots = [
    { ts: '2026-01-01T00:00:00Z', cash: 90, exposure: 10 },
    { ts: '2026-01-02T00:00:00Z', cash: 80, exposure: 25 },
  ];

  it('is labelled as balance with positions at cost, and asks for no env (server default = current mode)', async () => {
    const user = userEvent.setup();
    const calls = stubApi(routes({ 'GET /api/history': { snapshots } }));
    renderApp('/');
    const fold = await screen.findByRole('button', { name: /Balance, 30 Days/ });
    expect(fold).toHaveTextContent('$105.00 at cost');
    await user.click(fold);
    expect(screen.getByText(/Balance with positions at cost \(what was paid, not market value\)/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Balance with positions at cost, in dollars/ })).toBeInTheDocument();
    for (const c of calls.filter((x) => x.key === 'GET /api/history')) expect(c.path).not.toMatch(/env=/);
  });

  it('Activity asks for no env either', async () => {
    const calls = stubApi(routes({ 'GET /api/activity': { items: [] } }));
    renderApp('/activity');
    await screen.findByText('No activity yet.');
    const asked = calls.filter((x) => x.key === 'GET /api/activity');
    expect(asked.length).toBeGreaterThan(0);
    for (const c of asked) expect(c.path).not.toMatch(/env=/);
  });
});

describe('Account page', () => {
  const rowOf = (text) => screen.getByText(text).closest('li');

  it('shows paid and value now per position, and the plain value for a resting order', async () => {
    stubApi(routes());
    renderApp('/account');
    await screen.findByText('Demo B');
    expect(rowOf('Demo B')).toHaveTextContent('Paid $6.25');
    expect(rowOf('Demo B')).toHaveTextContent('Value now $5.00');
    expect(rowOf('Demo C')).toHaveTextContent('Value $3.60');
    expect(rowOf('Demo C')).not.toHaveTextContent('Paid');
    const totals = screen.getByTestId('pos-totals');
    expect(totals).toHaveTextContent('$15.25 Paid');
    expect(totals).toHaveTextContent('$13.00 Value now');
  });

  it('a combo row shows its short name and leg count once', async () => {
    stubApi(routes());
    renderApp('/account');
    const combo = (await screen.findByText('10-leg combo')).closest('li');
    expect(combo).toHaveTextContent(COMBO_TICKER);
    expect(combo.textContent.match(/10[- ]leg/g)).toHaveLength(1);
  });

  it('adds the leg count when the name does not say it, and names a combo with no title', async () => {
    stubApi(routes({ 'GET /api/account/positions': [200, [row({ title: 'Weekend parlay', legs: 3 }), row({ ticker: COMBO_TICKER, title: null, legs: 4 })]] }));
    renderApp('/account');
    expect((await screen.findByText('Weekend parlay')).closest('li')).toHaveTextContent('3 legs');
    expect(screen.getByText('4-leg combo')).toBeInTheDocument();
  });

  it('an unknown figure is a dash on the row and in the total', async () => {
    stubApi(routes({ 'GET /api/account/positions': [200, [row({}), row({ ticker: 'KXDEMOB-26JAN01-T2', title: 'Demo B', cost: null })]] }));
    renderApp('/account');
    await screen.findByText('Demo B');
    expect(rowOf('Demo B')).toHaveTextContent('Paid —');
    expect(screen.getByTestId('pos-totals')).toHaveTextContent('— Paid');
    expect(screen.getByTestId('pos-totals')).toHaveTextContent('$9.00 Value now');
  });
});

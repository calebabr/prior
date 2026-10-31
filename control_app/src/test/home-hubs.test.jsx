import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

beforeEach(() => window.sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const RUNNING = {
  ...STATUS,
  desired: 'running',
  kill_switch: false,
  mode: 'demo',
  last_run_at: new Date(Date.now() - 4 * 60000).toISOString(),
  account: { cash: 212.4, exposure: 37.6 },
  limits: { max_open_frac: 0.4 },
  arb: { state: 'demo', last_scan_utc: new Date(Date.now() - 10000).toISOString() },
};
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
const routes = (extra = {}) => ({
  'GET /api/status': RUNNING,
  'GET /api/analytics/ledger': LEDGER,
  'GET /api/account/positions': [200, []],
  'GET /api/markets': { markets: [] },
  ...extra,
});

describe('Strategies hub', () => {
  it('shows one read-only card per strategy and the Maker + Arbitrage total', async () => {
    stubApi(routes());
    renderApp('/strategies');
    const maker = await screen.findByTestId('strategy-card-maker');
    await waitFor(() => expect(maker).toHaveTextContent('+$17.12'));
    expect(screen.getByTestId('strategy-card-arb')).toHaveTextContent('+$2.40');
    expect(screen.queryByTestId('strategy-card-you')).toBeNull();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByTestId('hub-total')).toHaveTextContent('+$19.52');
    expect(screen.getByTestId('hub-total')).toHaveTextContent('not in the total');
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Strategies']);
  });

  it('degrades per source: a failed ledger gives dashes and a retry, the cards and run state still render', async () => {
    stubApi(routes({ 'GET /api/analytics/ledger': [500, { detail: 'boom' }] }));
    renderApp('/strategies');
    expect(await screen.findByTestId('hub-degraded')).toHaveTextContent("Couldn't load P&L");
    const maker = screen.getByTestId('strategy-card-maker');
    expect(maker).toHaveTextContent('Demo running');
    expect(maker).toHaveTextContent('—');
    expect(maker).not.toHaveTextContent('$0.00');
    expect(screen.getByTestId('strategy-card-arb')).toBeInTheDocument();
  });
});

describe('Performance hub', () => {
  it('opens with the All strategies comparison and links to Analytics, Shadow and Periods', async () => {
    stubApi(routes());
    renderApp('/performance');
    const table = await screen.findByTestId('perf-table');
    await waitFor(() => expect(within(table).getByTestId('perf-row-maker')).toHaveTextContent('+$17.12'));
    expect(within(table).getByTestId('perf-row-maker')).toHaveTextContent('+4.6%');
    expect(within(table).getByTestId('perf-total')).toHaveTextContent('+$19.52');
    expect(within(table).getByTestId('perf-row-you')).toHaveTextContent('not in the total');
    const links = within(screen.getByTestId('perf-links')).getAllByRole('link');
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/analytics', '/shadow', '/performance/periods']);
    expect(screen.queryByRole('link', { name: /Analysis/ })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Performance']);
  });

  it('the Periods link opens the Periods page', async () => {
    const user = userEvent.setup();
    stubApi(routes({ 'GET /api/periods': { periods: [], cash_flows: [] } }));
    renderApp('/performance');
    await user.click(await screen.findByRole('link', { name: /Periods/ }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Periods' })).toBeInTheDocument();
  });
});

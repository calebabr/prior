import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { renderApp, STATUS } from './helpers.jsx';
import LedgerView from '../components/analytics/LedgerView.jsx';

const RUNNING = {
  ...STATUS,
  desired: 'running',
  kill_switch: false,
  mode: 'demo',
  heartbeat_age_s: 60,
  account: { cash: 212.4, exposure: 37.6, headroom: 87.4 },
  limits: { run_budget: 25, max_open_frac: 0.4 },
  pnl_total: { d7: 13.58, d30: 13.58 },
  pnl_today: { maker: 0, arb: 0, you: 0, total: 0 },
  recent_passes: [],
};
const STRATS = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)' },
];
// Fill-date rows (-3.64 / +12.56) differ from the settlement-date figures (+0.18 / +13.40) on purpose.
const LEDGER = {
  env: 'demo',
  strategies: [
    { strategy: 'maker', realized_pnl: -3.64, roi_on_settled: null, open_cost: 8.2, fills: 9, settled_fills: 6, settled_cost: 372 },
    { strategy: 'arb', realized_pnl: 12.56, roi_on_settled: null, open_cost: 0, fills: 2, settled_fills: 2, settled_cost: 218 },
  ],
  settled_by_strategy: [
    { strategy: 'maker', realized_pnl: 0.18 },
    { strategy: 'arb', realized_pnl: 13.4 },
    { strategy: 'total', realized_pnl: 13.58 },
  ],
};

function stub(ledger) {
  const other = {
    'GET /api/status': RUNNING,
    'GET /api/history': { snapshots: [] },
    'GET /api/strategies': STRATS,
    'GET /api/account/positions': [],
    'GET /api/markets': { markets: [] },
    'GET /api/analytics/ledger': ledger,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn((url, init = {}) => {
      const key = `${init.method || 'GET'} ${String(url).replace('http://api.test', '').split('?')[0]}`;
      const body = key in other ? other[key] : { detail: 'not found' };
      const status = key in other ? 200 : 404;
      return Promise.resolve({ ok: status < 300, status, headers: { get: () => null }, text: () => Promise.resolve(JSON.stringify(body)) });
    }),
  );
}
beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe('Home rows use the settlement-date rule', () => {
  it('rows come from settled_by_strategy and add up to the headline', async () => {
    stub(LEDGER);
    renderApp('/');
    const maker = await screen.findByTestId('home-row-maker');
    const arb = screen.getByTestId('home-row-arb');
    expect(within(maker).getByText('+$0.18')).toBeInTheDocument();
    expect(within(arb).getByText('+$13.40')).toBeInTheDocument();
    expect(within(maker).queryByText('−$3.64')).toBeNull();
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('+$13.58');
    expect(0.18 + 13.4).toBeCloseTo(13.58, 2);
    expect(within(maker).getByText(/Open/)).toHaveTextContent('$8.20'); // open cost unchanged
  });

  it('an API without settled_by_strategy shows a dash, never the fill-date number', async () => {
    const { settled_by_strategy: _drop, ...old } = LEDGER;
    stub(old);
    renderApp('/');
    const maker = await screen.findByTestId('home-row-maker');
    expect(within(maker).queryByText('−$3.64')).toBeNull();
    expect(within(maker).getAllByText('—').length).toBeGreaterThan(0);
    expect(within(screen.getByTestId('home-row-arb')).queryByText('+$12.56')).toBeNull();
  });
});

describe('Ledger tab shows both attributions', () => {
  it('fill-day and settlement-day P&L side by side with the explanation', async () => {
    stub(LEDGER);
    render(<LedgerView days={7} env="demo" />);
    const table = await screen.findByTestId('ledger-table');
    expect(within(table).getByText(/by fill day/)).toBeInTheDocument();
    expect(within(table).getByText(/by settlement day/)).toBeInTheDocument();
    expect(within(table).getByText('−$3.64')).toBeInTheDocument();
    expect(screen.getByTestId('ledger-settled-maker')).toHaveTextContent('+$0.18');
    expect(screen.getByTestId('ledger-settled-arb')).toHaveTextContent('+$13.40');
    expect(screen.getByTestId('ledger-attribution')).toHaveTextContent(/day the market settled/);
  });

  it('missing settled_by_strategy shows not available in the settlement column', async () => {
    const { settled_by_strategy: _drop, ...old } = LEDGER;
    stub(old);
    render(<LedgerView days={7} env="demo" />);
    await screen.findByTestId('ledger-table');
    expect(screen.getByTestId('ledger-settled-maker')).toHaveTextContent('not available');
  });
});


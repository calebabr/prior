import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, STATUS } from './helpers.jsx';
import { ApiError, RETRY_POLICY, isRetryable, withRetry } from '../services/api.js';
import { headlineTotal } from '../utils/overview.js';

const RUNNING = {
  ...STATUS,
  desired: 'running',
  kill_switch: false,
  mode: 'demo',
  heartbeat_age_s: 60,
  account: { cash: 212.4, exposure: 37.6, headroom: 87.4 },
  limits: { run_budget: 25, max_open_frac: 0.4 },
  pnl: { d7: 4.83, d30: 17.12 },
  pnl_total: { d7: 5.73, d30: 19.52 },
  pnl_today: { maker: 1.4, arb: 0.35, you: -0.8, total: 0.95 },
  recent_passes: [],
};
const STRATS = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)' },
];
const LEDGER = {
  env: 'demo',
  strategies: [
    { strategy: 'maker', realized_pnl: 17.12, roi_on_settled: 0.046, open_cost: 8.2, fills: 9, settled_fills: 6, settled_cost: 372 },
    { strategy: 'arb', realized_pnl: 2.4, roi_on_settled: 0.011, open_cost: 0, fills: 2, settled_fills: 2, settled_cost: 218 },
  ],
  settled_by_strategy: [
    { strategy: 'maker', realized_pnl: 17.12 },
    { strategy: 'arb', realized_pnl: 2.4 },
  ],
};

/** Own fetch stub: a route returns { status, body, headers } or a Promise of one. Counts ledger calls. */
function stub(ledger) {
  const calls = { ledger: 0 };
  const other = {
    'GET /api/status': RUNNING,
    'GET /api/history': { snapshots: [] },
    'GET /api/strategies': STRATS,
    'GET /api/account/positions': [],
    'GET /api/markets': { markets: [] },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn((url, init = {}) => {
      const path = String(url).replace('http://api.test', '').split('?')[0];
      const key = `${init.method || 'GET'} ${path}`;
      let r;
      if (key === 'GET /api/analytics/ledger') {
        calls.ledger += 1;
        r = ledger(calls.ledger);
      } else if (key in other) r = { status: 200, body: other[key] };
      else r = { status: 404, body: { detail: 'not found' } };
      return Promise.resolve(r).then(({ status = 200, body, headers = {} }) => ({
        ok: status < 300,
        status,
        headers: { get: (n) => headers[n] ?? null },
        text: () => Promise.resolve(JSON.stringify(body)),
      }));
    }),
  );
  return calls;
}

const policy = { ...RETRY_POLICY };
beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  RETRY_POLICY.jitterMs = 0;
  RETRY_POLICY.defaultS = 0.02;
});
afterEach(() => {
  vi.unstubAllGlobals();
  Object.assign(RETRY_POLICY, policy);
});

describe('Home headline does not wait for the ledger', () => {
  it('shows the 30-day total from /api/status at once; the breakdown loads second', async () => {
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    stub(() => gate.then(() => ({ body: LEDGER })));
    renderApp('/');
    const fig = await screen.findByTestId('home-total-figure');
    expect(fig).toHaveTextContent('+$19.52'); // pnl_total.d30, ledger still pending
    expect(screen.queryByTestId('home-row-maker')).toBeNull();
    expect(within(screen.getByTestId('by-strategy')).getByTestId('skeleton')).toBeInTheDocument();
    expect(screen.queryByTestId('home-ledger-error')).toBeNull();
    release();
    expect(await screen.findByTestId('home-row-maker')).toBeInTheDocument();
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('+$19.52'); // cheap figure kept for 7/30
  });

  it('a failed ledger leaves the headline and shows a section error with Retry that recovers', async () => {
    const calls = stub((n) => (n === 1 ? { status: 500, body: { detail: 'boom' } } : { body: LEDGER }));
    renderApp('/');
    const err = await screen.findByTestId('home-ledger-error');
    expect(err).toHaveTextContent("Couldn't load P&L by strategy");
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('+$19.52');
    expect(within(screen.getByTestId('home-row-maker')).queryByText('$0.00')).toBeNull();
    expect(calls.ledger).toBe(1); // a 500 is not retried
    await userEvent.click(within(err).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByTestId('home-ledger-error')).toBeNull());
    expect(within(await screen.findByTestId('home-row-maker')).getByText('+$17.12')).toBeInTheDocument();
    expect(calls.ledger).toBe(2);
  });
});

describe('automatic retry of the ledger', () => {
  it('retries 429 analytics_busy honoring Retry-After and never shows the error', async () => {
    const calls = stub((n) =>
      n < 3 ? { status: 429, body: { detail: 'analytics_busy' }, headers: { 'Retry-After': '0' } } : { body: LEDGER },
    );
    renderApp('/');
    expect(await screen.findByTestId('home-row-maker')).toBeInTheDocument();
    expect(calls.ledger).toBe(3); // first try + two retries
    expect(screen.queryByTestId('home-ledger-error')).toBeNull();
  });

  it('retries 503 store_unavailable, then gives up after two retries with the manual Retry', async () => {
    const calls = stub(() => ({ status: 503, body: { detail: 'store_unavailable' }, headers: { 'Retry-After': '0' } }));
    renderApp('/');
    expect(await screen.findByTestId('home-ledger-error')).toBeInTheDocument();
    expect(calls.ledger).toBe(3);
    expect(screen.getByTestId('home-total-figure')).toHaveTextContent('+$19.52'); // headline unaffected
  });

  it('keeps the section loading (no error) while it waits to retry', async () => {
    RETRY_POLICY.defaultS = 0.3;
    const calls = stub((n) =>
      n === 1 ? { status: 429, body: { detail: 'analytics_busy' } } : { body: LEDGER },
    );
    renderApp('/');
    await screen.findByTestId('home-total-figure');
    await waitFor(() => expect(calls.ledger).toBe(1));
    expect(screen.queryByTestId('home-ledger-error')).toBeNull();
    expect(screen.queryByTestId('home-row-maker')).toBeNull();
    expect(await screen.findByTestId('home-row-maker', undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(calls.ledger).toBe(2);
  });

  it('does not retry a 400 or any other 4xx', async () => {
    const calls = stub(() => ({ status: 400, body: { detail: 'bad' } }));
    renderApp('/');
    expect(await screen.findByTestId('home-ledger-error')).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 150));
    expect(calls.ledger).toBe(1);
  });
});

describe('withRetry / headlineTotal', () => {
  const busy = (ra) => Object.assign(new ApiError(429, 'analytics_busy'), { retryAfter: ra });

  it('only analytics_busy and store_unavailable are retryable', () => {
    expect(isRetryable(busy(1))).toBe(true);
    expect(isRetryable(new ApiError(503, 'store_unavailable'))).toBe(true);
    for (const e of [new ApiError(429, 'rate_limited'), new ApiError(400, 'x'), new ApiError(422, 'x'), new ApiError(500, 'x'), new ApiError(0, null)]) {
      expect(isRetryable(e)).toBe(false);
    }
  });

  it('waits Retry-After (+ bounded jitter) and stops after two retries', async () => {
    RETRY_POLICY.jitterMs = 40;
    const run = vi.fn(() => Promise.reject(busy(0)));
    const t0 = performance.now();
    await expect(withRetry(run)).rejects.toMatchObject({ detail: 'analytics_busy' });
    expect(run).toHaveBeenCalledTimes(3);
    expect(performance.now() - t0).toBeLessThan(1000);
  });

  it('uses the default delay without Retry-After, and caps a long one', async () => {
    const spy = vi.spyOn(globalThis, 'setTimeout');
    RETRY_POLICY.defaultS = 0.01;
    const run = vi.fn().mockRejectedValueOnce(busy(null)).mockResolvedValueOnce('ok');
    await expect(withRetry(run)).resolves.toBe('ok');
    const waits = spy.mock.calls.map((c) => c[1]).filter((n) => typeof n === 'number');
    expect(waits).toContain(10);
    spy.mockRestore();
  });

  it('headline: 7d/30d from status, This period and missing figures from the ledger', () => {
    const s = { pnl_total: { d7: 1.5, d30: 9 } };
    expect(headlineTotal(s, '7d', 99)).toEqual({ pnl: 1.5, source: 'status' });
    expect(headlineTotal(s, '30d', 99)).toEqual({ pnl: 9, source: 'status' });
    expect(headlineTotal(s, 'current', 4)).toEqual({ pnl: 4, source: 'ledger' });
    expect(headlineTotal({ pnl_total: null }, '30d', 4)).toEqual({ pnl: 4, source: 'ledger' });
    expect(headlineTotal(s, 'current', null)).toEqual({ pnl: null, source: null });
    expect(headlineTotal(null, '30d', null)).toEqual({ pnl: null, source: null });
  });
});

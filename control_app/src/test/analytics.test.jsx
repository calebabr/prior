import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { MOCK_CELLS, analyticsSummary, analyticsTrades, mockMyTrades, wilson } from '../services/mockAnalytics.js';
import { curveFor } from '../components/analytics/PnlCurve.jsx';
import WinRateCiBar from '../components/analytics/WinRateCiBar.jsx';
import { MIN_N } from '../utils/analytics.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const NOW = Date.parse('2026-10-13T18:00:00Z');
const qs = (call) => new URL(call.path, 'http://x').searchParams;

// Contract-shaped responses from the same generator `npm run dev:mock` uses.
const mockSummary = (call) => analyticsSummary(qs(call), { mode: 'demo', nowMs: NOW });
const mockTrades = (call) => analyticsTrades(qs(call), { mode: 'demo', nowMs: NOW });
const mockMine = (call) => mockMyTrades(qs(call), { nowMs: NOW });

const EMPTY = {
  env: 'demo', days: 30, generated_utc: '2026-10-13T18:00:00Z', truncated: false,
  totals: { settled_fills: 0, contracts: 0, cost: 0, pnl: 0, roi: null, win_rate: null, open_fills: 0, max_drawdown: 0 },
  pnl_curve: [], by_cell: [], by_series: [], by_band: [], by_ttc: [], fill_quality: [],
  arb: { opportunities: 0, executed: 0, pnl: null, daily: [] },
};

function group(key, n, wins, extra = {}) {
  const [lo, hi] = wilson(wins, n);
  return {
    key, n, wins, contracts: n * 5, cost: n * 4.6, pnl: 1.5, roi: 0.01, avg_price: 0.42,
    win_rate: wins / n, win_ci_low: lo, win_ci_high: hi,
    edge: wins / n - 0.42, edge_ci_low: lo - 0.42, edge_ci_high: hi - 0.42, ...extra,
  };
}

function base(extra = {}) {
  return stubApi({
    'GET /api/status': STATUS,
    'GET /api/analytics/summary': mockSummary,
    'GET /api/analytics/trades': mockTrades,
    'GET /api/analytics/my-trades': mockMine,
    ...extra,
  });
}

const calls = (all, key) => all.filter((c) => c.key === key);

describe('Analytics page', () => {
  it('renders totals, chart, edge rows, fill quality, arb and trades from mock data', async () => {
    base();
    renderApp('/analytics');
    expect(await screen.findByRole('heading', { name: 'Analytics' })).toBeInTheDocument();
    const totals = await screen.findByTestId('totals');
    expect(within(totals).getByText('Return')).toBeInTheDocument();
    expect(within(totals).getByText('Max Drawdown')).toBeInTheDocument();
    expect(screen.getByTestId('pnl-curve')).toBeInTheDocument();
    expect(await screen.findByTestId('bot-vs-you')).toHaveTextContent(/Bot.*You/);
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Edge' }));
    expect((await screen.findAllByTestId('edge-row')).length).toBeGreaterThanOrEqual(MOCK_CELLS.length);   // one per cell
    await user.click(screen.getByRole('tab', { name: 'Execution' }));
    expect(await screen.findByRole('table', { name: /fill quality by week/i })).toBeInTheDocument();
    expect(screen.getByText('Opportunities')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Trades' }));
    expect(await screen.findAllByTestId('trade-row')).toHaveLength(50);
    // Help icons link into the glossary.
    expect(screen.getAllByRole('button', { name: /what is/i }).length).toBeGreaterThan(5);
    // Read-only page: no control actions besides the always-present Stop in the header.
    const main = document.querySelector('main');
    expect(within(main).queryByRole('button', { name: /^start/i })).toBeNull();
  });

  it('defaults to 30 days in the current mode and refetches when the window or account changes', async () => {
    const user = userEvent.setup();
    const all = base();
    renderApp('/analytics');
    await screen.findByTestId('totals');
    const first = calls(all, 'GET /api/analytics/summary')[0];
    expect(qs(first).get('days')).toBe('30');
    expect(qs(first).has('env')).toBe(false);

    await user.click(screen.getByRole('radio', { name: '90 days' }));
    await waitFor(() => expect(calls(all, 'GET /api/analytics/summary').some((c) => qs(c).get('days') === '90')).toBe(true));
    await user.click(screen.getByRole('tab', { name: 'Trades' }));
    await waitFor(() => expect(calls(all, 'GET /api/analytics/trades').some((c) => qs(c).get('days') === '90')).toBe(true));

    await user.click(screen.getByRole('button', { name: 'Demo + Live' }));
    await waitFor(() =>
      expect(calls(all, 'GET /api/analytics/summary').some((c) => qs(c).get('env') === 'all' && qs(c).get('days') === '90')).toBe(true),
    );
    expect(await screen.findByText(/Demo and live accounts, settled in the last 90 days/)).toBeInTheDocument();
  });

  it('sends trade filters to the API and resets to the first page', async () => {
    const user = userEvent.setup();
    const all = base();
    renderApp('/analytics?tab=trades');
    await screen.findAllByTestId('trade-row');
    await user.click(screen.getByRole('radio', { name: '180 days' }));
    await user.click(screen.getByText(/^Filters/));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Series' }), 'KXDEMOB');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Result' }), 'loss');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Maker or Taker' }), 'maker');
    await waitFor(() => {
      const last = calls(all, 'GET /api/analytics/trades').at(-1);
      const q = qs(last);
      expect(q.get('series')).toBe('KXDEMOB');
      expect(q.get('result')).toBe('loss');
      expect(q.get('kind')).toBe('maker');
      expect(q.get('offset')).toBe('0');
      expect(q.get('days')).toBe('180');
      expect(q.has('side')).toBe(false);
    });
    expect(screen.getByText('Filters (3 on)')).toBeInTheDocument();
    const rows = await screen.findAllByTestId('trade-row');
    rows.forEach((r) => expect(within(r).getByText('Lost')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(qs(calls(all, 'GET /api/analytics/trades').at(-1)).has('series')).toBe(false));
  });

  it('Load more requests the next page and appends it', async () => {
    const user = userEvent.setup();
    const all = base();
    renderApp('/analytics?tab=trades');
    expect(await screen.findAllByTestId('trade-row')).toHaveLength(50);
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(screen.getAllByTestId('trade-row')).toHaveLength(100));
    const last = calls(all, 'GET /api/analytics/trades').at(-1);
    expect(qs(last).get('offset')).toBe('50');
    expect(qs(last).get('limit')).toBe('50');
    expect(screen.getByText(/Showing 100 of/)).toBeInTheDocument();
  });

  it('shows "No settled trades yet" for an empty window', async () => {
    base({
      'GET /api/analytics/summary': EMPTY,
      'GET /api/analytics/trades': { total: 0, offset: 0, limit: 50, trades: [] },
    });
    renderApp('/analytics');
    expect(await screen.findByText('No settled trades yet.')).toBeInTheDocument();
    expect(screen.queryByTestId('pnl-curve')).toBeNull();
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Execution' }));
    expect(screen.getByText('No arbitrage opportunities in this window.')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Trades' }));
    expect(await screen.findByText('No trades in this window yet.')).toBeInTheDocument();
  });

  it('shows the database-down message when the store is unavailable', async () => {
    base({
      'GET /api/analytics/summary': [503, { detail: 'store_unavailable' }],
      'GET /api/analytics/trades': [503, { detail: 'store_unavailable' }],
    });
    renderApp('/analytics');
    // The header database banner can appear a tick before the page's own error.
    await waitFor(() =>
      expect(screen.getAllByRole('alert').some((a) => /can't reach its database/i.test(a.textContent))).toBe(true),
    );
  });

  it('shows a retryable error on a server error and recovers', async () => {
    const user = userEvent.setup();
    let fail = true;
    base({
      'GET /api/analytics/summary': (call) => (fail ? [500, { detail: 'boom' }] : mockSummary(call)),
    });
    renderApp('/analytics');
    const alert = await screen.findByText('boom');
    fail = false;
    await user.click(within(alert.closest('[role="alert"]')).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('totals')).toBeInTheDocument();
  });

  it(`marks groups with fewer than ${MIN_N} fills as too few to judge`, async () => {
    const summary = {
      ...EMPTY,
      totals: { ...EMPTY.totals, settled_fills: 205, pnl: 3, roi: 0.01, win_rate: 0.6 },
      pnl_curve: [
        { date: '2026-10-12', pnl: 1, cum_pnl: 1, drawdown: 0, maker_pnl: 1, taker_pnl: 0 },
        { date: '2026-10-13', pnl: 2, cum_pnl: 3, drawdown: 0, maker_pnl: 2, taker_pnl: 0 },
      ],
      by_cell: [
        { ...group('0.35-0.50|3-9h|yes', 5, 5), cell: '0.35-0.50|3-9h|yes', in_config: true },
        { ...group('0.50-0.62|9-30h|no', 200, 196), cell: '0.50-0.62|9-30h|no', in_config: true },
      ],
    };
    base({ 'GET /api/analytics/summary': summary });
    renderApp('/analytics?tab=edge');
    const rows = await screen.findAllByTestId('edge-row');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute('data-thin', 'true');
    expect(within(rows[0]).getByText('Too Few to Judge')).toBeInTheDocument();
    expect(rows[1]).toHaveAttribute('data-thin', 'false');
    expect(within(rows[1]).queryByText('Too Few to Judge')).toBeNull();
    expect(within(rows[1]).getByText(/50-62¢/)).toBeInTheDocument();
  });

  it('lives under the Performance tab (stage 2: Analytics is no longer a tab of its own)', async () => {
    base();
    renderApp('/analytics');
    expect(await screen.findByRole('heading', { name: 'Analytics' })).toBeInTheDocument();
    const main = screen.getByRole('navigation', { name: 'Main' });
    expect(within(main).getByRole('link', { name: 'Performance' })).toHaveClass('active');
    expect(within(main).queryByRole('link', { name: 'Analytics' })).toBeNull();
  });
});

describe('analytics pieces', () => {
  it('curveFor recomputes maker-only cumulative P&L and drawdown', () => {
    const pts = [
      { date: 'a', cum_pnl: 5, drawdown: 0, maker_pnl: 5, taker_pnl: 0 },
      { date: 'b', cum_pnl: 1, drawdown: -4, maker_pnl: -6, taker_pnl: 2 },
      { date: 'c', cum_pnl: 4, drawdown: -1, maker_pnl: 3, taker_pnl: 0 },
    ];
    expect(curveFor(pts, 'all').map((p) => p.dd)).toEqual([0, -4, -1]);
    expect(curveFor(pts, 'maker')).toEqual([
      { date: 'a', cum: 5, dd: 0 },
      { date: 'b', cum: -1, dd: -6 },
      { date: 'c', cum: 2, dd: -3 },
    ]);
  });

  it('WinRateCiBar colours by where the CI sits relative to the price paid', () => {
    const { container, rerender } = render(<WinRateCiBar winRate={0.57} low={0.54} high={0.59} avgPrice={0.52} />);
    expect(container.querySelector('svg')).toHaveAttribute('data-verdict', 'above');
    rerender(<WinRateCiBar winRate={0.45} low={0.4} high={0.49} avgPrice={0.52} />);
    expect(container.querySelector('svg')).toHaveAttribute('data-verdict', 'below');
    rerender(<WinRateCiBar winRate={0.53} low={0.45} high={0.57} avgPrice={0.52} />);
    expect(container.querySelector('svg')).toHaveAttribute('data-verdict', 'straddles');
    rerender(<WinRateCiBar winRate={null} low={0.45} high={0.57} avgPrice={0.52} />);
    expect(container.querySelector('svg')).toBeNull();
  });

  it('mock trades endpoint honours filters, pagination and rejects bad values', () => {
    const p = (s) => new URLSearchParams(s);
    const [st, page1] = analyticsTrades(p('days=180&limit=20'), { nowMs: NOW });
    expect(st).toBe(200);
    expect(page1.trades).toHaveLength(20);
    expect(page1.total).toBeGreaterThan(300);
    const [, page2] = analyticsTrades(p('days=180&limit=20&offset=20'), { nowMs: NOW });
    expect(page2.trades[0].fill_id).not.toBe(page1.trades[0].fill_id);
    const [, f] = analyticsTrades(p('days=180&series=KXDEMOC&kind=maker&status=settled&result=win&limit=200'), { nowMs: NOW });
    expect(f.trades.length).toBeGreaterThan(0);
    f.trades.forEach((t) => {
      expect(t.series).toBe('KXDEMOC');
      expect(t.kind).toBe('maker');
      expect(t.result).toBe('win');
    });
    expect(analyticsTrades(p('side=maybe'), { nowMs: NOW })[0]).toBe(422);
    expect(analyticsTrades(p('limit=500'), { nowMs: NOW })[0]).toBe(422);
    expect(analyticsSummary(p('days=12'), { nowMs: NOW })[0]).toBe(422);
  });

  it('mock summary has a drawdown, losses, arb days and several hundred fills over 180 days', () => {
    const [, s] = analyticsSummary(new URLSearchParams('days=180'), { nowMs: NOW });
    expect(s.totals.settled_fills).toBeGreaterThan(300);
    expect(s.totals.max_drawdown).toBeLessThan(0);
    expect(s.totals.win_rate).toBeLessThan(1);
    expect(s.arb.executed).toBeGreaterThan(0);
    expect(s.by_series.map((g) => g.key)).toEqual(expect.arrayContaining(['KXDEMOA', 'KXDEMOB', 'KXDEMOF']));
    for (const key of ['by_cell', 'by_series', 'by_band', 'by_ttc']) {
      for (const g of s[key]) {
        expect(Object.keys(g)).toEqual(expect.arrayContaining(['key', 'n', 'wins', 'win_ci_low', 'win_ci_high', 'edge', 'edge_ci_low', 'edge_ci_high', 'roi', 'avg_price']));
      }
    }
    expect(s.by_cell.every((g) => 'cell' in g && 'in_config' in g)).toBe(true);
  });
});

// ---------------------------------------------------------------- S-28: abort, debounce, stagger, 429
// A fetch stub that keeps each request's AbortSignal and can hold responses open.
function deferredFetch(handlers) {
  const reqs = [];
  const fn = vi.fn((url, init = {}) => {
    const path = String(url).replace('http://api.test', '');
    const key = `${init.method || 'GET'} ${path.split('?')[0]}`;
    const req = { key, path, q: qs({ path }), signal: init.signal, resolve: null };
    reqs.push(req);
    const respond = ([status, body, headers = {}]) => ({
      ok: status < 300,
      status,
      text: () => Promise.resolve(JSON.stringify(body)),
      headers: { get: (h) => headers[h] ?? headers[h.toLowerCase()] ?? null },
    });
    const h = handlers[key];
    const out = h ? h(req) : [404, { detail: 'not found' }];
    if (out === 'hold') {
      return new Promise((resolve) => {
        req.resolve = (r) => resolve(respond(Array.isArray(r) ? r : [200, r]));
      });
    }
    return Promise.resolve(respond(Array.isArray(out) ? out : [200, out]));
  });
  vi.stubGlobal('fetch', fn);
  return reqs;
}

const tradesOf = (reqs) => reqs.filter((r) => r.key === 'GET /api/analytics/trades');
const summariesOf = (reqs) => reqs.filter((r) => r.key === 'GET /api/analytics/summary');
const tradesFor = (req) => mockTrades({ path: req.path });

describe('Analytics request hygiene (S-28)', () => {
  it('rapid filter changes are debounced; a superseded request is aborted and never reaches the UI', async () => {
    const user = userEvent.setup();
    const reqs = deferredFetch({
      'GET /api/status': () => STATUS,
      'GET /api/analytics/summary': (r) => mockSummary({ path: r.path }),
      'GET /api/analytics/trades': () => 'hold',
      'GET /api/analytics/my-trades': (r) => mockMine({ path: r.path }),
    });
    renderApp('/analytics?tab=trades');
    await waitFor(() => expect(tradesOf(reqs)).toHaveLength(1));
    tradesOf(reqs)[0].resolve(tradesFor(tradesOf(reqs)[0]));
    expect(await screen.findAllByTestId('trade-row')).toHaveLength(50);

    // Three quick changes -> one request with the final combination.
    await user.click(screen.getByText(/^Filters/));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Result' }), 'loss');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Result' }), 'win');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Side' }), 'no');
    await waitFor(() => expect(tradesOf(reqs)).toHaveLength(2));
    const second = tradesOf(reqs)[1];
    expect(second.q.get('result')).toBe('win');
    expect(second.q.get('side')).toBe('no');
    expect(tradesOf(reqs).some((r) => r.q.get('result') === 'loss')).toBe(false);

    // A further change while the second is in flight aborts it.
    await user.selectOptions(screen.getByRole('combobox', { name: 'Result' }), 'loss');
    await waitFor(() => expect(tradesOf(reqs)).toHaveLength(3));
    const third = tradesOf(reqs)[2];
    expect(second.signal.aborted).toBe(true);
    expect(third.signal.aborted).toBe(false);

    // The aborted one answering late changes nothing; only the last one renders.
    second.resolve({ total: 1, offset: 0, limit: 50, trades: [{ ...tradesFor(second)[1].trades[0], ticker: 'STALE-TICKER' }] });
    third.resolve(tradesFor(third));
    const rows = await screen.findAllByTestId('trade-row');
    rows.forEach((r) => expect(within(r).getByText('Lost')).toBeInTheDocument());
    expect(screen.queryByText('STALE-TICKER')).toBeNull();
    expect(screen.queryAllByRole('alert')).toHaveLength(0); // aborts are not errors
  });

  it('the trade list waits for the summary of the same window; a window change aborts the old summary', async () => {
    const user = userEvent.setup();
    const reqs = deferredFetch({
      'GET /api/status': () => STATUS,
      'GET /api/analytics/summary': () => 'hold',
      'GET /api/analytics/trades': (r) => tradesFor(r),
      'GET /api/analytics/my-trades': (r) => mockMine({ path: r.path }),
    });
    renderApp('/analytics?tab=trades');
    await waitFor(() => expect(summariesOf(reqs)).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(tradesOf(reqs)).toHaveLength(0); // not fired alongside the summary

    await user.click(screen.getByRole('radio', { name: '180 days' }));
    await waitFor(() => expect(summariesOf(reqs)).toHaveLength(2));
    expect(summariesOf(reqs)[0].signal.aborted).toBe(true);
    expect(tradesOf(reqs)).toHaveLength(0);

    const s180 = summariesOf(reqs)[1];
    s180.resolve(mockSummary({ path: s180.path }));
    expect(await screen.findByText(/settled in the last 180 days/)).toBeInTheDocument();
    await waitFor(() => expect(tradesOf(reqs)).toHaveLength(1));
    expect(tradesOf(reqs)[0].q.get('days')).toBe('180');
    expect(screen.queryAllByRole('alert')).toHaveLength(0);
  });

  it('leaving the page aborts requests in flight', async () => {
    const reqs = deferredFetch({
      'GET /api/status': () => STATUS,
      'GET /api/analytics/summary': () => 'hold',
      'GET /api/analytics/trades': () => 'hold',
    });
    const { unmount } = renderApp('/analytics');
    await waitFor(() => expect(summariesOf(reqs)).toHaveLength(1));
    unmount();
    expect(summariesOf(reqs)[0].signal.aborted).toBe(true);
  });

  it('429 rate_limited shows "Too many requests" with the Retry-After seconds', async () => {
    deferredFetch({
      'GET /api/status': () => STATUS,
      'GET /api/analytics/summary': () => [429, { detail: 'rate_limited' }, { 'Retry-After': '12' }],
      'GET /api/analytics/trades': () => [429, { detail: 'rate_limited' }],
      'GET /api/analytics/my-trades': (r) => mockMine({ path: r.path }),
    });
    renderApp('/analytics?tab=trades');
    expect(await screen.findByText('Too many requests — try again in 12 s.')).toBeInTheDocument();
    expect(await screen.findByText('Too many requests — try again shortly.')).toBeInTheDocument();
    expect(screen.queryByText('Loading analytics…')).toBeNull();
  });
});

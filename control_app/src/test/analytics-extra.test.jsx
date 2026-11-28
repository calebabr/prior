import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { setToken } from '../services/api.js';
import { analyticsSummary, analyticsTrades, mockMyTrades, wilson } from '../services/mockAnalytics.js';
import PnlCurve, { curveFor } from '../components/analytics/PnlCurve.jsx';
import WinRateCiBar, { ciDomain } from '../components/analytics/WinRateCiBar.jsx';
import HelpIcon from '../components/HelpIcon.jsx';
import {
  MIN_N, bandLabel, cents, isNum, parseCell, points, rate, seriesName, shortDate, signCls, ttcLabel, weekLabel,
} from '../utils/analytics.js';
import { checkSummary, checkTrades } from './contractShape.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const NOW = Date.parse('2026-10-13T18:00:00Z');
const qs = (call) => new URL(call.path, 'http://x').searchParams;
const mockSummary = (call) => analyticsSummary(qs(call), { mode: 'demo', nowMs: NOW });
const mockTrades = (call) => analyticsTrades(qs(call), { mode: 'demo', nowMs: NOW });
const mockMine = (call) => mockMyTrades(qs(call), { nowMs: NOW });

const EMPTY = {
  env: 'demo', days: 30, generated_utc: '2026-10-13T18:00:00Z', truncated: false,
  totals: { settled_fills: 0, contracts: 0, cost: 0, pnl: 0, roi: null, win_rate: null, open_fills: 0, max_drawdown: 0 },
  pnl_curve: [], by_cell: [], by_series: [], by_band: [], by_ttc: [], fill_quality: [],
  arb: { opportunities: 0, executed: 0, pnl: null, daily: [] },
};
const NO_TRADES = { total: 0, offset: 0, limit: 50, trades: [] };

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

// ---------------------------------------------------------------- mock data vs contract
describe('mockAnalytics matches the contract shapes exactly', () => {
  const p = (s) => new URLSearchParams(s);
  for (const days of [7, 30, 90, 180]) {
    for (const env of ['demo', 'live', 'all']) {
      it(`summary days=${days} env=${env}`, () => {
        const [st, body] = analyticsSummary(p(`days=${days}&env=${env}`), { nowMs: NOW });
        expect(st).toBe(200);
        expect(checkSummary(body, { days, env })).toEqual([]);
      });
    }
  }

  it('summary with no env follows the bot mode (demo and live)', () => {
    for (const mode of ['demo', 'live']) {
      const [, body] = analyticsSummary(p('days=30'), { mode, nowMs: NOW });
      expect(checkSummary(body, { days: 30, env: mode })).toEqual([]);
    }
  });

  it('summary with days omitted defaults to 30', () => {
    const [, body] = analyticsSummary(p(''), { nowMs: NOW });
    expect(body.days).toBe(30);
    expect(checkSummary(body)).toEqual([]);
  });

  it('the shape checker itself rejects broken payloads (guards the guard)', () => {
    const [, ok] = analyticsSummary(p('days=30'), { nowMs: NOW });
    const bad = JSON.parse(JSON.stringify(ok));
    delete bad.totals.roi;
    bad.pnl_curve[0].drawdown = 1;
    bad.by_cell[0].in_config = 'yes';
    bad.extra = 1;
    const problems = checkSummary(bad);
    expect(problems.join('\n')).toMatch(/missing roi/);
    expect(problems.join('\n')).toMatch(/drawdown > 0/);
    expect(problems.join('\n')).toMatch(/in_config not boolean/);
    expect(problems.join('\n')).toMatch(/unexpected extra/);
    expect(checkTrades({ total: 1, offset: 0, limit: 50, trades: [{}] }).length).toBeGreaterThan(5);
  });

  const TRADE_QUERIES = [
    'days=7', 'days=30', 'days=90&limit=200', 'days=180&limit=1', 'days=180&offset=40&limit=25', 'env=all&days=180&limit=200',
    'env=live&days=180', 'status=open&days=180', 'status=settled&result=loss&days=180', 'kind=taker&days=180',
    'kind=maker&side=yes&days=180', 'side=no&band=0.50-0.62&days=180', 'band=other&days=180', 'series=KXDEMOF&days=180',
    'cell=0.50-0.62|9-30h|no&days=180',
  ];
  for (const q of TRADE_QUERIES) {
    it(`trades ?${q}`, () => {
      const params = p(q);
      const [st, body] = analyticsTrades(params, { nowMs: NOW });
      expect(st).toBe(200);
      expect(
        checkTrades(body, { limit: Number(params.get('limit') || 50), offset: Number(params.get('offset') || 0) }),
      ).toEqual([]);
    });
  }

  it('trades pages are newest first across pages and never overlap', () => {
    const [, a] = analyticsTrades(p('days=180&limit=200'), { nowMs: NOW });
    const [, b] = analyticsTrades(p('days=180&limit=200&offset=200'), { nowMs: NOW });
    const ids = new Set(a.trades.map((t) => t.fill_id));
    expect(b.trades.every((t) => !ids.has(t.fill_id))).toBe(true);
    expect(Date.parse(a.trades.at(-1).ts)).toBeGreaterThanOrEqual(Date.parse(b.trades[0].ts));
  });

  it('offset beyond the end gives empty trades and the real total (not 422)', () => {
    const [st, body] = analyticsTrades(p('days=7&offset=100000'), { nowMs: NOW });
    expect(st).toBe(200);
    expect(body.trades).toEqual([]);
    expect(body.total).toBeGreaterThanOrEqual(0);
  });

  it('is deterministic for a fixed clock', () => {
    expect(analyticsSummary(p('days=90'), { nowMs: NOW })).toEqual(analyticsSummary(p('days=90'), { nowMs: NOW }));
  });
});

// ---------------------------------------------------------------- utils
describe('analytics utils', () => {
  it('cents / rate / points format numbers and show a dash for null, NaN and strings', () => {
    expect(cents(0.93)).toBe('93¢');
    expect(cents(0.934)).toBe('93.4¢');
    expect(cents(0.8)).toBe('80¢');
    for (const bad of [null, undefined, NaN, Infinity, '0.9']) {
      expect(cents(bad)).toBe('—');
      expect(rate(bad)).toBe('—');
      expect(points(bad)).toBe('—');
    }
    expect(rate(0.95)).toBe('95.0%');
    expect(rate(0)).toBe('0.0%');
    expect(rate(1, 0)).toBe('100%');
    expect(points(0.016)).toBe('+1.6 pts');
    expect(points(-0.032)).toBe('-3.2 pts');
    expect(points(0)).toBe('0.0 pts');
  });

  it('isNum and signCls', () => {
    expect(isNum(0)).toBe(true);
    expect(isNum(NaN)).toBe(false);
    expect(isNum(null)).toBe(false);
    expect(signCls(1)).toBe('pos');
    expect(signCls(-1)).toBe('neg');
    expect(signCls(0)).toBe('');
    expect(signCls(null)).toBe('muted');
  });

  it('parseCell / bandLabel / ttcLabel / weekLabel / shortDate / seriesName', () => {
    expect(parseCell('0.35-0.50|3-9h|yes')).toEqual({ price: '35-50¢', ttc: '3-9 h', side: 'YES' });
    expect(parseCell(null)).toEqual({ price: '?', ttc: '?', side: '?' });
    expect(parseCell('garbage')).toEqual({ price: 'garbage', ttc: '?', side: '?' });
    expect(bandLabel('0.45-0.59')).toBe('45-59¢');
    expect(bandLabel('other')).toBe('Other prices');
    expect(bandLabel(undefined)).toBe('—');
    expect(ttcLabel('>30h')).toBe('>30 h');
    expect(ttcLabel(null)).toBe('—');
    expect(weekLabel('2026-W40')).toBe('W40');
    expect(weekLabel(null)).toBe('—');
    expect(shortDate('2026-10-01')).toBe('Oct 1');
    expect(shortDate('2026-10-01T23:59:59Z')).toBe('Oct 1');
    expect(shortDate('')).toBe('—');
    expect(shortDate('not-a-date')).toBe('not-a-date');
    expect(seriesName('KXDEMOB')).toBe('Demo series B');
    expect(seriesName('KXNEWTHING')).toBe('KXNEWTHING');
    expect(seriesName(null)).toBe('—');
  });

  it('small-n threshold is 30: 29 fills are thin, 30 are not', async () => {
    const summary = {
      ...EMPTY,
      totals: { ...EMPTY.totals, settled_fills: 59, pnl: 3, roi: 0.01, win_rate: 0.6 },
      pnl_curve: [
        { date: '2026-10-12', pnl: 1, cum_pnl: 1, drawdown: 0, maker_pnl: 1, taker_pnl: 0 },
        { date: '2026-10-13', pnl: 2, cum_pnl: 3, drawdown: 0, maker_pnl: 2, taker_pnl: 0 },
      ],
      by_series: [group('KXDEMOB', MIN_N - 1, MIN_N - 2), group('KXDEMOA', MIN_N, MIN_N - 1)],
    };
    base({ 'GET /api/analytics/summary': summary, 'GET /api/analytics/trades': NO_TRADES });
    const user = userEvent.setup();
    renderApp('/analytics?tab=edge');
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Group edge by' }), 'series');
    const rows = await screen.findAllByTestId('edge-row');
    expect(rows[0]).toHaveAttribute('data-thin', 'true');
    expect(rows[1]).toHaveAttribute('data-thin', 'false');
  });

  it('ciDomain: empty input, rounds down to 5 pts, never above 0.95', () => {
    expect(ciDomain([])).toEqual([0, 1]);
    expect(ciDomain(undefined)).toEqual([0, 1]);
    expect(ciDomain([{ win_ci_low: null, avg_price: null, win_rate: null }])).toEqual([0, 1]);
    const [lo, hi] = ciDomain([{ win_ci_low: 0.43, avg_price: 0.5, win_rate: 0.55 }]);
    expect(lo).toBeCloseTo(0.4, 6);
    expect(hi).toBe(1);
    expect(ciDomain([{ win_ci_low: 0.99, avg_price: 0.99, win_rate: 0.99 }])[0]).toBe(0.95);
    expect(ciDomain([{ win_ci_low: -0.5, avg_price: 0.2, win_rate: 0.3 }])[0]).toBe(0);
  });

  it('CI bar positions: price tick and dot map linearly on the domain and clamp outside it', () => {
    const { container } = render(<WinRateCiBar winRate={0.5} low={0.4} high={0.6} avgPrice={0.45} domain={[0.4, 0.6]} />);
    const svg = container.querySelector('svg');
    const rect = svg.querySelector('rect');
    const lines = svg.querySelectorAll('line');
    const tick = lines[lines.length - 1];
    const dot = svg.querySelector('circle');
    // W=300, pad 4: x(v) = 4 + (v - d0)/(d1 - d0) * 292
    expect(Number(rect.getAttribute('x'))).toBeCloseTo(4, 3);
    expect(Number(rect.getAttribute('width'))).toBeCloseTo(292, 3);
    expect(Number(tick.getAttribute('x1'))).toBeCloseTo(4 + 0.25 * 292, 3);
    expect(Number(dot.getAttribute('cx'))).toBeCloseTo(4 + 0.5 * 292, 3);
    // CI wholly outside the domain still draws a visible (>= 2 wide) bar inside the svg box
    const out = render(<WinRateCiBar winRate={0.5} low={0.45} high={0.55} avgPrice={0.3} domain={[0.6, 0.9]} />).container.querySelector('svg');
    const r2 = out.querySelector('rect');
    expect(Number(r2.getAttribute('width'))).toBeGreaterThanOrEqual(2);
    expect(Number(r2.getAttribute('x'))).toBeGreaterThanOrEqual(0);
    expect(Number(out.querySelector('circle').getAttribute('cx'))).toBeCloseTo(4, 3);
  });

  it('CI bar renders nothing when any of win rate, CI or price is missing', () => {
    for (const props of [
      { winRate: null, low: 0.8, high: 0.9, avgPrice: 0.9 },
      { winRate: 0.9, low: null, high: 0.9, avgPrice: 0.9 },
      { winRate: 0.9, low: 0.8, high: undefined, avgPrice: 0.9 },
      { winRate: 0.9, low: 0.8, high: 0.9, avgPrice: NaN },
    ]) {
      const { container, unmount } = render(<WinRateCiBar {...props} />);
      expect(container.querySelector('svg')).toBeNull();
      unmount();
    }
  });
});

// ---------------------------------------------------------------- drawdown chart
describe('PnlCurve with sparse and odd curves', () => {
  const pt = (date, cum, dd, extra = {}) => ({ date, pnl: 0, cum_pnl: cum, drawdown: dd, maker_pnl: 0, taker_pnl: 0, ...extra });

  it('curveFor tolerates missing and non-numeric fields and a non-array', () => {
    expect(curveFor(undefined)).toEqual([]);
    expect(curveFor(null, 'maker')).toEqual([]);
    expect(curveFor([{ date: 'a' }, { date: 'b', cum_pnl: 'x', drawdown: null }], 'all')).toEqual([
      { date: 'a', cum: 0, dd: 0 }, { date: 'b', cum: 0, dd: 0 },
    ]);
    expect(curveFor([{ date: 'a' }, { date: 'b', taker_pnl: 2 }], 'taker').map((p) => p.cum)).toEqual([0, 2]);
  });

  it('curveFor drawdown never goes above zero and recovers at new highs', () => {
    const pts = [pt('a', 0, 0, { maker_pnl: 3 }), pt('b', 0, 0, { maker_pnl: -5 }), pt('c', 0, 0, { maker_pnl: 4 }), pt('d', 0, 0, { maker_pnl: 4 })];
    const out = curveFor(pts, 'maker');
    expect(out.map((p) => p.cum)).toEqual([3, -2, 2, 6]);
    expect(out.map((p) => p.dd)).toEqual([0, -5, -1, 0]);
    expect(out.every((p) => p.dd <= 0)).toBe(true);
  });

  it('a curve with fewer than 2 points shows the not-enough-data message instead of crashing', () => {
    for (const points_ of [[], [pt('2026-10-01', 1, 0)], undefined]) {
      const { container, unmount } = render(<PnlCurve points={points_} />);
      expect(container.textContent).toMatch(/Not enough settled days/);
      unmount();
    }
  });

  it('sparse curve (only days with settlements, gaps between) draws and reports max drawdown on its date', () => {
    const sparse = [pt('2026-08-01', 2, 0), pt('2026-08-20', -3, -5), pt('2026-10-12', 4, 0)];
    render(<PnlCurve points={sparse} />);
    const root = screen.getByTestId('pnl-curve');
    expect(within(root).getByText(/Max/).textContent).toMatch(/-\$5\.00 on Aug 20/);
    expect(root.querySelector('svg.pnl-svg path[d^="M"]').getAttribute('d')).not.toMatch(/NaN/);
    expect(root.innerHTML).not.toMatch(/NaN|Infinity/);
    expect(screen.getByRole('img', { name: /from Aug 1 to Oct 12: \+\$4\.00\. Largest drawdown -\$5\.00/ })).toBeInTheDocument();
  });

  it('all-flat and all-positive curves have no NaN geometry and say "none" for drawdown', () => {
    for (const pts of [
      [pt('2026-10-01', 0, 0), pt('2026-10-02', 0, 0), pt('2026-10-03', 0, 0)],
      [pt('2026-10-01', 1, 0), pt('2026-10-02', 2, 0)],
    ]) {
      const { container, unmount } = render(<PnlCurve points={pts} />);
      expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
      expect(container.textContent).toMatch(/Max Drawdown\s*none/);
      unmount();
    }
  });

  it('all-negative curve with a long drawdown does not produce NaN', () => {
    const { container } = render(<PnlCurve points={[pt('2026-10-01', -1, -1), pt('2026-10-02', -4, -4), pt('2026-10-03', -3, -4)]} />);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
    expect(container.textContent).toMatch(/-\$4\.00 on Oct 2/);
  });

  it('maker / taker kinds fall back to zeros when the split fields are absent', () => {
    const { container } = render(<PnlCurve kind="maker" points={[{ date: '2026-10-01', cum_pnl: 1, drawdown: 0 }, { date: '2026-10-02', cum_pnl: 2, drawdown: 0 }]} />);
    expect(container.innerHTML).not.toMatch(/NaN/);
  });
});

// ---------------------------------------------------------------- page states
describe('Analytics page edge states', () => {
  it('null roi / win_rate / edge render as dashes, not "null" or NaN', async () => {
    const summary = {
      ...EMPTY,
      totals: { settled_fills: 3, contracts: 6, cost: 5, pnl: 0, roi: null, win_rate: null, open_fills: 1, max_drawdown: 0 },
      pnl_curve: [
        { date: '2026-10-12', pnl: 0, cum_pnl: 0, drawdown: 0, maker_pnl: 0, taker_pnl: 0 },
        { date: '2026-10-13', pnl: 0, cum_pnl: 0, drawdown: 0, maker_pnl: 0, taker_pnl: 0 },
      ],
      by_series: [{
        key: 'KXDEMOB', n: 3, wins: 0, contracts: 6, cost: 5, pnl: 0, roi: null, avg_price: null, win_rate: null,
        win_ci_low: null, win_ci_high: null, edge: null, edge_ci_low: null, edge_ci_high: null,
      }],
    };
    base({ 'GET /api/analytics/summary': summary, 'GET /api/analytics/trades': NO_TRADES });
    const user = userEvent.setup();
    renderApp('/analytics');
    const totals = await screen.findByTestId('totals');
    expect(within(totals).getByText('Win Rate').closest('div').textContent).toMatch(/—/);
    expect(within(totals).getByText('Return').closest('div').textContent).toMatch(/—/);
    await user.click(screen.getByRole('tab', { name: 'Edge' }));
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Group edge by' }), 'series');
    const row = await screen.findByTestId('edge-row');
    expect(row.textContent).not.toMatch(/null|NaN|undefined/);
    expect(row.querySelector('svg.wr-ci')).toBeNull();
    expect(document.querySelector('main').textContent).not.toMatch(/NaN|undefined|\bnull\b/);
  });

  it('empty arrays everywhere: no charts, no crash, empty-state copy, Stop still there', async () => {
    base({ 'GET /api/analytics/summary': EMPTY, 'GET /api/analytics/trades': NO_TRADES });
    renderApp('/analytics');
    expect(await screen.findByText('No settled trades yet.')).toBeInTheDocument();
    expect(screen.queryByTestId('edge-row')).toBeNull();
    expect(screen.queryByTestId('pnl-curve')).toBeNull();
    expect(screen.getByRole('button', { name: /stop the bot/i })).toBeEnabled();
  });

  it('empty settled window but open fills mentions how many are waiting', async () => {
    base({
      'GET /api/analytics/summary': { ...EMPTY, totals: { ...EMPTY.totals, open_fills: 4 } },
      'GET /api/analytics/trades': NO_TRADES,
    });
    renderApp('/analytics');
    expect(await screen.findByText(/4 fills are still waiting to settle/)).toBeInTheDocument();
  });

  it('truncated=true shows the large-window notice, truncated=false does not', async () => {
    base({ 'GET /api/analytics/summary': (c) => ({ ...mockSummary(c)[1], truncated: true }) });
    const { unmount } = renderApp('/analytics');
    expect(await screen.findByText(/only the most recent fills were counted/i)).toBeInTheDocument();
    unmount();
    base();
    renderApp('/analytics');
    await screen.findByTestId('totals');
    expect(screen.queryByText(/only the most recent fills were counted/i)).toBeNull();
  });

  it('401 on the summary signs the user out to the login page', async () => {
    stubApi({
      'GET /api/status': STATUS,
      'GET /api/analytics/summary': [401, { detail: 'expired' }],
      'GET /api/analytics/trades': [401, { detail: 'expired' }],
    });
    renderApp('/analytics');
    expect(await screen.findByLabelText('Username')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Analytics' })).toBeNull();
  });

  it('401 on the trades list alone also signs out', async () => {
    base({ 'GET /api/analytics/trades': [401, { detail: 'expired' }] });
    renderApp('/analytics?tab=trades');
    expect(await screen.findByLabelText('Username')).toBeInTheDocument();
  });

  it('a 401 clears the cached analytics so a second user does not see them', async () => {
    base();
    const { unmount } = renderApp('/analytics');
    await screen.findByTestId('totals');
    unmount();
    stubApi({ 'GET /api/status': [401, { detail: 'expired' }], 'GET /api/analytics/summary': [401, { detail: 'x' }] });
    renderApp('/analytics');
    await screen.findByLabelText('Username');
    expect(screen.queryByTestId('totals')).toBeNull();
  });

  it('store down (503) on summary only: trades still render, error is retryable and names the database', async () => {
    base({ 'GET /api/analytics/summary': [503, { detail: 'store_unavailable' }] });
    renderApp('/analytics?tab=trades');
    // The header database banner can appear a tick before the page's own error.
    await waitFor(() =>
      expect(screen.getAllByRole('alert').some((a) => /can't reach its database/i.test(a.textContent))).toBe(true),
    );
    expect(screen.queryByTestId('totals')).toBeNull();
    expect(await screen.findAllByTestId('trade-row')).toHaveLength(50);
  });

  it('store down on trades only: totals render, trade list shows an error with Retry', async () => {
    base({ 'GET /api/analytics/trades': [503, { detail: 'store_unavailable' }] });
    const user = userEvent.setup();
    renderApp('/analytics');
    expect(await screen.findByTestId('totals')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Trades' }));
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.some((a) => /database/i.test(a.textContent))).toBe(true);
  });

  it('429 rate limit is shown as an error (page does not hang on "Loading")', async () => {
    base({ 'GET /api/analytics/summary': [429, { detail: 'rate_limited' }] });
    renderApp('/analytics');
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.queryByText('Loading analytics…')).toBeNull();
  });

  it('422 from the trades endpoint (bad filter) does not crash the page', async () => {
    base({ 'GET /api/analytics/trades': [422, { detail: [{ loc: ['query', 'band'], msg: 'bad' }] }] });
    const user = userEvent.setup();
    renderApp('/analytics');
    expect(await screen.findByTestId('totals')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Trades' }));
    expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0);
  });

  it('a summary for a stale window is not shown as the selected window (30 -> 180 days, request pending)', async () => {
    const user = userEvent.setup();
    let release;
    base({
      'GET /api/analytics/summary': (call) => {
        if (qs(call).get('days') === '180') return new Promise((r) => { release = r; });
        return mockSummary(call);
      },
    });
    renderApp('/analytics');
    await screen.findByTestId('totals');
    expect(await screen.findByText(/settled in the last 30 days/)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: '180 days' }));
    expect(screen.queryByText(/settled in the last 30 days/)).toBeNull();
    await waitFor(() => expect(typeof release).toBe('function'));
    expect(screen.queryByText(/settled in the last 180 days/)).toBeNull();
  });
});

// ---------------------------------------------------------------- HelpIcon keyboard
function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.pathname + l.hash}</output>;
}

describe('HelpIcon accessibility', () => {
  it('is a focusable button reachable with Tab; Enter opens the popover and its More link goes to the term', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/x']}>
        <button type="button">before</button>
        <HelpIcon term="fill-rate" />
        <Where />
      </MemoryRouter>,
    );
    await user.tab();
    expect(screen.getByRole('button', { name: 'before' })).toHaveFocus();
    await user.tab();
    const btn = screen.getByRole('button', { name: /what is fill rate\?/i });
    expect(btn).toHaveFocus();
    await user.keyboard('{Enter}');
    const more = screen.getByRole('link', { name: /More/ });
    expect(more).toHaveFocus();
    expect(more).toHaveAttribute('href', '/terminology#fill-rate');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent('/terminology#fill-rate');
  });

  it('has an accessible name that is not just the glyph; the svg is hidden from assistive tech and not focusable', () => {
    render(
      <MemoryRouter>
        <HelpIcon term="edge" />
      </MemoryRouter>,
    );
    const link = screen.getByRole('button');
    expect(link).toHaveAccessibleName(/^What is /);
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    expect(link.querySelector('svg')).toHaveAttribute('focusable', 'false');
    expect(link).not.toHaveAttribute('tabindex', '-1');
  });

  it('an unknown term does not crash and still renders a link', () => {
    render(
      <MemoryRouter>
        <HelpIcon term="no-such-term" />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'What is no-such-term?' })).toHaveAttribute('href', '/terminology#no-such-term');
  });

  it('Enter on the first tab stop opens the definition and Enter again on More navigates (no pointer needed)', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/x']}>
        <HelpIcon term="roi" />
        <Where />
      </MemoryRouter>,
    );
    await user.tab();
    await user.keyboard('{Enter}');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent('/terminology#roi');
  });
});

// ---------------------------------------------------------------- Terminology hash
describe('Terminology hash handling', () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  it('an unknown hash id renders the full glossary without crashing or highlighting anything', async () => {
    base();
    renderApp('/terminology#definitely-not-a-term');
    expect(await screen.findByRole('heading', { name: /terminology/i })).toBeInTheDocument();
    expect(document.querySelectorAll('li[id]').length).toBeGreaterThan(40);
    expect(document.querySelector('.flash, .highlight, [data-highlight="true"]')).toBeNull();
    expect(window.HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
  });

  it('empty hash "#" and an id-with-spaces hash are harmless', async () => {
    for (const h of ['#', '#%20%20', '#fill%20rate']) {
      base();
      const { unmount } = renderApp(`/terminology${h}`);
      expect(await screen.findByRole('heading', { name: /terminology/i })).toBeInTheDocument();
      unmount();
    }
  });

  it('a known hash scrolls to and highlights that entry', async () => {
    base();
    renderApp('/terminology#win-rate');
    await waitFor(() => expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled());
    expect(document.getElementById('win-rate')).toBeTruthy();
  });

  // S-29 (fixed): a malformed percent-escape in the hash (e.g. /terminology#%E0%A4%A) used to throw URIError
  // during render; Terminology.jsx now ignores it.
  it('a malformed percent-escape in the hash does not crash the page', async () => {
    base();
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderApp('/terminology#%E0%A4%A');
    expect(await screen.findByRole('heading', { name: /terminology/i })).toBeInTheDocument();
    spy.mockRestore();
  });
});

// ---------------------------------------------------------------- Stop reachable
describe('Stop from /analytics and /terminology', () => {
  for (const path of ['/analytics', '/terminology']) {
    it(`${path}: header Stop is visible, confirms, calls POST /api/bot/stop with no 2FA header`, async () => {
      const user = userEvent.setup();
      window.HTMLElement.prototype.scrollIntoView = vi.fn();
      const calls = base({
        'POST /api/bot/stop': { desired: 'stopped', kill_switch: true, cancelled: 1, errors: [], arb_kill_switch: true },
      });
      renderApp(path);
      const header = document.querySelector('header.appbar');
      const stop = within(header).getByRole('button', { name: /stop the bot/i });
      expect(stop).toBeEnabled();
      await user.click(stop);
      await user.click(await screen.findByRole('button', { name: 'Stop now' }));
      expect(await screen.findByText('Stopped. Kill switch set.')).toBeInTheDocument();
      const stops = calls.filter((c) => c.key === 'POST /api/bot/stop');
      expect(stops).toHaveLength(1);
      expect(stops[0].headers['X-2FA-Code']).toBeUndefined();
    });
  }

  it('/analytics: Stop works while the analytics endpoints are failing (503) and while they hang', async () => {
    const user = userEvent.setup();
    for (const hang of [false, true]) {
      const calls = stubApi({
        'GET /api/status': STATUS,
        'GET /api/analytics/summary': () => (hang ? new Promise(() => {}) : [503, { detail: 'store_unavailable' }]),
        'GET /api/analytics/trades': () => (hang ? new Promise(() => {}) : [503, { detail: 'store_unavailable' }]),
        'POST /api/bot/stop': { desired: 'stopped', kill_switch: true, cancelled: 0, errors: [] },
      });
      const { unmount } = renderApp('/analytics');
      const stop = within(document.querySelector('header.appbar')).getByRole('button', { name: /stop the bot/i });
      await user.click(stop);
      await user.click(await screen.findByRole('button', { name: 'Stop now' }));
      await waitFor(() => expect(calls.filter((c) => c.key === 'POST /api/bot/stop')).toHaveLength(1));
      unmount();
    }
  });

  it('the /analytics and /terminology routes are registered and unknown routes do not mask them', async () => {
    base();
    const { unmount } = renderApp('/analytics');
    expect(await screen.findByRole('heading', { name: 'Analytics' })).toBeInTheDocument();
    unmount();
    renderApp('/terminology');
    expect(await screen.findByRole('heading', { name: /terminology/i })).toBeInTheDocument();
  });
});

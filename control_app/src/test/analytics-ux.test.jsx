// Summary-first UX sprint: Markets folded cards, Analytics tabs / picker / Too Early group, range-bar key,
// My Trades scorecard + journal (Y5), manual trades kept out of bot statistics.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { setToken } from '../services/api.js';
import { analyticsSummary, analyticsTrades, mockMyTrades, mockTradeNote } from '../services/mockAnalytics.js';
import WinRateCiBar from '../components/analytics/WinRateCiBar.jsx';
import { BarEnds } from '../components/analytics/BarLegend.jsx';
import CiBar from '../components/CiBar.jsx';
import { verdictInfo } from '../pages/Markets.jsx';
import { edgeText, shortReason } from '../utils/analytics.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

const NOW = Date.parse('2026-10-13T18:00:00Z');
const qs = (call) => new URL(call.path, 'http://x').searchParams;
const q = (s) => new URLSearchParams(s);

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const MARKETS = {
  markets: [
    { series: 'KXDEMOA', enabled: true, fills: 40, cost: 180, pnl: 3.2, events: 24, roi: 0.021, ci_low: -0.014, ci_high: 0.056, verdict: 'keep', need_more: null },
    { series: 'KXDEMOF', enabled: false, fills: 8, cost: 40, pnl: -1.1, events: 4, roi: -0.02, ci_low: null, ci_high: null, verdict: null, need_more: 30 },
  ],
  report_utc: null,
};

function routes(extra = {}) {
  return {
    'GET /api/status': STATUS,
    'GET /api/markets': MARKETS,
    'GET /api/analytics/summary': (c) => analyticsSummary(qs(c), { mode: 'demo', nowMs: NOW }),
    'GET /api/analytics/trades': (c) => analyticsTrades(qs(c), { mode: 'demo', nowMs: NOW }),
    'GET /api/analytics/my-trades': (c) => mockMyTrades(qs(c), { nowMs: NOW }),
    ...extra,
  };
}

describe('Markets cards', () => {
  it('open folded: name, fills, P&L and verdict; no chart, axis or key until a card is opened', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/strategies/maker/results');
    const toggle = await screen.findByRole('button', { name: /^Demo Series A/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('40 Fills');
    expect(toggle).toHaveTextContent('+$3.20');
    expect(toggle).toHaveTextContent('Keep');
    expect(screen.queryByTestId('ci-bar-scale')).toBeNull();
    expect(screen.queryByTestId('bar-legend')).toBeNull();
    expect(screen.queryByTestId('ci-summary')).toBeNull();
    // One explainer for the whole page, not per card.
    expect(screen.getAllByTestId('ci-explainer')).toHaveLength(1);
    await user.click(toggle);
    expect(screen.getByTestId('ci-summary')).toHaveTextContent('Return +2.1%');
    expect(screen.getByTestId('ci-summary')).toHaveTextContent('Likely Range −1.4% to +5.6%');
    expect(screen.getByTestId('ci-bar-scale')).toBeInTheDocument();
    expect(screen.getByTestId('bar-legend')).toHaveTextContent('Actual Return +2.1%');
  });

  it('per-card queue stats are gone (they live in Analytics > Execution)', async () => {
    stubApi(routes({ 'GET /api/markets': { markets: [{ ...MARKETS.markets[0], queue_stats: { episodes: 5, fills: 5 } }] } }));
    renderApp('/markets');
    await screen.findByText('Demo Series A');
    expect(screen.queryByTestId('queue-position')).toBeNull();
  });

  it('the Pause button is unambiguous: a paused series says Not Trading, never "Paused" in a second control', async () => {
    stubApi(routes());
    renderApp('/markets');
    await screen.findByText('Demo Series A');
    expect(screen.getAllByRole('button', { name: /pause/i })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /resume/i })).toHaveLength(1);
  });

  it('verdict chips carry a plain-words tooltip', () => {
    expect(verdictInfo({ verdict: 'pause' })).toMatchObject({ label: 'Pause Suggested', tone: 'danger' });
    expect(verdictInfo({ verdict: 'keep' }).title).toMatch(/in line with what the research expected/);
    expect(verdictInfo({ verdict: null, need_more: 30 }).label).toBe('Collecting Data (30 More Fills)');
    expect(verdictInfo({})).toBeNull();
  });
});

describe('range bar key (CI bar legend)', () => {
  const props = { winRate: 0.495, low: 0.4, high: 0.56, avgPrice: 0.42, domain: [0.35, 0.6] };

  it('lists only the marks drawn, with their values', () => {
    const { container, rerender } = render(<WinRateCiBar {...props} />);
    const key = screen.getByTestId('bar-legend');
    expect(key).toHaveTextContent('Actual Win Rate 49.5%');
    expect(key).toHaveTextContent('Likely Range (95%)');
    expect(key).toHaveTextContent('Break-Even (Price Paid 42¢)');
    expect(key).not.toHaveTextContent('Research Expected');
    expect(key).not.toHaveTextContent('Per-Fill Range');
    expect(container.querySelector('[data-mark="expected"]')).toBeNull();
    rerender(<WinRateCiBar {...props} expected={0.55} clusterLow={0.34} clusterHigh={0.59} />);
    const key2 = screen.getByTestId('bar-legend');
    expect(key2).toHaveTextContent('Research Expected 55.0%');
    expect(key2).toHaveTextContent('Per-Fill Range');
    expect(container.querySelector('[data-mark="expected"]')).not.toBeNull();
    expect(container.querySelector('[data-mark="wilson"]')).not.toBeNull();
    expect(key2.textContent).not.toMatch(/\bpts\b/);
  });

  it('labels both ends with unit-bearing values and keeps aria-label accurate', () => {
    render(<WinRateCiBar {...props} />);
    expect(screen.getByTestId('bar-ends')).toHaveTextContent('40.0%');
    expect(screen.getByTestId('bar-ends')).toHaveTextContent('56.0%');
    expect(screen.getByRole('img')).toHaveAccessibleName(/Win rate 49\.5%, likely range 40\.0% to 56\.0%, price paid 42¢/);
  });

  it('end labels are clamped inside the card and merge when the bounds are close', () => {
    const { container, rerender } = render(<BarEnds lo={0.0} hi={1} loText="1%" hiText="9%" />);
    const spans = [...container.querySelectorAll('span')];
    expect(spans.map((s) => s.style.left)).toEqual(['10%', '90%']);
    rerender(<BarEnds lo={0.5} hi={0.55} loText="88.0%" hiText="90.0%" />);
    const one = container.querySelectorAll('span');
    expect(one).toHaveLength(1);
    expect(one[0]).toHaveTextContent('88.0% to 90.0%');
  });

  it('Markets CiBar key names the zero line as break-even', () => {
    render(<CiBar roi={0.02} low={-0.01} high={0.05} />);
    expect(screen.getByTestId('bar-legend')).toHaveTextContent('Break-Even 0%');
  });
});

describe('Analytics tabs and Edge tab', () => {
  it('has Summary, Edge, Execution and Trades; tab choice lives in the URL; summary is default', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics');
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Summary', 'Edge', 'Execution', 'Trades', 'Ledger', 'Scorecard']);
    expect(await screen.findByTestId('totals')).toBeInTheDocument();
    expect(screen.getByText('Is the Edge Holding?')).toBeInTheDocument();
    expect(screen.queryByTestId('calibration')).toBeNull();
    await user.click(screen.getByRole('tab', { name: 'Edge' }));
    expect(screen.getByRole('tab', { name: 'Edge' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByTestId('calibration')).toBeInTheDocument();
    // Calibration numbers hide behind "Show Numbers".
    expect(screen.getByText('Show Numbers')).toBeInTheDocument();
  });

  it('Edge picker shows one breakdown at a time', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics?tab=edge');
    const picker = await screen.findByRole('combobox', { name: 'Group edge by' });
    expect([...picker.options].map((o) => o.text)).toEqual(['Cell', 'Series', 'Price Band', 'Time Left']);
    await user.selectOptions(picker, 'series');
    expect(screen.getByRole('tabpanel', { name: 'Edge by series' })).toBeInTheDocument();
    await user.selectOptions(picker, 'ttc');
    expect(screen.getByRole('tabpanel', { name: 'Edge by time to close' })).toBeInTheDocument();
  });

  it('no axis, bar or key while every Edge row is folded; they appear inside an opened row only', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics?tab=edge');
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Group edge by' }), 'series');
    const rows = await screen.findAllByTestId('edge-row');
    expect(screen.queryByTestId('ci-scale')).toBeNull();
    expect(screen.queryByTestId('bar-legend')).toBeNull();
    await user.click(within(rows[0]).getByRole('button', { expanded: false }));
    expect(within(rows[0]).getByTestId('ci-scale')).toBeInTheDocument();
    expect(within(rows[0]).getByTestId('bar-legend')).toBeInTheDocument();
    expect(screen.getAllByTestId('ci-scale')).toHaveLength(1);
  });

  it('Too Early cells share one folded line (open by default only when nothing else is shown)', async () => {
    const user = userEvent.setup();
    const cell = (key, verdict) => ({
      key, cell: key, in_config: true, n: 10, wins: 5, contracts: 40, cost: 30, pnl: 1, roi: 0.02, avg_price: 0.43,
      win_rate: 0.5, win_ci_low: 0.24, win_ci_high: 0.76, edge: -0.03, edge_ci_low: -0.3, edge_ci_high: 0.05, markets: 6,
      expected_win_rate: 0.55, verdict,
    });
    const s = (cells) => ({ ...analyticsSummary(q('days=30'), { nowMs: NOW })[1], by_cell: cells, calibration: undefined });
    stubApi(routes({ 'GET /api/analytics/summary': () => s([cell('0.35-0.50|3-9h|yes', 'too_early'), cell('0.50-0.62|9-30h|no', 'on_track')]) }));
    const { unmount } = renderApp('/analytics?tab=edge');
    const group = await screen.findByTestId('too-early-group');
    expect(group).toHaveTextContent('Too Early (1 Cell)');
    expect(within(group).queryByTestId('edge-row')).toBeNull(); // folded
    await user.click(within(group).getAllByRole('button')[0]);
    expect(within(group).getByTestId('edge-row')).toBeInTheDocument();
    unmount();
    stubApi(routes({ 'GET /api/analytics/summary': () => s([cell('0.35-0.50|3-9h|yes', 'too_early')]) }));
    renderApp('/analytics?tab=edge');
    const only = await screen.findByTestId('too-early-group');
    expect(within(only).getByTestId('edge-row')).toBeInTheDocument();
  });

  it('verdict chips explain themselves in plain words; copy is short and uses % not pts', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics?tab=edge');
    const rows = await screen.findAllByTestId('edge-row');
    const chip = within(rows[0]).getByText(/On Track|Below|Above|Too Early/);
    expect(chip.getAttribute('title')).toMatch(/\./);
    await user.click(within(rows[0]).getByRole('button', { expanded: false }));
    expect(document.querySelector('main').textContent).not.toMatch(/\bpts\b/);
    expect(edgeText(0.016)).toBe('+1.6%');
    expect(edgeText(-0.032)).toBe('−3.2%');
    expect(shortReason({ win_rate: 0.49, expected_win_rate: 0.55 })).toBe('Won 49%; expected 55%');
  });

  it('Execution tab carries the queue-position table moved from Markets', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics');
    await screen.findByTestId('totals');
    await user.click(screen.getByRole('tab', { name: 'Execution' }));
    expect(await screen.findByText('Queue Position, All Series')).toBeInTheDocument();
  });
});

describe('My Trades (Y5)', () => {
  it('manual trades never enter the bot statistics: mock summary and default trade list are bot-only', () => {
    const [, sum] = analyticsSummary(q('days=180'), { nowMs: NOW });
    const [, trades] = analyticsTrades(q('days=180&limit=200'), { nowMs: NOW });
    expect(trades.trades.every((t) => t.source === 'bot')).toBe(true);
    const [, all] = analyticsTrades(q('days=180&limit=200&source=all'), { nowMs: NOW });
    expect(all.trades.some((t) => t.source === 'you')).toBe(true);
    const [, mine] = analyticsTrades(q('days=180&limit=200&source=you'), { nowMs: NOW });
    expect(mine.trades.every((t) => t.source === 'you')).toBe(true);
    const [, my] = mockMyTrades(q('days=180'), { nowMs: NOW });
    expect(sum.totals.settled_fills).not.toBe(sum.totals.settled_fills + my.totals.settled_fills);
    expect(JSON.stringify(sum.by_series)).not.toMatch(/KXFED|KXNBA/);
  });

  it('Summary shows "Bot · You" side by side and the bot-only toggle is the default', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics');
    const line = await screen.findByTestId('bot-vs-you');
    expect(line).toHaveTextContent(/Bot \+\$16\.40.*You/);
    expect(line).toHaveTextContent('This Month');
    expect(screen.getByRole('button', { name: 'Bot Only' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByTestId('combined-line')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Bot + You' }));
    expect(screen.getByTestId('combined-line')).toHaveTextContent('Bot + You');
  });

  it('Trades tab: scorecard, journal rows, and Bot + You asks the trade list for source=all', async () => {
    const user = userEvent.setup();
    const calls = stubApi(routes());
    renderApp('/analytics?tab=trades');
    const my = await screen.findByTestId('my-trades');
    expect(await within(my).findByTestId('score-you')).toBeInTheDocument();
    expect(within(my).getByTestId('score-bot')).toBeInTheDocument();
    expect((await within(my).findAllByTestId('my-trade-row')).length).toBeGreaterThan(3);
    await screen.findAllByTestId('trade-row');
    expect(calls.filter((c) => c.key === 'GET /api/analytics/trades').every((c) => !c.path.includes('source='))).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Bot + You' }));
    await waitFor(() => expect(calls.some((c) => c.key === 'GET /api/analytics/trades' && c.path.includes('source=all'))).toBe(true));
  });

  it('a journal note saves with PUT /api/trades/{id}/note (no 2FA code) and shows in the row', async () => {
    const user = userEvent.setup();
    const calls = stubApi(routes({
      'PUT /api/trades/my-008/note': (c) => mockTradeNote('PUT', 'my-008', c.body),
    }));
    renderApp('/analytics?tab=trades');
    const rows = await screen.findAllByTestId('my-trade-row');
    const row = rows[0];
    await user.click(within(row).getByRole('button', { name: /Add Note/ }));
    await user.type(within(row).getByRole('textbox'), 'Fed hunch  again');
    expect(within(row).getByText('16/200')).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: 'Save Note' }));
    expect(await within(row).findByTestId('trade-note')).toHaveTextContent('Fed hunch again');
    const put = calls.find((c) => c.method === 'PUT');
    expect(put.body).toEqual({ note: 'Fed hunch  again' });
    expect(put.headers['X-2FA-Code']).toBeUndefined();
  });

  it('a note over 200 characters is refused by the input and the server error is shown', async () => {
    const user = userEvent.setup();
    stubApi(routes({ 'PUT /api/trades/my-008/note': [400, { detail: ['note: at most 200 characters'] }] }));
    renderApp('/analytics?tab=trades');
    const row = (await screen.findAllByTestId('my-trade-row'))[0];
    await user.click(within(row).getByRole('button', { name: /(Add|Edit) Note/ }));
    const box = within(row).getByRole('textbox');
    expect(box).toHaveAttribute('maxlength', '200');
    await user.type(box, 'x');
    await user.click(within(row).getByRole('button', { name: 'Save Note' }));
    expect(await within(row).findByRole('alert')).toHaveTextContent(/200 characters/);
  });

  it('mock note endpoint: plain text, 200-char cap, clear with empty string, unknown id 404', () => {
    expect(mockTradeNote('PUT', 'my-008', { note: '  hello   world ' })).toEqual([200, { fill_id: 'my-008', note: 'hello world' }]);
    expect(mockTradeNote('GET', 'my-008')[1].note).toBe('hello world');
    expect(mockTradeNote('PUT', 'my-008', { note: 'x'.repeat(201) })[0]).toBe(400);
    expect(mockTradeNote('PUT', 'my-008', { note: '' })[1].note).toBe('');
    expect(mockTradeNote('PUT', 'nope', { note: 'a' })[0]).toBe(404);
    expect(mockTradeNote('PUT', 'bad id', { note: 'a' })[0]).toBe(400);
  });
});

describe('Title Case on Analytics and Markets', () => {
  it('headings, card titles, tabs, chips and stat labels start with a capital letter', async () => {
    const user = userEvent.setup();
    stubApi(routes());
    renderApp('/analytics');
    await screen.findByTestId('totals');
    const labels = (root) =>
      [...root.querySelectorAll('h2, h3, dt, [role="tab"], .chip, .tag, th')].map((e) => e.textContent.trim()).filter(Boolean);
    const main = document.querySelector('main');
    for (const tab of ['Summary', 'Edge', 'Execution', 'Trades']) {
      await user.click(screen.getByRole('tab', { name: tab }));
      await screen.findByRole('tab', { name: tab, selected: true });
      await new Promise((r) => setTimeout(r, 50));
      for (const l of labels(main)) {
        if (/^[0-9$+−\-—]/.test(l)) continue;
        expect(l[0], `"${l}" under ${tab}`).toBe(l[0].toUpperCase());
      }
    }
  });
});

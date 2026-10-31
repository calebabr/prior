import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ReturnsCard from '../components/analytics/ReturnsCard.jsx';
import { clearReturnsMemo } from '../hooks/useReturns.js';
import { setToken } from '../services/api.js';
import { mockReturns, mockReturnsFills } from '../services/mockAnalytics.js';
import { formatPeriodReturn } from '../utils/format.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

const NOW = Date.parse('2026-10-31T21:00:00Z');
const qs = (call) => new URL(call.path, 'http://x').searchParams;
const fromMock = (call) => mockReturns(qs(call), { mode: 'demo', nowMs: NOW });
const fillsMock = (call) => mockReturnsFills(qs(call), { mode: 'demo', nowMs: NOW });

function period(over = {}) {
  const base = {
    start_local: '2026-10-28', end_local: '2026-10-28', start_utc: '2026-10-28T05:00:00Z', end_utc: '2026-10-29T05:00:00Z',
    label: 'Oct 28', partial: false, elapsed_hours: null, realized_pnl: 1.2, by_strategy: { maker: 0.2, arb: 1.0 },
    settled_fills: 7, fills_by_strategy: { maker: 2, arb: 5 }, base: 500, return_rate: 0.0024, return_reason: 'ok',
    flows_in_period: { count: 0, net: 0, deposits: 0, withdrawals: 0 }, equity_change: 1.2,
    you: { settled_fills: 0, realized_pnl: 0 }, small_sample: false, empty: false, ...over,
  };
  return base;
}
function body(periods, over = {}) {
  return {
    env: 'demo', days: 30, granularity: 'day', tz_used: 'UTC', generated_utc: '2026-10-31T21:00:00Z', attribution: 'x',
    label: 'demo, indicative only', small_sample_threshold: 5, min_base: 10, truncated: false, periods,
    totals: { realized_pnl: periods.reduce((a, p) => a + p.realized_pnl, 0), settled_fills: periods.reduce((a, p) => a + p.settled_fills, 0) },
    notes: [], ...over,
  };
}
const three = () => [
  period({ start_local: '2026-10-29', label: 'Oct 29' }),
  period({ start_local: '2026-10-30', label: 'Oct 30', realized_pnl: -2.1, by_strategy: { maker: -2.1, arb: 0 }, settled_fills: 3, small_sample: true, return_rate: -0.0041 }),
  period({ start_local: '2026-10-31', label: 'Oct 31', partial: true, elapsed_hours: 21, realized_pnl: 0.5, by_strategy: { maker: 0.5, arb: 0 } }),
];

function mount(props = {}) {
  return render(
    <MemoryRouter>
      <ReturnsCard days={30} env={undefined} onUseDays={() => {}} {...props} />
    </MemoryRouter>,
  );
}
const returnsCalls = (calls) => calls.filter((c) => c.key === 'GET /api/analytics/returns');

beforeEach(() => {
  setToken('tok');
  clearReturnsMemo();
});
afterEach(() => vi.unstubAllGlobals());

describe('Returns card states', () => {
  it('populated: one bar per day, readout of the newest, key and scope lines', async () => {
    stubApi({ 'GET /api/analytics/returns': fromMock });
    mount();
    const bars = await screen.findAllByTestId('returns-bar');
    expect(bars).toHaveLength(30);
    expect(screen.getByTestId('returns-readout')).toHaveTextContent(/settled fill/);
    expect(screen.getByRole('list', { name: 'Chart key' })).toHaveTextContent(/Maker/);
    expect(screen.getByRole('list', { name: 'Chart key' })).toHaveTextContent(/Arbitrage/);
    expect(screen.getByTestId('scope-returns')).toBeInTheDocument();
    expect(screen.getByText(/Percentages are not additive/)).toBeInTheDocument();
    expect(screen.getByTestId('returns-label')).toHaveTextContent('demo, indicative only');
    expect(screen.getByRole('img', { name: /day returns, 30 periods/ })).toBeInTheDocument();
  });

  it('partial period: the readout and the bar say "so far", with a dashed outline', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body(three()) });
    mount();
    await screen.findAllByTestId('returns-bar');
    expect(screen.getByTestId('returns-readout')).toHaveTextContent('Today so far');
    expect(screen.getAllByTestId('returns-bar')[2]).toHaveAccessibleName(/Today so far/);
    expect(document.querySelector('[data-partial="true"] .rt-outline-dashed')).not.toBeNull();
    expect(screen.getByText(/still running \(so far\)/)).toBeInTheDocument();
  });

  it('empty: explains and offers 90 days, which asks the page for it', async () => {
    const onUseDays = vi.fn();
    stubApi({ 'GET /api/analytics/returns': () => body([period({ realized_pnl: 0, by_strategy: { maker: 0, arb: 0 }, settled_fills: 0, empty: true })]) });
    mount({ onUseDays });
    expect(await screen.findByText('No settled fills in this window.')).toBeInTheDocument();
    expect(screen.queryByTestId('returns-bar')).toBeNull();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Use 90 days' }));
    expect(onUseDays).toHaveBeenCalledWith(90);
  });

  it('loading: a skeleton while the request is open', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    mount();
    expect(await screen.findByTestId('skeleton')).toBeInTheDocument();
    expect(screen.getByText('Loading returns')).toBeInTheDocument();
    expect(screen.getByRole('radiogroup', { name: 'Return period' })).toBeInTheDocument(); // controls stay live
  });

  it('error: plain message with Retry, then the data on retry', async () => {
    let n = 0;
    stubApi({ 'GET /api/analytics/returns': () => (++n === 1 ? [500, { detail: 'boom' }] : body(three())) });
    mount();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/boom|error|failed/i);
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findAllByTestId('returns-bar')).toHaveLength(3);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('small sample: faded bar, marker and the note', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body(three()) });
    mount();
    await screen.findAllByTestId('returns-bar');
    expect(document.querySelectorAll('[data-small="true"]')).toHaveLength(1);
    expect(document.querySelectorAll('.rt-nmark')).toHaveLength(1);
    expect(screen.getByText(/Faded bars have fewer than 5 settled fills/)).toBeInTheDocument();
    expect(screen.getAllByTestId('returns-bar')[1]).toHaveAccessibleName(/small sample/);
  });

  it('negative bars: typographic minus, readout follows the selected bar', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body(three()) });
    mount();
    const bars = await screen.findAllByTestId('returns-bar');
    expect(bars[1]).toHaveAccessibleName('Oct 30: −$2.10, −0.41%, 3 settled fills, small sample');
    await userEvent.setup().click(bars[1]);
    expect(screen.getByTestId('returns-dollar')).toHaveTextContent('−$2.10');
    expect(screen.getByTestId('returns-dollar')).toHaveClass('neg');
    expect(screen.getByTestId('returns-pct')).toHaveTextContent('−0.41%');
    expect(screen.getByTestId('returns-readout')).toHaveTextContent(/Small sample: fewer than 5/);
    expect(document.body.textContent).not.toMatch(/-\$\d/);
  });

  it('null return rate: dollar stays, percent is a dash with the reason', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body([period({ return_rate: null, base: null, return_reason: 'no_start_snapshot' })]) });
    mount();
    await screen.findAllByTestId('returns-bar');
    expect(screen.getByTestId('returns-dollar')).toHaveTextContent('+$1.20');
    const pct = screen.getByTestId('returns-pct');
    expect(pct).toHaveTextContent('—');
    expect(pct.querySelector('[title]')).toHaveAttribute('title', expect.stringMatching(/starting balance/));
  });
});

describe('Granularity requests', () => {
  it('Day uses the page window, Week asks for 84 days, Month for 180; never gated', async () => {
    const calls = stubApi({ 'GET /api/analytics/returns': fromMock });
    mount({ days: 90 });
    await screen.findAllByTestId('returns-bar');
    expect(qs(returnsCalls(calls)[0]).get('days')).toBe('90');
    expect(qs(returnsCalls(calls)[0]).get('granularity')).toBe('day');
    const group = screen.getByRole('radiogroup', { name: 'Return period' });
    for (const r of within(group).getAllByRole('radio')) expect(r).not.toBeDisabled();
    const user = userEvent.setup();
    await user.click(within(group).getByRole('radio', { name: 'Week' }));
    await waitFor(() => expect(returnsCalls(calls)).toHaveLength(2));
    expect(qs(returnsCalls(calls)[1]).get('granularity')).toBe('week');
    expect(qs(returnsCalls(calls)[1]).get('days')).toBe('84');
    await waitFor(() => expect(screen.getAllByTestId('returns-bar')).toHaveLength(12));
    await user.click(within(group).getByRole('radio', { name: 'Month' }));
    await waitFor(() => expect(returnsCalls(calls)).toHaveLength(3));
    expect(qs(returnsCalls(calls)[2]).get('days')).toBe('180');
    expect(qs(returnsCalls(calls)[2]).get('tz')).toBeTruthy();
  });

  it('flipping back inside a minute does not refetch', async () => {
    const calls = stubApi({ 'GET /api/analytics/returns': fromMock });
    mount();
    await screen.findAllByTestId('returns-bar');
    const user = userEvent.setup();
    await user.click(screen.getByRole('radio', { name: 'Week' }));
    await screen.findAllByText(/Week shows the last 12 weeks/);
    await user.click(screen.getByRole('radio', { name: 'Day' }));
    await waitFor(() => expect(screen.getAllByTestId('returns-bar')).toHaveLength(30));
    expect(returnsCalls(calls)).toHaveLength(2);
  });
});

describe('Table and sheet', () => {
  it('table view: caption, columns and a row per period with a Fills button', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body(three()) });
    mount();
    await screen.findAllByTestId('returns-bar');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Show as table' }));
    const table = screen.getByRole('table');
    expect(table).toHaveAccessibleName(/Realized P&L and return for each day/);
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent.trim())).toEqual(
      ['Period', 'Maker', 'Arbitrage', 'Total', 'Fills', 'Return', 'Details'],
    );
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent('Today so far'); // newest first
    expect(rows[1]).toHaveTextContent('−$2.10');
    expect(rows[1]).toHaveTextContent('−0.41%');
    expect(screen.queryByTestId('returns-bars')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Show as bars' }));
    expect(screen.getByTestId('returns-bars')).toBeInTheDocument();
  });

  it('tapping a bar then Details opens the fills sheet, which loads the route and closes', async () => {
    const calls = stubApi({ 'GET /api/analytics/returns': fromMock, 'GET /api/analytics/returns/fills': fillsMock });
    mount();
    const bars = await screen.findAllByTestId('returns-bar');
    const user = userEvent.setup();
    await user.click(bars[28]);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    const sheet = await screen.findByTestId('returns-sheet');
    const fills = calls.filter((c) => c.key === 'GET /api/analytics/returns/fills');
    expect(fills).toHaveLength(1);
    expect(qs(fills[0]).get('start')).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(qs(fills[0]).get('granularity')).toBe('day');
    expect(await within(sheet).findAllByText(/KXDEMOA/)).not.toHaveLength(0);
    expect(within(sheet).getByText('Your own trades are not included.')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.click(within(sheet).getByRole('button', { name: 'Close' }));
    expect(screen.queryByTestId('returns-sheet')).toBeNull();
  });

  it('Enter on the focused bar opens the sheet; arrows move the selection', async () => {
    stubApi({ 'GET /api/analytics/returns': () => body(three()), 'GET /api/analytics/returns/fills': () => [200, { period: {}, total: 0, offset: 0, limit: 50, fills: [] }] });
    mount();
    const bars = await screen.findAllByTestId('returns-bar');
    bars[2].focus();
    const user = userEvent.setup();
    await user.keyboard('{ArrowLeft}');
    expect(bars[1]).toHaveAttribute('aria-selected', 'true');
    expect(bars[1]).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(await screen.findByTestId('returns-sheet')).toBeInTheDocument();
  });
});

describe('Layout and safety', () => {
  it('the plot scrolls inside itself; nothing can widen the page', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/returns.css'), 'utf8');
    expect(css).toMatch(/\.rt-scroll\s*\{[^}]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.rt-scroll\s*\{[^}]*min-width:\s*0/);
    expect(css).toMatch(/\.rt-tablewrap\s*\{[^}]*overflow-x:\s*auto/);
    expect(css).not.toMatch(/\bwidth:\s*\d{3,}px/); // no fixed pixel widths
  });

  it('formatPeriodReturn: two decimals under 1%, one from 1%, dash for null', () => {
    expect(formatPeriodReturn(0.0041)).toBe('+0.41%');
    expect(formatPeriodReturn(-0.0041)).toBe('−0.41%');
    expect(formatPeriodReturn(0.027)).toBe('+2.7%');
    expect(formatPeriodReturn(null)).toBe('—');
  });

  it('Stop stays visible on the Analytics route with the Returns card showing', async () => {
    stubApi({
      'GET /api/status': STATUS,
      'GET /api/analytics/summary': () => [500, { detail: 'x' }],
      'GET /api/analytics/returns': fromMock,
    });
    renderApp('/analytics');
    expect((await screen.findAllByRole('button', { name: /stop/i })).length).toBeGreaterThan(0);
  });
});

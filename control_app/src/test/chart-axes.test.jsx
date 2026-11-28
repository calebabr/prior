import userEvent from '@testing-library/user-event';
// Axis labels + units on every chart (TASKS backlog, owner request 2026-10-13).
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import Sparkline from '../components/Sparkline.jsx';
import CiBar from '../components/CiBar.jsx';
import PnlCurve from '../components/analytics/PnlCurve.jsx';
import Calibration from '../components/analytics/Calibration.jsx';
import FillQualityTrend from '../components/analytics/FillQualityTrend.jsx';
import ArbHistory from '../components/analytics/ArbHistory.jsx';
import { CiScale, ciTicks } from '../components/analytics/WinRateCiBar.jsx';
import { dedupeTicks, indexTicks, minMidMax, moneyDigits, stepTicks } from '../utils/chart.js';
import { money } from '../utils/format.js';
import { STATUS, renderApp, stubApi } from './helpers.jsx';

const ticksOf = (root, axis) => [...root.querySelectorAll(`.chart-${axis}tick`)].map((n) => n.textContent);
const posOf = (root, axis) =>
  [...root.querySelectorAll(`.chart-${axis}tick`)].map((n) => {
    const s = n.style;
    if (axis === 'y') return parseFloat(s.top) / 100;
    return s.right ? 1 - parseFloat(s.right) / 100 : parseFloat(s.left) / 100;
  });
const title = (root, axis) => root.querySelector(`[data-axis="${axis}"]`)?.textContent;

function noOverlap(positions, gap) {
  const p = positions.slice().sort((a, b) => a - b);
  for (let i = 1; i < p.length; i++) expect(p[i] - p[i - 1]).toBeGreaterThanOrEqual(gap - 1e-9);
}

const DAYS = Array.from({ length: 31 }, (_, i) => new Date(Date.UTC(2026, 8, 13 + i)).toISOString().slice(0, 10));

describe('axis helpers', () => {
  it('minMidMax gives min / mid / max, one tick when flat, and drops duplicate labels', () => {
    const pos = (v) => 1 - (v - 10) / 10;
    expect(minMidMax(10, 20, String, pos).map((t) => t.label)).toEqual(['20', '15', '10']);
    expect(minMidMax(5, 5, String, () => 0.5)).toEqual([{ v: 5, label: '5', pos: 0.5 }]);
    const fmt = (v) => money(v, { digits: 0 });
    expect(minMidMax(10, 10.4, fmt, (v) => 1 - (v - 10) / 0.4).map((t) => t.label)).toEqual(['$10']);
    expect(minMidMax(NaN, 3, String, pos)).toEqual([]);
  });

  it('indexTicks picks first / middle / last and never overlaps', () => {
    const t = indexTicks(31, (i) => `d${i}`, (i) => i / 30);
    expect(t.map((x) => x.label)).toEqual(['d0', 'd15', 'd30']);
    expect(indexTicks(2, (i) => `d${i}`, (i) => i).map((x) => x.label)).toEqual(['d0', 'd1']);
    expect(indexTicks(1, () => 'only', () => 0).map((x) => x.label)).toEqual(['only']);
    expect(indexTicks(0, () => 'x', () => 0)).toEqual([]);
    // Same label twice (e.g. two snapshots on one day) is shown once.
    expect(indexTicks(3, () => 'Oct 1', (i) => i / 2)).toHaveLength(1);
  });

  it('dedupeTicks drops NaN positions and ticks closer than the gap', () => {
    const t = dedupeTicks([{ label: 'a', pos: 0 }, { label: 'b', pos: 0.05 }, { label: 'c', pos: NaN }, { label: 'd', pos: 1 }], 0.1);
    expect(t.map((x) => x.label)).toEqual(['a', 'd']);
  });

  it('stepTicks and moneyDigits', () => {
    expect(stepTicks(0.75, 1, 0.05)).toEqual([0.75, 0.8, 0.85, 0.9, 0.95, 1]);
    expect(stepTicks(1, 0, 0.1)).toEqual([]);
    expect(moneyDigits(236.99, 272.73)).toBe(0);
    expect(moneyDigits(100, 101.5)).toBe(2);
  });
});

describe('Sparkline axes', () => {
  const values = [236.99, 250, 245, 272.73];
  const labels = ['Sep 13', 'Sep 23', 'Oct 3', 'Oct 13'];

  it('renders $ y ticks (max / mid / min), date x ticks and axis titles with units', () => {
    render(
      <Sparkline
        values={values}
        labels={labels}
        yFormat={(v) => money(v, { digits: 0 })}
        yTitle="Balance + positions ($)"
        xTitle="Date"
        testId="spark"
        label="Balance + positions in dollars"
      />,
    );
    const root = screen.getByTestId('spark');
    expect(title(root, 'y')).toBe('Balance + positions ($)');
    expect(title(root, 'x')).toBe('Date');
    expect(ticksOf(root, 'y')).toEqual(['$273', '$255', '$237']);
    expect(ticksOf(root, 'x')).toEqual(['Sep 13', 'Oct 3', 'Oct 13']);
    // Highest value at the top of the plot, lowest at the bottom.
    const [top, , bottom] = posOf(root, 'y');
    expect(top).toBeLessThan(bottom);
    // Ticks and titles are hidden from screen readers; the svg label carries the units.
    expect(root.querySelector('.chart-y')).toHaveAttribute('aria-hidden', 'true');
    expect(root.querySelector('.chart-x')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('img')).toHaveAccessibleName(/dollars/);
    expect(root.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it('flat data gives one y tick in the middle and no NaN', () => {
    render(<Sparkline values={[100, 100, 100]} labels={['a', 'b', 'c']} yFormat={(v) => money(v)} yTitle="$" testId="flat" />);
    const root = screen.getByTestId('flat');
    expect(ticksOf(root, 'y')).toEqual(['$100.00']);
    expect(posOf(root, 'y')[0]).toBeCloseTo(0.5, 6);
    expect(root.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it('skips non-numeric points and keeps labels aligned with the values kept', () => {
    render(<Sparkline values={[1, null, 3]} labels={['W1', 'W2', 'W3']} yTitle="t" testId="gap" />);
    const root = screen.getByTestId('gap');
    expect(ticksOf(root, 'x')).toEqual(['W1', 'W3']);
    expect(root.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it('without axis props it stays a bare svg (back-compat)', () => {
    const { container } = render(<Sparkline values={[1, 2]} />);
    expect(container.querySelector('.chart')).toBeNull();
    expect(container.querySelector('svg.sparkline')).not.toBeNull();
  });
});

describe('Overview balance chart', () => {
  it('shows a $ scale and first / middle / last dates under the 30-day balance line', async () => {
    const snaps = DAYS.map((d, i) => ({ ts: `${d}T12:00:00Z`, cash: 200 + i, exposure: i === 30 ? 42.73 : 36.99 }));
    stubApi({ 'GET /api/status': STATUS, 'GET /api/history': { snapshots: snaps } });
    renderApp('/');
    await userEvent.setup().click(await screen.findByRole('button', { name: /Balance, 30 Days/ }));
    const root = await screen.findByTestId('equity-chart');
    expect(title(root, 'y')).toBe('Balance, positions at cost ($)');
    expect(title(root, 'x')).toBe('Date');
    const lo = 236.99;
    const hi = 272.73;
    expect(ticksOf(root, 'y')).toEqual([money(hi, { digits: 0 }), money((lo + hi) / 2, { digits: 0 }), money(lo, { digits: 0 })]);
    expect(ticksOf(root, 'x')).toEqual(['Sep 13', 'Sep 28', 'Oct 13']);
    expect(within(root).getByRole('img')).toHaveAccessibleName(/in dollars.*Sep 13 to Oct 13/);
    // The owner's summary line stays.
    expect(screen.getByText(/Balance with positions at cost \(what was paid, not market value\), 30 days: \$236\.99 → \$272\.73/)).toBeInTheDocument();
  });
});

describe('PnlCurve axes', () => {
  const pts = [
    { date: '2026-10-01', cum_pnl: 2, drawdown: 0 },
    { date: '2026-10-02', cum_pnl: -3, drawdown: -5 },
    { date: '2026-10-03', cum_pnl: 8, drawdown: 0 },
    { date: '2026-10-04', cum_pnl: 12, drawdown: 0 },
    { date: '2026-10-05', cum_pnl: 10, drawdown: -2 },
  ];

  it('labels cumulative P&L ($), drawdown ($) and dates, with max / zero / min ticks', () => {
    render(<PnlCurve points={pts} />);
    const main = screen.getByTestId('pnl-main');
    const dd = screen.getByTestId('pnl-drawdown');
    expect(title(main, 'y')).toBe('Cumulative P&L ($)');
    expect(title(dd, 'y')).toBe('Drawdown From Peak ($)');
    expect(title(dd, 'x')).toBe('Date');
    const y = ticksOf(main, 'y');
    expect(y).toContain('+$12');
    expect(y).toContain('-$3');
    expect(y).toContain('$0');
    noOverlap(posOf(main, 'y'), 0.14);
    expect(ticksOf(dd, 'y')).toEqual(['$0', '-$5']);
    expect(ticksOf(dd, 'x')).toEqual(['Oct 1', 'Oct 3', 'Oct 5']);
    expect(screen.getByRole('img')).toHaveAccessibleName(/Cumulative P&L in dollars by date, from Oct 1 to Oct 5/);
  });

  it('the y gutters of the two strips share one width so the plots line up', () => {
    render(<PnlCurve points={pts} />);
    const a = screen.getByTestId('pnl-main').querySelector('.chart-sizer').getAttribute('data-sizer');
    const b = screen.getByTestId('pnl-drawdown').querySelector('.chart-sizer').getAttribute('data-sizer');
    expect(a).toBe(b);
  });

  it('flat zero curve: single $0 tick, no NaN', () => {
    const { container } = render(
      <PnlCurve points={[{ date: '2026-10-01', cum_pnl: 0, drawdown: 0 }, { date: '2026-10-02', cum_pnl: 0, drawdown: 0 }]} />,
    );
    expect(ticksOf(screen.getByTestId('pnl-main'), 'y')).toEqual(['$0']);
    expect(ticksOf(screen.getByTestId('pnl-drawdown'), 'x')).toEqual(['Oct 1', 'Oct 2']);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});

describe('Calibration axes', () => {
  const buckets = [
    { bucket: '0.44-0.46', lo: 0.44, hi: 0.46, n: 20, markets: 10, win_rate: 0.5, ci_low: 0.42, ci_high: 0.56, avg_price: 0.45 },
    { bucket: '0.54-0.56', lo: 0.54, hi: 0.56, n: 50, markets: 30, win_rate: 0.57, ci_low: 0.55, ci_high: 0.59, avg_price: 0.55 },
  ];

  it('x is price paid in cents, y is win rate in %, and the diagonal is labelled fair price', () => {
    render(<Calibration buckets={buckets} />);
    const root = screen.getByTestId('calibration-chart');
    expect(title(root, 'x')).toBe('Price Paid (¢)');
    expect(title(root, 'y')).toBe('Win Rate (%)');
    expect(ticksOf(root, 'x')).toEqual(['40¢', '45¢', '50¢', '55¢', '60¢']);
    const y = ticksOf(root, 'y');
    expect(y[y.length - 1]).toBe('100%');
    expect(y.every((t) => /^\d+%$/.test(t))).toBe(true);
    noOverlap(posOf(root, 'x'), 0.15);
    expect(root.querySelector('[data-mark="fair"]')).not.toBeNull();
    expect(within(root).getByText('fair price')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /win rate in percent against price paid in cents/i })).toBeInTheDocument();
    expect(root.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});

describe('FillQualityTrend axes', () => {
  it('front-of-line rate (%) and contracts ahead by week', () => {
    const weeks = [
      { week: '2026-W38', fills: 10, front: 6, trade_through: 4, front_rate: 0.6, avg_queue_ahead: 12, requotes: 1 },
      { week: '2026-W39', fills: 10, front: 8, trade_through: 2, front_rate: 0.8, avg_queue_ahead: 20, requotes: 0 },
      { week: '2026-W40', fills: 10, front: 7, trade_through: 3, front_rate: 0.7, avg_queue_ahead: 4, requotes: 2 },
    ];
    render(<FillQualityTrend weeks={weeks} />);
    const fr = screen.getByTestId('fq-front-chart');
    expect(title(fr, 'y')).toBe('Front-of-line rate (%)');
    expect(title(fr, 'x')).toBe('Week');
    expect(ticksOf(fr, 'y')).toEqual(['80%', '70%', '60%']);
    expect(ticksOf(fr, 'x')).toEqual(['W38', 'W39', 'W40']);
    const ah = screen.getByTestId('fq-ahead-chart');
    expect(title(ah, 'y')).toMatch(/Contracts ahead/);
    expect(ticksOf(ah, 'y')).toEqual(['20', '12', '4']);
  });

  it('a single week draws no trend chart', () => {
    render(<FillQualityTrend weeks={[{ week: '2026-W40', fills: 3, front: 1, trade_through: 1, front_rate: 0.5, avg_queue_ahead: 2 }]} />);
    expect(screen.queryByTestId('fq-front-chart')).toBeNull();
    expect(screen.queryByTestId('fq-ahead-chart')).toBeNull();
  });
});

describe('ArbHistory axes', () => {
  it('x is date, y is a whole-number count', () => {
    const daily = DAYS.slice(0, 5).map((date, i) => ({ date, opportunities: [0, 4, 1, 0, 2][i], executed: i === 1 ? 1 : 0 }));
    render(<ArbHistory arb={{ opportunities: 7, executed: 1, pnl: 0.5, daily }} />);
    const root = screen.getByTestId('arb-chart');
    expect(title(root, 'y')).toBe('Opportunities per day (count)');
    expect(title(root, 'x')).toBe('Date');
    expect(ticksOf(root, 'y')).toEqual(['4', '0', '2']);
    expect(ticksOf(root, 'x')).toEqual(['Sep 13', 'Sep 15', 'Sep 17']);
    expect(screen.getByRole('img')).toHaveAccessibleName(/per day \(count\)/);
  });

  it('max of 1 shows 0 and 1 only (no fractional tick)', () => {
    const daily = DAYS.slice(0, 3).map((date, i) => ({ date, opportunities: i === 1 ? 1 : 0, executed: 0 }));
    render(<ArbHistory arb={{ opportunities: 1, executed: 0, pnl: 0, daily }} />);
    expect(ticksOf(screen.getByTestId('arb-chart'), 'y')).toEqual(['1', '0']);
  });
});

describe('CI scales', () => {
  it('CiScale shows % ticks every 5 pts aligned with the bars', () => {
    render(<CiScale domain={[0.4, 0.6]} />);
    const root = screen.getByTestId('ci-scale');
    expect(ticksOf(root, 'x')).toEqual(['40%', '45%', '50%', '55%', '60%']);
    expect(title(root, 'y')).toBe('Win Rate (%), ticks every 5%');
    const p = posOf(root, 'x');
    expect(p[0]).toBeCloseTo(4 / 300, 3);
    expect(p[2]).toBeCloseTo(0.5, 3);
  });

  it('ciTicks widens the step on a wide domain (at most 6 ticks)', () => {
    expect(ciTicks([0.5, 1]).values.length).toBeLessThanOrEqual(6);
    expect(ciTicks([0, 1]).step).toBe(0.2);
  });

  it('Markets CiBar: return % ticks from -8% to +8% with units', () => {
    render(<CiBar roi={0.02} low={-0.01} high={0.05} />);
    const root = screen.getByTestId('ci-bar-scale');
    expect(ticksOf(root, 'x')).toEqual(['-8%', '-4%', '0%', '+4%', '+8%']);
    expect(screen.getByText('Actual Return +2.0%')).toBeInTheDocument();
    expect(screen.getByTestId('ci-ends')).toHaveTextContent('−1.0%');
    expect(screen.getByTestId('ci-ends')).toHaveTextContent('+5.0%');
    expect(screen.getByRole('img')).toHaveAccessibleName(/scale from -8% to \+8% in 4% steps/);
  });
});

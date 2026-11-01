import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const R = (lo, mid, hi, ci90) => ({ lo, mid, hi, ...(ci90 ? { ci90 } : {}) });
const SUMMARY = (over = {}) => ({
  enabled: true,
  standin: false,
  status: { days_covered: 10, settled_episodes: 40, central_fills: 22.5, too_early: false, last_pass_ts: null, pass_ok_pct: 1, truncated: false },
  headline: { pnl: R(1, 5, 9, [0.5, 9.5]), roi: R(0.004, 0.01, 0.016, [0.003, 0.017]), fill_rate: R(0.2, 0.3, 0.4), peak_capital: 300 },
  headline_zero_fee: { pnl: R(2, 6, 10, [1, 11]), roi: R(0.005, 0.011, 0.017, [0.004, 0.018]), fill_rate: R(0.2, 0.3, 0.4), peak_capital: 300 },
  by_series: [{ series: 'KXDEMOA', episodes: 12, fills_mid: 7, pnl: R(1, 2, 3) }],
  by_cell: [{ cell: ['a', 'b'], episodes: 12, fills_mid: 7, pnl: R(1, 2, 3) }],
  daily: [
    { day: '2026-10-01', cum_lo: 0.1, cum_mid: 0.4, cum_hi: 0.9 },
    { day: '2026-10-02', cum_lo: 0.3, cum_mid: 0.9, cum_hi: 1.8 },
  ],
  fee_mode: 'maker',
  caveats: [],
  ...over,
});

const base = (summary) => stubApi({ 'GET /api/status': STATUS, 'GET /api/shadow/summary': summary });

describe('Shadow estimate page', () => {
  it('shows the banner, a range, the chart and the caveats, and switches to zero fees', async () => {
    base(SUMMARY());
    const user = userEvent.setup();
    renderApp('/shadow');
    expect(await screen.findByText(/Simulated\. No orders were placed/)).toBeInTheDocument();
    expect(screen.getByText(/after maker fees/i)).toBeInTheDocument();
    expect(screen.getByTestId('shadow-band')).toBeInTheDocument();
    expect(screen.getByText(/what it cannot capture/i)).toBeInTheDocument();
    expect(screen.queryByText(/placeholder/i)).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Zero fees' }));
    expect(screen.getByText(/zero fees/i, { selector: 'h3' })).toBeInTheDocument();
  });

  it('says the estimates are the same, with no dead toggle, when no market carries a maker fee', async () => {
    const same = SUMMARY().headline;
    base(SUMMARY({ headline_zero_fee: JSON.parse(JSON.stringify(same)) }));
    renderApp('/shadow');
    expect(await screen.findByTestId('fees-same')).toHaveTextContent(/No maker fee applies/i);
    expect(screen.queryByRole('button', { name: 'Zero fees' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'After fees' })).toBeNull();
    expect(screen.getByText('Estimated P&L', { selector: 'h3' })).toBeInTheDocument();
  });

  it('keeps the fee toggle when after-fee and zero-fee estimates differ', async () => {
    base(SUMMARY());
    renderApp('/shadow');
    expect(await screen.findByRole('button', { name: 'Zero fees' })).toBeInTheDocument();
    expect(screen.queryByTestId('fees-same')).toBeNull();
  });

  it('warns on the stand-in and explains the too-early state', async () => {
    base(SUMMARY({ standin: true, status: { ...SUMMARY().status, too_early: true } }));
    renderApp('/shadow');
    expect(await screen.findByText(/uses a placeholder/i)).toBeInTheDocument();
    expect(screen.getByText(/too early to estimate/i)).toBeInTheDocument();
    expect(screen.queryByTestId('shadow-band')).toBeNull();
  });

  it('says Shadow is not estimating when the simulated ceiling is full, instead of an estimate', async () => {
    base(SUMMARY({ status: { ...SUMMARY().status, ceiling_full: true, held_cost: 248.51, ceiling: 75 } }));
    renderApp('/shadow');
    const note = await screen.findByTestId('ceiling-full');
    expect(note).toHaveTextContent('Shadow is not estimating: the simulated open-position ceiling is full; it clears as positions settle');
    expect(note).toHaveTextContent('$248.51');
    expect(screen.queryByTestId('headline-block')).toBeNull();
    expect(screen.queryByTestId('shadow-band')).toBeNull();
  });

  it('shows the estimate as before when the ceiling is not full', async () => {
    base(SUMMARY({ status: { ...SUMMARY().status, ceiling_full: false } }));
    renderApp('/shadow');
    expect(await screen.findByTestId('headline-block')).toBeInTheDocument();
    expect(screen.queryByTestId('ceiling-full')).toBeNull();
  });

  it('explains how to turn shadow on when disabled', async () => {
    base(SUMMARY({ enabled: false, headline: null }));
    renderApp('/shadow');
    expect(await screen.findByText(/shadow estimate is off/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Server' })).toHaveAttribute('href', '/server');
  });

  const NEW_H = (over = {}) => ({
    ...SUMMARY().headline,
    headline_status: 'ok',
    break_even_win_rate: 0.904,
    observed_win_rate: 1,
    n_settled_fills: 92,
    peak_held_capital: 773.9,
    return_on_held: 0.122,
    first_fill_per_market: { cost: 197.23, pnl: 15.32, roi: 0.078, markets: 22 },
    ...over,
  });

  it('greys the headline with a reason while too early and shows no interval when ci90 is null', async () => {
    const h = NEW_H({ headline_status: 'too_early', pnl: R(1, 5, 9, null), roi: R(0.004, 0.01, 0.016, null) });
    base(SUMMARY({ headline: h, status: { ...SUMMARY().status, headline_status: 'too_early', too_early: true } }));
    renderApp('/shadow');
    expect(await screen.findByTestId('too-early-reason')).toHaveTextContent(/7 close days/);
    expect(screen.getByTestId('headline-block')).toHaveClass('sh-grey');
    expect(screen.queryByText(/90%:/)).toBeNull();
  });

  it('shows break-even vs observed win rate with n, first fill, held capital and return on held', async () => {
    base(SUMMARY({ headline: NEW_H() }));
    renderApp('/shadow');
    const box = await screen.findByTestId('honesty-stats');
    expect(box).toHaveTextContent('90.4%');
    expect(box).toHaveTextContent('n = 92 settled fills');
    expect(box).toHaveTextContent(/First fill per market/);
    expect(box).toHaveTextContent('22 markets');
    expect(box).toHaveTextContent('773.90');
    expect(box).toHaveTextContent('12.2%');
    expect(screen.getByTestId('headline-block')).not.toHaveClass('sh-grey');
  });

  it('shows the capped count and the close-day label', async () => {
    base(SUMMARY({ headline: NEW_H(), day_basis: 'close_day', status: { ...SUMMARY().status, capped_episodes: 268 } }));
    renderApp('/shadow');
    expect(await screen.findByTestId('honesty-stats')).toHaveTextContent('268');
    expect(screen.getByTestId('day-basis')).toHaveTextContent(/market close day/);
  });

  const VAR = (interval, baseline, extra = {}) => ({
    interval, baseline, pnl: R(1, 5, 9), roi: R(0, 0.01, 0.02), fill_rate: R(0.2, 0.3, 0.4),
    central_fills: 20, settled_episodes: 10, n_settled_fills: 8, capped_episodes: 0, days_covered: 3,
    too_early: true, headline_status: 'too_early', peak_held_capital: 50, last_pass_ts: null, pass_ok_pct: 1, ...extra,
  });

  it('shows the variant table with the paired difference (null ci) and the reliability note', async () => {
    base(SUMMARY({
      headline: NEW_H(),
      variants_enabled: true,
      variants: [VAR(5, false), VAR(10, true)],
      variant_compare: [{ interval: 5, vs: 10, days: 3, pnl_diff: { mid: 1.5, ci90: null }, too_early: true, ci_reliable: false, reliability_note: 'Too few days to separate the cadences.' }],
    }));
    renderApp('/shadow');
    const tbl = await screen.findByTestId('variant-table');
    expect(tbl).toHaveTextContent('5 min');
    expect(tbl).toHaveTextContent('10 min (baseline)');
    expect(tbl).toHaveTextContent('+$1.50');
    expect(tbl).not.toHaveTextContent('90%:');
    expect(screen.getByTestId('variant-note')).toHaveTextContent(/Too few days/);
  });

  it('shows the interval when the compare ci90 is present', async () => {
    base(SUMMARY({
      headline: NEW_H(),
      variants_enabled: true,
      variants: [VAR(7, false), VAR(10, true)],
      variant_compare: [{ interval: 7, vs: 10, days: 9, pnl_diff: { mid: 1, ci90: [-0.5, 2.5] }, too_early: false, ci_reliable: true, reliability_note: '' }],
    }));
    renderApp('/shadow');
    expect(await screen.findByTestId('variant-table')).toHaveTextContent('90%:');
  });

  it('hides variants when variants_enabled is false', async () => {
    base(SUMMARY({ headline: NEW_H(), variants_enabled: false, variants: [], variant_compare: [] }));
    renderApp('/shadow');
    await screen.findByTestId('honesty-stats');
    expect(screen.queryByTestId('variant-section')).toBeNull();
  });

  it('hides the variant section when there are no variants', async () => {
    base(SUMMARY({ headline: NEW_H() }));
    renderApp('/shadow');
    await screen.findByTestId('honesty-stats');
    expect(screen.queryByTestId('variant-section')).toBeNull();
  });

  it('shows no data, never 0%, for a null roi', async () => {
    base(SUMMARY({ headline: NEW_H({ roi: { lo: null, mid: null, hi: null, ci90: null } }) }));
    renderApp('/shadow');
    await screen.findByTestId('honesty-stats');
    const h3 = screen.getByText('Estimated ROI', { selector: 'h3' });
    const block = h3.parentElement;
    expect(block).toHaveTextContent('no data');
    expect(block).not.toHaveTextContent('0.0%');
  });

  const LEGACY = (over = {}) => ({
    banner: 'legacy, uncapped, not comparable',
    episodes: 300,
    from: '2026-10-18T10:00:00Z',
    to: '2026-10-22T21:00:00Z',
    headline: {
      pnl: R(50, 94.26, 140), roi: R(0.05, 0.1035, 0.15), fill_rate: R(0.2, 0.3, 0.4),
      peak_capital: 900, peak_held_capital: 773.9, break_even_win_rate: 0.9, observed_win_rate: 1,
      first_fill_per_market: { cost: 197, pnl: 15, roi: 0.078, markets: 22 },
    },
    status: { settled_episodes: 250, days_covered: 4 },
    ...over,
  });

  it('keeps legacy numbers out of the headline and shows them in a separate collapsed card', async () => {
    base(SUMMARY({ headline: NEW_H(), legacy_banner: 'legacy, uncapped, not comparable', legacy: LEGACY() }));
    renderApp('/shadow');
    const card = await screen.findByTestId('legacy-card');
    expect(card).toHaveTextContent('Legacy episodes (before the fix)');
    expect(screen.getByTestId('legacy-banner')).toHaveTextContent('legacy, uncapped, not comparable');
    expect(card.querySelector('details')).not.toHaveAttribute('open');
    expect(screen.getByTestId('legacy-stats')).toHaveTextContent('94.26');
    expect(screen.getByTestId('headline-block')).not.toHaveTextContent('94.26');
  });

  it('shows - never 0 for null legacy numbers', async () => {
    const l = LEGACY({ headline: { pnl: R(null, null, null), roi: R(null, null, null), fill_rate: R(null, null, null) } });
    base(SUMMARY({ headline: NEW_H(), legacy: l }));
    renderApp('/shadow');
    const stats = await screen.findByTestId('legacy-stats');
    expect(stats).toHaveTextContent('-');
    expect(stats).not.toHaveTextContent('$0');
    expect(stats).not.toHaveTextContent('0.0%');
  });

  it('shows no legacy card when there is no legacy block', async () => {
    base(SUMMARY({ headline: NEW_H(), legacy: null }));
    renderApp('/shadow');
    await screen.findByTestId('honesty-stats');
    expect(screen.queryByTestId('legacy-card')).toBeNull();
  });

  it('shows the no-corrected-data state, no headline figures, and still the legacy card', async () => {
    const nulls = { pnl: R(null, null, null, null), roi: R(null, null, null, null), fill_rate: R(null, null, null),
      headline_status: 'no_corrected_data', peak_capital: null, peak_held_capital: null };
    base(SUMMARY({
      headline: nulls, headline_zero_fee: nulls,
      status: { ...SUMMARY().status, headline_status: 'no_corrected_data', too_early: true },
      legacy: LEGACY(),
    }));
    renderApp('/shadow');
    expect(await screen.findByTestId('no-corrected')).toHaveTextContent('No corrected data yet for this period');
    expect(screen.queryByTestId('headline-block')).toBeNull();
    expect(screen.queryByText(/too early to estimate/i)).toBeNull();
    expect(screen.getByTestId('legacy-card')).toBeInTheDocument();
  });
});

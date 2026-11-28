import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { armNote, armWarning, killSwitchHelp } from '../utils/arming.js';
import { MIN_RANGE_MARKETS, tooFewForRange, winRecords } from '../utils/ci.js';
import { periodOptions } from '../components/PeriodSelect.jsx';
import { todayFigure } from '../utils/pnlScopes.js';
import { overallSentence } from '../pages/Health.jsx';
import QueuePosition, { DEMO_NOTE } from '../components/QueuePosition.jsx';
import { returnHint } from '../components/analytics/ScorecardView.jsx';
import { stoppedLine } from '../utils/overview.js';
import { TO_DEMO_WARNING, TO_DEMO_ARB, TO_DEMO_ARB_LIVE, STOP_COPY } from '../utils/stopCopy.js';

// Design audit A6-A12, A14-A16, A18, A20-A24. All figures made up.

beforeEach(() => window.sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const SRC = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(SRC, rel), 'utf8');
const LIVE = { ...STATUS, desired: 'running', kill_switch: false, mode: 'live', live_allowed: true };

describe('A6: switching to demo', () => {
  it('says to Stop first, that live orders stay open and can fill, and that positions stay', async () => {
    const user = userEvent.setup();
    const calls = stubApi({ 'GET /api/status': { ...LIVE, arb: { state: 'log-only' } }, 'PUT /api/mode': { mode: 'demo' } });
    renderApp('/mode');
    await user.click(await screen.findByRole('button', { name: /switch to demo/i }));
    const box = screen.getByTestId('to-demo-warning');
    expect(box).toHaveTextContent('Tap Stop BEFORE switching, while still in live.');
    expect(box).toHaveTextContent('live orders stay open and can fill until you tap Stop, cancel them on Kalshi, or the market closes');
    expect(box).toHaveTextContent('Switching cancels nothing');
    expect(box).toHaveTextContent('Filled positions stay until settlement.');
    expect(box).toHaveTextContent(TO_DEMO_ARB);
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
    // the request itself is unchanged
    await user.type(screen.getByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Switch to demo' }));
    await screen.findByText('Mode is now DEMO');
    expect(calls.find((c) => c.method === 'PUT').body).toEqual({ mode: 'demo' });
  });

  it('says arbitrage keeps trading real money when it is live', async () => {
    const user = userEvent.setup();
    stubApi({ 'GET /api/status': { ...LIVE, arb: { state: 'live' } } });
    renderApp('/mode');
    await user.click(await screen.findByRole('button', { name: /switch to demo/i }));
    expect(screen.getByTestId('to-demo-warning')).toHaveTextContent(TO_DEMO_ARB_LIVE);
  });

  it('no copy claims a Stop reaches the other mode', () => {
    expect(STOP_COPY.alreadyStopped()).toBe('Already stopped. Stop again cancels any leftover bot orders.');
    expect(TO_DEMO_WARNING.join(' ')).not.toMatch(/Stop in demo does not/);
  });
});

describe('A7: Today under "Maker and arbitrage"', () => {
  it('uses the bot figure, and says so when only an older total exists', async () => {
    expect(todayFigure({ pnl_today: { bot: -1.36, total: 0.95 } })).toEqual({ value: -1.36, withYou: false, onlyYours: false });
    expect(todayFigure({ pnl_today: { bot: null, total: 0.95 } })).toMatchObject({ value: null, onlyYours: true });
    expect(todayFigure({ pnl_today: { total: 0.95 } })).toEqual({ value: 0.95, withYou: true, onlyYours: false });

    stubApi({ 'GET /api/status': { ...LIVE, pnl_today_state: 'settled', pnl_today: { bot: -1.36, you: 2.31, total: 0.95 } } });
    const first = renderApp('/');
    expect(await screen.findByTestId('home-today')).toHaveTextContent('Today −$1.36');
    expect(screen.getByTestId('home-today')).not.toHaveTextContent('0.95');
    first.unmount();
    window.sessionStorage.clear();
    stubApi({ 'GET /api/status': { ...LIVE, pnl_today_state: 'settled', pnl_today: { total: 0.95 } } });
    renderApp('/');
    expect(await screen.findByTestId('home-today')).toHaveTextContent('Today, incl. your trades +$0.95');
  });

  it('only your own trades settled: no bot figure, and not $0.00', async () => {
    stubApi({ 'GET /api/status': { ...LIVE, pnl_today_state: 'settled', pnl_today: { bot: null, total: 2 } } });
    renderApp('/');
    expect(await screen.findByTestId('today-empty')).toHaveTextContent('No bot settlements yet today');
  });
});

describe('A8 and A9: labels', () => {
  it('A8: the Activity 7 / 30 day tiles say they are maker only', async () => {
    stubApi({ 'GET /api/status': { ...LIVE, pnl: { d7: 1, d30: 2 } }, 'GET /api/activity': { items: [] } });
    renderApp('/activity');
    expect(await screen.findByTestId('scope-maker')).toHaveTextContent('maker fills only');
  });

  it.each([
    ['live', 'Log only, no orders'],
    ['demo', 'Log only, no live orders'],
  ])('A9: log-only arbitrage in %s mode reads "%s"', async (mode, text) => {
    stubApi({ 'GET /api/status': { ...LIVE, mode, arb: { state: 'log-only' } } });
    renderApp('/strategies');
    await waitFor(() => expect(screen.getByTestId('strategy-card-arb')).toHaveTextContent(text));
    expect(screen.getByTestId('strategy-card-arb')).not.toHaveTextContent('Demo running');
  });
});

describe('A10: arming wording follows what is live', () => {
  it('in live mode warns that real orders can follow, in demo says live trading does not start', () => {
    expect(armNote({ demo: false })).toBe('Live is on: arming lets a strategy place real orders now.');
    expect(armNote({ demo: true, arbLive: false })).toMatch(/only takes effect when live mode is on.*Checks are for live/);
    expect(armNote({ demo: true, arbLive: true })).toMatch(/Arbitrage is on and set to Live/);
    expect(armWarning('maker', { demo: false })).toEqual({ text: 'Live is on: arming lets this strategy place real orders now.', danger: true });
    expect(armWarning('maker', { demo: true }).text).toMatch(/does not start live trading/);
    // Arbitrage has its own Live setting, whatever the app mode.
    expect(armWarning('arbitrage', { demo: true, arbMode: 'live', arbOn: true })).toMatchObject({ danger: true, text: expect.stringMatching(/real orders now/) });
    expect(armWarning('arbitrage', { demo: true, arbMode: 'live', arbOn: false }).text).toMatch(/turning it on trades real money/);
    expect(armWarning('arbitrage', { demo: false, arbMode: 'demo', arbOn: true }).text).toMatch(/places no orders/);
  });

  it('the live-mode page note and the Maker dialog say it', async () => {
    const user = userEvent.setup();
    stubApi({
      'GET /api/status': LIVE,
      'GET /api/preflight': { ready: true, checks: [{ id: 'a', label: 'ok', status: 'pass' }] },
      'GET /api/arming': { maker: { armed: false, mode: 'live' }, arbitrage: { armed: false, mode: 'demo' }, live_allowed: true },
    });
    renderApp('/arming');
    expect(await screen.findByTestId('arm-note')).toHaveTextContent('Live is on: arming lets a strategy place real orders now.');
    expect(screen.getByTestId('arm-note')).not.toHaveTextContent('demo mode');
    await user.click(screen.getByRole('switch', { name: /maker/i }));
    expect(screen.getByRole('dialog')).toHaveTextContent('place real orders now');
  });
});

describe('A11: why the kill-switch check failed and how to clear it', () => {
  const check = (maker, arb) => ({ id: 'kill_switch', status: 'fail', detail: `maker kill switch: ${maker}; arbitrage kill switch: ${arb}` });
  it('gives only the steps for the switches that are on', () => {
    const both = killSwitchHelp(check('True', 'True'), { demo: false, makerArmed: true });
    expect(both.cause).toMatch(/Both kill switches are on\. Stop turns both on/);
    expect(both.steps).toEqual([
      'On Home, tap Start. In Live this starts real trading.',
      'Open Arbitrage, Controls, and tap Turn on. It comes back as log only.',
      'Wait for one run, then tap Run checks again.',
    ]);
    expect(killSwitchHelp(check('False', 'True'), { demo: true }).steps.join(' ')).not.toMatch(/Start/);
    expect(killSwitchHelp(check('True', 'False'), { demo: true }).steps.join(' ')).not.toMatch(/Turn on/);
  });

  it('in live with Maker not armed (Start would be refused) goes through demo', () => {
    const h = killSwitchHelp(check('True', 'True'), { demo: false, makerArmed: false });
    expect(h.steps[0]).toMatch(/switch to Demo \(Start is refused in Live until Maker is armed\)/);
    const at = (re) => h.steps.findIndex((s) => re.test(s));
    // Stop comes before the switch, so switching never starts real trading by itself: only Start does.
    expect(at(/^Arm, then tap Stop/)).toBeGreaterThan(0);
    expect(at(/switch to Live while the bot is stopped/)).toBeGreaterThan(at(/tap Stop/));
    expect(at(/tap Start\. This starts real trading/)).toBeGreaterThan(at(/switch to Live/));
    expect(h.steps.at(-1)).toMatch(/Go live/);
  });

  it('shows under the failed check on the Arming page', async () => {
    stubApi({
      'GET /api/status': { ...LIVE, desired: 'stopped', kill_switch: true },
      'GET /api/preflight': { ready: false, checks: [{ ...check('True', 'True'), label: 'No kill switch active' }] },
      'GET /api/arming': { maker: { armed: true, mode: 'live' }, arbitrage: { armed: false, mode: 'demo' }, live_allowed: true },
    });
    renderApp('/arming');
    const help = await screen.findByTestId('kill-switch-help');
    expect(help).toHaveTextContent('Stop turns both on');
    expect(within(help).getByRole('link', { name: 'Home' })).toHaveAttribute('href', '/');
    expect(within(help).getByRole('link', { name: 'Arbitrage' })).toHaveAttribute('href', '/strategies/arb/controls');
  });
});

describe('A12: the Scorecard does not call live money play money', () => {
  it('words the hint from the environment', () => {
    expect(returnHint('demo')).toBe('Play money and a small sample. Do not read this as a forecast.');
    expect(returnHint('live')).toMatch(/^Real money, but a small sample\./);
    expect(returnHint('all')).toMatch(/^Demo and live mixed/);
    expect(returnHint(undefined)).toBe('A small sample. Do not read this as a forecast.');
  });

  it('renders the live hint, and a neutral label when the server sends none', async () => {
    stubApi({ 'GET /api/status': LIVE, 'GET /api/analytics/scorecard': { env: 'live', return: { roi: 0.01, cost: 10, sample_settled_fills: 2 } } });
    renderApp('/analytics?tab=scorecard');
    expect(await screen.findByTestId('sc-return-hint')).toHaveTextContent('Real money');
    expect(screen.getByTestId('sc-return-label')).toHaveTextContent(/^indicative only$/);
  });
});

describe('A14 and A18: periods', () => {
  const periods = [1, 2, 3, 4, 5, 6, 7].map((i) => ({ id: `p${i}`, name: `Test ${i}`, started_at: `2026-0${i}-01T00:00:00Z`, current: i === 7 }));
  it('lists newest first in the select and the list, and the input names no demo period', async () => {
    expect(periodOptions(periods)[0].id).toBe('p7');
    stubApi({ 'GET /api/status': STATUS, 'GET /api/periods': { periods, cash_flows: [] } });
    renderApp('/performance/periods');
    const list = await screen.findByLabelText('Period timeline');
    const rows = within(list).getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Test 7');
    expect(rows[0]).toHaveTextContent('(current)');
    expect(list).not.toHaveTextContent('Test 1');
    expect(screen.getByLabelText('New Period Name')).toHaveAttribute('placeholder', 'Period name');
  });
});

describe('A15: no range on too few results', () => {
  it('needs the health check minimum of markets and at least one loss', () => {
    expect(MIN_RANGE_MARKETS).toBe(20);
    expect(tooFewForRange({ events: 4, fills: 40 })).toBe(true);
    expect(tooFewForRange({ events: 19 })).toBe(true);
    expect(tooFewForRange({ events: 20 })).toBe(false);
    expect(tooFewForRange({ fills: 8 })).toBe(true);
    expect(tooFewForRange({})).toBe(true);
    expect(tooFewForRange({ events: 30 }, { n: 31, wins: 31 })).toBe(true);
    expect(tooFewForRange({ events: 30 }, { n: 31, wins: 30 })).toBe(false);
    expect(winRecords({ by_series: [{ key: 'A', n: 3, wins: 3 }, { key: 'B', n: 2, wins: 1 }] })).toEqual({ A: { n: 3, wins: 3 }, B: { n: 2, wins: 1 }, all: { n: 5, wins: 4 } });
    expect(winRecords(null)).toEqual({});
    const sum = { overall: { fills: 1, events: 1, pnl: 0.2, roi: 0.05, ci_low: 0.04, ci_high: 0.06 } };
    expect(overallSentence(sum)).toBe('1 fill · +$0.20 P&L · return +5.00% · too few results for a range');
  });

  const market = (over) => ({ series: 'KXDEMOA', enabled: true, fills: 40, events: 30, cost: 100, pnl: 4, roi: 0.04, ci_low: 0.03, ci_high: 0.05, verdict: 'keep', ...over });
  async function openMarket(m, bySeries = []) {
    const user = userEvent.setup();
    stubApi({ 'GET /api/status': STATUS, 'GET /api/markets': { markets: [m] }, 'GET /api/analytics/summary': { by_series: bySeries } });
    renderApp('/strategies/maker/results');
    await user.click(await screen.findByRole('button', { name: /^Demo Series A/ }));
  }

  it('Maker Results hides the bar for few markets', async () => {
    await openMarket(market({ fills: 1, events: 1 }));
    expect(screen.getByTestId('range-too-few')).toHaveTextContent('Too few results for a range.');
    expect(screen.queryByTestId('ci-summary')).toBeNull();
    expect(screen.queryByTestId('ci-bar-scale')).toBeNull();
    expect(screen.getByRole('button', { name: /^Demo Series A/ })).toHaveTextContent('1 Fill');
  });

  it('Maker Results hides the bar when there has been no loss', async () => {
    await openMarket(market({}), [{ key: 'KXDEMOA', n: 40, wins: 40 }]);
    expect(await screen.findByTestId('range-too-few')).toBeInTheDocument();
    expect(screen.queryByTestId('ci-summary')).toBeNull();
  });

  it('Maker Results shows it with enough markets and a loss', async () => {
    await openMarket(market({}), [{ key: 'KXDEMOA', n: 40, wins: 38 }]);
    expect(screen.getByTestId('ci-summary')).toHaveTextContent('Likely Range');
    expect(screen.queryByTestId('range-too-few')).toBeNull();
  });

  it('Health says too few for the window and each market, and prints "1 fill"', async () => {
    const few = { fills: 1, events: 1, cost: 2, pnl: 0.2, roi: 0.052, ci_low: 0.047, ci_high: 0.057 };
    stubApi({
      'GET /api/status': STATUS,
      'GET /api/health': { summary: { overall: few, per_series: { KXDEMOA: few }, actions: { pause: [], need_more: { KXDEMOA: 1 } } }, report_text: '' },
    });
    renderApp('/health');
    const line = await screen.findByText(/too few results for a range/);
    expect(line).toHaveTextContent('1 fill ·');
    expect(screen.getAllByText('Too few results for a range.')).toHaveLength(2); // the window and the market
    expect(document.querySelector('main').textContent).not.toMatch(/4\.70%|1 fills/);
    expect(screen.getByText('1 More Fill Needed')).toBeInTheDocument();
  });
});

describe('A16 and A20: the Performance table', () => {
  const pos = (over) => ({ kind: 'position', source: 'bot', strategy: 'maker', cost: 5, value: 4, ...over });
  const LEDGER = { env: 'live', strategies: [{ strategy: 'you', open_cost: 0 }], settled_by_strategy: [] };
  const cells = (id) => within(screen.getByTestId(id)).getAllByRole('cell').map((c) => c.textContent);

  it('shows what open positions cost and are worth now, from the live positions, with no drawdown column', async () => {
    stubApi({
      'GET /api/status': LIVE,
      'GET /api/analytics/ledger': LEDGER,
      'GET /api/account/positions': [200, [pos({}), pos({ source: 'you', strategy: null, cost: 9, value: 7.5 })]],
    });
    renderApp('/performance');
    await waitFor(() => expect(cells('perf-row-you')).toContain('$9.00'));
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Strategy', 'P&L', 'Return', 'Open, paid', 'Value now']);
    expect(cells('perf-row-you').slice(2)).toEqual(['$9.00', '$7.50']);
    expect(cells('perf-row-maker').slice(2)).toEqual(['$5.00', '$4.00']);
    expect(cells('perf-total').slice(2)).toEqual(['$5.00', '$4.00']);
  });

  it('a missing figure or unreadable positions is a dash, never $0.00', async () => {
    stubApi({ 'GET /api/status': LIVE, 'GET /api/analytics/ledger': LEDGER, 'GET /api/account/positions': [200, [pos({ cost: null })]] });
    const first = renderApp('/performance');
    await waitFor(() => expect(cells('perf-row-maker').slice(2)).toEqual(['—', '$4.00']));
    first.unmount();
    window.sessionStorage.clear();
    stubApi({ 'GET /api/status': LIVE, 'GET /api/analytics/ledger': LEDGER, 'GET /api/account/positions': [503, { detail: 'kalshi_unavailable' }] });
    renderApp('/performance');
    await screen.findByTestId('perf-table');
    await waitFor(() => expect(cells('perf-total').slice(2)).toEqual(['—', '—']));
  });

  it('a bot position with no known strategy leaves the strategy rows unknown but counts in the total', async () => {
    stubApi({ 'GET /api/status': LIVE, 'GET /api/analytics/ledger': LEDGER, 'GET /api/account/positions': [200, [pos({ strategy: null })]] });
    renderApp('/performance');
    await waitFor(() => expect(cells('perf-total').slice(2)).toEqual(['$5.00', '$4.00']));
    expect(cells('perf-row-maker').slice(2)).toEqual(['—', '—']);
  });
});

describe('A21: one spelling and one name', () => {
  it('no visible "Realised" or "Profit per $1 Spent" is left', () => {
    const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((d) => (d.isDirectory() ? walk(path.join(dir, d.name)) : [path.join(dir, d.name)]));
    const files = ['components', 'pages', 'utils'].flatMap((d) => walk(path.join(SRC, d)));
    for (const f of files) {
      const text = fs.readFileSync(f, 'utf8');
      expect(/\b(Realised|Unrealised)\b|not realised|Profit per \$1/.test(text), f).toBe(false);
    }
  });
  it('the stopped line reads plainly', () => {
    const now = new Date(2026, 5, 10, 22, 0).getTime();
    const at = (d, h) => new Date(2026, 5, d, h, 5).toISOString();
    expect(stoppedLine({ stoppedAt: at(10, 21), cancelled: 1 }, now)).toMatch(/^Stopped at 9:05 PM, 1 order cancelled$/);
    expect(stoppedLine({ stoppedAt: at(9, 21), cancelled: 0 }, now)).toMatch(/^Stopped yesterday 9:05 PM, 0 orders cancelled$/);
    expect(stoppedLine({ stoppedAt: at(1, 9) }, now)).toMatch(/^Stopped Jun 1, 9:05 AM$/);
    expect(stoppedLine({}, now)).toBe('Stopped');
  });
});

describe('A22 and A23: arbitrage controls and demo leftovers', () => {
  const ARB = {
    settings: { enabled: true, live: false, arb_scan_cap: 10, arb_day_cap: 30, arb_series: ['KXA'], arb_demo_execute: false },
    status: { state: 'log-only', live_allowed: true },
  };
  it('uses the Maker checkbox style, groups the demo-only setting and does not tie the caps to demo', async () => {
    stubApi({ 'GET /api/status': LIVE, 'GET /api/arb': ARB });
    renderApp('/strategies/arb/controls');
    const demo = await screen.findByTestId('demo-only-settings');
    expect(within(demo).getByRole('checkbox', { name: /demo execute/i }).closest('label')).toHaveClass('ux-check');
    expect(screen.getByRole('checkbox', { name: 'KXA' }).closest('label')).toHaveClass('ux-check');
    expect(demo).toHaveTextContent('No effect while the app is in live mode.');
    const text = document.querySelector('main').textContent;
    expect(text).toContain('Most money one scan may put into arbitrage sets.');
    expect(text).not.toMatch(/in demo-execute/);
  });

  it('the demo queue note is left out in live', () => {
    const qs = { episodes: 10, fills: 60, by_queue: [] };
    const first = render(<QueuePosition qs={qs} live />);
    expect(screen.queryByText(DEMO_NOTE)).toBeNull();
    first.unmount();
    render(<QueuePosition qs={qs} />);
    expect(screen.getByText(DEMO_NOTE)).toBeInTheDocument();
  });
});

describe('A24: contrast and tap targets (styles)', () => {
  it('dims empty verdicts by colour, not opacity, and gives related links a 40 px target', () => {
    const analytics = read('styles/analytics.css');
    expect(analytics).not.toMatch(/verdict-counts li\.muted \{[^}]*opacity/);
    expect(read('styles/help.css')).toMatch(/\.term-related a \{[^}]*min-height: var\(--tap\)/);
  });
});

describe('A Stop that came first is said in plain words', () => {
  it('on a refused switch to live', async () => {
    const user = userEvent.setup();
    stubApi({
      'GET /api/status': { ...LIVE, mode: 'demo', desired: 'stopped', live_allowed: true },
      'PUT /api/mode': [409, { detail: 'stopped_during_start' }],
      'POST /api/mode': [409, { detail: 'stopped_during_start' }],
    });
    renderApp('/mode');
    await user.click(await screen.findByRole('button', { name: /switch to live/i }));
    await user.type(await screen.findByRole('textbox'), 'LIVE');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Go LIVE' }));
    expect(await screen.findByText(/Stop won\. Nothing was turned on\./)).toBeInTheDocument();
    expect(screen.queryByText('Live trading is not allowed by the server')).toBeNull();
  });
});

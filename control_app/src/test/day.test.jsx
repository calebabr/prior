import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import ReturnsCard from '../components/analytics/ReturnsCard.jsx';
import { clearDayMemo } from '../hooks/useDay.js';
import { clearReturnsMemo } from '../hooks/useReturns.js';
import { setToken } from '../services/api.js';
import { mockReturns } from '../services/mockAnalytics.js';
import { mockDay } from '../services/mockDay.js';
import { dayPath, groupByEvent, todayString, verdict } from '../utils/day.js';
import { titleForPath, parentForPath } from '../utils/routes.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

const qs = (call) => new URL(call.path, 'http://x').searchParams;
const dayFromMock = (call) => mockDay(qs(call), { mode: 'demo' });
const today = () => todayString();
const emptyBody = (date) => ({
  date, tz_used: 'UTC', env: 'demo', generated_utc: '2026-11-06T12:00:00Z', strategy: null,
  summary: { filled: { count: 0, cost: 0 }, settled: { count: 0, wins: 0, losses: 0 }, realized: 0, settles_later: 0 },
  filled: [], settled: [],
  analytics: [{ key: 'fill_rate', label: 'Fill rate', today: null, avg7: null }], notes: [],
});
const BASE = { 'GET /api/status': STATUS, 'GET /api/history': { snapshots: [] }, 'GET /api/strategies': [] };

beforeEach(() => {
  setToken('tok');
  clearDayMemo();
  clearReturnsMemo();
});
afterEach(() => vi.unstubAllGlobals());

describe('Day helpers and routes', () => {
  it('titles the parametrised path and parents it to Performance', () => {
    expect(titleForPath('/performance/day/2026-11-06')).toBe('Day');
    expect(parentForPath('/performance/day/2026-11-06')).toBe('/performance');
    expect(titleForPath('/performance/day/nope')).toBeNull();
    expect(dayPath('2026-11-06')).toBe('/performance/day/2026-11-06');
  });
  it('groups rows by market event and writes the one-line verdict', () => {
    const g = groupByEvent([{ ticker: 'KXA-26X-B1' }, { ticker: 'KXA-26Y-B1' }, { ticker: 'KXA-26X-B2' }]);
    expect(g.map((x) => [x.event, x.rows.length])).toEqual([['KXA-26X', 2], ['KXA-26Y', 1]]);
    const v = verdict({ filled: { count: 6 }, settled: { count: 4 }, realized: 1.2, settles_later: 2 }, (x) => `+$${x.toFixed(2)}`);
    expect(v).toBe('6 fills, 4 settled, +$1.20, 2 settle later');
  });
});

describe('Day page', () => {
  it('shows the summary, status words, settlement time of open fills and the analytics rows', async () => {
    stubApi({ ...BASE, 'GET /api/analytics/day': dayFromMock });
    renderApp(dayPath(today()));
    expect(await screen.findByTestId('day-filled')).toBeInTheDocument();
    expect(screen.getByTestId('day-date')).toHaveTextContent('Today');
    expect(screen.getByTestId('day-verdict').textContent).toMatch(/\d+ fills?, \d+ settled, [+−-]\$/);
    expect(screen.getAllByText(/^Open$/).length).toBeGreaterThan(0); // status is a word, not only a colour
    expect(screen.getAllByText(/Settles by/).length).toBeGreaterThan(0);
    expect(screen.getByTestId('day-analytics')).toHaveTextContent('Fill rate');
    expect(screen.getByTestId('day-analytics')).toHaveTextContent('7-day avg');
  });

  it('empty day says "No fills yet today."', async () => {
    stubApi({ ...BASE, 'GET /api/analytics/day': (c) => emptyBody(qs(c).get('date')) });
    renderApp(dayPath(today()));
    expect(await screen.findByText('No fills yet today.')).toBeInTheDocument();
  });

  it('error shows a Retry that refetches; chips send the strategy', async () => {
    let fail = true;
    const calls = stubApi({ ...BASE, 'GET /api/analytics/day': (c) => (fail ? [500, { detail: 'boom' }] : dayFromMock(c)) });
    renderApp(dayPath(today()));
    const retry = await screen.findByRole('button', { name: 'Retry' }, { timeout: 8000 });
    fail = false;
    await userEvent.click(retry);
    expect(await screen.findByTestId('day-filled')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Arbitrage' }));
    await waitFor(() => expect(calls.some((c) => c.key === 'GET /api/analytics/day' && qs(c).get('strategy') === 'arb')).toBe(true));
  });

  it('a future or malformed date goes to today', async () => {
    const calls = stubApi({ ...BASE, 'GET /api/analytics/day': dayFromMock });
    renderApp('/performance/day/2999-01-01');
    await waitFor(() => expect(calls.some((c) => c.key === 'GET /api/analytics/day' && qs(c).get('date') === today())).toBe(true));
  });
});

describe('Entry points', () => {
  it('Home shows today card with the three numbers and links to the page', async () => {
    stubApi({ ...BASE, 'GET /api/analytics/day': dayFromMock });
    renderApp('/');
    const link = await screen.findByTestId('home-day-link');
    expect(link).toHaveAttribute('href', dayPath(today()));
    expect(link).toHaveTextContent('Filled');
    expect(link).toHaveTextContent('Settled');
    expect(link).toHaveTextContent('Realized');
  });

  it('Performance has a Today row that links to the day page', async () => {
    stubApi({ ...BASE, 'GET /api/analytics/ledger': { strategies: [], reconciliation: {} } });
    renderApp('/performance');
    const row = await screen.findByRole('link', { name: /Today/ });
    expect(row).toHaveAttribute('href', dayPath(today()));
  });

  it('the daily Returns readout opens that bar\'s day', async () => {
    const NOW = Date.parse('2026-10-31T21:00:00Z');
    stubApi({ 'GET /api/analytics/returns': (c) => mockReturns(qs(c), { mode: 'demo', nowMs: NOW }) });
    render(
      <MemoryRouter>
        <ReturnsCard days={30} env={undefined} onUseDays={() => {}} />
      </MemoryRouter>,
    );
    const open = await screen.findByTestId('returns-open-day');
    expect(open.getAttribute('href')).toMatch(/^\/performance\/day\/\d{4}-\d{2}-\d{2}$/);
  });

  it('tapping the selected bar again navigates to that day', async () => {
    const NOW = Date.parse('2026-10-31T21:00:00Z');
    stubApi({ 'GET /api/analytics/returns': (c) => mockReturns(qs(c), { mode: 'demo', nowMs: NOW }) });
    function Where() {
      return <p data-testid="where">{useLocation().pathname}</p>;
    }
    render(
      <MemoryRouter>
        <ReturnsCard days={30} env={undefined} onUseDays={() => {}} />
        <Where />
      </MemoryRouter>,
    );
    const bars = await screen.findAllByTestId('returns-bar');
    await userEvent.click(bars[bars.length - 1]); // the newest is already selected: this opens its day
    await waitFor(() => expect(screen.getByTestId('where').textContent).toMatch(/^\/performance\/day\/\d{4}-\d{2}-\d{2}$/));
  });
});

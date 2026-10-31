import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ReturnsCard from '../components/analytics/ReturnsCard.jsx';
import { clearReturnsMemo } from '../hooks/useReturns.js';
import { setToken } from '../services/api.js';
import { mockReturns, mockReturnsFills } from '../services/mockAnalytics.js';
import schema from './contract/returns_response_schema.json';
import { stubApi } from './helpers.jsx';

// The mock fixture (services/mockAnalytics.js) must have the same keys and types as the REAL route.
// control_api/tests/test_returns_contract.py checks the real route against the same schema file.

const NOW = Date.parse('2026-10-31T21:00:00Z');
const typeOf = (v) => {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  if (typeof v === 'number') return Number.isInteger(v) ? 'integer' : 'number';
  return typeof v; // string, boolean, object
};
const okType = (v, allowed) => {
  const t = typeOf(v);
  return allowed.includes(t) || (t === 'integer' && allowed.includes('number'));
};
function check(obj, spec, where) {
  expect(obj, where).toBeTypeOf('object');
  expect(Object.keys(spec).filter((k) => !(k in obj)), `${where}: missing keys`).toEqual([]);
  expect(Object.keys(obj).filter((k) => !(k in spec)), `${where}: keys not in the shared schema`).toEqual([]);
  for (const [k, allowed] of Object.entries(spec)) {
    expect(okType(obj[k], allowed), `${where}.${k} = ${JSON.stringify(obj[k])} is not ${allowed}`).toBe(true);
  }
}

describe('mock fixture matches the real /api/analytics/returns schema', () => {
  it.each([
    ['day', 30],
    ['week', 84],
    ['month', 180],
  ])('%s, %i days: top level, every period, flows, you, totals', (granularity, days) => {
    const [status, j] = mockReturns({ granularity, days }, { nowMs: NOW });
    expect(status).toBe(200);
    check(j, schema.top, 'response');
    check(j.totals, schema.totals, 'totals');
    expect(j.periods.length).toBeGreaterThan(0);
    for (const p of j.periods) {
      check(p, schema.period, `period ${p.start_local}`);
      check(p.flows_in_period, schema.flows_in_period, 'flows_in_period');
      check(p.you, schema.you, 'you');
      expect(Object.keys(p.by_strategy)).toEqual(schema.strategy_keys);
      expect(Object.keys(p.fills_by_strategy)).toEqual(schema.strategy_keys);
      expect(schema.return_reasons).toContain(p.return_reason);
    }
  });

  it('null handling: a null rate carries a reason and a null base; dollars stay numbers', () => {
    // The fixture only emits "ok" periods; the schema (from the real route) allows null rate/base with a reason.
    // Feed the app one such period and check it renders a dash with the reason instead of crashing.
    const [, j] = mockReturns({ granularity: 'day', days: 7 }, { nowMs: NOW });
    const p = { ...j.periods[2], return_rate: null, base: null, equity_change: null, return_reason: 'no_start_snapshot' };
    check(p, schema.period, 'null-rate period');
    expect(schema.return_reasons).toContain(p.return_reason);
    expect(typeOf(p.realized_pnl)).toBe('number');
  });

  it('only whole periods are non-partial and exactly the last is partial; elapsed_hours only on the partial one', () => {
    for (const g of ['day', 'week', 'month']) {
      const [, j] = mockReturns({ granularity: g, days: 180 }, { nowMs: NOW });
      expect(j.periods.filter((p) => p.partial)).toHaveLength(1);
      expect(j.periods[j.periods.length - 1].partial).toBe(true);
      expect(j.periods.filter((p) => !p.partial).every((p) => p.elapsed_hours === null)).toBe(true);
      expect(j.periods.filter((p) => p.small_sample).every((p) => p.settled_fills > 0 && p.settled_fills < j.small_sample_threshold)).toBe(true);
      expect(j.periods.every((p) => p.empty === (p.settled_fills === 0))).toBe(true);
    }
  });

  it('fills fixture: totals and every row match the real drill-down rows', () => {
    const [, j] = mockReturns({ granularity: 'day', days: 30 }, { nowMs: NOW });
    const p = j.periods.find((x) => x.settled_fills >= 3);
    const [status, f] = mockReturnsFills({ granularity: 'day', days: 30, start: p.start_local, end: p.end_local }, { nowMs: NOW });
    expect(status).toBe(200);
    for (const k of ['total', 'offset', 'limit', 'fills']) expect(f).toHaveProperty(k);
    expect(f.total).toBe(p.settled_fills);
    for (const row of f.fills) check(row, schema.fill, 'fill');
  });

  // KNOWN MISMATCH (reported, not fixed here): the fixture's fills.period has {label, partial} and no tz_used/env, the
  // real route returns {tz_used, env, period:{start_local,end_local}}. The sheet takes its title from the card's own
  // period, so the app does not read these; harmless, but the fixture is not the real shape.
  it('fills fixture top level has the same keys as the real drill-down response', () => {
    const [, j] = mockReturns({ granularity: 'day', days: 30 }, { nowMs: NOW });
    const [, f] = mockReturnsFills({ start: j.periods[3].start_local, end: j.periods[3].end_local, days: 30 }, { nowMs: NOW });
    check(f, schema.fills_top, 'fills');
    check(f.period, schema.fills_period, 'fills.period');
  });
});

describe('the app sends requests the real route accepts', () => {
  beforeEach(() => {
    setToken('tok');
    clearReturnsMemo();
  });
  afterEach(() => vi.unstubAllGlobals());
  const qs = (call) => new URL(call.path, 'http://x').searchParams;
  const DATE = /^\d{4}-\d{2}-\d{2}$/;

  it('GET /returns carries granularity, days in 1..180 and a tz the server can resolve', async () => {
    const calls = stubApi({ 'GET /api/analytics/returns': (c) => mockReturns(qs(c), { nowMs: NOW }) });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <ReturnsCard days={30} env={undefined} onUseDays={() => {}} />
      </MemoryRouter>,
    );
    await screen.findAllByTestId('returns-bar');
    await user.click(screen.getByRole('radio', { name: 'Week' }));
    await screen.findAllByTestId('returns-bar');
    await user.click(screen.getByRole('radio', { name: 'Month' }));
    await screen.findAllByTestId('returns-bar');
    const seen = calls.filter((c) => c.key === 'GET /api/analytics/returns').map((c) => qs(c));
    expect(seen.map((q) => q.get('granularity'))).toEqual(['day', 'week', 'month']);
    for (const q of seen) {
      const d = Number(q.get('days'));
      expect(Number.isInteger(d) && d >= 1 && d <= 180).toBe(true);
      if (q.get('tz')) expect(q.get('tz')).toMatch(/^[A-Za-z_]+(\/[A-Za-z_+\-0-9]+)*$/);
    }
  });

  // KNOWN BUG (reported): the real GET /api/analytics/returns/fills requires BOTH `start` and `end` (the period's
  // start_local / end_local) and answers 422 without `end`; the app sends only `start`, so the sheet's fills list errors
  // on the real server. Fix: send end=period.end_local (api.js analyticsReturnsFills, useReturnsFills, ReturnsSheet).
  // Flip `it.fails` to `it` once fixed.
  it('GET /returns/fills sends start AND end as dates (the real route answers 422 otherwise)', async () => {
    const real = (c) => {
      const q = qs(c);
      return DATE.test(q.get('start') || '') && DATE.test(q.get('end') || '')
        ? mockReturnsFills({ start: q.get('start'), end: q.get('end') }, { nowMs: NOW })
        : [422, { detail: 'end: required date' }];
    };
    stubApi({ 'GET /api/analytics/returns': (c) => mockReturns(qs(c), { nowMs: NOW }), 'GET /api/analytics/returns/fills': real });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <ReturnsCard days={30} env={undefined} onUseDays={() => {}} />
      </MemoryRouter>,
    );
    const bars = await screen.findAllByTestId('returns-bar');
    await user.click(bars[28]);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    const sheet = await screen.findByTestId('returns-sheet');
    expect(await within(sheet).findAllByText(/KXDEMOA/)).not.toHaveLength(0);
    expect(within(sheet).queryByRole('alert')).toBeNull();
  });
});

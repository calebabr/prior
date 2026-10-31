import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { setToken } from '../services/api.js';
import useStrategySummary from '../hooks/useStrategySummary.js';
import { stubApi, STATUS } from './helpers.jsx';

const REGISTRY = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)' },
  { id: 'you', source: 'you', name: 'You', color: 'var(--warn)' },
];
const Z = { fills: 0, settled_fills: 0, settled_cost: 0, realized_pnl: 0, open_cost: 0, roi_on_settled: null };
const LEDGER = {
  env: 'demo',
  strategies: [
    { ...Z, strategy: 'maker', fills: 12, settled_fills: 10, settled_cost: 40, realized_pnl: 2, open_cost: 8.2, roi_on_settled: 0.05 },
    { ...Z, strategy: 'arb', fills: 2, settled_fills: 2, settled_cost: 10, realized_pnl: 0.5, open_cost: 0 },
    { ...Z, strategy: 'you', fills: 3, realized_pnl: -4, open_cost: 6.6 },
  ],
  settled_by_strategy: [
    { strategy: 'maker', realized_pnl: 2 },
    { strategy: 'arb', realized_pnl: 0.5 },
    { strategy: 'you', realized_pnl: -4 },
  ],
  total: { ...Z, realized_pnl: -1.5 },
};
const POSITIONS = [
  { kind: 'position', value: 8.2, source: 'bot', strategy: 'maker' },
  { kind: 'position', value: 4.8, source: 'bot', strategy: 'maker' },
  { kind: 'order', value: 3.6, source: 'bot', strategy: 'maker' },
  { kind: 'position', value: 6.6, source: 'you', strategy: null },
];
const MARKETS = {
  markets: [
    { series: 'KXDEMOA', enabled: true, verdict: 'keep' },
    { series: 'KXDEMOB', enabled: true, verdict: 'pause' },
    { series: 'KXDEMOC', enabled: false, verdict: null },
  ],
};
const RUNNING = { ...STATUS, desired: 'running', kill_switch: false, mode: 'demo', last_run_at: '2026-10-30T20:00:00Z', arb: { state: 'log-only', last_scan_utc: '2026-10-30T20:01:00Z' } };

const ok = {
  'GET /api/strategies': [200, REGISTRY],
  'GET /api/status': RUNNING,
  'GET /api/analytics/ledger': LEDGER,
  'GET /api/account/positions': [200, POSITIONS],
  'GET /api/markets': MARKETS,
};

beforeEach(() => {
  sessionStorage.clear();
  setToken('tok');
});
afterEach(() => vi.unstubAllGlobals());

const settled = (r) => waitFor(() => expect(r.result.current.loading).toBe(false));
const byId = (r, id) => r.result.current.strategies.find((s) => s.id === id);

describe('useStrategySummary', () => {
  it('composes the hub from the five sources; total is Maker + Arbitrage and excludes manual trades', async () => {
    stubApi(ok);
    const r = renderHook(() => useStrategySummary({ range: '30d' }));
    await settled(r);
    const { strategies, total, manual, partial, errors, capabilities } = r.result.current;
    expect(partial).toBe(false);
    expect(Object.values(errors).every((e) => e === null)).toBe(true);
    expect(strategies.map((s) => [s.id, s.shortName])).toEqual([['maker', 'Maker'], ['arb', 'Arbitrage']]);
    const maker = byId(r, 'maker');
    expect(maker).toMatchObject({ state: 'running', mode: 'demo', pnl: 2, roi: 0.05, openCost: 8.2, fills: 12, exposure: 13, resting: 3.6, drawdown: null });
    expect(maker.series).toEqual({ enabled: 2, total: 3, paused: 1 });
    expect(maker.to).toBe('/strategies/maker');
    expect(byId(r, 'arb')).toMatchObject({ state: 'running', pnl: 0.5, series: null });
    expect(total.pnl).toBe(2.5);
    expect(total.roi).toBeCloseTo(0.05, 4); // 2.5 / 50 settled cost
    expect(total.openCost).toBe(8.2);
    expect(manual).toMatchObject({ pnl: -4, fills: 3, openCost: 6.6, exposure: 6.6 });
    expect(capabilities).toMatchObject({ allRange: false, strategyDrawdown: false });
  });

  it('degrades per source: positions failing leaves exposure null and everything else intact', async () => {
    stubApi({ ...ok, 'GET /api/account/positions': [500, { detail: 'boom' }] });
    const r = renderHook(() => useStrategySummary({}));
    await settled(r);
    expect(r.result.current.partial).toBe(true);
    expect(r.result.current.errors.positions).toBeTruthy();
    expect(r.result.current.errors.ledger).toBeNull();
    expect(byId(r, 'maker')).toMatchObject({ exposure: null, pnl: 2, state: 'running' });
    expect(r.result.current.total.pnl).toBe(2.5);
  });

  it('degrades per source: ledger failing leaves P&L null (not 0) but state and series still show', async () => {
    stubApi({ ...ok, 'GET /api/analytics/ledger': [500, { detail: 'boom' }] });
    const r = renderHook(() => useStrategySummary({}));
    await settled(r);
    expect(r.result.current.errors.ledger).toBeTruthy();
    expect(byId(r, 'maker')).toMatchObject({ pnl: null, roi: null, openCost: null, fills: null, state: 'running', exposure: 13 });
    expect(byId(r, 'maker').series.total).toBe(3);
    expect(r.result.current.total.pnl).toBeNull();
  });

  it('degrades per source: markets failing drops only the series counts; registry failing falls back to Maker and Arbitrage', async () => {
    stubApi({ ...ok, 'GET /api/markets': [500, { detail: 'x' }], 'GET /api/strategies': [500, { detail: 'x' }] });
    const r = renderHook(() => useStrategySummary({}));
    await settled(r);
    expect(byId(r, 'maker').series).toBeNull();
    expect(r.result.current.strategies.map((s) => s.id)).toEqual(['maker', 'arb']);
    expect(byId(r, 'maker').pnl).toBe(2);
  });

  it('status failing (self-fetch) gives an unknown state, other fields intact', async () => {
    stubApi({ ...ok, 'GET /api/status': [500, { detail: 'x' }] });
    const r = renderHook(() => useStrategySummary({}));
    await settled(r);
    expect(byId(r, 'maker')).toMatchObject({ state: 'unknown', stateText: 'Unknown', pnl: 2 });
    expect(r.result.current.errors.status).toBeTruthy();
  });

  it('uses a status passed in and does not fetch /api/status; a stopped bot reads Stopped', async () => {
    const calls = stubApi(ok);
    const r = renderHook(() => useStrategySummary({ status: { ...STATUS, arb: { state: 'stopped' } } }));
    await settled(r);
    expect(calls.some((c) => c.key === 'GET /api/status')).toBe(false);
    expect(byId(r, 'maker')).toMatchObject({ state: 'stopped', stateText: 'Stopped', nextRunAt: null });
    expect(byId(r, 'arb')).toMatchObject({ state: 'stopped', enabled: false });
  });

  it('sends the range as days= or period= and never the unsupported "all"', async () => {
    const calls = stubApi(ok);
    const r = renderHook(({ range }) => useStrategySummary({ range }), { initialProps: { range: '7d' } });
    await settled(r);
    const ledgerPath = () => calls.filter((c) => c.key === 'GET /api/analytics/ledger').map((c) => c.path);
    expect(ledgerPath()[0]).toMatch(/days=7/);
    r.rerender({ range: 'current' });
    await waitFor(() => expect(ledgerPath().length).toBe(2));
    expect(ledgerPath()[1]).toMatch(/period=current/);
    expect(ledgerPath()[1]).not.toMatch(/days=/);
    expect(ledgerPath().join()).not.toMatch(/period=all/);
  });
});

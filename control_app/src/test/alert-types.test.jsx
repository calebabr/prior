import { describe, expect, it } from 'vitest';
import { ALERT_TYPES } from '../pages/Alerts.jsx';

describe('Alerts filter chips', () => {
  it('has a chip for each alert type the server delivers under its own name', () => {
    const ids = ALERT_TYPES.map((t) => t.id);
    expect(ids).toEqual(expect.arrayContaining(['', 'fill', 'settled', 'daily', 'weekly', 'error', 'stop', 'breaker', 'arb_legging']));
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('labels the two new types in plain words', () => {
    const by = Object.fromEntries(ALERT_TYPES.map((t) => [t.id, t.label]));
    expect(by.weekly).toBe('Weekly');
    expect(by.arb_legging).toBe('Legging');
  });
});

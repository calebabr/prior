// Which setting belongs to which strategy page. The Maker Controls view shows the MAKER keys, the Arbitrage
// Controls view shows the arbitrage fields (served by /api/arb), and Settings always shows the full form.
// A key that is not listed here is GLOBAL: it appears only in Settings, so a new backend key can never
// silently vanish from the app. No setting is removed anywhere.

export const MAKER_KEYS = ['run_budget', 'max_order', 'max_open_frac', 'series', 'record_decisions'];

/** 'maker' | 'arb' | 'global' for a /api/config key. */
export function configScope(key) {
  const k = String(key || '');
  if (MAKER_KEYS.includes(k)) return 'maker';
  if (k.startsWith('arb_')) return 'arb';
  return 'global';
}

export const isMakerKey = (key) => configScope(key) === 'maker';
export const isGlobalKey = (key) => configScope(key) === 'global';

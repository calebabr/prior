// Design tokens and per-device display preferences (theme, text size).
//
// Three dark palettes. Meanings are the same in every one: red = Stop / loss, green = gain / on track,
// amber = warning. applyTheme() writes the palette to CSS variables on :root and sets data-theme;
// index.css only references var(--...) (its :root block is the Midnight Slate fallback before JS runs).
// Preferences live in localStorage per device (no 2FA, not a bot setting). Every access is guarded:
// private windows and blocked storage must never break the app.

export const THEME_KEY = 'prior.theme';
export const TEXT_SIZE_KEY = 'prior.textSize';
// The app had another name before: keep reading the old keys so saved preferences survive the rename.
export const LEGACY_KEYS = { 'prior.theme': 'keel.theme', 'prior.textSize': 'keel.textSize', 'prior.lastVisit': 'keel.lastVisit' };

/** Read a preference under its new key, falling back to the pre-rename key when the new one is absent. */
export function readWithLegacy(storage, key) {
  const v = storage.getItem(key);
  if (v !== null && v !== undefined) return v;
  const old = LEGACY_KEYS[key];
  return old ? storage.getItem(old) : null;
}

export const DEFAULT_THEME = 'midnight';
export const DEFAULT_TEXT_SIZE = 'normal';

const overlay = 'rgba(6, 9, 13, 0.72)';

export const THEMES = {
  midnight: {
    label: 'Midnight Slate',
    colors: {
      bg: '#0f141b',
      surface: '#18202a',
      surface2: '#202a36',
      border: '#263241',
      text: '#e6edf3',
      muted: '#8b98a8',
      accent: '#4ea1ff',
      accentText: '#06121f',
      ok: '#3fb97a',
      warn: '#e3b341',
      danger: '#f06a6a',
      dangerText: '#1f0707',
      chart2: '#a78bfa',
      off: '#6b7684',
      borderStrong: '#6a7a8d',
      overlay,
    },
  },
  teal: {
    label: 'Deep Teal',
    colors: {
      bg: '#0b1416',
      surface: '#12201f',
      surface2: '#19292a',
      border: '#1f3533',
      text: '#e3f1ee',
      muted: '#86a39d',
      accent: '#2dd4bf',
      accentText: '#04201c',
      ok: '#5ad08a',
      warn: '#f2b84b',
      danger: '#ef6b6b',
      dangerText: '#1f0707',
      chart2: '#f59e8b',
      off: '#66827c',
      borderStrong: '#587670',
      overlay,
    },
  },
  graphite: {
    label: 'Graphite & Amber',
    colors: {
      bg: '#121212',
      surface: '#1c1c1e',
      surface2: '#262628',
      border: '#2c2c2e',
      text: '#ececec',
      muted: '#9a9a9e',
      accent: '#f5a524',
      accentText: '#1f1300',
      ok: '#4cc38a',
      warn: '#ff8a3d',
      danger: '#ff5c5c',
      dangerText: '#1f0707',
      chart2: '#64b5f6',
      off: '#7a7a7e',
      borderStrong: '#757579',
      overlay,
    },
  },
};

export const THEME_IDS = Object.keys(THEMES);
export const THEME_COLOR = THEMES[DEFAULT_THEME].colors.bg;

// Root font-size in px. Everything in index.css is rem-based, so this scales the whole interface.
export const TEXT_SIZES = { small: 14, normal: 16, large: 18 };
export const TEXT_SIZE_IDS = Object.keys(TEXT_SIZES);

// Status badge kinds -> palette key + label. Used by StatusBadge.
export const badgeStyles = {
  live: { color: 'ok', label: 'Live Running' },
  demo: { color: 'warn', label: 'Demo Running' },
  stopped: { color: 'off', label: 'Stopped' },
  error: { color: 'danger', label: 'Error' },
  stale: { color: 'warn', label: 'Not Confirmed' },
  unknown: { color: 'off', label: 'Unknown' },
};

function toVarName(key) {
  return '--' + key.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
}

function local() {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function readPref(key) {
  try {
    const s = local();
    return s ? readWithLegacy(s, key) : null;
  } catch {
    return null;
  }
}

function writePref(key, value) {
  try {
    const s = local();
    if (s) s.setItem(key, value);
  } catch {
    /* blocked or full: the preference just doesn't persist */
  }
}

export function validTheme(id) {
  return Object.prototype.hasOwnProperty.call(THEMES, id) ? id : DEFAULT_THEME;
}

export function validTextSize(size) {
  return Object.prototype.hasOwnProperty.call(TEXT_SIZES, size) ? size : DEFAULT_TEXT_SIZE;
}

export function readTheme() {
  return validTheme(readPref(THEME_KEY));
}

export function readTextSize() {
  return validTextSize(readPref(TEXT_SIZE_KEY));
}

/** Write the palette to :root. `persist` stores the choice for this device. */
export function applyTheme(id = readTheme(), { persist = false } = {}) {
  const theme = validTheme(id);
  if (persist) writePref(THEME_KEY, theme);
  if (typeof document === 'undefined') return theme;
  const root = document.documentElement;
  for (const [k, v] of Object.entries(THEMES[theme].colors)) root.style.setProperty(toVarName(k), v);
  root.dataset.theme = theme;
  root.style.colorScheme = 'dark';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEMES[theme].colors.bg);
  return theme;
}

export function applyTextSize(size = readTextSize(), { persist = false } = {}) {
  const key = validTextSize(size);
  if (persist) writePref(TEXT_SIZE_KEY, key);
  if (typeof document === 'undefined') return key;
  document.documentElement.style.fontSize = `${TEXT_SIZES[key]}px`;
  document.documentElement.dataset.textSize = key;
  return key;
}

// Apply the saved preferences now. Returns an unsubscribe function (kept for the old signature).
export function initTheme() {
  applyTheme();
  applyTextSize();
  return () => {};
}

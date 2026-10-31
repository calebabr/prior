// "A new version is waiting" signal. Two sources feed one tiny store:
//  1. the service worker (vite-plugin-pwa onNeedRefresh), and
//  2. a check of the deployed index.html: its hashed entry script differs from the one this page is running.
// (The server's api_version is a different release stream from this bundle's package version, so it is not used.)
// Informational only: it never blocks data or actions.
const listeners = new Set();
const RELOAD_KEY = 'prior_update_reloaded_at';
const RELOAD_GUARD_MS = 60_000;
let ready = false;
let applySW = null;
let lastCheck = 0;

function recentlyReloaded() {
  try {
    const t = Number(sessionStorage.getItem(RELOAD_KEY) || 0);
    return t > 0 && Date.now() - t < RELOAD_GUARD_MS;
  } catch {
    return false;
  }
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export const getReady = () => ready;
export function markReady() {
  if (ready || recentlyReloaded()) return; // no reload loop if the new bundle is still stale
  ready = true;
  listeners.forEach((fn) => fn());
}
export function resetForTests() {
  ready = false;
  applySW = null;
  lastCheck = 0;
  listeners.clear();
}
export function setApplyServiceWorker(fn) {
  applySW = fn;
}

/** Ask a waiting worker to take over (best effort), then reload once. */
export async function applyUpdate() {
  try {
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    /* storage may be blocked */
  }
  try {
    if (applySW) await Promise.race([applySW(false), new Promise((r) => setTimeout(r, 1500))]);
  } catch {
    /* fall through to reload */
  }
  window.location.reload();
}

const ENTRY_RE = /\/assets\/index-[\w-]+\.js/;

/** Compare the deployed entry script with the one running. Quiet on any failure or in dev (no hashed names). */
export async function checkDeployedBundle(fetchFn = fetch, doc = document) {
  if (ready || Date.now() - lastCheck < 30_000) return;
  lastCheck = Date.now();
  try {
    const running = [...doc.querySelectorAll('script[src]')].map((s) => s.getAttribute('src')).find((s) => ENTRY_RE.test(s || ''));
    if (!running) return;
    const res = await fetchFn(`/index.html?_=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return;
    const deployed = ENTRY_RE.exec(await res.text());
    if (deployed && deployed[0] !== ENTRY_RE.exec(running)[0]) markReady();
  } catch {
    /* offline or blocked: say nothing */
  }
}

/** Called once from main.jsx. */
export async function startUpdateChecks() {
  try {
    const { registerSW } = await import('virtual:pwa-register');
    const updateSW = registerSW({
      immediate: true,
      onNeedRefresh: markReady,
      onRegisteredSW(_url, reg) {
        if (reg) setInterval(() => reg.update().catch(() => {}), 10 * 60_000);
      },
    });
    setApplyServiceWorker(updateSW);
  } catch {
    /* no service worker support */
  }
  const check = () => document.visibilityState === 'visible' && checkDeployedBundle();
  document.addEventListener('visibilitychange', check);
  setTimeout(check, 5000);
}

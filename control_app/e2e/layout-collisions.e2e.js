import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { test, expect } from '@playwright/test';
import { login } from './helpers.js';
import { LEGACY_REDIRECTS, ROUTE_TITLES } from '../src/utils/routes.js';

// Layout-collision audit: every route in the route table (plus legacy redirects and the Analytics Returns
// Day / Week / Month views) at 320, 375, 390 and 430 px, mock backend. For each page it checks the sticky chrome
// (header, page-title bar, back controls, sub-nav, tab bar) and the first screen of content for:
//   overlap  - two visible text-bearing or interactive elements whose boxes intersect (neither inside the other)
//   hscroll  - horizontal page scroll, or an element past the viewport edge (outside its own scroller)
//   clipped  - text cut off by overflow with no ellipsis
//   small    - interactive element under 40 px in either direction
//   cover    - a fixed / sticky floating element (status widget, toast, banner) over content text
//   stop     - the header Stop button not the top element at its own points; with a dialog open, the Stop button above
//              the card missing, covered, small, under the status bar or overlapping the card
// Findings are collected (not failing on first), written with a screenshot to LAYOUT_SHOTS (default: the OS temp
// dir, never the repo) as findings.json, and the test fails if any remain that are not in KNOWN.
const SHOTS = process.env.LAYOUT_SHOTS || path.join(os.tmpdir(), 'layout-collision-shots');
fs.mkdirSync(SHOTS, { recursive: true });

const WIDTHS = [320, 375, 390, 430];
const THEMES = [
  { id: 'midnight', widths: WIDTHS },
  { id: 'graphite', widths: WIDTHS },
  { id: 'teal', widths: [375] },
];

// Interactive targets that are knowingly under 40 px (shared glyph with a padded hit area, bars with their own
// hit columns). Keep this list short and explained; anything else under 40 px is a finding.
const SMALL_OK = ['help-icon', 'rt-hit'];
// Findings accepted as not fixed in this pass, as `${kind}|${route}|${selector prefix}` prefixes. Empty on purpose.
const KNOWN = [];

function localDate(offsetDays) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const routes = [
  ...ROUTE_TITLES.map((r) => ({ path: r.path, label: r.path })),
  ...LEGACY_REDIRECTS.map((r) => ({ path: r.from, label: `${r.from} (legacy)` })),
  { path: '/analytics', label: '/analytics#returns-day', returns: 'Day' },
  { path: '/analytics', label: '/analytics#returns-week', returns: 'Week' },
  { path: '/analytics', label: '/analytics#returns-month', returns: 'Month' },
  { path: '/performance/analysis', label: '/performance/analysis (reserved)' },
  // Day view: today, and a past day (what a bar tap opens). Home carries the Today card (scanned as '/').
  { path: `/performance/day/${localDate(0)}`, label: '/performance/day/:date (today)' },
  { path: `/performance/day/${localDate(-3)}`, label: '/performance/day/:date (past)' },
];

/** Runs in the page. Returns raw findings for the current scroll position. */
function auditInPage({ smallOk, labelTargets = false }) {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const out = [];
  const sel = (el) => {
    const id = el.getAttribute('data-testid');
    const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '')
      .split(/\s+/).filter(Boolean).slice(0, 2).join('.');
    return `${el.tagName.toLowerCase()}${cls ? '.' + cls : ''}${id ? `[data-testid=${id}]` : ''}`;
  };
  const visible = (el) => {
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
      if (cs.clipPath && cs.clipPath !== 'none' && /inset\(50%\)/.test(cs.clipPath)) return false;
    }
    return true;
  };
  const srOnly = (el) => !!el.closest('.sr-only, h1, [hidden]') && getComputedStyle(el).position === 'absolute';
  const inScroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
    }
    return false;
  };
  const area = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 1.5 && h > 3 ? w * h : 0;
  };
  const box = (r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });
  const R = (n) => Math.round(n);
  // A visually hidden native input (custom checkbox / radio) is represented by its label; a normal input by itself.
  const boxOf = (el) => {
    const lab = el.closest('label');
    const hidden = el.matches('input') && (getComputedStyle(el).opacity === '0' || el.getBoundingClientRect().width < 8);
    return (hidden && lab ? lab : el).getBoundingClientRect();
  };

  // --- items: text-bearing leaves (rects of their own text) and interactive elements (their box)
  const items = [];
  const INTERACTIVE = 'button, a[href], select, input:not([type=hidden]), textarea, summary, [role=tab], [role=radio], [role=switch]';
  for (const el of document.body.querySelectorAll('*')) {
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (!visible(el) || srOnly(el)) continue;
    const isInt = el.matches(INTERACTIVE);
    let rects = [];
    const own = Array.from(el.childNodes).filter((n) => n.nodeType === 3 && n.textContent.trim());
    if (own.length) {
      const range = document.createRange();
      for (const n of own) {
        range.selectNodeContents(n);
        rects.push(...Array.from(range.getClientRects()).filter((r) => r.width > 1 && r.height > 1).map(box));
      }
    }
    if (isInt) {
      const r = boxOf(el);
      if (r.width > 1 && r.height > 1) rects = [box(r), ...rects];
    }
    const ecs = getComputedStyle(el);
    if (ecs.overflowX !== 'visible') {
      // text clipped (ellipsis) by its own box: only the visible part can collide
      const eb = el.getBoundingClientRect();
      rects = rects.map((r) => ({ left: Math.max(r.left, eb.left), top: r.top, right: Math.min(r.right, eb.right), bottom: r.bottom })).filter((r) => r.right > r.left);
    }
    rects = rects.filter((r) => r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw);
    if (!rects.length) continue;
    items.push({ el, rects, name: sel(el), text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30) });
  }
  const inTab = (el) => !!el.closest('nav.tabbar, .tabbar');
  // Sticky chrome floats over content that has scrolled under it on purpose; only the unscrolled state is a layout claim.
  const CHROME = 'header.appbar, .page-title, .page-back, .subnav, .tabbar';
  const scrolled = window.scrollY > 8;
  // Bottom save bar (.sticky-actions) and toasts float over the page by design; `cover` reports toasts and widgets.
  const fixedStuff = (el) => !!el.closest('.toast, [role=status].toast, .status-widget, .sticky-actions');
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      const a = items[i];
      const b = items[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      if (a.el.matches('.rt-hit') || b.el.matches('.rt-hit')) continue; // transparent tap columns over the bars, by design
      if (inTab(a.el) !== inTab(b.el)) continue; // tab bar floats over content by design
      if (scrolled && !!a.el.closest(CHROME) !== !!b.el.closest(CHROME)) continue;
      if (fixedStuff(a.el) || fixedStuff(b.el)) continue; // reported as `cover`
      let hit = 0;
      for (const ra of a.rects) for (const rb of b.rects) hit = Math.max(hit, area(ra, rb));
      if (hit) out.push({ kind: 'overlap', selector: `${a.name} "${a.text}" x ${b.name} "${b.text}"`, detail: `${R(hit)} px2` });
    }
  }

  // --- horizontal scroll and elements past the edge
  const sw = document.documentElement.scrollWidth;
  if (sw > vw) out.push({ kind: 'hscroll', selector: 'html', detail: `scrollWidth ${sw} > ${vw}` });
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || srOnly(el) || el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if ((r.right > vw + 0.5 || r.left < -0.5) && !inScroller(el) && visible(el)) {
      out.push({ kind: 'hscroll', selector: sel(el), detail: `${R(r.left)}..${R(r.right)} of ${vw}` });
    }
  }

  // --- text clipped without an ellipsis
  for (const it of items) {
    const el = it.el;
    const cs = getComputedStyle(el);
    if (el.closest('svg, .rt-scroll, .rt-tablewrap')) continue;
    const clips = ['hidden', 'clip'].includes(cs.overflowX);
    if (clips && el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== 'ellipsis' && !el.matches('input, select, textarea')) {
      out.push({ kind: 'clipped', selector: it.name, detail: `"${it.text}" ${el.scrollWidth}>${el.clientWidth}` });
    }
  }

  // --- small targets
  for (const it of items) {
    const el = it.el;
    if (!el.matches(INTERACTIVE)) continue;
    if (smallOk.some((c) => el.classList.contains(c))) continue;
    if (getComputedStyle(el).display === 'inline') continue; // inline link inside a sentence
    // labelTargets: a checkbox inside a <label> is tapped through the label, so the label is what is measured.
    const label = labelTargets && el.matches('input[type=checkbox]') && el.closest('label');
    const r = label ? label.getBoundingClientRect() : boxOf(el);
    if (r.height < 39.5 || r.width < 38) out.push({ kind: 'small', selector: it.name, detail: `"${it.text}" ${R(r.width)}x${R(r.height)}` });
  }

  // --- floating elements (fixed / sticky) other than the chrome, over content text
  const chrome = 'header.appbar, nav.tabbar, .tabbar, .page-title, .page-back, .subnav, .sidebar';
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    if (el.closest(chrome) || el.closest('.sticky-actions') || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > vh) continue;
    for (const it of items) {
      if (el.contains(it.el) || it.el.contains(el) || it.el.closest(chrome)) continue;
      let hit = 0;
      for (const ri of it.rects) hit = Math.max(hit, area(box(r), ri));
      if (hit) out.push({ kind: 'cover', selector: `${sel(el)} over ${it.name} "${it.text}"`, detail: `${R(hit)} px2` });
    }
  }

  // --- the header Stop button is the top element at its own points
  const stop = document.querySelector('header.appbar button[aria-label*="top" i], header.appbar .stop-btn');
  if (stop) {
    const r = stop.getBoundingClientRect();
    const pts = [[0.5, 0.5], [0.15, 0.25], [0.85, 0.25], [0.15, 0.75], [0.85, 0.75]];
    const bad = pts.filter(([fx, fy]) => {
      const t = document.elementFromPoint(r.left + r.width * fx, r.top + r.height * fy);
      return !t || !stop.contains(t);
    });
    if (bad.length) out.push({ kind: 'stop', selector: sel(stop), detail: `${bad.length}/5 points covered` });
  } else {
    out.push({ kind: 'stop', selector: 'header', detail: 'no Stop button found' });
  }
  return out;
}


/** Runs in the page: the open dialog / popover against the (simulated) safe-area insets. */
function auditOverlay({ safeTop, safeBottom }) {
  const out = [];
  const vh = window.innerHeight;
  const R = (n) => Math.round(n);
  const all = document.querySelectorAll('.modal');
  const dlg = all[all.length - 1] || null; // the Stop confirm is stacked above any other dialog
  const pop = document.querySelector('.popover');
  const onTop = (el, x, y) => {
    const t = document.elementFromPoint(x, y);
    return !!t && el.contains(t);
  };
  if (dlg) {
    const r = dlg.getBoundingClientRect();
    if (r.top < safeTop - 0.5) out.push({ kind: 'modal', selector: 'dialog', detail: `card top ${R(r.top)} < safe-top ${safeTop}` });
    if (r.bottom > vh - safeBottom + 0.5) out.push({ kind: 'modal', selector: 'dialog', detail: `card bottom ${R(r.bottom)} > ${vh - safeBottom}` });
    const maxH = vh - safeTop - safeBottom - 24;
    if (r.height > maxH + 1) out.push({ kind: 'modal', selector: 'dialog', detail: `card ${R(r.height)} px taller than ${maxH}` });
    const title = dlg.querySelector('.modal-title');
    if (title) {
      const t = title.getBoundingClientRect();
      if (t.top < safeTop - 0.5 || t.bottom > r.bottom + 0.5) out.push({ kind: 'modal', selector: 'title', detail: `title ${R(t.top)}..${R(t.bottom)} outside card/safe area` });
      if (!onTop(title, t.left + 4, t.top + t.height / 2)) out.push({ kind: 'modal', selector: 'title', detail: 'title covered' });
    }
    const x = dlg.querySelector('.modal-x');
    if (x) {
      const b = x.getBoundingClientRect();
      if (b.width < 39.5 || b.height < 39.5) out.push({ kind: 'modal', selector: 'dismiss', detail: `${R(b.width)}x${R(b.height)}` });
      if (b.top < safeTop - 0.5) out.push({ kind: 'modal', selector: 'dismiss', detail: `top ${R(b.top)} under the status bar` });
      if (!onTop(x, b.left + b.width / 2, b.top + b.height / 2)) out.push({ kind: 'modal', selector: 'dismiss', detail: 'dismiss covered' });
    }
    const sc = dlg.querySelector('.modal-scroll');
    if (sc && getComputedStyle(sc).overflowY !== 'auto') out.push({ kind: 'modal', selector: 'body', detail: 'body does not scroll inside the card' });
    const oy = getComputedStyle(dlg).overflowY;
    if (oy === 'auto' || oy === 'scroll') out.push({ kind: 'modal', selector: 'card', detail: 'card itself scrolls (header would scroll away)' });
    const layer = dlg.closest('.modal-backdrop');
    if (layer && !layer.classList.contains('modal-layer-stop')) {
      const stop = layer.querySelector('.modal-stop button');
      if (!stop) out.push({ kind: 'stop', selector: 'dialog', detail: 'no Stop above the dialog' });
      else {
        const b = stop.getBoundingClientRect();
        if (b.width < 39.5 || b.height < 39.5) out.push({ kind: 'stop', selector: 'dialog Stop', detail: `${R(b.width)}x${R(b.height)}` });
        if (b.top < safeTop - 0.5 || b.right > window.innerWidth + 0.5) out.push({ kind: 'stop', selector: 'dialog Stop', detail: `outside the safe area (top ${R(b.top)})` });
        if (b.bottom > r.top + 0.5) out.push({ kind: 'stop', selector: 'dialog Stop', detail: `overlaps the card (${R(b.bottom)} > ${R(r.top)})` });
        const hidden = [[0.5, 0.5], [0.15, 0.25], [0.85, 0.25], [0.15, 0.75], [0.85, 0.75]].filter(([fx, fy]) => !onTop(stop, b.left + b.width * fx, b.top + b.height * fy));
        if (hidden.length) out.push({ kind: 'stop', selector: 'dialog Stop', detail: `${hidden.length}/5 points covered` });
      }
    }
  }
  if (pop && !dlg) {
    const head = document.querySelector('header.appbar button.stop-btn');
    const b = head && head.getBoundingClientRect();
    if (!b || !onTop(head, b.left + b.width / 2, b.top + b.height / 2)) out.push({ kind: 'stop', selector: 'popover', detail: 'header Stop covered by the popover' });
  }
  if (pop) {
    const r = pop.getBoundingClientRect();
    if (r.top < safeTop - 0.5) out.push({ kind: 'modal', selector: 'popover', detail: `top ${R(r.top)} < safe-top ${safeTop}` });
    if (r.right > window.innerWidth + 0.5 || r.left < -0.5) out.push({ kind: 'modal', selector: 'popover', detail: `${R(r.left)}..${R(r.right)} outside viewport` });
  }
  return { out, found: !!(dlg || pop) };
}

for (const theme of THEMES) {
  for (const width of theme.widths) {
    test(`layout collisions: ${theme.id} at ${width}px`, async ({ page }) => {
      test.setTimeout(300_000);
      await page.setViewportSize({ width, height: 740 });
      await page.addInitScript((t) => {
        try { window.localStorage.setItem('prior.theme', t); } catch { /* storage may be blocked */ }
      }, theme.id);
      await login(page);
      const findings = [];
      const seen = new Set();
      let n = 0;
      const record = async (raw, label) => {
        let shot = null;
        for (const f of raw) {
          const key = `${f.kind}|${label}|${f.selector}`;
          if (seen.has(key)) continue;
          seen.add(key);
          if (!shot) {
            shot = path.join(SHOTS, `${theme.id}-${width}-${String(++n).padStart(2, '0')}-${label.replace(/[^a-z0-9]+/gi, '_')}.png`);
            await page.screenshot({ path: shot });
          }
          findings.push({ ...f, route: label, width, theme: theme.id, screenshot: shot });
        }
      };
      const go = async (p) => {
        // Client-side navigation: a full goto would reset the in-memory mock and the session.
        await page.evaluate((x) => {
          window.history.pushState({}, '', x);
          window.dispatchEvent(new PopStateEvent('popstate'));
        }, p);
        await page.waitForTimeout(450);
      };
      const setSafe = (top, bottom) =>
        page.evaluate(([t, b]) => {
          const s = document.documentElement.style;
          if (t) {
            s.setProperty('--safe-top', `${t}px`);
            s.setProperty('--safe-bottom', `${b}px`);
          } else {
            s.removeProperty('--safe-top');
            s.removeProperty('--safe-bottom');
          }
        }, [top, bottom]);

      for (const route of routes) {
        await go(route.path);
        if (route.returns) {
          const radio = page.getByRole('radio', { name: route.returns, exact: true }).first();
          await radio.scrollIntoViewIfNeeded();
          await radio.click();
          await page.waitForTimeout(250);
        } else {
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.waitForTimeout(150);
        }
        await record(await page.evaluate(auditInPage, { smallOk: SMALL_OK }), route.label);
      }

      // Safe-area simulation: env(safe-area-inset-*) cannot be set in Playwright, so the app reads --safe-top / --safe-bottom
      // (index.css :root, defaulting to env()) and the test overrides them on <html>: 47 px (notch) and 59 px (dynamic island).
      for (const [safeTop, safeBottom] of [[0, 0], [47, 34], [59, 34]]) {
        await setSafe(safeTop, safeBottom);
        const tag = `safe${safeTop}`;
        if (safeTop) {
          // page chrome with an inset: the pages that carry actions in the title row
          for (const p of ['/shadow', '/analytics', '/performance', '/strategies/maker/controls', `/performance/day/${localDate(0)}`]) {
            await go(p);
            await page.evaluate(() => window.scrollTo(0, 0));
            await page.waitForTimeout(200);
            await record(await page.evaluate(auditInPage, { smallOk: SMALL_OK }), `${p}@${tag}`);
          }
        }
        // overlays: confirm dialog (header Stop), returns sheet, help popover, "go live" confirm
        const overlays = [
          ['stop-confirm', async () => {
            await go('/');
            await page.locator('header.appbar').getByRole('button', { name: /stop the bot/i }).click();
          }],
          ['returns-sheet', async () => {
            await go('/analytics');
            const details = page.getByRole('button', { name: 'Details' }).first();
            await details.scrollIntoViewIfNeeded();
            await details.click();
          }],
          ['help-popover', async () => {
            await go('/settings');
            await page.locator('button.help-icon').first().click();
          }],
          ['arming-confirm', async () => {
            await go('/arming');
            await page.getByRole('switch', { name: /arbitrage/i }).click();
          }],
          ['live-confirm', async () => {
            await go('/strategies/arb/controls');
            await page.getByRole('button', { name: /go live/i }).first().click();
          }],
        ];
        for (const [name, open] of overlays) {
          try {
            await open();
          } catch {
            /* the control may not exist in this state; reported below */
          }
          await page.waitForTimeout(400);
          const res = await page.evaluate(auditOverlay, { safeTop, safeBottom });
          if (!res.found) {
            findings.push({ kind: 'modal', route: `${name}@${tag}`, width, theme: theme.id, selector: name, detail: 'overlay did not open', screenshot: '' });
          } else {
            await record(res.out, `${name}@${tag}`);
          }
          await page.keyboard.press('Escape');
          await page.waitForTimeout(150);
        }
      }
      await setSafe(0, 0);
      fs.writeFileSync(path.join(SHOTS, `findings-${theme.id}-${width}.json`), JSON.stringify(findings, null, 2));
      const unknown = findings.filter((f) => !KNOWN.some((k) => `${f.kind}|${f.route}|${f.selector}`.startsWith(k)));
      expect(
        unknown.map((f) => `${f.kind} ${f.route} @${f.width} ${f.selector} (${f.detail}) -> ${f.screenshot}`),
        `layout findings (screenshots in ${SHOTS})`,
      ).toEqual([]);
    });
  }
}

// Circuit-breaker states (mock scenarios via sessionStorage 'keel.mock'): the red banner on Home and the compact one
// on other pages, the amber warning, the unreadable Settings card, and the restart acknowledgement dialog (checkbox),
// at every width and with the status-bar inset. Stop must stay uncovered in each.
const BREAKER_SCENARIOS = [
  { id: 'breaker-tripped', routes: ['/', '/settings', '/more', '/activity', '/strategies/arb/controls'] },
  { id: 'breaker-warning', routes: ['/', '/settings'] },
  { id: 'breaker-unreadable', routes: ['/settings'] },
];

for (const scenario of BREAKER_SCENARIOS) {
  for (const width of WIDTHS) {
    test(`layout collisions: ${scenario.id} at ${width}px`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.setViewportSize({ width, height: 740 });
      await page.addInitScript((s) => {
        try { window.sessionStorage.setItem('keel.mock', s); } catch { /* storage may be blocked */ }
      }, scenario.id);
      await login(page);
      const findings = [];
      const seen = new Set();
      const record = async (raw, label) => {
        let shot = null;
        for (const f of raw) {
          const key = `${f.kind}|${label}|${f.selector}`;
          if (seen.has(key)) continue;
          seen.add(key);
          if (!shot) {
            shot = path.join(SHOTS, `${scenario.id}-${width}-${findings.length}-${label.replace(/[^a-z0-9]+/gi, '_')}.png`);
            await page.screenshot({ path: shot });
          }
          findings.push({ ...f, route: label, width, theme: scenario.id, screenshot: shot });
        }
      };
      const go = async (p) => {
        await page.evaluate((x) => {
          window.history.pushState({}, '', x);
          window.dispatchEvent(new PopStateEvent('popstate'));
        }, p);
        await page.waitForTimeout(450);
      };
      const setSafe = (top, bottom) =>
        page.evaluate(([t, b]) => {
          const s = document.documentElement.style;
          if (t) {
            s.setProperty('--safe-top', `${t}px`);
            s.setProperty('--safe-bottom', `${b}px`);
          } else {
            s.removeProperty('--safe-top');
            s.removeProperty('--safe-bottom');
          }
        }, [top, bottom]);

      for (const [safeTop, safeBottom] of [[0, 0], [47, 34]]) {
        await setSafe(safeTop, safeBottom);
        for (const r of scenario.routes) {
          await go(r);
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.waitForTimeout(150);
          await record(await page.evaluate(auditInPage, { smallOk: SMALL_OK }), `${scenario.id}:${r}@safe${safeTop}`);
        }
        if (scenario.id === 'breaker-tripped') {
          await go('/');
          await page.getByTestId('breaker-restart').click();
          await page.waitForTimeout(400);
          const res = await page.evaluate(auditOverlay, { safeTop, safeBottom });
          if (!res.found) {
            findings.push({ kind: 'modal', route: `breaker-restart-confirm@safe${safeTop}`, width, theme: scenario.id, selector: 'breaker-restart-confirm', detail: 'overlay did not open', screenshot: '' });
          } else {
            await record(res.out, `breaker-restart-confirm@safe${safeTop}`);
          }
          await page.keyboard.press('Escape');
          await page.waitForTimeout(150);
        }
      }
      await setSafe(0, 0);
      fs.writeFileSync(path.join(SHOTS, `findings-${scenario.id}-${width}.json`), JSON.stringify(findings, null, 2));
      const unknown = findings.filter((f) => !KNOWN.some((k) => `${f.kind}|${f.route}|${f.selector}`.startsWith(k)));
      expect(
        unknown.map((f) => `${f.kind} ${f.route} @${f.width} ${f.selector} (${f.detail}) -> ${f.screenshot}`),
        `layout findings (screenshots in ${SHOTS})`,
      ).toEqual([]);
    });
  }
}

// Numbers and trades: the Home account card (live, unavailable, older API, very large figures), the "You" row, the
// balance chart opened, the Account page with a combo row, the arbitrage "set to LIVE while off" warning and dialog,
// and the breaker card's "your own trades left out" rows. The audits above only see the first screen, so these pages are audited at every scroll position.
const NUMBER_SCENARIOS = [
  { id: 'numbers', mock: '', pages: ['/', '/account'] },
  { id: 'account-na', mock: 'account-na', pages: ['/'] },
  { id: 'account-old', mock: 'account-old', pages: ['/'] },
  { id: 'account-big', mock: 'account-big', pages: ['/', '/account'] },
  { id: 'arb-live-off', mock: 'arb-live-off', pages: ['/strategies/arb', '/strategies/arb/controls'] },
  { id: 'breaker-own', mock: '', pages: ['/settings'] },
];

/** What must be on screen, so a scan of a page that failed to render the element cannot pass. */
async function expectNumbers(page, scenario, route) {
  if (route === '/') {
    await expect(page.getByTestId('home-account')).toBeVisible();
    if (scenario === 'account-old') await expect(page.getByTestId('home-account')).toContainText('From the last account snapshot.');
    else await expect(page.getByTestId('acct-total')).toBeVisible();
    await expect(page.getByTestId('manual-open')).toContainText('2 open positions');
    const fold = page.getByRole('button', { name: /Balance, 30 Days/ });
    if ((await fold.getAttribute('aria-expanded')) !== 'true') await fold.click();
    await expect(page.getByTestId('equity-chart')).toBeVisible();
  } else if (route === '/account') {
    await expect(page.getByTestId('pos-totals')).toBeVisible();
    await expect(page.getByText('10-leg combo')).toBeVisible();
  } else if (route === '/settings') {
    await expect(page.getByTestId('breaker-excluded')).toBeVisible();
    await expect(page.getByTestId('breaker-unproven')).toBeVisible();
  } else {
    await expect(page.getByTestId('arb-set-live')).toBeVisible();
  }
}

for (const scenario of NUMBER_SCENARIOS) {
  for (const width of WIDTHS) {
    test(`layout collisions: ${scenario.id} (numbers and trades) at ${width}px`, async ({ page }) => {
      test.setTimeout(180_000);
      await page.setViewportSize({ width, height: 740 });
      await page.addInitScript((s) => {
        try { window.sessionStorage.setItem('keel.mock', s); } catch { /* storage may be blocked */ }
      }, scenario.mock);
      await login(page);
      const findings = [];
      const seen = new Set();
      const record = async (raw, label) => {
        let shot = null;
        for (const f of raw) {
          const key = `${f.kind}|${label}|${f.selector}`;
          if (seen.has(key)) continue;
          seen.add(key);
          if (!shot) {
            shot = path.join(SHOTS, `${scenario.id}-${width}-${findings.length}-${label.replace(/[^a-z0-9]+/gi, '_')}.png`);
            await page.screenshot({ path: shot });
          }
          findings.push({ ...f, route: label, width, theme: scenario.id, screenshot: shot });
        }
      };
      const go = async (p) => {
        await page.evaluate((x) => {
          window.history.pushState({}, '', x);
          window.dispatchEvent(new PopStateEvent('popstate'));
        }, p);
        await page.waitForTimeout(450);
      };
      const setSafe = (top, bottom) =>
        page.evaluate(([t, b]) => {
          const s = document.documentElement.style;
          if (t) {
            s.setProperty('--safe-top', `${t}px`);
            s.setProperty('--safe-bottom', `${b}px`);
          } else {
            s.removeProperty('--safe-top');
            s.removeProperty('--safe-bottom');
          }
        }, [top, bottom]);
      const overlay = async (name, safeTop, safeBottom) => {
        await page.waitForTimeout(400);
        const res = await page.evaluate(auditOverlay, { safeTop, safeBottom });
        if (!res.found) {
          findings.push({ kind: 'modal', route: `${name}@safe${safeTop}`, width, theme: scenario.id, selector: name, detail: 'overlay did not open', screenshot: '' });
        } else {
          await record(res.out, `${name}@safe${safeTop}`);
        }
        await page.keyboard.press('Escape');
        await page.waitForTimeout(150);
      };

      for (const [safeTop, safeBottom] of [[0, 0], [47, 34]]) {
        await setSafe(safeTop, safeBottom);
        for (const route of scenario.pages) {
          await go(route);
          await expectNumbers(page, scenario.id, route);
          const height = await page.evaluate(() => document.documentElement.scrollHeight);
          for (let y = 0; y < height; y += 420) {
            await page.evaluate((to) => window.scrollTo(0, to), y);
            await page.waitForTimeout(120);
            await record(await page.evaluate(auditInPage, { smallOk: SMALL_OK, labelTargets: true }), `${scenario.id}:${route}@safe${safeTop}`);
          }
        }
        if (scenario.id === 'numbers') {
          for (const name of ['Market Value', 'Paid for Positions', 'Unrealized P&L']) {
            await go('/');
            await page.getByTestId('home-account').getByRole('button', { name: `What is ${name}?` }).click();
            await overlay(`help-${name}`, safeTop, safeBottom);
          }
        }
        if (scenario.id === 'breaker-own') {
          await go('/settings');
          await page.getByRole('button', { name: /What is Your Own Trades/i }).click();
          await overlay('help-breaker-own-trades', safeTop, safeBottom);
        }
        if (scenario.id === 'arb-live-off') {
          await go('/strategies/arb/controls');
          await page.getByRole('button', { name: /turn on/i }).click();
          await expect(page.getByRole('dialog')).toContainText('trades real money');
          await overlay('arb-turn-on-live', safeTop, safeBottom);
        }
      }
      await setSafe(0, 0);
      fs.writeFileSync(path.join(SHOTS, `findings-${scenario.id}-${width}.json`), JSON.stringify(findings, null, 2));
      const unknown = findings.filter((f) => !KNOWN.some((k) => `${f.kind}|${f.route}|${f.selector}`.startsWith(k)));
      expect(
        unknown.map((f) => `${f.kind} ${f.route} @${f.width} ${f.selector} (${f.detail}) -> ${f.screenshot}`),
        `layout findings (screenshots in ${SHOTS})`,
      ).toEqual([]);
    });
  }
}

// Design audit A1-A24: the persistent Stop warning (failed and partial), the "Stopped at" line, the LIVE tag, the
// "Not Confirmed" badge, the demo-switch warning, the kill-switch steps, the "too few results" lines, long period
// lists, the demo-only group, and every dialog layering (Stop above a dialog, above a dialog whose request is in
// flight, and the Stop confirm on top). Every page is audited at every scroll position.
const AUDIT_SCENARIOS = [
  { id: 'audit-live', mock: 'live', pages: ['/', '/more', '/activity', '/strategies', '/performance', '/strategies/arb/controls', '/arming', '/analytics?tab=scorecard'] },
  { id: 'audit-stop-fail', mock: 'live,stop-fail', stop: true, pages: ['/', '/settings', '/strategies/arb/controls', '/performance'] },
  { id: 'audit-stop-partial', mock: 'live,stop-partial', stop: true, pages: ['/', '/mode', '/analytics'] },
  { id: 'audit-stop-ok', mock: 'live', stop: true, pages: ['/'] },
  { id: 'audit-stale', mock: 'live,status-drop', refresh: true, pages: ['/', '/strategies', '/strategies/maker'] },
  { id: 'audit-status-down', mock: 'status-down', pages: ['/'] },
  { id: 'audit-stopped', mock: 'live,stopped', pages: ['/', '/arming', '/strategies'] },
  { id: 'audit-few', mock: 'few-results', pages: ['/strategies/maker/results', '/health'] },
  { id: 'audit-periods', mock: 'periods-many', pages: ['/performance/periods'] },
];

async function expectAudit(page, id, route) {
  if (id === 'audit-live' && route === '/') await expect(page.getByTestId('live-tag')).toBeVisible();
  if (id === 'audit-live' && route === '/arming') await expect(page.getByTestId('arm-note')).toContainText('Live is on');
  if (id === 'audit-live' && route === '/strategies/arb/controls') await expect(page.getByTestId('demo-only-settings')).toBeVisible();
  if (id === 'audit-stop-fail') await expect(page.getByTestId('stop-alert')).toContainText('Stop did NOT reach the server');
  if (id === 'audit-stop-partial') await expect(page.getByTestId('stop-alert')).toContainText('not everything was cancelled');
  if (id === 'audit-stop-ok') await expect(page.getByTestId('stopped-line')).toContainText('9 orders cancelled');
  if (id === 'audit-stale') await expect(page.locator('header.appbar .badge-label')).toHaveText('Not Confirmed');
  if (id === 'audit-stale') await expect(page.locator('header.appbar .badge-label')).toBeVisible();
  if (id === 'audit-status-down') await expect(page.getByTestId('state-label')).toHaveText('Status unknown');
  if (id === 'audit-stopped' && route === '/') await expect(page.getByTestId('already-stopped')).toContainText('leftover bot orders');
  if (id === 'audit-stopped' && route === '/arming') await expect(page.getByTestId('kill-switch-help')).toBeVisible();
  if (id === 'audit-few' && route === '/health') await expect(page.getByText('Too few results for a range.').first()).toBeVisible();
  if (id === 'audit-few' && route !== '/health') {
    const card = page.getByRole('button', { name: /^Demo Series A/ });
    if ((await card.getAttribute('aria-expanded')) !== 'true') await card.click();
    await expect(page.getByTestId('range-too-few').first()).toBeVisible();
  }
  if (id === 'audit-periods') await expect(page.getByLabel('Period timeline')).toContainText('Test period 7');
  if (id === 'audit-live' && route === '/performance') {
    await expect(page.getByTestId('perf-table')).toContainText('Value now');
    // A20: every column is on screen, with no sideways scrolling inside the table either.
    await expect.poll(() => page.locator('.perf-scroll').evaluate((el) => el.scrollWidth - el.clientWidth), { message: 'performance table scrolls sideways' }).toBeLessThanOrEqual(1);
  }
}

function collector(page, id, width, findings) {
  const seen = new Set();
  return async (raw, label) => {
    let shot = null;
    for (const f of raw) {
      const key = `${f.kind}|${label}|${f.selector}`;
      if (seen.has(key)) continue;
      seen.add(key);
      if (!shot) {
        shot = path.join(SHOTS, `${id}-${width}-${findings.length}-${label.replace(/[^a-z0-9]+/gi, '_')}.png`);
        await page.screenshot({ path: shot });
      }
      findings.push({ ...f, route: label, width, theme: id, screenshot: shot });
    }
  };
}
const clientGo = async (page, p) => {
  await page.evaluate((x) => {
    window.history.pushState({}, '', x);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, p);
  await page.waitForTimeout(450);
};
const insets = (page, top, bottom) =>
  page.evaluate(([t, b]) => {
    const s = document.documentElement.style;
    if (t) {
      s.setProperty('--safe-top', `${t}px`);
      s.setProperty('--safe-bottom', `${b}px`);
    } else {
      s.removeProperty('--safe-top');
      s.removeProperty('--safe-bottom');
    }
  }, [top, bottom]);
const report = (id, width, findings) => {
  fs.writeFileSync(path.join(SHOTS, `findings-${id}-${width}.json`), JSON.stringify(findings, null, 2));
  expect(
    findings.map((f) => `${f.kind} ${f.route} @${f.width} ${f.selector} (${f.detail}) -> ${f.screenshot}`),
    `layout findings (screenshots in ${SHOTS})`,
  ).toEqual([]);
};
// Full-page shots of the audit screens, for looking at (not for assertions).
const look = (page, name) => (process.env.AUDIT_LOOK ? page.screenshot({ path: path.join(SHOTS, `look-${name}.png`) }) : null);

for (const scenario of AUDIT_SCENARIOS) {
  for (const width of WIDTHS) {
    test(`layout collisions: ${scenario.id} at ${width}px`, async ({ page }) => {
      test.setTimeout(240_000);
      await page.setViewportSize({ width, height: 740 });
      await page.addInitScript((s) => {
        try { window.sessionStorage.setItem('keel.mock', s); } catch { /* storage may be blocked */ }
      }, scenario.mock);
      await login(page);
      const findings = [];
      const record = collector(page, scenario.id, width, findings);
      if (scenario.stop) {
        await page.locator('header.appbar').getByRole('button', { name: /stop the bot/i }).click();
        await page.getByRole('button', { name: 'Stop now' }).click();
        await expect(page.locator('.modal')).toHaveCount(0);
        await page.waitForTimeout(600);
        // The success toast floats over the page for a few seconds by design; the lasting screen is what is scanned.
        await expect(page.locator('.toast')).toHaveCount(0, { timeout: 10_000 });
      }
      if (scenario.refresh) {
        await expect(page.getByTestId('live-tag')).toBeVisible();
        await page.evaluate(() => window.dispatchEvent(new Event('km:stopped'))); // forces a second status read, which fails
      }
      for (const [safeTop, safeBottom] of [[0, 0], [47, 34]]) {
        await insets(page, safeTop, safeBottom);
        for (const route of scenario.pages) {
          await clientGo(page, route);
          await expectAudit(page, scenario.id, route);
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.waitForTimeout(150);
          await look(page, `${scenario.id}-${width}-safe${safeTop}-${route.replace(/[^a-z0-9]+/gi, '_')}`);
          const height = await page.evaluate(() => document.documentElement.scrollHeight);
          for (let y = 0; y < height; y += 360) {
            await page.evaluate((to) => window.scrollTo(0, to), y);
            await page.waitForTimeout(120);
            await record(await page.evaluate(auditInPage, { smallOk: SMALL_OK, labelTargets: true }), `${scenario.id}:${route}@safe${safeTop}`);
          }
        }
      }
      await insets(page, 0, 0);
      report(scenario.id, width, findings);
    });
  }
}

for (const width of WIDTHS) {
  test(`layout collisions: Stop above dialogs at ${width}px`, async ({ page }) => {
    test.setTimeout(240_000);
    await page.setViewportSize({ width, height: 740 });
    await page.addInitScript(() => {
      try { window.sessionStorage.setItem('keel.mock', 'live,mode-hang'); } catch { /* storage may be blocked */ }
    });
    await login(page);
    const findings = [];
    const record = collector(page, 'stop-layers', width, findings);
    const overlay = async (name, safeTop, safeBottom) => {
      await page.waitForTimeout(400);
      await look(page, `stop-layers-${width}-safe${safeTop}-${name}`);
      const res = await page.evaluate(auditOverlay, { safeTop, safeBottom });
      expect(res.found, `${name} did not open`).toBe(true);
      await record(res.out, `${name}@safe${safeTop}`);
    };
    const dialogStop = page.getByTestId('modal-stop').getByRole('button', { name: /stop the bot/i });
    // The visual viewport also shrinks when the keyboard is open: 420 px stands in for that.
    for (const [safeTop, safeBottom, height] of [[0, 0, 740], [47, 34, 740], [59, 34, 740], [47, 0, 420]]) {
      await page.setViewportSize({ width, height });
      await insets(page, safeTop, safeBottom);
      const tag = height === 420 ? '-short' : '';
      await clientGo(page, '/mode');
      await page.getByRole('button', { name: /switch to demo/i }).click();
      await expect(page.getByTestId('to-demo-warning')).toBeVisible();
      await overlay(`to-demo${tag}`, safeTop, safeBottom);
      await dialogStop.click(); // a real tap: fails if anything covers it
      await expect(page.getByRole('dialog', { name: 'Stop all trading?' })).toBeVisible();
      await overlay(`stop-over-dialog${tag}`, safeTop, safeBottom);
      await page.getByRole('button', { name: 'Cancel' }).last().click();
      await expect(page.getByRole('dialog', { name: 'Switch to DEMO?' })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.locator('.modal')).toHaveCount(0);
    }
    await page.setViewportSize({ width, height: 740 });
    await insets(page, 47, 34);
    await clientGo(page, '/analytics');
    const details = page.getByRole('button', { name: 'Details' }).first();
    await details.scrollIntoViewIfNeeded();
    await details.click();
    await overlay('returns-sheet', 47, 34);
    await page.keyboard.press('Escape');
    await clientGo(page, '/settings');
    await page.locator('button.help-icon').first().click();
    await overlay('help-popover', 47, 34);
    await page.keyboard.press('Escape');
    // Last: a mode switch that never answers. The dialog cannot be closed, Stop still works.
    await clientGo(page, '/mode');
    await page.getByRole('button', { name: /switch to demo/i }).click();
    await page.getByLabel('6-digit code').fill('482913');
    await page.getByRole('button', { name: 'Switch to demo' }).click();
    await expect(page.getByRole('dialog').getByRole('button', { name: 'Working…' })).toBeVisible();
    await overlay('busy-2fa', 47, 34);
    await dialogStop.click();
    await overlay('stop-over-busy', 47, 34);
    await page.getByRole('button', { name: 'Stop now' }).click();
    await expect(page.getByText('This request had already left')).toBeVisible();
    await overlay('busy-after-stop', 47, 34);
    await insets(page, 0, 0);
    report('stop-layers', width, findings);
  });
}

// A24: the Terminology section links and "See also" links are 40 px targets all the way down the page.
for (const width of WIDTHS) {
  test(`layout collisions: terminology link targets at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 740 });
    await login(page);
    await clientGo(page, '/terminology');
    const links = page.locator('.term-jump a, .term-related a');
    await expect(links.first()).toBeVisible();
    const small = await links.evaluateAll((els) =>
      els.map((e) => ({ text: e.textContent, w: e.getBoundingClientRect().width, h: e.getBoundingClientRect().height })).filter((r) => r.h < 39.5 || r.w < 38),
    );
    expect(small).toEqual([]);
    const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(sideways).toBeLessThanOrEqual(0);
  });
}

// The signed-out page (its footer lost the demo / live wording). It has no header, so there is no Stop to check.
for (const width of WIDTHS) {
  test(`layout collisions: login at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 740 });
    await page.goto('/');
    await expect(page.getByText('Protected by 2FA')).toBeVisible();
    const found = (await page.evaluate(auditInPage, { smallOk: SMALL_OK, labelTargets: true })).filter(
      // The reveal-password eye sits inside the password field on purpose.
      (f) => f.kind !== 'stop' && !(f.kind === 'overlap' && f.selector.includes('button.pw-eye')),
    );
    expect(found.map((f) => `${f.kind} ${f.selector} (${f.detail})`)).toEqual([]);
    // With a Stop warning left over (a Stop refused because the session had expired).
    await page.evaluate(() => {
      const result = { kind: 'failed', title: 'Stop was refused: sign in again', text: 'Session expired. Please sign in again. Tap Stop again.', sentAt: 1, resolvedAt: 1 };
      window.localStorage.setItem('prior.stop', JSON.stringify({ result, pending: [] }));
    });
    await page.reload();
    await expect(page.getByTestId('stop-alert')).toBeVisible();
    await page.evaluate(() => document.documentElement.style.setProperty('--safe-top', '47px'));
    const again = (await page.evaluate(auditInPage, { smallOk: SMALL_OK, labelTargets: true })).filter(
      (f) => f.kind !== 'stop' && !(f.kind === 'overlap' && f.selector.includes('button.pw-eye')),
    );
    expect(again.map((f) => `${f.kind} ${f.selector} (${f.detail})`)).toEqual([]);
  });
}

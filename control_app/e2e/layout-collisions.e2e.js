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
//   stop     - the header Stop button not the top element at its own points
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

const routes = [
  ...ROUTE_TITLES.map((r) => ({ path: r.path, label: r.path })),
  ...LEGACY_REDIRECTS.map((r) => ({ path: r.from, label: `${r.from} (legacy)` })),
  { path: '/analytics', label: '/analytics#returns-day', returns: 'Day' },
  { path: '/analytics', label: '/analytics#returns-week', returns: 'Week' },
  { path: '/analytics', label: '/analytics#returns-month', returns: 'Month' },
  { path: '/performance/analysis', label: '/performance/analysis (reserved)' },
];

/** Runs in the page. Returns raw findings for the current scroll position. */
function auditInPage({ smallOk }) {
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
    const r = boxOf(el);
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
  const dlg = document.querySelector('.modal');
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
          for (const p of ['/shadow', '/analytics', '/performance', '/strategies/maker/controls']) {
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

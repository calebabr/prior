import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import useScrollIdle from '../hooks/useScrollIdle.js';
import BackLink from './BackLink.jsx';
import { useNavTrailRecorder } from '../hooks/useNavTrail.js';
import { documentTitle, parentForPath, titleForPath } from '../utils/routes.js';
import { iconForPath } from './navIcons.jsx';

const REDUCE_QUERY = '(prefers-reduced-motion: reduce)';

function usePrefersReducedMotion() {
  const get = () => typeof window !== 'undefined' && !!window.matchMedia && !!window.matchMedia(REDUCE_QUERY).matches;
  const [reduce, setReduce] = useState(get);
  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const mq = window.matchMedia(REDUCE_QUERY);
    const on = () => setReduce(!!mq.matches);
    if (mq.addEventListener) mq.addEventListener('change', on);
    else if (mq.addListener) mq.addListener(on);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', on);
      else if (mq.removeListener) mq.removeListener(on);
    };
  }, []);
  return reduce;
}

/**
 * Title bar directly under the app header on every signed-in page.
 * - Sticky (top: --appbar-h), in normal flow at the top of <main>: at the top of the page it fills its own
 *   reserved band; once scrolled it pins under the header and overlays content (no layout shift either way).
 * - Always shown at the top. Scrolled down: shown while scrolling, fades out after ~2.5 s idle, back on any scroll.
 *   Hidden = opacity 0 + visibility hidden + no pointer events, so it never blocks taps on the content beneath.
 * - aria-hidden: each page carries its own <h1> (visually hidden by index.css, see "page title" there), so
 *   screen readers get the heading once and never on reappearance.
 * - Publishes --appbar-h and --sticky-top (header + this bar while shown) on :root for other sticky elements.
 * Themed: accent bar + the page's nav icon + bold title; once stuck it is a slightly raised, accent-tinted band
 * with a soft bottom edge so content visibly scrolls under it.
 * Never covers the header (z-index below it) or the bottom tab bar (it sits at the top).
 */
export default function PageTitle() {
  const { pathname } = useLocation();
  const title = titleForPath(pathname);
  useNavTrailRecorder();
  const parent = parentForPath(pathname);
  const back = parent ? { label: titleForPath(parent) } : null;
  const Icon = iconForPath(pathname);
  const { atTop, idle } = useScrollIdle({ resetKey: pathname });
  const reduce = usePrefersReducedMotion();
  const shown = atTop || !idle;
  const ref = useRef(null);

  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);
  useEffect(() => () => {
    document.title = documentTitle(null);
  }, []);

  // Sticky-stack height for the bar's own `top` and for other sticky elements.
  useLayoutEffect(() => {
    const root = document.documentElement;
    const appbar = document.querySelector('header.appbar');
    const measure = () => {
      const headerH = appbar ? appbar.offsetHeight : 0;
      const barH = ref.current ? ref.current.offsetHeight : 0;
      if (headerH) root.style.setProperty('--appbar-h', `${headerH}px`);
      root.style.setProperty('--sticky-top', `${headerH + (shown ? barH : 0)}px`);
    };
    measure();
    let ro = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(measure);
      if (appbar) ro.observe(appbar);
      if (ref.current) ro.observe(ref.current);
    }
    window.addEventListener('resize', measure);
    return () => {
      if (ro) ro.disconnect();
      window.removeEventListener('resize', measure);
    };
    // title: the bar mounts/unmounts with it (ref.current changes), so re-measure and re-observe.
  }, [shown, title]);
  useEffect(() => () => {
    document.documentElement.style.removeProperty('--sticky-top');
    document.documentElement.style.removeProperty('--appbar-h');
  }, []);

  // Keep the title and the page head's actions (Refresh, range buttons) from drawing on top of each other: reserve the
  // actions' width on the right of the bar; when title + actions do not fit on one row, stack the actions below the bar.
  // Measures real widths (title text scrollWidth is its natural width even while clipped), so it is independent of the
  // current mode and settles in one pass. Page heads render after data loads, so it also watches the page content.
  useLayoutEffect(() => {
    const bar = ref.current;
    if (!bar) return undefined;
    let raf = 0;
    const measure = () => {
      raf = 0;
      const head = bar.parentElement && bar.parentElement.querySelector('.page > .page-head:first-child');
      let actionsW = 0;
      if (head) {
        const kids = Array.from(head.children).filter((k) => !k.matches('h1, .sr-only') && k.offsetWidth > 0);
        if (kids.length) {
          const rects = kids.map((k) => k.getBoundingClientRect());
          actionsW = Math.ceil(Math.max(...rects.map((r) => r.right)) - Math.min(...rects.map((r) => r.left))) + 10;
        }
      }
      const text = bar.querySelector('.page-title-text');
      const icon = bar.querySelector('.page-title-icon');
      const accent = bar.querySelector('.page-title-accent');
      const cs = getComputedStyle(bar);
      const used = (el) => (el && el.offsetWidth ? el.offsetWidth + 10 : 0);
      const need = parseFloat(cs.paddingLeft) + used(icon) + used(accent) + (text ? text.scrollWidth : 0) + (actionsW ? actionsW + 10 : 0);
      const stack = actionsW > 0 && need > bar.clientWidth;
      bar.setAttribute('data-stack', stack ? 'true' : 'false');
      bar.style.setProperty('--title-actions-w', stack || !actionsW ? '0px' : `${actionsW}px`);
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    measure();
    const content = bar.parentElement;
    let mo = null;
    let ro = null;
    if (typeof MutationObserver !== 'undefined' && content) {
      mo = new MutationObserver(schedule);
      mo.observe(content, { childList: true, subtree: true, characterData: true });
    }
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(schedule);
      ro.observe(bar);
    }
    window.addEventListener('resize', schedule);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      if (mo) mo.disconnect();
      if (ro) ro.disconnect();
      window.removeEventListener('resize', schedule);
    };
  }, [title, pathname]);

  if (!title) return null;
  const cls = ['page-title', back ? 'has-back' : '', shown ? 'is-shown' : 'is-hidden', atTop ? 'at-top' : 'is-stuck', reduce ? 'no-motion' : '']
    .filter(Boolean)
    .join(' ');
  const bar = (
    <div
      ref={ref}
      className={cls}
      aria-hidden="true"
      data-testid="page-title"
      data-state={shown ? 'shown' : 'hidden'}
      style={back ? { '--back-ch': back.label.length } : undefined}
    >
      <span className="page-title-accent" />
      {Icon && (
        <span className="page-title-icon">
          <Icon />
        </span>
      )}
      <span className="page-title-text" title={title}>{title}</span>
    </div>
  );
  if (!back) return bar;
  // The back link is a sibling, not a child: the bar is aria-hidden (decorative) and a link must stay reachable.
  // It overlays the bar's left end (the bar reserves room with `has-back`) and fades with it.
  return (
    <>
      {bar}
      <BackLink className={`page-back ${shown ? 'is-shown' : 'is-hidden'}`} />
    </>
  );
}

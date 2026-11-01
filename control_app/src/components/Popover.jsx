import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const MARGIN = 8;

/**
 * Small anchored popover primitive (HelpIcon builds the (?) definition on it).
 *   const ref = useRef(null);
 *   <button ref={ref} onClick={() => setOpen(true)}>?</button>
 *   <Popover open={open} onClose={() => setOpen(false)} anchorRef={ref} label="Definition: Edge">...</Popover>
 * - Rendered in a portal at the end of <body>, positioned below the anchor (above when there is no room),
 *   clamped to the viewport, so it is never clipped by a card or scroll area and works at 375 px.
 * - Focus moves into it when it opens, Tab / Shift+Tab stay inside it, and focus returns to the anchor
 *   when it closes.
 * - Esc and a tap/click outside (anywhere but the popover and its anchor) call onClose. The anchor's own
 *   click toggles it, so the caller decides what that does.
 */
export default function Popover({ open, onClose, anchorRef, label, children, className = '', width = 320 }) {
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [pos, setPos] = useState({ top: 0, left: 0, width });

  // Position from the anchor; follow scroll and resize while open.
  useLayoutEffect(() => {
    if (!open) return undefined;
    const place = () => {
      const a = anchorRef && anchorRef.current;
      const pop = ref.current;
      const vw = window.innerWidth || document.documentElement.clientWidth || 360;
      const vh = window.innerHeight || document.documentElement.clientHeight || 640;
      const w = Math.min(width, vw - 2 * MARGIN);
      if (!a) {
        setPos({ top: MARGIN, left: Math.max(MARGIN, (vw - w) / 2), width: w });
        return;
      }
      const r = a.getBoundingClientRect();
      const h = pop ? pop.offsetHeight : 0;
      const below = r.bottom + 6;
      const top = below + h > vh - MARGIN && r.top - 6 - h > MARGIN ? r.top - 6 - h : below;
      const centre = r.left + r.width / 2 - w / 2;
      const left = Math.min(Math.max(MARGIN, centre), Math.max(MARGIN, vw - w - MARGIN));
      setPos({ top, left, width: w });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open, anchorRef, width]);

  // Focus in on open, back to the anchor on close.
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    const pop = ref.current;
    if (pop) {
      const first = pop.querySelector(FOCUSABLE);
      (first || pop).focus({ preventScroll: true });
    }
    return () => {
      const a = anchorRef && anchorRef.current;
      const back = a && document.contains(a) ? a : previous;
      if (back && typeof back.focus === 'function' && document.contains(back)) back.focus({ preventScroll: true });
    };
  }, [open, anchorRef]);

  // Esc, outside tap, Tab trap.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeRef.current && closeRef.current();
        return;
      }
      if (e.key !== 'Tab' || !ref.current) return;
      const items = [...ref.current.querySelectorAll(FOCUSABLE)];
      if (!items.length) {
        e.preventDefault();
        ref.current.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === ref.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (!ref.current.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    const onOutside = (e) => {
      const t = e.target;
      if (ref.current && ref.current.contains(t)) return;
      if (anchorRef && anchorRef.current && anchorRef.current.contains(t)) return;
      closeRef.current && closeRef.current();
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onOutside);
    document.addEventListener('touchstart', onOutside, { passive: true });
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('mousedown', onOutside);
      document.removeEventListener('touchstart', onOutside);
    };
  }, [open, anchorRef]);

  if (!open || typeof document === 'undefined') return null;
  return createPortal(
    <div
      ref={ref}
      className={`popover ${className}`.trim()}
      role="dialog"
      aria-label={label}
      tabIndex={-1}
      style={{
        // never above the safe-area inset (status bar) and never taller than the space below it; long text scrolls inside
        top: `max(${pos.top}px, calc(var(--safe-top) + 8px))`,
        left: pos.left,
        width: pos.width,
        maxHeight: 'calc(100dvh - var(--safe-top) - var(--safe-bottom) - 24px)',
        overflowY: 'auto',
      }}
    >
      {children}
    </div>,
    document.body,
  );
}

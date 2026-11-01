import { useEffect, useLayoutEffect, useRef } from 'react';

// Body scroll lock shared by all open modals (iOS needs position:fixed; restore the scroll offset on the last close).
let lockCount = 0;
let saved = null;
function lockBody() {
  if (lockCount++ > 0) return;
  const b = document.body;
  saved = { y: window.scrollY || 0, position: b.style.position, top: b.style.top, width: b.style.width, overflow: b.style.overflow };
  b.style.position = 'fixed';
  b.style.top = `-${saved.y}px`;
  b.style.width = '100%';
  b.style.overflow = 'hidden';
}
function unlockBody() {
  if (lockCount === 0 || --lockCount > 0) return;
  const b = document.body;
  const { y, position, top, width, overflow } = saved;
  b.style.position = position;
  b.style.top = top;
  b.style.width = width;
  b.style.overflow = overflow;
  saved = null;
  try {
    window.scrollTo(0, y);
  } catch {
    /* jsdom: not implemented */
  }
}

// Centered dialog (mobile-first): a fixed header (title + dismiss) and a body that scrolls inside the card, kept inside the
// safe-area insets by index.css (--safe-top / --safe-bottom); tracks the visual viewport so the iOS keyboard never covers it. Escape / backdrop tap call onClose
// (pass onClose={undefined} to make it non-dismissable, e.g. while busy).
export default function Modal({ title, onClose, children, labelledBy = 'modal-title' }) {
  const ref = useRef(null);
  const backdropRef = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useLayoutEffect(() => {
    lockBody();
    // Size the backdrop to the visible area (above the on-screen keyboard) before first paint.
    const vv = window.visualViewport;
    const root = backdropRef.current;
    const sync = () => {
      if (!vv || !root) return;
      root.style.setProperty('--vv-top', `${vv.offsetTop}px`);
      root.style.setProperty('--vv-h', `${vv.height}px`);
    };
    sync();
    if (vv) {
      vv.addEventListener('resize', sync);
      vv.addEventListener('scroll', sync);
    }
    return () => {
      unlockBody();
      if (vv) {
        vv.removeEventListener('resize', sync);
        vv.removeEventListener('scroll', sync);
      }
    };
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape' && closeRef.current) closeRef.current();
    };
    const trap = (e) => {
      if (e.key !== 'Tab' || !ref.current) return;
      const items = ref.current.querySelectorAll('input:not(:disabled), button:not(:disabled), [href], select, textarea');
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('keydown', trap);
    const first = ref.current && ref.current.querySelector('input, .btn.primary, .btn.danger');
    if (first) first.focus({ preventScroll: true });
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('keydown', trap);
    };
  }, []);

  return (
    <div
      className="modal-backdrop"
      ref={backdropRef}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && closeRef.current) closeRef.current();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={labelledBy} ref={ref}>
        {(title || onClose) && (
          <div className="modal-head">
            {title ? (
              <h2 id={labelledBy} className="modal-title">
                {title}
              </h2>
            ) : (
              <span />
            )}
            {onClose && (
              <button type="button" className="modal-x" aria-label="Dismiss dialog" data-testid="modal-x" onClick={() => closeRef.current && closeRef.current()}>
                <span aria-hidden="true">×</span>
              </button>
            )}
          </div>
        )}
        <div className="modal-scroll">{children}</div>
      </div>
    </div>
  );
}

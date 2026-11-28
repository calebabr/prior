import { createContext, useContext, useEffect, useLayoutEffect, useRef } from 'react';

/** The Stop control shown above every dialog (DialogProvider supplies it when signed in; null = none). */
export const ModalStopContext = createContext(null);

// Open dialogs, oldest first. The top one (a Stop confirm beats everything) handles Escape and the Tab trap.
const stack = [];
function topEntry() {
  for (let i = stack.length - 1; i >= 0; i--) if (stack[i].priority) return stack[i];
  return stack[stack.length - 1];
}
const FOCUSABLE = 'input:not(:disabled), button:not(:disabled), [href], select, textarea';

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
// Every dialog has the Stop button above its card, where the header Stop sits, so Stop is reachable even while a
// request is in flight. layer="stop" is the Stop confirm itself: it stacks on top of any open dialog and gives focus
// back when it closes.
export default function Modal({ title, onClose, children, labelledBy = 'modal-title', layer }) {
  const ref = useRef(null);
  const wrapRef = useRef(null);
  const areaRef = useRef(null);
  const backdropRef = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const priority = layer === 'stop';
  const stopControl = useContext(ModalStopContext);
  const showStop = !priority && !!stopControl;

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
    const me = { priority };
    const previous = document.activeElement;
    stack.push(me);
    const isTop = () => topEntry() === me;
    const focusFirst = () => {
      const el = ref.current && ref.current.querySelector('input:not(:disabled), .btn.primary, .btn.danger');
      if (el) el.focus({ preventScroll: true });
    };
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.modalHandled || !isTop()) return;
      e.modalHandled = true;
      if (closeRef.current) closeRef.current();
    };
    const trap = (e) => {
      if (e.key !== 'Tab' || !wrapRef.current || !isTop()) return;
      const items = wrapRef.current.querySelectorAll(FOCUSABLE);
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (!wrapRef.current.contains(document.activeElement)) {
        e.preventDefault();
        firstEl.focus();
      } else if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    // A dialog underneath must not pull focus away from the Stop confirm (for example when its request fails).
    const guard = (e) => {
      if (isTop() && wrapRef.current && !wrapRef.current.contains(e.target)) focusFirst();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('keydown', trap);
    if (priority) document.addEventListener('focusin', guard);
    if (isTop()) focusFirst();
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('keydown', trap);
      document.removeEventListener('focusin', guard);
      stack.splice(stack.indexOf(me), 1);
      if (priority && previous && previous !== document.body && previous.isConnected && !previous.disabled) {
        previous.focus({ preventScroll: true });
      }
    };
  }, [priority]);

  return (
    <div
      className={`modal-backdrop${priority ? ' modal-layer-stop' : ''}${showStop ? ' has-stop' : ''}`}
      ref={backdropRef}
      onMouseDown={(e) => {
        const t = e.target;
        if ((t === backdropRef.current || t === wrapRef.current || t === areaRef.current) && closeRef.current) closeRef.current();
      }}
    >
      <div className="modal-wrap" role="dialog" aria-modal="true" aria-labelledby={labelledBy} ref={wrapRef}>
        <div className="modal-area" ref={areaRef}>
          <div className="modal" ref={ref}>
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
        {/* After the card in the document (Tab reaches the dialog first), drawn above it by CSS order. */}
        {showStop && (
          <div className="modal-stop" data-testid="modal-stop">
            {stopControl}
          </div>
        )}
      </div>
    </div>
  );
}

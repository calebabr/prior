import { useEffect, useRef } from 'react';
import { usePageVisible } from './usePageVisible.js';

/**
 * Calls `fn` every `ms` while the page is visible, and once straight away when it becomes visible again (the owner
 * switching back from another app). It does not call on mount: the caller's own first load does that.
 * `ms` of 0 or undefined turns it off.
 */
export default function usePollWhileVisible(fn, ms) {
  const visible = usePageVisible();
  const wasHidden = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!ms) return undefined;
    if (!visible) {
      wasHidden.current = true;
      return undefined;
    }
    if (wasHidden.current) {
      wasHidden.current = false;
      fnRef.current();
    }
    const id = setInterval(() => fnRef.current(), ms);
    return () => clearInterval(id);
  }, [visible, ms]);
}

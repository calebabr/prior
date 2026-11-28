import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import ConfirmDialog from '../components/ConfirmDialog.jsx';
import { ModalStopContext } from '../components/Modal.jsx';
import TwoFactorPrompt from '../components/TwoFactorPrompt.jsx';
import Toasts from '../components/Toasts.jsx';
import { STOPPED_EVENT, noteCrossedStop } from '../services/stopResult.js';

/**
 * App-wide modals and toasts.
 *
 *   const { confirm, twoFactor, notify } = useDialogs();
 *
 *   await confirm({ title, message, confirmLabel, danger, requireText })  -> boolean
 *   await twoFactor({ title, message, confirmLabel, danger, resumesTrading, action: (code) => api.x(code) })
 *        -> action result, or null if cancelled. A rejected/used code keeps the
 *           modal open for another try; any other error closes it and is re-thrown.
 *   notify({ kind: 'ok' | 'error' | 'info', title, text, details })
 *
 * For Stop only: confirmStop(opts) is a confirm on its own layer, above any open dialog; dismissDialogs() cancels
 * every other dialog once Stop is confirmed. A request already in flight cannot be recalled: its dialog closes when
 * it answers, a rejected code is not offered again, and with `resumesTrading` the answer resolves to null and a
 * persistent "check that the bot is stopped" warning is raised unless the server clearly refused it.
 */

const DialogContext = createContext(null);

const announceStopped = () => window.dispatchEvent(new Event(STOPPED_EVENT));
// A 4xx answer means the server refused it; a lost connection or a server error leaves it unknown.
const refused = (err) => !!err && err.status >= 400 && err.status < 500;

export function DialogProvider({ children, stopControl = null }) {
  const [confirmState, setConfirmState] = useState(null);
  const [tfaState, setTfaState] = useState(null);
  const [stopState, setStopState] = useState(null);
  const [toasts, setToasts] = useState([]);
  const nextId = useRef(1);
  const anchorRef = useRef(null);
  const openRef = useRef({});
  openRef.current = { confirm: confirmState, tfa: tfaState, stop: stopState };

  const confirm = useCallback(
    (opts) =>
      new Promise((resolve) => {
        setConfirmState({
          ...opts,
          resolve: (v) => {
            setConfirmState(null);
            resolve(v);
          },
        });
      }),
    [],
  );

  const confirmStop = useCallback(
    (opts) =>
      new Promise((resolve) => {
        if (openRef.current.stop) openRef.current.stop.resolve(false);
        setStopState({
          ...opts,
          resolve: (v) => {
            setStopState(null);
            resolve(v);
          },
        });
      }),
    [],
  );

  const twoFactor = useCallback(
    (opts) => {
      // iOS opens the keyboard only if focus() runs synchronously inside the tap: focus a hidden anchor now.
      if (anchorRef.current) anchorRef.current.focus({ preventScroll: true });
      return new Promise((resolve, reject) => {
        const control = { busy: false, superseded: false };
        setTfaState({
          ...opts,
          control,
          done: (result) => {
            setTfaState(null);
            if (!control.superseded) {
              resolve(result);
              return;
            }
            const crossed = !!opts.resumesTrading && result != null;
            if (crossed) noteCrossedStop(opts.title);
            announceStopped(); // pages re-read: this answer may be newer than the reload the Stop triggered
            resolve(opts.resumesTrading ? null : result);
          },
          fail: (err) => {
            setTfaState(null);
            if (control.superseded) {
              if (opts.resumesTrading && !refused(err)) noteCrossedStop(opts.title);
              announceStopped();
            }
            reject(err);
          },
        });
      });
    },
    [],
  );

  const dismissDialogs = useCallback(() => {
    const { confirm: c, tfa } = openRef.current;
    if (c) c.resolve(false);
    if (!tfa) return;
    if (tfa.control.busy) {
      tfa.control.superseded = true;
      setTfaState((s) => (s ? { ...s, superseded: true } : s));
    } else {
      tfa.done(null);
    }
  }, []);

  const dismiss = useCallback((id) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  const notify = useCallback(
    (toast) => {
      const id = nextId.current++;
      setToasts((t) => [...t.slice(-3), { id, kind: 'info', ...toast }]);
      const ms = toast.kind === 'error' ? 12000 : 6000;
      setTimeout(() => dismiss(id), ms);
      return id;
    },
    [dismiss],
  );

  const value = useMemo(
    () => ({ confirm, confirmStop, dismissDialogs, twoFactor, notify }),
    [confirm, confirmStop, dismissDialogs, twoFactor, notify],
  );

  return (
    <DialogContext.Provider value={value}>
      <ModalStopContext.Provider value={stopControl}>
        {children}
        {confirmState && <ConfirmDialog {...confirmState} />}
        {tfaState && <TwoFactorPrompt {...tfaState} anchorRef={anchorRef} />}
        {stopState && <ConfirmDialog {...stopState} layer="stop" />}
        <input
          ref={anchorRef}
          className="kb-anchor"
          data-testid="kb-anchor"
          aria-hidden="true"
          tabIndex={-1}
          inputMode="numeric"
          autoComplete="off"
          readOnly={false}
        />
        <Toasts toasts={toasts} onDismiss={dismiss} />
      </ModalStopContext.Provider>
    </DialogContext.Provider>
  );
}

export function useDialogs() {
  const ctx = useContext(DialogContext);
  if (!ctx) throw new Error('useDialogs must be used inside <DialogProvider>');
  return ctx;
}

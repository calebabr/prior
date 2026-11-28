import { useLayoutEffect, useRef, useState } from 'react';
import Modal from './Modal.jsx';

/**
 * 6-digit authenticator code modal for 2FA-protected actions. Runs `action(code)` itself so it
 * can show the loading state and keep the modal open when the server rejects the code
 * (403 2fa_invalid / 2fa_required). Other errors close the modal and go to `fail`.
 */
export default function TwoFactorPrompt({
  title = 'Enter 2FA code',
  message,
  confirmLabel = 'Confirm',
  danger = false,
  action,
  done,
  fail,
  anchorRef,
  control,
  superseded = false,
}) {
  const inputRef = useRef(null);
  // Hand focus from the shell's keyboard anchor to the real input (moving focus keeps the iOS keyboard up).
  useLayoutEffect(() => {
    if (inputRef.current) inputRef.current.focus({ preventScroll: true });
    const a = anchorRef && anchorRef.current;
    if (a) {
      a.value = '';
      if (document.activeElement === a) a.blur();
    }
  }, [anchorRef]);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const valid = /^\d{6}$/.test(code);

  async function submit(e) {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    if (control) control.busy = true;
    try {
      const result = action ? await action(code) : code;
      done(result);
    } catch (err) {
      if (control) control.busy = false;
      if (err && err.is2fa) {
        if (control && control.superseded) {
          done(null); // Stop was tapped meanwhile: do not offer to send this again
          return;
        }
        setBusy(false);
        setCode('');
        setError(
          err.detail === '2fa_required'
            ? 'The server did not receive a code. Try again.'
            : 'Code Rejected: Wrong, expired or already used. Wait for the next code and try again.',
        );
        return;
      }
      fail(err);
    }
  }

  return (
    <Modal title={title} onClose={busy ? undefined : () => done(null)} labelledBy="tfa-title">
      <form onSubmit={submit}>
        {superseded && (
          <p className="error-text modal-body" role="status">
            Stop was sent. This request had already left and may still finish.
          </p>
        )}
        {message && <div className="modal-body">{message}</div>}
        <label className="field">
          <span className="field-label">Code from your authenticator app</span>
          <input
            className="code-input"
            ref={inputRef}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="\d{6}"
            maxLength={6}
            placeholder="123456"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            disabled={busy}
            aria-label="6-digit code"
          />
        </label>
        <p className="hint">Each code works once. If you just used one, wait for the next.</p>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => done(null)} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className={`btn ${danger ? 'danger' : 'primary'}`} disabled={!valid || busy}>
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </form>
    </Modal>
  );
}

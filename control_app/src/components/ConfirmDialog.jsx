import { useState } from 'react';
import Modal from './Modal.jsx';

/**
 * Yes/no confirmation. With `requireText` (e.g. "LIVE") the confirm button only
 * enables once the user has typed that exact text. With `requireCheck` (a label) the button also needs that
 * acknowledgement checkbox ticked.
 */
export default function ConfirmDialog({
  title = 'Are you sure?',
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  requireText,
  requireCheck,
  resolve,
}) {
  const [typed, setTyped] = useState('');
  const [checked, setChecked] = useState(false);
  const ok = (!requireText || typed === requireText) && (!requireCheck || checked);
  return (
    <Modal title={title} onClose={() => resolve(false)} labelledBy="confirm-title">
      {message && <div className="modal-body">{message}</div>}
      {requireText && (
        <label className="field">
          <span className="field-label">
            Type <strong>{requireText}</strong> to confirm
          </span>
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoCapitalize="characters"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            aria-label={`Type ${requireText} to confirm`}
          />
        </label>
      )}
      {requireCheck && (
        <label className="ux-check breaker-check-row">
          <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          <span>{requireCheck}</span>
        </label>
      )}
      <div className="modal-actions">
        <button type="button" className="btn" onClick={() => resolve(false)}>
          {cancelLabel}
        </button>
        <button
          type="button"
          className={`btn ${danger ? 'danger' : 'primary'}`}
          disabled={!ok}
          onClick={() => resolve(true)}
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

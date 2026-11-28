import { badgeStyles } from '../utils/theme.js';
import { badgeKind } from '../utils/status.js';

/**
 * 🟢 live running / 🟡 demo running / ⚪ stopped / 🔴 error or heartbeat > 30 min.
 * Pass `status` (GET /api/status) or an explicit `kind`. `unconfirmed` (the status is old or the last read failed)
 * shows "Not confirmed" instead of a state that may no longer be true. The compact header badge drops the word
 * "Live" because the LIVE tag sits beside it.
 */
export default function StatusBadge({ status, kind: forcedKind, compact = false, unconfirmed = false }) {
  const { kind, reason } = forcedKind
    ? { kind: forcedKind, reason: null }
    : unconfirmed
      ? { kind: 'stale', reason: 'The latest status could not be read' }
      : badgeKind(status);
  const style = badgeStyles[kind] || badgeStyles.unknown;
  const label = compact && kind === 'error' ? 'Error' : compact && kind === 'live' ? 'Running' : style.label;
  return (
    <span
      className={`badge badge-${kind} ${compact ? 'badge-compact' : ''}`}
      title={reason || style.label}
      data-kind={kind}
    >
      <span className="badge-dot" aria-hidden="true" />
      <span className="badge-label">{label}</span>
    </span>
  );
}

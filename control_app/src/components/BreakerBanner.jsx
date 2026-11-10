import { Link } from 'react-router-dom';
import { dateTime } from '../utils/format.js';
import { breakerReasonLabel } from '../utils/breaker.js';
import '../styles/breaker.css';

/**
 * Loss circuit breaker notices from status.breaker.
 * - tripped: red. `onRestart` (Home) shows the Start flow button; elsewhere (`compact`) a link to Home.
 * - warning (not tripped): amber one-liner, Home only (`compact` hides it).
 * Informational only: it never covers Stop, which stays in the header.
 */
export default function BreakerBanner({ breaker, compact = false, onRestart, busy = false }) {
  if (!breaker) return null;
  if (breaker.tripped) {
    return (
      <div className={`breaker-banner breaker-tripped${compact ? ' is-compact' : ''}`} role="alert" data-testid="breaker-banner">
        <div className="breaker-text">
          <strong className="breaker-title">Circuit breaker tripped</strong>
          <span className="breaker-sub">
            {breakerReasonLabel(breaker.reason)} reached{breaker.utc ? <> at {dateTime(breaker.utc)}</> : null}. Trading is stopped and will not restart until you acknowledge it.
          </span>
        </div>
        {compact ? (
          <Link to="/" className="btn small breaker-action" data-testid="breaker-link">
            Go to Start
          </Link>
        ) : (
          onRestart && (
            <button type="button" className="btn small breaker-action" onClick={onRestart} disabled={busy} data-testid="breaker-restart">
              Review and restart
            </button>
          )
        )}
      </div>
    );
  }
  if (breaker.warning && !compact) {
    return (
      <div className="breaker-banner breaker-warn" role="status" data-testid="breaker-warning">
        <div className="breaker-text">
          <strong className="breaker-title">Circuit breaker check skipped</strong>
          <span className="breaker-sub">{String(breaker.warning)}</span>
        </div>
      </div>
    );
  }
  return null;
}

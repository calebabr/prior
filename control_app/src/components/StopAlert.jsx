import { dismissStopRecord, useStopRecord } from '../services/stopResult.js';

const SHOWN = ['failed', 'partial', 'unconfirmed'];

/**
 * The persistent warning for a Stop that failed, only partly worked or was never confirmed. Part of the sticky
 * header (every page and the login screen), below the Stop button. Gone after a later successful Stop or Dismiss.
 */
export default function StopAlert() {
  const rec = useStopRecord();
  if (!rec || !SHOWN.includes(rec.kind)) return null;
  return (
    <div className={`stop-alert stop-alert-${rec.kind}`} role="alert" data-testid="stop-alert">
      <div className="stop-alert-body">
        <strong className="stop-alert-title">{rec.title}</strong>
        <span className="stop-alert-text">{rec.text}</span>
        {rec.details && rec.details.length > 0 && (
          <ul className="detail-list">
            {rec.details.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        )}
      </div>
      <button type="button" className="btn small stop-alert-dismiss" aria-label="Dismiss Stop warning" onClick={dismissStopRecord}>
        Dismiss
      </button>
    </div>
  );
}

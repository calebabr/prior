import { Link } from 'react-router-dom';
import StatusBadge from './StatusBadge.jsx';
import StopButton from './StopButton.jsx';
import StopAlert from './StopAlert.jsx';
import PriorLogo from './PriorLogo.jsx';
import { useStatus } from '../context/StatusContext.jsx';
import { isMockMode } from '../services/api.js';
import { APP_NAME } from '../utils/routes.js';

// Sticky header on every signed-in page: Prior lockup + LIVE tag + status badge + compact STOP (always visible), and
// under them the warning for a Stop that did not fully work.
export default function Header() {
  const { status, unconfirmed } = useStatus();
  return (
    <header className={`appbar${unconfirmed ? ' is-unconfirmed' : ''}`}>
      <Link to="/" className="appbar-title" aria-label={`${APP_NAME} home`}>
        <PriorLogo size={24} />
        <span className="appbar-name">{APP_NAME}</span>
        {isMockMode() && <span className="mock-tag">MOCK</span>}
      </Link>
      <div className="appbar-right">
        {status && status.mode === 'live' && (
          <span className="live-tag" data-testid="live-tag" title="Live mode: real money">
            LIVE
          </span>
        )}
        <StatusBadge status={status} unconfirmed={unconfirmed} compact />
        <StopButton compact />
      </div>
      <StopAlert />
    </header>
  );
}

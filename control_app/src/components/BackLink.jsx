import { Link, useLocation, useNavigate } from 'react-router-dom';
import { usePreviousPath } from '../hooks/useNavTrail.js';
import { parentForPath, titleForPath } from '../utils/routes.js';

/**
 * Title-bar navigation controls for a child page (nothing on the five tabs):
 *  - "‹" Back: real history (navigate(-1)) when the app has an earlier in-app page, so the owner can bounce between
 *    pages; with no in-app history (deep link, reload, first page) it goes to the parent. Accessible name "Back".
 *  - "<Parent> ›" breadcrumb: always goes UP to the parent page ("Up to Performance").
 * Both are >= 40 px tall (the .page-back class sets 44). Rendered as one group so the title can follow it.
 */
export default function BackLink({ className = '' }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const prev = usePreviousPath();
  const parent = parentForPath(pathname);
  if (!parent) return null;
  const parentTitle = titleForPath(parent);
  const prevTitle = prev ? titleForPath(prev) : null;
  const useHistory = !!prev;
  return (
    <div className={`back-group ${className}`.trim()} data-testid="back-group">
      <Link
        to={useHistory ? prev : parent}
        className="back-btn"
        aria-label="Back"
        title={prevTitle ? `Back to ${prevTitle}` : `Back to ${parentTitle}`}
        data-testid="back-link"
        onClick={(e) => {
          if (!useHistory || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
          e.preventDefault();
          navigate(-1);
        }}
      >
        <span aria-hidden="true">‹</span>
      </Link>
      <Link to={parent} className="back-up" aria-label={`Up to ${parentTitle}`} data-testid="up-link">
        <span aria-hidden="true" className="back-up-full">{parentTitle}</span>
        <span aria-hidden="true" className="back-sep">›</span>
        <span aria-hidden="true" className="back-up-short">↑</span>
      </Link>
    </div>
  );
}

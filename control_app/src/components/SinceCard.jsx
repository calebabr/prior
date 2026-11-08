import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getSince } from '../services/overviewApi.js';
import { readLastVisit, sinceLabel, sinceParts, writeLastVisit } from '../utils/overview.js';
import '../styles/overview.css';

/**
 * "Since you last checked" strip (item 30; one line, dismissible, never pushes money figures down): new fills, markets settled and the P&L change since this device's
 * last visit (localStorage prior.lastVisit). Read-only. The last-visit time is written when the page is left
 * or hidden, and when the card is dismissed. First visit: writes now, shows nothing. Hidden when nothing
 * changed or the request fails.
 */
export default function SinceCard() {
  const [since, setSince] = useState(null);
  const [from, setFrom] = useState(null);
  const [gone, setGone] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    const mountedAt = Date.now();
    const last = readLastVisit();
    if (!last) writeLastVisit();
    else {
      setFrom(last);
      getSince(last)
        .then((d) => alive && setSince(d))
        .catch(() => {});
    }
    const stamp = () => writeLastVisit();
    const hide = () => document.visibilityState === 'hidden' && stamp();
    document.addEventListener('visibilitychange', hide);
    window.addEventListener('pagehide', stamp);
    return () => {
      alive = false;
      document.removeEventListener('visibilitychange', hide);
      window.removeEventListener('pagehide', stamp);
      // Not for the instant unmount of a StrictMode dev double-mount.
      if (Date.now() - mountedAt > 2000) stamp();
    };
  }, []);

  if (gone || !since || !from) return null;
  const parts = sinceParts(since, sinceLabel(from)).map((t) =>
    (/^\d/.test(t) ? t.toLowerCase() : t).replace(/ Since /, ' since '),
  );
  if (!parts.length) return null;
  return (
    <section className="since-card" data-testid="since-card" aria-label="Since you last checked">
      <div className="since-body">
        <button
          type="button"
          className={`since-line ${open ? 'open' : ''}`}
          aria-expanded={open}
          data-testid="since-toggle"
          onClick={() => setOpen((o) => !o)}
        >
          <span className="since-title">Since you last checked:</span> {parts.join(', ')}
        </button>
        {open && (
          <Link to="/alerts" className="since-more" data-testid="since-all-alerts">
            All alerts
          </Link>
        )}
      </div>
      <button
        type="button"
        className="since-dismiss"
        aria-label="Dismiss"
        onClick={() => {
          writeLastVisit();
          setGone(true);
        }}
      >
        ×
      </button>
    </section>
  );
}

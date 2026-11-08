import {
  IconAccount, IconActivity, IconAnalytics, IconArb, IconBell, IconHealth, IconLogs, IconMarkets, IconMode,
  IconBook, IconLock, IconMore, IconOverview, IconServer, IconSettings,
} from './Icons.jsx';

// Route -> the icon shown in the tab bar, sidebar, More page and the page-title bar.
// A deeper path falls back to its nearest parent that has an icon ("/strategies/maker/results" -> maker).
const ICONS = {
  '/': IconOverview,
  '/strategies': IconMarkets,
  '/strategies/maker': IconMarkets,
  '/strategies/arb': IconArb,
  '/performance': IconAnalytics,
  '/performance/periods': IconAnalytics,
  '/analytics': IconAnalytics,
  '/shadow': IconAnalytics,
  '/activity': IconActivity,
  '/more': IconMore,
  '/health': IconHealth,
  '/settings': IconSettings,
  '/account': IconAccount,
  '/alerts': IconBell,
  '/mode': IconMode,
  '/arming': IconLock,
  '/server': IconServer,
  '/logs': IconLogs,
  '/terminology': IconBook,
};

export function iconForPath(path) {
  let p = String(path || '/').replace(/\/+$/, '') || '/';
  while (p) {
    if (ICONS[p]) return ICONS[p];
    const i = p.lastIndexOf('/');
    if (i <= 0) return null;
    p = p.slice(0, i);
  }
  return null;
}

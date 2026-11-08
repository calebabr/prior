// The one route map for signed-in pages: titles, bottom tabs, the wide-screen sidebar groups, the More page
// groups and the legacy redirects. Used by the sticky page title (components/PageTitle.jsx), document.title,
// NavBar, More and App.jsx (src/test/route-map.test.jsx and page-title.test.jsx check every entry renders).
// Information architecture: UI-1a stage 2, section 3 (five phone tabs: Home | Strategies | Performance |
// Activity | More). Paths of pages that already existed are unchanged.
export const APP_NAME = 'Prior';
export const TAGLINE_LEAD = 'Your strategies.';
export const TAGLINE_ACCENT = 'One tap away.';
export const TAGLINE = `${TAGLINE_LEAD} ${TAGLINE_ACCENT}`;

// ---------------------------------------------------------------- strategies

/** Strategy ids that have a page, with the short name used in chrome (no "Bot · " prefix). */
export const STRATEGY_PAGES = { maker: 'Maker', arb: 'Arbitrage' };
export const STRATEGY_IDS = Object.keys(STRATEGY_PAGES);
/** The three views of a strategy page (one path segment each, so Back works). */
export const STRATEGY_VIEWS = [
  { id: 'overview', label: 'Overview', segment: '' },
  { id: 'results', label: 'Results', segment: 'results' },
  { id: 'controls', label: 'Controls', segment: 'controls' },
];

/** "/strategies/maker", "/strategies/maker/results", ... (`view` is 'overview' | 'results' | 'controls'). */
export function strategyPath(id, view = 'overview') {
  const v = STRATEGY_VIEWS.find((x) => x.id === view) || STRATEGY_VIEWS[0];
  return `/strategies/${id}${v.segment ? `/${v.segment}` : ''}`;
}

// Reserved, hidden slot: the Analysis page does not exist yet (no tab, no link). The path is claimed so a later
// build can mount it without a route change; for now it falls back to the Performance hub.
export const PERFORMANCE_ANALYSIS_PATH = '/performance/analysis';

// ---------------------------------------------------------------- titles

const strategyTitles = STRATEGY_IDS.flatMap((id) =>
  // parent: where the back control goes.
  STRATEGY_VIEWS.map((v) => ({ path: strategyPath(id, v.id), title: STRATEGY_PAGES[id], parent: '/strategies' })),
);

// Page names are Title Case. Strategy sub-pages title as "Maker" / "Arbitrage" (the view name is in the tablist).
export const ROUTE_TITLES = [
  { path: '/', title: 'Home' },
  { path: '/strategies', title: 'Strategies' },
  ...strategyTitles,
  { path: '/performance', title: 'Performance' },
  { path: '/performance/periods', title: 'Periods', parent: '/performance' },
  { path: '/analytics', title: 'Analytics', parent: '/performance' },
  { path: '/shadow', title: 'Shadow Estimate', parent: '/performance' },
  { path: '/activity', title: 'Activity' },
  { path: '/more', title: 'More' },
  { path: '/health', title: 'Health', parent: '/more' },
  { path: '/server', title: 'Server', parent: '/more' },
  { path: '/mode', title: 'Mode', parent: '/more' },
  { path: '/arming', title: 'Arming', parent: '/more' },
  { path: '/logs', title: 'Logs', parent: '/more' },
  { path: '/account', title: 'Account', parent: '/more' },
  { path: '/settings', title: 'Settings', parent: '/more' },
  { path: '/alerts', title: 'Alerts', parent: '/more' },
  { path: '/terminology', title: 'Terminology', parent: '/more' },
];

// ---------------------------------------------------------------- legacy redirects

/** Old path -> new path. Query string and hash are kept (notification links, bookmarks). */
export const LEGACY_REDIRECTS = [
  { from: '/markets', to: '/strategies/maker/controls' },
  { from: '/arb', to: '/strategies/arb' },
];

function normalise(pathname) {
  return String(pathname || '/').replace(/\/+$/, '') || '/';
}

/** The new path for an old one ("/markets/" matches too), or null when `pathname` is not a legacy path. */
export function redirectTarget(pathname) {
  const p = normalise(pathname);
  const hit = LEGACY_REDIRECTS.find((r) => r.from === p);
  return hit ? hit.to : null;
}

// Pages whose path carries a parameter (not in ROUTE_TITLES, which lists literal paths): the Day view.
export const PATTERN_ROUTES = [
  { pattern: /^\/performance\/day\/\d{4}-\d{2}-\d{2}$/, title: 'Day', parent: '/performance' },
];
const patternFor = (p) => PATTERN_ROUTES.find((r) => r.pattern.test(p));

/** Page title for a pathname ("/health/" and "/health" both match), or null if unknown. */
export function titleForPath(pathname) {
  const p = normalise(pathname);
  const hit = ROUTE_TITLES.find((r) => r.path === p) || patternFor(p);
  return hit ? hit.title : null;
}

/** Parent of a route (the back arrow's target), or null for the five tabs and unknown paths. */
export function parentForPath(pathname) {
  const p = normalise(pathname);
  const hit = ROUTE_TITLES.find((r) => r.path === p) || patternFor(p);
  return hit && hit.parent ? hit.parent : null;
}

/** document.title for a page: "Health · Prior"; just "Prior" when unknown. */
export function documentTitle(title) {
  return title ? `${title} · ${APP_NAME}` : APP_NAME;
}

// ---------------------------------------------------------------- bottom tabs

// Phone bottom tabs, in the owner's order (fixed). `key` is what tabForPath() returns.
export const TAB_ROUTES = [
  { key: 'home', path: '/', label: 'Home' },
  { key: 'strategies', path: '/strategies', label: 'Strategies' },
  { key: 'performance', path: '/performance', label: 'Performance' },
  { key: 'activity', path: '/activity', label: 'Activity' },
  { key: 'more', path: '/more', label: 'More' },
];

const under = (p, base) => p === base || p.startsWith(`${base}/`);

/**
 * Which tab key lights up for a pathname: 'home' | 'strategies' | 'performance' | 'activity' | 'more'.
 * Old paths (/markets, /arb) light Strategies so the tab never flashes More while they redirect;
 * /analytics and /shadow light Performance. Unknown paths return null.
 */
export function tabForPath(pathname) {
  const p = normalise(pathname);
  if (p === '/') return 'home';
  if (under(p, '/strategies') || p === '/markets' || p === '/arb') return 'strategies';
  if (under(p, '/performance') || p === '/analytics' || p === '/shadow') return 'performance';
  if (under(p, '/activity')) return 'activity';
  if (p === '/more' || MORE_ROUTES.some((r) => under(p, r.path))) return 'more';
  return null;
}

// ---------------------------------------------------------------- More page and sidebar groups

// What the More page lists, grouped (System, You, Help). The same groups head the wide-screen sidebar.
export const MORE_GROUPS = [
  {
    id: 'system',
    label: 'System',
    items: [
      { path: '/health', label: 'Health', blurb: 'Monitor summary and health check' },
      { path: '/server', label: 'Server', blurb: 'Keep-awake pings and the external pinger' },
      { path: '/mode', label: 'Mode', blurb: 'Switch between demo and live' },
      { path: '/arming', label: 'Arming', blurb: 'Live-readiness checks and arming switches' },
      { path: '/logs', label: 'Logs', blurb: 'Server log and audit trail' },
    ],
  },
  {
    id: 'you',
    label: 'You',
    items: [
      { path: '/account', label: 'Account', blurb: 'Your Kalshi balance and open positions' },
      { path: '/settings', label: 'Settings', blurb: 'Limits, polling, display and text size' },
      { path: '/alerts', label: 'Alerts', blurb: 'Past alerts and notification choices' },
    ],
  },
  {
    id: 'help',
    label: 'Help',
    items: [{ path: '/terminology', label: 'Terminology', blurb: 'Plain-language glossary' }],
  },
];

/** Flat list of every page under More, in group order. */
export const MORE_ROUTES = MORE_GROUPS.flatMap((g) => g.items);

// Wide-screen (>= 1024 px) sidebar: same destinations as the tabs, with children under Strategies and
// Performance, then the More groups. `children` are indented links; Strategies is expanded by default.
export const SIDEBAR_MAIN = [
  { path: '/', label: 'Home', tab: 'home' },
  {
    path: '/strategies',
    label: 'Strategies',
    tab: 'strategies',
    end: true,
    children: [
      { path: '/strategies/maker', label: 'Maker', match: '/strategies/maker' },
      { path: '/strategies/arb', label: 'Arbitrage', match: '/strategies/arb' },
    ],
  },
  {
    path: '/performance',
    label: 'Performance',
    tab: 'performance',
    end: true,
    selfActive: false,
    children: [
      { path: '/performance', label: 'All Strategies', end: true },
      { path: '/analytics', label: 'Analytics' },
      { path: '/shadow', label: 'Shadow Estimate' },
      { path: '/performance/periods', label: 'Periods' },
    ],
  },
  { path: '/activity', label: 'Activity', tab: 'activity' },
];

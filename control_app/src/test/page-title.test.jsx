import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import PageTitle from '../components/PageTitle.jsx';
import { SCROLL_IDLE_MS } from '../hooks/useScrollIdle.js';
import { ROUTE_TITLES, documentTitle, titleForPath } from '../utils/routes.js';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

function setScrollY(y) {
  Object.defineProperty(window, 'scrollY', { value: y, writable: true, configurable: true });
}
function scrollTo(y) {
  setScrollY(y);
  act(() => {
    fireEvent.scroll(window);
  });
}
const bar = () => screen.getByTestId('page-title');
const renderBar = (path = '/health') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <PageTitle />
    </MemoryRouter>,
  );

const realMatchMedia = window.matchMedia;

beforeEach(() => {
  setScrollY(0);
  setToken(null);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.matchMedia = realMatchMedia;
  setScrollY(0);
});

describe('route -> title map', () => {
  it('maps every signed-in route and builds document.title', () => {
    expect(ROUTE_TITLES.map((r) => r.title)).toEqual([
      'Home', 'Strategies', 'Maker', 'Maker', 'Maker', 'Arbitrage', 'Arbitrage', 'Arbitrage', 'Performance', 'Periods',
      'Analytics', 'Shadow Estimate', 'Activity', 'More', 'Health', 'Server', 'Mode', 'Arming', 'Logs', 'Account', 'Settings',
      'Alerts', 'Terminology',
    ]);
    expect(titleForPath('/')).toBe('Home');
    expect(titleForPath('/health/')).toBe('Health');
    expect(titleForPath('/markets')).toBeNull(); // redirects; never titled
    expect(titleForPath('/nope')).toBeNull();
    expect(documentTitle('Health')).toBe('Health · Prior');
    expect(documentTitle(null)).toBe('Prior');
  });
});

describe('PageTitle scroll behaviour (fake timers)', () => {
  beforeEach(() => vi.useFakeTimers());

  it('is visible at the top and never hides there', () => {
    renderBar();
    expect(bar()).toHaveTextContent('Health');
    expect(bar()).toHaveAttribute('data-state', 'shown');
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS * 4));
    expect(bar()).toHaveAttribute('data-state', 'shown');
    expect(bar()).toHaveClass('at-top');
  });

  it('scrolled down: visible while scrolling, hides after idle, reappears on scroll', () => {
    renderBar();
    scrollTo(400);
    expect(bar()).toHaveAttribute('data-state', 'shown');
    expect(bar()).toHaveClass('is-stuck');
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS - 100));
    scrollTo(450); // still scrolling: idle countdown restarts
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS - 100));
    expect(bar()).toHaveAttribute('data-state', 'shown');
    act(() => vi.advanceTimersByTime(200));
    expect(bar()).toHaveAttribute('data-state', 'hidden');
    expect(bar()).toHaveClass('is-hidden');
    scrollTo(420);
    expect(bar()).toHaveAttribute('data-state', 'shown');
  });

  it('back at the top it shows and stays shown', () => {
    renderBar();
    scrollTo(300);
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS + 10));
    expect(bar()).toHaveAttribute('data-state', 'hidden');
    scrollTo(0);
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS * 3));
    expect(bar()).toHaveAttribute('data-state', 'shown');
  });

  it('publishes --sticky-top: header + bar while shown, header only when hidden', () => {
    renderBar();
    // jsdom has no layout: offsetHeight is 0, so only the shape is checked.
    expect(document.documentElement.style.getPropertyValue('--sticky-top')).toBe('0px');
    scrollTo(300);
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS + 10));
    expect(document.documentElement.style.getPropertyValue('--sticky-top')).toBe('0px');
  });

  it('prefers-reduced-motion: no transition class, instant show/hide', () => {
    window.matchMedia = (query) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    });
    renderBar();
    expect(bar()).toHaveClass('no-motion');
    scrollTo(500);
    act(() => vi.advanceTimersByTime(SCROLL_IDLE_MS + 10));
    expect(bar()).toHaveAttribute('data-state', 'hidden');
    scrollTo(510);
    expect(bar()).toHaveAttribute('data-state', 'shown');
  });

  it('without reduced motion it fades (no no-motion class)', () => {
    renderBar();
    expect(bar()).not.toHaveClass('no-motion');
  });
});

describe('PageTitle in the app', () => {
  for (const { path, title } of ROUTE_TITLES) {
    it(`${path} shows "${title}" in the bar, sets document.title and has one accessible h1`, async () => {
      stubApi({ 'GET /api/status': STATUS, 'GET /api/history': { snapshots: [] } });
      renderApp(path);
      expect(bar()).toHaveTextContent(title);
      expect(bar()).toHaveAttribute('aria-hidden', 'true');
      expect(document.title).toBe(`${title} · Prior`);
      // The page's own h1 is the single accessible heading (the bar is aria-hidden).
      await waitFor(() => expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1));
      expect(screen.getByRole('heading', { level: 1 }).textContent).toContain(title);
    });
  }

  it('signed out: no bar, document.title is the app name', () => {
    renderApp('/', { signedIn: false });
    expect(screen.queryByTestId('page-title')).toBeNull();
    expect(document.title).toBe('Prior');
  });

  it('header Stop stays visible and clickable while the bar is shown', async () => {
    const user = userEvent.setup();
    stubApi({ 'GET /api/status': STATUS });
    renderApp('/strategies');
    expect(bar()).toHaveAttribute('data-state', 'shown');
    const header = document.querySelector('header.appbar');
    const stop = within(header).getByRole('button', { name: /stop the bot/i });
    expect(bar().contains(stop)).toBe(false);
    expect(stop.closest('[aria-hidden="true"]')).toBeNull();
    // The bar sits inside <main>, after the header, never inside it or the tab bar.
    expect(header.contains(bar())).toBe(false);
    expect(document.querySelector('main.content').contains(bar())).toBe(true);
    await user.click(stop);
    expect(await screen.findByRole('button', { name: 'Stop now' })).toBeInTheDocument();
  });
});

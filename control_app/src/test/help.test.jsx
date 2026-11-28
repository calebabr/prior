import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { GLOSSARY, GROUPS } from '../utils/glossary.js';
import { editDistance, highlightSegments, rankTerms, searchTerms, stem } from '../utils/search.js';
import { clearTrail, getTrail, recordTrail } from '../utils/navTrail.js';
import { titleCase } from '../utils/format.js';
import HelpIcon from '../components/HelpIcon.jsx';
import Terminology from '../pages/Terminology.jsx';

const top = (q) => rankTerms(q, GLOSSARY)[0]?.id;

describe('search v2: query -> expected top result', () => {
  const table = [
    ['queue', 'queue-position'],
    ['queues', 'queue-position'],
    ['qeue', 'queue-position'],
    ['quue', 'queue-position'],
    ['P&L', 'pnl'],
    ['pnl', 'pnl'],
    ['profit', 'pnl'],
    ['profits', 'pnl'],
    ['ci', 'confidence-interval'],
    ['range', 'confidence-interval'],
    ['confidence interval', 'confidence-interval'],
    ['confidance', 'confidence-interval'],
    ['filled', 'fill'],
    ['fills', 'fill'],
    ['quotes', 'quote'],
    ['quoted', 'quote'],
    ['stopping', 'stop'],
    ['kill switch', 'kill-switch'],
    ['fee', 'fee'],
    ['fees', 'fee'],
    ['2fa', 'two-factor'],
    ['heartbeet', 'heartbeat'],
    ['drawdown', 'drawdown'],
    ['sttlement', 'settlement'],
    ['cash', 'balance'],
  ];
  it.each(table)('"%s" -> %s', (q, id) => {
    expect(top(q)).toBe(id);
  });

  it('stems plurals and verb endings to a common form', () => {
    expect(stem('queues')).toBe(stem('queue'));
    expect(stem('filled')).toBe(stem('fills'));
    expect(stem('stopped')).toBe(stem('stop'));
    expect(stem('quoting')).toBe(stem('quote'));
    expect(stem('ci')).toBe('ci');
  });

  it('edit distance counts a swapped pair as one edit and stops early past the cap', () => {
    expect(editDistance('queue', 'qeue')).toBe(1);
    expect(editDistance('fill', 'filll')).toBe(1);
    expect(editDistance('abcdef', 'fedcba', 2)).toBeGreaterThan(2);
  });

  it('1-2 letter queries do not match inside words, and gibberish matches nothing', () => {
    expect(rankTerms('zzzqqq', GLOSSARY)).toEqual([]);
    const ids = rankTerms('ci', GLOSSARY).map((e) => e.id);
    expect(ids[0]).toBe('confidence-interval');
    expect(ids).not.toContain('sample-size');
  });

  it('field weights: a title hit beats an alias hit beats a short-line hit beats a body-only hit', () => {
    const mk = (o) => ({ id: 'x', term: 'Zed', aliases: [], short: '', body: '', ...o });
    const entries = [
      mk({ id: 'body', term: 'Body Term', body: 'we mention banana here' }),
      mk({ id: 'short', term: 'Short Term', short: 'about banana' }),
      mk({ id: 'alias', term: 'Alias Term', aliases: ['banana'] }),
      mk({ id: 'title', term: 'Banana' }),
    ];
    expect(rankTerms('banana', entries).map((e) => e.id)).toEqual(['title', 'alias', 'short', 'body']);
    const kinds = Object.fromEntries(searchTerms('banana', entries).map((r) => [r.entry.id, r.kind]));
    expect(kinds).toEqual({ title: 'name', alias: 'name', short: 'short', body: 'body' });
  });

  it('strong/weak split: P&L has fewer strong results than total matches', () => {
    const r = searchTerms('P&L', GLOSSARY);
    expect(r[0].entry.id).toBe('pnl');
    expect(r.filter((x) => x.strong).length).toBeLessThan(r.length);
  });

  it('highlightSegments marks matched words (also by stem) and leaves the rest', () => {
    const segs = highlightSegments('Fills from the front of the line', 'fill');
    expect(segs.filter((s) => s.hit).map((s) => s.text)).toEqual(['Fills']);
    expect(highlightSegments('abc', '').every((s) => !s.hit)).toBe(true);
    expect(highlightSegments('Total P&L today', 'p&l').filter((s) => s.hit).map((s) => s.text.trim())).toEqual(['P', 'L']);
  });
});

describe('glossary Title Case', () => {
  it('term names and group titles are Title Case (idempotent under titleCase), code names untouched', () => {
    for (const e of GLOSSARY) {
      if (/_/.test(e.term)) continue;
      expect(titleCase(e.term).replace(/\bVs\b/, 'vs'), e.id).toBe(e.term);
    }
    for (const g of GROUPS) expect(titleCase(g.title)).toBe(g.title);
    const names = Object.fromEntries(GLOSSARY.map((e) => [e.id, e.term]));
    expect(names['queue-position']).toBe('Queue Position');
    expect(names['front-of-line']).toBe('Front of Line');
    expect(names['maker-taker']).toBe('Maker vs Taker');
    expect(names['open-fraction']).toContain('max_open_frac');
  });
});

function Where() {
  const l = useLocation();
  return <output data-testid="where">{l.pathname + l.hash}</output>;
}

describe('HelpIcon popover', () => {
  it('opens on tap, shows the short definition, closes on Esc and returns focus to the icon', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <HelpIcon term="queue-position" />
      </MemoryRouter>,
    );
    const btn = screen.getByRole('button', { name: 'What is Queue Position?' });
    await user.click(btn);
    const pop = screen.getByRole('dialog', { name: 'Definition: Queue Position' });
    expect(pop).toHaveTextContent(/queue/i);
    expect(within(pop).getByRole('link', { name: /More/ })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(btn).toHaveFocus();
    expect(btn).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes on an outside click, and the icon toggles it', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <p>elsewhere</p>
        <HelpIcon term="edge" />
      </MemoryRouter>,
    );
    const btn = screen.getByRole('button', { name: 'What is Edge?' });
    await user.click(btn);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.click(screen.getByText('elsewhere'));
    expect(screen.queryByRole('dialog')).toBeNull();
    await user.click(btn);
    await user.click(btn);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps focus inside while open (Tab wraps) and More navigates to the term and closes the popover', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/x']}>
        <HelpIcon term="roi" />
        <Where />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole('button', { name: /What is Return \(ROI\)/ }));
    const more = screen.getByRole('link', { name: /More/ });
    await user.tab();
    expect(more).toHaveFocus(); // the only tab stop in the popover: focus wraps to itself
    await user.click(more);
    expect(screen.getByTestId('where')).toHaveTextContent('/terminology#roi');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does not trigger a parent click handler (card headers stay put)', async () => {
    const user = userEvent.setup();
    const parent = vi.fn();
    render(
      <MemoryRouter>
        <div onClick={parent} role="presentation">
          <HelpIcon term="edge" />
        </div>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole('button', { name: 'What is Edge?' }));
    await user.click(screen.getByRole('link', { name: /More/ }));
    expect(parent).not.toHaveBeenCalled();
  });
});

function renderPage(entry = '/terminology') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/terminology" element={<Terminology />} />
        <Route path="/more" element={<h1>More Page</h1>} />
        <Route path="*" element={<h1>Elsewhere</h1>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Terminology search UI', () => {
  let scrollTo;
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    scrollTo = vi.fn();
    window.scrollTo = scrollTo;
    clearTrail();
  });
  afterEach(() => {
    delete Element.prototype.scrollIntoView;
    vi.restoreAllMocks();
  });

  const mockLayout = (listTop = 900) => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function () {
      const top = this.classList && this.classList.contains('term-results') ? listTop : 0;
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top };
    });
  };

  it('labels the top result "Best Match" and highlights matched words', async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'queue');
    const first = container.querySelector('li.term');
    expect(first.id).toBe('queue-position');
    expect(within(first).getByText('Best Match')).toBeInTheDocument();
    expect(container.querySelectorAll('.term-best').length).toBe(1);
    expect(first.querySelector('mark')).not.toBeNull();
  });

  it('folds body-only matches under "Also mentioned in N more terms" and expands on tap', async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'P&L');
    const all = searchTerms('P&L', GLOSSARY);
    const strong = all.filter((r) => r.strong).length;
    const weak = all.length - strong;
    expect(weak).toBeGreaterThan(0);
    expect(container.querySelectorAll('li.term').length).toBe(strong);
    await user.click(screen.getByRole('button', { name: `Also mentioned in ${weak} more ${weak === 1 ? 'term' : 'terms'}` }));
    expect(container.querySelectorAll('li.term').length).toBe(strong + weak);
    expect(screen.queryByRole('button', { name: /Also mentioned/ })).toBeNull();
  });

  it('changing the query scrolls the first result under the sticky box (smooth); an unchanged query does not', async () => {
    const user = userEvent.setup();
    mockLayout(-600); // the list has scrolled off the top of the viewport
    Object.defineProperty(window, 'scrollY', { value: 3000, configurable: true });
    document.documentElement.style.setProperty('--sticky-top', '100px');
    renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'q');
    expect(scrollTo).toHaveBeenCalled();
    // list top -600 + scrollY 3000 - (sticky-top 100 + box height 0)
    expect(scrollTo.mock.calls.at(-1)[0]).toEqual({ top: 2300, behavior: 'smooth' });
    scrollTo.mockClear();
    // Tapping a result (hash change, same query) must not scroll-reset.
    await user.click(document.querySelector('li.term h3 a'));
    expect(scrollTo).not.toHaveBeenCalled();
    delete window.scrollY;
    document.documentElement.style.removeProperty('--sticky-top');
  });

  it('does not scroll when the page is already above the list (nothing to reset)', async () => {
    const user = userEvent.setup();
    mockLayout(300);
    Object.defineProperty(window, 'scrollY', { value: 0, configurable: true });
    renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'fee');
    expect(scrollTo).not.toHaveBeenCalled();
    delete window.scrollY;
  });

  it('reduced motion scrolls instantly and the fade-in is switched off in CSS', async () => {
    const user = userEvent.setup();
    mockLayout(-600);
    Object.defineProperty(window, 'scrollY', { value: 3000, configurable: true });
    window.matchMedia = vi.fn().mockImplementation((q) => ({
      matches: q.includes('reduce'),
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'q');
    expect(scrollTo.mock.calls.at(-1)[0].behavior).toBe('auto');
    delete window.scrollY;
    delete window.matchMedia;
    const css = fs.readFileSync(`${process.cwd()}/src/styles/help.css`, 'utf8');
    expect(css).toMatch(/prefers-reduced-motion: reduce\)\s*\{[^@]*\.term-results\s*\{\s*animation:\s*none/);
  });

  it('focus and caret stay in the search box while typing', async () => {
    const user = userEvent.setup();
    renderPage();
    const box = screen.getByLabelText('Filter terms');
    await user.type(box, 'qeue');
    expect(box).toHaveFocus();
    expect(box).toHaveValue('qeue');
    expect(box.selectionStart).toBe(4);
    expect(document.querySelector('li.term').id).toBe('queue-position');
  });

  it('shows a no-match message for gibberish', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText('Filter terms'), 'zzzqqq');
    expect(screen.getByText(/No terms match/)).toBeInTheDocument();
  });
});

describe('Back link (item 32)', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    window.scrollTo = vi.fn();
    clearTrail();
  });
  afterEach(() => {
    delete Element.prototype.scrollIntoView;
    clearTrail();
  });

  it('no back link when there is no recorded origin', () => {
    renderPage();
    expect(screen.queryByRole('link', { name: /Back to/ })).toBeNull();
  });

  it('shows "Back to <Page>" and returns to that route, restoring the scroll position', async () => {
    const user = userEvent.setup();
    recordTrail('/more', 640);
    expect(getTrail()).toMatchObject({ path: '/more', scrollY: 640, label: 'More' });
    renderPage('/terminology#edge');
    const back = screen.getByRole('link', { name: /Back to More/ });
    expect(back).toHaveAttribute('href', '/more');
    Object.defineProperty(document.documentElement, 'scrollHeight', { value: 5000, configurable: true });
    await user.click(back);
    expect(await screen.findByRole('heading', { name: 'More Page' })).toBeInTheDocument();
    await vi.waitFor(() => expect(window.scrollTo).toHaveBeenCalledWith(0, 640));
    delete document.documentElement.scrollHeight;
  });

  it('a tap on any in-app link to /terminology records the page and scroll it came from', async () => {
    const user = userEvent.setup();
    window.history.pushState({}, '', '/analytics');
    Object.defineProperty(window, 'scrollY', { value: 321, configurable: true });
    render(
      <MemoryRouter>
        <HelpIcon term="edge" />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole('button', { name: 'What is Edge?' }));
    await user.click(screen.getByRole('link', { name: /More/ }));
    expect(getTrail()).toMatchObject({ path: '/analytics', scrollY: 321, label: 'Analytics' });
    delete window.scrollY;
    window.history.pushState({}, '', '/');
  });

  it('never records the glossary itself as the origin', () => {
    recordTrail('/terminology#edge', 10);
    expect(getTrail()).toBeNull();
  });
});

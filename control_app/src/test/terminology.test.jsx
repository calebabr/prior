import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import fs from 'node:fs';
import path from 'node:path';
import { GLOSSARY, GROUPS, getTerm } from '../utils/glossary.js';
import { rankTerms, scoreTerm, spaced, stem } from '../utils/search.js';
import HelpIcon from '../components/HelpIcon.jsx';
import Terminology, { HIGHLIGHT_MS } from '../pages/Terminology.jsx';
import { setToken } from '../services/api.js';
import { renderApp, stubApi, STATUS } from './helpers.jsx';

const SRC = path.resolve(process.cwd(), 'src');

// Required by docs/ANALYTICS_CONTRACT.md "Glossary / help icons".
const REQUIRED = [
  'fill', 'quote', 'resting-bid', 'best-bid', 'spread', 'maker-taker', 'post-only', 'position', 'exposure', 'balance',
  'headroom', 'budget', 'open-fraction', 'settlement', 'pnl', 'realised-pnl', 'pnl-window', 'roi', 'edge', 'win-rate',
  'fill-rate', 'fill-quality', 'trade-through', 'queue-position', 'front-of-line', 're-quote', 'cell', 'price-band',
  'time-to-close', 'demo-vs-live', 'kill-switch', 'stop', 'legging', 'arbitrage', 'heartbeat',
  'drawdown', 'confidence-interval', 'ci-bars', 'sample-size', 'fee', 'contract', 'series', 'two-factor',
  // Sprint extension (owner-approved 2026-10-13)
  'calibration', 'clustered-ci', 'edge-verdict', 'expected-edge', 'rest-time', 'missed-fill', 'markets-count', 'csv-export',
];

function sourceFiles(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== 'test') out.push(...sourceFiles(p));
    } else if (/\.(jsx?|tsx?)$/.test(e.name)) out.push(p);
  }
  return out;
}

// Every term id referenced by a <HelpIcon term=...> in src/ (literal, or string literals inside
// term={...}, or values of a *_TERMS lookup object used as term={X[k]}).
function usedTerms() {
  const found = [];
  const dynamic = [];
  for (const file of sourceFiles(SRC)) {
    const text = fs.readFileSync(file, 'utf8');
    const rel = path.relative(SRC, file);
    for (const m of text.matchAll(/<HelpIcon\b[^>]*?\bterm=("([^"]*)"|'([^']*)'|\{([^}]*)\})/g)) {
      if (m[2] != null || m[3] != null) {
        found.push({ id: m[2] ?? m[3], file: rel });
      } else {
        const lits = [...m[4].matchAll(/['"`]([^'"`]+)['"`]/g)].map((x) => x[1]);
        if (lits.length) lits.forEach((id) => found.push({ id, file: rel }));
        else dynamic.push({ expr: m[4], file: rel, text });
      }
    }
  }
  // Dynamic forms we can still check statically:
  //   term={LOOKUP[k]}  -> every string value of `const LOOKUP = {...}` in that file
  //   term={x.prop}     -> every `prop: '...'` string in that file
  // Anything else fails, so authors keep terms checkable.
  for (const d of dynamic) {
    const obj = d.expr.match(/^\s*([A-Za-z_$][\w$]*)\[/);
    const prop = d.expr.match(/^\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.([A-Za-z_$][\w$]*)\s*$/);
    let values = [];
    if (obj) {
      const def = d.text.match(new RegExp(`const ${obj[1]}\\s*=\\s*\\{([^}]*)\\}`));
      if (!def) throw new Error(`Cannot find ${obj[1]} in ${d.file}`);
      values = [...def[1].matchAll(/:\s*['"]([^'"]+)['"]/g)].map((v) => v[1]);
    } else if (prop) {
      values = [...d.text.matchAll(new RegExp(`\\b${prop[1]}:\\s*['"]([^'"]+)['"]`, 'g'))].map((v) => v[1]);
    }
    if (!values.length) throw new Error(`Unchecked dynamic HelpIcon term in ${d.file}: ${d.expr}`);
    values.forEach((id) => found.push({ id, file: d.file }));
  }
  return found;
}

describe('glossary', () => {
  it('ids are unique kebab-case and every entry is complete', () => {
    const ids = GLOSSARY.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    const groups = GROUPS.map((g) => g.id);
    for (const e of GLOSSARY) {
      expect(e.id).toMatch(/^[a-z0-9]+(-[a-z0-9]+)*$/);
      expect(e.term.trim().length).toBeGreaterThan(0);
      expect(e.short.trim().length).toBeGreaterThan(0);
      expect(e.body.trim().length).toBeGreaterThan(40);
      expect(groups).toContain(e.group);
      for (const r of e.related || []) expect(getTerm(r), `${e.id} -> ${r}`).not.toBeNull();
    }
  });

  it('has every id the contract requires', () => {
    const missing = REQUIRED.filter((id) => !getTerm(id));
    expect(missing).toEqual([]);
  });

  it('every <HelpIcon term> used anywhere in src/ exists in GLOSSARY', () => {
    const used = usedTerms();
    expect(used.length).toBeGreaterThan(30);
    const bad = used.filter((u) => !getTerm(u.id)).map((u) => `${u.file}: ${u.id}`);
    expect(bad).toEqual([]);
  });

  it('every existing page has at least one help icon', () => {
    const pages = ['Overview', 'Markets', 'Activity', 'Health', 'Settings', 'Mode', 'Server', 'Logs', 'strategy/ArbitrageViews'];
    for (const p of pages) {
      const text = fs.readFileSync(path.join(SRC, 'pages', `${p}.jsx`), 'utf8');
      expect(text, p).toMatch(/<HelpIcon\b/);
    }
  });

  it('no help icon sits inside the Stop button', () => {
    const text = fs.readFileSync(path.join(SRC, 'components', 'StopButton.jsx'), 'utf8');
    expect(text).not.toMatch(/HelpIcon/);
  });
});

describe('HelpIcon', () => {
  it('is a labelled (?) button that opens a definition popover with a More link to the anchor', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <HelpIcon term="fill-rate" />
      </MemoryRouter>,
    );
    const btn = screen.getByRole('button', { name: 'What is Fill Rate?' });
    expect(btn.textContent).toBe('');
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    await user.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    const pop = screen.getByRole('dialog', { name: 'Definition: Fill Rate' });
    expect(within(pop).getByText(getTerm('fill-rate').short)).toBeInTheDocument();
    expect(within(pop).getByRole('link', { name: /More/ })).toHaveAttribute('href', '/terminology#fill-rate');
  });

  it('renders a plain More link outside a router', async () => {
    const user = userEvent.setup();
    render(<HelpIcon term="edge" />);
    await user.click(screen.getByRole('button', { name: 'What is Edge?' }));
    expect(screen.getByRole('link', { name: /More/ })).toHaveAttribute('href', '/terminology#edge');
  });
});

function renderTerms(entry = '/terminology') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/terminology" element={<Terminology />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Terminology page', () => {
  let scrolled;
  beforeEach(() => {
    scrolled = [];
    Element.prototype.scrollIntoView = vi.fn(function () {
      scrolled.push(this.id);
    });
  });
  afterEach(() => {
    delete Element.prototype.scrollIntoView;
    vi.useRealTimers();
  });

  it('renders every group and every entry with an anchor id', () => {
    const { container } = renderTerms();
    expect(screen.getByRole('heading', { level: 1, name: 'Terminology' })).toBeInTheDocument();
    for (const g of GROUPS) expect(screen.getByRole('heading', { level: 2, name: g.title })).toBeInTheDocument();
    for (const e of GLOSSARY) expect(container.querySelector(`li#${e.id}`)).not.toBeNull();
  });

  it('filter narrows the list and shows a no-match message', async () => {
    const user = userEvent.setup();
    const { container } = renderTerms();
    const box = screen.getByLabelText('Filter terms');
    await user.type(box, 'trade-through');
    expect(container.querySelector('li#trade-through')).not.toBeNull();
    expect(container.querySelector('li#heartbeat')).toBeNull();
    expect(container.querySelectorAll('li.term').length).toBeLessThan(GLOSSARY.length);
    await user.clear(box);
    await user.type(box, 'zzzqqq');
    expect(container.querySelectorAll('li.term').length).toBe(0);
    expect(screen.getByText(/No terms match/)).toBeInTheDocument();
  });

  it('a hash scrolls to and briefly highlights its entry', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = renderTerms('/terminology#ci-bars');
    const el = container.querySelector('li#ci-bars');
    expect(el).not.toBeNull();
    expect(el).toHaveClass('highlight');
    await act(async () => {
      vi.advanceTimersByTime(50);
    });
    expect(scrolled).toContain('ci-bars');
    await act(async () => {
      vi.advanceTimersByTime(HIGHLIGHT_MS + 50);
    });
    expect(el).not.toHaveClass('highlight');
  });

  it('related links move the hash to the related term', async () => {
    const user = userEvent.setup();
    const { container } = renderTerms('/terminology');
    const edge = container.querySelector('li#edge');
    await user.click(within(edge).getByRole('link', { name: 'Win Rate' }));
    expect(container.querySelector('li#win-rate')).toHaveClass('highlight');
  });
});

describe('navigation', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('More menu links to Terminology', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const user = userEvent.setup();
    stubApi({ 'GET /api/status': STATUS, 'GET /api/history': { snapshots: [] } });
    renderApp('/');
    await user.click(await screen.findByRole('link', { name: 'More' }));
    const link = within(screen.getByRole('navigation', { name: 'More pages' })).getByRole('link', { name: 'Terminology' });
    expect(link).toHaveAttribute('href', '/terminology');
    await user.click(link);
    expect(await screen.findByRole('heading', { level: 1, name: 'Terminology' })).toBeInTheDocument();
    setToken(null);
    delete Element.prototype.scrollIntoView;
  });

  it('a help icon on Overview opens its definition', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const user = userEvent.setup();
    stubApi({ 'GET /api/status': STATUS, 'GET /api/history': { snapshots: [] } });
    const { container } = renderApp('/');
    await user.click(await screen.findByRole('button', { name: 'What is Stop?' }));
    await user.click(within(await screen.findByRole('dialog', { name: 'Definition: Stop' })).getByRole('link', { name: /More/ }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Terminology' })).toBeInTheDocument();
    expect(container.querySelector('li#stop')).toHaveClass('highlight');
    setToken(null);
    delete Element.prototype.scrollIntoView;
  });
});

// ---------------------------------------------------------------- ranked search (TASKS: "Terminology search ranked by relevance")
describe('rankTerms', () => {
  const ids = (q) => rankTerms(q, GLOSSARY).map((e) => e.id);

  it('"P&L" puts the P&L entry first, above Fee (owner complaint)', () => {
    const r = ids('P&L');
    expect(r[0]).toBe('pnl');
    expect(r.indexOf('fee')).toBeGreaterThan(r.indexOf('pnl'));
    expect(r.indexOf('fee')).toBeGreaterThan(r.indexOf('realised-pnl'));
    expect(r.indexOf('fee')).toBeGreaterThan(r.indexOf('pnl-window'));
  });

  it('"pnl", "p&l", "P & L" and "profit and loss" all rank P&L first', () => {
    for (const q of ['pnl', 'p&l', 'P & L', ' P&L ', 'profit and loss', 'Profit']) expect(ids(q)[0], q).toBe('pnl');
  });

  it('aliases work: ci, 2fa, return, arb, killswitch, cash', () => {
    expect(ids('ci')[0]).toBe('confidence-interval');
    expect(ids('2FA')[0]).toBe('two-factor');
    expect(ids('return')[0]).toBe('roi');
    expect(ids('arb')[0]).toBe('arbitrage');
    expect(ids('kill switch')[0]).toBe('kill-switch');
    expect(ids('killswitch')[0]).toBe('kill-switch');
    expect(ids('stop')[0]).toBe('stop');
    expect(ids('cash')[0]).toBe('balance');
  });

  it('exact title beats starts-with beats word-boundary beats substring', () => {
    const r = ids('fill');
    expect(r[0]).toBe('fill');
    // "Fill quality"/"Fill rate" start with it, "Missed fill" has it at a word boundary.
    expect(r.indexOf('fill-quality')).toBeLessThan(r.indexOf('missed-fill'));
    expect(r.indexOf('fill-rate')).toBeLessThan(r.indexOf('missed-fill'));
    expect(scoreTerm('fill', getTerm('fill'))).toBeGreaterThan(scoreTerm('fill', getTerm('fill-rate')));
    expect(scoreTerm('fill', getTerm('fill-rate'))).toBeGreaterThan(scoreTerm('fill', getTerm('missed-fill')));
  });

  it('ties are alphabetical by term', () => {
    const r = rankTerms('fill', GLOSSARY);
    for (let i = 1; i < r.length; i++) {
      const a = scoreTerm('fill', r[i - 1]);
      const b = scoreTerm('fill', r[i]);
      expect(a).toBeGreaterThanOrEqual(b);
      if (a === b) expect(r[i - 1].term.localeCompare(r[i].term, 'en', { sensitivity: 'base' })).toBeLessThanOrEqual(0);
    }
  });

  it('property: for any query, every title/alias/id hit outranks every short/description-only hit', () => {
    const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '');
    const stemmed = (s) => spaced(s).split(' ').map(stem).join('');
    // A hit is a substring match (1-2 letter queries only at the start of a word: "ci" is not a hit inside
    // "precision"), or the same after stemming ("fills" reaches the alias "filled"). See utils/search.js.
    const nameHit = (e, q) =>
      [e.term, e.id, ...(e.aliases || [])].some((n) =>
        norm(q).length >= 3
          ? norm(n).includes(norm(q)) || stemmed(n).includes(stemmed(q))
          : ` ${spaced(n)}`.includes(` ${spaced(q)}`) || stemmed(n) === stemmed(q),
      );
    // Queries: every word of every title, alias and id, plus a few prose words.
    const queries = new Set(['fee', 'price', 'market', 'settled', 'queue', 'risk', 'money', 'win', 'p&l', 'ci', 'stop']);
    for (const e of GLOSSARY) {
      for (const n of [e.term, e.id, ...(e.aliases || [])]) {
        for (const w of String(n).split(/[^A-Za-z0-9&]+/)) if (w.length >= 2) queries.add(w);
      }
    }
    for (const q of queries) {
      const r = rankTerms(q, GLOSSARY);
      let seenProse = false;
      for (const e of r) {
        const hit = nameHit(e, q);
        if (!hit) seenProse = true;
        else expect(seenProse, `"${q}": ${e.id} (title hit) ranked below a description-only hit`).toBe(false);
      }
    }
  });

  it('empty or punctuation-only query returns nothing (page shows groups)', () => {
    expect(rankTerms('', GLOSSARY)).toEqual([]);
    expect(rankTerms('  &  ', GLOSSARY)).toEqual([]);
  });

  it('aliases are unique lowercase-insensitive per entry and non-empty', () => {
    for (const e of GLOSSARY) {
      if (!e.aliases) continue;
      expect(Array.isArray(e.aliases)).toBe(true);
      for (const a of e.aliases) expect(String(a).trim().length).toBeGreaterThan(0);
      const n = e.aliases.map((a) => a.toLowerCase());
      expect(new Set(n).size, e.id).toBe(n.length);
    }
  });
});

describe('Terminology page search', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    delete Element.prototype.scrollIntoView;
  });

  it('typing shows one flat ranked list without section headers; clearing restores groups', async () => {
    const user = userEvent.setup();
    const { container } = renderTerms();
    const box = screen.getByLabelText('Filter terms');
    await user.type(box, 'P&L');
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull();
    expect(container.querySelector('.term-jump')).toBeNull();
    const items = [...container.querySelectorAll('li.term')].map((li) => li.id);
    expect(items[0]).toBe('pnl');
    // Fee only mentions P&L in its body, so it is folded under "Also mentioned in N more terms".
    expect(items).not.toContain('fee');
    await user.click(screen.getByRole('button', { name: /Also mentioned in \d+ more terms?/ }));
    const all = [...container.querySelectorAll('li.term')].map((li) => li.id);
    expect(all.indexOf('fee')).toBeGreaterThan(items.length - 1);
    expect(screen.getByText(`${all.length} of ${GLOSSARY.length} terms`)).toBeInTheDocument();
    await user.clear(box);
    for (const g of GROUPS) expect(screen.getByRole('heading', { level: 2, name: g.title })).toBeInTheDocument();
    expect(container.querySelectorAll('li.term').length).toBe(GLOSSARY.length);
  });

  it('a hash deep-link still works while a filter is set', async () => {
    const user = userEvent.setup();
    const { container } = renderTerms();
    const box = screen.getByLabelText('Filter terms');
    await user.type(box, 'P&L');
    // Related link inside a result that is itself in the results: filter kept.
    const pnl = container.querySelector('li#pnl');
    await user.click(within(pnl).getByRole('link', { name: 'Realized P&L' }));
    expect(box).toHaveValue('P&L');
    expect(container.querySelector('li#realised-pnl')).toHaveClass('highlight');
    // Related link to a term the filter hides: filter cleared so the target exists.
    expect(scoreTerm('P&L', getTerm('win-rate'))).toBeNull();
    await user.click(within(container.querySelector('li#pnl')).getByRole('link', { name: 'Win Rate' }));
    expect(box).toHaveValue('');
    expect(container.querySelector('li#win-rate')).toHaveClass('highlight');
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
});

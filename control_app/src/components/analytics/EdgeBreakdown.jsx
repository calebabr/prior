import { useState } from 'react';
import HelpIcon from '../HelpIcon.jsx';
import Chip from '../Chip.jsx';
import FoldCard from '../FoldCard.jsx';
import WinRateCiBar, { CiScale, ciDomain } from './WinRateCiBar.jsx';
import { formatMoney, formatPct, num } from '../../utils/format.js';
import {
  MIN_N, VERDICT_WORDS, bandLabel, cents, edgeText, isNum, parseCell, rate, seriesName, shortReason, signCls, ttcLabel,
} from '../../utils/analytics.js';

// One breakdown at a time, chosen from a picker (item 16).
export const GROUPS = [
  { key: 'cell', label: 'Cell', noun: 'cell', field: 'by_cell', help: 'cell' },
  { key: 'series', label: 'Series', noun: 'series', field: 'by_series', help: 'series' },
  { key: 'band', label: 'Price Band', noun: 'price band', field: 'by_band', help: 'price-band' },
  { key: 'ttc', label: 'Time Left', noun: 'time to close', field: 'by_ttc', help: 'time-to-close' },
];

function groupTitle(tab, g) {
  if (tab === 'cell') {
    const c = parseCell(g.cell || g.key);
    return `${c.price} · ${c.ttc} · ${c.side}`;
  }
  if (tab === 'series') return seriesName(g.key);
  if (tab === 'band') return bandLabel(g.key);
  return `${ttcLabel(g.key)} to Close`;
}

// Contract verdicts (by_cell rows): too_early | on_track | below | above. Plain words in the tooltip (item 23).
export const VERDICTS = {
  too_early: { label: 'Too Early', cls: 'pill-warn', tone: 'warn' },
  on_track: { label: 'On Track', cls: 'pill-ok', tone: 'ok' },
  below: { label: 'Below', cls: 'pill-danger', tone: 'danger' },
  above: { label: 'Above', cls: 'pill-ok', tone: 'ok' },
};

export function VerdictPill({ verdict }) {
  const v = VERDICTS[verdict];
  if (!v) return null;
  return (
    <Chip tone={v.tone} title={VERDICT_WORDS[verdict]}>
      <span data-verdict={verdict} title={VERDICT_WORDS[verdict]}>{v.label}</span>
    </Chip>
  );
}

/** One folded row: name, verdict chip, P&L, markets. Tap for the full numbers and the range bar. */
export function EdgeRow({ tab, g, domain }) {
  const hasVerdict = !!VERDICTS[g.verdict];
  const thin = hasVerdict ? g.verdict === 'too_early' : !isNum(g.n) || g.n < MIN_N;
  const clustered = isNum(g.edge_cluster_ci_low) && isNum(g.edge_cluster_ci_high);
  const eLo = clustered ? g.edge_cluster_ci_low : g.edge_ci_low;
  const eHi = clustered ? g.edge_cluster_ci_high : g.edge_ci_high;
  const reason = hasVerdict ? shortReason(g) : '';
  const lo = isNum(g.cluster_ci_low) ? g.cluster_ci_low : g.win_ci_low;
  const hi = isNum(g.cluster_ci_high) ? g.cluster_ci_high : g.win_ci_high;
  return (
    <li className={`edge-row ${thin ? 'thin' : ''}`} data-testid="edge-row" data-thin={thin ? 'true' : 'false'}>
      <FoldCard
        title={groupTitle(tab, g)}
        summary={
          <>
            {hasVerdict ? <VerdictPill verdict={g.verdict} /> : thin && <Chip tone="warn" title={VERDICT_WORDS.too_early}>Too Few to Judge</Chip>}
            <span className={`edge-pnl ${signCls(g.pnl)}`}>{formatMoney(g.pnl, { sign: true })}</span>
            {isNum(g.markets) && (
              <span>
                <b className="num">{num(g.markets)}</b> Markets
              </span>
            )}
          </>
        }
      >
        {reason && <p className="edge-reason">{reason}</p>}
        <dl className="stats edge-stats">
          <div>
            <dt>Fills</dt>
            <dd>{num(g.n)}</dd>
          </div>
          <div>
            <dt>Won</dt>
            <dd>{rate(g.win_rate)}</dd>
          </div>
          <div>
            <dt>Paid</dt>
            <dd>{cents(g.avg_price)}</dd>
          </div>
          <div>
            <dt>Return</dt>
            <dd className={signCls(g.roi)}>{formatPct(g.roi, { digits: 1 })}</dd>
          </div>
          {isNum(g.expected_win_rate) && (
            <div>
              <dt>Expected</dt>
              <dd>{rate(g.expected_win_rate)}</dd>
            </div>
          )}
          {!isNum(g.expected_win_rate) && isNum(g.expected_roi) && (
            <div>
              <dt>Expected Return</dt>
              <dd>{formatPct(g.expected_roi, { digits: 1 })}</dd>
            </div>
          )}
          <div>
            <dt>Edge</dt>
            <dd className={thin ? '' : signCls(g.edge)}>{edgeText(g.edge)}</dd>
          </div>
        </dl>
        {isNum(lo) && isNum(hi) && (
          <p className="ci-summary">
            <span>
              Win Rate <b>{rate(g.win_rate)}</b>
            </span>
            <span className="sep" aria-hidden="true">·</span>
            <span>
              Likely Range <b>{rate(lo)}</b> to <b>{rate(hi)}</b>
            </span>
          </p>
        )}
        <CiScale domain={domain} />
        <WinRateCiBar
          winRate={g.win_rate}
          low={g.win_ci_low}
          high={g.win_ci_high}
          clusterLow={g.cluster_ci_low}
          clusterHigh={g.cluster_ci_high}
          expected={g.expected_win_rate}
          avgPrice={g.avg_price}
          domain={domain}
        />
        {isNum(eLo) && isNum(eHi) && (
          <p className="hint">
            Edge Range (Win Rate Minus Break-Even): {edgeText(eLo)} to {edgeText(eHi)}
            <HelpIcon term="edge" />
          </p>
        )}
      </FoldCard>
    </li>
  );
}

/** Per-cell / series / price band / time-to-close edge, maker fills only (bot only: manual trades are excluded). */
export default function EdgeBreakdown({ summary }) {
  const [tab, setTab] = useState('cell');
  const t = GROUPS.find((x) => x.key === tab);
  const groups = (summary && Array.isArray(summary[t.field]) ? summary[t.field] : []).filter(Boolean);
  const domain = ciDomain(groups);
  // Item 14: "Too Early" cells collapse into one folded line.
  const early = tab === 'cell' ? groups.filter((g) => g.verdict === 'too_early') : [];
  const shown = tab === 'cell' ? groups.filter((g) => g.verdict !== 'too_early') : groups;
  return (
    <>
      <label className="an-picker">
        <span>Group By</span>
        <select aria-label="Group edge by" value={tab} onChange={(e) => setTab(e.target.value)}>
          {GROUPS.map((x) => (
            <option key={x.key} value={x.key}>
              {x.label}
            </option>
          ))}
        </select>
        <HelpIcon term={t.help} />
      </label>
      <p className="hint" data-testid="edge-legend">
        Dot = win rate, bar = likely range
        <HelpIcon term="ci-bars" />, tick = price paid. A bar right of the tick is making money.
        {tab === 'cell' && <HelpIcon term="markets-count" />}
        {groups.some((g) => isNum(g.cluster_ci_low)) && <HelpIcon term="clustered-ci" />}
      </p>
      {groups.length ? (
        <>
          <ul className="edge-list" role="tabpanel" aria-label={`Edge by ${t.noun}`}>
            {shown.map((g) => (
              <EdgeRow key={g.key || g.cell} tab={tab} g={g} domain={domain} />
            ))}
            {early.length > 0 && (
              <li className="edge-row thin" data-testid="too-early-group">
                <FoldCard
                  key={shown.length === 0 ? 'open' : 'folded'}
                  title={`Too Early (${early.length} ${early.length === 1 ? 'Cell' : 'Cells'})`}
                  summary={<span>Not enough markets yet to judge.</span>}
                  defaultOpen={shown.length === 0}
                >
                  <ul className="edge-list inner">
                    {early.map((g) => (
                      <EdgeRow key={g.key || g.cell} tab={tab} g={g} domain={domain} />
                    ))}
                  </ul>
                </FoldCard>
              </li>
            )}
          </ul>
        </>
      ) : (
        <p className="muted small">No settled maker fills in this window.</p>
      )}
    </>
  );
}

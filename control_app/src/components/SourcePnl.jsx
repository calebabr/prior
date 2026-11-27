import { formatMoney, titleCase } from '../utils/format.js';
import { pnlClass } from './StatusCard.jsx';
import '../styles/overview.css';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

// Used until GET /api/strategies answers (or if it fails): same ids and names as the registry.
const FALLBACK = [
  { id: 'maker', source: 'bot', name: 'Bot · Maker', color: 'var(--accent)' },
  { id: 'arb', source: 'bot', name: 'Bot · Arb', color: 'var(--chart2)' },
  { id: 'you', source: 'you', name: 'You', color: 'var(--warn)' },
];

/** Sum of position values per source: {bot, you} or null when positions are unknown. */
export function riskBySource(positions) {
  if (!Array.isArray(positions)) return null;
  let bot = 0;
  let you = 0;
  for (const p of positions) {
    if (p && p.kind === 'order') continue; // resting orders are not at risk yet; rows without kind count as positions
    const v = isNum(p && p.value) ? p.value : 0;
    if ((p && p.source) === 'you') you += v;
    else bot += v;
  }
  return { bot, you };
}

/**
 * P&L by source (Y2): today's realized P&L per strategy (names and colours come from /api/strategies,
 * so a new strategy shows up with no change here) plus one split bar of $ at risk, Bot vs You.
 * Manual trades ("You") are shown here only; they never count in bot statistics.
 */
export default function SourcePnl({ pnlToday, strategies, positions, totalLabel = 'Total Today' }) {
  const list = Array.isArray(strategies) && strategies.length ? strategies : FALLBACK;
  const bySrc = pnlToday || {};
  const risk = riskBySource(positions);
  const bot = list.find((s) => s.source === 'bot') || FALLBACK[0];
  const you = list.find((s) => s.id === 'you') || FALLBACK[2];
  const total = risk ? risk.bot + risk.you : 0;
  const botPct = total > 0 ? (risk.bot / total) * 100 : 0;
  return (
    <div className="src-pnl" data-testid="source-pnl">
      <ul className="src-rows">
        {list.map((s) => (
          <li key={s.id} data-source={s.id}>
            <span className="src-swatch" style={{ background: s.color }} aria-hidden="true" />
            <span className="src-name">{titleCase(s.name)}</span>
            <span className={`src-val ${pnlClass(bySrc[s.id])}`}>{formatMoney(bySrc[s.id], { sign: true })}</span>
          </li>
        ))}
        <li className="src-total">
          <span className="src-swatch" aria-hidden="true" />
          <span className="src-name">{totalLabel}</span>
          <span className={`src-val ${pnlClass(bySrc.total)}`}>{formatMoney(bySrc.total, { sign: true })}</span>
        </li>
      </ul>
      {risk && total > 0 && (
        <div className="src-risk" data-testid="risk-split">
          <div className="src-risk-head">
            <span>At Risk</span>
            <span>{formatMoney(total)}</span>
          </div>
          <div
            className="src-bar"
            role="img"
            aria-label={`At risk: bot ${formatMoney(risk.bot)}, you ${formatMoney(risk.you)}`}
          >
            <span style={{ width: `${botPct}%`, background: bot.color }} />
            <span style={{ width: `${100 - botPct}%`, background: you.color }} />
          </div>
          <div className="src-risk-legend">
            <span><i style={{ background: bot.color }} aria-hidden="true" />Bot {formatMoney(risk.bot)}</span>
            <span><i style={{ background: you.color }} aria-hidden="true" />You {formatMoney(risk.you)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

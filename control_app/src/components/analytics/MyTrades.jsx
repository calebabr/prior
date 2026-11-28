import { useState } from 'react';
import Chip from '../Chip.jsx';
import HelpIcon from '../HelpIcon.jsx';
import { ErrorNotice, Loading } from '../Notices.jsx';
import PnlCurve from './PnlCurve.jsx';
import { analyticsApi, NOTE_MAX } from '../../services/analyticsApi.js';
import { formatMoney, formatPct, formatTime, num, titleCase } from '../../utils/format.js';
import { kalshiMarketUrl } from '../../utils/kalshi.js';
import { cents, isNum, rate, signCls } from '../../utils/analytics.js';
import CollapsibleList from '../CollapsibleList.jsx';

/** "This Month", "This Week" or "Last 90 Days" for the selected window. */
export function periodLabel(days) {
  if (days === 7) return 'This Week';
  if (days === 30) return 'This Month';
  return `Last ${days} Days`;
}

/** "Bot +$16.40 · You −$3.20 This Month": the one-line comparison (bot and manual trades stay separate). */
export function BotVsYou({ bot, you, days }) {
  if (!you && !bot) return null;
  return (
    <p className="bvy" data-testid="bot-vs-you">
      <span>
        Bot <b className={signCls(bot && bot.pnl)}>{formatMoney(bot && bot.pnl, { sign: true })}</b>
      </span>
      <span className="sep" aria-hidden="true">·</span>
      <span>
        You <b className={signCls(you && you.pnl)}>{formatMoney(you && you.pnl, { sign: true })}</b>
      </span>
      <span className="muted">{periodLabel(days)}</span>
    </p>
  );
}

function Column({ label, t, testId }) {
  return (
    <div className="mt-col" data-testid={testId}>
      <h3 className="subhead">{label}</h3>
      <dl className="stats">
        <div>
          <dt>P&amp;L<HelpIcon term="pnl" /></dt>
          <dd className={signCls(t && t.pnl)}>{formatMoney(t && t.pnl, { sign: true })}</dd>
        </div>
        <div>
          <dt>Return<HelpIcon term="roi" /></dt>
          <dd className={signCls(t && t.roi)}>{formatPct(t && t.roi, { digits: 1 })}</dd>
        </div>
        <div>
          <dt>Win Rate<HelpIcon term="win-rate" /></dt>
          <dd>{rate(t && t.win_rate, 0)}</dd>
        </div>
        <div>
          <dt>Settled Trades</dt>
          <dd>{num(t && t.settled_fills)}</dd>
        </div>
      </dl>
    </div>
  );
}

/** Journal note on one trade: one line of plain text, up to 200 characters. Saved on its own, no code needed. */
export function NoteEditor({ trade, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(trade.note || '');
  const [state, setState] = useState({ busy: false, error: null });

  async function save(e) {
    e.preventDefault();
    setState({ busy: true, error: null });
    try {
      const r = await analyticsApi.putNote(trade.fill_id, text);
      onSaved(trade.fill_id, r && typeof r.note === 'string' ? r.note : text.trim());
      setState({ busy: false, error: null });
      setEditing(false);
    } catch (error) {
      setState({ busy: false, error });
    }
  }

  if (!editing) {
    return (
      <div className="mt-note">
        {trade.note ? <p className="mt-note-text" data-testid="trade-note">{trade.note}</p> : null}
        <button
          type="button"
          className="btn small ghost"
          onClick={() => {
            setText(trade.note || '');
            setEditing(true);
          }}
          aria-label={`${trade.note ? 'Edit Note' : 'Add Note'} for ${trade.ticker}`}
        >
          {trade.note ? 'Edit Note' : 'Add Note'}
        </button>
      </div>
    );
  }
  return (
    <form className="mt-note-form" onSubmit={save}>
      <label>
        <span className="sr-only">Note for {trade.ticker}</span>
        <input
          type="text"
          value={text}
          maxLength={NOTE_MAX}
          onChange={(e) => setText(e.target.value)}
          placeholder="Why did you take this trade?"
          autoFocus
        />
      </label>
      <div className="mt-note-row">
        <span className="muted small">{text.length}/{NOTE_MAX}</span>
        <button type="button" className="btn small" onClick={() => setEditing(false)} disabled={state.busy}>
          Cancel
        </button>
        <button type="submit" className="btn small primary" disabled={state.busy}>
          {state.busy ? 'Saving…' : 'Save Note'}
        </button>
      </div>
      <ErrorNotice error={state.error} />
    </form>
  );
}

function TradeRow({ t, onSaved }) {
  const settled = t.status === 'settled';
  const chip = !settled ? { tone: 'muted', text: 'Open' } : t.result === 'win' ? { tone: 'ok', text: 'Won' } : { tone: 'danger', text: 'Lost' };
  const url = kalshiMarketUrl(t.ticker);
  return (
    <li className="mt-trade" data-testid="my-trade-row">
      <div className="mt-trade-top">
        <span className="left">
          <Chip tone={chip.tone}>{chip.text}</Chip>
          <Chip tone="accent">You</Chip>
        </span>
        <span className={`trade-pnl ${settled ? signCls(t.pnl) : 'muted'}`}>{settled ? formatMoney(t.pnl, { sign: true }) : '—'}</span>
      </div>
      <div className="mt-title">{t.title || t.ticker}</div>
      <div className="trade-meta">
        <span className="mono">{t.ticker}</span>
        <span>
          <b>{String(t.side || '').toUpperCase()}</b> @ {cents(t.price)} × {num(t.count)}
        </span>
        <span>{formatMoney(t.cost)}</span>
        <span>{formatTime(t.ts, { withDate: true })}</span>
        {url && (
          <a href={url} target="_blank" rel="noopener noreferrer">
            Open in Kalshi
          </a>
        )}
      </div>
      <NoteEditor trade={t} onSaved={onSaved} />
    </li>
  );
}

/**
 * "My Trades" scorecard and journal (Y5): the owner's own manual trades beside the bot.
 * Bot statistics elsewhere on the page never include these trades.
 */
export default function MyTrades({ days, resource }) {
  const { data, error, loading, reload, patchNote } = resource;
  const mine = data && data.totals;
  const trades = (data && data.trades) || [];
  return (
    <div data-testid="my-trades">
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading text="Loading your trades…" />}
      {data && (
        <>
          <BotVsYou bot={data.bot} you={mine} days={days} />
          <div className="mt-cols">
            <Column label="You" t={mine} testId="score-you" />
            <Column label="Bot" t={data.bot} testId="score-bot" />
          </div>
          {Array.isArray(data.pnl_curve) && data.pnl_curve.length >= 2 && (
            <>
              <h3 className="subhead">Your P&amp;L Over Time</h3>
              <PnlCurve points={data.pnl_curve} kind="all" />
            </>
          )}
          <h3 className="subhead">Journal</h3>
          {!trades.length && <p className="muted">No manual trades in this window.</p>}
          <CollapsibleList items={trades} pageKey="analytics-my-trades" limit={10} label="trades">
            {(vis) => (
              <ul className="rows mt-list" aria-label="My trades">
                {vis.map((t) => (
                  <TradeRow key={t.fill_id} t={t} onSaved={patchNote} />
                ))}
              </ul>
            )}
          </CollapsibleList>
          {isNum(data.total_trades) && data.total_trades > trades.length && (
            <p className="hint">Showing the newest {num(trades.length)} of {num(data.total_trades)}.</p>
          )}
          <p className="hint">{titleCase('manual trades')} are kept out of every bot statistic.</p>
        </>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from 'react';
import Card from './Card.jsx';
import PnlScope from './PnlScope.jsx';
import PeriodSelect from './PeriodSelect.jsx';
import { IconLock } from './Icons.jsx';
import { ErrorNotice, Loading, StaleNotice } from './Notices.jsx';
import { useDialogs } from '../context/DialogContext.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import { dateTime, formatMoney, formatPct, humanize } from '../utils/format.js';
import { pnlClass } from './StatusCard.jsx';
import '../styles/periods.css';
import CollapsibleList from './CollapsibleList.jsx';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const when = (v) => (v ? dateTime(v) : 'Start of data');

const pad2 = (n) => String(n).padStart(2, '0');
/** A Date as the local "YYYY-MM-DDTHH:mm" string a datetime-local input uses. */
const localInput = (d) =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

function Row({ label, children, testId }) {
  return (
    <div className="sum-row" data-testid={testId}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The period summary: fills, P&L and the two "return on money spent" lines. Cash flows are shown, never netted into P&L. */
export function PeriodSummary({ summary }) {
  if (!summary) return null;
  const cf = summary.cash_flows || {};
  return (
    <div className="period-summary" data-testid="period-summary">
      <dl>
        <Row label="Fills">{isNum(summary.fills) ? summary.fills : '—'}</Row>
        <Row label="Settled">
          {isNum(summary.settled) ? summary.settled : '—'}
          {isNum(summary.wins) ? ` (${summary.wins} won)` : ''}
        </Row>
        <Row label="Profit & Loss" testId="ps-pnl">
          <span className={pnlClass(summary.pnl)}>{formatMoney(summary.pnl, { sign: true })}</span>
        </Row>
        <Row label="Money Spent" testId="ps-spent">{formatMoney(summary.money_spent)}</Row>
        <Row label="Settled Cost">{formatMoney(summary.settled_cost)}</Row>
        <Row label="Return On Settled Cost" testId="ps-ret-settled">{formatPct(summary.return_on_settled_cost)}</Row>
        <Row label="Return On All Money Spent" testId="ps-ret-all">{formatPct(summary.return_on_all_cost)}</Row>
        <Row label="Still Open">{formatMoney(summary.open_at_risk)} at risk</Row>
        {(isNum(cf.deposits) || isNum(cf.withdrawals)) && (
          <>
            <Row label="Deposits">{formatMoney(cf.deposits)}</Row>
            <Row label="Withdrawals">{formatMoney(cf.withdrawals)}</Row>
            {isNum(cf.return_on_capital) && <Row label="Return On Capital" testId="ps-ret-capital">{formatPct(cf.return_on_capital)}</Row>}
          </>
        )}
      </dl>
      <PnlScope id="period" />
      {summary.pnl_basis === 'fill_date' && (
        <p className="hint" data-testid="ps-basis">
          Profit &amp; Loss is counted by fill date: a position belongs to the period it was filled in, even if it settles later.
        </p>
      )}
      {cf.equity_change_label && isNum(cf.equity_change_ex_flows) && (
        <>
          <p className="hint" data-testid="ps-equity-change">
            Equity change {formatMoney(cf.equity_change_ex_flows, { sign: true })}: {cf.equity_change_label}
          </p>
          <PnlScope id="equity" />
        </>
      )}
      <p className="hint">Deposits and withdrawals are listed for context. They are never added to or taken out of P&amp;L.</p>
    </div>
  );
}

export default function PeriodsCard() {
  const { twoFactor, notify, confirm } = useDialogs();
  const periods = useResource(() => api.getPeriods(), '/api/periods');
  const [selected, setSelected] = useState('');
  const [summary, setSummary] = useState({ data: null, error: null, loading: false });
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [kind, setKind] = useState('deposit');
  const [amount, setAmount] = useState('');
  const [flowNote, setFlowNote] = useState('');
  const [flowWhen, setFlowWhen] = useState(() => localInput(new Date()));
  const [whenTouched, setWhenTouched] = useState(false);
  const [busy, setBusy] = useState(null);
  const [formError, setFormError] = useState(null);

  const list = (periods.data && periods.data.periods) || [];
  const flows = (periods.data && periods.data.cash_flows) || [];

  // Default to the current period once the list arrives.
  useEffect(() => {
    if (selected || !list.length) return;
    const cur = list.find((p) => p.current) || list[list.length - 1];
    setSelected(cur.id);
  }, [list, selected]);

  const loadSummary = useCallback(async () => {
    if (!selected) return;
    setSummary((s) => ({ ...s, loading: true }));
    try {
      const data = await api.getPeriodSummary(selected, { source: 'all' });
      setSummary({ data, error: null, loading: false });
    } catch (error) {
      setSummary({ data: null, error, loading: false });
    }
  }, [selected]);
  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  async function run(label, title, message, action, okTitle, { onConflict } = {}) {
    setBusy(label);
    setFormError(null);
    try {
      const r = await twoFactor({ title, message, confirmLabel: label, action });
      if (r) {
        notify({ kind: 'ok', title: okTitle });
        periods.setData(r);
        await loadSummary();
        if (r.warnings && r.warnings.length) {
          notify({ kind: 'info', title: 'Check this entry', text: r.warnings.map((w) => w.message).join(' ') });
        }
        return r;
      }
    } catch (err) {
      if (err && err.status === 409 && onConflict) {
        await onConflict(err);
        return null;
      }
      setFormError(err);
      notify({ kind: 'error', title: `${label} failed`, text: err.message, details: err.details });
    } finally {
      setBusy(null);
    }
    return null;
  }

  async function markPeriod(e) {
    e.preventDefault();
    const n = name.trim();
    if (!n || busy) return;
    const r = await run(
      'Mark New Period',
      'Mark a new period?',
      <p>Starts <strong>{n}</strong> now. Figures from before this moment stay in the earlier period. This cannot be undone.</p>,
      (code) => api.createPeriod(n, note.trim() || undefined, code),
      'New Period Marked',
    );
    if (r) {
      setName('');
      setNote('');
      if (r.created) setSelected(r.created);
    }
  }

  // The date is required (the server refuses an entry without one): it defaults to now and can be changed.
  const chosen = flowWhen ? new Date(flowWhen) : null;
  const chosenValid = !!chosen && !Number.isNaN(chosen.getTime());
  const futureTime = chosenValid && chosen.getTime() > Date.now();
  const curPeriod = list.find((p) => p.current);
  const inCurrent = chosenValid && !futureTime && !!curPeriod && (!curPeriod.started_at || chosen.getTime() >= new Date(curPeriod.started_at).getTime());

  function flowMessage(amt) {
    return (
      <p>
        {kind === 'deposit' ? 'Money added to' : 'Money taken out of'} the account: <strong>{formatMoney(amt)}</strong> dated{' '}
        <strong>{dateTime(chosen.toISOString())}</strong>. It is recorded for context and never changes P&amp;L.
      </p>
    );
  }

  async function submitFlow(body, amt) {
    return run(
      'Add Cash Flow',
      `Record a ${kind}?`,
      flowMessage(amt),
      (code) => api.addCashFlow(body, code),
      'Cash Flow Recorded',
      {
        // 409: an entry with the same kind, amount and day exists. Ask explicitly, then send confirm_duplicate with a fresh code.
        onConflict: async (err) => {
          const ok = await confirm({
            title: 'Possible duplicate',
            message: (
              <>
                <p>{err.details.join(' ') || 'An entry with the same type, amount and date already exists.'}</p>
                <p>Add it anyway?</p>
              </>
            ),
            confirmLabel: 'Add anyway',
          });
          if (ok) {
            const again = await submitFlow({ ...body, confirm_duplicate: true }, amt);
            if (again) resetFlow();
          }
        },
      },
    );
  }

  function resetFlow() {
    setAmount('');
    setFlowNote('');
    setWhenTouched(false);
    setFlowWhen(localInput(new Date()));
  }

  async function addFlow(e) {
    e.preventDefault();
    const amt = Number(amount);
    if (!(amt > 0) || busy || futureTime || !chosenValid) return;
    const body = { kind, amount: amt, ts: chosen.toISOString() };
    if (flowNote.trim()) body.note = flowNote.trim();
    const r = await submitFlow(body, amt);
    if (r) resetFlow();
  }

  async function removeFlow(f) {
    await run(
      'Delete',
      'Delete this entry?',
      <p>Removes the {f.kind} of <strong>{formatMoney(f.amount)}</strong> from {when(f.ts)}.</p>,
      (code) => api.deleteCashFlow(f.id, code),
      'Cash Flow Deleted',
    );
  }

  return (
    <>
      <Card title="Periods" subtitle="Mark a new period when you change how the bot runs, so results stay comparable.">
        <StaleNotice staleAt={periods.error ? periods.staleAt : null} />
        <ErrorNotice error={periods.error} onRetry={periods.reload} />
        {periods.loading && !periods.data && <Loading />}
        {list.length > 0 && (
          <>
            <PeriodSelect periods={list} value={selected} onChange={setSelected} includeAll={false} id="periods-card-select" />
            {/* Newest first, so the current period is never behind "Show all". */}
            <CollapsibleList items={[...list].reverse()} pageKey="periods-timeline" label="periods">
              {(vis) => (
                <ul className="period-timeline" aria-label="Period timeline">
                  {vis.map((p) => (
                    <li key={p.id} data-period={p.id}>
                      <span>{p.name}{p.current ? ' (current)' : ''}</span>
                      <span className="period-when">{when(p.started_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CollapsibleList>
          </>
        )}
        <form className="period-form" onSubmit={markPeriod}>
          <label htmlFor="period-name">New Period Name</label>
          <input id="period-name" type="text" maxLength={60} value={name} onChange={(e) => setName(e.target.value)} placeholder="Period name" />
          <label htmlFor="period-note">Note (Optional)</label>
          <input id="period-note" type="text" maxLength={200} value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="submit" className="btn block" disabled={!name.trim() || !!busy}>
            {busy === 'Mark New Period' ? 'Marking…' : <><IconLock /> Mark New Period</>}
          </button>
        </form>
        {formError && formError.status === 409 && formError.detail === 'period_too_soon' && (
          <p className="hint">A period was marked less than 5 minutes ago. Wait a few minutes.</p>
        )}
      </Card>

      <Card title="Period Summary" subtitle={(list.find((p) => p.id === selected) || {}).name}>
        <ErrorNotice error={summary.error} onRetry={loadSummary} />
        {summary.loading && !summary.data && <Loading />}
        <PeriodSummary summary={summary.data} />
      </Card>

      <Card title="Deposits And Withdrawals" subtitle="Shown beside returns, never mixed into P&L.">
        {flows.length === 0 ? (
          <p className="muted">No entries yet.</p>
        ) : (
          <CollapsibleList items={flows} pageKey="cash-flows" label="entries">
            {(vis) => (
              <ul className="flow-list" aria-label="Cash flows">
                {vis.map((f) => (
                  <li key={f.id} data-flow={f.id}>
                    <span>
                      {humanize(f.kind)} <strong className="num">{formatMoney(f.amount)}</strong>
                      <span className="flow-when"> · {dateTime(f.ts)}</span>
                      {f.note ? <span className="muted"> · {f.note}</span> : null}
                    </span>
                    <button type="button" className="btn small" onClick={() => removeFlow(f)} disabled={!!busy} aria-label={`Delete ${f.kind} of ${formatMoney(f.amount)}`}>
                      Delete
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CollapsibleList>
        )}
        <form className="period-form" onSubmit={addFlow}>
          <div className="row">
            <div>
              <label htmlFor="flow-kind">Type</label>
              <select id="flow-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="deposit">Deposit</option>
                <option value="withdrawal">Withdrawal</option>
              </select>
            </div>
            <div>
              <label htmlFor="flow-amount">Amount ($)</label>
              <input id="flow-amount" type="number" inputMode="decimal" min="0.01" max="100000" step="any" value={amount} onChange={(e) => setAmount(e.target.value)} />
            </div>
          </div>
          <label htmlFor="flow-when">When it happened</label>
          <input
            id="flow-when"
            type="datetime-local"
            required
            value={flowWhen}
            max={localInput(new Date())}
            onChange={(e) => {
              setFlowWhen(e.target.value);
              setWhenTouched(true);
            }}
          />
          <p className="hint">Set the real date for past deposits</p>
          {formError && formError.status === 422 && (
            <div className="notice notice-error" role="alert" data-testid="flow-422">
              <strong>Not recorded.</strong>
              <ul className="detail-list">{formError.details.map((d, i) => <li key={i}>{d}</li>)}</ul>
            </div>
          )}
          {futureTime && <p className="notice notice-error" role="alert" data-testid="flow-when-future">That time is in the future. Pick now or an earlier time.</p>}
          {inCurrent && whenTouched && <p className="hint" data-testid="flow-when-current">This changes the capital base of the current period</p>}
          <label htmlFor="flow-note">Note (Optional)</label>
          <input id="flow-note" type="text" maxLength={200} value={flowNote} onChange={(e) => setFlowNote(e.target.value)} />
          <button type="submit" className="btn block" disabled={!(Number(amount) > 0) || !!busy || futureTime || !chosenValid}>
            {busy === 'Add Cash Flow' ? 'Saving…' : <><IconLock /> Add Entry</>}
          </button>
        </form>
      </Card>
    </>
  );
}

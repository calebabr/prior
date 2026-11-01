import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import Card from '../components/Card.jsx';
import ShadowBand from '../components/ShadowBand.jsx';
import PnlScope from '../components/PnlScope.jsx';
import PeriodSelect from '../components/PeriodSelect.jsx';
import { ErrorNotice, Loading, StaleNotice } from '../components/Notices.jsx';
import { useResource } from '../hooks/useResource.js';
import { api } from '../services/api.js';
import { ago, money, pct } from '../utils/format.js';
import '../styles/shadow.css';
import CollapsibleList from '../components/CollapsibleList.jsx';

// Shadow estimate (docs/SHADOW_CONTRACT.md): a simulation from public market data. No orders are placed,
// so every figure here is an estimate, never real P&L.
//   GET /api/shadow/summary -> {enabled, standin, status, headline, headline_zero_fee, by_series, by_cell, daily, caveats}

export const CAVEATS = [
  'Our own market impact: real orders would sit in the queue and change who gets filled. The simulation cannot see that.',
  'Hidden queue changes between checks: cancels and trades between two looks are unknown, which is why the result is a range of low, middle and high.',
  'Cash limits use an assumed capital: the simulation applies the real open-position limits to an assumed balance, not your actual cash.',
  'Outages and rejects: a real run can miss passes or have orders refused. The simulation assumes none.',
  'Small sample: with few settled episodes the range is wide and the middle figure moves a lot.',
];

const sgn = (v) => (typeof v === 'number' && v > 0 ? 'pos' : typeof v === 'number' && v < 0 ? 'neg' : '');
const NO_DATA = 'no data';
const usd = (v) => (typeof v === 'number' ? money(v, { sign: true }) : NO_DATA);
const rate = (v) => (typeof v === 'number' ? pct(v, { sign: false, digits: 1 }) : NO_DATA);
const roi = (v) => (typeof v === 'number' ? pct(v, { sign: true }) : NO_DATA);
const cellLabel = (c) => (Array.isArray(c) ? c.join(' / ') : String(c ?? ''));

function Range({ label, r, fmt, empty = false }) {
  if (!r) return null;
  if (empty) {
    return (
      <div>
        <h3 className="subhead">{label}</h3>
        <p className="muted" data-testid="range-empty">{NO_ESTIMATE_TEXT}</p>
      </div>
    );
  }
  const ci = Array.isArray(r.ci90) ? r.ci90 : null;
  return (
    <div>
      <h3 className="subhead">{label}</h3>
      <dl className="sh-range">
        <div>
          <dt>Low</dt>
          <dd className={sgn(r.lo)}>{fmt(r.lo)}</dd>
        </div>
        <div className="mid">
          <dt>Middle</dt>
          <dd className={sgn(r.mid)}>{fmt(r.mid)}</dd>
          {ci && (
            <div className="sh-ci">
              90%: {fmt(ci[0])} to {fmt(ci[1])}
            </div>
          )}
        </div>
        <div>
          <dt>High</dt>
          <dd className={sgn(r.hi)}>{fmt(r.hi)}</dd>
        </div>
      </dl>
    </div>
  );
}

const winText = (v) => (typeof v === 'number' ? pct(v, { sign: false, digits: 1 }) : '—');
export const NO_ESTIMATE_TEXT = 'No estimate yet';
export const NO_ESTIMATE_REASON = 'Nothing has settled yet, so there is no estimate. A zero here would mean no data, not a result.';
export const CEILING_FULL_TEXT = 'Shadow is not estimating: the simulated open-position ceiling is full; it clears as positions settle';
export const TOO_EARLY_REASON = 'Too early to trust: needs at least 7 close days with a fill and 30 filled episodes.';

function Honesty({ h, st, data }) {
  const ff = h.first_fill_per_market;
  const capped = st.capped_episodes;
  return (
    <>
      <dl className="stats" data-testid="honesty-stats">
        <Stat label="Break-even win rate">{winText(h.break_even_win_rate)}</Stat>
        <Stat label="Observed win rate">
          {winText(h.observed_win_rate)}
          {typeof h.n_settled_fills === 'number' ? ` (n = ${h.n_settled_fills} settled fills)` : ''}
        </Stat>
        <Stat label="Peak held capital">{typeof h.peak_held_capital === 'number' ? money(h.peak_held_capital) : '—'}</Stat>
        <Stat label="Return on held capital">{pctOrDash(h.return_on_held)}</Stat>
        <Stat label="Peak resting bids">{typeof h.peak_capital === 'number' ? money(h.peak_capital) : '—'}</Stat>
        {ff && (
          <Stat label="First fill per market">
            {pctOrDash(ff.roi)} on {money(ff.cost)} ({ff.markets ?? '—'} markets, P&amp;L {usd(ff.pnl)})
          </Stat>
        )}
        {typeof capped === 'number' && <Stat label="Capped episodes">{capped} (refused by the limits, not scored)</Stat>}
      </dl>
      {data.day_basis === 'close_day' && (
        <p className="hint" data-testid="day-basis">Days are counted by market close day.</p>
      )}
    </>
  );
}

function VariantSection({ enabled, variants, compare }) {
  const rows = Array.isArray(variants) ? variants : [];
  if (enabled !== true || !rows.length) return null;
  const cmp = Array.isArray(compare) ? compare : [];
  const cmpFor = (iv) => cmp.find((c) => c && c.interval === iv);
  const notes = cmp.filter((c) => c && c.reliability_note);
  const mid = (r) => (r && typeof r.mid === 'number' ? r.mid : null);
  return (
    <Card title="Check-interval variants (simulated)" subtitle="Same estimator, different check cadence. Compared with the 10-minute baseline.">
      <div data-testid="variant-section">
        <CollapsibleList items={rows} pageKey="shadow-variants" label="variants">
          {(vis) => (
            <div className="sh-table-wrap">
              <table className="sh-table" data-testid="variant-table">
                <thead>
                  <tr>
                    <th scope="col">Interval</th>
                    <th scope="col">Fills</th>
                    <th scope="col">Fill rate</th>
                    <th scope="col">P&amp;L mid</th>
                    <th scope="col">Diff vs 10 min</th>
                  </tr>
                </thead>
                <tbody>
                  {vis.map((v) => {
                    const c = cmpFor(v.interval);
                    const d = c && c.pnl_diff;
                    const dm = mid(d);
                    const ci = d && Array.isArray(d.ci90) ? d.ci90 : null;
                    const early = v.headline_status === 'too_early';
                    return (
                      <tr key={v.interval} className={early ? 'sh-grey' : undefined}>
                        <th scope="row">
                          {v.interval} min{v.baseline ? ' (baseline)' : ''}
                        </th>
                        <td>{typeof v.central_fills === 'number' ? v.central_fills.toFixed(1) : '—'}</td>
                        <td>{mid(v.fill_rate) != null ? rate(mid(v.fill_rate)) : '—'}</td>
                        <td>{mid(v.pnl) != null ? usd(mid(v.pnl)) : '—'}</td>
                        <td>
                          {v.baseline
                            ? '—'
                            : dm == null
                              ? '—'
                              : ci
                                ? `${usd(dm)} (90%: ${usd(ci[0])} to ${usd(ci[1])})`
                                : usd(dm)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CollapsibleList>
        {notes.map((c) => (
          <p className="hint" key={c.interval} data-testid="variant-note">
            {c.interval} min: {c.reliability_note}
          </p>
        ))}
      </div>
    </Card>
  );
}

function PnlTable({ rows, keyOf, labelOf, head, pageKey }) {
  if (!rows || !rows.length) return <p className="muted small">Nothing settled yet.</p>;
  return (
    <CollapsibleList items={rows} pageKey={pageKey} label="rows">
      {(vis) => (
    <div className="sh-table-wrap">
      <table className="sh-table">
        <thead>
          <tr>
            <th scope="col">{head}</th>
            <th scope="col">Episodes</th>
            <th scope="col">Fills</th>
            <th scope="col">Low</th>
            <th scope="col">Mid</th>
            <th scope="col">High</th>
          </tr>
        </thead>
        <tbody>
          {vis.map((r) => (
            <tr key={keyOf(r)}>
              <th scope="row">{labelOf(r)}</th>
              <td>{r.episodes}</td>
              <td>{typeof r.fills_mid === 'number' ? r.fills_mid.toFixed(1) : '—'}</td>
              <td>{usd(r.pnl && r.pnl.lo)}</td>
              <td>{usd(r.pnl && r.pnl.mid)}</td>
              <td>{usd(r.pnl && r.pnl.hi)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
      )}
    </CollapsibleList>
  );
}

const pctOrDash = (v) => (typeof v === 'number' ? pct(v, { sign: true, digits: 1 }) : '—');
const shareText = (v) => (typeof v === 'number' ? pct(v, { sign: false, digits: 0 }) : '—');
const durationText = (s) => (typeof s === 'number' ? (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`) : '—');

function Stat({ label, children }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function EventDays({ days, stats, note }) {
  const rows = Array.isArray(days) ? days.filter((d) => d && d.day) : [];
  const newest = rows.slice().reverse();
  return (
    <Card title="By event day" subtitle="Return for each day the markets settled. Simulated.">
      {note && (
        <div className="notice notice-info" role="status" data-testid="reliability-note">
          {note}
        </div>
      )}
      {stats && stats.event_days > 0 && (
        <dl className="stats" data-testid="day-stats">
          <Stat label="Days with fills">{stats.days_with_fills} of {stats.event_days}</Stat>
          <Stat label="Mean daily return">{pctOrDash(stats.mean_return_mid)}</Stat>
          <Stat label="Spread (SD)">{pctOrDash(stats.sd_return_mid)}</Stat>
          <Stat label="Worst day">{stats.worst_day ? `${stats.worst_day.day} ${pctOrDash(stats.worst_day.return_mid)}` : '—'}</Stat>
          <Stat label="Best day">{stats.best_day ? `${stats.best_day.day} ${pctOrDash(stats.best_day.return_mid)}` : '—'}</Stat>
          <Stat label="Best day share of P&L">{shareText(stats.best_day_pnl_share)}</Stat>
          <Stat label="Top 3 days share">{shareText(stats.top3_pnl_share)}</Stat>
        </dl>
      )}
      {!rows.length ? (
        <p className="muted small">No settled days in this view yet.</p>
      ) : (
        <CollapsibleList items={newest} pageKey="shadow-event-days" label="days">
          {(vis) => (
            <div className="sh-table-wrap">
              <table className="sh-table" data-testid="event-day-table">
                <thead>
                  <tr>
                    <th scope="col">Day</th>
                    <th scope="col">Episodes</th>
                    <th scope="col">Filled</th>
                    <th scope="col">Return low</th>
                    <th scope="col">Mid</th>
                    <th scope="col">High</th>
                    <th scope="col">P&amp;L mid</th>
                  </tr>
                </thead>
                <tbody>
                  {vis.map((d) => (
                    <tr key={d.day}>
                      <th scope="row">{d.day}</th>
                      <td>{d.episodes}</td>
                      <td>{d.filled_episodes}</td>
                      <td>{pctOrDash(d.return && d.return.lo)}</td>
                      <td>{pctOrDash(d.return && d.return.mid)}</td>
                      <td>{pctOrDash(d.return && d.return.hi)}</td>
                      <td>{usd(d.pnl && d.pnl.mid)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CollapsibleList>
      )}
    </Card>
  );
}

function ArbWindow({ title, w }) {
  if (!w) return null;
  return (
    <div>
      <h3 className="subhead">{title}</h3>
      <dl className="stats">
        <Stat label="Opportunities">{w.opportunities ?? '—'}</Stat>
        <Stat label="YES / NO">{w.yes ?? '—'} / {w.no ?? '—'}</Stat>
        <Stat label="Est. profit after fees">{money(w.profit, { sign: true })}</Stat>
        <Stat label="Median set cost">{money(w.median_cost)}</Stat>
        <Stat label="Would pass the risk cap">{shareText(w.gate_pass_share)}</Stat>
        <Stat label="Typical lifetime">{durationText(w.median_duration_s)}</Stat>
      </dl>
    </div>
  );
}

function ArbSection({ arb, period }) {
  if (!arb || !arb.enabled) {
    return (
      <Card title="Arbitrage opportunities (simulated)">
        <p className="muted" data-testid="arb-off">The arbitrage scan is off. Nothing is scanned and no orders are ever placed.</p>
      </Card>
    );
  }
  const r7 = arb.rolling && arb.rolling['7d'];
  const r30 = arb.rolling && arb.rolling['30d'];
  const name = period && period.name;
  const lab = (n, r) => (r && r.clipped_to_period && name ? `Last ${n} days within ${name}` : `Last ${n} days`);
  const days = (Array.isArray(arb.days) ? arb.days : []).slice().reverse();
  const last = arb.last_scan;
  return (
    <Card title="Arbitrage opportunities (simulated)" subtitle="Read-only scan of public prices. Nothing is executed.">
      <div data-testid="arb-section">
        <ArbWindow title={lab(7, r7)} w={r7} />
        <ArbWindow title={lab(30, r30)} w={r30} />
        {last && (
          <p className="hint" data-testid="arb-last-scan">
            Last scan {last.ts ? ago(last.ts) : '—'}: {last.skipped ? `skipped (${String(last.skipped).replace(/_/g, ' ')})` : `${last.opportunities ?? 0} found`}.
          </p>
        )}
        {days.length > 0 && (
          <CollapsibleList items={days} pageKey="shadow-arb-days" label="days">
            {(vis) => (
              <div className="sh-table-wrap">
                <table className="sh-table">
                  <thead>
                    <tr>
                      <th scope="col">Day</th>
                      <th scope="col">Found</th>
                      <th scope="col">YES</th>
                      <th scope="col">NO</th>
                      <th scope="col">Profit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vis.map((d) => (
                      <tr key={d.day}>
                        <th scope="row">{d.day}</th>
                        <td>{d.opportunities}</td>
                        <td>{d.yes}</td>
                        <td>{d.no}</td>
                        <td>{usd(d.profit)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CollapsibleList>
        )}
      </div>
    </Card>
  );
}

const DASH = '-';
const lUsd = (v) => (typeof v === 'number' ? money(v, { sign: true }) : DASH);
const lRoi = (v) => (typeof v === 'number' ? pct(v, { sign: true, digits: 1 }) : DASH);
const lRate = (v) => (typeof v === 'number' ? pct(v, { sign: false, digits: 1 }) : DASH);
const lMoney = (v) => (typeof v === 'number' ? money(v) : DASH);
const lDay = (t) => (typeof t === 'string' && t.length >= 10 ? t.slice(0, 10) : DASH);
const LEGACY_BANNER = 'legacy, uncapped, not comparable';

// Episodes stored before the caps were applied at creation. Never mixed into the headline.
function LegacyCard({ legacy }) {
  if (!legacy) return null;
  const h = legacy.headline || {};
  const st = legacy.status || {};
  const ff = h.first_fill_per_market;
  const mid = (r) => (r ? r.mid : null);
  return (
    <section className="card sh-legacy" data-testid="legacy-card">
      <details>
        <summary>
          <span className="card-title">Legacy episodes (before the fix)</span>
          <span className="sh-legacy-banner" data-testid="legacy-banner">{legacy.banner || LEGACY_BANNER}</span>
        </summary>
        <p className="hint">
          {legacy.episodes ?? DASH} episodes, {lDay(legacy.from)} to {lDay(legacy.to)}. Shown for reference only; they
          are not part of the headline above.
        </p>
        <dl className="stats" data-testid="legacy-stats">
          <Stat label="Estimated P&L, middle">{lUsd(mid(h.pnl))}</Stat>
          <Stat label="Estimated ROI, middle">{lRoi(mid(h.roi))}</Stat>
          <Stat label="Estimated fill rate, middle">{lRate(mid(h.fill_rate))}</Stat>
          <Stat label="Peak held capital">{lMoney(h.peak_held_capital)}</Stat>
          <Stat label="Peak resting bids">{lMoney(h.peak_capital)}</Stat>
          <Stat label="Break-even win rate">{lRate(h.break_even_win_rate)}</Stat>
          <Stat label="Observed win rate">{lRate(h.observed_win_rate)}</Stat>
          <Stat label="First fill per market">{ff ? `${lRoi(ff.roi)} on ${lMoney(ff.cost)} (${ff.markets ?? DASH} markets)` : DASH}</Stat>
          <Stat label="Settled episodes">{st.settled_episodes ?? DASH}</Stat>
          <Stat label="Days with fills">{st.days_covered ?? DASH}</Stat>
        </dl>
      </details>
    </section>
  );
}

export default function Shadow() {
  const periods = useResource(() => api.getPeriods(), '/api/periods');
  const [periodSel, setPeriodSel] = useState(null); // null = the current period
  const periodList = (periods.data && periods.data.periods) || [];
  const currentPeriod = periodList.find((p) => p.current);
  const periodId = periodSel || (currentPeriod && currentPeriod.id) || 'current';
  const { data, error, loading, staleAt, reload } = useResource(
    () => api.getShadowSummary({ period: periodId }),
    `/api/shadow/summary?period=${periodId}`,
  );
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [periodId]);
  const [zeroFee, setZeroFee] = useState(false);
  // Weather markets carry no maker fee, so the after-fee and zero-fee estimates are identical: say so instead of
  // showing a toggle that appears to do nothing.
  const sameFees = !!(data && data.headline && data.headline_zero_fee
    && JSON.stringify(data.headline.pnl) === JSON.stringify(data.headline_zero_fee.pnl)
    && JSON.stringify(data.headline.roi) === JSON.stringify(data.headline_zero_fee.roi));

  const st = (data && data.status) || {};
  const h = data && (zeroFee && data.headline_zero_fee ? data.headline_zero_fee : data.headline);
  const tooEarlyNew = !!(h && (h.headline_status === 'too_early' || (!h.headline_status && st.headline_status === 'too_early')));
  const ceilingFull = st.ceiling_full === true;
  // No settled simulated fills at all: the zeroed headline is "no data", never an estimate of $0.00.
  const noEstimate = !!(h && tooEarlyNew && !(h.n_settled_fills > 0) && !(st.settled_episodes > 0));
  const noCorrected = !!(data && ((h && h.headline_status === 'no_corrected_data') || st.headline_status === 'no_corrected_data'));
  const caveats = data && Array.isArray(data.caveats) && data.caveats.length ? data.caveats : CAVEATS;

  return (
    <div className="page">
      <div className="page-head">
        <h1>Shadow Estimate</h1>
        <button type="button" className="btn small" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>
      {periodList.length > 0 && (
        <PeriodSelect periods={periodList} value={periodId} onChange={setPeriodSel} id="shadow-period-select" />
      )}
      {data && data.period && data.period.label && (
        <p className="hint" data-testid="shadow-period-label">Showing: {data.period.label}</p>
      )}
      {data && data.period_ok === false && (
        <div className="notice notice-error" role="alert">Period markers could not be read, so these figures are empty. Try again shortly.</div>
      )}
      <StaleNotice staleAt={error ? staleAt : null} />
      <ErrorNotice error={error} onRetry={reload} />
      {loading && !data && <Loading />}

      {data && (
        <>
          <div className="notice notice-info sh-banner">
            Simulated. No orders were placed. Real fills could be lower or higher; the range runs from pessimistic to
            optimistic queue assumptions.
          </div>
          {data.standin && (
            <div className="notice notice-error" role="alert">
              Strategy not installed: shadow uses a placeholder, so these numbers say nothing about the real strategy.
            </div>
          )}

          {!data.enabled ? (
            <Card title="Shadow estimate is off">
              <p>
                Turn it on in <Link to="/server">Server</Link>: tick Shadow estimate (the shadow_enabled cloud setting)
                and save with a fresh 2FA code. Nothing is simulated while it is off. No orders are ever placed either
                way.
              </p>
            </Card>
          ) : (
            <>
              <Card title="Coverage">
                <dl className="stats">
                  <div>
                    <dt>Days with fills</dt>
                    <dd>{st.days_covered ?? '—'}</dd>
                  </div>
                  <div>
                    <dt>Settled episodes</dt>
                    <dd>{st.settled_episodes ?? '—'}</dd>
                  </div>
                  <div>
                    <dt>Estimated fills (middle)</dt>
                    <dd>{typeof st.central_fills === 'number' ? st.central_fills.toFixed(1) : '—'}</dd>
                  </div>
                  <div>
                    <dt>Last pass</dt>
                    <dd>{st.last_pass_ts ? ago(st.last_pass_ts) : '—'}</dd>
                  </div>
                </dl>
                {st.truncated && <p className="hint">Older episodes were left out of this estimate to keep it fast.</p>}
              </Card>

              {ceilingFull ? (
                <div className="notice notice-error" role="alert" data-testid="ceiling-full">
                  {CEILING_FULL_TEXT}
                  {typeof st.held_cost === 'number' && typeof st.ceiling === 'number' && (
                    <span className="hint">
                      {' '}
                      Held {money(st.held_cost)} against a ceiling of {money(st.ceiling)}.
                    </span>
                  )}
                </div>
              ) : noCorrected ? (
                <div className="notice notice-info" role="status" data-testid="no-corrected">
                  No corrected data yet for this period
                </div>
              ) : !h || (!tooEarlyNew && st.too_early) ? (
                <div className="notice notice-info" role="status">
                  Too early to estimate. The range needs more days and settled episodes before the figures mean much.
                </div>
              ) : (
                <>
                  <Card title="Estimated results (simulated)">
                    {tooEarlyNew && (
                      <div className="notice notice-info" role="status" data-testid="too-early-reason">
                        {TOO_EARLY_REASON}
                      </div>
                    )}
                    <div className={tooEarlyNew ? 'sh-grey' : undefined} data-testid="headline-block">
                    {sameFees ? (
                      <div className="notice notice-info" role="status" data-testid="fees-same">
                        No maker fee applies to these markets, so the after-fee and zero-fee estimates are the same.
                      </div>
                    ) : (
                      <div className="sh-toggle" role="group" aria-label="Fee treatment">
                        <button type="button" className="btn small" aria-pressed={!zeroFee} onClick={() => setZeroFee(false)}>
                          After fees
                        </button>
                        <button
                          type="button"
                          className="btn small"
                          aria-pressed={zeroFee}
                          onClick={() => setZeroFee(true)}
                          disabled={!data.headline_zero_fee}
                        >
                          Zero fees
                        </button>
                      </div>
                    )}
                    {noEstimate && (
                      <div className="notice notice-info" role="status" data-testid="no-estimate">
                        {NO_ESTIMATE_REASON}
                      </div>
                    )}
                    <Range
                      label={sameFees ? 'Estimated P&L' : zeroFee ? 'Estimated P&L, zero fees' : 'Estimated P&L, after maker fees'}
                      r={h.pnl}
                      fmt={usd}
                      empty={noEstimate}
                    />
                    <PnlScope id="shadow" />
                    <Range label="Estimated ROI" r={h.roi} fmt={roi} />
                    <Range label="Estimated fill rate" r={h.fill_rate} fmt={rate} empty={noEstimate} />
                    <Honesty h={h} st={st} data={data} />
                    </div>
                  </Card>

                  <Card title="Cumulative estimated P&L">
                    <ShadowBand daily={data.daily} />
                  </Card>

                  <Card title="By series">
                    <PnlTable rows={data.by_series} keyOf={(r) => r.series} labelOf={(r) => r.series} head="Series" pageKey="shadow-by-series" />
                  </Card>
                  <Card title="By cell">
                    <PnlTable rows={data.by_cell} keyOf={(r) => cellLabel(r.cell)} labelOf={(r) => cellLabel(r.cell)} head="Cell" pageKey="shadow-by-cell" />
                  </Card>
                </>
              )}
              <LegacyCard legacy={data.legacy} />
              <VariantSection enabled={data.variants_enabled} variants={data.variants} compare={data.variant_compare} />
              <EventDays days={data.by_event_day} stats={data.day_stats} note={data.reliability_note} />
              <ArbSection arb={data.arb} period={data.period} />
            </>
          )}

          <Card title="How this is estimated and what it cannot capture">
            <p>
              The app watches public order books and replays where a resting bid would have stood in the queue. Fills
              are bracketed by a pessimistic and an optimistic queue assumption. Every figure is an estimate, not a
              record of trades.
            </p>
            <ul className="sh-notes">
              {caveats.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </Card>
        </>
      )}
    </div>
  );
}

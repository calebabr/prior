import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { formatMoney } from '../../utils/format.js';
import { MIN_SLOT_PX, barLabel, isSmall, scaleOf, shortLabel, stacks, strategyColor } from '../../utils/returns.js';
import { dedupeTicks, indexTicks } from '../../utils/chart.js';
import '../../styles/returns.css';

const H = 160;
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * Stacked bars per period on a zero line: one segment per strategy (server order), positive parts above, negative
 * below, a short tick at the total. Overlaid HTML buttons carry selection (arrow keys move, Enter opens `onOpen`).
 * Direction is position plus the signed figures, never colour alone. Small-sample bars are faded with a dotted outline
 * and a marker under the axis; the partial period has a dashed outline. The plot scrolls sideways when it needs
 * more than MIN_SLOT_PX per bar; the y gutter stays fixed.
 */
export default function ReturnsBars({ periods, granularity, selected, onSelect, onOpen, scale: scaleProp }) {
  const n = periods.length;
  const scale = useMemo(() => scaleProp || scaleOf(periods), [periods, scaleProp]);
  const span = scale.hi - scale.lo || 1;
  const y = (v) => ((scale.hi - v) / span) * H;
  const zeroY = y(0);
  const keys = useMemo(() => {
    const seen = [];
    for (const p of periods) for (const k of Object.keys(p.by_strategy || {})) if (!seen.includes(k)) seen.push(k);
    return seen;
  }, [periods]);

  const scroller = useRef(null);
  const [scrolls, setScrolls] = useState(false);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    el.scrollLeft = el.scrollWidth; // newest bar at the right
    setScrolls(el.scrollWidth > el.clientWidth + 1);
  }, [n, granularity]);
  useEffect(() => {
    const el = scroller.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setScrolls(el.scrollWidth > el.clientWidth + 1));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (n === 0) return null;
  const total = periods.reduce((a, p) => a + (isNum(p.realized_pnl) ? p.realized_pnl : 0), 0);
  const best = periods.reduce((a, p) => (p.realized_pnl > a.realized_pnl ? p : a), periods[0]);
  const worst = periods.reduce((a, p) => (p.realized_pnl < a.realized_pnl ? p : a), periods[0]);
  const last = periods[n - 1];
  const summary = `${granularity} returns, ${n} periods from ${shortLabel(periods[0], granularity)} to ${shortLabel(last, granularity)}. Latest ${formatMoney(last.realized_pnl, { sign: true })}. Best ${formatMoney(best.realized_pnl, { sign: true })}, worst ${formatMoney(worst.realized_pnl, { sign: true })}, total ${formatMoney(total, { sign: true })}.`;

  const yTicks = dedupeTicks(
    [
      { label: formatMoney(scale.hi, { compact: true, digits: 0 }), pos: 0 },
      { label: '$0', pos: zeroY / H },
      { label: formatMoney(scale.lo, { compact: true, digits: 0 }), pos: 1 },
    ],
    0.16,
  );
  const xTicks = indexTicks(n, (i) => shortLabel(periods[i], granularity), (i) => (n === 1 ? 0 : (i + 0.5) / n));

  const sel = selected == null || selected >= n ? n - 1 : selected;
  function onKey(e) {
    let next = null;
    if (e.key === 'ArrowRight') next = Math.min(n - 1, sel + 1);
    else if (e.key === 'ArrowLeft') next = Math.max(0, sel - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (onOpen) onOpen(sel);
      return;
    }
    if (next == null) return;
    e.preventDefault();
    onSelect(next);
    const btns = e.currentTarget.querySelectorAll('[role="option"]');
    if (btns[next]) btns[next].focus();
  }

  return (
    <div className="rt-chart" data-testid="returns-bars">
      <div className="rt-ygutter" aria-hidden="true" style={{ height: H }}>
        {yTicks.map((t) => (
          <span key={`${t.label}@${t.pos}`} className="rt-ytick" style={{ top: `${t.pos * 100}%` }}>
            {t.label}
          </span>
        ))}
      </div>
      <div className="rt-scroll" ref={scroller} data-testid="returns-scroll">
        <div className="rt-inner" style={{ minWidth: n * MIN_SLOT_PX }}>
          <svg className="rt-svg" viewBox={`0 0 ${n} ${H}`} preserveAspectRatio="none" width="100%" height={H} role="img" aria-label={summary}>
            <line x1="0" x2={n} y1={zeroY} y2={zeroY} className="rt-zero" vectorEffect="non-scaling-stroke" />
            {periods.map((p, i) => {
              const small = isSmall(p);
              let up = 0;
              let down = 0;
              const segs = keys.map((k, ki) => {
                const v = (p.by_strategy || {})[k];
                if (!isNum(v) || v === 0) return null;
                const from = v > 0 ? up : down;
                const to = from + v;
                if (v > 0) up = to;
                else down = to;
                const top = y(Math.max(from, to));
                const bottom = y(Math.min(from, to));
                return <rect key={k} x={i + 0.14} width={0.72} y={top} height={Math.max(0.5, bottom - top)} fill={strategyColor(ki)} />;
              });
              const s = stacks(p);
              const hasBar = s.pos > 0 || s.neg < 0;
              const tot = isNum(p.realized_pnl) ? p.realized_pnl : 0;
              return (
                <g
                  key={p.start_local}
                  className={`rt-bar${small ? ' is-small' : ''}${p.partial ? ' is-partial' : ''}`}
                  data-small={small ? 'true' : 'false'}
                  data-partial={p.partial ? 'true' : 'false'}
                  data-empty={hasBar ? 'false' : 'true'}
                >
                  {hasBar ? (
                    <>
                      <g className="rt-segs">{segs}</g>
                      {(small || p.partial) && (
                        <rect
                          x={i + 0.14}
                          width={0.72}
                          y={y(s.pos)}
                          height={Math.max(1, y(s.neg) - y(s.pos))}
                          fill="none"
                          className={p.partial ? 'rt-outline-dashed' : 'rt-outline-dotted'}
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                      <line x1={i + 0.04} x2={i + 0.96} y1={y(tot)} y2={y(tot)} className="rt-total" vectorEffect="non-scaling-stroke" />
                    </>
                  ) : (
                    <line x1={i + 0.14} x2={i + 0.86} y1={zeroY} y2={zeroY} className="rt-empty-tick" vectorEffect="non-scaling-stroke" />
                  )}
                  {i === sel && <rect x={i} width={1} y={0} height={H} className="rt-sel" />}
                </g>
              );
            })}
          </svg>
          <div className="rt-marks" aria-hidden="true">
            {periods.map((p, i) =>
              isSmall(p) ? (
                <span key={p.start_local} className="rt-nmark" style={{ left: `${(i / n) * 100}%`, width: `${100 / n}%` }}>
                  <i />
                </span>
              ) : null,
            )}
          </div>
          <div className="rt-hits" role="listbox" aria-label={`Returns by ${granularity}`} aria-orientation="horizontal" onKeyDown={onKey}>
            {periods.map((p, i) => (
              <button
                key={p.start_local}
                type="button"
                role="option"
                aria-selected={i === sel}
                tabIndex={i === sel ? 0 : -1}
                className="rt-hit"
                style={{ left: `${(i / n) * 100}%`, width: `${100 / n}%` }}
                aria-label={barLabel(p, granularity)}
                data-testid="returns-bar"
                onClick={() => onSelect(i)}
              />
            ))}
          </div>
          <div className="rt-xaxis" aria-hidden="true">
            {xTicks.map((t) => (
              <span
                key={t.i}
                className="rt-xtick"
                style={t.pos <= 0.05 ? { left: `${t.pos * 100}%` } : t.pos >= 0.95 ? { right: `${(1 - t.pos) * 100}%` } : { left: `${t.pos * 100}%`, transform: 'translateX(-50%)' }}
              >
                {t.label}
              </span>
            ))}
          </div>
        </div>
      </div>
      {scrolls && (
        <p className="hint rt-scrollhint" data-testid="returns-scrollhint">
          Scroll sideways for older {granularity === 'day' ? 'days' : granularity === 'week' ? 'weeks' : 'months'}.
        </p>
      )}
    </div>
  );
}

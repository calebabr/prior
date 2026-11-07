"""Sprint 2A-3 verifier: the independent recomputation (`shadow_verifier.py`) against `shadow_model.summarize`,
the per-episode model functions, the store read path the runner and the period route use, and `run_cycle`'s caps.

Synthetic data only (KXDEMO* tickers). A mismatch beyond rounding (0.005 dollars, 0.0005 for a rate) fails with
the exact field, expected and got values.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from control_api.db.store import MemoryStore
from control_api.services import shadow as sh
from control_api.services import shadow_model as m
from control_api.tests import shadow_verifier as V

UTC = timezone.utc
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=20)
TOL_USD, TOL_RATE = 0.005 + 1e-9, 0.0005 + 1e-9

RATE_KEYS = {"roi", "fill_rate", "break_even_win_rate", "observed_win_rate", "return_on_held", "return",
             "mean_return_mid", "sd_return_mid", "return_mid", "best_day_pnl_share", "top3_pnl_share"}


@pytest.fixture(autouse=True)
def _no_fee_file(tmp_path, monkeypatch):
    monkeypatch.setenv(sh.FEE_SCHEDULE_ENV, str(tmp_path / "absent.json"))


# ================================================================ synthetic episodes
_n = [0]


def ep(*, ticker="KXDEMOA-26OCT11-T1", series=None, side="no", price=0.9, count=10.0, q0=0.0, level_min=None,
       traded=0.0, through=False, through_vol=None, join=T0, rest=timedelta(minutes=10), settle_after=timedelta(hours=12),
       result="no", fee_mode=None, capped=False, cell=("c", 1), end_reason="cancel"):
    _n[0] += 1
    level_min = q0 if level_min is None else level_min
    end = join + rest
    fill = ({"lo": 0.0, "mid": 0.0, "hi": 0.0} if capped
            else V.fill_scenarios(q0, level_min, traded, through, count, through_vol))
    close = end + settle_after
    e = {"ep_id": f"v{_n[0]:06d}", "env": "prod", "ticker": ticker, "series": series or ticker.split("-")[0],
         "side": side, "book_side": "bid", "price": price, "order_price": price, "count": float(count),
         "cost": count * price, "cell": list(cell), "close_time": close, "join_ts": join, "last_seen_ts": end,
         "end_ts": end, "end_reason": end_reason, "q0": q0, "level_min": level_min, "level_last": level_min,
         "n_obs": 1, "trades_read": True, "traded_vol": traded, "through": through,
         "through_ts": (join + rest / 2) if through else None, "through_vol": through_vol, "truncated": False,
         "fill": fill, "result": result, "settled_ts": close if result is not None else None,
         "standin": False, "capped": capped, "cap_reason": "open_cap" if capped else None, "interval": 10,
         "fee_mode": fee_mode, "ts": join}
    e["pnl"] = None if capped or result not in ("yes", "no") else {
        "zero": V.ep_pnl(e, "zero"), "maker": V.ep_pnl(e, V.ep_mode(e))}
    return e


def stacking_case():
    """The audit's F1 shape: one market re-quoted 12 times as the price falls, every level traded through."""
    out = []
    for k in range(12):
        p = round(0.90 - 0.01 * k, 2)
        out.append(ep(ticker="KXDEMOA-26OCT11-T67", price=p, count=11.0, q0=5, traded=2, through=True,
                      through_vol=None if k % 3 == 0 else 3.0 + k, join=T0 + timedelta(minutes=10 * k),
                      settle_after=timedelta(hours=10) - timedelta(minutes=10 * k), result="no"))
    out.append(ep(ticker="KXDEMOA-26OCT11-T70", price=0.85, count=12.0, q0=4, level_min=1, traded=9,
                  join=T0 + timedelta(minutes=5), result="no"))
    return out


def all_win_case(days=8, per_day=6):
    """Every filled bid wins, enough days and fills to pass the gate (ci shown)."""
    rng = random.Random(3)
    out = []
    for d in range(days):
        for j in range(per_day):
            p = round(rng.uniform(0.82, 0.96), 2)
            out.append(ep(ticker=f"KXDEMO{'AB'[j % 2]}-D{d}-T{j}", price=p, count=float(rng.choice([4, 10, 13])),
                          q0=rng.choice([0, 3, 20]), level_min=rng.choice([0, 2]), traded=rng.choice([0, 5, 30]),
                          through=rng.random() < 0.4, through_vol=rng.choice([None, 0.0, 7.0]),
                          join=T0 + timedelta(days=d, minutes=13 * j), result="no"))
    return out


def trade_through_case():
    """F4: through fills with known through volume (lo below count), with a null through volume (old episode, full
    fill in all three), and with a queue larger than everything that traded (lo = 0)."""
    return [
        ep(price=0.9, count=10, q0=50, traded=30, through=True, through_vol=25.0, result="no"),      # 5 / 7.5 / 10
        ep(price=0.88, count=10, q0=50, traded=0, through=True, through_vol=1.0, result="no",
           join=T0 + timedelta(minutes=20)),                                                         # 0 / 5 / 10
        ep(price=0.86, count=8, q0=50, traded=0, through=True, through_vol=None, result="yes",
           join=T0 + timedelta(minutes=40)),                                                          # old, 8 x3, lost
        ep(price=0.84, count=9, q0=2, traded=1, through=True, through_vol=999.0, result="no",
           join=T0 + timedelta(minutes=60)),                                                          # capped at count
    ]


def mixed_case():
    """Wins and losses over 9 close days, two series, one with the zero fee, unfilled days, ties in the held sweep,
    capped and open episodes in the input."""
    rng = random.Random(11)
    out = []
    for d in range(9):
        for j in range(5):
            tk = f"KXDEMO{'AB'[j % 2]}-M{d}-T{j % 3}"
            out.append(ep(ticker=tk, side=rng.choice(["yes", "no"]), price=round(rng.uniform(0.80, 0.97), 2),
                          count=float(rng.choice([3, 7, 15])), q0=rng.choice([0, 4, 12]),
                          level_min=rng.choice([0, 1, 4]), traded=rng.choice([0, 6, 18, 40]),
                          through=rng.random() < 0.3, through_vol=rng.choice([None, 2.0, 30.0]),
                          join=T0 + timedelta(days=d, minutes=30 * (j // 2)),   # pairs share join/end: sweep ties
                          rest=timedelta(minutes=30), result=rng.choice(["yes", "no", "no"]),
                          fee_mode="zero" if j % 2 else rng.choice([None, "maker"]),
                          cell=("c", j % 2)))
    out.append(ep(ticker="KXDEMOA-M9-T0", traded=0, q0=5, join=T0 + timedelta(days=10), result="no"))  # unfilled day
    out.append(ep(ticker="KXDEMOA-M3-T9", capped=True, result="no", join=T0 + timedelta(days=3)))
    opn = ep(ticker="KXDEMOB-M12-T1", join=NOW - timedelta(hours=1), result=None)
    opn.update(end_ts=None, end_reason=None, fill=None, trades_read=False, settled_ts=None,
               close_time=NOW + timedelta(hours=5), pnl=None)
    out.append(opn)
    return out


def gate_case():
    """7 filled close days, 3 filled episodes of 20 contracts each: 60 central contracts but only 7 x 1 episodes..."""
    out = [ep(ticker=f"KXDEMOA-G{d}-T1", count=20.0, traded=40, q0=0, join=T0 + timedelta(days=d), result="no")
           for d in range(7)]
    return out


CASES = {"stacking": stacking_case, "all_win": all_win_case, "trade_through": trade_through_case,
         "mixed": mixed_case, "gate": gate_case}


# ================================================================ comparison
def _close(a, b, rate: bool) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(float(a) - float(b)) <= (TOL_RATE if rate else TOL_USD)


def _cmp(out: list, path: str, exp, got, rate=False):
    if not _close(exp, got, rate):
        out.append(f"{path}: expected {exp!r}, got {got!r}")


def _cmp3(out, path, exp, got, rate=False):
    for s in V.SCEN:
        _cmp(out, f"{path}.{s}", exp[s], (got or {}).get(s), rate)


def diff(mine: dict, theirs: dict) -> list[str]:
    out: list[str] = []
    for hk in ("headline", "headline_zero_fee"):
        e, g = mine[hk], theirs[hk]
        _cmp3(out, f"{hk}.pnl", e["pnl"], g["pnl"])
        _cmp3(out, f"{hk}.roi", e["roi"], g["roi"], True)
        _cmp3(out, f"{hk}.fill_rate", e["fill_rate"], g["fill_rate"], True)
        for k in ("break_even_win_rate", "observed_win_rate"):
            _cmp(out, f"{hk}.{k}", e[k], g.get(k), True)
        # the model divides by the peak held capital already rounded to the cent, so a small peak moves the ratio by
        # up to (half a cent / peak) of itself; the verifier divides by the unrounded peak
        rg, rh = e["return_on_held"], g.get("return_on_held")
        slack = 0.005 / e["peak_held_capital"] * abs(rg) if rg is not None and e["peak_held_capital"] > 0 else 0.0
        if rg is None or rh is None or abs(float(rg) - float(rh)) > TOL_RATE + slack:
            _cmp(out, f"{hk}.return_on_held", rg, rh, True)
        for k in ("peak_held_capital", "peak_capital"):
            _cmp(out, f"{hk}.{k}", e[k], g.get(k))
        for k in ("n_settled_fills", "ci_reliable", "headline_status"):
            _cmp(out, f"{hk}.{k}", e[k], g.get(k))
        ff, gf = e["first_fill_per_market"], g.get("first_fill_per_market") or {}
        _cmp(out, f"{hk}.first_fill_per_market.cost", ff["cost"], gf.get("cost"))
        _cmp(out, f"{hk}.first_fill_per_market.pnl", ff["pnl"], gf.get("pnl"))
        _cmp(out, f"{hk}.first_fill_per_market.roi", ff["roi"], gf.get("roi"), True)
        _cmp(out, f"{hk}.first_fill_per_market.markets", ff["markets"], gf.get("markets"))
        if e["headline_status"] == "too_early":
            for k in ("pnl", "roi"):
                if g[k].get("ci90") is not None:
                    out.append(f"{hk}.{k}.ci90: expected null while too early, got {g[k]['ci90']!r}")
        else:
            for k in ("pnl", "roi"):
                ci = g[k].get("ci90")
                if not (isinstance(ci, list) and len(ci) == 2 and ci[0] <= ci[1]):
                    out.append(f"{hk}.{k}.ci90: expected [lo<=hi] once ok, got {ci!r}")
    st, gs = mine["status"], theirs["status"]
    for k in ("days_covered", "settled_days", "settled_episodes", "too_early", "headline_status", "capped_episodes"):
        _cmp(out, f"status.{k}", st[k], gs.get(k))
    _cmp(out, "status.central_fills", st["central_fills"], gs.get("central_fills"))
    for k in ("fee_mode", "day_basis"):
        _cmp(out, k, mine[k], theirs.get(k))
    if mine["fee_modes"] != theirs.get("fee_modes"):
        out.append(f"fee_modes: expected {mine['fee_modes']}, got {theirs.get('fee_modes')}")
    gser = {r["series"]: r for r in theirs["by_series"]}
    if set(gser) != set(mine["by_series"]):
        out.append(f"by_series keys: expected {sorted(mine['by_series'])}, got {sorted(gser)}")
    for k, r in mine["by_series"].items():
        g = gser.get(k, {})
        _cmp(out, f"by_series[{k}].episodes", r["episodes"], g.get("episodes"))
        _cmp(out, f"by_series[{k}].fills_mid", r["fills_mid"], g.get("fills_mid"))
        _cmp3(out, f"by_series[{k}].pnl", r["pnl"], g.get("pnl"))
    gcell = {tuple(r["cell"]): r for r in theirs["by_cell"]}
    for k, r in mine["by_cell"].items():
        g = gcell.get(k, {})
        _cmp(out, f"by_cell[{k}].episodes", r["episodes"], g.get("episodes"))
        _cmp3(out, f"by_cell[{k}].pnl", r["pnl"], g.get("pnl"))
    if len(mine["daily"]) != len(theirs["daily"]):
        out.append(f"daily length: expected {len(mine['daily'])}, got {len(theirs['daily'])}")
    for e, g in zip(mine["daily"], theirs["daily"]):
        _cmp(out, "daily.day", e["day"], g.get("day"))
        for k in ("cum_lo", "cum_mid", "cum_hi"):
            _cmp(out, f"daily[{e['day']}].{k}", e[k], g.get(k))
    gdays = {r["day"]: r for r in theirs.get("by_event_day") or []}
    if set(gdays) != {d["day"] for d in mine["by_event_day"]}:
        out.append(f"by_event_day days: expected {[d['day'] for d in mine['by_event_day']]}, got {sorted(gdays)}")
    for d in mine["by_event_day"]:
        g = gdays.get(d["day"], {})
        for k in ("episodes", "filled_episodes"):
            _cmp(out, f"by_event_day[{d['day']}].{k}", d[k], g.get(k))
        _cmp3(out, f"by_event_day[{d['day']}].cost", d["cost"], g.get("cost"))
        _cmp3(out, f"by_event_day[{d['day']}].pnl", d["pnl"], g.get("pnl"))
        _cmp3(out, f"by_event_day[{d['day']}].return", d["return"], g.get("return"), True)
    ds, gd = mine["day_stats"], theirs.get("day_stats") or {}
    for k in ("event_days", "days_with_fills", "filled_episodes"):
        _cmp(out, f"day_stats.{k}", ds[k], gd.get(k))
    for k in ("mean_return_mid", "sd_return_mid", "best_day_pnl_share", "top3_pnl_share"):
        _cmp(out, f"day_stats.{k}", ds[k], gd.get(k), True)
    for k in ("worst_day", "best_day"):
        e, g = ds[k], gd.get(k)
        if (e is None) != (g is None):
            out.append(f"day_stats.{k}: expected {e}, got {g}")
        elif e is not None:
            _cmp(out, f"day_stats.{k}.day", e["day"], g.get("day"))
            _cmp(out, f"day_stats.{k}.return_mid", e["return_mid"], g.get("return_mid"), True)
            _cmp(out, f"day_stats.{k}.pnl_mid", e["pnl_mid"], g.get("pnl_mid"))
    if mine["reliability_note_expected"] != (theirs.get("reliability_note") is not None):
        out.append(f"reliability_note: expected {'text' if mine['reliability_note_expected'] else 'null'}, "
                   f"got {theirs.get('reliability_note')!r}")
    return out


# ================================================================ per-episode functions
def test_fill_scenarios_match_on_a_grid():
    bad = []
    for q0 in (0, 3, 10, 50):
        for lm in (0, 2, 10, 60):
            for tv in (0, 1, 5, 12, 80):
                for through in (False, True):
                    for thv in (None, 0.0, 4.0, 100.0):
                        for cnt in (1, 7.0, 10):
                            mine = V.fill_scenarios(q0, lm, tv, through, cnt, thv)
                            got = m.fill_scenarios(q0, lm, tv, through, cnt, through_vol=thv)
                            if any(abs(mine[s] - got[s]) > 1e-9 for s in V.SCEN):
                                bad.append(((q0, lm, tv, through, thv, cnt), mine, got))
    assert bad == []


def test_maker_fee_and_episode_score_match():
    bad = []
    for n in (0, 1, 2.5, 7, 10, 11, 100, 175.7):
        for p in (0.01, 0.5, 0.8, 0.85, 0.9, 0.93, 0.99):
            for mode in ("maker", "zero"):
                if abs(V.fee(n, p, mode) - m.maker_fee(n, p, mode)) > 1e-9:
                    bad.append((n, p, mode, V.fee(n, p, mode), m.maker_fee(n, p, mode)))
    assert bad == []
    for e in mixed_case() + trade_through_case():
        if not V.is_scored(e):
            continue
        got = m.score_episode(e)
        for mode in ("zero", "maker"):
            mine = V.ep_pnl(e, mode)
            for s in V.SCEN:
                assert got[mode][s] == pytest.approx(mine[s], abs=TOL_USD), (e["ep_id"], mode, s)


# ================================================================ summarize vs the recomputation
@pytest.mark.parametrize("name", sorted(CASES))
def test_summarize_matches_the_independent_recomputation(name):
    eps = CASES[name]()
    mine = V.recompute(eps, NOW)
    got = m.summarize(eps, NOW)
    assert diff(mine, got) == []


def test_case_shapes_are_what_they_claim():
    """Guards the synthetic sets themselves (so a passing comparison means what it says)."""
    s = V.recompute(stacking_case(), NOW)
    assert s["headline"]["first_fill_per_market"]["markets"] == 2 and s["headline"]["n_settled_fills"] == 13
    w = V.recompute(all_win_case(), NOW)
    assert w["headline"]["observed_win_rate"] == 1.0 and w["status"]["too_early"] is False
    t = V.recompute(trade_through_case(), NOW)["headline"]
    assert t["pnl"]["lo"] < t["pnl"]["mid"] < t["pnl"]["hi"]
    x = V.recompute(mixed_case(), NOW)
    assert 0 < x["headline"]["observed_win_rate"] < 1 and x["fee_mode"] == "per_series"
    assert x["status"]["capped_episodes"] == 1 and x["status"]["settled_days"] > x["status"]["days_covered"]
    g = V.recompute(gate_case(), NOW)
    assert g["status"]["days_covered"] == 7 and g["status"]["central_fills"] >= 30


def test_ci_resamples_only_filled_days():
    """F3: once past the gate, adding close days whose episodes never filled leaves the interval unchanged."""
    base = all_win_case()
    extra = [ep(ticker=f"KXDEMOA-Z{d}-T1", traded=0, q0=9, join=T0 + timedelta(days=30 + d), result="no")
             for d in range(4)]
    a, b = m.summarize(base, NOW + timedelta(days=40)), m.summarize(base + extra, NOW + timedelta(days=40))
    assert a["headline"]["headline_status"] == "ok"
    assert a["headline"]["pnl"]["ci90"] == b["headline"]["pnl"]["ci90"]
    assert a["headline"]["roi"]["ci90"] == b["headline"]["roi"]["ci90"]


def test_capped_episodes_change_nothing_but_their_count():
    base = mixed_case()
    caps = [ep(ticker=f"KXDEMOA-M{d}-T7", capped=True, result="no", join=T0 + timedelta(days=d)) for d in range(5)]
    a, b = m.summarize(base, NOW), m.summarize(base + caps, NOW)
    assert b["status"]["capped_episodes"] == a["status"]["capped_episodes"] + 5
    a["status"].pop("capped_episodes"), b["status"].pop("capped_episodes")
    assert a["headline"] == b["headline"] and a["status"] == b["status"] and a["by_event_day"] == b["by_event_day"]


# ================================================================ the store read path (runner + period route)
async def _through_store(eps):
    st = MemoryStore()
    await st.shadow_upsert_episodes(eps)
    settled = await st.shadow_settled(NOW - timedelta(days=sh.SHADOW_SUMMARY_DAYS), fields=sh.SUMMARY_FIELDS,
                                      limit=sh.SHADOW_SUMMARY_MAX_ROWS, scored_only=True)
    open_eps = await st.shadow_open_episodes(fields=sh.SUMMARY_FIELDS, limit=sh.SHADOW_SUMMARY_MAX_OPEN)
    return list(settled) + list(open_eps)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_store_projection_feeds_summarize_the_same_numbers(name):
    """The runner's cached summary and the period route read the episodes back with SUMMARY_FIELDS: the numbers
    from that projection must equal the recomputation on the full raw episodes."""
    eps = CASES[name]()
    got = m.summarize(await _through_store(eps), NOW)
    assert diff(V.recompute(eps, NOW), got) == []


# ================================================================ run_cycle caps (F1) and stored fills
@dataclass(frozen=True)
class Quote:
    ticker: str
    side: str
    price: float
    count: float
    book_side: str
    order_price: float
    cell: tuple

    @property
    def cost(self) -> float:
        return self.count * self.price


class Strat:
    def __init__(self, series_cap=None):
        self.prices: dict[str, float] = {}
        self.series_cap = series_cap          # passed to the cycle as series_share_limit (the model reads a private file, not the strategy)

    def desired_quotes(self, markets, books, now, max_order_dollars, budget):
        out, left = [], float(budget)
        for mk in markets:
            p = self.prices.get(mk["ticker"])
            if p is None:
                continue
            n = math.floor(min(max_order_dollars, left) / p + 1e-9)
            if n >= 1:
                out.append(Quote(mk["ticker"], "yes", p, float(n), "bid", p, ("c", 1)))
                left -= n * p
        return out


class MD:
    def __init__(self, tickers, close):
        self.mkts = {s: [{"ticker": t, "close_time": close.isoformat()} for t in ts] for s, ts in tickers.items()}
        self.books, self.rows, self.results = {}, {}, {}

    def markets(self, series, status, min_close_ts=None):
        return list(self.mkts.get(series, [])), False

    def orderbook(self, ticker, depth=None):
        return self.books.get(ticker, {})

    def market(self, ticker):
        return self.results.get(ticker, {"ticker": ticker, "result": ""})

    def trades(self, ticker, min_ts, max_ts):
        return [t for t in self.rows.get(ticker, [])
                if min_ts <= datetime.fromisoformat(t["created_time"]).timestamp() <= max_ts], False


def _trade(ts, yes_price, count, taker="no"):
    return {"trade_id": f"t{ts.timestamp()}-{yes_price}-{count}", "created_time": ts.isoformat(),
            "yes_price_dollars": f"{yes_price:.4f}", "count_fp": str(count), "taker_side": taker}


def _simulate(seed):
    """16 ten-minute cycles over six markets (two series); the markets close after 12 cycles and settle on the
    next ones. Returns the final episodes, the per-cycle snapshots, the limits and the last cycle time."""
    rng = random.Random(seed)
    start = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    close = start + timedelta(hours=2)
    tickers = {"KXDEMOA": [f"KXDEMOA-26OCT19-T{i}" for i in range(3)],
               "KXDEMOB": [f"KXDEMOB-26OCT19-T{i}" for i in range(3)]}
    cfg = dict(capital=rng.choice([20.0, 60.0, 150.0, 400.0]), max_open_frac=rng.choice([0.2, 0.5, 0.9]),
               run_budget=rng.choice([10.0, 40.0, 200.0]), max_order=rng.choice([5.0, 15.0, 50.0]),
               series_cap=rng.choice([None, 0.35, 0.6]))
    st, md = Strat(cfg["series_cap"]), MD(tickers, close)
    eps: dict[str, dict] = {}
    snaps: list[tuple[datetime, dict]] = []
    now = start
    for k in range(16):
        now = start + timedelta(minutes=10 * k)
        if now < close:
            for ts in tickers.values():
                for t in ts:
                    p = round(rng.uniform(0.80, 0.97), 2)
                    st.prices[t] = p
                    md.books[t] = {"yes_dollars": [[f"{p:.4f}", str(rng.choice([0, 3, 40]))]]}
                    r = rng.random()
                    if r < 0.4:
                        md.rows.setdefault(t, []).append(_trade(now + timedelta(minutes=3), p - 0.02,
                                                                rng.choice([1, 50, 500])))
                    elif r < 0.7:
                        md.rows.setdefault(t, []).append(_trade(now + timedelta(minutes=4), p, rng.choice([5, 100])))
        elif not md.results:
            for ts in tickers.values():
                for t in ts:
                    md.results[t] = {"ticker": t, "result": rng.choice(["yes", "no"]), "status": "settled"}
        cur = list(eps.values())
        inp = sh.CycleInput(
            series=list(tickers), run_budget=cfg["run_budget"], max_order=cfg["max_order"], now=now,
            open_eps=[e for e in cur if e.get("end_ts") is None],
            ended_unread=sorted([e for e in cur if e.get("end_ts") is not None and e.get("trades_read") is not True],
                                key=lambda e: e["end_ts"]),
            unsettled=[e for e in cur if e.get("result") is None and e.get("close_time") and e["close_time"] <= now],
            held=[e for e in cur if e.get("end_ts") is not None and e.get("result") is None],
            capital=cfg["capital"], max_open_frac=cfg["max_open_frac"], fee_schedule=None,
            series_share_limit=cfg["series_cap"], market_share_limit=None)
        for u in sh.run_cycle(md, st, inp).upserts:
            eps[u["ep_id"]] = {**eps.get(u["ep_id"], {}), **u}
        snaps.append((now, {i: dict(e) for i, e in eps.items()}))
    SIM_TRADES[seed] = md.rows
    return eps, snaps, cfg, now


SIM_TRADES: dict[int, dict] = {}


SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
def test_run_cycle_episodes_are_consistent_and_summarize_matches(seed):
    """Capped episodes are well formed and never scored; every stored fill is what the raw queue fields give (F4);
    the stored per-episode score matches; summarize over the cycle's episodes equals the recomputation."""
    eps, snaps, cfg, now = _simulate(seed)
    for t, state in snaps:
        bad = [v for v in V.cap_violations(state.values(), capital=cfg["capital"],
                                           max_open_frac=cfg["max_open_frac"], run_budget=cfg["run_budget"],
                                           series=["KXDEMOA", "KXDEMOB"], series_cap=cfg["series_cap"])
               if v.startswith("capped")]
        assert bad == [], (t, bad[:3])
    bad = []
    for e in eps.values():
        if e.get("capped") or e.get("fill") is None or e.get("q0") is None:
            continue
        exp = V.fill_scenarios(e["q0"], e["level_min"] if e.get("level_min") is not None else e["q0"],
                               e.get("traded_vol") or 0.0, bool(e.get("through")), e["count"], e.get("through_vol"))
        if any(abs(exp[s] - e["fill"][s]) > 1e-9 for s in V.SCEN):
            bad.append((e["ep_id"], exp, e["fill"]))
    assert bad == []
    scored = [e for e in eps.values() if V.is_scored(e)]
    assert scored, "the simulation should settle something"
    # independent trade volumes (F4 through volume, F8 window) from the simulated public trades
    for e in eps.values():
        if e.get("capped") or not e.get("trades_read") or e.get("fill") is None or e.get("end_ts") is None:
            continue
        rows = []
        for t in SIM_TRADES[seed].get(e["ticker"], []):
            ts = datetime.fromisoformat(t["created_time"])
            inside = e["join_ts"] <= ts < e["end_ts"] if e["end_reason"] == "cancel" else e["join_ts"] <= ts <= e["end_ts"]
            if inside:
                rows.append(t)
        p = e["price"]
        through_vol = sum(float(t["count_fp"]) for t in rows if float(t["yes_price_dollars"]) < p - 1e-9)
        at_price = sum(float(t["count_fp"]) for t in rows
                       if abs(float(t["yes_price_dollars"]) - p) < 1e-9 and t["taker_side"] == "no")
        assert e["through_vol"] == pytest.approx(through_vol), e["ep_id"]
        assert e["traded_vol"] == pytest.approx(at_price), e["ep_id"]
        assert bool(e["through"]) == (through_vol > 0), e["ep_id"]
    for e in scored:
        assert e.get("pnl") is not None
        for s in V.SCEN:
            assert e["pnl"]["maker"][s] == pytest.approx(V.ep_pnl(e, V.ep_mode(e))[s], abs=TOL_USD)
    assert diff(V.recompute(eps.values(), now), m.summarize(list(eps.values()), now)) == []


def _limit_violations(seed):
    eps, snaps, cfg, now = _simulate(seed)
    ceiling = cfg["capital"] * cfg["max_open_frac"]
    return V.funding_violations(snaps, eps, ceiling=ceiling), cfg


FUNDING_OVERSHOOT_SEEDS: set[int] = set()    # verifier finding V1 fixed (was {7, 13, 36, 39})


@pytest.mark.parametrize("seed", SEEDS)
def test_run_cycle_never_commits_more_than_the_ceiling(request, seed):
    """F1 economic invariant with hindsight: held positions (final mid fills) and held + resting bids never exceed
    max_open_frac x shadow_capital at any cycle."""
    if seed in FUNDING_OVERSHOOT_SEEDS:
        request.applymarker(pytest.mark.xfail(strict=True, reason="verifier finding V1: a continuing bid the caps "
                                              "cancel is not counted as held when the new bids are sized"))
    v, cfg = _limit_violations(seed)
    assert v == [], (cfg, v[:4])


@pytest.mark.parametrize("seed", SEEDS)
def test_run_cycle_never_commits_more_than_the_series_cap(seed):
    """The series limit sizes each bid to the room left: held + resting per series never exceed limit x ceiling."""
    eps, snaps, cfg, _now = _simulate(seed)
    v = V.series_funding_violations(snaps, eps, ceiling=cfg["capital"] * cfg["max_open_frac"],
                                    series_cap=cfg["series_cap"])
    assert v == [], (cfg, v[:4])


def test_v1_minimal_reproduction_capped_cancel_frees_room_it_already_used():
    """V1, two series, one market each, ceiling 30. Cycle 0 rests 15 + 15 and both levels trade through (filled in
    full). Cycle 1: A re-quotes at a new price (ended, read: held 15), the per-series share drops to 7.5, so B's
    continuing bid (15, already filled) is cancelled by the caps AFTER the held position was measured; the room it
    used is handed to new bids. True exposure: held 30 + resting 14.9 > 30, and held 44.9 > 30 one cycle later."""
    start = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    ta, tb = "KXDEMOA-26OCT19-T1", "KXDEMOB-26OCT19-T1"
    md, st = MD({"KXDEMOA": [ta], "KXDEMOB": [tb]}, start + timedelta(days=1)), Strat()
    eps: dict[str, dict] = {}
    snaps = []
    for k, (pa, pb) in enumerate([(0.75, 0.75), (0.74, 0.75), (0.74, 0.75)]):
        now = start + timedelta(minutes=10 * k)
        st.prices.update({ta: pa, tb: pb})
        for t, p in ((ta, pa), (tb, pb)):
            md.books[t] = {"yes_dollars": [[f"{p:.4f}", "5"]]}
            md.rows.setdefault(t, []).append(_trade(now + timedelta(minutes=3), p - 0.02, 1000))
        cur = list(eps.values())
        inp = sh.CycleInput(series=["KXDEMOA", "KXDEMOB"], run_budget=40.0, max_order=50.0, now=now,
                            open_eps=[e for e in cur if e.get("end_ts") is None],
                            ended_unread=[e for e in cur if e.get("end_ts") is not None and not e.get("trades_read")],
                            held=[e for e in cur if e.get("end_ts") is not None and e.get("result") is None],
                            capital=150.0, max_open_frac=0.2, fee_schedule=None, series_share_limit=None, market_share_limit=None)
        for u in sh.run_cycle(md, st, inp).upserts:
            eps[u["ep_id"]] = {**eps.get(u["ep_id"], {}), **u}
        snaps.append((now, {i: dict(e) for i, e in eps.items()}))
    v = V.funding_violations(snaps, eps, ceiling=30.0)
    assert v == []


def test_v2_headline_gate_and_reliability_note_agree():
    """V2: the too-early gate counts central filled CONTRACTS (>= 30) while the reliability note counts filled
    EPISODES (>= 30). Seven filled days with one 20-contract fill each give headline_status "ok" (a 90% interval is
    shown) while the same response still says the figures are too thin."""
    got = m.summarize(gate_case(), NOW)
    assert (got["headline"]["headline_status"] == "ok") == (got["reliability_note"] is None)

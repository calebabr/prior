"""Update Local Data: copy everything useful for analysis from the store (MongoDB Atlas) into a git-ignored folder.

    python -m control_api.tools.pull_data [--days 90] [--out local_data] [--seed-demo] [--shadow]

Reads MONGODB_URI / MONGODB_DB from the environment, else from `.env.local` at the repo root
(KEY=VALUE lines). Use a READ-ONLY Atlas database user (built-in role `read` on the bot database)
for this URI. This tool only calls list/get methods; it never writes to the store. The users and
revoked_tokens collections are never read. Audit rows are reduced to ts/user/action/ok/details (no IP or
user agent), and every exported value goes through `scrub()` (secret-looking keys dropped, secret-looking
strings redacted) as a second line of defence.
`--seed-demo` uses an in-memory store filled with fake data, so no Atlas is needed.

Output (all datetimes ISO-8601 UTC; read DATA_DICTIONARY.md first):
  SUMMARY.md, DATA_DICTIONARY.md, state.json, journal.csv, fills.csv, fill_pnl.csv, settlements.csv,
  account_snapshots.csv, heartbeats.csv, quote_episodes.csv, arb_opportunities.csv, monitor_summaries.json,
  config_versions.json, audit.csv, and analytics_summary.json when control_api.services.analytics exists.
  With --shadow also: shadow_episodes.csv and shadow_passes.csv (scrubbed Shadow-mode rows; read-only).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from control_api.db import schemas as S
from control_api.services import fillquality as fq
from control_api.services import pnl as pnlsvc
from control_api.services import queuepos
from control_api.services.logbuffer import redact

REPO_ROOT = Path(__file__).resolve().parents[2]
# Keys dropped wherever they appear (exact, lower-cased) ...
SECRET_KEYS = {"password_hash", "password", "totp_secret", "totp_secret_enc", "totp", "api_key", "private_key",
               "token", "jti", "ip", "ua", "user_agent", "authorization", "cookie", "x-2fa-code",
               "sessions_valid_after", "last_totp_step", "failed_logins", "locked_until"} | set(S.FORBIDDEN_KEYS)
# ... and any key containing one of these.
SECRET_PARTS = ("password", "passwd", "secret", "totp", "private_key", "privatekey", "api_key", "apikey", "jwt",
                "mongodb_uri", "token", "bearer", "credential", "_ip", "ip_addr")
AUDIT_KEYS = ["ts", "user", "action", "ok", "details"]
# --shadow: the only Shadow episode fields exported (ids, the hypothetical bid, caps, timing, fill and settlement state)
SHADOW_EPISODE_KEYS = ["ep_id", "ticker", "series", "side", "price", "count", "cost", "methodology", "capped",
                       "cap_reason", "join_ts", "end_ts", "end_reason", "close_time", "trades_read", "fill",
                       "result", "settled_ts", "settle_checked_ts", "interval", "fee_mode", "last_seen_ts", "through",
                       "through_ts", "through_vol", "traded_vol", "truncated", "n_obs", "pnl"]
SHADOW_EXPORT_EPISODES = 20_000            # newest episodes exported (Atlas keeps 30 days, at most ~1500 new a day)
SHADOW_EXPORT_PASSES = 5_000               # newest pass docs exported
BIG = 10_000_000
FILL_LOOKBACK = timedelta(days=7)          # = analytics.LOOKBACK - window
# Strings that might hold a secret get the full logbuffer.redact() pass; the rest skip it (speed on big pulls).
_MAYBE_SECRET = re.compile(r"(?i)begin|mongodb|eyj|bearer|password|secret|token|api[_-]?key|2fa|totp")
_ENV_SECRETS: list[str] | None = None


def _redact(text: str) -> str:
    global _ENV_SECRETS
    if _ENV_SECRETS is None:
        from control_api.services import logbuffer
        _ENV_SECRETS = [v for v in ((os.environ.get(n) or "").strip() for n in logbuffer._SECRET_ENVS) if len(v) >= 6]
    if _MAYBE_SECRET.search(text) or any(v in text for v in _ENV_SECRETS):
        return redact(text)
    return text

CSV_TABLES = ("journal", "fills", "fill_pnl", "settlements", "account_snapshots", "heartbeats", "quote_episodes",
              "arb_opportunities", "audit")
# Append-only history (local_data/history/<name>.csv): natural key per table. Rows are upserted (newest pull wins,
# e.g. an open fill becomes settled) and never removed, so history survives data ageing out of Atlas (TTL 45/180 d).
HISTORY_KEYS = {
    "fills": ("fill_id",), "fill_pnl": ("fill_id",), "settlements": ("ticker",), "journal": ("row_hash",),
    "quote_episodes": ("order_id",), "arb_opportunities": ("sig", "ts"), "account_snapshots": ("ts",),
    "heartbeats": ("ts",),
}
ARCHIVE_KEEP = 90                          # dated archive folders kept
_ARCHIVE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Nested dicts flattened one level into "parent.child" columns (everything else nested -> JSON string).
FLATTEN = {"fills": ("fill_quality",), "quote_episodes": ("last_obs",)}


def load_env_local(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _iso(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def secret_key(k) -> bool:
    k = str(k).lower()
    return k in SECRET_KEYS or any(p in k for p in SECRET_PARTS)


def clean(obj):
    """JSON-safe, scrubbed copy: ISO-UTC datetimes; Mongo _id and secret-looking keys dropped; strings redacted
    (PEM keys, Mongo URIs, JWTs, bearer tokens, key=value secrets, the process's own secret env values)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items() if k != "_id" and not secret_key(k)}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    v = _iso(obj)
    if isinstance(v, str):
        return _redact(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return _redact(str(v))


scrub = clean


def flatten(row: dict, keys: tuple[str, ...] = ()) -> dict:
    out = {}
    for k, v in row.items():
        if k in keys and isinstance(v, dict):
            for ck, cv in v.items():
                out[f"{k}.{ck}"] = cv
        else:
            out[k] = v
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None, flat: tuple[str, ...] = ()) -> list[str]:
    """Write rows (scrubbed) and return the header actually written."""
    rows = [flatten(clean(r), flat) for r in rows]
    if fields is None:
        fields = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
    fields = fields or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return fields


def _to_dt(v):
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime) and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v if isinstance(v, datetime) else None


def _ts(d: dict):
    return _to_dt(d.get("ts")) if isinstance(d, dict) else None


def _series(row: dict) -> str:
    for k in ("series", "series_ticker"):
        if row.get(k):
            return str(row[k])
    t = str(row.get("ticker") or row.get("market_ticker") or "(no ticker)")
    return t.split("-")[0]


def _r(v, nd=2):
    return None if v is None else round(v, nd)


async def collect_shadow(store) -> dict:
    """Read-only (shadow_recent, shadow_recent_passes): the newest Shadow episodes (any state) and pass docs."""
    return {"shadow_episodes": list(await store.shadow_recent(SHADOW_EXPORT_EPISODES, None) or []),
            "shadow_passes": list(await store.shadow_recent_passes(SHADOW_EXPORT_PASSES) or [])}


async def collect(store, days: int, shadow: bool = False) -> dict:
    """Read-only: only get_*/list_*/last_* calls (plus shadow_recent* with `shadow`). Never touches users or
    revoked tokens."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    lister = getattr(store, "list_monitor_summaries", None)
    if lister is not None:
        monitors = await lister(since=since, limit=10_000)
    else:                                                    # older store without the list method
        last = await store.last_monitor_summary()
        monitors = [last] if last and (_ts(last) or since) >= since else []
    data = {
        "state": await store.get_state(),
        "cloud_settings": await store.get_cloud_settings(),
        "config": await store.get_config(),
        "last_heartbeat": await store.last_heartbeat(),
        "last_monitor_summary": await store.last_monitor_summary(),
        "monitor_summaries": monitors,
        "journal": await store.list_journal(since=since, limit=BIG),
        "account_snapshots": await store.list_account_snapshots(since=since, limit=BIG),
        # analytics needs fills from up to 7 days before the window (they settle inside it); exports use the window
        "fills_lookback": await store.list_fills(since=since - FILL_LOOKBACK, limit=BIG),
        "settlements": await store.list_settlements(since=since, limit=BIG),
        "quote_episodes": await store.list_quote_episodes(since=since, limit=BIG),
        "arb_opportunities": await store.list_arb_opportunities(since=since, limit=BIG),
        "audit": [a for a in await store.list_audit(limit=BIG) if (_ts(a) or since) >= since],
        "config_versions": await store.list_config_versions(limit=10_000),
        "heartbeats": await store.list_heartbeats(since=since, limit=BIG),
    }
    for name in ("periods", "cash_flows"):                  # all history (tiny, permanent); absent on an older store
        lister = getattr(store, f"list_{name}", None)
        data[name] = list(await lister()) if lister is not None else []
    data["fills"] = [f for f in data["fills_lookback"] if (_ts(f) or since) >= since]
    if shadow:
        data.update(await collect_shadow(store))
    return data


# ---------------------------------------------------------------- derived: per-fill realised P&L
def fill_pnl_rows(fills: list[dict], settlements: list[dict]) -> list[dict]:
    res = {}
    for s in settlements:
        r = s.get("market_result") or s.get("result")
        if s.get("ticker") and r in ("yes", "no"):
            res[s["ticker"]] = (r, s.get("ts"))
    out = []
    for f in sorted(fills, key=lambda d: _ts(d) or datetime.min.replace(tzinfo=timezone.utc)):
        side = pnlsvc.fill_side(f)
        price = pnlsvc.fill_price(f, side) if side else None
        count = pnlsvc.fill_count(f)
        fee = pnlsvc._f(f.get("fee_cost")) or 0.0
        ticker = f.get("ticker") or f.get("market_ticker")
        hit = res.get(ticker)
        pnl = pnlsvc.fill_pnl(f, hit[0]) if hit else None
        fqd = f.get(fq.FIELD) if isinstance(f.get(fq.FIELD), dict) else {}
        out.append({
            "fill_id": f.get("fill_id"), "ts": f.get("ts"), "env": f.get("env"), "ticker": ticker,
            "series": _series(f), "side": side, "kind": "taker" if f.get("is_taker") else "maker",
            "price": _r(price, 4), "count": count, "fee": _r(fee, 4),
            "cost": _r(price * count + fee, 4) if price is not None else None,
            "status": "settled" if hit else "open", "market_result": hit[0] if hit else None,
            "result": (None if not hit or side is None else "win" if hit[0] == side else "loss"),
            "settled_utc": hit[1] if hit else None, "pnl": _r(pnl, 4),
            "trade_through": fqd.get("trade_through"),
        })
    return out


# ---------------------------------------------------------------- SUMMARY.md
def _pnl_block(rows: list[dict], kind: str) -> list[str]:
    rs = [r for r in rows if r["kind"] == kind and r["status"] == "settled" and r["pnl"] is not None]
    if not rs:
        return [f"- {kind}: no settled fills in the window"]
    pnl = sum(r["pnl"] for r in rs)
    cost = sum(r["cost"] or 0 for r in rs)
    wins = sum(1 for r in rs if r["result"] == "win")
    avgp = sum(r["price"] or 0 for r in rs) / len(rs)
    return [f"- {kind}: {len(rs)} settled fills, {sum(r['count'] for r in rs):g} contracts, cost ${cost:.2f}, "
            f"realised P&L ${pnl:.2f}, ROI {pnl / cost:.2%}" if cost else f"- {kind}: P&L ${pnl:.2f}",
            f"  - win rate {wins / len(rs):.1%} vs average price {avgp:.3f} (edge {wins / len(rs) - avgp:+.3f}; "
            "small samples are noisy, see analytics_summary.json for CIs)"]


def summary_md(data: dict, days: int, source: str, pulled: datetime, pnl_rows: list[dict],
               analytics: dict | None, analytics_note: str) -> str:
    st = data["state"] or {}
    L = ["# Bot data summary", "", f"- Pulled: {pulled.isoformat()} (source: {source})",
         f"- Window: last {days} days (by each row's `ts`)",
         f"- Bot state: desired={st.get('desired')} mode={st.get('mode')} "
         f"arb_desired={st.get('arb_desired')} arb_mode={st.get('arb_mode')}",
         "- Read `DATA_DICTIONARY.md` for what every file and column means."]
    snaps = sorted(data["account_snapshots"], key=lambda d: _ts(d) or pulled)
    if snaps:
        s = clean(snaps[-1])
        L.append("- Latest account snapshot (cash/exposure): " + ", ".join(
            f"{k}={v}" for k, v in s.items() if not isinstance(v, (dict, list))))
    hb = clean(data["last_heartbeat"]) if data["last_heartbeat"] else None
    L.append(f"- Last heartbeat: {hb.get('ts') if hb else 'none'}")

    L += ["", "## Activity per series", "", "| series | quotes | cancels | fills |", "|---|---|---|---|"]
    per: dict[str, Counter] = defaultdict(Counter)
    for r in data["journal"]:
        ev = str(r.get("event") or "").lower()
        kind = ("quotes" if ("quote" in ev or "place" in ev) else "cancels" if "cancel" in ev
                else "fills" if "fill" in ev else None)
        if kind:
            per[_series(r)][kind] += 1
    fills_by: Counter = Counter(_series(f) for f in data["fills"])
    for s in sorted(set(per) | set(fills_by)):
        L.append(f"| {s} | {per[s]['quotes']} | {per[s]['cancels']} | {fills_by[s] or per[s]['fills']} |")

    # settlements + realised P&L
    sets = data["settlements"]
    results = Counter(str(s.get("market_result") or s.get("result") or "?") for s in sets)
    L += ["", "## Settlements and realised P&L", "",
          f"- Settlements in window: {len(sets)} (results: " + (", ".join(f"{k}={v}" for k, v in sorted(results.items()))
                                                                or "none") + ")",
          f"- Fills in window: {len(pnl_rows)} ({sum(1 for r in pnl_rows if r['status'] == 'settled')} settled, "
          f"{sum(1 for r in pnl_rows if r['status'] == 'open')} open/unsettled)"]
    L += _pnl_block(pnl_rows, "maker") + _pnl_block(pnl_rows, "taker")
    ms = [r for r in pnl_rows if r["kind"] == "maker" and r["status"] == "settled" and r["pnl"] is not None]
    if ms:
        L += ["", "| series (maker) | settled fills | wins | P&L $ | cost $ |", "|---|---|---|---|---|"]
        by: dict[str, list] = defaultdict(list)
        for r in ms:
            by[r["series"]].append(r)
        for s in sorted(by):
            rs = by[s]
            L.append(f"| {s} | {len(rs)} | {sum(1 for r in rs if r['result'] == 'win')} | "
                     f"{sum(r['pnl'] for r in rs):.2f} | {sum(r['cost'] or 0 for r in rs):.2f} |")

    # quote / queue stats
    eps = data["quote_episodes"]
    L += ["", "## Quotes and queue position", ""]
    if eps:
        by_id = {str(f.get("fill_id")): f for f in data["fills"]}
        q = queuepos.aggregate(eps, by_id, now=pulled)
        out = Counter(str(e.get("outcome") or e.get("status") or "?") for e in eps)
        L += [f"- Quote episodes (maker orders): {q['episodes']}; with a fill: {q['fills']}",
              "- Outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(out.items())),
              f"- Median contracts ahead at join: {q['median_queue_ahead_at_join']}; "
              f"re-quote reset share: {q['requote_reset_share']}; front-of-line fill share: {q['front_fill_share']}",
              "", "| queue ahead at join | episodes | fills | fill rate | trade-through share |", "|---|---|---|---|---|"]
        L += [f"| {b['bucket']} | {b['episodes']} | {b['fills']} | {b['fill_rate']} | {b['trade_through_share']} |"
              for b in q["by_queue"]]
    else:
        L.append("- no quote episodes in the window")
    fqa = fq.aggregate(data["fills"])
    L.append(f"- Fill quality (evaluated maker fills): n={fqa['n']}, trade-through share={fqa['trade_through_share']}, "
             f"avg 60-min move={fqa['avg_move_60m_c']}c")

    # arbitrage
    arbs = data["arb_opportunities"]
    log_only = [a for a in arbs if str(a.get("executed") or S.ARB_LOG_ONLY) == S.ARB_LOG_ONLY]
    executed = len(arbs) - len(log_only)
    L += ["", "## Bracket arbitrage", "",
          f"- Opportunity docs: {len(arbs)} (sightings incl. repeats: "
          f"{sum(int(a.get('seen_count') or 1) for a in arbs)}); executed: {executed}",
          "- By kind: " + (", ".join(f"{k}={v}" for k, v in sorted(Counter(str(a.get('kind')) for a in arbs).items()))
                           or "none"),
          f"- Sum of quoted (theoretical) profit over log-only docs: "
          f"${sum(pnlsvc._f(a.get('profit')) or 0 for a in log_only):.2f}"]

    L += ["", "## Heartbeat gaps > 15 min", ""]
    hts = sorted(t for t in (_ts(h) for h in data["heartbeats"]) if t)
    gaps = [(a, b) for a, b in zip(hts, hts[1:]) if b - a > timedelta(minutes=15)]
    if len(hts) < 2:
        L.append("- (not enough heartbeat history in the store to compute gaps)")
    elif not gaps:
        L.append("- none")
    L += [f"- {a.isoformat()} -> {b.isoformat()} ({(b - a).total_seconds() / 60:.0f} min)" for a, b in gaps[-20:]]
    fails = [h for h in data["heartbeats"] if h.get("ok") is False]
    L.append(f"- Failed passes (heartbeat ok=false): {len(fails)}")
    L += ["", "## Last errors", ""]
    errs = [r for r in data["journal"] if "error" in str(r.get("event", "")).lower() or r.get("error")]
    errs = sorted(errs, key=lambda d: _ts(d) or pulled)[-10:]
    L += [f"- {clean(r).get('ts')}: {json.dumps(clean(r))[:300]}" for r in errs] or ["- none"]

    L += ["", "## Monitor", "", f"- Monitor summaries in window: {len(data['monitor_summaries'])} "
          "(all in monitor_summaries.json); last one:", ""]
    m = clean(data["last_monitor_summary"]) if data["last_monitor_summary"] else None
    L.append(f"```json\n{json.dumps(m, indent=2)[:3000]}\n```" if m else "- no monitor summary")

    L += ["", "## Analytics", ""]
    if analytics:
        t = analytics.get("totals") or {}
        L.append("- analytics_summary.json (same shape as GET /analytics/summary): " + ", ".join(
            f"{k}={v}" for k, v in t.items()))
        L += analytics_lines(analytics)
    else:
        L.append(f"- {analytics_note}")
    L += ["", "## Files", "", "See DATA_DICTIONARY.md. All datetimes are ISO-8601 UTC. Read-only copy; do not commit."]
    return "\n".join(L) + "\n"


def analytics_lines(a: dict) -> list[str]:
    """Extra SUMMARY.md lines from optional analytics keys (edge verdicts, fill rate, calibration). Guards every key."""
    L: list[str] = []
    exp = a.get("expectations") if isinstance(a.get("expectations"), dict) else None
    if exp:
        L.append(f"- Research expectations: loaded={exp.get('loaded')} (source: {exp.get('source')})")
    cells = [c for c in (a.get("by_cell") or []) if isinstance(c, dict) and c.get("verdict")]
    if cells:
        L += ["", "### Edge vs research expectation (per cell, maker fills)", "",
              "| cell | markets | fills | win rate | expected | verdict | why |", "|---|---|---|---|---|---|---|"]
        for c in cells:
            reason = str(c.get("verdict_reason") or "").replace("|", "/")
            expected = c.get("expected_win_rate")
            if expected is None and c.get("expected_roi") is not None:
                expected = f"ROI {c.get('expected_roi')}"
            L.append(f"| {c.get('cell') or c.get('key')} | {c.get('markets')} | {c.get('n')} | {c.get('win_rate')} | "
                     f"{expected} | {c.get('verdict')} | {reason} |")
        v = Counter(str(c.get("verdict")) for c in cells)
        L += ["", "- Verdicts: " + ", ".join(f"{k}={n}" for k, n in sorted(v.items()))]
    fr = a.get("fill_rate")
    if isinstance(fr, dict) and isinstance(fr.get("totals"), dict):
        t = fr["totals"]
        L.append(f"- Fill rate (closed quote episodes): quotes={t.get('quotes')}, filled={t.get('filled')}, "
                 f"partial={t.get('partial')}, unfilled={t.get('unfilled')}, fill_rate={t.get('fill_rate')}, "
                 f"median rest={t.get('median_rest_minutes')} min, median to fill={t.get('median_minutes_to_fill')} min")
    cal = a.get("calibration")
    if isinstance(cal, list) and cal:
        used = [b for b in cal if isinstance(b, dict) and b.get("n")]
        L.append(f"- Calibration: {len(used)} of {len(cal)} 2-cent price buckets have settled maker fills "
                 "(win rate vs price per bucket: analytics_summary.json -> calibration)")
    return L


# ---------------------------------------------------------------- history + archive
def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    if not path.is_file():
        return [], []
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def merge_history(out: Path, names=tuple(HISTORY_KEYS)) -> dict[str, list[str]]:
    """Upsert this pull's CSVs (already scrubbed) into history/<name>.csv by natural key. Returns {path: header}.
    Values are re-scrubbed on the way through, so history written by an older version is cleaned too."""
    hist = out / "history"
    hist.mkdir(exist_ok=True)
    headers: dict[str, list[str]] = {}
    for name in names:
        keys = HISTORY_KEYS[name]
        old_h, old = _read_csv(hist / f"{name}.csv")
        new_h, new = _read_csv(out / f"{name}.csv")
        new_h = [c for c in new_h if c != "empty"]
        if not old and not new:
            continue
        header = [c for c in old_h if not secret_key(c)]
        header += [c for c in new_h if c not in header and not secret_key(c)]
        merged: dict[tuple, dict] = {}
        for row in old + new:                                  # newer pull overwrites the same key
            k = tuple(row.get(c) or "" for c in keys)
            if not any(k):
                continue
            merged[k] = {**merged.get(k, {}), **row}
        rows = sorted(merged.values(), key=lambda r: (r.get("ts") or "", tuple(r.get(c) or "" for c in keys)))
        tmp = hist / f".{name}.csv.tmp"
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: clean(v) for c, v in r.items() if c in header})
        os.replace(tmp, hist / f"{name}.csv")
        headers[f"history/{name}.csv"] = header
    return headers


def archive_snapshot(out: Path, pulled: datetime, keep: int = ARCHIVE_KEEP) -> str:
    """Copy SUMMARY.md (+ analytics_summary.json) to archive/YYYY-MM-DD/ (UTC date; a same-day re-run overwrites)
    and keep only the newest `keep` dated folders. Only folders named YYYY-MM-DD are ever pruned."""
    arch = out / "archive"
    day = arch / pulled.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    for name in ("SUMMARY.md", "analytics_summary.json"):
        src = out / name
        if src.is_file():
            shutil.copyfile(src, day / name)
        elif (day / name).exists():
            (day / name).unlink()
    dated = sorted(d for d in arch.iterdir() if d.is_dir() and _ARCHIVE_DIR.match(d.name))
    for d in dated[:max(0, len(dated) - keep)]:
        shutil.rmtree(d)
    return day.name


# ---------------------------------------------------------------- analytics (optional service)
def try_analytics(data: dict, days: int, pulled: datetime) -> tuple[dict | None, str]:
    """analytics.summarize() (pure; same output as GET /analytics/summary) if that module exists. Never raises."""
    try:
        from control_api.services import analytics as an  # type: ignore[attr-defined]
    except ImportError:
        return None, "analytics_summary.json skipped: control_api.services.analytics is not available"
    fn = getattr(an, "summarize", None)
    if fn is None:
        return None, "analytics_summary.json skipped: control_api.services.analytics has no summarize()"
    env = (data["state"] or {}).get("mode") or "all"
    try:
        res = fn(fills=data.get("fills_lookback", data["fills"]), settlements=data["settlements"],
                 episodes=data["quote_episodes"], arbs=data["arb_opportunities"], config=data["config"],
                 now=pulled, days=max([d for d in (7, 30, 90, 180) if d <= days] or [7]), env=env,
                 journal=[r for r in data["journal"] if r.get("event") == "quote"])
        if not isinstance(res, dict):
            return None, "analytics_summary.json skipped: summarize() did not return a dict"
        return clean(res), ""
    except Exception as e:  # noqa: BLE001
        return None, f"analytics_summary.json skipped: summarize() failed ({type(e).__name__})"


# ---------------------------------------------------------------- DATA_DICTIONARY.md
TS = "UTC datetime, ISO-8601"
FILES_DOC = {
    "SUMMARY.md": ("Human-readable overview of the pull: state, activity, settlements and realised P&L, queue "
                   "stats, arbitrage counts, heartbeat gaps, last errors, last monitor summary.", {}),
    "DATA_DICTIONARY.md": ("This file: every exported file and column.", {}),
    "state.json": ("Current control state (one JSON object).", {
        "state": "Bot control state: desired (running|stopped) / mode (demo|live) for the maker; arb_desired / "
                 "arb_mode for bracket arbitrage; updated_utc (" + TS + "); updated_by.",
        "cloud_settings": "Keep-awake settings (non-secret): keepalive_minutes (self-ping interval, min), "
                          "pinger_minutes (cron-job.org interval, min), pinger_enabled, silent_alert_minutes, "
                          "cronjob_job_id, pinger_sync {ok, ts, error}, updated_utc, updated_by.",
        "config": "Current maker_config.json as stored (latest config version). `cells` is the validated strategy "
                  "table [price_lo, price_hi, hours_to_close_lo, hours_to_close_hi, side]; never edit it.",
        "last_heartbeat": "Most recent heartbeat (see heartbeats.csv), regardless of window.",
        "last_monitor_summary": "Most recent weekly maker_monitor summary, regardless of window.",
        "pulled_at": "When this pull ran (" + TS + ").",
        "window_days": "The --days window used for the time-filtered files.",
    }),
    "journal.csv": ("The bot's journal (maker_strategy writes one row per event), rows with ts in the window.", {
        "utc": "Event time as written by the bot (" + TS + ").",
        "event": "Event type: account (balance snapshot), quote (resting bid placed), cancel, error, skip, etc.",
        "ticker": "Kalshi market ticker (series-date-bracket), empty for account rows.",
        "side": "Side bought: yes | no.",
        "price": "Bid price in dollars for the side bought.",
        "count": "Contracts.", "cost": "price x count, dollars.",
        "cell": "The strategy cell matched, JSON [price_lo, price_hi, ttc_lo_h, ttc_hi_h, side].",
        "note": "Free text / JSON: account rows hold {cash, exposure, headroom}; quote rows hold the order id.",
        "row_hash": "sha1 of the 9 CSV fields (dedupe key).", "ts": "Parsed event time (" + TS + ").",
    }),
    "fills.csv": ("Kalshi fills (our executions) with ts in the window, passed through from GET /portfolio/fills.", {
        "fill_id": "Kalshi fill id (unique).", "trade_id": "Kalshi trade id.", "order_id": "Our order id.",
        "ticker": "Market ticker.", "market_ticker": "Market ticker (newer API field name).",
        "side": "Side of the order: yes | no.", "outcome_side": "Side bought (newer API), yes | no.",
        "action": "buy | sell.", "count": "Contracts filled.", "count_fp": "Contracts filled (fixed-point string).",
        "yes_price": "YES price, cents.", "no_price": "NO price, cents.",
        "yes_price_dollars": "YES price, dollars.", "no_price_dollars": "NO price, dollars.",
        "price": "Price, dollars (if Kalshi sent it).", "is_taker": "true = we took liquidity (arb legs/manual); "
        "false = maker fill (the strategy).", "fee_cost": "Fee charged, dollars.",
        "created_time": "Kalshi fill time (" + TS + ").", "ts": "Fill time used by the store (" + TS + ").",
        "env": "demo | live (P&L never mixes the two).",
        "fill_quality.trade_through": "true if a public trade printed at a worse price for our side within 60 min "
                                      "after the fill (we were picked off / adverse selection signal).",
        "fill_quality.move_60m_c": "Maker-side price move 60 min after the fill, cents (positive = in our favor).",
        "fill_quality.trades_60m": "Public trades seen in the 60-min window.",
        "fill_quality.fill_price": "Our fill price used for the evaluation, dollars.",
        "fill_quality.side": "Side evaluated (yes|no).",
        "fill_quality.evaluated_utc": "When fill quality was computed (" + TS + ").",
        "fill_quality.unreadable": "true if the fill could not be evaluated.",
    }),
    "fill_pnl.csv": ("Derived here: one row per fill in the window joined to its market's settlement. P&L = "
                     "count x (win - price) - fee (services/pnl.fill_pnl). Fills from before the window are not "
                     "included even if they settled inside it.", {
        "fill_id": "Kalshi fill id.", "ts": "Fill time (" + TS + ").", "env": "demo | live.",
        "ticker": "Market ticker.", "series": "Series (ticker prefix, e.g. KXDEMOB).",
        "side": "Side bought: yes | no.", "kind": "maker (is_taker false: the strategy) | taker (arb legs/manual).",
        "price": "Price paid per contract for the side bought, dollars.", "count": "Contracts.",
        "fee": "Fee, dollars.", "cost": "price x count + fee, dollars.",
        "status": "settled (market has a yes/no result) | open.", "market_result": "Settlement result: yes | no.",
        "result": "win (result == side) | loss.", "settled_utc": "Settlement time (" + TS + ").",
        "pnl": "Realised P&L of this fill, dollars (empty if open).",
        "trade_through": "Copy of fill_quality.trade_through (empty if not evaluated).",
    }),
    "settlements.csv": ("Kalshi /portfolio/settlements rows with ts (settlement time) in the window, one per market.", {
        "ticker": "Market ticker (unique).", "market_result": "yes | no (which side paid $1).",
        "result": "Result (older field name).",
        "yes_count": "YES contracts held at settlement.", "no_count": "NO contracts held at settlement.",
        "yes_total_cost": "Total cost of the YES position, cents.", "no_total_cost": "Total cost of the NO position, cents.",
        "revenue": "Payout received, cents.", "settled_time": "Kalshi settlement time (" + TS + ").",
        "ts": "Settlement time used by the store (" + TS + ").", "fee_cost": "Fees, dollars.",
        "value": "Settlement value per contract, cents.",
    }),
    "account_snapshots.csv": ("Account balance snapshots (from the journal's account rows), ts in the window.", {
        "ts": TS + ".", "cash": "Cash balance, dollars (excludes resting bids).",
        "exposure": "Cost of open positions, dollars.",
        "headroom": "Dollars the bot may still commit this pass (budget / open-fraction limited).",
    }),
    "heartbeats.csv": ("One row per trading pass, ts in the window.", {
        "ts": "Pass end time (" + TS + ").", "ok": "true if the pass ran without error.",
        "error": "Error text (redacted) when ok is false.", "mode": "demo | live.",
        "reason": "What triggered the pass (tick, timer, self-ping, manual...).",
        "duration_s": "Pass duration, seconds.", "desired": "Desired state at the time.",
        "quotes": "Quotes placed in the pass (seed/demo data only).",
    }),
    "quote_episodes.csv": ("Queue-position telemetry: one row per maker order resting at one price (services/queuepos.py), "
                           "ts (join time) in the window. Queue sizes are in contracts.", {
        "order_id": "Our Kalshi order id (unique).", "ts": "When the order joined the book (" + TS + ").",
        "env": "demo | live.", "series": "Series.", "ticker": "Market ticker.", "side": "Side bought: yes | no.",
        "book_side": "bid | ask (where the order rests in the YES book).",
        "price": "Price paid for the side bought, dollars.", "order_price": "YES-book order price, dollars.",
        "count": "Contracts ordered.", "status": "open | closed.",
        "outcome": "filled | repriced (re-quote moved price; queue priority reset) | cancelled | stopped | expired.",
        "queue_ahead_at_join": "Estimated contracts ahead of us when we joined.",
        "join_source": "kalshi (exact queue position) | book (order-book estimate, biased up).",
        "join_level_size": "Total size at our price level at join.",
        "kalshi_queue_at_join": "Kalshi's exact queue position at join (if available).",
        "last_obs.ts": "Last observation time (" + TS + ").", "last_obs.level": "Size at our price level.",
        "last_obs.traded": "Volume traded at our price on our side since join.",
        "last_obs.through": "true if a trade printed through our price since join.",
        "last_obs.lo": "Lower bound of contracts ahead.", "last_obs.hi": "Upper bound of contracts ahead.",
        "last_obs.nc": "No-cancel estimate of contracts ahead.", "last_obs.kq": "Kalshi exact queue position.",
        "last_obs.rem": "Our remaining (unfilled) contracts.",
        "obs": "JSON list of the last 4 observations (same fields as last_obs).",
        "obs_count": "Number of observations made.", "filled_count": "Contracts filled.",
        "fill_ids": "JSON list of fill ids (first fill first).", "first_fill_utc": "First fill time (" + TS + ").",
        "upper_at_fill": "Upper bound of contracts ahead at the first fill.",
        "front_fill": "true if we were at the front of the line when filled (upper_at_fill < 1).",
        "front_eval": "true once front_fill has been evaluated.", "gone_utc": "When the order left the book (" + TS + ").",
        "closed_utc": "When the episode closed (" + TS + ").", "updated_utc": "Last update (" + TS + ").",
    }),
    "arb_opportunities.csv": ("Bracket-arbitrage log (sum of a bracket's best prices < $1), first-seen ts in the window. "
                              "Identical log-only sightings within 120 s are collapsed into one row.", {
        "ts": "First seen (" + TS + ").", "last_seen": "Last seen (" + TS + ").",
        "seen_count": "Sightings collapsed into this row.", "event": "Kalshi event ticker.",
        "kind": "NO | YES (which side of every bracket is bought).", "contracts": "Contracts per leg.",
        "profit": "Guaranteed profit at the quoted prices after fees, dollars.", "cost": "Total cost, dollars.",
        "sum_best": "Sum of best prices across the brackets, dollars.",
        "legs": "JSON [[ticker, side, limit_price, contracts], ...].",
        "executed": "log-only | FILLED {...} | PARTIAL {...} | ERROR ... .", "mode": "demo | live.",
        "sig": "sha1 signature used for de-duplication.",
    }),
    "monitor_summaries.json": ("All weekly maker_monitor results with ts in the window, newest first (a JSON list). "
                               "Each: ts (" + TS + "), by, summary {generated_utc, window_days, overall {fills, pnl, "
                               "cost, roi, ci_low, ci_high}, per_series, fill_rate, actions {pause, need_more}}, "
                               "report_text. Seed data stores the summary fields at the top level.", {}),
    "config_versions.json": ("Every config version (all history, not windowed), newest first: version, ts (" + TS +
                             "), by (owner | monitor | seed), before, after (full config).", {}),
    "periods.json": ("Demo-period markers (all history, oldest first; a JSON list): id, name, started_at (" + TS +
                     "), note, created_by. Period 1 is implicit: it runs from the start of the data to the first "
                     "marker. A fill belongs to the period it was filled in, even if it settles later.", {}),
    "cash_flows.json": ("Deposit and withdrawal events (all history, newest first; a JSON list): id, ts (" + TS +
                        "), kind (deposit | withdrawal), amount (USD, positive), note, created_by. They are shown "
                        "beside P&L and never netted into it.", {}),
    "audit.csv": ("Control actions (login, 2fa, start, stop, config changes...), ts in the window. IP and user agent "
                  "are NOT exported.", {
        "ts": TS + ".", "user": "Username.", "action": "Action name (e.g. bot.start, config.update).",
        "ok": "true if it succeeded.", "details": "JSON details (scrubbed).",
    }),
    "analytics_summary.json": ("Precomputed analytics, same shape as GET /analytics/summary (docs/ANALYTICS_CONTRACT.md): "
                               "totals, pnl_curve, by_cell, by_series, by_band, by_ttc (win rate with Wilson 95% CI, "
                               "edge = win_rate - avg_price), fill_quality per ISO week, arb. Money in dollars. Computed by "
                               "services/analytics.summarize for env = the bot's current mode and days = the largest of "
                               "7/30/90/180 not above --days (min 7); fills from 7 days before the window are included "
                               "so fills settled inside it count.", {}),
}
FILES_DOC["history/"] = ("Append-only history, one CSV per table (fills, fill_pnl, settlements, journal, quote_episodes, "
                          "arb_opportunities, account_snapshots, heartbeats). Every pull upserts its rows by natural "
                          "key (fill_id; fill_id; ticker; row_hash; order_id; sig+ts; ts; ts): the newest pull wins for "
                          "a key, and rows are never removed, so history keeps data after it ages out of Atlas "
                          "(TTL 45 d for quote episodes/arb, 180 d for the rest). Columns mean the same as in the "
                          "top-level file of the same name; columns added later are empty on older rows. Not "
                          "limited by --days. Sorted by ts.", {})
FILES_DOC["archive/"] = ("archive/YYYY-MM-DD/ (UTC date of the pull): copies of SUMMARY.md and analytics_summary.json "
                         "from that day's last pull, so you can see how the numbers moved. The newest 90 dated folders "
                         "are kept; older ones are deleted.", {})
FILES_DOC["pull.log"] = ("Output of the scheduled daily pull (scripts/schedule_pull_data.ps1), appended each run. "
                         "Check it when the data looks stale. Absent until the schedule has run.", {})
FILES_DOC["shadow_episodes.csv"] = (
    "Only with --shadow. One row per Shadow-mode hypothetical bid (an 'episode'; nothing real was ordered), newest "
    "20,000 of the last 30 days, scrubbed fields only. Use it to see which episodes are capped, ended, unread or "
    "unsettled.", {
        "ep_id": "Episode id (sha256 hex).", "ticker": "Market ticker.", "series": "Series.",
        "side": "Side the hypothetical bid buys: yes | no.", "price": "Bid price for that side, dollars.",
        "count": "Contracts the bid would have been for.", "cost": "count x price, dollars.",
        "methodology": "1 = legacy (uncapped, joined before the capped code); 2 = capped. The headline uses 2 only.",
        "capped": "true if a limit refused the bid (recorded, never scored).",
        "cap_reason": "series_cap | market_cap | open_cap | run_budget when capped, else empty.",
        "join_ts": "When the bid joined (" + TS + ").", "end_ts": "When the bid ended (" + TS + "); empty while open.",
        "end_reason": "cancel | closed | shadow_off | stale.", "close_time": "Market close (" + TS + ").",
        "trades_read": "true once the public trades after the join were read for the fill estimate.",
        "fill": "Fill estimate JSON {lo, mid, hi} in contracts; empty = unknown.",
        "result": "yes | no | void once settled; empty = not yet settled.",
        "settled_ts": "When the result was read (" + TS + ").",
        "settle_checked_ts": "When settlement was last looked up for this episode (" + TS + ").",
        "interval": "Shadow cadence in minutes (5 | 7 | 10) that created the bid.",
        "fee_mode": "Fee schedule applied to the modeled fill: empty = not set (legacy).",
        "last_seen_ts": "Last cycle that still saw the bid resting (" + TS + ").",
        "through": "true once a public trade crossed the bid price after the join (modeled fill evidence).",
        "through_ts": "First such crossing trade (" + TS + "); empty if none.",
        "through_vol": "Contracts traded through the price after the join; empty if not read yet.",
        "traded_vol": "Contracts traded at the price level after the join.",
        "truncated": "true if the trade read hit its page limit, so the fill estimate may be understated.",
        "n_obs": "Number of cycles the bid was observed.",
        "pnl": "Modeled profit in dollars once settled; empty before. Not a real fill.",
    })
FILES_DOC["shadow_passes.csv"] = (
    "Only with --shadow. One row per Shadow cycle (newest 5,000): counts, errors and the held-position figures.", {
        "ts": "Cycle time (" + TS + ").", "ok": "true if the cycle ran without error.",
        "held_cost": "Dollars the simulated held position counted this cycle.",
        "ceiling": "Simulated open-exposure ceiling, dollars.", "headroom": "Ceiling room left for new bids, dollars.",
        "held_stale_ignored": "Ended episodes left out of the held position as legacy or long past close.",
        "capped_new": "New bids refused by a limit this cycle.", "settled": "Episodes settled this cycle.",
        "voided": "Episodes voided this cycle.", "errors": "Error type names JSON (no messages).",
    })
GENERIC = {"fills.csv": "Kalshi fill field passed through unchanged (see Kalshi API docs).",
           "settlements.csv": "Kalshi settlement field passed through unchanged (see Kalshi API docs).",
           "arb_opportunities.csv": "Extra field recorded by the arbitrage scanner."}


def _describe(fname: str, col: str) -> str:
    fname = fname.split("/", 1)[1] if fname.startswith("history/") else fname
    cols = FILES_DOC.get(fname, ("", {}))[1]
    if col in cols:
        return cols[col]
    if "." in col:
        parent, child = col.split(".", 1)
        return f"Sub-field `{child}` of `{parent}`."
    if col == "empty":
        return "Placeholder header: no rows in the window."
    return GENERIC.get(fname, "Extra field stored with the row (passed through as recorded).")


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def dictionary_md(headers: dict[str, list[str]], written: list[str], pulled: datetime) -> str:
    L = ["# Local data dictionary", "", f"Generated by `python -m control_api.tools.pull_data` at {pulled.isoformat()}.",
         "", "Conventions: all datetimes are **UTC**, ISO-8601. Money is in **dollars** unless a column says cents. "
         "Prices are per contract (a contract pays $1 if it wins). `maker` = resting post-only bid (the strategy); "
         "`taker` = arb legs or manual trades. demo and live data are both present: filter on `env` / `mode`. "
         "Read-only copy of the store; never commit it.", "",
         "Start with SUMMARY.md, then the file you need. `fill_pnl.csv` is the easiest table for P&L questions.", ""]
    for fname in written:
        base = fname.split("/", 1)[1] if fname.startswith("history/") else fname
        about, cols = FILES_DOC.get(fname) or FILES_DOC.get(base, ("(undocumented file)", {}))
        if fname.startswith("history/"):
            about, cols = f"History of {base} (see `history/` above for how rows merge). {about}", {}
        L += [f"## {fname}", "", about, ""]
        if fname in headers:
            L += ["| column | meaning |", "|---|---|"]
            L += [f"| `{c}` | {_cell(_describe(fname, c))} |" for c in headers[fname]]
            L.append("")
        elif cols:
            L += ["| key | meaning |", "|---|---|"] + [f"| `{k}` | {_cell(v)} |" for k, v in cols.items()] + [""]
    return "\n".join(L)


# ---------------------------------------------------------------- write
def write_all(data: dict, out: Path, days: int, source: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    pulled = datetime.now(timezone.utc)
    state = {k: clean(data[k]) for k in ("state", "cloud_settings", "config", "last_heartbeat", "last_monitor_summary")}
    state["pulled_at"] = pulled.isoformat()
    state["window_days"] = days
    counts: dict[str, int] = {}
    headers: dict[str, list[str]] = {}
    (out / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    counts["state.json"] = 1

    tables = {name: data[name] for name in CSV_TABLES if name not in ("fill_pnl", "audit")}
    tables["fill_pnl"] = fill_pnl_rows(data["fills"], data["settlements"])
    tables["audit"] = [{k: a.get(k) for k in AUDIT_KEYS} for a in data["audit"]]
    for name in CSV_TABLES:
        fname = f"{name}.csv"
        headers[fname] = write_csv(out / fname, tables[name], AUDIT_KEYS if name == "audit" else None,
                                   FLATTEN.get(name, ()))
        counts[fname] = len(tables[name])
    if "shadow_episodes" in data:                             # --shadow: scrubbed, fixed field list for episodes
        headers["shadow_episodes.csv"] = write_csv(out / "shadow_episodes.csv", data["shadow_episodes"],
                                                   SHADOW_EPISODE_KEYS)
        counts["shadow_episodes.csv"] = len(data["shadow_episodes"])
        headers["shadow_passes.csv"] = write_csv(out / "shadow_passes.csv", data["shadow_passes"])
        counts["shadow_passes.csv"] = len(data["shadow_passes"])
    for name in ("monitor_summaries", "config_versions", "periods", "cash_flows"):
        (out / f"{name}.json").write_text(json.dumps(clean(data[name]), indent=2), encoding="utf-8")
        counts[f"{name}.json"] = len(data[name])

    analytics, note = try_analytics(data, days, pulled)
    stale = out / "analytics_summary.json"
    if analytics is not None:
        stale.write_text(json.dumps(analytics, indent=2), encoding="utf-8")
        counts["analytics_summary.json"] = 1
    elif stale.exists():
        stale.unlink()                                        # never leave a stale precompute behind
    (out / "SUMMARY.md").write_text(summary_md(data, days, source, pulled, tables["fill_pnl"], analytics, note),
                                    encoding="utf-8")
    counts["SUMMARY.md"] = 1
    hist_headers = merge_history(out)
    for k, h in hist_headers.items():
        headers[k] = h
        counts[k] = len(_read_csv(out / k)[1])
    counts["archive/" + archive_snapshot(out, pulled)] = 1
    written = (["SUMMARY.md", "DATA_DICTIONARY.md"] + [k for k in counts if "/" not in k and k != "SUMMARY.md"]
               + ["history/"] + sorted(hist_headers) + ["archive/", "pull.log"])
    (out / "DATA_DICTIONARY.md").write_text(dictionary_md(headers, written, pulled), encoding="utf-8")
    counts["DATA_DICTIONARY.md"] = 1
    if analytics is None:
        print(note)
    return counts


async def run(days: int, out: Path, seed_demo: bool, shadow: bool = False) -> dict:
    if days < 1:
        raise SystemExit("--days must be >= 1")
    if seed_demo:
        from control_api.db import MemoryStore
        from control_api.db.seed import seed
        store, source = MemoryStore(), "demo seed (MemoryStore)"
        await seed(store)
    else:
        env = load_env_local(REPO_ROOT / ".env.local")
        uri = os.environ.get("MONGODB_URI") or env.get("MONGODB_URI")
        if not uri:
            raise SystemExit("MONGODB_URI not set. Create .env.local at the repo root with a READ-ONLY Atlas URI:\n"
                             "  MONGODB_URI=mongodb+srv://<readonly-user>:<password>@<cluster>/\n"
                             "  MONGODB_DB=<db name>")
        from control_api.db import get_store
        db = os.environ.get("MONGODB_DB") or env.get("MONGODB_DB")
        store = get_store(SimpleNamespace(store="mongo", mongodb_uri=uri, mongodb_db=db))
        source = "MongoDB Atlas"
    try:
        data = await collect(store, days, shadow)
    finally:
        await store.close()
    return write_all(data, out, days, source)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Copy bot data from the store into a local folder (read-only).")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", default=str(REPO_ROOT / "local_data"))
    p.add_argument("--seed-demo", action="store_true", help="use fake in-memory data instead of Atlas")
    p.add_argument("--shadow", action="store_true",
                   help="also export shadow_episodes.csv and shadow_passes.csv (scrubbed, read-only)")
    a = p.parse_args(argv)
    out = Path(a.out).resolve()
    counts = asyncio.run(run(a.days, out, a.seed_demo, a.shadow))
    print(f"Wrote {out}")
    for k, v in counts.items():
        print(f"  {k:24s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

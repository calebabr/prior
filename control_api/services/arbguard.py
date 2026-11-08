"""Arbitrage safety state that must survive a restart or a leader change (Sprint 3 pre-work). Pure functions, no I/O.

Render's disk is ephemeral, so three things are also kept in the cloud-settings document (never returned by
``GET /api/cloud``, which lists only its editable fields). None holds a secret.

* ``arb_contain`` = {kind: [record, ...]}: the contained partials of the last 48 h per execution kind ("live",
  "demo-execute"), the same records ``services/arbcontain`` builds. The series pause is still COMPUTED from each
  record's time by ``arbcontain.paused`` (until the next 00:00 America/New_York), so persisting the records never
  extends or shortens the rule; it only stops a restart or a new leader from forgetting one. The arbitrage log is
  still read too, and the two are merged by record id.
* ``arb_kill`` = {reason, event, utc}: the arbitrage legging kill switch. The local kill file is written back from
  it on startup and on each new leadership, so a restart can never turn a legging kill (which needs the owner's
  acknowledgement) into a plain "arbitrage off". The acknowledged restart clears it.
* ``shutdown_overrun`` = {utc, instance, maker_step, arb_set}: written by an instance whose graceful shutdown ran out
  of time while a step was still running (L-2). When ``arb_set`` is set, an arbitrage set was being sent and may be
  part-filled with no containment; the next leader turns that into the legging kill switch and an urgent alert.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import arbcontain

CLOUD_CONTAIN = "arb_contain"
CLOUD_KILL = "arb_kill"
CLOUD_OVERRUN = "shutdown_overrun"
KINDS = ("live", "demo-execute")
MAX_RECORDS = 50                      # per kind; 48 h of contained partials is a handful at most
STR_MAX = 120
OVERRUN_REASON = "legging risk: shutdown overran during an arbitrage set"


def _iso(ts: Any) -> str | None:
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str) or not v:
        return None
    try:
        ts = datetime.fromisoformat(v[:40])
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _s(v: Any, n: int = STR_MAX) -> str:
    return str(v)[:n] if v is not None else ""


# ------------------------------------------------------------------ contained partials (series pause)
def encode_records(records: Iterable[dict], now: datetime) -> list[dict]:
    """Records -> stored form (ISO time), last 48 h only, newest MAX_RECORDS kept."""
    out = []
    for r in arbcontain.prune(list(records or []), now):
        ts = _iso(r.get("ts"))
        if not ts or not r.get("id"):
            continue
        out.append({"id": _s(r["id"], 64), "ts": ts, "event": _s(r.get("event")), "loss": float(r.get("loss") or 0.0),
                    "how": _s(r.get("how"), 20) or None})
    return out[-MAX_RECORDS:]


def decode_records(raw: Any, kind: str, now: datetime) -> list[dict]:
    """Stored form -> arbcontain records for `kind`. Anything malformed is dropped; the loss is clamped to >= 0
    exactly like arbcontain.records_from_docs."""
    if not isinstance(raw, Mapping):
        return []
    rows = raw.get(kind)
    if not isinstance(rows, list):
        return []
    out = []
    for d in rows[-MAX_RECORDS:]:
        if not isinstance(d, Mapping) or not d.get("id"):
            continue
        ts = _parse_ts(d.get("ts"))
        ev = d.get("event")
        if ts is None or not isinstance(ev, str) or not ev:
            continue
        loss = arbcontain._num(d.get("loss"))
        out.append(arbcontain.record(ev, loss if loss is not None and loss >= 0 else 0.0, ts, str(d["id"]),
                                     d.get("how") if isinstance(d.get("how"), str) else None))
    return arbcontain.prune(out, now)


def contain_doc(by_kind: Mapping[str, Iterable[dict]], stored: Any, now: datetime) -> dict:
    """The full ``arb_contain`` value to save: every kind in memory (merged with what was stored for it, so a
    record is never dropped by a write) plus the stored kinds this process has not loaded."""
    out: dict[str, list[dict]] = {}
    for kind in KINDS:
        mem = list(by_kind.get(kind) or [])
        old = decode_records(stored, kind, now)
        recs = arbcontain.merge(mem, old) if mem or old else []
        enc = encode_records(recs, now)
        if enc:
            out[kind] = enc
    return out


# ------------------------------------------------------------------ the legging kill switch
def kill_from_file(text: str | None) -> dict | None:
    """The kill file's JSON (as written by arb_scanner or the runner) -> the stored doc, or None for no kill /
    a Stop kill (Stop persists itself through the state document)."""
    import json
    if not text or text.startswith("stopped by"):
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return {"reason": "legging risk: unreadable kill file", "event": "", "utc": None}
    reason = _s(doc.get("reason")) or "legging risk"
    if "legging" not in reason:
        reason = f"legging risk: {reason}"[:STR_MAX]
    return {"reason": reason, "event": _s(doc.get("event")), "utc": _s(doc.get("utc"), 40) or None}


def normalize_kill(raw: Any) -> dict | None:
    """Stored ``arb_kill`` -> {reason, event, utc} (reason always says "legging", so kill_reason reads it as the
    legging kind) or None."""
    if not isinstance(raw, Mapping) or not raw.get("reason"):
        return None
    reason = _s(raw.get("reason"))
    if "legging" not in reason:
        reason = f"legging risk: {reason}"[:STR_MAX]
    return {"reason": reason, "event": _s(raw.get("event")), "utc": _s(raw.get("utc"), 40) or None}


def kill_file_text(doc: Mapping) -> str:
    import json
    return json.dumps({"reason": doc["reason"], "event": doc.get("event") or "", "utc": doc.get("utc"),
                       "restored": True})


# ------------------------------------------------------------------ shutdown overrun marker (L-2)
def overrun_doc(instance: str, now: datetime, *, maker_step: bool, arb_set: Mapping | None) -> dict:
    a = None
    if isinstance(arb_set, Mapping):
        a = {"kind": _s(arb_set.get("kind"), 20), "event": _s(arb_set.get("event"))}
    return {"utc": _iso(now), "instance": _s(instance, 64), "maker_step": bool(maker_step), "arb_set": a}


def normalize_overrun(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    a = raw.get("arb_set")
    a = {"kind": _s(a.get("kind"), 20), "event": _s(a.get("event"))} if isinstance(a, Mapping) else None
    return {"utc": _s(raw.get("utc"), 40) or None, "instance": _s(raw.get("instance"), 64),
            "maker_step": raw.get("maker_step") is True, "arb_set": a}


def overrun_kill(raw: Any, own_instance: str) -> dict | None:
    """The kill doc a new leader must apply for a predecessor's overrun mid arbitrage set, else None. Our own
    marker is never applied by ourselves (a process cannot succeed itself)."""
    ov = normalize_overrun(raw)
    if ov is None or ov["arb_set"] is None or ov["instance"] == _s(own_instance, 64):
        return None
    return {"reason": OVERRUN_REASON, "event": ov["arb_set"]["event"], "utc": ov["utc"]}

__all__ = ["CLOUD_CONTAIN", "CLOUD_KILL", "CLOUD_OVERRUN", "OVERRUN_REASON", "contain_doc", "decode_records",
           "encode_records", "kill_file_text", "kill_from_file", "normalize_kill", "normalize_overrun",
           "overrun_doc", "overrun_kill"]

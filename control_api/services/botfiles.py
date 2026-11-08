"""kalshi_bot file helpers: import path, atomic config writes, journal tailing, config schema."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BOT_DIR = Path(__file__).resolve().parents[2] / "kalshi_bot"


def ensure_bot_path() -> None:
    p = str(BOT_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def strategy():
    """Import kalshi_bot/maker_strategy.py as a top-level module (it is not a package)."""
    ensure_bot_path()
    import maker_strategy
    return maker_strategy


def monitor():
    ensure_bot_path()
    import maker_monitor
    return maker_monitor


def is_standin() -> bool:
    """True unless the private strategy has been overlaid on the public stand-in kalshi_bot (fail-closed: any
    doubt counts as the stand-in, which refuses LIVE). Overlay contract (scripts/overlay_private.py):
      * the stand-in maker_strategy sets IS_STANDIN = True; the real one must not set it True;
      * the overlay writes kalshi_bot/_overlay.py with IS_STANDIN = False after copying the private files.
    Both must agree. Never raises; does no I/O beyond the imports (safe on any path except Stop, which never
    calls it)."""
    try:
        ensure_bot_path()
        import importlib
        ms = importlib.import_module("maker_strategy")
        if getattr(ms, "IS_STANDIN", False) is not False:
            return True
        ov = importlib.import_module("_overlay")
        return getattr(ov, "IS_STANDIN", True) is not False
    except Exception:  # noqa: BLE001   (missing marker, import error: stand-in)
        return True


EDITABLE = ("run_budget", "max_order", "max_open_frac", "poll_minutes", "series", "record_decisions")
# Optional strategy keys: absent from the strategy DEFAULTS (and from a config that never set them), so they are
# shown with this default and only stored once written. Never added to the stored config or its hash implicitly.
OPTIONAL_DEFAULTS = {"record_decisions": False}


def schema(settings: Any) -> dict:
    ms = strategy()
    return {
        "run_budget": {"type": "number", "unit": "USD per run", "min": 1, "max": settings.max_run_budget,
                       "help": "Max $ of new bids per run, split across series.", "editable": True},
        "max_order": {"type": "number", "unit": "USD per order", "min": 1, "max": 1000,
                      "help": "Max $ per single order.", "editable": True},
        "max_open_frac": {"type": "number", "unit": "fraction of balance", "min": 0.05,
                          "max": settings.max_open_frac,
                          "help": "Hard cap on $ at risk as a fraction of balance + open positions.",
                          "editable": True},
        "poll_minutes": {"type": "integer", "unit": "minutes", "min": 2, "max": settings.max_poll_minutes,
                         "help": "How often a trading pass runs (cloud ceiling keeps it under the sleep timeout).",
                         "editable": True},
        "series": {"type": "list", "unit": "Kalshi series", "min": 1, "max": len(ms.DEFAULTS["series"]),
                   "options": list(ms.DEFAULTS["series"]),
                   "help": "Markets to quote (allowed series only).", "editable": True},
        "record_decisions": {"type": "boolean", "unit": "on/off", "min": None, "max": None, "default": False,
                             "help": "Record why each trading pass did what it did (journal rows). Observation "
                                     "only: it never changes what is quoted or ordered.", "editable": True},
        "cells": {"type": "list", "unit": "strategy cells", "min": None, "max": None,
                  "help": "The validated strategy. Never editable.", "editable": False},
    }


def ceilings(settings: Any) -> dict:
    return {"run_budget": settings.max_run_budget, "max_open_frac": settings.max_open_frac,
            "poll_minutes": settings.max_poll_minutes}


def public_config(cfg: dict | None) -> dict:
    """Plain JSON config without _help keys; cells as lists."""
    out = {k: v for k, v in (cfg or {}).items() if not str(k).startswith("_")}
    if "cells" in out:
        out["cells"] = [list(c) for c in out["cells"]]
    return out


def config_view(cfg: dict | None) -> dict:
    """public_config plus OPTIONAL_DEFAULTS for keys the config does not set (GET /api/config, change lists)."""
    out = public_config(cfg)
    for k, v in OPTIONAL_DEFAULTS.items():
        out.setdefault(k, v)
    return out


def merge_and_validate(current: dict | None, update: dict, settings: Any) -> dict:
    """Merge a partial update into the current config; raise ValueError([msgs]) on any problem."""
    ms = strategy()
    errs: list[str] = []
    if "cells" in update:
        errs.append("cells: never writable")
    unknown = [k for k in update if k not in EDITABLE and k != "cells"]
    if unknown:
        errs.append(f"unknown fields: {unknown}")
    ser = update.get("series")
    if isinstance(ser, (list, tuple)) and len(ser) != len({str(s) for s in ser}):
        errs.append("series: duplicate entries are not allowed")      # S-5 (API layer only)
    if "record_decisions" in update and not isinstance(update["record_decisions"], bool):
        errs.append("record_decisions: must be true or false")       # strict: never "true", 1 or null
    if errs:
        raise ValueError(errs)
    merged = dict(ms.DEFAULTS)
    merged.update(public_config(current))
    merged.update({k: v for k, v in update.items() if v is not None})
    try:
        ms.validate_config(merged)
    except (ValueError, TypeError, KeyError) as e:
        msg = str(e).replace("maker_config.json invalid -> ", "")
        errs.extend(m.strip() for m in msg.split(";") if m.strip())
    if "record_decisions" in merged and not isinstance(merged["record_decisions"], bool):
        errs.append("record_decisions: must be true or false")       # a stored/file value, same rule
    try:
        if float(merged["run_budget"]) > settings.max_run_budget:
            errs.append(f"run_budget: API ceiling is {settings.max_run_budget}")
        if float(merged["max_open_frac"]) > settings.max_open_frac:
            errs.append(f"max_open_frac: API ceiling is {settings.max_open_frac}")
        if int(merged["poll_minutes"]) > settings.max_poll_minutes:
            errs.append(f"poll_minutes: API ceiling is {settings.max_poll_minutes}")
    except (TypeError, ValueError):
        errs.append("numeric fields must be numbers")
    if errs:
        raise ValueError(errs)
    return public_config(merged)


def validate_stored(cfg: dict | None, settings: Any) -> dict:
    """S-12: a whole config from the store/file/monitor gets the same checks as PUT /api/config
    (validate_config + API ceilings + no duplicate series) plus the arb block's checks. Raises ValueError([msgs])."""
    if not isinstance(cfg, dict) or not cfg:
        raise ValueError(["config: missing or not an object"])
    errs: list[str] = []
    ser = cfg.get("series")
    if isinstance(ser, (list, tuple)) and len(ser) != len({str(s) for s in ser}):
        errs.append("series: duplicate entries are not allowed")
    try:
        out = merge_and_validate(cfg, {}, settings)
    except ValueError as e:
        errs.extend(e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)])
        out = {}
    if "arb" in cfg:
        from . import arb as arbsvc
        block = cfg.get("arb")
        if not isinstance(block, dict):
            errs.append("arb: must be an object")
        else:
            try:
                out["arb"] = arbsvc.validate(arbsvc.defaults(), block, settings)
            except ValueError as e:
                errs.extend(e.args[0])
    if errs:
        raise ValueError(errs)
    return out


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_config_file(path: Path, cfg: dict) -> None:
    """Write cfg into maker_config.json atomically, keeping existing _README/_help keys."""
    path = Path(path)
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    for k, v in public_config(cfg).items():
        raw[k] = v
    atomic_write_text(path, json.dumps(raw, indent=2))


def read_config_file(path: Path) -> dict | None:
    try:
        return public_config(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


class JournalTail:
    """Reads rows appended to the journal CSV since the last call (by byte offset)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0

    def skip_to_end(self) -> None:
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def read_new(self) -> list[dict]:
        if not self.path.exists():
            self.offset = 0
            return []
        if self.path.stat().st_size < self.offset:
            self.offset = 0
        with self.path.open("rb") as f:
            header = f.readline().decode("utf-8", "replace")
            start = max(self.offset, f.tell())
            f.seek(start)
            data = f.read()
        # only consume complete lines
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        data = data[: cut + 1]
        self.offset = start + len(data)
        fields = next(csv.reader([header.strip()])) if header.strip() else []
        if not fields:
            return []
        return [dict(zip(fields, r)) for r in csv.reader(io.StringIO(data.decode("utf-8", "replace"))) if r]


def write_journal_rows(path: Path, rows: list[dict], fields: tuple) -> None:
    """Rehydrate the journal CSV from store rows (oldest first)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow(["" if r.get(f) is None else r.get(f) for f in fields])
    atomic_write_text(Path(path), buf.getvalue())

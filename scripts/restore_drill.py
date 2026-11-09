"""Restore a backup folder into a SCRATCH database (dry-run by default).

    MONGODB_URI=... MONGODB_DB=<live db name> python scripts/restore_drill.py --backup backups/<stamp> \
        --target-db kalshi_scratch_drill [--apply] [--replace]

Safety rules (all enforced before any write):
  * the target database name must contain the word "scratch";
  * the target must differ (case-insensitively) from the configured live database (MONGODB_DB, default
    kalshi_control) and from the backup's source database;
  * without --apply nothing is written (the plan and checksums are verified and printed);
  * a non-empty scratch collection is refused unless --replace (which drops only that scratch collection).
Checksums in manifest.json are verified first. After --apply, document counts are compared with the manifest.
The URI is read from the environment only and is never printed.
Exit codes: 0 ok, 1 restore/verify failed, 2 refused or configuration problem.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_lib as bl  # noqa: E402


class Refused(Exception):
    pass


def check_target(target: str, live_db: str, source_db: str = "") -> None:
    t = (target or "").strip()
    if not t:
        raise Refused("--target-db is required")
    if "scratch" not in t.lower():
        raise Refused(f"target database {t!r} must contain the word 'scratch'")
    for label, other in (("live", live_db), ("source", source_db)):
        if other and t.lower() == other.strip().lower():
            raise Refused(f"target database equals the {label} database {other!r}")


def load_manifest(backup: Path) -> dict:
    mf = backup / "manifest.json"
    if not mf.is_file():
        raise Refused(f"no manifest.json in {backup}")
    m = json.loads(mf.read_text(encoding="utf-8"))
    for name, info in m.get("collections", {}).items():
        f = backup / f"{name}.ndjson.gz"
        if not f.is_file():
            raise Refused(f"missing file for collection {name}")
        if bl.sha256_file(f) != info.get("sha256"):
            raise Refused(f"checksum mismatch for collection {name}")
    return m


def restore(db: bl.Db, backup: Path, *, live_db: str, apply: bool = False, replace: bool = False) -> dict:
    """Returns {"dry_run", "target", "ok", "collections": {name: {"expected": n, "restored": n}}}."""
    m = load_manifest(backup)
    check_target(db.name, live_db, m.get("source_db", ""))
    plan = m.get("collections", {})
    for name in plan:
        if db.count(name) and not replace:
            raise Refused(f"scratch collection {name} is not empty (use --replace to drop it first)")
    report: dict = {"dry_run": not apply, "target": db.name, "collections": {}}
    for name, info in plan.items():
        entry = {"expected": info["docs"], "restored": 0}
        report["collections"][name] = entry
        if not apply:
            continue
        if replace:
            db.drop(name)
        batch: list[dict] = []
        for doc in bl.read_collection(backup / f"{name}.ndjson.gz"):
            batch.append(doc)
            if len(batch) >= bl.BATCH:
                db.insert_many(name, batch)
                batch = []
        if batch:
            db.insert_many(name, batch)
        entry["restored"] = db.count(name)
    report["ok"] = (not apply) or all(e["restored"] == e["expected"] for e in report["collections"].values())
    return report


def main(argv: list[str] | None = None, env: dict | None = None, db: bl.Db | None = None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(description="Restore a backup into a scratch database (dry-run by default).")
    p.add_argument("--backup", required=True)
    p.add_argument("--target-db", required=True)
    p.add_argument("--apply", action="store_true", help="really write (default is a dry run)")
    p.add_argument("--replace", action="store_true", help="drop non-empty scratch collections first")
    a = p.parse_args(argv)
    live = env.get("MONGODB_DB") or bl.DEFAULT_DB
    try:
        m = load_manifest(Path(a.backup))
        check_target(a.target_db, live, m.get("source_db", ""))
        if db is None:
            uri = env.get("MONGODB_URI", "")
            if not uri:
                raise Refused("MONGODB_URI is not set")
            db = bl.MongoDb(uri, a.target_db)
        elif db.name != a.target_db:
            raise Refused("database handle does not match --target-db")
    except Refused as e:
        print(f"REFUSED: {bl.scrub(str(e))}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"cannot start: {bl.scrub(type(e).__name__ + ': ' + str(e))}", file=sys.stderr)
        return 2
    try:
        rep = restore(db, Path(a.backup), live_db=live, apply=a.apply, replace=a.replace)
    except Refused as e:
        print(f"REFUSED: {bl.scrub(str(e))}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"restore failed: {bl.scrub(type(e).__name__ + ': ' + str(e))}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(("DRY RUN (nothing written)" if rep["dry_run"] else "APPLIED") + f" -> scratch database {rep['target']}")
    for k, v in rep["collections"].items():
        print(f"  {k:24s} expected {v['expected']:>8}  restored {v['restored']:>8}")
    print("RESULT: " + ("ok" if rep["ok"] else "COUNT MISMATCH"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

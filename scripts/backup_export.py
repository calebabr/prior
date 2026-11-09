"""Export the bot database to a local folder (read-only against Atlas).

    MONGODB_URI=... MONGODB_DB=kalshi_control python scripts/backup_export.py [--out backups] [--include-sensitive]

Use a READ-ONLY Atlas user (role `read`) for MONGODB_URI. Writes <out>/<UTC stamp>/<collection>.ndjson.gz and
manifest.json. Credentials, sessions and leases (users, revoked_tokens, push_subscriptions, runner_lease,
handover_nonces) are skipped unless --include-sensitive. Kalshi keys are never in the database. Never prints
the URI. Exit codes: 0 ok, 1 export failed, 2 configuration problem.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_lib as bl  # noqa: E402


def export(db: bl.Db, out_dir: Path, *, include_sensitive: bool = False, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    dest = out_dir / now.strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=False)
    cols: dict[str, dict] = {}
    skipped: list[str] = []
    for name in db.collection_names():
        if name in bl.SENSITIVE and not include_sensitive:
            skipped.append(name)
            continue
        f = dest / f"{name}.ndjson.gz"
        n = bl.write_collection(f, db.find_all(name))
        cols[name] = {"docs": n, "sha256": bl.sha256_file(f)}
    manifest = {"format": 1, "source_db": db.name, "created_utc": now.isoformat(),
                "include_sensitive": include_sensitive, "skipped": skipped, "collections": cols}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return dest


def main(argv: list[str] | None = None, env: dict | None = None, db: bl.Db | None = None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(description="Export the bot database to a local backup folder (read-only).")
    p.add_argument("--out", default="backups")
    p.add_argument("--include-sensitive", action="store_true",
                   help="also export users/sessions/leases (keep the folder encrypted)")
    a = p.parse_args(argv)
    if db is None:
        uri = env.get("MONGODB_URI", "")
        if not uri:
            print("MONGODB_URI is not set (use a read-only Atlas user).", file=sys.stderr)
            return 2
        try:
            db = bl.MongoDb(uri, env.get("MONGODB_DB") or bl.DEFAULT_DB)
        except Exception as e:  # noqa: BLE001
            print(f"cannot connect: {bl.scrub(type(e).__name__ + ': ' + str(e))}", file=sys.stderr)
            return 2
    try:
        dest = export(db, Path(a.out), include_sensitive=a.include_sensitive)
    except Exception as e:  # noqa: BLE001
        print(f"export failed: {bl.scrub(type(e).__name__ + ': ' + str(e))}", file=sys.stderr)
        return 1
    finally:
        db.close()
    m = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    print(f"Backup written to {dest}")
    for k, v in m["collections"].items():
        print(f"  {k:24s} {v['docs']}")
    if m["skipped"]:
        print("  skipped (sensitive): " + ", ".join(m["skipped"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

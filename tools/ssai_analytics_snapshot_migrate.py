from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ssai_analytics_db import connect_analytics_db  # noqa: E402
from app.services.ssai_analytics_snapshot_migration import (  # noqa: E402
    MIGRATIONS,
    apply_snapshot_migrations,
    inspect_snapshot_schema,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate/inspect the existing SSAI_ANALYTICS snapshot schema")
    parser.add_argument("--apply", action="store_true", help="Apply schema migrations")
    parser.add_argument("--inspect", action="store_true", help="Read schema and smoke row counts")
    parser.add_argument("--applied-by", default="", help="Non-secret migration actor label")
    args = parser.parse_args()

    if args.inspect:
        result: dict[str, object] = {"inspect": True, "database": "SSAI_ANALYTICS"}
        try:
            conn = connect_analytics_db("migration")
            try:
                result.update(inspect_snapshot_schema(conn))
            finally:
                conn.close()
        except Exception as exc:
            result.update({"ok": False, "error_type": type(exc).__name__})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        result["ok"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        print(
            json.dumps(
                {
                    "apply": False,
                    "database": "SSAI_ANALYTICS",
                    "migrations": [
                        {"migration_id": item.migration_id, "checksum": item.checksum}
                        for item in MIGRATIONS
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    actor = str(args.applied_by or getpass.getuser() or "").strip()
    result: dict[str, object] = {"apply": True, "database": "SSAI_ANALYTICS"}
    try:
        conn = connect_analytics_db("migration")
        try:
            result["migration"] = apply_snapshot_migrations(conn, applied_by=actor)
        finally:
            conn.close()
    except Exception as exc:
        result.update({"ok": False, "error_type": type(exc).__name__})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

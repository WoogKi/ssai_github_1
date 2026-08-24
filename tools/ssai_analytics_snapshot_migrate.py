from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ssai_analytics_snapshot_migration import (  # noqa: E402
    MIGRATIONS,
    SnapshotMigrationError,
    apply_snapshot_migrations,
    inspect_snapshot_schema,
)


def _safe_error_details(exc: Exception) -> dict[str, str]:
    root = exc.cause if isinstance(exc, SnapshotMigrationError) else exc
    args = getattr(root, "args", ()) or ()
    sqlstate = str(args[0] or "") if args else ""
    message = " | ".join(str(value) for value in args[1:]) or str(root)
    message = re.sub(r"(?i)(password|pwd|uid)\s*=\s*[^;\s]+", r"\1=<redacted>", message)
    return {
        "failed_migration_id": exc.migration_id if isinstance(exc, SnapshotMigrationError) else "",
        "error_type": type(root).__name__,
        "sqlstate": sqlstate[:20],
        "sql_error_message": message[:800],
    }
from app.services.ssai_analytics_target_resolver import (  # noqa: E402
    connect_company_analytics_db,
    normalize_sql_server_identity,
    resolve_analytics_target,
)


def _target_summary(company_id: int) -> dict[str, object]:
    """Resolve the only allowed migration target without exposing credentials."""
    target = resolve_analytics_target(company_id, "migration")
    return {
        "company_id": int(company_id),
        "target_id": target.target_id,
        "erp_server_identity": target.erp_server_identity,
        "analytics_server": target.analytics_server,
        "database": target.database,
        "same_server_target": (
            target.erp_server_identity
            == normalize_sql_server_identity(target.analytics_server)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate/inspect one company-resolved SSAI_ANALYTICS snapshot schema")
    parser.add_argument("--company-id", type=int, required=True, help="Company whose ERP SQL Server selects SSAI_ANALYTICS")
    parser.add_argument("--apply", action="store_true", help="Apply schema migrations")
    parser.add_argument("--inspect", action="store_true", help="Read schema and smoke row counts")
    parser.add_argument("--applied-by", default="", help="Non-secret migration actor label")
    args = parser.parse_args()

    try:
        target = _target_summary(args.company_id)
    except Exception as exc:
        print(json.dumps({"ok": False, "company_id": int(args.company_id), "error_type": type(exc).__name__}, ensure_ascii=False, indent=2))
        return 1

    if args.inspect:
        result: dict[str, object] = {"inspect": True, **target}
        try:
            conn = connect_company_analytics_db(args.company_id, "migration")
            try:
                result.update(inspect_snapshot_schema(conn))
            finally:
                conn.close()
        except Exception as exc:
            result.update({"ok": False, **_safe_error_details(exc)})
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
                    **target,
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
    result: dict[str, object] = {"apply": True, **target}
    try:
        conn = connect_company_analytics_db(args.company_id, "migration")
        try:
            result["migration"] = apply_snapshot_migrations(conn, applied_by=actor)
        finally:
            conn.close()
    except Exception as exc:
        result.update({"ok": False, **_safe_error_details(exc)})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

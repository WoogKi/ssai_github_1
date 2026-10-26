"""Explicit cleanup of one obsolete manifest with atomic audit history."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.snapshot_operation_history import (
    OperationEvent, analytics_target_hash, insert_event_on_cursor, utc_now,
)
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db


CHILDREN = {
    "snapshot": (
        "payload", "frequency_product", "frequency_projection",
        "frequency_monthly_activity", "frequency_scope_stock",
        "frequency_source_contract", "frequency_source_diagnostics",
    ),
    "monthly": (
        "monthly_io_fact", "frequency_month_scope_stock", "frequency_month_fact",
        "frequency_month_source_diagnostics", "frequency_month_lifecycle",
    ),
}
PARENTS = {"snapshot": "manifest", "monthly": "frequency_month_manifest"}


def _published_snapshot_is_obsolete(cursor: object, company_id: int,
                                    candidate: tuple[object, ...]) -> bool:
    """Only an older profile with a replacement for the same month may be removed."""
    from app.services.dashboard_inventory_frequency_snapshot_service import (
        build_frequency_snapshot_plan, frequency_snapshot_read_keys,
        resolve_dashboard_profile_stock_scope,
    )

    evaluation_month = str(candidate[1])
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
        product_di_codes=scope.product_di_codes,
        product_class_codes=scope.product_class_codes,
        io_gu_codes=scope.io_gu_codes, stock_mode=scope.stock_mode,
    )
    operating_keys = {
        (key.snapshot_type, key.evaluation_month, key.scope_fingerprint,
         key.schema_version, key.algorithm_version, key.profile_fingerprint or "")
        for key in frequency_snapshot_read_keys(plan)
    }
    if tuple(str(value or "") for value in candidate) in operating_keys:
        return False
    published = cursor.execute(
        "SELECT snapshot_type,evaluation_month,scope_fingerprint,schema_version,"
        "algorithm_version,profile_fingerprint FROM snapshot.manifest "
        "WHERE company_id=? AND evaluation_month=? AND status='published' "
        "AND approval_status='approved'", str(company_id), evaluation_month,
    ).fetchall()
    return any(tuple(str(value or "") for value in row) in operating_keys
               for row in published)


def cleanup_one(*, company_id: int, kind: str, manifest_id: int,
                expected_checksum: str, expected_rows: int, apply: bool,
                expected_generation: int,
                allow_published_history: bool = False) -> dict[str, object]:
    if company_id <= 0 or manifest_id <= 0 or expected_rows <= 0 or kind not in CHILDREN:
        raise ValueError("invalid cleanup scope")
    if expected_generation <= 0:
        raise ValueError("invalid expected generation")
    if len(expected_checksum) != 64 or any(c not in "0123456789abcdef" for c in expected_checksum.lower()):
        raise ValueError("expected checksum must be SHA256")
    started_at = utc_now()
    started = time.perf_counter()
    connection = connect_company_analytics_db(company_id, "writer" if apply else "reader", timeout=30)
    try:
        cursor = connection.cursor()
        parent = PARENTS[kind]
        extra = (",generation_no,snapshot_type,evaluation_month,scope_fingerprint,"
                 "schema_version,algorithm_version,profile_fingerprint") if kind == "snapshot" else ",generation_no"
        row = cursor.execute(
            f"SELECT company_id,status,approval_status,checksum{extra} FROM snapshot.{parent} "
            f"{'WITH (UPDLOCK,HOLDLOCK)' if apply else ''} WHERE manifest_id=?",
            manifest_id,
        ).fetchone()
        if not row or str(row[0]) != str(company_id):
            raise ValueError("manifest missing or belongs to another company")
        if str(row[3]).lower() != expected_checksum.lower():
            raise ValueError("checksum-mismatched manifest cannot be deleted")
        if int(row[4]) != expected_generation:
            raise ValueError("generation-mismatched manifest cannot be deleted")
        if str(row[1]) == "published":
            if (kind != "snapshot" or not allow_published_history
                    or str(row[2]) != "approved"
                    or not _published_snapshot_is_obsolete(cursor, company_id, tuple(row[5:11]))):
                raise ValueError("operating or unverified published manifest cannot be deleted")
        elif str(row[1]) not in {"superseded", "invalidated", "failed", "draft"}:
            raise ValueError("manifest status is not eligible for cleanup")
        counts = {child: int(cursor.execute(
            f"SELECT COUNT_BIG(*) FROM snapshot.{child} WHERE manifest_id=?", manifest_id,
        ).fetchval()) for child in CHILDREN[kind]}
        total = sum(counts.values()) + 1
        if total != expected_rows:
            raise ValueError("expected delete row count differs from current manifest children")
        result: dict[str, object] = {
            "company_id": company_id, "kind": kind, "manifest_id": manifest_id,
            "generation_no": int(row[4]), "status": str(row[1]),
            "expected_rows": total, "child_counts": counts,
            "applied": False,
        }
        if not apply:
            return result
        deleted = 0
        for child, count in counts.items():
            if count:
                cursor.execute(f"DELETE FROM snapshot.{child} WHERE manifest_id=?", manifest_id)
                if cursor.rowcount != count:
                    raise AssertionError("manifest child delete count changed during transaction")
                deleted += count
        cursor.execute(f"DELETE FROM snapshot.{parent} WHERE manifest_id=? AND company_id=?",
                       manifest_id, str(company_id))
        if cursor.rowcount != 1:
            raise AssertionError("manifest parent delete count changed during transaction")
        deleted += 1
        if deleted != total:
            raise AssertionError("cleanup deleted count differs from preflight")
        event = OperationEvent(
            company_id, "cleanup", "cleanup_committed", "succeeded",
            run_id=str(uuid4()), started_at=started_at, finished_at=utc_now(),
            manifest_id=manifest_id, generation_no=int(row[4]),
            deleted_manifest_ids=(manifest_id,), deleted_rows=deleted,
            whole_checksum=expected_checksum,
            total_ms=round((time.perf_counter() - started) * 1000, 3),
            details={"deleted_snapshot_ids": [manifest_id] if kind == "snapshot" else [],
                     "deleted_monthly_ids": [manifest_id] if kind == "monthly" else []},
        )
        if not insert_event_on_cursor(cursor, event, target_hash=analytics_target_hash(company_id)):
            raise AssertionError("cleanup event was not inserted")
        connection.commit()
        result["applied"] = True
        result["deleted_rows"] = deleted
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--kind", choices=tuple(CHILDREN), required=True)
    parser.add_argument("--manifest-id", type=int, required=True)
    parser.add_argument("--expected-checksum", required=True)
    parser.add_argument("--expected-rows", type=int, required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--allow-published-history", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = cleanup_one(company_id=args.company_id, kind=args.kind,
                             manifest_id=args.manifest_id,
                             expected_checksum=args.expected_checksum,
                             expected_rows=args.expected_rows, apply=args.apply,
                             expected_generation=args.expected_generation,
                             allow_published_history=args.allow_published_history)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

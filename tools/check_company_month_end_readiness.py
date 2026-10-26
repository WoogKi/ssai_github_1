"""Bounded, read-only 202611 monthly I/O readiness inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan, resolve_dashboard_profile_stock_scope,
)
from app.services.monthly_io_components import (  # noqa: E402
    MONTHLY_IO_ALGORITHM_VERSION, MONTHLY_IO_SCHEMA_VERSION,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.sql_server_monthly_io_repository import SqlServerMonthlyIoRepository  # noqa: E402
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402
from app.services.ssai_company_admin_service import list_companies  # noqa: E402
from tools.build_monthly_io_company import run_incremental  # noqa: E402


KNOWN_COMPANY_IDS = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 40)
EVALUATION_MONTH = "202611"


def _recent_closed_evaluation_months() -> tuple[str, ...]:
    latest = 2026 * 12 + 10 - 1
    return tuple(f"{index // 12:04d}{index % 12 + 1:02d}"
                 for index in range(latest - 11, latest + 1))


def classify_readiness(*, approved_months: tuple[str, ...],
                       reused_months: tuple[str, ...] = (),
                       missing_months: tuple[str, ...] = (),
                       exact_metadata_ready: bool = False,
                       checksum_verified: bool = False,
                       error: str | None = None) -> str:
    if error and error not in {
        "approved historical price seed is missing; use the full initial build",
        "approved historical price months are incomplete",
    }:
        return "D"
    if not approved_months:
        return "C"
    if (not error and exact_metadata_ready and checksum_verified
            and missing_months == ("202610",) and reused_months):
        return "A"
    return "B"


def _parse_ids(text: str) -> tuple[int, ...]:
    parts = [piece.strip() for piece in text.split(",")]
    if not parts or any(not piece.isdecimal() or int(piece) <= 0 for piece in parts):
        raise ValueError("approved company IDs must be a nonempty comma-separated list")
    ids = tuple(int(piece) for piece in parts)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate approved company ID")
    return ids


def _read_manifest_metadata(company_id: int, *, profile_fingerprint: str,
                            scope_fingerprint: str, timeout: int) -> dict[str, Any]:
    connection = connect_company_analytics_db(company_id, "reader", timeout=timeout)
    try:
        cursor = connection.cursor()
        snapshot = cursor.execute(
            """SELECT TOP 1 manifest_id,generation_no,schema_version,algorithm_version,
                      profile_fingerprint,checksum
               FROM snapshot.manifest WHERE company_id=? AND evaluation_month='202610'
                 AND status='published' AND approval_status='approved'
               ORDER BY generation_no DESC,manifest_id DESC""",
            str(company_id),
        ).fetchone()
        months = cursor.execute(
            """SELECT TOP (36) basis_month,manifest_id,generation_no,checksum,
                      product_universe_fingerprint,product_count
               FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND profile_fingerprint=? AND scope_fingerprint=?
                 AND schema_version=? AND algorithm_version=?
                 AND status='published' AND approval_status='approved'
                 AND basis_month<'202611'
               ORDER BY basis_month DESC,generation_no DESC""",
            str(company_id), profile_fingerprint, scope_fingerprint,
            MONTHLY_IO_SCHEMA_VERSION, MONTHLY_IO_ALGORITHM_VERSION,
        ).fetchall()
        retained = cursor.execute(
            """SELECT DISTINCT TOP (12) evaluation_month
               FROM snapshot.manifest WHERE company_id=? AND status='published'
                 AND approval_status='approved' AND evaluation_month>='202511'
                 AND evaluation_month<='202610'
               ORDER BY evaluation_month DESC""",
            str(company_id),
        ).fetchall()
        operation_history_available = cursor.execute(
            "SELECT OBJECT_ID(N'snapshot.operation_event',N'U')"
        ).fetchone()[0] is not None
        return {
            "september_snapshot": (
                {"manifest_id": int(snapshot[0]), "generation_no": int(snapshot[1]),
                 "schema_version": str(snapshot[2]), "algorithm_version": str(snapshot[3]),
                 "profile_matches_current": str(snapshot[4]) == (
                     scope_fingerprint if str(snapshot[3]) ==
                     SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION
                     else profile_fingerprint
                 ),
                 "checksum": str(snapshot[5])} if snapshot else None
            ),
            "monthly_manifests": tuple(
                {"basis_month": str(row[0]), "manifest_id": int(row[1]),
                 "generation_no": int(row[2]), "checksum": str(row[3]),
                 "universe_fingerprint": str(row[4]), "product_count": int(row[5])}
                for row in months
            ),
            "retained_evaluation_months": tuple(sorted({str(row[0]) for row in retained})),
            "operation_history_available": operation_history_available,
        }
    finally:
        connection.close()


def inspect_company(company_id: int, *, timeout: int = 120,
                    metadata_reader: Callable[..., dict[str, Any]] = _read_manifest_metadata,
                    planner: Callable[..., dict[str, Any]] = run_incremental,
                    repository_factory: Callable[..., SqlServerMonthlyIoRepository] | None = None,
                    scope_loader: Callable[..., Any] = resolve_dashboard_profile_stock_scope) -> dict[str, Any]:
    scope = scope_loader(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=EVALUATION_MONTH,
        stock_codes=scope.stock_codes, product_group_codes=scope.product_group_codes,
        product_di_codes=scope.product_di_codes, product_class_codes=scope.product_class_codes,
        io_gu_codes=scope.io_gu_codes, stock_mode=scope.stock_mode,
    )
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes:
        raise ValueError("stored I/O roles are not fully classified")
    scope_fingerprint = io_scope.fingerprint(plan.profile_fingerprint)
    metadata = metadata_reader(company_id, profile_fingerprint=plan.profile_fingerprint,
                               scope_fingerprint=scope_fingerprint, timeout=timeout)
    approved = tuple(sorted({row["basis_month"] for row in metadata["monthly_manifests"]}))
    report: dict[str, Any] = {
        "company_id": company_id, "verification": "read_only_metadata_and_stored_checksum",
        "snapshot": metadata["september_snapshot"], "approved_months": approved,
        "monthly_manifests": metadata["monthly_manifests"],
        "retained_evaluation_months": metadata["retained_evaluation_months"],
        "missing_recent_evaluation_months": tuple(
            month for month in _recent_closed_evaluation_months()
            if month not in metadata["retained_evaluation_months"]
        ),
        "operation_history_available": metadata.get("operation_history_available", False),
        "profile_fingerprint": plan.profile_fingerprint,
        "scope_fingerprint": scope_fingerprint,
        "stock_code_count": len(plan.stock_codes), "saved_io_count": len(plan.io_gu_codes),
        "reused_months": (), "missing_months": (), "stored_checksum_verified": False,
    }
    if metadata["september_snapshot"] and not metadata["september_snapshot"]["profile_matches_current"]:
        report["grade"] = "D"
        report["reason"] = "approved September Snapshot profile differs from current Dashboard"
        return report
    if not approved:
        report["grade"] = "C" if report["operation_history_available"] else "D"
        report["reason"] = ("no approved integrated monthly IO for current profile"
                            if report["operation_history_available"] else
                            "operation history migration not available for approval")
        return report
    try:
        planned = planner(company_id, EVALUATION_MONTH, apply=False, plan_only=True,
                          timeout=timeout, created_by="readiness")
        reused = tuple(planned["reused_months"])
        missing = tuple(planned["missing_months"])
        report["reused_months"] = reused
        report["missing_months"] = missing
        if missing == ("202610",) and reused and report["operation_history_available"]:
            repository = (repository_factory(company_id, timeout) if repository_factory else
                          SqlServerMonthlyIoRepository(
                              lambda: connect_company_analytics_db(company_id, "reader", timeout=timeout),
                              lambda: connect_company_analytics_db(company_id, "writer", timeout=timeout),
                          ))
            if len(reused) >= 7:
                evidence = repository.read_price_period_for_scope(
                    company_id=company_id, months=reused, first_bucket_month=reused[-6],
                    profile_fingerprint=plan.profile_fingerprint,
                    scope_fingerprint=scope_fingerprint, stock_codes=plan.stock_codes,
                )
                if len(evidence) != len(reused):
                    raise ValueError("approved month checksum coverage is incomplete")
            else:
                for month in reused:
                    repository.read_price_current_for_scope(
                        company_id=company_id, basis_month=month,
                        profile_fingerprint=plan.profile_fingerprint,
                        scope_fingerprint=scope_fingerprint, stock_codes=plan.stock_codes,
                        row_kind="PRICE_BUCKET",
                    )
            report["stored_checksum_verified"] = True
        report["grade"] = classify_readiness(
            approved_months=approved, reused_months=reused, missing_months=missing,
            exact_metadata_ready=True, checksum_verified=report["stored_checksum_verified"],
        )
        report["reason"] = "approved months reused; final snapshot still reads three R120 months"
    except Exception as exc:
        safe = str(exc) if str(exc) in {
            "approved historical price seed is missing; use the full initial build",
            "approved historical price months are incomplete",
        } else type(exc).__name__
        report["grade"] = classify_readiness(approved_months=approved, error=safe)
        report["reason"] = type(exc).__name__
        report["check_error"] = safe
    if report["grade"] == "B" and not report["operation_history_available"]:
        report["grade"] = "D"
        report["reason"] = "operation history migration not available for approval"
    return report


def build_inventory(*, live_read_only: bool, approved_company_ids: tuple[int, ...] = (),
                    approval_reference: str = "", timeout: int = 120,
                    registry_loader: Callable[..., list[dict[str, Any]]] = list_companies,
                    inspector: Callable[..., dict[str, Any]] = inspect_company) -> dict[str, Any]:
    if not 1 <= timeout <= 120:
        raise ValueError("timeout must be 1..120 seconds")
    if not live_read_only:
        if approved_company_ids or approval_reference:
            raise ValueError("live approval options require --live-read-only")
        return {"mode": "offline_plan", "evaluation_month": EVALUATION_MONTH,
                "registry_verified": False, "companies": [
                    {"company_id": company_id, "grade": "D", "reason": "DB not contacted"}
                    for company_id in KNOWN_COMPANY_IDS
                ]}
    if not approved_company_ids or not approval_reference.strip():
        raise ValueError("explicit approved company scope and approval reference are required")
    registered = registry_loader(include_inactive=True, top=2000)
    if len(registered) >= 2000:
        raise RuntimeError("company registry exceeds bounded inventory")
    ids = tuple(sorted({int(row["company_id"]) for row in registered}))
    if len(ids) != len(registered) or not set(approved_company_ids) <= set(ids):
        raise ValueError("approved scope does not match unique registered companies")
    companies = []
    for company_id in ids:
        if company_id not in approved_company_ids:
            companies.append({"company_id": company_id, "grade": "D",
                              "reason": "company DB read not approved"})
            continue
        try:
            companies.append(inspector(company_id, timeout=timeout))
        except Exception as exc:
            companies.append({"company_id": company_id, "grade": "D",
                              "reason": type(exc).__name__})
    return {"mode": "approved_read_only", "evaluation_month": EVALUATION_MONTH,
            "registry_verified": True, "approved_company_ids": approved_company_ids,
            "companies": companies}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--approved-company-ids", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    ids = _parse_ids(args.approved_company_ids) if args.approved_company_ids else ()
    result = build_inventory(live_read_only=args.live_read_only,
                             approved_company_ids=ids,
                             approval_reference=args.approval_reference,
                             timeout=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

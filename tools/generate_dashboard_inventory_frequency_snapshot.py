from __future__ import annotations

import argparse
import getpass
import json
import time
from decimal import Decimal
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
    require_unified_snapshot_algorithm,
    resolve_dashboard_profile_stock_scope,
    selected_io_snapshot_key,
)
from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION,
    PRODUCT_STATISTICS_SCHEMA_VERSION,
    SnapshotContractError,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402


def _json_decimal(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _plan_json(plan: Any, *, apply: bool, timeout_seconds: int, force: bool, scope_source: str, saved_io_full: bool = False) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry-run",
        "company_id": plan.company_id,
        "evaluation_month": plan.evaluation_month,
        "schema_version": PRODUCT_STATISTICS_SCHEMA_VERSION,
        "algorithm_version": SAVED_IO_AVG_PURCHASE_ALGORITHM_VERSION if saved_io_full else PRODUCT_STATISTICS_ALGORITHM_VERSION,
        "basis_months": list(plan.basis_months),
        "basis_from": plan.basis_from,
        "basis_to": plan.basis_to,
        "stock_scope": {
            "mode": "selected" if plan.stock_codes else "all",
            "count": len(plan.stock_codes),
            "stock_codes": list(plan.stock_codes),
            "source": scope_source,
        },
        "product_scope": {
            "profile_fingerprint": plan.profile_fingerprint,
            "product_group_count": len(plan.product_group_codes),
            "product_di_count": len(plan.product_di_codes),
            "product_class_count": len(plan.product_class_codes),
            "io_gu_count": len(plan.io_gu_codes),
            "stock_mode": plan.stock_mode,
            "universe": "current stock OR basis normal inbound OR basis normal outbound",
        },
        "erp_read_plan": {
            "sql_call_count": plan.erp_sql_call_count,
            "queries": [
                "profile-scoped Rddbc040 + current stock + scope-bound Rddbc110 lifecycle/inbound/purchase-price projections",
                "profile-scoped Rddbc120 bounded event stream + frequency/return/sales-price projections",
            ],
            "timeout_seconds_each": timeout_seconds,
            "retry_count": 0,
        },
        "analytics_write_plan": plan.analytics_write_plan,
        "force_new_generation": bool(force),
        "manual_approval": "required; this command never approves or publishes",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an unapproved Dashboard outbound-frequency snapshot draft"
    )
    parser.add_argument("--company-id", required=True, type=int)
    parser.add_argument("--evaluation-month", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--dashboard-profile", action="store_true")
    scope.add_argument("--stock-code", action="append", default=[])
    scope.add_argument("--all-stock-locations", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--created-by", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--saved-io-full", action="store_true", help="company-profile saved-I/O contract")
    parser.add_argument("--direct-price-baseline", action="store_true", help="Exact comparison only: bypass approved monthly price reuse")
    args = parser.parse_args()

    if args.direct_price_baseline and not args.saved_io_full:
        parser.error("--direct-price-baseline requires --saved-io-full")

    if args.saved_io_full and (args.force or not args.dashboard_profile):
        parser.error("--saved-io-full requires --dashboard-profile and no --force")
    try:
        require_unified_snapshot_algorithm(args.evaluation_month, saved_io_full=bool(args.saved_io_full))
    except SnapshotContractError as exc:
        parser.error(str(exc))

    from app.services.snapshot_operation_history import OperationEvent, record_event, utc_now
    from uuid import uuid4

    run_id = str(uuid4())
    started_at = utc_now()
    started = time.perf_counter()
    if args.apply:
        record_event(OperationEvent(args.company_id, "snapshot_generate", "scope_start", "started",
                                    run_id=run_id, started_at=started_at,
                                    evaluation_month=args.evaluation_month))
    manual_scope = None if args.dashboard_profile else ([] if args.all_stock_locations else args.stock_code)
    try:
        resolved_scope = resolve_dashboard_profile_stock_scope(
            company_id=args.company_id,
            manual_stock_codes=manual_scope,
        )
        plan = build_frequency_snapshot_plan(
            company_id=args.company_id,
            evaluation_month=args.evaluation_month,
            stock_codes=resolved_scope.stock_codes,
            product_group_codes=resolved_scope.product_group_codes,
            product_di_codes=resolved_scope.product_di_codes,
            product_class_codes=resolved_scope.product_class_codes,
            io_gu_codes=resolved_scope.io_gu_codes,
            stock_mode=resolved_scope.stock_mode,
        )
    except SnapshotContractError as exc:
        if args.apply:
            record_event(OperationEvent(args.company_id, "snapshot_generate", "scope_failed", "failed",
                                        run_id=run_id, started_at=started_at, finished_at=utc_now(),
                                        evaluation_month=args.evaluation_month,
                                        error_code=type(exc).__name__))
        print(json.dumps({
            "ok": False,
            "stage": "dashboard_profile_scope",
            "error_code": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:
        if args.apply:
            record_event(OperationEvent(args.company_id, "snapshot_generate", "scope_failed", "failed",
                                        run_id=run_id, started_at=started_at, finished_at=utc_now(),
                                        evaluation_month=args.evaluation_month,
                                        error_code=type(exc).__name__))
        print(json.dumps({
            "ok": False,
            "stage": "dashboard_profile_scope",
            "error_code": f"dashboard_profile_scope_unexpected:{type(exc).__name__}",
        }, ensure_ascii=False, indent=2))
        return 1
    output = _plan_json(
        plan,
        apply=bool(args.apply),
        timeout_seconds=max(1, int(args.timeout_seconds)),
        force=bool(args.force),
        scope_source=resolved_scope.scope_source,
        saved_io_full=bool(args.saved_io_full),
    )
    if args.saved_io_full:
        io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
        output["saved_io_roles"] = io_scope.snapshot_role_report()
        if io_scope.unclassified_codes:
            if args.apply:
                record_event(OperationEvent(args.company_id, "snapshot_generate", "scope_failed", "failed",
                                            run_id=run_id, started_at=started_at, finished_at=utc_now(),
                                            evaluation_month=args.evaluation_month,
                                            error_code="saved_io_roles_unconfirmed"))
            output.update({"ok": False, "error_code": "saved_io_roles_unconfirmed"})
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 1
        output["product_scope"]["profile_fingerprint"] = selected_io_snapshot_key(plan, io_scope).profile_fingerprint
    if not args.apply:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    actor = str(args.created_by or getpass.getuser() or "").strip()
    record_event(OperationEvent(args.company_id, "snapshot_generate", "scope_resolved", "stage_complete",
                                run_id=run_id, started_at=started_at,
                                evaluation_month=args.evaluation_month))
    try:
        result = generate_frequency_snapshot_draft(
            plan=plan,
            created_by=actor,
            timeout_seconds=max(1, int(args.timeout_seconds)),
            force=bool(args.force),
            progress_reporter=lambda message: print(f"[진행] {message}", flush=True),
            saved_io_full=bool(args.saved_io_full),
            monthly_reuse=not bool(args.direct_price_baseline),
        )
        relational_snapshot = result["relational_snapshot"]
        draft = result["draft"]
        grade_counts: dict[str, int] = {}
        for row in relational_snapshot.frequency_products:
            grade = str(row["frequency_grade"])
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
        output.update(
            {
                "ok": True,
                "representation": EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
                "schema_version": relational_snapshot.key.schema_version,
                "algorithm_version": relational_snapshot.key.algorithm_version,
                "product_count": relational_snapshot.item_count,
                "normal_event_count": sum(int(row["occurrence_count"]) for row in relational_snapshot.monthly_activity),
                "ignored_product_event_count": relational_snapshot.source_diagnostics.get("ignored_product_event_count"),
                "grade_counts": grade_counts,
                "excluded_counts": relational_snapshot.source_diagnostics,
                "checksum": relational_snapshot.checksum,
                "generation_no": draft.generation_no,
                "manifest_id": draft.manifest_id,
                "draft_status": draft.status,
                "approval_status": draft.approval_status,
                "read_status": result["read_status"],
                "no_op": draft.no_op,
                "source_metrics": result.get("source_metrics", {}),
                "generation_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        record_event(OperationEvent(
            args.company_id, "snapshot_generate", "draft_committed", "succeeded",
            run_id=run_id, started_at=started_at, finished_at=utc_now(),
            evaluation_month=args.evaluation_month, manifest_id=draft.manifest_id,
            generation_no=draft.generation_no, product_count=relational_snapshot.item_count,
            whole_checksum=relational_snapshot.checksum,
            total_ms=output["generation_elapsed_ms"],
            approval_status=draft.approval_status,
            details={"no_op": bool(draft.no_op),
                     "source_call_count": result.get("source_metrics", {}).get("source_call_count"),
                     "grade_counts": grade_counts},
        ))
    except Exception as exc:
        record_event(OperationEvent(args.company_id, "snapshot_generate", "generation_failed", "failed",
                                    run_id=run_id, started_at=started_at, finished_at=utc_now(),
                                    evaluation_month=args.evaluation_month,
                                    total_ms=round((time.perf_counter() - started) * 1000, 3),
                                    error_code=type(exc).__name__))
        output.update({"ok": False, "error_type": type(exc).__name__, "error": str(exc),
                       "generation_elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
        print(json.dumps(output, ensure_ascii=False, indent=2, default=_json_decimal))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2, default=_json_decimal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

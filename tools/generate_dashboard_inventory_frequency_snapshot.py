from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan,
    generate_frequency_snapshot_draft,
    resolve_dashboard_profile_stock_scope,
)
from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    EXTENDED_RELATIONAL_FREQUENCY_REPRESENTATION,
    PRODUCT_STATISTICS_ALGORITHM_VERSION,
    PRODUCT_STATISTICS_SCHEMA_VERSION,
    SnapshotContractError,
)


def _plan_json(plan: Any, *, apply: bool, timeout_seconds: int, force: bool, scope_source: str) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry-run",
        "company_id": plan.company_id,
        "evaluation_month": plan.evaluation_month,
        "schema_version": PRODUCT_STATISTICS_SCHEMA_VERSION,
        "algorithm_version": PRODUCT_STATISTICS_ALGORITHM_VERSION,
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
    args = parser.parse_args()

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
        print(json.dumps({
            "ok": False,
            "stage": "dashboard_profile_scope",
            "error_code": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:
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
    )
    if not args.apply:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    actor = str(args.created_by or getpass.getuser() or "").strip()
    try:
        result = generate_frequency_snapshot_draft(
            plan=plan,
            created_by=actor,
            timeout_seconds=max(1, int(args.timeout_seconds)),
            force=bool(args.force),
            progress_reporter=lambda message: print(f"[진행] {message}"),
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
            }
        )
    except Exception as exc:
        output.update({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

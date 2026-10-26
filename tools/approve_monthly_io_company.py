"""Independently recheck one incremental monthly Draft before explicit approval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    build_frequency_snapshot_plan, resolve_dashboard_profile_stock_scope,
)
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.snapshot_operation_history import (  # noqa: E402
    OperationEvent, analytics_target_hash, utc_now,
)
from app.services.sql_server_monthly_io_repository import (  # noqa: E402
    MonthlyIoMaterialization, SqlServerMonthlyIoRepository,
)
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402
from tools.build_monthly_io_company import _require_closed_source_months, run_incremental  # noqa: E402


def inspect_or_approve_month(
    *, company_id: int, evaluation_month: str, basis_month: str,
    manifest_id: int, generation_no: int, expected_checksum: str,
    approved_by: str, approval_reason: str, approve: bool, timeout: int = 120,
    repository: SqlServerMonthlyIoRepository | None = None,
    evidence_builder: Callable[..., dict[str, Any]] = run_incremental,
    scope_loader: Callable[..., Any] = resolve_dashboard_profile_stock_scope,
    target_hasher: Callable[[int], str] = analytics_target_hash,
) -> dict[str, Any]:
    if company_id <= 0 or manifest_id <= 0 or generation_no <= 0 or not 1 <= timeout <= 120:
        raise ValueError("invalid monthly approval identity or timeout")
    if len(expected_checksum) != 64 or any(c not in "0123456789abcdef" for c in expected_checksum.lower()):
        raise ValueError("expected checksum must be SHA-256")
    if not approved_by.strip() or not approval_reason.strip():
        raise ValueError("approval identity and reason are required")
    plan_scope = scope_loader(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month,
        stock_codes=plan_scope.stock_codes, product_group_codes=plan_scope.product_group_codes,
        product_di_codes=plan_scope.product_di_codes, product_class_codes=plan_scope.product_class_codes,
        io_gu_codes=plan_scope.io_gu_codes, stock_mode=plan_scope.stock_mode,
    )
    if basis_month not in plan.basis_months or basis_month != plan.basis_to[:6]:
        raise ValueError("approval is limited to the new final source month")
    _require_closed_source_months((basis_month,), today=date.today())
    io_scope = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    if io_scope.unclassified_codes:
        raise ValueError("stored I/O roles are not fully classified")
    scope_fingerprint = io_scope.fingerprint(plan.profile_fingerprint)
    repo = repository or SqlServerMonthlyIoRepository(
        lambda: connect_company_analytics_db(company_id, "reader", timeout=timeout),
        lambda: connect_company_analytics_db(company_id, "writer", timeout=timeout),
    )
    args = dict(company_id=company_id, evaluation_month=evaluation_month,
                timeout=timeout, created_by=approved_by)
    preparation = evidence_builder(**args, apply=False, plan_only=True)
    if tuple(preparation["missing_months"]) != (basis_month,):
        raise ValueError("exactly one unapproved final month is required")
    evidence: list[MonthlyIoMaterialization] = []
    fresh = evidence_builder(**args, apply=False, plan_only=False,
                             evidence_callback=evidence.append)
    if tuple(fresh["missing_months"]) != (basis_month,) or len(evidence) != 1:
        raise ValueError("incremental evidence changed during inspection")
    item = evidence[0]
    if (item.company_id != company_id or item.basis_month != basis_month
            or item.profile_fingerprint != plan.profile_fingerprint
            or item.scope_fingerprint != scope_fingerprint or item.stock_codes != plan.stock_codes
            or item.checksum.lower() != expected_checksum.lower()
            or fresh["new_months"][basis_month]["checksum"] != item.checksum):
        raise ValueError("fresh source evidence differs from pinned monthly identity")
    stored = repo.read_generation(item, generation_no)
    if (stored.manifest_id != manifest_id or stored.generation_no != generation_no
            or stored.status != "draft" or stored.approval_status != "pending"
            or stored.materialization.checksum != item.checksum
            or stored.materialization.product_universe_fingerprint != item.product_universe_fingerprint):
        raise ValueError("monthly draft identity or stored checksum mismatch")
    result = {
        "company_id": company_id, "evaluation_month": evaluation_month,
        "basis_month": basis_month, "manifest_id": manifest_id,
        "generation_no": generation_no, "checksum": item.checksum,
        "product_count": len(item.product_codes), "fact_count": len(item.facts),
        "source_select_count": fresh["erp_select_count"],
        "r110_exact": "fresh_source_matches_stored_facts",
        "r120_exact": "direct_projection_and_stored_facts_match",
        "status": "inspected_draft",
    }
    if not approve:
        return result
    latest = scope_loader(company_id=company_id)
    if latest != plan_scope:
        raise ValueError("Dashboard profile changed during monthly approval")
    now = utc_now()
    event = OperationEvent(
        company_id, "monthly_build", "month_approved", "succeeded",
        run_id=str(uuid4()), started_at=now, finished_at=now,
        evaluation_month=evaluation_month, basis_month=basis_month,
        manifest_id=manifest_id, generation_no=generation_no,
        profile_fingerprint=item.profile_fingerprint,
        product_count=len(item.product_codes), fact_count=len(item.facts),
        source_row_count=sum(f.source_row_count for f in item.facts if f.row_kind == "BASE"),
        exact_status="passed", whole_checksum=item.checksum,
        approved_by=approved_by, approval_status="approved",
        details={"source_call_count": fresh["erp_select_count"],
                 "source_verified": True, "approval_reason": approval_reason.strip()},
    )
    approved = repo.approve_checked(
        item, generation_no, expected_checksum=item.checksum,
        approved_by=approved_by, approval_reason=approval_reason,
        exact_confirmed=True, approval_event=event,
        target_hash=target_hasher(company_id),
    )
    if (approved.manifest_id != manifest_id or approved.status != "published"
            or approved.approval_status != "approved"):
        raise RuntimeError("monthly approval postcondition failed")
    current = repo.read_current(item)
    if (current.manifest_id != manifest_id or current.generation_no != generation_no
            or current.materialization.checksum != item.checksum):
        raise RuntimeError("monthly operating generation differs after approval")
    result["status"] = "published_approved"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--evaluation-month", required=True)
    parser.add_argument("--basis-month", required=True)
    parser.add_argument("--manifest-id", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--expected-checksum", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-reason", required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    result = inspect_or_approve_month(
        company_id=args.company_id, evaluation_month=args.evaluation_month,
        basis_month=args.basis_month, manifest_id=args.manifest_id,
        generation_no=args.generation, expected_checksum=args.expected_checksum,
        approved_by=args.approved_by, approval_reason=args.approval_reason,
        approve=args.approve, timeout=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

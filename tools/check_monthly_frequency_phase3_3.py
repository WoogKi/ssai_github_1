from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    build_relational_frequency_snapshot_from_aggregates,
    scope_fingerprint,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_event_grain_chunks,
    _company_snapshot_repository,
    _query_company_chunks,
    _query_company_df,
    build_frequency_month_source_plan,
    build_frequency_snapshot_plan,
    frequency_snapshot_key,
    outbound_event_grain_stream_sql,
    product_universe_sql,
    resolve_dashboard_profile_stock_scope,
)
from app.services.monthly_frequency_aggregate import (  # noqa: E402
    MONTHLY_FREQUENCY_ALGORITHM_VERSION,
    MONTHLY_FREQUENCY_SCHEMA_VERSION,
    PRODUCT_LIFECYCLE_ALGORITHM_VERSION,
    PRODUCT_LIFECYCLE_SCHEMA_VERSION,
    MonthlyFrequencyMaterialization,
    ProductLifecycleAuthority,
    assess_source_freshness,
    build_monthly_frequency_materialization,
    build_product_lifecycle_authority,
    compose_independent_monthly_window,
    extract_monthly_frequency_window,
    plan_product_lifecycle_refresh,
    product_lifecycle_sql,
    project_new_product_frequency,
    rebuild_frequency_snapshot_from_monthly_window,
)
from app.services.sql_server_monthly_frequency_repository import (  # noqa: E402
    SqlServerMonthlyFrequencyRepository,
    SqlServerProductLifecycleRepository,
)
from app.services.ssai_analytics_target_resolver import connect_company_analytics_db  # noqa: E402


def _read_outbound(plan, timeout_seconds: int):
    sql, binds = outbound_event_grain_stream_sql(plan)
    started = time.perf_counter()
    rows, diagnostics = _aggregate_event_grain_chunks(
        _query_company_chunks(plan.company_id, sql, binds, timeout_seconds)
    )
    return rows, diagnostics, time.perf_counter() - started


def _snapshot_equality(left, right) -> dict[str, bool]:
    monthly = lambda snapshot: tuple(sorted(
        (
            row["month"], row["product_code"], row["stock_code"],
            int(row["occurrence_count"]), int(row["outbound_quantity"]),
            int(row["outbound_day_count"]),
        )
        for row in snapshot.monthly_activity
    ))
    products = lambda snapshot: tuple(sorted(
        (
            row["product_code"], int(row["occurrence_count_3m"]),
            row["frequency_grade"], row["data_status"],
        )
        for row in snapshot.frequency_products
    ))
    return {
        "key": left.key == right.key,
        "scope": left.stock_codes == right.stock_codes,
        "monthly_activity": monthly(left) == monthly(right),
        "frequency_products": products(left) == products(right),
        "source_fingerprint": left.source_fingerprint == right.source_fingerprint,
        "checksum": left.checksum == right.checksum,
    }


def _repositories(company_id: int):
    reader = lambda: connect_company_analytics_db(company_id, "reader")
    writer = lambda: connect_company_analytics_db(company_id, "writer")
    return (
        SqlServerMonthlyFrequencyRepository(reader_connection_factory=reader, writer_connection_factory=writer),
        SqlServerProductLifecycleRepository(reader_connection_factory=reader, writer_connection_factory=writer),
    )


def _lifecycle_reference(company_id: int, scope_value: str) -> ProductLifecycleAuthority:
    return ProductLifecycleAuthority(
        schema_version=PRODUCT_LIFECYCLE_SCHEMA_VERSION,
        algorithm_version=PRODUCT_LIFECYCLE_ALGORITHM_VERSION,
        company_id=str(company_id), stock_codes=(), scope_fingerprint=scope_value,
        product_codes=(), product_universe_fingerprint="", source_fingerprint="",
        source_watermark=None, source_watermark_status="unverified", lifecycle=(), checksum="",
    )


def _month_reference(
    *, company_id: int, month: str, scope_value: str, lifecycle_result,
) -> MonthlyFrequencyMaterialization:
    authority = lifecycle_result.authority
    return MonthlyFrequencyMaterialization(
        monthly_schema_version=MONTHLY_FREQUENCY_SCHEMA_VERSION,
        monthly_algorithm_version=MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        company_id=str(company_id), basis_month=month, stock_codes=authority.stock_codes,
        scope_fingerprint=scope_value, source_fingerprint="", source_watermark=None,
        source_watermark_status="unverified", source_diagnostics={}, facts=(),
        product_codes=authority.product_codes,
        product_universe_fingerprint=authority.product_universe_fingerprint,
        lifecycle_authority_manifest_id=lifecycle_result.manifest_id,
        lifecycle_authority_checksum=authority.checksum,
    )


def _publish_authority(repository, authority, actor: str):
    draft = repository.publish(authority, created_by=actor)
    if draft.no_op and draft.status == "published" and draft.approval_status == "approved":
        return draft
    return repository.approve_checked(
        authority, draft.generation_no, expected_checksum=authority.checksum,
        approved_by=actor, approval_reason="Phase 3.3 shadow lifecycle authority",
    )


def _publish_month(repository, materialization, actor: str):
    draft = repository.publish(materialization, created_by=actor)
    if draft.no_op and draft.status == "published" and draft.approval_status == "approved":
        return draft
    return repository.approve_checked(
        materialization, draft.generation_no, expected_checksum=materialization.checksum,
        approved_by=actor, approval_reason="Phase 3.3 monthly shadow comparison",
    )


def run_company(*, company_id: int, timeout_seconds: int, as_of_date: str) -> dict[str, object]:
    evaluation_month = "202609"
    actor = f"monthly-frequency-phase3-3-company{company_id}"
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    scope_value = scope_fingerprint(scope.stock_codes)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month, stock_codes=scope.stock_codes,
    )

    universe_sql, universe_binds = product_universe_sql()
    started = time.perf_counter()
    universe_df = _query_company_df(company_id, universe_sql, universe_binds, timeout_seconds)
    product_codes = tuple(sorted({
        str(value or "").strip() for value in universe_df["product_code"] if str(value or "").strip()
    }))
    universe_elapsed = time.perf_counter() - started

    direct_rows, direct_diagnostics, direct_elapsed = _read_outbound(plan, timeout_seconds)
    direct = build_relational_frequency_snapshot_from_aggregates(
        company_id=company_id, evaluation_month=evaluation_month, stock_codes=scope.stock_codes,
        product_codes=product_codes, monthly_rows=direct_rows, source_diagnostics=direct_diagnostics,
    )
    operating_repo = _company_snapshot_repository(company_id)
    operating_key = operating_repo.resolve_latest_eligible_key(
        frequency_snapshot_key(plan), available_through=as_of_date,
    )
    operating = operating_repo.read(operating_key)
    approved = operating_repo.inspect_generation(
        operating_key, int(operating.generation_no or 0),
    ).relational_snapshot
    direct_vs_approved = _snapshot_equality(direct, approved) if approved is not None else {}
    if approved is None or not all(direct_vs_approved.values()):
        raise AssertionError(f"company {company_id} direct baseline differs from approved Snapshot: {direct_vs_approved}")

    lifecycle_query, lifecycle_binds = product_lifecycle_sql(
        stock_codes=scope.stock_codes, cutoff_date=as_of_date,
    )
    started = time.perf_counter()
    lifecycle_df = _query_company_df(company_id, lifecycle_query, lifecycle_binds, timeout_seconds)
    observed_authority = build_product_lifecycle_authority(
        company_id=company_id, stock_codes=scope.stock_codes, product_codes=product_codes,
        lifecycle_rows=lifecycle_df.to_dict("records"), source_watermark=None,
        source_watermark_status="unverified",
    )
    lifecycle_preflight_elapsed = time.perf_counter() - started

    monthly_repo, lifecycle_repo = _repositories(company_id)
    try:
        current_authority = lifecycle_repo.read_current(_lifecycle_reference(company_id, scope_value))
        refresh = plan_product_lifecycle_refresh(
            current_authority.authority, observed_authority, complete_change_tracking=False,
        )
        if refresh.write_required:
            raise AssertionError(
                f"company {company_id} existing lifecycle authority changed; review before write"
            )
        authority_write_elapsed = 0.0
    except LookupError:
        started = time.perf_counter()
        authority_result = _publish_authority(lifecycle_repo, observed_authority, actor)
        authority_write_elapsed = time.perf_counter() - started
        current_authority = lifecycle_repo.read_current(observed_authority)
        if current_authority.manifest_id != authority_result.manifest_id:
            raise AssertionError("approved lifecycle authority was not selected as current")
        refresh = plan_product_lifecycle_refresh(
            current_authority.authority, observed_authority, complete_change_tracking=False,
        )
    lifecycle_freshness = assess_source_freshness(
        stored_scope_fingerprint=current_authority.authority.scope_fingerprint,
        expected_scope_fingerprint=scope_value,
        stored_source_fingerprint=current_authority.authority.source_fingerprint,
        observed_source_fingerprint=observed_authority.source_fingerprint,
        stored_watermark=current_authority.authority.source_watermark,
        observed_watermark=observed_authority.source_watermark,
        stored_watermark_status=current_authority.authority.source_watermark_status,
        observed_watermark_status=observed_authority.source_watermark_status,
    )
    if lifecycle_freshness.status != "current" or lifecycle_freshness.watermark_verified:
        raise AssertionError("lifecycle exact-fingerprint freshness contract failed")

    monthly_source_elapsed: dict[str, float] = {}
    current_months: dict[str, MonthlyFrequencyMaterialization] = {}
    month_write_elapsed: dict[str, float] = {}
    for month in plan.basis_months:
        reference = _month_reference(
            company_id=company_id, month=month, scope_value=scope_value,
            lifecycle_result=current_authority,
        )
        try:
            current_months[month] = monthly_repo.read_current(reference).materialization
            month_write_elapsed[month] = 0.0
            if month == "202608":
                month_plan = build_frequency_month_source_plan(
                    company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
                )
                rows, diagnostics, elapsed = _read_outbound(month_plan, timeout_seconds)
                monthly_source_elapsed[month] = elapsed
                observed_month = build_monthly_frequency_materialization(
                    company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
                    monthly_rows=rows, source_diagnostics=diagnostics,
                    source_watermark=None, source_watermark_status="unverified",
                    product_codes=product_codes,
                    lifecycle_authority_manifest_id=current_authority.manifest_id,
                    lifecycle_authority_checksum=current_authority.authority.checksum,
                )
                if observed_month.checksum != current_months[month].checksum:
                    raise AssertionError(f"company {company_id} approved {month} fact is stale")
        except LookupError:
            month_plan = build_frequency_month_source_plan(
                company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
            )
            rows, diagnostics, elapsed = _read_outbound(month_plan, timeout_seconds)
            monthly_source_elapsed[month] = elapsed
            materialization = build_monthly_frequency_materialization(
                company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
                monthly_rows=rows, source_diagnostics=diagnostics,
                source_watermark=None, source_watermark_status="unverified",
                product_codes=product_codes,
                lifecycle_authority_manifest_id=current_authority.manifest_id,
                lifecycle_authority_checksum=current_authority.authority.checksum,
            )
            started = time.perf_counter()
            result = _publish_month(monthly_repo, materialization, actor)
            month_write_elapsed[month] = time.perf_counter() - started
            current_months[month] = monthly_repo.read_current(materialization).materialization
            if result.status != "published" or result.approval_status != "approved":
                raise AssertionError("monthly shadow generation was not approved")

    started = time.perf_counter()
    reused = [
        monthly_repo.read_current(_month_reference(
            company_id=company_id, month=month, scope_value=scope_value,
            lifecycle_result=current_authority,
        )).materialization
        for month in ("202606", "202607")
    ]
    approved_two_month_read_elapsed = time.perf_counter() - started
    august = current_months["202608"]
    started = time.perf_counter()
    composed = compose_independent_monthly_window(
        extract_monthly_frequency_window(direct), (reused[0], reused[1], august),
    )
    shadow = rebuild_frequency_snapshot_from_monthly_window(composed)
    combine_grade_elapsed = time.perf_counter() - started
    equality = _snapshot_equality(shadow, direct)
    approved_equality = _snapshot_equality(shadow, approved)
    if not all(equality.values()) or not all(approved_equality.values()):
        raise AssertionError(f"company {company_id} monthly shadow equality failed")

    inbound_months = {
        row.product_code: row.first_normal_inbound_month
        for row in current_authority.authority.lifecycle if row.first_normal_inbound_month
    }
    f_projection = project_new_product_frequency(
        shadow, first_normal_inbound_months=inbound_months,
        lifecycle_scope_fingerprint=shadow.key.scope_fingerprint,
    )
    f_count = sum(row.frequency_grade == "F" for row in f_projection)
    august_source_elapsed = monthly_source_elapsed.get("202608", 0.0)
    monthly_core_elapsed = approved_two_month_read_elapsed + august_source_elapsed + combine_grade_elapsed
    preflight_included_elapsed = (
        lifecycle_preflight_elapsed + authority_write_elapsed + monthly_core_elapsed
    )
    return {
        "company_id": company_id,
        "scope": list(scope.stock_codes),
        "scope_fingerprint": scope_value,
        "product_count": len(product_codes),
        "event_count": int(direct_diagnostics["distinct_normal_event_count"]),
        "source_row_count": int(direct_diagnostics["source_row_count"]),
        "direct_vs_approved": direct_vs_approved,
        "shadow_vs_direct": equality,
        "shadow_vs_approved": approved_equality,
        "checksum": shadow.checksum,
        "source_fingerprint": shadow.source_fingerprint,
        "source_watermark": shadow.source_watermark,
        "source_watermark_status": shadow.source_watermark_status,
        "lifecycle": {
            "manifest_id": current_authority.manifest_id,
            "generation": current_authority.generation_no,
            "refresh": refresh.__dict__,
            "freshness": lifecycle_freshness.__dict__,
            "write_elapsed": round(authority_write_elapsed, 3),
        },
        "monthly_write_elapsed": {key: round(value, 3) for key, value in month_write_elapsed.items()},
        "f_count": f_count,
        "elapsed_seconds": {
            "product_universe": round(universe_elapsed, 3),
            "direct_three_month": round(direct_elapsed, 3),
            "lifecycle_preflight": round(lifecycle_preflight_elapsed, 3),
            "approved_two_month_fact_read": round(approved_two_month_read_elapsed, 3),
            "new_one_month_aggregate": round(august_source_elapsed, 3),
            "combine_and_grade": round(combine_grade_elapsed, 6),
            "monthly_core": round(monthly_core_elapsed, 3),
            "preflight_included": round(preflight_included_elapsed, 3),
        },
        "monthly_core_faster": monthly_core_elapsed < direct_elapsed,
        "operating_reader_switched": False,
        "operating_snapshot_generation": operating.generation_no,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency Phase 3.3 shadow comparison")
    parser.add_argument("--company-id", type=int, required=True, choices=(4, 6))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--as-of-date", default="20260906")
    args = parser.parse_args()
    result = run_company(
        company_id=args.company_id,
        timeout_seconds=args.timeout_seconds,
        as_of_date=args.as_of_date,
    )
    print(json.dumps(result, ensure_ascii=True, default=str, indent=2))
    print(f"PASS company {args.company_id} direct vs monthly shadow comparison")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

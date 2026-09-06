from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    build_relational_frequency_snapshot_from_aggregates,
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
    MonthlyFrequencyMaterialization,
    build_monthly_frequency_materialization,
    build_product_lifecycle_authority,
    compose_independent_monthly_window,
    extract_monthly_frequency_window,
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


def _monthly_repository() -> SqlServerMonthlyFrequencyRepository:
    return SqlServerMonthlyFrequencyRepository(
        reader_connection_factory=lambda: connect_company_analytics_db(4, "reader"),
        writer_connection_factory=lambda: connect_company_analytics_db(4, "writer"),
    )


def _lifecycle_repository() -> SqlServerProductLifecycleRepository:
    return SqlServerProductLifecycleRepository(
        reader_connection_factory=lambda: connect_company_analytics_db(4, "reader"),
        writer_connection_factory=lambda: connect_company_analytics_db(4, "writer"),
    )


def _monthly_reference(month: str, scope_value: str) -> MonthlyFrequencyMaterialization:
    return MonthlyFrequencyMaterialization(
        monthly_schema_version=MONTHLY_FREQUENCY_SCHEMA_VERSION,
        monthly_algorithm_version=MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        company_id="4",
        basis_month=month,
        stock_codes=(),
        scope_fingerprint=scope_value,
        source_fingerprint="",
        source_watermark=None,
        source_watermark_status="unverified",
        source_diagnostics={},
        facts=(),
    )


def _quality_fixture() -> None:
    authority = build_product_lifecycle_authority(
        company_id=4,
        stock_codes=("00001",),
        product_codes=("READY", "REG_AFTER", "OUT_BEFORE", "LONG", "MISSING"),
        lifecycle_rows=(
            {"product_code": "READY", "product_registered_date": "20260101", "first_normal_inbound_date": "20260201", "first_outbound_date": "20260202"},
            {"product_code": "REG_AFTER", "product_registered_date": "20260301", "first_normal_inbound_date": "20260201", "first_outbound_date": "20260302"},
            {"product_code": "OUT_BEFORE", "product_registered_date": "20260101", "first_normal_inbound_date": "20260201", "first_outbound_date": "20260115"},
            {"product_code": "LONG", "product_registered_date": "20200101", "first_normal_inbound_date": "20260201", "first_outbound_date": "20260202"},
            {"product_code": "MISSING", "product_registered_date": "20260101", "first_normal_inbound_date": None, "first_outbound_date": None},
        ),
    )
    rows = {row.product_code: row for row in authority.lifecycle}
    assert rows["READY"].quality_status == "ready" and not rows["READY"].review_required
    assert rows["REG_AFTER"].registered_after_inbound and rows["REG_AFTER"].review_required
    assert rows["OUT_BEFORE"].outbound_before_inbound and rows["OUT_BEFORE"].review_required
    assert rows["LONG"].long_registration_to_inbound_gap and rows["LONG"].review_required
    assert rows["MISSING"].quality_status == "insufficient" and not rows["MISSING"].review_required


def run_company4(*, timeout_seconds: int, as_of_date: str) -> dict[str, object]:
    company_id = 4
    evaluation_month = "202609"
    actor = "monthly-frequency-phase3-1-company4"
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes,
    )

    universe_sql, universe_binds = product_universe_sql()
    started = time.perf_counter()
    universe_df = _query_company_df(company_id, universe_sql, universe_binds, timeout_seconds)
    universe_elapsed = time.perf_counter() - started
    product_codes = tuple(sorted({
        str(value or "").strip() for value in universe_df["product_code"] if str(value or "").strip()
    }))

    direct_rows, direct_diagnostics, direct_elapsed = _read_outbound(plan, timeout_seconds)
    direct = build_relational_frequency_snapshot_from_aggregates(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes,
        product_codes=product_codes,
        monthly_rows=direct_rows,
        source_diagnostics=direct_diagnostics,
    )
    operating_repo = _company_snapshot_repository(company_id)
    operating_key = operating_repo.resolve_latest_eligible_key(
        frequency_snapshot_key(plan), available_through=as_of_date,
    )
    operating = operating_repo.read(operating_key)
    approved = operating_repo.inspect_generation(
        operating_key, int(operating.generation_no or 0),
    ).relational_snapshot
    direct_equality = _snapshot_equality(direct, approved) if approved is not None else {}
    if approved is None or not all(direct_equality.values()):
        raise AssertionError(f"direct baseline differs from approved Snapshot: {direct_equality}")

    monthly_repo = _monthly_repository()
    old_read_started = time.perf_counter()
    old_approved = {
        month: monthly_repo.read_current(_monthly_reference(month, direct.key.scope_fingerprint))
        for month in ("202606", "202607")
    }
    old_two_month_read_elapsed = time.perf_counter() - old_read_started

    lifecycle_query, lifecycle_binds = product_lifecycle_sql(
        stock_codes=scope.stock_codes, cutoff_date=as_of_date,
    )
    started = time.perf_counter()
    lifecycle_df = _query_company_df(company_id, lifecycle_query, lifecycle_binds, timeout_seconds)
    lifecycle_source_elapsed = time.perf_counter() - started
    lifecycle_rows = lifecycle_df.to_dict("records")
    lifecycle_authority = build_product_lifecycle_authority(
        company_id=company_id,
        stock_codes=scope.stock_codes,
        product_codes=product_codes,
        lifecycle_rows=lifecycle_rows,
        source_watermark=None,
        source_watermark_status="unverified",
    )
    lifecycle_repo = _lifecycle_repository()
    started = time.perf_counter()
    lifecycle_draft = lifecycle_repo.publish(lifecycle_authority, created_by=actor)
    lifecycle_write_elapsed = time.perf_counter() - started
    if lifecycle_draft.no_op and lifecycle_draft.status == "published":
        lifecycle_approved = lifecycle_draft
    else:
        started = time.perf_counter()
        lifecycle_approved = lifecycle_repo.approve_checked(
            lifecycle_authority,
            lifecycle_draft.generation_no,
            expected_checksum=lifecycle_authority.checksum,
            approved_by=actor,
            approval_reason="Phase 3.1 shared lifecycle authority Gate",
        )
        lifecycle_approval_elapsed = time.perf_counter() - started
    if lifecycle_draft.no_op and lifecycle_draft.status == "published":
        lifecycle_approval_elapsed = 0.0
    started = time.perf_counter()
    lifecycle_read = lifecycle_repo.read_current(lifecycle_authority)
    lifecycle_read_elapsed = time.perf_counter() - started
    if lifecycle_read.authority != lifecycle_authority:
        raise AssertionError("lifecycle authority readback differs from approved generation")

    optimized: list[MonthlyFrequencyMaterialization] = []
    for month in ("202606", "202607"):
        old = old_approved[month].materialization
        optimized.append(build_monthly_frequency_materialization(
            company_id=company_id,
            basis_month=month,
            stock_codes=scope.stock_codes,
            monthly_rows=(fact.as_snapshot_row() for fact in old.facts),
            source_diagnostics=old.source_diagnostics,
            source_watermark=old.source_watermark,
            source_watermark_status=old.source_watermark_status,
            product_codes=product_codes,
            lifecycle_authority_manifest_id=lifecycle_approved.manifest_id,
            lifecycle_authority_checksum=lifecycle_authority.checksum,
        ))

    august_plan = build_frequency_month_source_plan(
        company_id=company_id, basis_month="202608", stock_codes=scope.stock_codes,
    )
    august_rows, august_diagnostics, august_source_elapsed = _read_outbound(august_plan, timeout_seconds)
    optimized.append(build_monthly_frequency_materialization(
        company_id=company_id,
        basis_month="202608",
        stock_codes=scope.stock_codes,
        monthly_rows=august_rows,
        source_diagnostics=august_diagnostics,
        source_watermark=None,
        source_watermark_status="unverified",
        product_codes=product_codes,
        lifecycle_authority_manifest_id=lifecycle_approved.manifest_id,
        lifecycle_authority_checksum=lifecycle_authority.checksum,
    ))

    persisted = []
    monthly_write_elapsed: dict[str, float] = {}
    monthly_approval_elapsed: dict[str, float] = {}
    for item in optimized:
        started = time.perf_counter()
        draft = monthly_repo.publish(item, created_by=actor, force=True)
        monthly_write_elapsed[item.basis_month] = time.perf_counter() - started
        started = time.perf_counter()
        approved_item = monthly_repo.approve_checked(
            item,
            draft.generation_no,
            expected_checksum=item.checksum,
            approved_by=actor,
            approval_reason="Phase 3.1 lifecycle-deduplicated monthly fact Gate",
        )
        monthly_approval_elapsed[item.basis_month] = time.perf_counter() - started
        persisted.append(approved_item)

    started = time.perf_counter()
    reused = [monthly_repo.read_current(item).materialization for item in optimized[:2]]
    approved_two_month_read_elapsed = time.perf_counter() - started
    august = monthly_repo.read_current(optimized[2]).materialization
    started = time.perf_counter()
    composed = compose_independent_monthly_window(
        extract_monthly_frequency_window(direct),
        (reused[0], reused[1], august),
    )
    shadow = rebuild_frequency_snapshot_from_monthly_window(composed)
    combine_grade_elapsed = time.perf_counter() - started
    equality = _snapshot_equality(shadow, direct)
    if not all(equality.values()):
        raise AssertionError(f"optimized monthly shadow differs from direct Snapshot: {equality}")

    inbound_months = {
        row.product_code: row.first_normal_inbound_month
        for row in lifecycle_read.authority.lifecycle if row.first_normal_inbound_month
    }
    f_projection = project_new_product_frequency(
        shadow,
        first_normal_inbound_months=inbound_months,
        lifecycle_scope_fingerprint=shadow.key.scope_fingerprint,
    )
    f_count = sum(row.frequency_grade == "F" for row in f_projection)
    if f_count != 270:
        raise AssertionError(f"company 4 F contract changed: {f_count} != 270")

    state_conn = connect_company_analytics_db(4, "reader")
    try:
        rows = state_conn.cursor().execute(
            """SELECT basis_month, COUNT(*) FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                 AND status='published' AND approval_status='approved'
               GROUP BY basis_month ORDER BY basis_month""",
            "4", direct.key.scope_fingerprint,
            MONTHLY_FREQUENCY_SCHEMA_VERSION, MONTHLY_FREQUENCY_ALGORITHM_VERSION,
        ).fetchall()
    finally:
        state_conn.close()
    if {str(row[0]): int(row[1]) for row in rows} != {"202606": 1, "202607": 1, "202608": 1}:
        raise AssertionError("monthly current approved generation is not singular")

    quality_counts = Counter(row.quality_status for row in lifecycle_read.authority.lifecycle)
    total_persistence = (
        lifecycle_write_elapsed + lifecycle_approval_elapsed + lifecycle_read_elapsed
        + sum(monthly_write_elapsed.values()) + sum(monthly_approval_elapsed.values())
        + approved_two_month_read_elapsed
    )
    return {
        "company_id": company_id,
        "scope_fingerprint": direct.key.scope_fingerprint,
        "product_count": len(product_codes),
        "direct_source_rows": direct_diagnostics["source_row_count"],
        "direct_event_count": direct_diagnostics["distinct_normal_event_count"],
        "snapshot_checksum": direct.checksum,
        "exact_equality": equality,
        "f_count": f_count,
        "lifecycle": {
            "manifest_id": lifecycle_approved.manifest_id,
            "generation": lifecycle_approved.generation_no,
            "status": lifecycle_approved.status,
            "approval_status": lifecycle_approved.approval_status,
            "checksum": lifecycle_authority.checksum,
            "quality_counts": dict(sorted(quality_counts.items())),
            "review_required_count": sum(row.review_required for row in lifecycle_read.authority.lifecycle),
        },
        "monthly": [
            {
                "basis_month": item.basis_month,
                "manifest_id": result.manifest_id,
                "generation": result.generation_no,
                "fact_count": len(item.facts),
                "lifecycle_inline_count": len(item.lifecycle),
                "lifecycle_manifest_id": item.lifecycle_authority_manifest_id,
            }
            for item, result in zip(optimized, persisted)
        ],
        "elapsed_seconds": {
            "product_universe": round(universe_elapsed, 3),
            "direct_three_month_erp": round(direct_elapsed, 3),
            "new_one_month_erp_202608": round(august_source_elapsed, 3),
            "old_two_month_read_with_inline_lifecycle": round(old_two_month_read_elapsed, 3),
            "approved_two_month_fact_read": round(approved_two_month_read_elapsed, 3),
            "combine_and_grade": round(combine_grade_elapsed, 6),
            "lifecycle_source": round(lifecycle_source_elapsed, 3),
            "lifecycle_write": round(lifecycle_write_elapsed, 3),
            "lifecycle_approval_integrity_read": round(lifecycle_approval_elapsed, 3),
            "lifecycle_read": round(lifecycle_read_elapsed, 3),
            "monthly_write": {key: round(value, 3) for key, value in monthly_write_elapsed.items()},
            "monthly_approval_readback": {key: round(value, 3) for key, value in monthly_approval_elapsed.items()},
            "persistence_approval_readback_total": round(total_persistence, 3),
        },
        "operating_reader_switched": False,
        "operating_snapshot_generation": operating.generation_no,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency Phase 3.1 performance Gate")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--as-of-date", default="20260906")
    args = parser.parse_args()
    _quality_fixture()
    print("PASS lifecycle quality flags")
    if args.company_id is not None:
        if args.company_id != 4:
            raise SystemExit("live Phase 3.1 Gate is restricted to company 4")
        result = run_company4(timeout_seconds=args.timeout_seconds, as_of_date=args.as_of_date)
        print(json.dumps(result, ensure_ascii=True, default=str, indent=2))
        print("PASS company 4 lifecycle authority and optimized monthly readback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

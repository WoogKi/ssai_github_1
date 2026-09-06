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
    ProductLifecycleAuthority,
    MonthlyFrequencyMaterialization,
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
from app.services.ssai_auth_service import connect_company_db  # noqa: E402


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


def _repositories():
    reader = lambda: connect_company_analytics_db(4, "reader")
    writer = lambda: connect_company_analytics_db(4, "writer")
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
    *,
    company_id: int,
    month: str,
    scope_value: str,
    lifecycle_result,
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


def _freshness_fixture() -> None:
    common = {
        "stored_scope_fingerprint": "a" * 64,
        "expected_scope_fingerprint": "a" * 64,
        "stored_source_fingerprint": "b" * 64,
    }
    verified = assess_source_freshness(
        **common, observed_source_fingerprint="b" * 64,
        stored_watermark="10", observed_watermark="10",
        stored_watermark_status="verified", observed_watermark_status="verified",
    )
    changed = assess_source_freshness(
        **common, observed_source_fingerprint="c" * 64,
        stored_watermark=None, observed_watermark=None,
        stored_watermark_status="unverified", observed_watermark_status="unverified",
    )
    no_evidence = assess_source_freshness(
        **common, observed_source_fingerprint=None,
        stored_watermark=None, observed_watermark=None,
        stored_watermark_status="unverified", observed_watermark_status="unverified",
    )
    exact = assess_source_freshness(
        **common, observed_source_fingerprint="b" * 64,
        stored_watermark=None, observed_watermark=None,
        stored_watermark_status="unverified", observed_watermark_status="unverified",
    )
    assert verified.status == "current" and verified.watermark_verified
    assert changed.status == "stale" and not changed.watermark_verified
    assert no_evidence.status == "unverified"
    assert exact.status == "current" and exact.evidence == "exact_fingerprint"
    current = build_product_lifecycle_authority(
        company_id=4, stock_codes=("00001",), product_codes=("P1",),
        lifecycle_rows=({
            "product_code": "P1", "product_registered_date": "20260101",
            "first_normal_inbound_date": "20260201", "first_outbound_date": "20260202",
        },),
    )
    observed = build_product_lifecycle_authority(
        company_id=4, stock_codes=("00001",), product_codes=("P1",),
        lifecycle_rows=({
            "product_code": "P1", "product_registered_date": "20260101",
            "first_normal_inbound_date": "20260115", "first_outbound_date": "20260202",
        },),
    )
    full = plan_product_lifecycle_refresh(current, observed, complete_change_tracking=False)
    incremental = plan_product_lifecycle_refresh(current, observed, complete_change_tracking=True)
    reuse = plan_product_lifecycle_refresh(current, current, complete_change_tracking=False)
    assert full.mode == "full_replace" and full.candidate_product_codes == ("P1",)
    assert incremental.mode == "incremental_candidates" and not incremental.requires_full_source_scan
    assert reuse.mode == "reuse" and not reuse.write_required


def _source_tracking_probe(timeout_seconds: int) -> dict[str, object]:
    conn = connect_company_db(4)
    try:
        if hasattr(conn, "timeout"):
            conn.timeout = max(1, int(timeout_seconds))
        cursor = conn.cursor()
        table_rows = cursor.execute(
            """SELECT T.name, T.is_tracked_by_cdc,
                      CASE WHEN CT.object_id IS NULL THEN 0 ELSE 1 END AS change_tracking_enabled,
                      SUM(CASE WHEN TY.name IN ('timestamp','rowversion') THEN 1 ELSE 0 END) AS rowversion_columns
               FROM sys.tables AS T
               JOIN sys.columns AS C ON C.object_id=T.object_id
               JOIN sys.types AS TY ON TY.user_type_id=C.user_type_id
               LEFT JOIN sys.change_tracking_tables AS CT ON CT.object_id=T.object_id
               WHERE T.name IN ('Rddbc040','Rddbc110','Rddbc120')
               GROUP BY T.name,T.is_tracked_by_cdc,CT.object_id ORDER BY T.name"""
        ).fetchall()
        candidate_rows = cursor.execute(
            """SELECT T.name,C.name,TY.name
               FROM sys.tables AS T
               JOIN sys.columns AS C ON C.object_id=T.object_id
               JOIN sys.types AS TY ON TY.user_type_id=C.user_type_id
               WHERE T.name IN ('Rddbc040','Rddbc110','Rddbc120')
                 AND (TY.name IN ('timestamp','rowversion') OR C.name LIKE '%Upd%'
                      OR C.name LIKE '%Mod%' OR C.name LIKE '%Update%')
               ORDER BY T.name,C.column_id"""
        ).fetchall()
    finally:
        conn.close()
    tables = [
        {
            "table": str(row[0]), "cdc": bool(row[1]),
            "change_tracking": bool(row[2]), "rowversion_columns": int(row[3]),
        }
        for row in table_rows
    ]
    complete = len(tables) == 3 and all(
        row["cdc"] or row["change_tracking"] or row["rowversion_columns"] > 0 for row in tables
    )
    return {
        "tables": tables,
        "candidate_change_columns": [
            {"table": str(row[0]), "column": str(row[1]), "type": str(row[2])}
            for row in candidate_rows
        ],
        "complete_change_tracking": complete,
    }


def run_company4(*, timeout_seconds: int, as_of_date: str) -> dict[str, object]:
    company_id = 4
    evaluation_month = "202609"
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    scope_value = scope_fingerprint(scope.stock_codes)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month, stock_codes=scope.stock_codes,
    )
    tracking = _source_tracking_probe(timeout_seconds)

    universe_sql, universe_binds = product_universe_sql()
    started = time.perf_counter()
    universe_df = _query_company_df(company_id, universe_sql, universe_binds, timeout_seconds)
    product_codes = tuple(sorted({
        str(value or "").strip() for value in universe_df["product_code"] if str(value or "").strip()
    }))
    universe_elapsed = time.perf_counter() - started

    monthly_repo, lifecycle_repo = _repositories()
    started = time.perf_counter()
    current_lifecycle = lifecycle_repo.read_current(
        _lifecycle_reference(company_id, scope_value)
    )
    lifecycle_read_elapsed = time.perf_counter() - started

    lifecycle_query, lifecycle_binds = product_lifecycle_sql(
        stock_codes=scope.stock_codes, cutoff_date=as_of_date,
    )
    started = time.perf_counter()
    lifecycle_df = _query_company_df(company_id, lifecycle_query, lifecycle_binds, timeout_seconds)
    observed_lifecycle = build_product_lifecycle_authority(
        company_id=company_id, stock_codes=scope.stock_codes, product_codes=product_codes,
        lifecycle_rows=lifecycle_df.to_dict("records"), source_watermark=None,
        source_watermark_status="unverified",
    )
    lifecycle_candidate_elapsed = time.perf_counter() - started
    refresh = plan_product_lifecycle_refresh(
        current_lifecycle.authority, observed_lifecycle,
        complete_change_tracking=bool(tracking["complete_change_tracking"]),
    )
    lifecycle_freshness = assess_source_freshness(
        stored_scope_fingerprint=current_lifecycle.authority.scope_fingerprint,
        expected_scope_fingerprint=scope_value,
        stored_source_fingerprint=current_lifecycle.authority.source_fingerprint,
        observed_source_fingerprint=observed_lifecycle.source_fingerprint,
        stored_watermark=current_lifecycle.authority.source_watermark,
        observed_watermark=observed_lifecycle.source_watermark,
        stored_watermark_status=current_lifecycle.authority.source_watermark_status,
        observed_watermark_status=observed_lifecycle.source_watermark_status,
    )
    if refresh.write_required:
        raise AssertionError(
            "company 4 lifecycle authority changed; review candidates before a new immutable write: "
            f"{refresh.candidate_count}"
        )

    references = {
        month: _month_reference(
            company_id=company_id, month=month, scope_value=scope_value,
            lifecycle_result=current_lifecycle,
        )
        for month in plan.basis_months
    }
    started = time.perf_counter()
    reused = [monthly_repo.read_current(references[month]).materialization for month in ("202606", "202607")]
    approved_two_month_read_elapsed = time.perf_counter() - started

    august_plan = build_frequency_month_source_plan(
        company_id=company_id, basis_month="202608", stock_codes=scope.stock_codes,
    )
    august_rows, august_diagnostics, august_source_elapsed = _read_outbound(august_plan, timeout_seconds)
    observed_august = build_monthly_frequency_materialization(
        company_id=company_id, basis_month="202608", stock_codes=scope.stock_codes,
        monthly_rows=august_rows, source_diagnostics=august_diagnostics,
        source_watermark=None, source_watermark_status="unverified",
        product_codes=current_lifecycle.authority.product_codes,
        lifecycle_authority_manifest_id=current_lifecycle.manifest_id,
        lifecycle_authority_checksum=current_lifecycle.authority.checksum,
    )
    stored_august = monthly_repo.read_current(references["202608"]).materialization
    august_freshness = assess_source_freshness(
        stored_scope_fingerprint=stored_august.scope_fingerprint,
        expected_scope_fingerprint=scope_value,
        stored_source_fingerprint=stored_august.source_fingerprint,
        observed_source_fingerprint=observed_august.source_fingerprint,
        stored_watermark=stored_august.source_watermark,
        observed_watermark=observed_august.source_watermark,
        stored_watermark_status=stored_august.source_watermark_status,
        observed_watermark_status=observed_august.source_watermark_status,
    )
    if stored_august.checksum != observed_august.checksum:
        raise AssertionError("approved 202608 monthly fact is stale against the exact source fingerprint")

    direct_rows, direct_diagnostics, direct_elapsed = _read_outbound(plan, timeout_seconds)
    direct = build_relational_frequency_snapshot_from_aggregates(
        company_id=company_id, evaluation_month=evaluation_month, stock_codes=scope.stock_codes,
        product_codes=product_codes, monthly_rows=direct_rows, source_diagnostics=direct_diagnostics,
    )
    started = time.perf_counter()
    composed = compose_independent_monthly_window(
        extract_monthly_frequency_window(direct), (reused[0], reused[1], stored_august),
    )
    shadow = rebuild_frequency_snapshot_from_monthly_window(composed)
    combine_grade_elapsed = time.perf_counter() - started
    equality = _snapshot_equality(shadow, direct)
    if not all(equality.values()):
        raise AssertionError(f"Phase 3.2 shadow differs from direct baseline: {equality}")

    operating_repo = _company_snapshot_repository(company_id)
    operating_key = operating_repo.resolve_latest_eligible_key(
        frequency_snapshot_key(plan), available_through=as_of_date,
    )
    operating = operating_repo.read(operating_key)
    approved = operating_repo.inspect_generation(
        operating_key, int(operating.generation_no or 0),
    ).relational_snapshot
    approved_equality = _snapshot_equality(shadow, approved) if approved is not None else {}
    if approved is None or not all(approved_equality.values()):
        raise AssertionError(f"Phase 3.2 shadow differs from approved Snapshot: {approved_equality}")

    inbound_months = {
        row.product_code: row.first_normal_inbound_month
        for row in current_lifecycle.authority.lifecycle if row.first_normal_inbound_month
    }
    projection = project_new_product_frequency(
        shadow, first_normal_inbound_months=inbound_months,
        lifecycle_scope_fingerprint=shadow.key.scope_fingerprint,
    )
    f_count = sum(row.frequency_grade == "F" for row in projection)
    if f_count != 270:
        raise AssertionError(f"F projection changed: {f_count}")
    quality_preserved = (
        tuple(current_lifecycle.authority.lifecycle) == tuple(observed_lifecycle.lifecycle)
    )
    if not quality_preserved:
        raise AssertionError("lifecycle quality/review state changed")

    critical_path = (
        approved_two_month_read_elapsed + august_source_elapsed + combine_grade_elapsed
    )
    shadow_with_refresh = critical_path + lifecycle_candidate_elapsed + lifecycle_read_elapsed
    return {
        "company_id": company_id,
        "tracking": tracking,
        "refresh_plan": refresh.__dict__,
        "lifecycle_freshness": lifecycle_freshness.__dict__,
        "monthly_202608_freshness": august_freshness.__dict__,
        "write_count": 0,
        "exact_equality": equality,
        "approved_snapshot_equality": approved_equality,
        "snapshot_checksum": shadow.checksum,
        "f_count": f_count,
        "quality_preserved": quality_preserved,
        "elapsed_seconds": {
            "product_universe": round(universe_elapsed, 3),
            "lifecycle_candidate_full_scan": round(lifecycle_candidate_elapsed, 3),
            "lifecycle_authority_read": round(lifecycle_read_elapsed, 3),
            "lifecycle_refresh_write": 0.0,
            "approved_two_month_fact_read": round(approved_two_month_read_elapsed, 3),
            "new_one_month_aggregate_202608": round(august_source_elapsed, 3),
            "combine_and_grade": round(combine_grade_elapsed, 6),
            "direct_three_month_aggregate": round(direct_elapsed, 3),
            "monthly_critical_path": round(critical_path, 3),
            "shadow_with_full_lifecycle_refresh": round(shadow_with_refresh, 3),
        },
        "operating_reader_switched": False,
        "operating_snapshot_generation": operating.generation_no,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency Phase 3.2 lifecycle/watermark Gate")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--as-of-date", default="20260906")
    args = parser.parse_args()
    _freshness_fixture()
    print("PASS source freshness verified/unverified fixtures")
    if args.company_id is not None:
        if args.company_id != 4:
            raise SystemExit("live Phase 3.2 Gate is restricted to company 4")
        print(json.dumps(
            run_company4(timeout_seconds=args.timeout_seconds, as_of_date=args.as_of_date),
            ensure_ascii=True, default=str, indent=2,
        ))
        print("PASS company 4 lifecycle refresh plan and watermark shadow Gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

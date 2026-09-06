from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
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
    MonthlyFrequencyMaterialization,
    build_monthly_frequency_materialization,
    compose_independent_monthly_window,
    extract_monthly_frequency_window,
    product_lifecycle_sql,
    project_new_product_frequency,
    rebuild_frequency_snapshot_from_monthly_window,
)
from app.services.sql_server_monthly_frequency_repository import (  # noqa: E402
    SqlServerMonthlyFrequencyRepository,
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


def _repository() -> SqlServerMonthlyFrequencyRepository:
    return SqlServerMonthlyFrequencyRepository(
        reader_connection_factory=lambda: connect_company_analytics_db(4, "reader"),
        writer_connection_factory=lambda: connect_company_analytics_db(4, "writer"),
    )


def inspect_pending_company4() -> dict[str, object]:
    conn = connect_company_analytics_db(4, "reader")
    try:
        row = conn.cursor().execute(
            """SELECT TOP 1 basis_month, scope_fingerprint, schema_version, algorithm_version,
                      generation_no, manifest_id
               FROM snapshot.frequency_month_manifest
               WHERE company_id='4' AND status='draft'
               ORDER BY manifest_id DESC"""
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"pending": False}
    reference = MonthlyFrequencyMaterialization(
        monthly_schema_version=str(row[2]), monthly_algorithm_version=str(row[3]),
        company_id="4", basis_month=str(row[0]), stock_codes=(),
        scope_fingerprint=str(row[1]), source_fingerprint="", source_watermark=None,
        source_watermark_status="unverified", source_diagnostics={}, facts=(),
    )
    result = _repository().read_generation(reference, int(row[4]))
    return {
        "pending": True, "manifest_id": int(row[5]), "generation": int(row[4]),
        "basis_month": str(row[0]), "checksum": result.materialization.checksum,
    }


def _fixture_lifecycle_boundaries() -> None:
    snapshot = build_relational_frequency_snapshot_from_aggregates(
        company_id=4, evaluation_month="202609", stock_codes=("00001",),
        product_codes=("M0", "M1", "M2", "M3"),
        monthly_rows=(),
        source_diagnostics={
            "diagnostic_contract_version": 2, "source_row_count": 0,
            "normal_positive_accepted_row_count": 0, "normal_positive_duplicate_row_count": 0,
            "normal_positive_conflicting_row_count": 0, "normal_positive_missing_key_row_count": 0,
            "normal_positive_nonintegral_row_count": 0, "normal_nonpositive_row_count": 0,
            "return_positive_row_count": 0, "return_nonpositive_row_count": 0,
            "other_tcode_row_count": 0, "normal_positive_row_count": 0,
            "distinct_normal_event_count": 0, "conflicting_event_count": 0,
            "ignored_product_event_count": 0,
        },
    )
    projected = project_new_product_frequency(
        snapshot,
        first_normal_inbound_months={"M0": "202609", "M1": "202608", "M2": "202607", "M3": "202606"},
        lifecycle_scope_fingerprint=snapshot.key.scope_fingerprint,
    )
    grades = {row.product_code: row.frequency_grade for row in projected}
    assert grades == {"M0": "F", "M1": "F", "M2": "F", "M3": "X"}


def run_company4(*, timeout_seconds: int, as_of_date: str) -> dict[str, object]:
    company_id = 4
    evaluation_month = "202609"
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id, evaluation_month=evaluation_month, stock_codes=scope.stock_codes,
    )

    universe_sql, universe_binds = product_universe_sql()
    started = time.perf_counter()
    universe_df = _query_company_df(company_id, universe_sql, universe_binds, timeout_seconds)
    universe_elapsed = time.perf_counter() - started
    product_codes = tuple(sorted({str(value or "").strip() for value in universe_df["product_code"] if str(value or "").strip()}))

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
    approved = operating_repo.inspect_generation(operating_key, int(operating.generation_no or 0)).relational_snapshot
    direct_vs_approved = _snapshot_equality(direct, approved) if approved is not None else {}
    if approved is None or not all(direct_vs_approved.values()):
        raise AssertionError(
            "company 4 direct baseline must equal the approved 202609 Snapshot: "
            + json.dumps({
                "equality": direct_vs_approved,
                "direct_product_count": len(direct.frequency_products),
                "approved_product_count": len(approved.frequency_products) if approved is not None else None,
                "direct_event_count": direct.source_diagnostics.get("distinct_normal_event_count"),
                "approved_event_count": approved.source_diagnostics.get("distinct_normal_event_count") if approved is not None else None,
                "direct_source_rows": direct.source_diagnostics.get("source_row_count"),
                "approved_source_rows": approved.source_diagnostics.get("source_row_count") if approved is not None else None,
                "direct_source_fingerprint": direct.source_fingerprint,
                "approved_source_fingerprint": approved.source_fingerprint if approved is not None else None,
            }, sort_keys=True)
        )

    lifecycle_query, lifecycle_binds = product_lifecycle_sql(stock_codes=scope.stock_codes, cutoff_date=as_of_date)
    started = time.perf_counter()
    lifecycle_df = _query_company_df(company_id, lifecycle_query, lifecycle_binds, timeout_seconds)
    lifecycle_elapsed = time.perf_counter() - started
    lifecycle_rows = lifecycle_df.to_dict("records")

    monthly = []
    monthly_elapsed: dict[str, float] = {}
    for month in plan.basis_months:
        month_plan = build_frequency_month_source_plan(
            company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
        )
        rows, diagnostics, elapsed = _read_outbound(month_plan, timeout_seconds)
        monthly_elapsed[month] = elapsed
        monthly.append(build_monthly_frequency_materialization(
            company_id=company_id, basis_month=month, stock_codes=scope.stock_codes,
            monthly_rows=rows, source_diagnostics=diagnostics,
            source_watermark=None, source_watermark_status="unverified",
            product_codes=product_codes, lifecycle_rows=lifecycle_rows,
        ))

    repository = _repository()
    persisted = []
    write_started = time.perf_counter()
    for item in monthly:
        draft = repository.publish(item, created_by="monthly-frequency-phase3-company4")
        if draft.no_op and draft.status == "published" and draft.approval_status == "approved":
            approved_month = repository.read_current(item)
        else:
            approved_result = repository.approve_checked(
                item, draft.generation_no, expected_checksum=item.checksum,
                approved_by="monthly-frequency-phase3-company4",
                approval_reason="Phase 3 independent monthly materialization equality Gate",
            )
            if approved_result.status != "published" or approved_result.approval_status != "approved":
                raise AssertionError("monthly approval did not publish the exact draft")
            approved_month = repository.read_current(item)
        if approved_month.materialization != item:
            raise AssertionError("approved monthly readback differs from its immutable draft")
        persisted.append(approved_month)
    write_elapsed = time.perf_counter() - write_started

    read_started = time.perf_counter()
    reused = [repository.read_current(monthly[index]).materialization for index in (0, 1)]
    approved_read_elapsed = time.perf_counter() - read_started
    composed = compose_independent_monthly_window(
        extract_monthly_frequency_window(direct),
        (reused[0], reused[1], persisted[2].materialization),
    )
    rebuild_started = time.perf_counter()
    shadow = rebuild_frequency_snapshot_from_monthly_window(composed)
    rebuild_elapsed = time.perf_counter() - rebuild_started
    equality = _snapshot_equality(shadow, direct)
    if not all(equality.values()):
        raise AssertionError(f"independent monthly shadow differs from direct Snapshot: {equality}")

    inbound_months = {
        str(row.get("product_code") or ""): str(row.get("first_normal_inbound_month") or "")
        for row in lifecycle_rows if row.get("first_normal_inbound_month")
    }
    f_projection = project_new_product_frequency(
        shadow, first_normal_inbound_months=inbound_months,
        lifecycle_scope_fingerprint=shadow.key.scope_fingerprint,
    )
    lifecycle_counts = Counter(row.lifecycle_status for row in persisted[2].materialization.lifecycle)
    f_rows = [row for row in f_projection if row.frequency_grade == "F"]

    state_conn = connect_company_analytics_db(4, "reader")
    try:
        state_rows = state_conn.cursor().execute(
            """SELECT basis_month, generation_no, status, approval_status, COUNT(*) OVER (PARTITION BY basis_month) AS current_count
               FROM snapshot.frequency_month_manifest
               WHERE company_id=? AND scope_fingerprint=? AND schema_version=? AND algorithm_version=?
                 AND status='published'
               ORDER BY basis_month""",
            "4", direct.key.scope_fingerprint,
            monthly[0].monthly_schema_version, monthly[0].monthly_algorithm_version,
        ).fetchall()
    finally:
        state_conn.close()
    if len(state_rows) != 3 or any(int(row[4]) != 1 for row in state_rows):
        raise AssertionError("each monthly identity must have exactly one current approved generation")

    return {
        "company_id": company_id,
        "evaluation_month": evaluation_month,
        "basis_months": list(plan.basis_months),
        "scope": list(scope.stock_codes),
        "scope_fingerprint": direct.key.scope_fingerprint,
        "product_count": len(product_codes),
        "direct_source_rows": direct_diagnostics["source_row_count"],
        "direct_event_count": direct_diagnostics["distinct_normal_event_count"],
        "monthly": [
            {
                "basis_month": row.materialization.basis_month,
                "generation": row.generation_no,
                "manifest_id": row.manifest_id,
                "status": row.status,
                "approval_status": row.approval_status,
                "fact_count": len(row.materialization.facts),
                "source_fingerprint": row.materialization.source_fingerprint,
                "checksum": row.materialization.checksum,
                "source_watermark_status": row.materialization.source_watermark_status,
            }
            for row in persisted
        ],
        "shadow_equality": equality,
        "snapshot_checksum": shadow.checksum,
        "f_count": len(f_rows),
        "f_samples": [row.__dict__ for row in f_rows[:10]],
        "lifecycle_status_counts": dict(sorted(lifecycle_counts.items())),
        "lifecycle_evidence_counts": {
            "registered": sum(bool(row.product_registered_date) for row in persisted[2].materialization.lifecycle),
            "first_normal_inbound": sum(bool(row.first_normal_inbound_date) for row in persisted[2].materialization.lifecycle),
            "first_outbound": sum(bool(row.first_outbound_date) for row in persisted[2].materialization.lifecycle),
        },
        "elapsed_seconds": {
            "product_universe": round(universe_elapsed, 3),
            "direct_three_month": round(direct_elapsed, 3),
            "monthly_materialize": {key: round(value, 3) for key, value in monthly_elapsed.items()},
            "new_one_month_202608": round(monthly_elapsed["202608"], 3),
            "approved_two_month_read": round(approved_read_elapsed, 3),
            "combine_and_rebuild": round(rebuild_elapsed, 6),
            "lifecycle": round(lifecycle_elapsed, 3),
            "publish_approve_readback": round(write_elapsed, 3),
        },
        "operating_reader_switched": False,
        "operating_snapshot_generation": operating.generation_no,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency Phase 3 independent persistence Gate")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--as-of-date", default="20260906")
    parser.add_argument("--inspect-pending", action="store_true")
    args = parser.parse_args()
    _fixture_lifecycle_boundaries()
    print("PASS F boundaries M0/M0+1/M0+2/M0+3")
    if args.inspect_pending:
        print(json.dumps(inspect_pending_company4(), ensure_ascii=True, indent=2))
        return 0
    if args.company_id is not None:
        if args.company_id != 4:
            raise SystemExit("live Phase 3 Gate is restricted to company 4")
        print(json.dumps(run_company4(timeout_seconds=args.timeout_seconds, as_of_date=args.as_of_date), ensure_ascii=True, default=str, indent=2))
        print("PASS company 4 independent monthly persistence and shadow equality")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

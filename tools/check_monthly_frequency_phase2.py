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
    SnapshotContractError,
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
    build_monthly_frequency_materialization,
    compose_frequency_window,
    extract_monthly_frequency_window,
    first_normal_inbound_month_sql,
    project_new_product_frequency,
    rebuild_frequency_snapshot_from_monthly_window,
)


def _diagnostics(source_rows: int, accepted: int) -> dict[str, int]:
    return {
        "diagnostic_contract_version": 2,
        "source_row_count": source_rows,
        "normal_positive_accepted_row_count": accepted,
        "normal_positive_duplicate_row_count": source_rows - accepted,
        "normal_positive_conflicting_row_count": 0,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0,
        "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0,
        "other_tcode_row_count": 0,
        "normal_positive_row_count": source_rows,
        "distinct_normal_event_count": accepted,
        "conflicting_event_count": 0,
        "ignored_product_event_count": 0,
    }


def _fixture_snapshot():
    return build_relational_frequency_snapshot_from_aggregates(
        company_id=4,
        evaluation_month="202609",
        stock_codes=("00001",),
        product_codes=("M0", "M1", "M2", "M3", "NONE"),
        monthly_rows=(
            {"month": "202606", "product_code": "M3", "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 1, "outbound_day_count": 1},
            {"month": "202607", "product_code": "M2", "stock_code": "00001", "occurrence_count": 2, "outbound_quantity": 2, "outbound_day_count": 1},
            {"month": "202608", "product_code": "M1", "stock_code": "00001", "occurrence_count": 3, "outbound_quantity": 3, "outbound_day_count": 2},
            {"month": "202608", "product_code": "M0", "stock_code": "00001", "occurrence_count": 4, "outbound_quantity": 4, "outbound_day_count": 2},
        ),
        source_diagnostics=_diagnostics(11, 10),
    )


def test_one_month_materialization_and_two_month_reuse() -> None:
    source_plan = build_frequency_month_source_plan(
        company_id=4,
        basis_month="202602",
        stock_codes=("00001",),
    )
    assert source_plan.basis_from == "20260201"
    assert source_plan.basis_to == "20260228"
    assert source_plan.erp_sql_call_count == 1
    original = _fixture_snapshot()
    window = extract_monthly_frequency_window(original)
    august_rows = [
        fact.as_snapshot_row() for fact in window.facts if fact.basis_month == "202608"
    ]
    materialized = build_monthly_frequency_materialization(
        company_id=4,
        basis_month="202608",
        stock_codes=("00001",),
        monthly_rows=august_rows,
        source_diagnostics=_diagnostics(7, 7),
    )
    reused = [fact for fact in window.facts if fact.basis_month in {"202606", "202607"}]
    composed = compose_frequency_window(
        window,
        reused_months=("202606", "202607"),
        reused_facts=reused,
        materialized_month=materialized,
    )
    rebuilt = rebuild_frequency_snapshot_from_monthly_window(composed)
    assert _canonical_monthly(rebuilt) == _canonical_monthly(original)
    assert rebuilt.frequency_products == original.frequency_products
    assert rebuilt.source_fingerprint == original.source_fingerprint
    assert rebuilt.checksum == original.checksum
    assert materialized.erp_source_call_count == 1


def test_f_boundaries_and_legacy_preservation() -> None:
    original = _fixture_snapshot()
    projection = project_new_product_frequency(
        original,
        first_normal_inbound_months={
            "M0": "202609",
            "M1": "202608",
            "M2": "202607",
            "M3": "202606",
        },
        lifecycle_scope_fingerprint=original.key.scope_fingerprint,
    )
    by_product = {row.product_code: row for row in projection}
    assert by_product["M0"].frequency_grade == "F"
    assert by_product["M1"].frequency_grade == "F"
    assert by_product["M2"].frequency_grade == "F"
    assert by_product["M3"].frequency_grade == by_product["M3"].legacy_frequency_grade
    assert by_product["NONE"].frequency_grade == by_product["NONE"].legacy_frequency_grade
    assert by_product["NONE"].lifecycle_status == "insufficient"
    assert all(row.legacy_frequency_grade in {"A", "B", "C", "D", "E", "X"} for row in projection)


def test_f_scope_and_future_fail_closed() -> None:
    original = _fixture_snapshot()
    try:
        project_new_product_frequency(
            original,
            first_normal_inbound_months={},
            lifecycle_scope_fingerprint="0" * 64,
        )
    except SnapshotContractError:
        pass
    else:
        raise AssertionError("F projection must reject a different lifecycle scope")
    projected = project_new_product_frequency(
        original,
        first_normal_inbound_months={"M0": "202610"},
        lifecycle_scope_fingerprint=original.key.scope_fingerprint,
    )
    row = next(item for item in projected if item.product_code == "M0")
    assert row.lifecycle_status == "invalid_future"
    assert row.frequency_grade == row.legacy_frequency_grade
    sql, _ = first_normal_inbound_month_sql(
        stock_codes=("00001",),
        cutoff_date="20260906",
    )
    assert "Rd11_Io_Gu IN ('001', '002')" in sql
    assert "COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0" in sql
    assert "ISDATE(LTRIM(RTRIM(I.Rd11_In_YyMmDd))) = 1" in sql


def _read_outbound(plan, *, timeout_seconds: int) -> tuple[list[dict], dict[str, int], float]:
    sql, binds = outbound_event_grain_stream_sql(plan)
    started = time.perf_counter()
    rows, diagnostics = _aggregate_event_grain_chunks(
        _query_company_chunks(plan.company_id, sql, binds, timeout_seconds)
    )
    return rows, diagnostics, time.perf_counter() - started


def _canonical_products(snapshot) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted(
        (
            row.get("product_code"),
            int(row.get("occurrence_count_3m") or 0),
            row.get("frequency_grade"),
            row.get("data_status"),
        )
        for row in snapshot.frequency_products
    ))


def _canonical_monthly(snapshot) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted(
        (
            row.get("month"),
            row.get("product_code"),
            row.get("stock_code"),
            int(row.get("occurrence_count") or 0),
            int(row.get("outbound_quantity") or 0),
            int(row.get("outbound_day_count") or 0),
        )
        for row in snapshot.monthly_activity
    ))


def _snapshot_equality(left, right) -> dict[str, bool]:
    return {
        "key": left.key == right.key,
        "scope": left.stock_codes == right.stock_codes,
        "monthly_activity": _canonical_monthly(left) == _canonical_monthly(right),
        "product_event_grade": _canonical_products(left) == _canonical_products(right),
        "source_fingerprint": left.source_fingerprint == right.source_fingerprint,
        "checksum": left.checksum == right.checksum,
    }


def run_company4(*, timeout_seconds: int, as_of_date: str) -> dict[str, object]:
    company_id = 4
    evaluation_month = "202609"
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes,
    )

    universe_sql, universe_binds = product_universe_sql()
    universe_started = time.perf_counter()
    universe_df = _query_company_df(
        company_id, universe_sql, universe_binds, timeout_seconds
    )
    universe_elapsed = time.perf_counter() - universe_started
    product_codes = tuple(sorted({
        str(value or "").strip()
        for value in universe_df["product_code"].tolist()
        if str(value or "").strip()
    }))

    direct_rows, direct_diagnostics, direct_elapsed = _read_outbound(
        plan, timeout_seconds=timeout_seconds
    )
    direct_snapshot = build_relational_frequency_snapshot_from_aggregates(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes,
        product_codes=product_codes,
        monthly_rows=direct_rows,
        source_diagnostics=direct_diagnostics,
    )

    repository = _company_snapshot_repository(company_id)
    key = frequency_snapshot_key(plan)
    operating_key = repository.resolve_latest_eligible_key(key, available_through=as_of_date)
    current = repository.read(operating_key)
    inspection = repository.inspect_generation(operating_key, int(current.generation_no or 0))
    approved_snapshot = inspection.relational_snapshot
    if approved_snapshot is None:
        raise AssertionError("company 4 approved relational Snapshot is required")
    direct_vs_approved = _snapshot_equality(direct_snapshot, approved_snapshot)
    if not all(direct_vs_approved.values()):
        raise AssertionError(f"company 4 direct baseline differs from approved Snapshot: {direct_vs_approved}")

    month_plan = build_frequency_month_source_plan(
        company_id=company_id,
        basis_month="202608",
        stock_codes=scope.stock_codes,
    )
    august_rows, august_diagnostics, august_elapsed = _read_outbound(
        month_plan, timeout_seconds=timeout_seconds
    )
    august = build_monthly_frequency_materialization(
        company_id=company_id,
        basis_month="202608",
        stock_codes=scope.stock_codes,
        monthly_rows=august_rows,
        source_diagnostics=august_diagnostics,
    )
    approved_window = extract_monthly_frequency_window(approved_snapshot)
    reference_window = extract_monthly_frequency_window(direct_snapshot)
    reused_facts = tuple(
        fact for fact in approved_window.facts if fact.basis_month in {"202606", "202607"}
    )
    composed = compose_frequency_window(
        reference_window,
        reused_months=("202606", "202607"),
        reused_facts=reused_facts,
        materialized_month=august,
    )
    rebuild_started = time.perf_counter()
    combined_snapshot = rebuild_frequency_snapshot_from_monthly_window(composed)
    rebuild_elapsed = time.perf_counter() - rebuild_started
    combined_vs_direct = _snapshot_equality(combined_snapshot, direct_snapshot)
    if not all(combined_vs_direct.values()):
        raise AssertionError(f"company 4 monthly composition differs from direct baseline: {combined_vs_direct}")

    inbound_sql, inbound_binds = first_normal_inbound_month_sql(
        stock_codes=scope.stock_codes,
        cutoff_date=as_of_date,
    )
    inbound_started = time.perf_counter()
    inbound_df = _query_company_df(
        company_id, inbound_sql, inbound_binds, timeout_seconds
    )
    inbound_elapsed = time.perf_counter() - inbound_started
    inbound_months = {
        str(row.get("product_code") or "").strip(): str(row.get("first_normal_inbound_month") or "").strip()
        for row in inbound_df.to_dict("records")
        if str(row.get("product_code") or "").strip()
    }
    projection = project_new_product_frequency(
        combined_snapshot,
        first_normal_inbound_months=inbound_months,
        lifecycle_scope_fingerprint=combined_snapshot.key.scope_fingerprint,
    )
    legacy_by_product = {
        str(row.get("product_code") or ""): str(row.get("frequency_grade") or "")
        for row in combined_snapshot.frequency_products
    }
    if any(row.legacy_frequency_grade != legacy_by_product[row.product_code] for row in projection):
        raise AssertionError("F projection changed the preserved legacy frequency grade")
    f_rows = [row for row in projection if row.frequency_grade == "F"]
    lifecycle_counts = Counter(row.lifecycle_status for row in projection)
    grade_counts = Counter(row.frequency_grade for row in projection)

    direct_august = tuple(
        row for row in direct_snapshot.monthly_activity if row["month"] == "202608"
    )
    materialized_august = tuple(fact.as_snapshot_row() for fact in august.facts)
    if direct_august != materialized_august:
        raise AssertionError("independent August facts differ from the direct baseline")

    return {
        "company_id": company_id,
        "evaluation_month": evaluation_month,
        "basis_months": list(plan.basis_months),
        "scope": list(scope.stock_codes),
        "scope_fingerprint": combined_snapshot.key.scope_fingerprint,
        "approved_generation": current.generation_no,
        "product_universe_count": len(product_codes),
        "direct_source_rows": direct_diagnostics["source_row_count"],
        "direct_event_count": direct_diagnostics["distinct_normal_event_count"],
        "august_source_rows": august_diagnostics["source_row_count"],
        "august_event_count": august_diagnostics["distinct_normal_event_count"],
        "august_fact_rows": len(august.facts),
        "august_source_fingerprint": august.source_fingerprint,
        "snapshot_source_fingerprint": combined_snapshot.source_fingerprint,
        "snapshot_checksum": combined_snapshot.checksum,
        "direct_vs_approved": direct_vs_approved,
        "combined_vs_direct": combined_vs_direct,
        "elapsed_seconds": {
            "product_universe": round(universe_elapsed, 3),
            "direct_three_month_outbound": round(direct_elapsed, 3),
            "independent_one_month_outbound": round(august_elapsed, 3),
            "monthly_rebuild": round(rebuild_elapsed, 6),
            "first_normal_inbound": round(inbound_elapsed, 3),
        },
        "source_reduction": {
            "outbound_months_read_direct": 3,
            "outbound_months_read_incremental": 1,
            "erp_outbound_statement_count_direct": 1,
            "erp_outbound_statement_count_incremental": 1,
        },
        "f_projection": {
            "f_count": len(f_rows),
            "grade_counts": dict(sorted(grade_counts.items())),
            "lifecycle_status_counts": dict(sorted(lifecycle_counts.items())),
            "samples": [
                {
                    "product_code": row.product_code,
                    "first_normal_inbound_month": row.first_normal_inbound_month,
                    "legacy_frequency_grade": row.legacy_frequency_grade,
                    "frequency_grade": row.frequency_grade,
                    "occurrence_count_3m": row.occurrence_count_3m,
                }
                for row in f_rows[:10]
            ],
        },
        "source_watermark": combined_snapshot.source_watermark,
        "source_watermark_status": combined_snapshot.source_watermark_status,
        "db_write_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency Phase 2 and F contract Gate")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--as-of-date", default="20260906")
    args = parser.parse_args()
    tests = (
        test_one_month_materialization_and_two_month_reuse,
        test_f_boundaries_and_legacy_preservation,
        test_f_scope_and_future_fail_closed,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    if args.company_id is not None:
        if args.company_id != 4:
            raise SystemExit("live Phase 2 Gate is restricted to company 4")
        result = run_company4(
            timeout_seconds=max(1, int(args.timeout_seconds)),
            as_of_date=args.as_of_date,
        )
        print(json.dumps(result, ensure_ascii=True, default=str, indent=2))
        print("PASS company 4 monthly frequency Phase 2 exact equality and F projection")
    print(f"PASS monthly frequency Phase 2 fixtures ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

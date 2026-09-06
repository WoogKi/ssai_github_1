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
    SnapshotContractError,
    build_relational_frequency_snapshot_from_aggregates,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _company_snapshot_repository,
    build_frequency_snapshot_plan,
    frequency_snapshot_key,
    resolve_dashboard_profile_stock_scope,
)
from app.services.monthly_frequency_aggregate import (  # noqa: E402
    DEFAULT_RETENTION_MONTHS,
    MONTHLY_FREQUENCY_ALGORITHM_VERSION,
    MONTHLY_FREQUENCY_SCHEMA_VERSION,
    OPERATING_ANALYSIS_WINDOW_MONTHS,
    SUPPORTED_RETENTION_MONTHS,
    extract_monthly_frequency_window,
    rebuild_frequency_snapshot_from_monthly_window,
)


def _snapshot():
    return build_relational_frequency_snapshot_from_aggregates(
        company_id=4,
        evaluation_month="202609",
        stock_codes=("00001", "00008"),
        product_codes=("P1", "P2", "P3"),
        monthly_rows=(
            {"month": "202606", "product_code": "P1", "stock_code": "00001", "occurrence_count": 4, "outbound_quantity": 8, "outbound_day_count": 3},
            {"month": "202607", "product_code": "P1", "stock_code": "00008", "occurrence_count": 2, "outbound_quantity": 5, "outbound_day_count": 2},
            {"month": "202608", "product_code": "P2", "stock_code": "00001", "occurrence_count": 1, "outbound_quantity": 7, "outbound_day_count": 1},
        ),
        source_diagnostics={
            "diagnostic_contract_version": 2,
            "source_row_count": 9,
            "normal_positive_accepted_row_count": 7,
            "normal_positive_duplicate_row_count": 1,
            "normal_positive_conflicting_row_count": 0,
            "normal_positive_missing_key_row_count": 0,
            "normal_positive_nonintegral_row_count": 0,
            "normal_nonpositive_row_count": 1,
            "return_positive_row_count": 0,
            "return_nonpositive_row_count": 0,
            "other_tcode_row_count": 0,
            "normal_positive_row_count": 8,
            "distinct_normal_event_count": 7,
            "conflicting_event_count": 0,
            "ignored_product_event_count": 0,
        },
    )


def _expect_contract_error(window, message: str) -> None:
    try:
        rebuild_frequency_snapshot_from_monthly_window(window)
    except SnapshotContractError:
        return
    raise AssertionError(message)


def test_exact_snapshot_rebuild() -> None:
    original = _snapshot()
    window = extract_monthly_frequency_window(original)
    rebuilt = rebuild_frequency_snapshot_from_monthly_window(window)
    assert window.basis_months == ("202606", "202607", "202608")
    assert window.monthly_schema_version == MONTHLY_FREQUENCY_SCHEMA_VERSION
    assert window.monthly_algorithm_version == MONTHLY_FREQUENCY_ALGORITHM_VERSION
    assert window.erp_source_call_count == 0
    assert rebuilt.key == original.key
    assert rebuilt.monthly_activity == original.monthly_activity
    assert rebuilt.frequency_products == original.frequency_products
    assert rebuilt.source_fingerprint == original.source_fingerprint
    assert rebuilt.checksum == original.checksum
    assert sum(fact.occurrence_count for fact in window.facts) == 7
    assert sum(fact.outbound_quantity for fact in window.facts) == 20
    assert sum(fact.outbound_day_count for fact in window.facts) == 6


def test_empty_month_and_x_grade_are_preserved() -> None:
    original = _snapshot()
    window = extract_monthly_frequency_window(original)
    assert {fact.basis_month for fact in window.facts} == {"202606", "202607", "202608"}
    rebuilt = rebuild_frequency_snapshot_from_monthly_window(window)
    p3 = next(row for row in rebuilt.frequency_products if row["product_code"] == "P3")
    assert p3["occurrence_count_3m"] == 0
    assert p3["frequency_grade"] == "X"


def test_scope_universe_and_provenance_fail_closed() -> None:
    window = extract_monthly_frequency_window(_snapshot())
    wrong_scope = replace(window, scope_fingerprint="0" * 64)
    _expect_contract_error(wrong_scope, "scope mismatch must fail")

    wrong_universe = replace(window, product_codes=(*window.product_codes, "P4"))
    _expect_contract_error(wrong_universe, "universe mismatch must fail")

    wrong_source = replace(window, source_fingerprint="f" * 64)
    _expect_contract_error(wrong_source, "source provenance mismatch must fail")

    wrong_version = replace(window, monthly_algorithm_version="unknown")
    _expect_contract_error(wrong_version, "monthly contract version mismatch must fail")


def test_window_and_retention_contract() -> None:
    assert OPERATING_ANALYSIS_WINDOW_MONTHS == 12
    assert DEFAULT_RETENTION_MONTHS == 24
    assert SUPPORTED_RETENTION_MONTHS == (24, 36)


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


def run_live_equality(*, company_id: int, evaluation_month: str, as_of_date: str) -> dict[str, object]:
    scope = resolve_dashboard_profile_stock_scope(company_id=company_id)
    plan = build_frequency_snapshot_plan(
        company_id=company_id,
        evaluation_month=evaluation_month,
        stock_codes=scope.stock_codes,
    )
    key = frequency_snapshot_key(plan)
    repository = _company_snapshot_repository(company_id)
    started = time.perf_counter()
    operating_key = repository.resolve_latest_eligible_key(key, available_through=as_of_date)
    current = repository.read(operating_key)
    inspection = repository.inspect_generation(operating_key, int(current.generation_no or 0))
    analytics_read_elapsed = time.perf_counter() - started
    original = inspection.relational_snapshot
    if original is None:
        raise AssertionError("approved relational frequency snapshot is required")

    started = time.perf_counter()
    window = extract_monthly_frequency_window(original)
    rebuilt = rebuild_frequency_snapshot_from_monthly_window(window)
    rebuild_elapsed = time.perf_counter() - started
    month_rows = Counter(fact.basis_month for fact in window.facts)
    month_events = Counter()
    month_quantities = Counter()
    month_days = Counter()
    for fact in window.facts:
        month_events[fact.basis_month] += fact.occurrence_count
        month_quantities[fact.basis_month] += fact.outbound_quantity
        month_days[fact.basis_month] += fact.outbound_day_count
    exact = {
        "key": rebuilt.key == original.key,
        "scope": rebuilt.stock_codes == original.stock_codes,
        "monthly_activity": rebuilt.monthly_activity == original.monthly_activity,
        "product_event_grade": _canonical_products(rebuilt) == _canonical_products(original),
        "source_fingerprint": rebuilt.source_fingerprint == original.source_fingerprint,
        "checksum": rebuilt.checksum == original.checksum,
    }
    if not all(exact.values()):
        raise AssertionError(f"live monthly aggregate equality failed: {exact}")
    return {
        "company_id": company_id,
        "evaluation_month": evaluation_month,
        "generation_no": current.generation_no,
        "basis_months": list(window.basis_months),
        "scope": list(window.stock_codes),
        "scope_fingerprint": window.scope_fingerprint,
        "product_universe_count": len(window.product_codes),
        "product_universe_fingerprint": window.product_universe_fingerprint,
        "monthly": {
            month: {
                "fact_rows": month_rows[month],
                "event_count": month_events[month],
                "outbound_quantity": month_quantities[month],
                "outbound_day_count_sum": month_days[month],
            }
            for month in window.basis_months
        },
        "source_fingerprint": window.source_fingerprint,
        "source_watermark": window.source_watermark,
        "source_watermark_status": window.source_watermark_status,
        "snapshot_checksum": window.expected_snapshot_checksum,
        "exact_equality": exact,
        "analytics_read_elapsed_seconds": round(analytics_read_elapsed, 3),
        "monthly_rebuild_elapsed_seconds": round(rebuild_elapsed, 6),
        "erp_source_call_count": window.erp_source_call_count,
        "current_generator_erp_logical_call_count": 2,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly frequency aggregate phase-1 contract gate.")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--evaluation-month", default="202609")
    parser.add_argument("--as-of-date", default="20260906")
    args = parser.parse_args()
    tests = (
        test_exact_snapshot_rebuild,
        test_empty_month_and_x_grade_are_preserved,
        test_scope_universe_and_provenance_fail_closed,
        test_window_and_retention_contract,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    if args.company_id is not None:
        live = run_live_equality(
            company_id=args.company_id,
            evaluation_month=args.evaluation_month,
            as_of_date=args.as_of_date,
        )
        print(json.dumps(live, ensure_ascii=True, default=str, indent=2))
        print("PASS live approved snapshot monthly aggregate exact equality")
    print(f"PASS monthly frequency aggregate phase1 ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

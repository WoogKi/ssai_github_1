from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    EXTENDED_ALGORITHM_VERSION,
    EXTENDED_SCHEMA_VERSION,
    FrequencyProjectionReadResult,
    SnapshotContractError,
    build_extended_relational_frequency_snapshot_from_aggregates,
    build_relational_frequency_projection,
    build_relational_frequency_snapshot_from_aggregates,
    validate_relational_frequency_projection,
    validate_relational_frequency_snapshot,
)
from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _aggregate_extended_event_grain_chunks,
    build_frequency_snapshot_plan,
    frequency_snapshot_key,
    frequency_snapshot_read_keys,
    read_approved_frequency_projection,
)
from app.services.dashboard_lite_facts import _attach_inventory_status_and_frequency  # noqa: E402
from app.services.analytics_sales_trend_service import _attach_approved_outbound_characteristics  # noqa: E402
from app.services.ssai_analytics_snapshot_migration import MIGRATION_006_SQL, MIGRATION_007_SQL, MIGRATIONS  # noqa: E402
from app.services.ssai_snapshot_repository import SnapshotReadResult  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _stream() -> pd.DataFrame:
    events = [
        ("20260601", "00007", "P1", "00001"),
        ("20260601", "00007", "P1", "00008"),
        ("20260602", "00007", "P1", "00001"),
        ("20260603", "A-01", "P1", "00001"),
        ("20260603", "00009", "P2", "00001"),
    ]
    common = {
        "source_row_count": None,
        "normal_positive_row_count": None,
        "normal_positive_missing_key_row_count": None,
        "normal_positive_nonintegral_row_count": None,
        "normal_nonpositive_row_count": None,
        "return_positive_row_count": None,
        "return_nonpositive_row_count": None,
        "other_tcode_row_count": None,
    }
    rows = [
        {
            "row_kind": "event", "outbound_date": day, "vendor_code": vendor,
            "product_code": product, "stock_code": stock, "outbound_quantity": 1,
            "mapping_count": 1, "exact_duplicate_row_count": 0, **common,
        }
        for day, vendor, product, stock in events
    ]
    rows.append({
        "row_kind": "diagnostics", "outbound_date": "", "vendor_code": "",
        "product_code": "", "stock_code": "", "outbound_quantity": None,
        "mapping_count": None, "exact_duplicate_row_count": None,
        "source_row_count": 5, "normal_positive_row_count": 5,
        "normal_positive_missing_key_row_count": 0,
        "normal_positive_nonintegral_row_count": 0,
        "normal_nonpositive_row_count": 0, "return_positive_row_count": 0,
        "return_nonpositive_row_count": 0, "other_tcode_row_count": 0,
    })
    return pd.DataFrame(rows)


def _extended_snapshot(*, evaluation_month: str = "202607", first_months=None):
    monthly, diagnostics, days, customers = _aggregate_extended_event_grain_chunks((_stream(),))
    return build_extended_relational_frequency_snapshot_from_aggregates(
        company_id="04", evaluation_month=evaluation_month, monthly_rows=monthly,
        product_codes=("P1", "P2", "P3"), product_day_counts=days,
        product_customer_counts=customers,
        first_normal_inbound_months=first_months or {"P1": "202605", "P2": "202607"},
        stock_codes=("00001", "00008"), source_diagnostics=diagnostics,
    )


def test_product_distinct_counts_and_leading_zero() -> None:
    monthly, diagnostics, days, customers = _aggregate_extended_event_grain_chunks(
        (_stream().iloc[:2], _stream().iloc[2:])
    )
    _assert(sum(int(row["occurrence_count"]) for row in monthly if row["product_code"] == "P1") == 4, "legacy occurrence changed")
    _assert(days["P1"] == 3, "same date across stock locations must count once")
    _assert(customers["P1"] == 2, "same customer across dates/stocks must count once")
    _assert(diagnostics["distinct_normal_event_count"] == 5, "event universe changed")


def test_v2_lifecycle_precedence_and_raw_counts() -> None:
    snapshot = _extended_snapshot()
    validate_relational_frequency_snapshot(snapshot)
    _assert(snapshot.key.schema_version == EXTENDED_SCHEMA_VERSION, "v2 schema key missing")
    _assert(snapshot.key.algorithm_version == EXTENDED_ALGORITHM_VERSION, "v2 algorithm key missing")
    rows = {str(row["product_code"]): row for row in snapshot.frequency_products}
    _assert(rows["P1"]["frequency_grade"] == "F" and rows["P1"]["legacy_frequency_grade"] != "F", "M2 must override only final grade")
    _assert(rows["P1"]["occurrence_count_3m"] == 4 and rows["P1"]["outbound_day_count_3m"] == 3, "F must preserve raw counts")
    _assert(rows["P2"]["frequency_grade"] == "F" and rows["P2"]["row_status"] == "ready", "M0 must be F")
    _assert(rows["P3"]["frequency_grade"] == "X" and rows["P3"]["lifecycle_status"] == "unknown_lifecycle", "unknown lifecycle must not invent F")


def test_m0_m1_m2_m3_boundary() -> None:
    months = {"P1": "202607", "P2": "202606", "P3": "202605"}
    snapshot = _extended_snapshot(evaluation_month="202607", first_months=months)
    grades = {str(row["product_code"]): str(row["frequency_grade"]) for row in snapshot.frequency_products}
    _assert(grades == {"P1": "F", "P2": "F", "P3": "F"}, "M0-M2 must be F regardless of raw frequency")
    m3 = _extended_snapshot(evaluation_month="202608", first_months={"P1": "202605", "P2": "202605", "P3": "202605"})
    _assert(all(str(row["frequency_grade"]) == str(row["legacy_frequency_grade"]) for row in m3.frequency_products), "M3 must transition to legacy A-E/X")


def test_v1_checksum_and_projection_compatibility() -> None:
    v2 = _extended_snapshot()
    v1 = build_relational_frequency_snapshot_from_aggregates(
        company_id=v2.key.company_id, evaluation_month=v2.key.evaluation_month,
        monthly_rows=v2.monthly_activity, product_codes=(row["product_code"] for row in v2.frequency_products),
        stock_codes=v2.stock_codes, source_diagnostics=v2.source_diagnostics,
    )
    validate_relational_frequency_snapshot(v1)
    legacy = {str(row["product_code"]): (int(row["occurrence_count_3m"]), str(row["frequency_grade"])) for row in v1.frequency_products}
    extended = {str(row["product_code"]): (int(row["occurrence_count_3m"]), str(row["legacy_frequency_grade"])) for row in v2.frequency_products}
    _assert(legacy == extended, "the same active universe must produce the same pre-F occurrence/grade")
    v1_rows, v1_headers = build_relational_frequency_projection(v1)
    v2_rows, v2_headers = build_relational_frequency_projection(v2)
    validate_relational_frequency_projection(rows=v1_rows, headers=v1_headers, require_complete=True, key=v1.key)
    validate_relational_frequency_projection(rows=v2_rows, headers=v2_headers, require_complete=True, key=v2.key)


def test_versioned_keys_and_additive_migration() -> None:
    plan = build_frequency_snapshot_plan(company_id=4, evaluation_month="202607", stock_codes=("00001",))
    statistics, v2, v1 = frequency_snapshot_read_keys(plan)
    _assert(statistics.schema_version == "2.1", "product-statistics key missing")
    _assert(v1 == frequency_snapshot_key(plan), "legacy key default changed")
    _assert(v2 == frequency_snapshot_key(plan, extended=True), "extended key missing")
    migration = next(item for item in MIGRATIONS if item.migration_id == "006_frequency_product_lifecycle_extension")
    _assert("outbound_day_count_3m" in migration.sql and "outbound_customer_count_3m" in migration.sql, "additive columns missing")
    _assert("frequency_grade IN ('F','A','B','C','D','E','X')" in migration.sql, "F constraint missing")
    _assert("relational_frequency_v2" in migration.sql, "v2 manifest representation missing")


def test_migration_006_sql_server_batch_boundary() -> None:
    column_add = MIGRATION_006_SQL.index(
        "ALTER TABLE snapshot.frequency_product ADD outbound_day_count_3m"
    )
    dynamic_batch = MIGRATION_006_SQL.index("EXEC(N'", column_add)
    extended_constraint = MIGRATION_006_SQL.index(
        "CK_snapshot_frequency_product_extended_counts", dynamic_batch
    )
    dynamic_end = MIGRATION_006_SQL.index("');", extended_constraint)
    _assert(column_add < dynamic_batch < extended_constraint < dynamic_end, "006 additive constraints need a post-ADD compile boundary")
    _assert(
        "outbound_day_count_3m IS NULL" in MIGRATION_006_SQL[dynamic_batch:dynamic_end]
        and "outbound_customer_count_3m IS NULL" in MIGRATION_006_SQL[dynamic_batch:dynamic_end],
        "new-column constraints escaped the dynamic compile scope",
    )
    _assert(
        [migration.migration_id for migration in MIGRATIONS][-4:]
        == [
            "005_frequency_lifecycle_authority",
            "006_frequency_product_lifecycle_extension",
            "007_snapshot_profile_fingerprint",
            "008_frequency_product_statistics_extension",
        ],
        "005-to-007 sequential runner order changed",
    )
    _assert(
        "ADD profile_fingerprint CHAR(64) NULL" in MIGRATION_007_SQL
        and "CK_snapshot_manifest_profile_fingerprint" in MIGRATION_007_SQL,
        "007 additive profile fingerprint migration is incomplete",
    )


def test_migration_007_sql_server_batch_boundary_and_checksums() -> None:
    column_add = MIGRATION_007_SQL.index(
        "ALTER TABLE snapshot.manifest ADD profile_fingerprint CHAR(64) NULL"
    )
    dynamic_batch = MIGRATION_007_SQL.index("EXEC(N'", column_add)
    profile_constraint = MIGRATION_007_SQL.index(
        "CK_snapshot_manifest_profile_fingerprint", dynamic_batch
    )
    dynamic_end = MIGRATION_007_SQL.index("');", profile_constraint)
    _assert(
        column_add < dynamic_batch < profile_constraint < dynamic_end,
        "007 profile constraint needs a post-ADD SQL Server compile boundary",
    )
    _assert(
        "profile_fingerprint IS NULL OR LEN(profile_fingerprint) = 64"
        in MIGRATION_007_SQL[dynamic_batch:dynamic_end],
        "007 new-column constraint escaped the dynamic compile scope",
    )

    expected_immutable_checksums = {
        "001_snapshot_manifest_payload": "6ed8cab340afe83c2d9c7e2317cb3ed37cfa56e8f652df97d214b8b2ffc68dd5",
        "002_snapshot_frequency_projection": "a0fcbde060a4c22d5a31757d961651edfb319386287f478a4cdb65df9b0e6cfa",
        "003_snapshot_relational_frequency_authority": "6bbb2b73124d8de55c08ded5182e0ea734e8396eb2180433e5e48906c47e7c98",
        "004_monthly_frequency_materialization": "2a64b1102c32824529ee207da0713d61be3699e27848c30c0b806c48941148c6",
        "005_frequency_lifecycle_authority": "53a45cfe7250e057c71ada45f9164c6649258e5e4db35ca1f4f58c68119066bd",
        "006_frequency_product_lifecycle_extension": "4ecba9c40fd71757980e813be672ef9c47d6fb1a619201eb63b738f01e5b5cf9",
        "008_frequency_product_statistics_extension": "a8c13d4687f498a5ad416d2f60e92fbe0c56bdb03ac2b8a5f066a905c4244d5d",
    }
    actual_checksums = {migration.migration_id: migration.checksum for migration in MIGRATIONS}
    _assert(
        all(
            actual_checksums.get(migration_id) == checksum
            for migration_id, checksum in expected_immutable_checksums.items()
        ),
        "an immutable 001-006 or 008 migration checksum changed",
    )
    _assert(
        actual_checksums.get("007_snapshot_profile_fingerprint")
        == "28ed73d3fba92a29c5bf7025536a1fad564b930b28d020f8637cf9a996c36f20",
        "007 migration checksum changed without updating its focused contract",
    )


def test_dashboard_row_reason_contract() -> None:
    snapshot = _extended_snapshot()
    frequency_rows = tuple(snapshot.frequency_products)
    rows = [
        {"product_code": "P1", "inventory_current_stock_present": True, "evaluation_expected_demand_present": True, "current_stock_qty": 1, "evaluation_expected_demand_qty": 1},
        {"product_code": "MISSING", "inventory_current_stock_present": True, "evaluation_expected_demand_present": True, "current_stock_qty": 1, "evaluation_expected_demand_qty": 1},
    ]
    result = _attach_inventory_status_and_frequency(
        rows,
        frequency_snapshot=SnapshotReadResult(status="ready", generation_no=3, checksum="a" * 64),
        frequency_rows=frequency_rows,
    )
    by_code = {row["제품코드"]: row for row in result["detail_rows"]}
    _assert(by_code["P1"]["출고빈도등급"] == "F", "Dashboard must retain F")
    _assert(by_code["MISSING"]["출고빈도 자료상태"] == "product_not_in_projection", "ready Snapshot row absence is not stale")


def test_unavailable_result_dimensions() -> None:
    ready = FrequencyProjectionReadResult(status="ready", authority_status="ready", resolution_status="exact_match")
    missing = FrequencyProjectionReadResult(status="missing", authority_status="missing", resolution_status="scope_mismatch")
    version = FrequencyProjectionReadResult(status="missing", authority_status="version_mismatch", resolution_status="version_mismatch")
    _assert(ready.usable and not missing.usable and not version.usable, "authority status must stay fail-closed")


def test_v1_operating_fallback_and_scope_reason() -> None:
    class Repository:
        def resolve_latest_eligible_key(self, key, *, available_through):
            return key if key.schema_version == "1.0" else None

        def read_frequency_projection(self, key, **_kwargs):
            return FrequencyProjectionReadResult(
                status="ready",
                rows=({"product_code": "P1", "occurrence_count_3m": 2, "frequency_grade": "A", "data_status": "ready"},),
            )

    result = read_approved_frequency_projection(
        company_id=4, evaluation_month="202607", stock_codes=("00001",),
        as_of_date="20260731", repository=Repository(),
    )
    _assert(result.usable and result.contract_version == "1.0", "approved v1 fallback must remain usable")

    class MismatchRepository(Repository):
        def resolve_latest_eligible_key(self, key, *, available_through):
            return None

        def classify_frequency_resolution(self, _key):
            return "scope_mismatch"

    mismatch = read_approved_frequency_projection(
        company_id=4, evaluation_month="202607", stock_codes=("00001",),
        as_of_date="20260731", repository=MismatchRepository(),
    )
    _assert(not mismatch.usable and mismatch.resolution_status == "scope_mismatch", "scope mismatch reason lost")


def test_analytics_extended_projection_attachment() -> None:
    snapshot = _extended_snapshot()
    projection = FrequencyProjectionReadResult(status="ready", rows=tuple(snapshot.frequency_products))
    out = _attach_approved_outbound_characteristics(
        pd.DataFrame({"제품코드": ["P1", "P3"]}),
        {
            "stock_cd_list": ["00001", "00008"],
            "_period_source_policy": {"evaluation_month": "202607", "effective_date_to": "20260731"},
        },
        projection_reader=lambda **_kwargs: projection,
    )
    _assert(out["출고일수"].tolist() == [3, 0], "Analytics day projection missing")
    _assert(out["출고거래처수"].tolist() == [2, 0], "Analytics customer projection missing")
    _assert(out.attrs["outbound_characteristics_additional_erp_call_count"] == 0, "projection must not add ERP calls")


def main() -> int:
    tests = (
        test_product_distinct_counts_and_leading_zero,
        test_v2_lifecycle_precedence_and_raw_counts,
        test_m0_m1_m2_m3_boundary,
        test_v1_checksum_and_projection_compatibility,
        test_versioned_keys_and_additive_migration,
        test_migration_006_sql_server_batch_boundary,
        test_migration_007_sql_server_batch_boundary_and_checksums,
        test_dashboard_row_reason_contract,
        test_unavailable_result_dimensions,
        test_v1_operating_fallback_and_scope_reason,
        test_analytics_extended_projection_attachment,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS snapshot frequency lifecycle extension ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

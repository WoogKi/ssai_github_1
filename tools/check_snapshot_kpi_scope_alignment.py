from __future__ import annotations

import sys
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import (  # noqa: E402
    _select_snapshot_product_universe,
    build_frequency_snapshot_plan,
    frequency_snapshot_read_keys,
    outbound_base_rows_sql,
    read_approved_frequency_projection,
    resolve_dashboard_profile_stock_scope,
)
from app.services.dashboard_inventory_frequency_snapshot import (  # noqa: E402
    FrequencyProjectionReadResult,
    build_product_statistics_relational_snapshot_from_aggregates,
    dashboard_profile_fingerprint,
)
from app.services.monthly_frequency_aggregate import product_lifecycle_sql  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_profile_contract_reaches_snapshot_plan() -> None:
    profile = {
        "stock_cd_list": ["00008", "00001"],
        "product_group_list": ["0013:A001", "0013:9998"],
        "product_di_list": ["0004:01"],
        "product_class_list": ["0031:02"],
        "io_gu_list": ["001", "501"],
        "stock_mode": "book",
    }
    resolved = resolve_dashboard_profile_stock_scope(
        company_id=7,
        profile_loader=lambda company_id: SimpleNamespace(
            status="ready", profile=profile, reason_code="", company_id=company_id
        ),
    )
    plan = build_frequency_snapshot_plan(
        company_id=resolved.company_id,
        evaluation_month="202609",
        stock_codes=resolved.stock_codes,
        product_group_codes=resolved.product_group_codes,
        product_di_codes=resolved.product_di_codes,
        product_class_codes=resolved.product_class_codes,
        io_gu_codes=resolved.io_gu_codes,
        stock_mode=resolved.stock_mode,
    )
    _assert(plan.stock_codes == ("00001", "00008"), "stock codes lost leading zero or sort contract")
    _assert(plan.product_group_codes == ("0013:9998", "0013:A001"), "product group scope missing")
    _assert(plan.product_di_codes == ("0004:01",), "product type scope missing")
    _assert(plan.product_class_codes == ("0031:02",), "product class scope missing")
    _assert(plan.io_gu_codes == ("001", "501") and plan.stock_mode == "book", "profile metadata missing")
    _assert(
        frequency_snapshot_read_keys(plan)[0].profile_fingerprint == plan.profile_fingerprint,
        "v2 repository key lost the saved-profile identity",
    )


def test_profile_fingerprint_is_canonical_and_sensitive() -> None:
    first = dashboard_profile_fingerprint(
        stock_codes=("00008", "00001", "00008"),
        product_group_codes=("0013:A001", "0013:9998"),
        product_di_codes=(),
        product_class_codes=None,
        stock_mode="real",
    )
    reordered = dashboard_profile_fingerprint(
        stock_codes=("00001", "00008"),
        product_group_codes=("0013:9998", "0013:A001"),
        product_di_codes=None,
        product_class_codes=(),
        stock_mode="real",
    )
    changed = dashboard_profile_fingerprint(
        stock_codes=("00001", "00008"),
        product_group_codes=("0013:9998",),
        product_di_codes=(),
        product_class_codes=(),
        stock_mode="real",
    )
    without_zero = dashboard_profile_fingerprint(
        stock_codes=("1", "00008"),
        product_group_codes=("0013:9998", "0013:A001"),
        product_di_codes=(),
        product_class_codes=(),
        stock_mode="real",
    )
    _assert(first == reordered, "equivalent reordered profile conditions changed fingerprint")
    _assert(first != changed, "saved product condition change did not change fingerprint")
    _assert(first != without_zero, "profile fingerprint coerced a leading-zero code")
    company4 = build_frequency_snapshot_plan(
        company_id=4, evaluation_month="202609", stock_codes=("00001",),
    )
    company7 = build_frequency_snapshot_plan(
        company_id=7, evaluation_month="202609", stock_codes=("00001",),
    )
    key4 = frequency_snapshot_read_keys(company4)[0]
    key7 = frequency_snapshot_read_keys(company7)[0]
    _assert(
        key4.profile_fingerprint == key7.profile_fingerprint and key4 != key7,
        "profile content must be deterministic while repository keys remain company-isolated",
    )


def test_profile_mismatch_fails_closed_and_v1_fallback_survives() -> None:
    class ProfileMismatchRepository:
        def resolve_latest_eligible_key(self, key, *, available_through):
            return None

        def classify_frequency_resolution(self, key):
            _assert(bool(key.profile_fingerprint), "v2 classifier did not receive profile fingerprint")
            return "profile_mismatch"

    mismatch = read_approved_frequency_projection(
        company_id=7,
        evaluation_month="202609",
        stock_codes=("00001",),
        product_group_codes=("0013:9998",),
        as_of_date="20260911",
        repository=ProfileMismatchRepository(),
    )
    _assert(
        not mismatch.usable and mismatch.resolution_status == "profile_mismatch",
        "profile mismatch did not fail closed with an explicit reason",
    )

    class LegacyFallbackRepository(ProfileMismatchRepository):
        def resolve_latest_eligible_key(self, key, *, available_through):
            return key if key.schema_version == "1.0" else None

        def read_frequency_projection(self, key, **_kwargs):
            return FrequencyProjectionReadResult(status="ready", rows=())

    fallback = read_approved_frequency_projection(
        company_id=7,
        evaluation_month="202609",
        stock_codes=("00001",),
        product_group_codes=("0013:9998",),
        as_of_date="20260911",
        repository=LegacyFallbackRepository(),
    )
    _assert(fallback.usable and fallback.contract_version == "1.0", "v1 operating fallback regressed")


def test_two_statement_sql_carries_scope_and_evidence() -> None:
    plan = build_frequency_snapshot_plan(
        company_id=7,
        evaluation_month="202609",
        stock_codes=("00001",),
        product_group_codes=("0013:9998",),
        product_di_codes=("0004:01",),
        product_class_codes=("0031:02",),
        io_gu_codes=("501",),
        stock_mode="real",
    )
    lifecycle_sql, lifecycle_binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes,
        cutoff_date="20260911",
        basis_from=plan.basis_from,
        basis_to=plan.basis_to,
        stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes,
        product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
    )
    outbound_sql, outbound_binds = outbound_base_rows_sql(plan)
    for marker in (
        "Rd04_Physic_Group_Gcode", "Rd04_Physic_Di_Gcode", "Rd04_Physic_Tax_Gcode",
        "basis_inbound_present", "current_stock_present", "dbo.Rddbc210",
    ):
        _assert(marker in lifecycle_sql, f"lifecycle SQL missing {marker}")
    _assert(lifecycle_binds["stock_month_to"] == "202608", "mid-month current stock must use prior monthly close")
    _assert(lifecycle_binds["use_current_detail"] == 1, "mid-month detail adjustment missing")
    _assert("INNER JOIN dbo.Rddbc040 AS P" in outbound_sql, "outbound source lacks profile product scope")
    _assert(outbound_binds["outbound_group_t_0"] == "9998", "outbound product-group bind missing")
    _assert("outbound_io" not in " ".join(outbound_binds), "saved IO list must not redefine canonical 500-599 frequency")
    _assert(plan.erp_sql_call_count == 2, "ERP statement contract changed")
    _assert("FirstOutbound" in lifecycle_sql and "first_outbound_date" in lifecycle_sql, "legacy lifecycle provenance changed")
    v21_sql, _ = product_lifecycle_sql(
        stock_codes=plan.stock_codes,
        cutoff_date="20260911",
        basis_from=plan.basis_from,
        basis_to=plan.basis_to,
        stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes,
        product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
        include_first_outbound=False,
    )
    _assert("FirstOutbound" not in v21_sql and "first_outbound_date" not in v21_sql, "v2.1 lifecycle query retained unused lifetime outbound scan")
    company8_sql, company8_binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes,
        cutoff_date="20260911",
        basis_from=plan.basis_from,
        basis_to=plan.basis_to,
        stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes,
        product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
        include_first_outbound=False,
        force_hash_join=True,
    )
    _assert("OPTION (HASH JOIN)" not in v21_sql, "default lifecycle join strategy changed")
    _assert(company8_sql.endswith("OPTION (HASH JOIN)"), "company8 lifecycle join strategy missing")
    _assert("ORDER BY P.product_code" in company8_sql, "company8 lifecycle result order changed")
    _assert(company8_binds == {**lifecycle_binds}, "join strategy must not change binds")
    v3_sql, v3_binds = product_lifecycle_sql(
        stock_codes=plan.stock_codes,
        cutoff_date="20260911",
        basis_from=plan.basis_from,
        basis_to=plan.basis_to,
        stock_mode=plan.stock_mode,
        product_group_codes=plan.product_group_codes,
        product_di_codes=plan.product_di_codes,
        product_class_codes=plan.product_class_codes,
        include_first_outbound=False,
        snapshot_projection_only=True,
    )
    _assert(v3_binds == lifecycle_binds, "v3 lifecycle bind contract changed")
    _assert(v3_sql.endswith("ORDER BY P.product_code"), "v3 lifecycle row order changed")
    for unused in ("COUNT_BIG(*) OVER ()", "Rd04_Add_Date", "P.product_registered_date", "I.first_normal_inbound_date,\n"):
        _assert(unused not in v3_sql, f"v3 lifecycle retained unused expression: {unused}")
        _assert(unused in lifecycle_sql, f"legacy lifecycle expression changed: {unused}")
    for required in (
        "AS first_normal_inbound_month", "AS current_stock_present", "AS basis_inbound_present",
        "AS avg_purchase_unit_cost", "AS purchase_price_basis_month", "Rd04_Physic_Group_Gcode",
        "Rd04_Physic_Di_Gcode", "Rd04_Physic_Tax_Gcode", "dbo.Rddbc210",
    ):
        _assert(required in v3_sql, f"v3 lifecycle lost required evidence: {required}")
    projection = v3_sql.split("\nSELECT P.product_code", 1)[1].split("\nFROM ProductUniverse AS P", 1)[0]
    columns = (
        "P.product_group_key", "P.product_di_key", "P.product_class_key",
        "AS first_normal_inbound_month", "AS current_stock_present", "AS basis_inbound_present",
        "AS avg_purchase_unit_cost", "AS purchase_price_basis_month",
    )
    _assert([projection.index(column) for column in columns] == sorted(projection.index(column) for column in columns),
            "v3 lifecycle projection column order changed")


def test_union_product_universe_and_leading_zero() -> None:
    lifecycle = pd.DataFrame(
        [
            {"product_code": "00001", "current_stock_present": 1, "basis_inbound_present": 0},
            {"product_code": "00002", "current_stock_present": 0, "basis_inbound_present": 1},
            {"product_code": "00003", "current_stock_present": 0, "basis_inbound_present": 0},
            {"product_code": "00004", "current_stock_present": 0, "basis_inbound_present": 0},
        ]
    )
    monthly = [{"product_code": "00003", "occurrence_count": 1}]
    products, diagnostics = _select_snapshot_product_universe(lifecycle, monthly)
    _assert(products == ["00001", "00002", "00003"], "stock/inbound/outbound union is incorrect")
    _assert("00004" not in products, "R040-only product leaked into snapshot universe")
    _assert(diagnostics == {
        "profile_product_count": 4,
        "current_stock_product_count": 1,
        "basis_inbound_product_count": 1,
        "basis_outbound_product_count": 1,
        "eligible_product_count": 3,
    }, "product universe diagnostics changed")


def test_v3_unused_lifecycle_columns_do_not_change_snapshot() -> None:
    legacy = pd.DataFrame([
        {
            "product_code": "00001", "current_stock_present": 1, "basis_inbound_present": 0,
            "first_normal_inbound_month": "202606", "avg_purchase_unit_cost": "10",
            "purchase_price_basis_month": "202608", "product_registered_date": "20260101",
            "first_normal_inbound_date": "20260603", "profile_product_count": 2,
        },
        {
            "product_code": "00002", "current_stock_present": 0, "basis_inbound_present": 1,
            "first_normal_inbound_month": "202608", "avg_purchase_unit_cost": "20",
            "purchase_price_basis_month": "202608", "product_registered_date": "20260201",
            "first_normal_inbound_date": "20260809", "profile_product_count": 2,
        },
    ])
    lean = legacy.drop(columns=["product_registered_date", "first_normal_inbound_date", "profile_product_count"])

    def snapshot(frame: pd.DataFrame):
        product_codes, diagnostics = _select_snapshot_product_universe(frame, ())
        rows = frame.set_index("product_code")
        result = build_product_statistics_relational_snapshot_from_aggregates(
            company_id=7, evaluation_month="202609", monthly_rows=(
                {"month": "202608", "product_code": "00001", "stock_code": "00001", "occurrence_count": 1,
                 "outbound_quantity": 2, "outbound_day_count": 1},
            ),
            product_codes=product_codes, product_day_counts={}, product_customer_counts={},
            first_normal_inbound_months=rows["first_normal_inbound_month"].to_dict(),
            outbound_paid_quantities={"00001": 2}, return_statistics={},
            purchase_prices={code: {"unit_price": rows.at[code, "avg_purchase_unit_cost"],
                                     "basis_month": rows.at[code, "purchase_price_basis_month"], "status": "ready"}
                             for code in product_codes},
            sales_prices={"00001": {"unit_price": "15", "status": "ready"}}, stock_codes=("00001",),
        )
        return diagnostics, result.checksum, result.frequency_products

    _assert(snapshot(legacy) == snapshot(lean), "v3 lifecycle projection changed product universe or checksum")


def test_verified_char5_monthly_stock_seek_preserves_projection() -> None:
    options = dict(
        stock_codes=("00001",), cutoff_date="20260920", basis_from="20260601",
        basis_to="20260831", stock_mode="real", include_first_outbound=False,
        snapshot_projection_only=True,
    )
    original_sql, original_binds = product_lifecycle_sql(**options)
    seek_sql, seek_binds = product_lifecycle_sql(**options, seek_char5_monthly_stock=True)
    _assert(seek_binds == original_binds, "company12 R210 seek changed lifecycle binds")
    _assert("OUTER APPLY" in seek_sql and "MonthlyStock AS" not in seek_sql,
            "company12 R210 seek did not move the aggregation below ProductUniverse")
    _assert("GROUP BY LTRIM(RTRIM(M.Rd21_Physic_Cd))" in original_sql,
            "default lifecycle stock aggregation changed")
    projection = lambda sql: sql.split("\nSELECT P.product_code", 1)[1].split("\nFROM ProductUniverse AS P", 1)[0]
    _assert(projection(seek_sql) == projection(original_sql), "company12 lifecycle projection changed")
    _assert(seek_sql.endswith("ORDER BY P.product_code"), "company12 lifecycle sort changed")
    _assert(seek_sql.count("dbo.Rddbc210") == 1, "company12 R210 source count changed")
    for count in range(1, 5):
        _assert(f"REPLICATE(CHAR(32), {count}) + P.product_code" in seek_sql,
                "company12 char(5) leading-space variant missing")

    values = (" ", "0", "A")
    raw_codes = ["".join(chars) for chars in product(values, repeat=5)]
    for raw_product in raw_codes:
        key = raw_product.strip(" ")
        if not key:
            continue
        for raw_stock in raw_codes:
            old_match = raw_stock.strip(" ") == key
            new_match = any(raw_stock.rstrip(" ") == (" " * count + key).rstrip(" ")
                            for count in range(5))
            _assert(old_match == new_match, "char(5) seek changes normalized stock product matching")

    stock_rows = (("0001 ", 2), (" 0001", -1), ("00002", 0))

    def snapshot(company_id: int, use_seek: bool):
        rows = []
        for code, basis_inbound in (("0001", 0), ("00002", 1), ("00003", 0)):
            quantity = sum(
                amount for raw_code, amount in stock_rows
                if (any(raw_code.rstrip(" ") == (" " * count + code).rstrip(" ")
                        for count in range(5)) if use_seek else raw_code.strip(" ") == code)
            )
            rows.append({
                "product_code": code, "current_stock_present": int(quantity != 0),
                "basis_inbound_present": basis_inbound,
            })
        product_codes, diagnostics = _select_snapshot_product_universe(pd.DataFrame(rows), ())
        result = build_product_statistics_relational_snapshot_from_aggregates(
            company_id=company_id, evaluation_month="202609", monthly_rows=(
                {"month": "202608", "product_code": "0001", "stock_code": "00001",
                 "occurrence_count": 1, "outbound_quantity": 2, "outbound_day_count": 1},
            ),
            product_codes=product_codes, product_day_counts={}, product_customer_counts={},
            first_normal_inbound_months={"0001": "202606", "00002": "202608"},
            outbound_paid_quantities={"0001": 2}, return_statistics={},
            purchase_prices={"0001": {"unit_price": "10", "basis_month": "202608", "status": "ready"},
                             "00002": {"unit_price": "20", "basis_month": "202608", "status": "ready"}},
            sales_prices={"0001": {"unit_price": "15", "status": "ready"}},
            stock_codes=("00001",),
        )
        return product_codes, diagnostics, result.checksum

    for company_id in (12, 13):
        _assert(snapshot(company_id, False) == snapshot(company_id, True),
                f"company{company_id} R210 seek changed universe or checksum")


def main() -> int:
    tests = (
        test_profile_contract_reaches_snapshot_plan,
        test_profile_fingerprint_is_canonical_and_sensitive,
        test_profile_mismatch_fails_closed_and_v1_fallback_survives,
        test_two_statement_sql_carries_scope_and_evidence,
        test_union_product_universe_and_leading_zero,
        test_v3_unused_lifecycle_columns_do_not_change_snapshot,
        test_verified_char5_monthly_stock_seek_preserves_projection,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS snapshot KPI scope alignment {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

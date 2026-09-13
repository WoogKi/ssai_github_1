from __future__ import annotations

import sys
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


def main() -> int:
    tests = (
        test_profile_contract_reaches_snapshot_plan,
        test_profile_fingerprint_is_canonical_and_sensitive,
        test_profile_mismatch_fails_closed_and_v1_fallback_survives,
        test_two_statement_sql_carries_scope_and_evidence,
        test_union_product_universe_and_leading_zero,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS snapshot KPI scope alignment {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

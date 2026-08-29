"""Fixture equality gate for the supported Dashboard product-group scope."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_narrow_sales_purchase_bundle
from app.services.dashboard_lite_facts import _filter_sales_source_for_dashboard
from app.services.dashboard_narrow_sales_candidate_service import _queries, can_use_dashboard_narrow_sales_candidate


COMPANY8_DEFAULT_PRODUCT_GROUPS = ["0013:9998", "0013:9999"]


def _params() -> dict[str, object]:
    return {
        "company_id": "8", "source_mode": "monthly_book",
        "month_from": "202507", "month_to": "202608", "evaluation_month": "202608",
        "date_from": "20250701", "date_to": "20260825", "policy_date": "20260825",
        "stock_cd_list": ["00001", "00008", "00013"],
        "product_group_list": COMPANY8_DEFAULT_PRODUCT_GROUPS,
        "dashboard_product_group_list": COMPANY8_DEFAULT_PRODUCT_GROUPS,
    }


def _sales_fixture() -> pd.DataFrame:
    rows = [
        ("202607", " P9998 ", "0013", "9998", "제품A", "제조사A", "V1", 10, 100, 10, 110),
        ("202608", "P9999", "0013", "9999", "제품B", "제조사B", "V2", -2, -20, -2, -22),
        ("202608", "POTHER", "0013", "0001", "제품외", "제조사외", "V3", 30, 300, 30, 330),
        ("202608", "PBLANK", "", "", "제품공백", "", "V4", 4, 40, 4, 44),
    ]
    return pd.DataFrame(rows, columns=[
        "기준월", "제품코드", "제품그룹Gcode", "제품그룹코드", "제품명", "제조사명", "매입처코드",
        "출고수량", "매출공급가액", "매출세액", "매출합계",
    ]).assign(규격="EA", 제조사코드="", 출고할증수량=0, 집계건수=1)


def _purchase_fixture() -> pd.DataFrame:
    rows = [
        ("202607", "P9998", "V1", "매입처1", 5, 50, 1),
        ("202608", " P9999 ", "V2", "매입처2", -1, -10, 1),
        ("202608", "POTHER", "V3", "매입처외", 9, 90, 1),
        ("202608", "PONLY", "V5", "매입전용", 7, 70, 1),
        ("", "P9998", "V1", "매입처1", 1, 10, 1),
    ]
    return pd.DataFrame(rows, columns=["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"])


def _candidate_scope(sales: pd.DataFrame, purchase: pd.DataFrame, params: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Mirror the SQL boundary: group universe, then sales/purchase intersection."""
    scoped_sales = _filter_sales_source_for_dashboard(sales, params)
    allowed = set(scoped_sales["제품코드"].fillna("").astype(str).str.strip())
    scoped_purchase = purchase.loc[
        purchase["제품코드"].fillna("").astype(str).str.strip().isin(allowed)
    ].copy()
    return scoped_sales, scoped_purchase


def main() -> int:
    params = _params()
    assert can_use_dashboard_narrow_sales_candidate(params) == (True, "monthly_product_group_contract")
    unsupported = dict(params, product_di_list=["0004:D1"])
    assert can_use_dashboard_narrow_sales_candidate(unsupported) == (False, "product_dimension_filter_contract")

    sales = _sales_fixture()
    purchase = _purchase_fixture()
    legacy_sales = _filter_sales_source_for_dashboard(sales, params)
    legacy_purchase = purchase.loc[
        purchase["제품코드"].fillna("").astype(str).str.strip().isin(
            set(legacy_sales["제품코드"].fillna("").astype(str).str.strip())
        )
    ].copy()
    candidate_sales, candidate_purchase = _candidate_scope(sales, purchase, params)
    pd.testing.assert_frame_equal(legacy_sales, candidate_sales, check_dtype=True)
    pd.testing.assert_frame_equal(legacy_purchase, candidate_purchase, check_dtype=True)
    assert candidate_sales["제품코드"].tolist() == [" P9998 ", "P9999"]
    assert candidate_purchase["제품코드"].tolist() == ["P9998", " P9999 ", "P9998"]

    legacy_bundle = build_dashboard_narrow_sales_purchase_bundle(
        legacy_sales, legacy_purchase, evaluation_month="202608", history_month_from="202507"
    )
    candidate_bundle = build_dashboard_narrow_sales_purchase_bundle(
        candidate_sales, candidate_purchase, evaluation_month="202608", history_month_from="202507"
    )
    for name in (
        "product_month_sales_df", "product_identity_df", "manufacturer_month_df",
        "sales_month_total_df", "purchase_month_total_df",
    ):
        pd.testing.assert_frame_equal(getattr(legacy_bundle, name), getattr(candidate_bundle, name), check_dtype=True)
    assert legacy_bundle.purchase_diagnostics == candidate_bundle.purchase_diagnostics

    queries, meta = _queries(params)
    assert meta["product_scope_applied"] is True
    for name in ("product_month_sales", "product_identity", "manufacturer_vendor_relation"):
        assert "FilteredProducts AS" in queries[name][0], name
        assert "INNER JOIN FilteredProducts AS FP ON FP.제품코드 = M.Rd22_Physic_Cd" in queries[name][0], name
        assert "FP.제품코드 = COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR" not in queries[name][0], name
    assert "FilteredProducts AS" in queries["purchase_facts"][0]
    assert "INNER JOIN FilteredProducts AS FP ON FP.제품코드 = M.Rd22_Physic_Cd" in queries["purchase_facts"][0]
    assert "FP.제품코드 = COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR" not in queries["purchase_facts"][0]
    assert "COALESCE(LTRIM(RTRIM(CONVERT(NVARCHAR(255), 제품코드))), N'') AS 제품코드" in queries["product_month_sales"][0]
    assert "ScopedPurchaseRows AS" in queries["purchase_facts"][0]
    assert "MAX(CASE WHEN" in queries["purchase_facts"][0]
    assert "OVER (PARTITION BY" in queries["purchase_facts"][0]
    assert "has_sales_product = 1" in queries["purchase_facts"][0]
    assert "FilteredSalesProducts AS" not in queries["purchase_facts"][0]
    sales_universe_sql = queries["purchase_facts"][0].split("PurchaseGrouped AS", 1)[0]
    assert "M.Rd22_Stock_YyMm" in sales_universe_sql
    assert "M.Rd22_Physic_Cd" in sales_universe_sql
    assert "S.Rd22_Physic_Cd" not in sales_universe_sql
    print("PASS: narrow product-group scope matches legacy sales universe and purchase intersection fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

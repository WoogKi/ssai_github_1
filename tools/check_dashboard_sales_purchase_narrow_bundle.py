"""Offline equality gate for the narrow Dashboard sales/purchase bundle."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_manufacturer_sales_trend_service import get_manufacturer_sales_trend_summary
from app.services.analytics_sales_trend_service import (
    adapt_dashboard_narrow_bundle_for_forecast,
    adapt_dashboard_narrow_bundle_for_manufacturer,
    adapt_dashboard_narrow_bundle_for_visual,
    build_dashboard_narrow_sales_purchase_bundle,
    build_dashboard_narrow_bundle_from_projections,
    get_sales_forecast_df,
)
from app.services.dashboard_lite_facts import (
    _build_demand_surge_history_by_product,
    _build_visual_phase2_summary,
    _monthly_sales_actuals_from_source,
    _monthly_sales_returns_from_source,
)


PARAMS = {
    "month_from": "202501",
    "month_to": "202607",
    "evaluation_month": "202607",
    "policy_date": "20260731",
    "source_mode": "monthly_book",
}


def _sales_fixture() -> pd.DataFrame:
    rows = [
        ("202501", "P1", "제품1", "규격1", "M1", "제조사1", "V2", "10", "1", "100", "10", "110", "0", "1"),
        ("202501", "P1", "제품1", "규격1", "M1", "제조사1", "V1", "5", "0", "50", "5", "55", "0", "1"),
        ("202502", "P1", "제품1", "규격1", "M1", "제조사1", "V1", "25", "2", "250", "25", "275", "0", "2"),
        ("202502", "P2", "제품2", "규격2", "M1", "제조사1", "V3", "8", "0", "80", "8", "88", "0", "1"),
        ("202503", "P2", "제품2", "규격2", "M2", "제조사2", "V4", "-2", "0", "-20", "-2", "-22", "22", "1"),
    ]
    return pd.DataFrame(rows, columns=[
        "기준월", "제품코드", "제품명", "규격", "제조사코드", "제조사명", "매입처코드",
        "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "매출반품금액", "집계건수",
    ])


def _purchase_fixture() -> pd.DataFrame:
    rows = [
        ("202501", "P1", "A", "매입처A", "10", "100", "1"),
        ("202502", "P1", "A", "매입처A", "0", "0", "0"),
        ("202502", "P2", "B", "매입처B", "2", "30", "1"),
        ("", "P3", "C", "매입처C", "1", "10", "1"),
        ("202502", "", "D", "매입처D", "1", "10", "1"),
        ("202502", "P4", "E", "매입처E", "bad", "10", "1"),
        ("202607", "P5", "F", "매입처F", "1", "10", "1"),
    ]
    return pd.DataFrame(rows, columns=["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"])


def _sorted(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return frame.reindex(columns=columns).sort_values(columns[:2], kind="stable").reset_index(drop=True)


def _projection_frames(bundle: object) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    identity = bundle.product_identity_df.copy()
    sales_columns = [
        "projection_kind", "기준월", "__dashboard_product_identity_id", "제품코드", "제약사명",
        "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "매출반품금액", "집계건수", "제품수", "매입처수",
    ]
    product_month = bundle.product_month_sales_df.copy()
    product_month["projection_kind"] = "product_month_sales"
    product_month["제약사명"] = ""
    product_month["제품수"] = 0
    product_month["매입처수"] = 0
    manufacturer = bundle.manufacturer_month_df.copy()
    manufacturer["projection_kind"] = "manufacturer_month"
    manufacturer["__dashboard_product_identity_id"] = ""
    manufacturer["제품코드"] = ""
    manufacturer["출고수량"] = 0
    manufacturer["출고할증수량"] = 0
    sales_total = bundle.sales_month_total_df.copy()
    sales_total["projection_kind"] = "sales_month_total"
    for column in ("__dashboard_product_identity_id", "제품코드", "제약사명", "출고수량", "출고할증수량", "매출공급가액", "매출세액", "집계건수", "제품수", "매입처수"):
        sales_total[column] = 0 if column not in {"__dashboard_product_identity_id", "제품코드", "제약사명"} else ""
    sales = pd.concat([product_month, manufacturer, sales_total], ignore_index=True).reindex(columns=sales_columns)

    diagnostic_columns = [
        "purchase_source_rows", "purchase_positive_rows", "purchase_nonpositive_rows", "purchase_unclassified_rows",
        "missing_product_code_rows", "missing_month_rows", "invalid_numeric_rows", "other_excluded_rows",
    ]
    purchase_total = bundle.purchase_month_total_df.copy()
    purchase_total["projection_kind"] = "purchase_month_total"
    for column in diagnostic_columns:
        purchase_total[column] = 0
    diagnostics = pd.DataFrame([{"projection_kind": "purchase_diagnostics", "기준월": "", "매입금액": 0, **bundle.purchase_diagnostics}])
    purchase = pd.concat([purchase_total, diagnostics], ignore_index=True).reindex(columns=["projection_kind", "기준월", "매입금액", *diagnostic_columns])
    return identity, sales, purchase


def main() -> int:
    sales = _sales_fixture()
    purchase = _purchase_fixture()
    bundle = build_dashboard_narrow_sales_purchase_bundle(
        sales,
        purchase,
        evaluation_month="202607",
        history_month_from="202501",
    )
    projection_identity, projection_sales, projection_purchase = _projection_frames(bundle)
    rebuilt = build_dashboard_narrow_bundle_from_projections(
        projection_identity,
        projection_sales,
        projection_purchase,
    )
    pd.testing.assert_frame_equal(bundle.product_identity_df, rebuilt.product_identity_df, check_dtype=True)
    pd.testing.assert_frame_equal(bundle.product_month_sales_df, rebuilt.product_month_sales_df, check_dtype=True)
    pd.testing.assert_frame_equal(bundle.manufacturer_month_df, rebuilt.manufacturer_month_df, check_dtype=True)
    pd.testing.assert_frame_equal(bundle.sales_month_total_df, rebuilt.sales_month_total_df, check_dtype=True)
    pd.testing.assert_frame_equal(bundle.purchase_month_total_df, rebuilt.purchase_month_total_df, check_dtype=True)
    assert bundle.purchase_diagnostics == rebuilt.purchase_diagnostics

    expected_identity = sales.loc[:, ["제품코드", "제품명", "규격", "제조사코드", "제조사명"]].drop_duplicates().sort_values(["제품코드", "제조사코드"], kind="stable").reset_index(drop=True)
    actual_identity = bundle.product_identity_df.loc[:, expected_identity.columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(actual_identity, expected_identity, check_dtype=True)

    forecast_raw = adapt_dashboard_narrow_bundle_for_forecast(bundle)
    raw_forecast = get_sales_forecast_df(PARAMS, raw_df=sales)
    narrow_forecast = get_sales_forecast_df(PARAMS, raw_df=forecast_raw)
    pd.testing.assert_frame_equal(raw_forecast, narrow_forecast, check_dtype=True, check_like=False)

    raw_history = _build_demand_surge_history_by_product(
        sales, evaluation_month="202607", history_month_from="202501"
    )
    narrow_history = _build_demand_surge_history_by_product(
        bundle.product_month_sales_df, evaluation_month="202607", history_month_from="202501"
    )
    assert raw_history == narrow_history

    raw_manufacturer = get_manufacturer_sales_trend_summary(PARAMS, raw_df=sales)
    narrow_manufacturer = get_manufacturer_sales_trend_summary(
        PARAMS, raw_df=adapt_dashboard_narrow_bundle_for_manufacturer(bundle)
    )
    pd.testing.assert_frame_equal(raw_manufacturer, narrow_manufacturer, check_dtype=True, check_like=False)

    raw_month_total = _monthly_sales_actuals_from_source(sales)
    narrow_month_total = _monthly_sales_actuals_from_source(bundle.sales_month_total_df)
    assert raw_month_total == narrow_month_total
    assert _monthly_sales_returns_from_source(sales) == _monthly_sales_returns_from_source(bundle.sales_month_total_df)

    visual_rows = [{"stock_cover_status": "ready", "주요매입처상태": "assigned"}]
    raw_visual, raw_trend = _build_visual_phase2_summary(visual_rows, purchase, evaluation_remaining_days=1)
    narrow_visual, narrow_trend = _build_visual_phase2_summary(
        visual_rows,
        adapt_dashboard_narrow_bundle_for_visual(bundle),
        evaluation_remaining_days=1,
    )
    assert raw_trend == narrow_trend
    assert raw_visual["purchase_trend_points"] == narrow_visual["purchase_trend_points"]

    expected_diagnostics = {
        "purchase_source_rows": 7,
        "purchase_positive_rows": 2,
        "purchase_nonpositive_rows": 1,
        "purchase_unclassified_rows": 4,
        "missing_product_code_rows": 1,
        "missing_month_rows": 1,
        "invalid_numeric_rows": 1,
        "other_excluded_rows": 1,
    }
    assert bundle.purchase_diagnostics == expected_diagnostics
    assert list(bundle.purchase_month_total_df.columns) == ["기준월", "매입금액"]
    assert list(bundle.sales_month_total_df.columns) == ["기준월", "매출합계", "매출반품금액"]
    print("PASS: dashboard narrow typed sales/purchase bundle equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

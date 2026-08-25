"""Offline equality gate for the Dashboard sales/purchase multi-grain contract."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_sales_purchase_grains
from app.services.dashboard_lite_facts import _build_demand_surge_history_by_product


EVALUATION_MONTH = "202607"
HISTORY_MONTH_FROM = "202501"


def _assert_frame_equal(actual: pd.DataFrame, expected: pd.DataFrame, columns: list[str]) -> None:
    actual = actual.reindex(columns=columns).sort_values(columns[:2], kind="stable").reset_index(drop=True)
    expected = expected.reindex(columns=columns).sort_values(columns[:2], kind="stable").reset_index(drop=True)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=True, check_like=False)


def _sales_fixture() -> pd.DataFrame:
    rows = [
        ("202501", "P1", "제품1", "제조사1", "M1", "V2", "10", "100", "1"),
        ("202501", "P1", "제품1", "제조사1", "M1", "V1", "5", "50", "1"),
        ("202502", "P1", "제품1", "제조사1", "M1", "V1", "25", "250", "2"),
        ("202502", "P2", "제품2", "제조사1", "M1", "V3", "8", "80", "1"),
        ("202503", "P2", "제품2", "제조사2", "M2", "V4", "-2", "-20", "1"),
    ]
    columns = ["기준월", "제품코드", "제품명", "제조사명", "제조사코드", "매입처코드", "출고수량", "매출공급가액", "집계건수"]
    return pd.DataFrame(rows, columns=columns)


def _purchase_fixture() -> pd.DataFrame:
    rows = [
        ("202601", "P1", "B", "매입처B", "10", "100", "1"),
        ("202601", "P1", "A", "매입처A", "10", "100", "1"),
        ("202602", "P1", "A", "매입처A", "0", "0", "0"),
        ("202501", "P2", "C", "매입처C", "5", "50", "1"),
        ("", "P3", "D", "매입처D", "1", "10", "1"),
        ("202601", "", "E", "매입처E", "1", "10", "1"),
        ("202601", "P4", "F", "매입처F", "bad", "10", "1"),
        ("202607", "P5", "G", "매입처G", "1", "10", "1"),
    ]
    return pd.DataFrame(rows, columns=["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"])


def _expected_product_month_sales(sales: pd.DataFrame) -> pd.DataFrame:
    work = sales.copy()
    for column in ("출고수량", "매출공급가액", "집계건수"):
        work[column] = pd.to_numeric(work[column], errors="coerce").fillna(0.0)
    expected = work.groupby(["기준월", "제품코드", "제품명", "제조사코드", "제조사명"], as_index=False)[["출고수량", "매출공급가액", "집계건수"]].sum()
    expected["매입처수"] = work.groupby(["기준월", "제품코드"])["매입처코드"].nunique().to_numpy()
    return expected


def _expected_purchase_diagnostics() -> dict[str, int]:
    return {
        "purchase_source_rows": 8,
        "purchase_positive_rows": 3,
        "purchase_nonpositive_rows": 1,
        "purchase_unclassified_rows": 4,
        "missing_product_code_rows": 1,
        "missing_month_rows": 1,
        "invalid_numeric_rows": 1,
        "other_excluded_rows": 1,
    }


def main() -> int:
    sales = _sales_fixture()
    purchase = _purchase_fixture()
    grains = build_dashboard_sales_purchase_grains(
        sales,
        purchase,
        evaluation_month=EVALUATION_MONTH,
        history_month_from=HISTORY_MONTH_FROM,
    )

    expected_sales = _expected_product_month_sales(sales)
    _assert_frame_equal(
        grains.sales_product_month_df,
        expected_sales,
        ["기준월", "제품코드", "제품명", "제조사코드", "제조사명", "출고수량", "매출공급가액", "집계건수", "매입처수"],
    )

    legacy_history = _build_demand_surge_history_by_product(
        sales,
        evaluation_month=EVALUATION_MONTH,
        history_month_from=HISTORY_MONTH_FROM,
    )
    compact_history = _build_demand_surge_history_by_product(
        grains.sales_product_month_df,
        evaluation_month=EVALUATION_MONTH,
        history_month_from=HISTORY_MONTH_FROM,
    )
    assert legacy_history == compact_history, "product-month demand history changed"

    expected_cardinality = (
        sales.groupby(["제조사코드", "제조사명"])["매입처코드"].nunique().sort_index().to_dict()
    )
    actual_cardinality = (
        grains.manufacturer_vendor_df.groupby(["제조사코드", "제조사명"])["매입처코드"].nunique().sort_index().to_dict()
    )
    assert actual_cardinality == expected_cardinality, "manufacturer/vendor cardinality changed"

    assert grains.purchase_diagnostics == _expected_purchase_diagnostics(), "purchase diagnostics changed"
    compact = grains.purchase_product_vendor_df
    p1 = compact.loc[compact["제품코드"].eq("P1")].copy()
    assert set(p1["매입처코드"]) == {"A", "B"}, "purchase vendor aggregation changed"
    # Equal amount/quantity/month candidates must retain the existing code ascending tie-break.
    winners = p1.sort_values(
        ["최근6완료월순매입금액", "최근6완료월순입고수량", "지원기간최근매입월", "매입처코드"],
        ascending=[False, False, False, True],
        kind="stable",
    )
    assert winners.iloc[0]["매입처코드"] == "A", "purchase vendor tie-break changed"

    expected_purchase_trend = pd.DataFrame(
        [
            ("202501", "P2", 50, 5.0, 1),
            ("202601", "", 10, 1.0, 1),
            ("202601", "P1", 200, 20.0, 2),
            ("202601", "P4", 10, 0.0, 1),
            ("202602", "P1", 0, 0.0, 0),
            ("202607", "P5", 10, 1.0, 1),
        ],
        columns=["기준월", "제품코드", "매입금액", "입고수량", "매입발생건수"],
    )
    _assert_frame_equal(
        grains.purchase_product_month_df,
        expected_purchase_trend,
        ["기준월", "제품코드", "매입금액", "입고수량", "매입발생건수"],
    )
    print("PASS: dashboard sales/purchase multi-grain equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline consumer equality gate for the Dashboard sales/purchase candidate.

This deliberately exercises the current Dashboard consumers.  It does not
select from an ERP database or switch the production source bundle.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_sales_purchase_grains
from app.services.dashboard_lite_facts import _attach_major_purchase_vendors, _build_visual_phase2_summary


EVALUATION_MONTH = "202607"
HISTORY_MONTH_FROM = "202501"
DIAGNOSTIC_KEYS = (
    "purchase_source_rows",
    "purchase_positive_rows",
    "purchase_nonpositive_rows",
    "purchase_unclassified_rows",
    "missing_product_code_rows",
    "missing_month_rows",
    "invalid_numeric_rows",
    "other_excluded_rows",
)


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


def _sales_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("202501", "P1", "제품1", "M1", "제조사1", "V2", "10", "100", "1"),
            ("202501", "P1", "제품1", "M1", "제조사1", "V1", "5", "50", "1"),
            ("202502", "P2", "제품2", "M2", "제조사2", "V3", "8", "80", "1"),
        ],
        columns=["기준월", "제품코드", "제품명", "제조사코드", "제조사명", "매입처코드", "출고수량", "매출공급가액", "집계건수"],
    )


def _inbound_rows() -> list[dict[str, object]]:
    return [
        {
            "product_code": "P1",
            "recent_inbound_vendor_code": "IN-A",
            "recent_inbound_vendor_name": "최근입고처A",
            "recent_inbound_vendor_source": "actual_inbound",
            "inbound_data_cutoff_date": "20260825",
            "재고위험상태": "긴급 부족",
            "위험보정부족예상금액": 100.0,
            "위험보정부족예상수량": 2.0,
            "과잉후보금액": 0.0,
        },
        {
            "product_code": "P2",
            "recent_inbound_vendor_code": "",
            "recent_inbound_vendor_name": "",
            "recent_inbound_vendor_source": "none",
            "inbound_data_cutoff_date": "20260825",
            "재고위험상태": "부족 주의",
            "위험보정부족예상금액": 50.0,
            "위험보정부족예상수량": 1.0,
            "과잉후보금액": 0.0,
        },
    ]


def _vendor_result(purchase: pd.DataFrame) -> dict[str, object]:
    return _attach_major_purchase_vendors(
        _inbound_rows(),
        purchase,
        evaluation_month=EVALUATION_MONTH,
        history_month_from=HISTORY_MONTH_FROM,
        source_call_count=2,
        vendor_lookback_days=90,
    )


def main() -> int:
    sales = _sales_fixture()
    purchase = _purchase_fixture()
    grains = build_dashboard_sales_purchase_grains(
        sales,
        purchase,
        evaluation_month=EVALUATION_MONTH,
        history_month_from=HISTORY_MONTH_FROM,
    )

    # Manufacturer/vendor cardinality is a typed source contract, not a new
    # Dashboard aggregation.  Preserve the exact normalized vendor set.
    raw_cardinality = (
        sales.assign(매입처코드=sales["매입처코드"].fillna("").astype(str).str.strip())
        .groupby(["제조사코드", "제조사명"], dropna=False)["매입처코드"].nunique()
        .sort_index()
    )
    compact_cardinality = (
        grains.manufacturer_vendor_df.groupby(["제조사코드", "제조사명"], dropna=False)["매입처코드"]
        .nunique()
        .sort_index()
    )
    pd.testing.assert_series_equal(raw_cardinality, compact_cardinality, check_dtype=True)

    # The visible purchase trend consumes only month/amount.  Compact rows
    # must therefore reproduce the existing consumer's exact output.
    visual_rows = [{"stock_cover_status": "ready", "주요매입처상태": "assigned"}]
    raw_visual, raw_trend = _build_visual_phase2_summary(visual_rows, purchase, evaluation_remaining_days=1)
    compact_trend_source = grains.purchase_product_month_df.loc[:, ["기준월", "매입금액"]].copy()
    compact_visual, compact_trend = _build_visual_phase2_summary(visual_rows, compact_trend_source, evaluation_remaining_days=1)
    assert raw_trend == compact_trend
    assert raw_visual["purchase_trend_status"] == compact_visual["purchase_trend_status"]
    assert raw_visual["purchase_trend_points"] == compact_visual["purchase_trend_points"]

    # Current major-vendor and vendor-risk outputs are sourced from inbound
    # facts.  Purchase history contributes diagnostics only; it must not alter
    # representative vendor selection, tie-breaks, or vendor-risk rows.
    raw_vendor = _vendor_result(purchase)
    empty_vendor = _vendor_result(pd.DataFrame(columns=purchase.columns))
    assert raw_vendor["rows"] == empty_vendor["rows"]
    assert raw_vendor["top_rows"] == empty_vendor["top_rows"]
    for key in (
        "risk_rows", "assigned_rows", "unassigned_rows", "vendor_count",
        "total_adjusted_shortage_amount", "assigned_adjusted_shortage_amount",
        "unassigned_adjusted_shortage_amount", "basis_mode", "basis_days",
        "fallback_policy",
    ):
        assert raw_vendor["summary"][key] == empty_vendor["summary"][key], key

    # Raw Dashboard diagnostics and typed candidate diagnostics use the same
    # classification boundary, including zero, negative, missing and invalid rows.
    for key in DIAGNOSTIC_KEYS:
        assert raw_vendor["summary"][key] == grains.purchase_diagnostics[key], key

    # The compact vendor grain retains the support/latest fields and the
    # existing ascending vendor-code tie-break input for a future runtime use.
    compact_vendor = grains.purchase_product_vendor_df.loc[lambda frame: frame["제품코드"].eq("P1")]
    winner = compact_vendor.sort_values(
        ["최근6완료월순매입금액", "최근6완료월순입고수량", "지원기간최근매입월", "매입처코드"],
        ascending=[False, False, False, True],
        kind="stable",
    ).iloc[0]
    assert winner["매입처코드"] == "A"
    assert {"지원기간순매입금액", "지원기간최근매입월", "최근6완료월최근매입월"}.issubset(compact_vendor.columns)

    print("PASS: dashboard sales/purchase consumer equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline authority/equality gate for Dashboard inbound facts.

The sales/purchase compact candidate must not produce, replace, or mutate these
inbound facts.  This Gate fixes the existing Rddbc110 -> facts -> consumer
contract with representative boundary cases and no database access.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_sales_purchase_grains
from app.services.dashboard_inbound_facts_service import build_dashboard_inbound_facts_frame
from app.services.dashboard_lite_facts import _attach_dashboard_inbound_facts, _attach_major_purchase_vendors


CUTOFF = "20260825"
INBOUND_COLUMNS = (
    "product_code", "master_order_vendor_code", "master_order_vendor_name",
    "inbound_date", "io_tcode", "vendor_code", "inbound_vendor_name",
    "quantity", "oquantity", "supply_price",
)


def _source_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Same quantity/price/date: vendor-code ascending must win.
            ("ACT", "MASTER-A", "마스터A", "20260820", "001", "V2", "실제V2", 10, 0, 100),
            ("ACT", "MASTER-A", "마스터A", "20260820", "001", "V1", "실제V1", 10, 0, 100),
            # Actual return is not a normal inbound or representative vendor.
            ("ACT", "MASTER-A", "마스터A", "20260822", "101", "V3", "반품V3", 5, 0, 50),
            # No actual inbound: retain product-master order-vendor fallback.
            ("MASTER", "MASTER-B", "마스터B", "", "", "", "", 0, 0, 0),
            # No actual or master vendor: retain explicit none.
            ("NONE", "", "", "", "", "", "", 0, 0, 0),
            # Two normal days in the 365-day window but outside 90 days: delayed.
            ("DELAY", "MASTER-D", "마스터D", "20250830", "001", "OLD", "과거V", 1, 0, 10),
            ("DELAY", "MASTER-D", "마스터D", "20250915", "001", "OLD", "과거V", 1, 0, 10),
            # Exactly 90-day inclusive boundary for current authority window.
            ("BOUND90", "", "", "20260528", "002", "B90", "경계V", 3, 0, 30),
        ],
        columns=INBOUND_COLUMNS,
    )


def _inventory_rows() -> list[dict[str, object]]:
    return [
        {
            "product_code": code,
            "재고위험상태": "긴급 부족" if code != "NONE" else "부족 주의",
            "위험보정부족예상금액": 100.0,
            "위험보정부족예상수량": 1.0,
            "과잉후보금액": 0.0,
        }
        for code in ("ACT", "MASTER", "NONE", "DELAY", "BOUND90")
    ]


def _candidate_grains() -> object:
    sales = pd.DataFrame(
        [("202608", "ACT", "제품", "M", "제조사", "V1", 1, 1, 1)],
        columns=["기준월", "제품코드", "제품명", "제조사코드", "제조사명", "매입처코드", "출고수량", "매출공급가액", "집계건수"],
    )
    purchase = pd.DataFrame(
        [("202608", "ACT", "V1", "실제V1", 1, 1, 1)],
        columns=["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"],
    )
    return build_dashboard_sales_purchase_grains(sales, purchase, evaluation_month="202609", history_month_from="202601")


def main() -> int:
    facts = build_dashboard_inbound_facts_frame(
        _source_fixture(), data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=90
    ).set_index("product_code")

    assert facts.loc["ACT", "recent_inbound_vendor_code"] == "V1"
    assert facts.loc["ACT", "recent_inbound_vendor_source"] == "actual_inbound"
    assert facts.loc["ACT", "normal_inbound_90_exists"]
    assert facts.loc["ACT", "normal_inbound_365_exists"]
    assert facts.loc["MASTER", "recent_inbound_vendor_code"] == "MASTER-B"
    assert facts.loc["MASTER", "recent_inbound_vendor_source"] == "master_order_vendor"
    assert bool(facts.loc["MASTER", "recent_inbound_vendor_fallback"])
    assert facts.loc["NONE", "recent_inbound_vendor_source"] == "none"
    assert not bool(facts.loc["NONE", "normal_inbound_365_exists"])
    assert bool(facts.loc["DELAY", "normal_inbound_365_exists"])
    assert not bool(facts.loc["DELAY", "normal_inbound_90_exists"])
    assert bool(facts.loc["DELAY", "inbound_delayed_candidate"])
    assert bool(facts.loc["BOUND90", "normal_inbound_90_exists"])

    rows = _inventory_rows()
    inbound_summary = _attach_dashboard_inbound_facts(rows, facts.reset_index(), inbound_source_call_count=1, vendor_lookback_days=90)
    assert inbound_summary["inbound_source_call_count"] == 1
    attached = {str(row["product_code"]): row for row in rows}
    for code in facts.index:
        row = attached[str(code)]
        for field in (
            "recent_inbound_vendor_code", "recent_inbound_vendor_name", "recent_inbound_vendor_source",
            "normal_inbound_90_exists", "normal_inbound_365_exists", "last_normal_inbound_date",
            "inbound_delay_days", "inbound_delayed_candidate", "inbound_data_status",
        ):
            expected = facts.loc[code, field]
            if pd.isna(expected):
                assert row[field] is None or pd.isna(row[field]), (code, field)
            else:
                assert row[field] == expected, (code, field)

    vendor_result = _attach_major_purchase_vendors(
        rows,
        pd.DataFrame(columns=["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"]),
        evaluation_month="202609", history_month_from="202601", source_call_count=2, vendor_lookback_days=90,
    )
    vendor_rows = {row["주요매입처코드"]: row for row in vendor_result["rows"]}
    assert "V1" in vendor_rows
    assert "MASTER-B" in vendor_rows
    assert attached["NONE"]["주요매입처상태"] == "recent_purchase_none"

    # The candidate owns sales/purchase grains only.  It has no inbound
    # authority fields and cannot overwrite the attached factual values.
    candidate = _candidate_grains()
    for frame in (
        candidate.sales_product_month_df,
        candidate.manufacturer_vendor_df,
        candidate.purchase_product_vendor_df,
        candidate.purchase_product_month_df,
    ):
        assert not any(column.startswith("recent_inbound_") or column.startswith("normal_inbound_") for column in frame.columns)
    assert attached["ACT"]["recent_inbound_vendor_code"] == "V1"
    assert bool(attached["DELAY"]["inbound_delayed_candidate"])

    print("PASS: dashboard inbound facts authority/equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline authority/equality gate for Dashboard inbound facts.

The sales/purchase compact candidate must not produce, replace, or mutate these
inbound facts.  This Gate fixes the existing Rddbc110 -> facts -> consumer
contract with representative boundary cases and no database access.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_sales_purchase_grains
from app.services.dashboard_inbound_facts_service import (
    _scope_authority_sql, _sql, build_dashboard_inbound_facts_frame,
    get_dashboard_inbound_facts, get_dashboard_inbound_scope_authority,
)
from app.services.dashboard_lite_facts import _attach_dashboard_inbound_facts, _attach_major_purchase_vendors
from app.services.order_calculation_service import filter_base_by_order_scope, order_scope_row_matches, _index


CUTOFF = "20260825"
INBOUND_COLUMNS = (
    "product_code", "master_order_vendor_code", "master_order_vendor_name",
    "master_order_staff_code", "master_order_staff_name",
    "manufacturer_vendor_code", "manufacturer_vendor_name",
    "manufacturer_staff_code", "manufacturer_staff_name",
    "inbound_date", "io_tcode", "vendor_code", "inbound_vendor_name",
    "inbound_vendor_staff_code", "inbound_vendor_staff_name",
    "quantity", "oquantity", "supply_price",
)


def _source_fixture() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Same quantity/price/date: vendor-code ascending must win.
            ("ACT", "MASTER-A", "마스터A", "U1", "담당자A", "M1", "제약사A", "P1", "제약담당A", "20260820", "001", "V2", "실제V2", "U12", "윤정아", 10, 0, 100),
            ("ACT", "MASTER-A", "마스터A", "U1", "담당자A", "M1", "제약사A", "P1", "제약담당A", "20260820", "001", "V1", "실제V1", "U11", "이기재", 10, 0, 100),
            # Actual return is not a normal inbound or representative vendor.
            ("ACT", "MASTER-A", "마스터A", "U1", "담당자A", "M1", "제약사A", "P1", "제약담당A", "20260822", "101", "V3", "반품V3", "U13", "반품담당", 5, 0, 50),
            # No actual inbound: retain product-master order-vendor fallback.
            ("MASTER", "MASTER-B", "마스터B", "U2", "담당자B", "M2", "제약사B", "P2", "제약담당B", "", "", "", "", "", "", 0, 0, 0),
            # No actual or master vendor: retain explicit none.
            ("NONE", "", "", "", "", "", "", "", "", "", "", "", "", "", "", 0, 0, 0),
            # Two normal days in the 365-day window but outside 90 days: delayed.
            ("DELAY", "MASTER-D", "마스터D", "U3", "담당자D", "M3", "제약사D", "P3", "제약담당D", "20250830", "001", "OLD", "과거V", "U31", "과거담당", 1, 0, 10),
            ("DELAY", "MASTER-D", "마스터D", "U3", "담당자D", "M3", "제약사D", "P3", "제약담당D", "20250915", "001", "OLD", "과거V", "U31", "과거담당", 1, 0, 10),
            # Exactly 90-day inclusive boundary for current authority window.
            ("BOUND90", "", "", "", "", "M9", "제약사경계", "P9", "제약담당경계", "20260528", "002", "B90", "경계V", "U90", "경계담당", 3, 0, 30),
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


def _legacy_order_scope(base: pd.DataFrame, suppliers: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    """Fix the former record-loop predicate as an equality oracle."""
    supplier_rows = _index(suppliers, "product_code")
    rows = []
    for basic in base.to_dict("records"):
        supplier = supplier_rows.get(str(basic.get("제품코드") or "").strip(), {})
        if order_scope_row_matches(params, supplier):
            rows.append(basic)
    return pd.DataFrame(rows, columns=base.columns)


def main() -> int:
    facts = build_dashboard_inbound_facts_frame(
        _source_fixture(), data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=90
    ).set_index("product_code")

    # The production SQL groups raw R110 rows before display-master joins.  The
    # positive/nonpositive bucket is part of the grain so positive-event
    # existence, vendor ranking, returns, zero sums, and negative quantities
    # retain the detail-path semantics.
    duplicate_events = pd.DataFrame(
        [
            ("AGG", "MASTER", "마스터", "U1", "담당", "M1", "제약", "P1", "제약담당", "20260820", "001", "V1", "매입처", "U2", "매입담당", 10, 0, 100),
            ("AGG", "MASTER", "마스터", "U1", "담당", "M1", "제약", "P1", "제약담당", "20260820", "001", "V1", "매입처", "U2", "매입담당", 5, 0, 50),
            ("AGG", "MASTER", "마스터", "U1", "담당", "M1", "제약", "P1", "제약담당", "20260820", "001", "V1", "매입처", "U2", "매입담당", -3, 0, -30),
            ("AGG", "MASTER", "마스터", "U1", "담당", "M1", "제약", "P1", "제약담당", "20260821", "101", "V1", "매입처", "U2", "매입담당", -2, 0, -20),
        ],
        columns=INBOUND_COLUMNS,
    )
    aggregate_keys = [column for column in INBOUND_COLUMNS if column not in {"quantity", "oquantity", "supply_price"}]
    aggregate_source = duplicate_events.assign(
        _positive=(duplicate_events["quantity"] + duplicate_events["oquantity"]).gt(0)
    ).groupby([*aggregate_keys, "_positive"], as_index=False, dropna=False)[
        ["quantity", "oquantity", "supply_price"]
    ].sum().drop(columns="_positive")
    raw_aggregate_facts = build_dashboard_inbound_facts_frame(
        duplicate_events, data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=90,
    )
    compact_aggregate_facts = build_dashboard_inbound_facts_frame(
        aggregate_source, data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=90,
    )
    pd.testing.assert_frame_equal(raw_aggregate_facts, compact_aggregate_facts)

    assert facts.loc["ACT", "recent_inbound_vendor_code"] == "V1"
    assert facts.loc["ACT", "recent_inbound_vendor_count_90"] == 2
    assert facts.loc["ACT", "master_order_staff_code"] == "U1"
    assert facts.loc["ACT", "master_order_staff_name"] == "담당자A"
    assert facts.loc["ACT", "recent_inbound_vendor_staff_code"] == "U11"
    assert facts.loc["ACT", "recent_inbound_vendor_staff_name"] == "이기재"
    assert facts.loc["ACT", "manufacturer_staff_code"] == "P1"
    assert facts.loc["ACT", "manufacturer_staff_name"] == "제약담당A"
    assert facts.loc["ACT", "recent_inbound_vendor_source"] == "actual_inbound"
    assert facts.loc["ACT", "normal_inbound_90_exists"]
    assert facts.loc["ACT", "normal_inbound_365_exists"]
    assert facts.loc["MASTER", "recent_inbound_vendor_staff_code"] == "U2"
    assert facts.loc["MASTER", "recent_inbound_vendor_staff_name"] == "담당자B"

    profile_45 = build_dashboard_inbound_facts_frame(
        _source_fixture(), data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=45
    ).set_index("product_code")
    assert profile_45.loc["ACT", "recent_inbound_vendor_count_90"] == 2
    assert facts.loc["MASTER", "recent_inbound_vendor_code"] == "MASTER-B"
    assert facts.loc["MASTER", "recent_inbound_vendor_source"] == "master_order_vendor"
    assert bool(facts.loc["MASTER", "recent_inbound_vendor_fallback"])
    assert facts.loc["NONE", "recent_inbound_vendor_source"] == "none"
    assert not bool(facts.loc["NONE", "normal_inbound_365_exists"])
    assert bool(facts.loc["DELAY", "normal_inbound_365_exists"])
    assert not bool(facts.loc["DELAY", "normal_inbound_90_exists"])
    assert bool(facts.loc["DELAY", "inbound_delayed_candidate"])
    assert bool(facts.loc["BOUND90", "normal_inbound_90_exists"])

    # Full-scope order calculation consumes only this compact product authority.
    # It must exactly preserve every representative-vendor field used by the
    # calculation while collapsing duplicate product rows from the ERP master.
    order_authority_columns = [
        "product_code", "recent_inbound_vendor_code", "recent_inbound_vendor_name",
        "recent_inbound_vendor_staff_code", "recent_inbound_vendor_staff_name",
        "manufacturer_vendor_code", "manufacturer_vendor_name",
        "manufacturer_staff_code", "manufacturer_staff_name",
        "recent_inbound_vendor_count_90", "recent_inbound_vendor_source",
    ]
    expected_order_authority = facts.reset_index()[order_authority_columns].sort_values("product_code").reset_index(drop=True)
    compact_source = pd.concat(
        [expected_order_authority, expected_order_authority.iloc[[0]]], ignore_index=True,
    )
    with patch(
        "app.services.dashboard_inbound_facts_service.query_to_df", return_value=compact_source,
    ):
        compact_order_authority = get_dashboard_inbound_scope_authority(
            {}, data_cutoff_date=CUTOFF, vendor_lookback_days=90,
        )
    pd.testing.assert_frame_equal(
        expected_order_authority,
        compact_order_authority[order_authority_columns].sort_values("product_code").reset_index(drop=True),
    )
    assert compact_order_authority["product_code"].is_unique
    assert compact_order_authority.attrs["inbound_source_rows"] == len(compact_source)
    assert compact_order_authority.attrs["inbound_product_scope_sql_mode"] == "profile_compact_authority"

    # Product-code pushdown is safe only after representative-vendor scope has
    # been resolved.  The selected detail subset must retain identical facts.
    scoped_codes = ["ACT", "MASTER", "BOUND90"]
    scoped_source = _source_fixture().loc[
        _source_fixture()["product_code"].isin(scoped_codes)
    ].copy()
    scoped_facts = build_dashboard_inbound_facts_frame(
        scoped_source, data_cutoff_date=CUTOFF, cycle_lookback_days=365, vendor_lookback_days=90
    ).set_index("product_code")
    authority_columns = [
        "recent_inbound_vendor_code", "recent_inbound_vendor_name",
        "recent_inbound_vendor_staff_code", "recent_inbound_vendor_staff_name",
        "recent_inbound_vendor_source", "manufacturer_vendor_code",
        "manufacturer_staff_code", "manufacturer_staff_name",
        "normal_inbound_raw_qty_365", "normal_inbound_positive_qty_365",
        "inbound_return_raw_qty_365", "recent_inbound_vendor_qty_90",
    ]
    pd.testing.assert_frame_equal(
        facts.loc[scoped_codes, authority_columns],
        scoped_facts.loc[scoped_codes, authority_columns],
    )
    scoped_sql, scoped_binds = _sql(
        {"inbound_product_code_list": ["00002", "00001", "00002"]},
        start_date="20250826", cutoff_date=CUTOFF,
    )
    unscoped_sql, unscoped_binds = _sql({}, start_date="20250826", cutoff_date=CUTOFF)
    assert "WITH InboundEvents AS" in unscoped_sql
    assert "LEFT JOIN InboundEvents AS I" in unscoped_sql
    assert "CASE WHEN COALESCE(I.Rd11_Quantity, 0) + COALESCE(I.Rd11_Oquantity, 0) > 0" in unscoped_sql
    assert "LEFT JOIN dbo.Rddbc110 AS I" not in unscoped_sql
    assert scoped_sql != unscoped_sql
    assert len([key for key in scoped_binds if key.startswith("inbound_product_code_")]) == 2
    assert "ProductUniverse" not in scoped_sql
    assert "STRING_SPLIT" not in scoped_sql
    assert "P.Rd04_Physic_Cd IN" in scoped_sql
    for scope_size in (290, 1800):
        codes = [f"{index:05d}" for index in range(scope_size)]
        large_sql, large_binds = _sql(
            {"inbound_product_code_list": codes}, start_date="20250826", cutoff_date=CUTOFF,
        )
        assert large_sql != unscoped_sql
        assert len([key for key in large_binds if key.startswith("inbound_product_code_")]) == scope_size
    for scope_size in (1801, 10296):
        codes = [f"{index:05d}" for index in range(scope_size)]
        large_sql, large_binds = _sql(
            {"inbound_product_code_list": codes}, start_date="20250826", cutoff_date=CUTOFF,
        )
        assert large_sql == unscoped_sql
        assert large_binds == unscoped_binds
    authority_sql, authority_binds = _scope_authority_sql(
        {}, cutoff_date=CUTOFF, vendor_lookback_days=90,
    )
    assert "ROW_NUMBER() OVER" in authority_sql
    assert "VendorAggregate" in authority_sql
    assert "VendorCount90" in authority_sql
    assert "recent_inbound_vendor_count_90" in authority_sql
    assert authority_binds["vendor_start"] == "20260528"
    assert authority_binds["vendor_count_start"] == "20260528"
    assert authority_binds["aggregate_start"] == "20260528"
    profile_sql, profile_binds = _sql(
        {
            "product_group_list": ["9998", "0013:9999"],
            "product_di_list": ["1"],
            "product_class_list": ["0031:A"],
        },
        start_date="20250826",
        cutoff_date=CUTOFF,
    )
    assert "P.Rd04_Physic_Group IN (%(product_group_0)s, %(product_group_1)s)" in profile_sql
    assert profile_binds["product_group_0"] == "9998"
    assert profile_binds["product_group_1"] == "9999"
    assert profile_binds["product_di_0"] == "1"
    assert profile_binds["product_class_0"] == "A"
    large_codes = [f"{index:05d}" for index in range(10296)]
    with patch(
        "app.services.dashboard_inbound_facts_service.query_to_df", return_value=_source_fixture()
    ) as source_query:
        large_facts = get_dashboard_inbound_facts(
            {"inbound_product_code_list": large_codes},
            data_cutoff_date=CUTOFF,
            cycle_lookback_days=365,
            vendor_lookback_days=90,
        )
    source_sql, source_params = source_query.call_args.args
    assert source_sql == unscoped_sql
    assert source_params == unscoped_binds
    assert large_facts.attrs["inbound_product_scope_count"] == 10296
    assert large_facts.attrs["inbound_product_scope_sql_mode"] == "oversized_profile_scope"
    assert large_facts.attrs["inbound_product_scope_bind_count"] == 0
    assert large_facts.attrs["inbound_product_scope_payload_count"] == 0

    # R110 facts remain unscoped; only the final order predicate changed from
    # a record loop to aligned Series.  Every staff/vendor authority branch
    # must retain the exact former product set and source row order.
    scope_base = pd.DataFrame(
        [("ACT", "a"), ("MASTER", "b"), ("NONE", "c"), ("DELAY", "d"), ("BOUND90", "e")],
        columns=["제품코드", "fixture_value"],
    )
    scope_suppliers = facts.reset_index()
    for scope_params in (
        {"order_staff_nm": "이기재"},
        {"pharma_staff_nm": "제약담당"},
        {"order_vendor_cd": "V1"},
        {"order_vendor_nm": "마스터"},
        {"order_staff_nm": "이기재", "order_vendor_cd": "V1"},
    ):
        expected_scope = _legacy_order_scope(scope_base, scope_suppliers, scope_params)
        actual_scope = filter_base_by_order_scope(scope_base, scope_suppliers, scope_params)
        pd.testing.assert_frame_equal(expected_scope, actual_scope)

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

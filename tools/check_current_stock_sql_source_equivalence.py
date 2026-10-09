from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import product_inventory_service as inventory
from app.services.product_master_filter_contract import PRODUCT_FILTER_KEYS


def _source_row(*, buy_cd: str, buy_nm: str, old_in_qty: int, old_in_amt: int, old_out_qty: int) -> dict[str, object]:
    return {
        "group_cd": "S01", "group_nm": "본사창고", "buy_cd": buy_cd, "buy_nm": buy_nm,
        "order_cd": "O01", "order_nm": "발주처", "maker_cd": "M01", "maker_nm": "제조사",
        "product_group_nm": "그룹", "product_di_nm": "구분", "product_class_nm": "분류",
        "physic_cd": "P001", "physic_nm": "제품", "standard": "100T", "kd_cd": "KD",
        "edi_cd": "EDI", "std_cd": "8800000000001", "pack_unit": "EA",
        "master_unit_cost": 100, "insu_date": "20260101", "before_insu_date": "20250101",
        "insu_price": 120, "before_insu_price": 110, "acc_unit": 1, "physic_tax": "과세",
        "special_manage_nm": "", "old_in_qty": old_in_qty, "old_in_amt": old_in_amt,
        "old_out_qty": old_out_qty, "now_in_qty": 0, "now_in_amt": 0,
        "now_out_qty": 0, "now_out_amt": 0,
    }


def _assert_sql_shape() -> None:
    params = {
        "current_stock_query": True,
        "group_basis": "stock",
        "date_from": "20260901",
        "date_to": "20260930",
        "base_month": "202609",
    }
    cfg = inventory._settings(params)
    sql, binds = inventory._build_month_carry_sql(params, cfg)
    if "WITH CurrentStockMonthAgg AS" not in sql:
        raise AssertionError("plain current-stock month carry did not use the product/stock preaggregate")
    if "month_ven_cd" in sql or "BuyVen" in sql:
        raise AssertionError("current-stock preaggregate retained the purchase-vendor axis")
    if any(key.startswith("carry_product_prescription") for key in binds):
        raise AssertionError("plain current-stock unexpectedly bound prescription codes")

    prescription = dict(params, product_prescription_semantic="prescription")
    prescription_sql, prescription_binds = inventory._build_month_carry_sql(prescription, cfg)
    preaggregate = prescription_sql.split("WITH CurrentStockMonthAgg AS", 1)[-1].split("GROUP BY", 1)[0]
    if (
        "WITH CurrentStockMonthAgg AS" not in prescription_sql
        or "EXISTS (SELECT 1 FROM dbo.Rddbc010 AS CompanyProductDi" not in preaggregate
        or "CompanyProductDi.Rd01_Tcode = PFilter.Rd04_Physic_Di" not in preaggregate
        or any(key.startswith("carry_product_prescription_") for key in prescription_binds)
    ):
        raise AssertionError("prescription current-stock predicate is not pushed into the preaggregate")
    for key in PRODUCT_FILTER_KEYS:
        value = True if key == "product_only_use" else "fixture"
        if key == "product_prescription_semantic":
            value = "unsupported"
        if inventory._can_use_current_stock_month_preaggregate(dict(params, **{key: value}), cfg):
            raise AssertionError(f"canonical product filter must retain the established source shape: {key}")

    for key, value in (
        ("physic_nm", "제품"), ("maker_cd", "M001"),
        ("ven_nm", "거래처"), ("buy_cd", "B001"), ("buy_nm", "매입처"),
        ("order_cd", "O001"), ("order_nm", "발주처"),
        ("nlq_unlabeled_name", "검색어"), ("current_stock_entity_phrase", "엔터티"),
    ):
        fallback_sql, _ = inventory._build_month_carry_sql(dict(params, **{key: value}), cfg)
        if "WITH CurrentStockMonthAgg AS" in fallback_sql:
            raise AssertionError(f"master filter must retain the established source shape: {key}")


def _assert_group_equivalence() -> None:
    baseline = pd.DataFrame([
        _source_row(buy_cd="B01", buy_nm="매입처1", old_in_qty=10, old_in_amt=1000, old_out_qty=2),
        _source_row(buy_cd="B02", buy_nm="매입처2", old_in_qty=-3, old_in_amt=-300, old_out_qty=1),
    ])
    candidate = pd.DataFrame([
        _source_row(buy_cd="", buy_nm="", old_in_qty=7, old_in_amt=700, old_out_qty=3),
    ])
    params = {"current_stock_query": True, "group_basis": "stock", "date_to": "20260930"}
    cfg = inventory._settings(params)
    baseline_grouped = inventory._prepare_grouped_df(baseline, pd.DataFrame(), cfg, params)
    candidate_grouped = inventory._prepare_grouped_df(candidate, pd.DataFrame(), cfg, params)
    pd.testing.assert_frame_equal(baseline_grouped, candidate_grouped, check_dtype=True)
    baseline_display, baseline_source, _ = inventory._build_current_stock_table_frames(baseline_grouped, cfg)
    candidate_display, candidate_source, _ = inventory._build_current_stock_table_frames(candidate_grouped, cfg)
    pd.testing.assert_frame_equal(baseline_display, candidate_display, check_dtype=True)
    pd.testing.assert_frame_equal(baseline_source, candidate_source, check_dtype=True)


def main() -> None:
    _assert_sql_shape()
    _assert_group_equivalence()
    print("CURRENT_STOCK_SQL_SOURCE_EQUIVALENCE=PASS")


if __name__ == "__main__":
    main()

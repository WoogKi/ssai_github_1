"""Fixture and static contract gate for the minimal manufacturer vendor relation."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import build_dashboard_narrow_sales_purchase_bundle
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_manufacturer_vendor_relation_probe import (
    MANUFACTURER_MONTH_COLUMNS,
    build_manufacturer_month_from_narrow_projections,
    build_manufacturer_vendor_relation_sql,
)


def _sales_fixture() -> pd.DataFrame:
    rows = [
        ("202501", "P1", "제품1", "규격1", "M1", "제조사1", "V1", 10, 0, 100, 10, 110, 1),
        ("202501", "P1", "제품1", "규격1", "M1", "제조사1", "V1", 5, 0, 50, 5, 55, 1),
        ("202501", "P1", "제품1", "규격1", "M1", "제조사1", "V2", 3, 0, 30, 3, 33, 1),
        ("202502", "P1", "제품1", "규격1", "M1", "제조사1", "V2", -2, 0, -20, -2, -22, 1),
        ("202502", "P2", "제품2", "규격2", "M1", "제조사1", "V2", 7, 1, 70, 7, 77, 1),
        ("202502", "P3", "제품3", "규격3", "", "", "V3", 8, 0, 80, 8, 88, 1),
        ("202503", "P4", "제품4", "규격4", "M2", "제조사2", "V1", 4, 0, 40, 4, 44, 1),
        ("202503", "P5", "제품5", "규격5", None, None, "V1", 6, 0, 60, 6, 66, 1),
    ]
    return pd.DataFrame(rows, columns=[
        "기준월", "제품코드", "제품명", "규격", "제조사코드", "제조사명", "매입처코드",
        "출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "집계건수",
    ])


def main() -> int:
    params, contract = dashboard_probe_contract()
    sql, bind, plan = build_manufacturer_vendor_relation_sql(params)
    assert plan["logical_source_call_count"] == 1
    assert plan["physical_query_count"] == 1
    assert contract["period_source_policy"]["use_hybrid"] is False
    assert "GROUP BY 기준월, 제품코드, 매입처코드" in sql
    assert "COUNT(DISTINCT" not in sql
    assert "LEFT JOIN" not in sql
    assert "__dashboard_product_identity_id" not in sql
    for forbidden in ("매출공급가액", "매출세액", "매출합계", "집계건수"):
        assert forbidden not in sql.split("SELECT 기준월, 제품코드, 매입처코드", 1)[1]
    assert bind["stage_stock_cd_0"] == "00001"

    sales = _sales_fixture()
    bundle = build_dashboard_narrow_sales_purchase_bundle(
        sales, pd.DataFrame(), evaluation_month="202607", history_month_from="202501"
    )
    relations = sales.loc[:, ["기준월", "제품코드", "매입처코드"]].copy()
    relations.loc[len(relations)] = ["202502", "P1", "V2"]  # exact duplicate must not change nunique
    actual = build_manufacturer_month_from_narrow_projections(
        bundle.product_month_sales_df.drop(columns=["__dashboard_product_identity_id"]),
        bundle.product_identity_df,
        relations,
    )
    expected = bundle.manufacturer_month_df.reindex(columns=MANUFACTURER_MONTH_COLUMNS)
    pd.testing.assert_frame_equal(expected, actual, check_dtype=True, check_like=False)

    bad_identity = bundle.product_identity_df.copy()
    bad_identity.loc[len(bad_identity)] = bad_identity.iloc[0]
    try:
        build_manufacturer_month_from_narrow_projections(
            bundle.product_month_sales_df.drop(columns=["__dashboard_product_identity_id"]), bad_identity, relations
        )
    except ValueError as exc:
        assert "one identity row per product code" in str(exc)
    else:
        raise AssertionError("ambiguous product identity must fail closed")
    print("PASS: dashboard manufacturer vendor relation static and reconstruction equality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

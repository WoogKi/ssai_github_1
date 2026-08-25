"""Static and fixture gate for the split Dashboard sales stage profiler."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.analytics_sales_trend_service import (
    build_dashboard_narrow_bundle_from_projections,
    build_dashboard_narrow_sales_purchase_bundle,
)
from tools.check_dashboard_sales_purchase_db_multigrain_equality import dashboard_probe_contract
from tools.check_dashboard_sales_purchase_narrow_bundle import _projection_frames, _purchase_fixture, _sales_fixture
from tools.check_dashboard_sales_purchase_sales_projection_stage_profiler import (
    SALES_FACT_COLUMNS,
    assemble_sales_facts_from_stages,
    build_sales_projection_stage_sql,
)


def main() -> int:
    params, contract = dashboard_probe_contract()
    queries, plan = build_sales_projection_stage_sql(params)
    assert list(queries) == ["product_month_sales", "product_identity", "manufacturer_month"]
    assert plan["logical_source_call_count"] == 1
    assert plan["physical_query_count"] == 3
    assert plan["sales_month_total_derivation"].startswith("product_month_sales")
    assert contract["period_source_policy"]["use_hybrid"] is False
    product_sql, product_bind = queries["product_month_sales"]
    identity_sql, identity_bind = queries["product_identity"]
    manufacturer_sql, manufacturer_bind = queries["manufacturer_month"]
    for sql in (product_sql, identity_sql, manufacturer_sql):
        assert "GROUP BY GROUPING SETS" not in sql
        assert "WITH (NOLOCK)" in sql
        assert "OPTION (RECOMPILE)" in sql
    assert "COUNT(DISTINCT" not in product_sql
    assert "LEFT JOIN dbo.Rddbc040" not in product_sql
    assert "__dashboard_product_identity_id" not in product_sql
    assert "ProductVendorCounts" in identity_sql
    assert "COUNT(DISTINCT 매입처코드)" in identity_sql
    assert identity_sql.index("ProductVendorCounts") < identity_sql.index("LEFT JOIN dbo.Rddbc040")
    assert "ProductVendorMonth" in manufacturer_sql
    assert "COUNT(DISTINCT 제품코드)" in manufacturer_sql
    assert "COUNT(DISTINCT 매입처코드)" in manufacturer_sql
    assert "LEFT JOIN dbo.Rddbc040" in manufacturer_sql
    assert product_bind["stage_stock_cd_0"] == "00001"
    assert identity_bind["stage_stock_cd_0"] == "00001"
    assert manufacturer_bind["stage_stock_cd_0"] == "00001"

    # The production monthly master lookup is product-code keyed.  Establish
    # that normal one-product/one-identity condition for the positive stage
    # assembly case; the generic legacy fixture intentionally also covers a
    # historical mixed-master edge case below.
    stage_sales_fixture = _sales_fixture()
    stage_sales_fixture.loc[stage_sales_fixture["제품코드"].eq("P2"), ["제조사코드", "제조사명"]] = ["M1", "제조사1"]
    legacy = build_dashboard_narrow_sales_purchase_bundle(
        stage_sales_fixture, _purchase_fixture(), evaluation_month="202607", history_month_from="202501"
    )
    identity, sales, purchase = _projection_frames(legacy)
    product = sales.loc[sales["projection_kind"].eq("product_month_sales")].drop(
        columns=["projection_kind", "제약사명", "제품수", "매입처수", "__dashboard_product_identity_id"]
    )
    manufacturer = sales.loc[sales["projection_kind"].eq("manufacturer_month")].drop(
        columns=["projection_kind", "__dashboard_product_identity_id", "제품코드", "출고수량", "출고할증수량"]
    )
    staged_sales = assemble_sales_facts_from_stages(product, identity, manufacturer)
    assert list(staged_sales.columns) == SALES_FACT_COLUMNS
    rebuilt = build_dashboard_narrow_bundle_from_projections(identity, staged_sales, purchase)
    pd.testing.assert_frame_equal(legacy.product_identity_df, rebuilt.product_identity_df, check_dtype=True)
    pd.testing.assert_frame_equal(legacy.product_month_sales_df, rebuilt.product_month_sales_df, check_dtype=True)
    pd.testing.assert_frame_equal(legacy.manufacturer_month_df, rebuilt.manufacturer_month_df, check_dtype=True)
    pd.testing.assert_frame_equal(legacy.sales_month_total_df, rebuilt.sales_month_total_df, check_dtype=True)

    mixed_identity_bundle = build_dashboard_narrow_sales_purchase_bundle(
        _sales_fixture(), _purchase_fixture(), evaluation_month="202607", history_month_from="202501"
    )
    mixed_identity, mixed_sales, _ = _projection_frames(mixed_identity_bundle)
    mixed_product = mixed_sales.loc[mixed_sales["projection_kind"].eq("product_month_sales")].drop(
        columns=["projection_kind", "제약사명", "제품수", "매입처수", "__dashboard_product_identity_id"]
    )
    mixed_manufacturer = mixed_sales.loc[mixed_sales["projection_kind"].eq("manufacturer_month")].drop(
        columns=["projection_kind", "__dashboard_product_identity_id", "제품코드", "출고수량", "출고할증수량"]
    )
    try:
        assemble_sales_facts_from_stages(mixed_product, mixed_identity, mixed_manufacturer)
    except ValueError as exc:
        assert "one identity row per product code" in str(exc)
    else:
        raise AssertionError("mixed product identity must fail closed")
    print("PASS: dashboard split sales projection stage profiler static and fixture gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

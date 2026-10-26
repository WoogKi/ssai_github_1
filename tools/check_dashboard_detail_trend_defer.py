"""DB-free regression for Dashboard-only detail grain and trend work."""
from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import analytics_sales_trend_service as sales
from app.services import dashboard_lite_facts as dashboard


def main() -> int:
    def row(month: str, product: str, vendor: str, customer: str,
            quantity: int, supply: int, tax: int, returns: int = 0) -> dict[str, object]:
        return {
            "기준월": month, "제품코드": product, "제품명": f"제품{product}", "규격": "1T",
            "거래처코드": customer, "매입처코드": vendor, "매입처명": f"매입처{vendor}",
            "제조사코드": f"M{product}", "제조사명": f"제조사{product}",
            "출고수량": quantity, "출고할증수량": 0, "매출공급가액": supply,
            "매출세액": tax, "매출합계": supply + tax, "매출반품금액": returns,
            "출고건수": 1,
        }

    rows = pd.DataFrame([
        row("202607", "00001", "V1", "C1", 2, 20, 2),
        row("202607", "00001", "V1", "C2", 3, 30, 3),
        row("202608", "00001", "V1", "C1", -1, -10, -1, 11),
        row("202608", "00001", "V2", "C3", 4, 0, 0),
        row("202609", "00001", "V2", "C2", 5, 50, 5),
        row("202609", "00002", "V3", "C1", 7, 70, 7),
        row("202610", "00001", "V1", "C1", 1, 10, 1),
        row("202610", "00002", "V3", "C2", 0, 0, 0),
    ])
    sqls: list[str] = []

    def fake_query(sql: str, _params: object = None) -> pd.DataFrame:
        sqls.append(sql)
        if "AS 거래처코드" in sql:
            return rows.sort_values(["제품코드", "기준월", "거래처코드"]).reset_index(drop=True)
        keys = ["기준월", "제품코드", "제품명", "규격", "제조사코드", "제조사명", "매입처코드", "매입처명"]
        values = ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "매출반품금액", "출고건수"]
        return rows.groupby(keys, as_index=False)[values].sum().sort_values(
            ["제품코드", "기준월", "매입처코드"]
        ).reset_index(drop=True)

    params = {
        "date_from": "20260701", "date_to": "20261026",
        "month_from": "202607", "month_to": "202610", "evaluation_month": "202610", "source_mode": "detail",
        "vendor_group_list": ["0019:01"], "vendor_kind_list": ["0009:02"],
        "stock_cd_list": ["10001"],
    }
    with patch.object(sales, "query_to_df", side_effect=fake_query):
        baseline = sales.get_sales_trend_df(dict(params))
        bundle = sales.get_dashboard_sales_source_bundle(dict(params))
    assert len(sqls) == 2
    assert "AS 거래처코드" in sqls[0] and "AS 거래처코드" not in sqls[1]
    assert "AS 시도명" in sqls[0] and "AS 시도명" not in sqls[1]
    for predicate in (
        "Ven_Cd.Rd03_Ven_Group_Gcode", "Ven_Cd.Rd03_Ven_Kind_Gcode",
        "Out_Put.Rd12_Stock_Cd", "Out_Put.Rd12_Out_YyMmDd",
    ):
        assert predicate in sqls[0] and predicate in sqls[1], predicate
    assert sqls[0].split("WHERE 1 = 1", 1)[1].split("GROUP BY", 1)[0] == sqls[1].split("WHERE 1 = 1", 1)[1].split("GROUP BY", 1)[0]
    assert "ORDER BY" in sqls[0] and "ORDER BY" in sqls[1]
    assert bundle["representation"] == "legacy_row_bundle"
    candidate = bundle["sales_df"]
    assert len(baseline) == 8 and len(candidate) == 7
    assert "거래처수" not in baseline.columns and "거래처수" not in candidate.columns
    assert "출고건수" not in baseline.columns and "출고건수" not in candidate.columns
    keys = ["기준월", "제품코드", "매입처코드", "제조사코드"]
    amounts = ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "매출반품금액"]
    pd.testing.assert_frame_equal(
        baseline.groupby(keys, as_index=False)[amounts].sum().sort_values(keys).reset_index(drop=True),
        candidate.groupby(keys, as_index=False)[amounts].sum().sort_values(keys).reset_index(drop=True),
        check_dtype=False,
    )
    for column in ("출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계", "매출반품금액"):
        assert float(baseline[column].sum()) == float(candidate[column].sum()), column
    assert "월시점 추세판정" in baseline.columns and candidate["월시점 추세판정"].eq("").all()
    pd.testing.assert_frame_equal(
        sales.get_sales_trend_summary_df(dict(params), raw_df=baseline),
        sales.get_sales_trend_summary_df(dict(params), raw_df=candidate),
        check_dtype=False,
    )
    pd.testing.assert_frame_equal(
        sales.get_sales_forecast_df(dict(params), raw_df=baseline),
        sales.get_sales_forecast_df(dict(params), raw_df=candidate),
        check_dtype=False,
    )
    empty_purchase = pd.DataFrame()
    base_grains = sales.build_dashboard_sales_purchase_grains(
        baseline, empty_purchase, evaluation_month="202610", history_month_from="202607"
    )
    compact_grains = sales.build_dashboard_sales_purchase_grains(
        candidate, empty_purchase, evaluation_month="202610", history_month_from="202607"
    )
    for name in ("sales_product_month_df", "manufacturer_vendor_df"):
        pd.testing.assert_frame_equal(getattr(base_grains, name), getattr(compact_grains, name), check_dtype=False)
    assert dashboard._monthly_sales_actuals_from_source(baseline) == dashboard._monthly_sales_actuals_from_source(candidate)
    assert dashboard._monthly_sales_returns_from_source(baseline) == dashboard._monthly_sales_returns_from_source(candidate)
    assert dashboard._build_demand_surge_history_by_product(
        baseline, evaluation_month="202610", history_month_from="202607"
    ) == dashboard._build_demand_surge_history_by_product(
        candidate, evaluation_month="202610", history_month_from="202607"
    )
    assert float(candidate["출고수량"].sum()) == 21
    assert float(candidate["매출합계"].sum()) == 187
    print("PASS Dashboard detail: monthly/product/vendor/manufacturer sales, return, forecast and history Exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

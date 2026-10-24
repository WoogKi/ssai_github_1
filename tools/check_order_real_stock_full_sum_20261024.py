"""DB-free contract gate for order-only full-period real stock."""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import analytics_sales_trend_service as stock_service
from app.services import order_calculation_service as order_service


ROWS = [
    ("P1", "00001", "202001", "000", 10, 1, 0, 0),
    ("P1", "00001", "202610", "999", 0, 0, 4, 2),
    ("P1", "00001", "205001", "123", 3, 0, 0, 0),
    ("P1", "00001", "210001", "777", 0, 0, 15, 0),
    ("P1", "00002", "210001", "777", 100, 0, 0, 0),
    ("P2", "00001", "202401", "200", 2, 0, 0, 0),
]


def _query(calls: list, sql: str, bind: dict) -> pd.DataFrame:
    calls.append((sql, dict(bind)))
    assert "FROM dbo.Rddbc210 AS M" in sql and "Rddbc220" not in sql
    assert "SUM(ISNULL(M.Rd21_In_Quantity, 0) + ISNULL(M.Rd21_In_Oquantity, 0)" in sql
    assert "- ISNULL(M.Rd21_Out_Quantity, 0) - ISNULL(M.Rd21_Out_Oquantity, 0))" in sql
    assert "Stock_YyMm <=" not in sql and "Io_Gu_Gcode" not in sql
    assert "LEFT(LTRIM(RTRIM(M.Rd21_Io_Gu))" not in sql
    assert "FROM dbo.Rddbc110 AS T" not in sql and "FROM dbo.Rddbc120 AS T" not in sql
    assert "stock_month_to" not in bind and "io_gu_gcode" not in bind
    stocks = {value for key, value in bind.items() if key.startswith("stock_cd_")}
    if "P_SCOPE" in sql:
        products = {"P1", "P2"}
    else:
        products = {value for key, value in bind.items() if key.startswith("cd")}
    values = {}
    for code, location, _month, _io, inbound, bonus_in, outbound, bonus_out in ROWS:
        if code in products and location in stocks:
            values[code] = values.get(code, 0) + inbound + bonus_in - outbound - bonus_out
    return pd.DataFrame([
        {"제품코드": code, "실재고수량": qty, "실재고평가단가": 1.0, "실재고금액": qty}
        for code, qty in values.items()
    ])


def _run_loader(*, batch_size: int, location: str) -> tuple[pd.DataFrame, list]:
    calls = []
    with patch.object(stock_service, "_stock_query_batch_plan", return_value={
        "effective_chunk_size": batch_size,
        "configured_batch_size": batch_size,
        "fixed_parameter_count": 0,
        "stock_cd_parameter_count": 1,
        "io_gu_parameter_count": 0,
        "total_parameter_count": 2,
    }), patch.object(stock_service, "query_to_df", side_effect=lambda sql, bind: _query(calls, sql, bind)):
        result = stock_service._load_product_current_stock(
            ["P1", "P2"], stock_mode="real", month_to="202610",
            date_to="20261015", policy_date="20261015",
            stock_cd_list=[location],
            product_scope_params={"dashboard_product_group_list": ["0013:9998"]},
            order_real_stock_full_sum=True,
        )
    return result, calls


def main() -> None:
    for batch_size in (1, 1600):
        result, calls = _run_loader(batch_size=batch_size, location="00001")
        assert len(calls) == 1
        assert result.set_index("제품코드")["실재고수량"].to_dict() == {"P1": -7, "P2": 2}
        assert result.attrs["stock_cutoff_month"] == ""
        assert "전체기간" in result.attrs["stock_source_label"]
    result, _ = _run_loader(batch_size=1600, location="00002")
    assert result.set_index("제품코드")["실재고수량"].to_dict() == {"P1": 100}

    with patch.object(stock_service, "query_to_df", return_value=pd.DataFrame()) as query:
        stock_service._load_product_current_stock(
            ["P1"], stock_mode="real", month_to="202610",
            stock_cd_list=["00001"],
        )
    normal_sql = query.call_args.args[0]
    assert "Stock_YyMm <=" in normal_sql and "Io_Gu_Gcode" in normal_sql

    with patch.object(stock_service, "_load_product_current_stock", return_value=pd.DataFrame({
        "제품코드": ["P1"], "실재고수량": [-7], "실재고평가단가": [0], "실재고금액": [0],
    })) as loader:
        shortage = stock_service.get_stock_shortage_df(
            {"evaluation_month": "202610", "date_to": "20261015", "stock_mode": "book"},
            sales_forecast_df=pd.DataFrame({"제품코드": ["P1"], "예상기준월수량": [10]}),
            product_universe_df=pd.DataFrame({"제품코드": ["P1"]}),
            order_real_stock_full_sum=True,
        )
    assert loader.call_args.kwargs["stock_mode"] == "real"
    assert loader.call_args.kwargs["order_real_stock_full_sum"] is True
    assert shortage.iloc[0]["현재재고수량"] == -7
    assert shortage.attrs["stock_cutoff_month"] == ""
    assert shortage.attrs["use_hybrid_detail"] is False

    with patch.object(stock_service, "query_to_df") as query:
        try:
            stock_service._load_product_current_stock(
                ["P1"], stock_mode="book", month_to="202610",
                order_real_stock_full_sum=True,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("order stock accepted book mode")
        query.assert_not_called()

    source = Path(order_service.__file__).read_text(encoding="utf-8")
    assert "order_real_stock_full_sum=True" in source
    assert "stock = decimal_or_none(demand.get(\"현재재고수량\"))" in source
    print("order real stock full sum: PASS (past/current/2050/2100/all IO/negative/location)")
    print("order-only boundary and legacy stock SQL: PASS")


if __name__ == "__main__":
    main()

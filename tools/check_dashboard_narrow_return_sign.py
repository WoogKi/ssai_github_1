"""DB-free regression for the Dashboard narrow sales return-sign contract."""

from __future__ import annotations

import inspect
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import analytics_sales_trend_service as sales
from app.services.dashboard_narrow_sales_candidate_service import _sales_cte, _trim


CASES = {
    "sale": [("509", 10, 1, 1000, 100)],
    "return": [("601", -2, -1, -200, -20)],
    "mixed": [("509", 10, 1, 1000, 100), ("601", -2, -1, -200, -20)],
    "quantity_sign_independent": [("601", 2, 0, -200, -20)],
    "amount_sign_independent": [("601", -2, 0, 200, 20)],
    "other_outgoing": [("709", 3, 0, 300, 30), ("902", 4, 0, 400, 40)],
    "missing_values": [("509", None, None, None, None)],
}


def _numeric_select(source_mode: str) -> tuple[str, str]:
    prefix = "Rd22" if source_mode == "monthly_book" else "Rd21"
    params = {
        "company_id": 8,
        "source_mode": source_mode,
        "month_from": "202602",
        "month_to": "202607",
        "evaluation_month": "202608",
        "date_from": "20260201",
        "date_to": "20260731",
        "io_gu_list": ["509", "601"],
    }
    sql, _, meta = _sales_cte(params)
    assert meta["source_mode"] == source_mode
    match = re.search(r"NumericSales AS \(\s*(SELECT .*?)\s*FROM SalesRows\s*\)", sql, re.DOTALL)
    assert match is not None
    numeric_select = match.group(1)
    assert "THEN -1 *" not in numeric_select
    for column in ("Out_Quantity", "Out_Oquantity", "Out_Supply_Price", "Out_Tax_Price"):
        assert f"COALESCE({prefix}_{column}, 0)" in numeric_select
    assert "CASE WHEN" in numeric_select and "ABS(" in numeric_select
    numeric_select = numeric_select.replace(_trim("제품코드"), "제품코드")
    numeric_select = numeric_select.replace(_trim("매입처코드"), "매입처코드")
    return prefix, numeric_select.replace(
        f"LEFT({prefix}_Io_Gu, 1)", f"SUBSTR({prefix}_Io_Gu, 1, 1)"
    )


def _check_case(prefix: str, numeric_select: str, rows: list[tuple]) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE SalesRows (기준월 TEXT, 제품코드 TEXT, 매입처코드 TEXT, "
            f"{prefix}_Io_Gu TEXT, {prefix}_Out_Quantity INTEGER, "
            f"{prefix}_Out_Oquantity INTEGER, {prefix}_Out_Supply_Price INTEGER, "
            f"{prefix}_Out_Tax_Price INTEGER)"
        )
        connection.executemany(
            "INSERT INTO SalesRows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [("202607", "P1", "V1", *row) for row in rows],
        )
        cursor = connection.execute(numeric_select + " FROM SalesRows")
        names = [column[0] for column in cursor.description]
        actual = [dict(zip(names, record)) for record in cursor.fetchall()]
    finally:
        connection.close()
    assert len(actual) == len(rows)
    for result, (io, qty, bonus, supply, tax) in zip(actual, rows):
        quantity = qty or 0
        bonus_quantity = bonus or 0
        supply_amount = supply or 0
        tax_amount = tax or 0
        assert result["출고수량"] == quantity
        assert result["출고할증수량"] == bonus_quantity
        assert result["매출공급가액"] == supply_amount
        assert result["매출세액"] == tax_amount
        assert result["매출합계"] == supply_amount + tax_amount
        assert result["매출반품금액"] == (
            abs(supply_amount + tax_amount) if io.startswith("6") else 0
        )
    assert sum(row["매출합계"] for row in actual) == sum((r[3] or 0) + (r[4] or 0) for r in rows)


def main() -> None:
    legacy = inspect.getsource(sales._get_sales_trend_monthly_df_fast)
    dashboard_legacy = inspect.getsource(sales.get_dashboard_sales_source_bundle)
    detail = inspect.getsource(sales.get_sales_trend_detail_df)
    assert "SUM(COALESCE(M.{p}_Out_Quantity, 0))" in legacy
    assert "SUM(COALESCE(M.{p}_Out_Supply_Price, 0))" in legacy
    assert "SUM(COALESCE(M.{p}_Out_Quantity, 0))" in dashboard_legacy
    assert "SUM(COALESCE(M.{p}_Out_Supply_Price, 0))" in dashboard_legacy
    assert "SUM(COALESCE(M.{p}_Out_Tax_Price, 0))" in dashboard_legacy
    assert "SUM(COALESCE(Out_Put.Rd12_Quantity, 0))" in detail
    assert "SUM(COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0))" in detail
    for source_mode in ("monthly_book", "monthly_real"):
        prefix, numeric_select = _numeric_select(source_mode)
        for rows in CASES.values():
            _check_case(prefix, numeric_select, rows)
    print(f"PASS: {len(CASES) * 2} narrow return-sign cases (R220/R210); legacy/detail raw-sign contract")


if __name__ == "__main__":
    main()

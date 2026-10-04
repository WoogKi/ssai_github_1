"""Offline unit-scope chunk completeness and timeout-error preservation gates."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import pandas as pd
import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import mssql_client as db
from app.services import order_calculation_service as calculation
from app.services import rddbc170_rddbc180_order_service as orders
from app.services.order_calculation_contract import recommend_quantity
from tools.check_order_editor_unit_perf_closeout_20261005 import check_editor_revision


COLS = ["제품코드", "발주일자", "발주거래처코드", "발주순번", "상세순번", "발주수량"]


def _params(codes):
    return {
        "date_from": "20260706", "date_to": "20261006",
        "status_codes": ["1", "2", "3"],
        "_order_unit_history_minimal": True,
        "_order_unit_selected_scope": True,
        "order_unit_product_code_list": codes,
        "product_prescription_semantic": "prescription",
        "order_vendor_cd": "V", "cost_apply_cd": "50002",
        "stock_apply_cd": "50001", "stock_cd_list": ["S1", "S2"],
        "include_blank_stock_cd": True,
    }


def check_single_source_modes() -> None:
    codes = [f"{index:05d}" for index in range(1000)] + ["A&B", "", "A&B"]
    large_codes = list(dict.fromkeys(code for code in codes if code))

    def row(code, sequence):
        return {"제품코드": code, "발주일자": "20261001", "발주거래처코드": "V",
                "발주순번": sequence, "상세순번": 1, "발주수량": Decimal(10)}

    def read_small(sql, values):
        assert "SELECT TOP" not in sql and "Rddbc040" not in sql
        assert "CAST(? AS xml)" in sql and "Rd18_Or_Di IN (?,?,?)" in sql
        assert "Rd18_OrVen_Cd = ?" in sql and "Rd18_Cost_Apply_Cd = ?" in sql
        assert "Rd18_Stock_Apply_Cd = ?" in sql and "Rd18_Stock_Cd IN (?,?)" in sql
        assert "NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL" in sql
        xml = next(value for value in values if isinstance(value, str) and value.startswith("<codes>"))
        assert [node.text for node in ElementTree.fromstring(xml)] == ["A&B"]
        return pd.DataFrame([row("A&B", 1)], columns=COLS)

    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", side_effect=read_small) as query:
        small = orders.get_order_df(_params(["A&B", "A&B", ""]), mode="order")
        assert query.call_count == 1
    assert small.attrs["order_history_query_mode"] == "unit_inference_r170_r180_scoped_sql"
    assert small.attrs["order_unit_history_complete_scope"] is True
    assert small.attrs["order_unit_scope_product_count"] == 1

    for count in (10, 100, 1000):
        with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
             patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
             patch.object(orders, "execute_bound_select", return_value=pd.DataFrame(columns=COLS)) as query:
            result = orders.get_order_df(_params(codes[:count]), mode="order")
            assert query.call_count == 1
            assert "CAST(? AS xml)" in query.call_args.args[0]
            assert result.attrs["order_unit_history_complete_scope"] is True

    def read_large(sql, values):
        assert "SELECT TOP" not in sql and "CAST(? AS xml)" not in sql
        assert "ORDER BY" not in sql and "Rddbc040" not in sql
        assert "Rd18_Or_Di IN (?,?,?)" in sql
        assert "Rd18_OrVen_Cd = ?" in sql and "Rd18_Cost_Apply_Cd = ?" in sql
        assert "Rd18_Stock_Apply_Cd = ?" in sql and "Rd18_Stock_Cd IN (?,?)" in sql
        assert "NULLIF(RTRIM(D.Rd18_Stock_Cd), '') IS NULL" in sql
        assert not any(str(value).startswith("<codes>") for value in values)
        return pd.DataFrame([row("OUTSIDE", 1), row("A&B", 2), row("00001", 3)], columns=COLS)

    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", side_effect=read_large) as query:
        large = orders.get_order_df(_params(codes), mode="order")
        assert query.call_count == 1
    assert set(large["제품코드"]) == {"A&B", "00001"}
    assert large.attrs["order_history_query_mode"] == "unit_inference_r170_r180_unscoped_python"
    assert large.attrs["order_unit_raw_source_rows"] == 3
    assert large.attrs["order_unit_scope_out_rows"] == 1
    assert large.attrs["order_unit_scope_product_count"] == len(large_codes)
    assert large.attrs["order_unit_physical_source_calls"] == 1
    assert large.attrs["order_unit_history_complete_scope"] is True
    assert calculation._unit_history_completeness(large, recent_start="20260906", top=2) == (True, True)

    empty = pd.DataFrame(columns=COLS)
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", return_value=empty) as query:
        result = orders.get_order_df(_params([f"{index:05d}" for index in range(25001)]), mode="order")
        assert query.call_count == 1 and result.attrs["order_unit_history_complete_scope"] is True


def check_failure_and_duplicate() -> None:
    codes = [f"{index:05d}" for index in range(1001)]
    first = pd.DataFrame([{"제품코드": codes[0], "발주일자": "20261001",
                           "발주거래처코드": "V", "발주순번": 1,
                           "상세순번": 1, "발주수량": Decimal(10)}])
    outside = first.copy()
    outside.loc[0, "제품코드"] = "OUTSIDE"
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", return_value=pd.concat([outside, first, first], ignore_index=True)) as query:
        duplicate = orders.get_order_df(_params(codes), mode="order")
        assert query.call_count == 1
        assert duplicate.attrs["order_unit_scope_duplicate_count"] == 1
        assert duplicate.attrs["order_unit_history_complete_scope"] is False
        assert calculation._unit_history_completeness(duplicate, recent_start="20260906", top=2) == (False, False)
    older = first.copy()
    older.loc[0, "발주일자"] = "20260707"
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", return_value=pd.concat([first, older, older], ignore_index=True)):
        duplicate_old = orders.get_order_df(_params(codes), mode="order")
        assert calculation._unit_history_completeness(duplicate_old, recent_start="20260906", top=100000) == (False, False)
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", return_value=pd.concat([outside, outside, first], ignore_index=True)):
        unaffected = orders.get_order_df(_params(codes), mode="order")
        assert unaffected.attrs["order_unit_scope_duplicate_count"] == 0
        assert unaffected.attrs["order_unit_history_complete_scope"] is True
    with patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB forbidden")), \
         patch("pyodbc.connect", side_effect=AssertionError("DB forbidden")), \
         patch.object(orders, "execute_bound_select", side_effect=RuntimeError("single source failed")) as query:
        try:
            orders.get_order_df(_params(codes), mode="order")
        except RuntimeError as exc:
            assert str(exc) == "single source failed"
        else:
            raise AssertionError("partial source escaped")
        assert query.call_count == 1
    assert recommend_quantity(Decimal("0.7"), None, increasing=False, default_unit_allowed=True) == 10
    assert recommend_quantity(Decimal("0.7"), None, increasing=False, default_unit_allowed=False) == 0
    assert recommend_quantity(Decimal("0.16"), Decimal(100), increasing=True,
                              default_unit_allowed=True, price=Decimal(1000000)) == 1


def check_original_error_preserved() -> None:
    class Driver:
        def __init__(self):
            self._timeout = 5

        @property
        def timeout(self):
            return self._timeout

        @timeout.setter
        def timeout(self, value):
            if value == 5:
                raise pyodbc.ProgrammingError("closed connection")
            self._timeout = value

    class Connection:
        def __init__(self):
            self.connection = type("Proxy", (), {"driver_connection": Driver()})()
            self.closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    engine = type("Engine", (), {"connect": lambda self: connection})()
    with patch.object(db, "_get_engine", return_value=engine):
        with db.read_only_request(timeout_seconds=120):
            try:
                with db.get_conn():
                    raise RuntimeError("original timeout")
            except RuntimeError as exc:
                assert str(exc) == "original timeout"
            else:
                raise AssertionError("original error was masked")
    assert connection.closed


if __name__ == "__main__":
    check_single_source_modes()
    check_failure_and_duplicate()
    check_original_error_preserved()
    check_editor_revision()
    print("PASS single unit scope, completeness, timeout error and editor regression; DB attempts=0")

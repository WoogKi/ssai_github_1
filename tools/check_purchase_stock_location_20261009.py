"""Offline R170/R180 location and order-calculation scope contract."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.services.order_calculation_service import build_pending_params
from app.services.order_stock_location_scope import (
    prepare_order_stock_scope, resolve_order_stock_scope,
)
from app.services import rddbc170_rddbc180_order_service as orders


def main() -> int:
    names = {
        "00001": "(본사 창고)", "00247": "(전주)창고",
        "00004": "전주 물류창고", "00901": "오토스토어",
    }
    default = ["00001", "00247", "00901"]
    cases = (
        ("발주 조회 재고위치 00001", ["00001"]),
        ("입고예정자료 조회 재고위치 (전주)", ["00247"]),
        ("발주 계산 재고위치 본사, 오토", ["00001", "00901"]),
        ("발주 조회 재고위치 00001 + 00247", ["00001", "00247"]),
        ("입고예정 조회 재고위치 00001/00901", ["00001", "00901"]),
        ("발주 계산 재고위치 본사와 오토", ["00001", "00901"]),
        ("발주 조회 재고위치 00001, 오토", ["00001", "00901"]),
        ("발주 조회 재고위치 00004", ["00004"]),
        ("입고예정자료 조회 재고위치 본사, (전주)", ["00001", "00247"]),
    )
    for question, expected in cases:
        parsed = resolve_registered_erp_table_nlq(question, today=date(2026, 10, 9))
        assert parsed, question
        scoped, error, _ = resolve_order_stock_scope(
            parsed["params"], default_codes=default, registered_names=names,
        )
        assert not error and scoped["stock_cd_list"] == expected, (question, scoped, error)
        assert not scoped.get("stock_nm"), question
    for question, action in (
        ("발주 조회", "발주조회"),
        ("입고예정 조회", "입고예정조회"),
        ("발주 계산", "발주 계산"),
    ):
        parsed = resolve_registered_erp_table_nlq(question, today=date(2026, 10, 9))
        assert parsed["action"] == action
        scoped, error, _ = resolve_order_stock_scope(
            parsed["params"], default_codes=default, registered_names=names,
        )
        assert not error and scoped["stock_cd_list"] == default
        alternate_default, error, _ = resolve_order_stock_scope(
            parsed["params"], default_codes=["00247", "00004"], registered_names=names,
        )
        assert not error and alternate_default["stock_cd_list"] == ["00247", "00004"]
        normalized = orders.normalize_order_params(
            {**alternate_default, "date_from": "20261001", "date_to": "20261009"},
            mode="expected",
        )
        clauses, _ = orders._filters(normalized, mode="expected")
        assert not normalized["include_blank_stock_cd"]
        assert "OR NULLIF(LTRIM(RTRIM(D.Rd18_Stock_Cd)), '') IS NULL" not in " ".join(clauses)
    for requested in (["00001", "99999"], ["99999"], []):
        selected, error, _ = resolve_order_stock_scope(
            {"stock_cd_list": requested, "_order_stock_terms_invalid": not requested},
            default_codes=default, registered_names=names,
        )
        assert error and not selected.get("_order_stock_scope_resolved")
    other_company, error, _ = resolve_order_stock_scope(
        {}, default_codes=["00901"], registered_names=names,
    )
    assert not error and other_company["stock_cd_list"] == ["00901"]
    exact, error, _ = resolve_order_stock_scope(
        {"stock_nm": "(본사 창고)"}, default_codes=default,
        registered_names={**names, "00004": "(본사 창고 별관)"},
    )
    assert not error and exact["stock_cd_list"] == ["00001"]
    _, error, _ = resolve_order_stock_scope(
        {"stock_nm": "본사"}, default_codes=default,
        registered_names={**names, "00004": "(본사 창고 별관)"},
    )
    assert error == "location_name_ambiguous"
    for request, expected in (
        ({}, default),
        ({"stock_cd_list": ["00001"]}, ["00001"]),
        ({"stock_cd_list": ["00247"]}, ["00247"]),
        ({"stock_cd_list": ["00001", "00247"]}, ["00001", "00247"]),
        ({"stock_cd_list": ["00001"], "stock_nm_list": ["오토"]}, ["00001", "00901"]),
    ):
        selected, error, _ = resolve_order_stock_scope(
            request, default_codes=default, registered_names=names, saved_only=True,
        )
        assert not error and selected["stock_cd_list"] == expected
        assert selected["_order_stock_scope_policy"] == "saved"
        if len(expected) > 1:
            assert selected["stock_cd"] == "" and selected["stock_cd_list"] == expected
    for request in ({"stock_cd": "00004"}, {"stock_cd_list": ["00001", "00004"]}):
        _, error, _ = resolve_order_stock_scope(
            request, default_codes=default, registered_names=names, saved_only=True,
        )
        assert error == "outside_registered_locations"
        allowed, allowed_error, _ = resolve_order_stock_scope(
            request, default_codes=default, registered_names=names,
        )
        assert not allowed_error and "00004" in allowed["stock_cd_list"]
    ambiguous = resolve_registered_erp_table_nlq("발주 조회 재고위치 전주", today=date(2026, 10, 9))
    _, error, _ = resolve_order_stock_scope(
        ambiguous["params"], default_codes=default, registered_names=names,
    )
    assert error == "location_name_ambiguous"
    with patch("app.db.mssql_client.get_current_company_id", return_value=4):
        _, error, _ = prepare_order_stock_scope(
            {"company_id": 7, "stock_cd": "00001"},
            default_codes=default, registered_names=names,
        )
        assert error == "company_mismatch"

    from app.sims.nlq.nlq_router import _try_handle_io_nlq

    def scoped_fixture(params, **kwargs):
        return resolve_order_stock_scope(
            params, default_codes=default, registered_names=names,
            saved_only=bool(kwargs.get("saved_only")),
        )

    for question, expected_action, expected_codes in (
        ("발주 조회 재고위치 본사 + 오토", "발주조회", ["00001", "00901"]),
        ("발주 조회 재고위치 본사, (전주)", "발주조회", ["00001", "00247"]),
        ("입고예정자료 조회 재고위치 00247", "입고예정조회", ["00247"]),
        ("발주 계산 재고위치 00001,00247", "발주 계산", ["00001", "00247"]),
        ("발주 조회 재고위치 00004", "발주조회", ["00004"]),
        ("입고예정자료 조회 재고위치 00004", "입고예정조회", ["00004"]),
    ):
        sent, called = [], []

        def service_result(params=None):
            called.append(dict(params or {}))
            return {"final": True, "type": "text", "title": expected_action,
                    "action": expected_action, "params": params, "data": "fixture",
                    "message": "fixture", "meta": {"result_status": "success", "source_call_count": 1}}

        service_path = {
            "발주조회": "app.services.rddbc170_rddbc180_order_service.get_order_result",
            "입고예정조회": "app.services.rddbc170_rddbc180_order_service.get_expected_inbound_result",
            "발주 계산": "app.services.order_calculation_service.get_order_calculation_result",
        }[expected_action]
        with patch("app.services.order_stock_location_scope.prepare_order_stock_scope", side_effect=scoped_fixture), \
             patch(service_path, side_effect=service_result), \
             patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: sent.append((payload, action)) or payload.get("meta")):
            assert _try_handle_io_nlq(question, room={}, session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1, logger=logging.getLogger("order.scope.fixture"))
        assert len(called) == 1 and called[0]["stock_cd_list"] == expected_codes, (question, called)
        assert sent and sent[-1][1] == expected_action
    for invalid_question in (
        "발주 조회 재고위치 00001,99999", "발주 조회 재고위치 전주",
        "발주 계산 재고위치 00004", "발주 계산 재고위치 00001,00004",
    ):
        sent, called = [], []
        action = "발주 계산" if "계산" in invalid_question else "발주조회"
        service_path = (
            "app.services.order_calculation_service.get_order_calculation_result"
            if action == "발주 계산" else "app.services.rddbc170_rddbc180_order_service.get_order_result"
        )
        with patch("app.services.order_stock_location_scope.prepare_order_stock_scope", side_effect=scoped_fixture), \
             patch(service_path, side_effect=lambda params=None: called.append(params)), \
             patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, action: sent.append(payload) or payload.get("meta")):
            assert _try_handle_io_nlq(invalid_question, room={}, session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1, logger=logging.getLogger("order.scope.fixture"))
        assert not called and sent[-1]["meta"]["result_status"] == "input_required"

    from app.ui.current_table_followups.generic import _strip_common_filter_value

    values = pd.Series(["삼진", "동제약품"])
    for query_tail in ("삼진 보여줘 TOP 1", "삼진 TOP 1 보여줘", "삼진보여줘top1"):
        assert _strip_common_filter_value(query_tail, known_values=values) == "삼진"
    assert _strip_common_filter_value("동제약품 상세히 보여줘", known_values=values) == "동제약품"

    assert "COALESCE(NULLIF(LTRIM(RTRIM(D.Rd18_Stock_Cd)), ''), '00001')" in orders._SELECT_COLUMNS
    assert "ST.Rd01_Gcode = '0018'" in orders._JOINS
    assert orders._ORDER_STOCK_CD_SQL in orders._JOINS
    for mode in ("order", "expected"):
        for scope, blanks in ((["00001"], True), (["00247"], False), (["00001", "00247"], True)):
            params = {"stock_cd_list": scope, "date_from": "20261001", "date_to": "20261009"}
            normalized = orders.normalize_order_params(params, mode=mode)
            clauses, binds = orders._filters(normalized, mode=mode)
            sql = " ".join(clauses)
            assert ("NULLIF(LTRIM(RTRIM(D.Rd18_Stock_Cd)), '') IS NULL" in sql) is blanks
            assert all(code in binds for code in scope)
            assert normalized["include_blank_stock_cd"] is blanks
    pending_params = build_pending_params({"policy_date": "20261009", "stock_cd_list": ["00247"]})
    assert not pending_params.get("include_blank_stock_cd")
    with patch.object(orders, "execute_bound_select", return_value=pd.DataFrame()) as select:
        orders.get_expected_inbound_product_totals({"stock_cd_list": ["00001", "00247"], "date_from": "20261001", "date_to": "20261009"})
    sql, binds = select.call_args.args
    assert orders._ORDER_STOCK_CD_SQL in sql
    assert "NULLIF(LTRIM(RTRIM(D.Rd18_Stock_Cd)), '') IS NULL" in sql
    assert binds.count("00001") == 1 and binds.count("00247") == 1
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE Rddbc180 (Rd18_Stock_Cd TEXT, Rd18_Quantity INTEGER, "
            "Rd18_Oquantity INTEGER, Rd18_In_Quantity INTEGER, Rd18_Or_Di TEXT)"
        )
        connection.executemany(
            "INSERT INTO Rddbc180 VALUES (?,?,?,?,?)",
            ((None, 10, 0, 0, "1"), ("", 4, 0, 0, "1"), ("   ", 6, 0, 0, "1"),
             ("00001", 3, 0, 1, "2"), ("00247", 8, 0, 2, "2"),
             ("00247", 7, 0, 0, "3")),
        )
        for scope, expected in ((["00001"], [("00001", 22)]),
                                (["00247"], [("00247", 6)]),
                                (["00001", "00247"], [("00001", 22), ("00247", 6)])):
            clauses, values = [], []
            orders._append_order_stock_filter(clauses, values, {"stock_cd_list": scope})
            observed = connection.execute(
                f"SELECT {orders._ORDER_STOCK_CD_SQL}, "
                "SUM(CASE D.Rd18_Or_Di WHEN '1' THEN D.Rd18_Quantity + D.Rd18_Oquantity "
                "WHEN '2' THEN D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity "
                "ELSE 0 END) FROM Rddbc180 AS D "
                f"WHERE {' AND '.join(clauses)} GROUP BY {orders._ORDER_STOCK_CD_SQL} "
                f"ORDER BY {orders._ORDER_STOCK_CD_SQL}", values,
            ).fetchall()
            assert observed == expected, (scope, observed)
            from app.services.analytics_sales_trend_service import _merge_expected_inbound_projection

            pending = sum(quantity for _, quantity in observed)
            shortage = pd.DataFrame({"제품코드": ["fixture-product"], "부족예상수량": [30]})
            projection = pd.DataFrame({"제품코드": ["fixture-product"], "입고예정수량": [pending]})
            projection.attrs["source_call_count"] = 1
            attached = _merge_expected_inbound_projection(shortage, projection)
            assert attached.loc[0, "입고예정수량"] == pending
            assert attached.loc[0, "입고예정 반영 부족수량"] == 30 - pending
            assert attached.attrs["expected_inbound_source_call_count"] == 1
    print("PASS order/expected/calculation location parsing, default, registration, company isolation and SQL scope; ERP calls 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

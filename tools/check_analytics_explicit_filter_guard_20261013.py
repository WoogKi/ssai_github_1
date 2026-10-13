"""Offline semantic and service-call checks for customer-grain forecast filters."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sims.nlq import nlq_router as router
from app.services import analytics_customer_sales_forecast_service as forecast


def _run(question: str, *, authorized: bool = True) -> tuple[dict, int]:
    sent: list[dict] = []
    service_calls: list[dict] = []

    def service(params: dict) -> dict:
        service_calls.append(dict(params))
        return {"final": True, "type": "text", "data": "fixture", "meta": {"row_count": 1}}

    with (
        patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: sent.append(payload)),
        patch("app.ui.ssai_login.has_permission", return_value=authorized),
        patch.object(router, "_get_analytics_handler", return_value=service),
        patch.object(router, "_apply_company_default_to_analytics_nlq", side_effect=lambda params, **_kwargs: params),
        patch.object(router, "_resolve_analytics_manufacturer_filter", side_effect=lambda _text, params, _intent, _logger: {"status": "not_needed", "params": params}),
    ):
        assert router.try_handle_nlq(
            question, room={"messages": []}, session_state={},
            make_ts=lambda: "now", next_seq=lambda: 1, logger=logging.getLogger(__name__),
        ), question
    assert len(sent) == 1, question
    return sent[0], len(service_calls)


def _run_with_sql_mock(question: str) -> tuple[dict, list[tuple[str, dict]]]:
    sent: list[dict] = []
    sql_calls: list[tuple[str, dict]] = []

    def query(sql: str, params: dict) -> pd.DataFrame:
        sql_calls.append((sql, dict(params)))
        return pd.DataFrame()

    def service(params: dict) -> dict:
        forecast._load_customer_master(params)
        return {"final": True, "type": "text", "data": "fixture", "meta": {"row_count": 1}}

    with (
        patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: sent.append(payload)),
        patch("app.ui.ssai_login.has_permission", return_value=True),
        patch.object(router, "_get_analytics_handler", return_value=service),
        patch.object(router, "_apply_company_default_to_analytics_nlq", side_effect=lambda params, **_kwargs: params),
        patch.object(router, "_resolve_analytics_manufacturer_filter", side_effect=lambda _text, params, _intent, _logger: {"status": "not_needed", "params": params}),
        patch.object(forecast, "query_to_df", side_effect=query),
    ):
        assert router.try_handle_nlq(
            question, room={"messages": []}, session_state={},
            make_ts=lambda: "now", next_seq=lambda: 1, logger=logging.getLogger(__name__),
        ), question
    assert len(sent) == 1, question
    return sent[0], sql_calls


def main() -> None:
    blocked = (
        ("지역별 매출예상 제약사 가상제약", "지역별 매출 예상", "제약사 가상제약"),
        ("영업사원별 매출 예상 제약사 가상제약", "영업사원별 매출 예상", "제약사 가상제약"),
        ("매출처별 매출 예상 제약사 가상제약", "매출처별 매출 예상", "제약사 가상제약"),
        ("지역별 매출 예상 발주처 가상발주처", "지역별 매출 예상", "발주처 가상발주처"),
        ("지역별 매출 예상 제품 가상제품", "지역별 매출 예상", "제품 가상제품"),
    )
    for question, action, condition in blocked:
        assert router._resolve_analytics_action(question) == action
        payload, calls = _run(question)
        assert calls == 0 and payload["meta"]["source_call_count"] == 0
        assert payload["meta"]["result_status"] == "input_required"
        assert condition in payload["meta"]["unsupported_conditions"]
        assert condition in payload["message"] and "실행하지 않았습니다" in payload["message"]
        assert f"'{action}'" in payload["message"]
    denied, calls = _run(blocked[0][0], authorized=False)
    assert calls == 0 and "'지역별 매출 예상'" not in denied["message"]

    supported = (
        ("지역별 매출 예상", "지역별 매출 예상", None, None),
        ("지역별 매출 예상 시도 서울", "지역별 매출 예상", "sido_nm", "서울"),
        ("영업사원별 매출 예상 영업사원 김", "영업사원별 매출 예상", "sales_man_nm", "김"),
        ("매출처별 매출 예상 거래처 가상거래처", "매출처별 매출 예상", "ven_nm", "가상거래처"),
    )
    for question, action, key, value in supported:
        assert router._resolve_analytics_action(question) == action
        params = router._build_analytics_params(question, action)
        assert not router._unsupported_customer_forecast_filters(action, params)
        payload, calls = _run(question)
        assert calls == 1 and payload["action"] == action
        if key:
            assert payload["params"][key] == value
            assert key in forecast.CUSTOMER_SALES_FORECAST_FILTER_KEYS

    for question, action in (
        ("품목별 매출 예상 제약사 가상제약", "품목별 매출 예상"),
        ("제약사별 매출 추세 분석", "제약사별 매출 추세 분석"),
    ):
        assert router._resolve_analytics_action(question) == action
        assert not router._unsupported_customer_forecast_filters(
            action, router._build_analytics_params(question, action),
        )
    assert forecast._has_master_filter({"sales_man_cd": "00001"})
    assert forecast._has_master_filter({"sido_nm": "서울"})

    for question, condition in (
        ("지역별 매출 예상 시도 서울 + 경기", "시도 서울 + 경기"),
        ("지역별 매출 예상 시도 서울/경기", "시도 서울/경기"),
        ("지역별 매출 예상 TOP 5", "TOP 5"),
        ("영업사원별 매출 예상 제약사 한미", "제약사 한미"),
        ("매출처별 매출 예상 제약사 한미", "제약사 한미"),
    ):
        payload, sql_calls = _run_with_sql_mock(question)
        assert not sql_calls and payload["meta"]["source_call_count"] == 0, question
        assert condition in payload["meta"]["unsupported_conditions"], question

    sql_cases = (
        ("지역별 매출 예상", {}, ()),
        ("지역별 매출 예상 시도 서울", {"sido_nm_like": "%서울%"}, ("Rd021_Sido",)),
        ("지역별 매출 예상 시도 서울 시군구 강남구", {"sido_nm_like": "%서울%", "gugun_nm_like": "%강남구%"}, ("Rd021_Sido", "Rd021_Gugun")),
        ("영업사원별 매출 예상 영업사원 김", {"sales_man_nm_like": "%김%"}, ("Rd06_User_Nm",)),
        ("영업사원별 매출 예상 영업사원코드 00001", {"sales_man_cd": "00001"}, ("Rd03_Sales_Man",)),
        ("매출처별 매출 예상 거래처 희성", {"ven_nm_like": "%희성%"}, ("Rd03_Ven_Nm",)),
    )
    for question, expected_params, fields in sql_cases:
        payload, sql_calls = _run_with_sql_mock(question)
        assert len(sql_calls) == 1, question
        sql, bindings = sql_calls[0]
        assert "FROM dbo.Rddbc030 AS V" in sql, question
        assert bindings == {"dummy": 1, **expected_params}, (question, bindings)
        for field in fields:
            assert field in sql, (question, field)
        assert payload["meta"]["result_status"] == "success", question

    from app.services.io_nlq import resolve_io_nlq
    from app.ui.knowledge_chat_adapter import parse_incomplete_sims_help_request

    assert parse_incomplete_sims_help_request("지역별 매출현황") is not None
    assert resolve_io_nlq("오늘 매출현황")["action"] == "출고명세 조회"
    print("PASS: analytics explicit filter guard, permissions, supported filters, adjacent routing; ERP=0 for blocked")


if __name__ == "__main__":
    main()

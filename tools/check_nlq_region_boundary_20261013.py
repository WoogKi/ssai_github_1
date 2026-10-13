"""Offline boundary checks for grouped sales status versus registered forecasts."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq.nlq_router import (
    _build_analytics_params,
    _resolve_analytics_action,
    _unregistered_grouped_sales_status,
    resolve_new_sims_nlq_candidate,
    try_handle_nlq,
)
from app.ui.knowledge_chat_adapter import (
    parse_incomplete_sims_help_request,
    verified_sims_help_examples,
    verified_sims_help_text,
)


def main() -> None:
    allowed = {"지역별 매출 예상", "영업사원별 매출 예상"}
    for query in (
        "지역별 매출현황", "지역별매출현황 조회",
        "지역별 매출현황 제약사 가상제약사",
        "지역별 매출현황 영업사원 가상민우",
        "영업사원별 매출현황",
    ):
        assert _unregistered_grouped_sales_status(query)
        intent = parse_incomplete_sims_help_request(query)
        assert intent and intent.reason == "unregistered_grouped_sales_status", query
        assert resolve_new_sims_nlq_candidate(query) is None, query
        room = {"messages": []}
        with patch("app.services.io_nlq.resolve_io_nlq", side_effect=AssertionError("IO invoked")):
            handled = try_handle_nlq(query, room, {}, lambda: "now", lambda: 1, logging.getLogger(__name__))
        assert not handled and not room["messages"], query
        text = verified_sims_help_text(intent, allowed_actions=allowed)
        assert "실행하지 않았습니다" in text and "매출현황과 매출 예상은 서로 다른 개념" in text
        examples = verified_sims_help_examples(intent, allowed_actions=allowed)
        if query == "지역별 매출현황":
            assert examples == (("지역별 매출 예상", "지역별 매출 예상"),)
            assert "지역별 매출 예상" in text
        elif "가상" in query:
            assert not examples, query

    intent = parse_incomplete_sims_help_request("지역별 매출현황")
    assert intent is not None
    assert not verified_sims_help_examples(intent, allowed_actions=set())
    assert "지역별 매출 예상" not in verified_sims_help_text(intent, allowed_actions=set())

    forecast = "지역별 매출 예상 영업사원 가상민우"
    assert parse_incomplete_sims_help_request(forecast) is None
    assert _resolve_analytics_action(forecast) == "지역별 매출 예상"
    assert resolve_new_sims_nlq_candidate(forecast) == {"route": "analytics", "action": "지역별 매출 예상"}
    assert _build_analytics_params(forecast, "지역별 매출 예상")["sales_man_nm"] == "가상민우"
    for query, action in (
        ("매출처별 매출 예상", "매출처별 매출 예상"),
        ("영업사원별 매출 예상", "영업사원별 매출 예상"),
        ("지역별 매출 예상", "지역별 매출 예상"),
    ):
        assert parse_incomplete_sims_help_request(query) is None
        assert _resolve_analytics_action(query) == action

    for query in ("오늘 매출현황", "거래처 가상거래처 오늘 매출현황"):
        assert parse_incomplete_sims_help_request(query) is None
        assert _unregistered_grouped_sales_status(query) is None
        parsed = resolve_io_nlq(query, today=date(2026, 10, 13))
        assert parsed and parsed["action"] == "출고명세 조회"
        assert parsed["params"]["date_from"] == "20261013"
        if "가상거래처" in query:
            assert parsed["params"]["ven_nm"] == "가상거래처"
    print("PASS: grouped sales status help boundary, permissions, parser, ERP=0; existing IO/forecast routes")


if __name__ == "__main__":
    main()

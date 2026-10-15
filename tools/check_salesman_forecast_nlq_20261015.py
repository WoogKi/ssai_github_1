"""Offline routing contract for salesperson sales-forecast expressions."""

import logging

from app.sims.nlq import nlq_router
from app.services import io_nlq
from app.ui.knowledge_chat_adapter import (
    parse_incomplete_sims_help_request,
    verified_sims_help_examples,
    verified_sims_help_text,
)


FORECAST_ACTION = "영업사원별 매출 예상"


def main() -> None:
    for question in (
        "영업사원별 매출 예상",
        "영업사원별 매출예측",
    ):
        assert nlq_router.resolve_new_sims_nlq_candidate(question) == {
            "route": "analytics", "action": FORECAST_ACTION,
        }, question
        intent = nlq_router._classify_analytics_metric_grouping(question)
        assert (intent["requested_metric"], intent["requested_grouping"]) == (
            "sales_forecast", "salesperson",
        ), question
        assert nlq_router._analytics_grouping_guard(question, FORECAST_ACTION) is None
        assert nlq_router._strip_analytics_action_phrases(question, FORECAST_ACTION) == ""
        params = nlq_router._build_analytics_params(question, FORECAST_ACTION)
        assert not params.get("sales_man_nm"), (question, params)
        assert nlq_router._get_analytics_handler(FORECAST_ACTION).__name__ == (
            "get_salesperson_sales_forecast_result"
        )

    for question in ("영업사원별 매출예산", "영업사원별 매출 예산 조회"):
        intent = parse_incomplete_sims_help_request(question)
        assert intent and intent.reason == "unregistered_grouped_sales_budget", question
        assert nlq_router.resolve_new_sims_nlq_candidate(question) is None
        assert nlq_router._resolve_analytics_action(question) is None
        assert verified_sims_help_examples(intent, allowed_actions={FORECAST_ACTION}) == (
            (FORECAST_ACTION, FORECAST_ACTION),
        ), question
        answer = verified_sims_help_text(intent, allowed_actions={FORECAST_ACTION})
        assert "매출예산은 매출 예상과 다른 업무" in answer
        assert "예산 조회는 실행하지 않았습니다" in answer
        assert "매출 예측을 원하시는 경우에만" in answer
        assert FORECAST_ACTION in answer
        denied = verified_sims_help_text(intent, allowed_actions=set())
        assert FORECAST_ACTION not in denied
        original_io_resolver = io_nlq.resolve_io_nlq
        try:
            io_nlq.resolve_io_nlq = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("budget must not reach IO")
            )
            assert not nlq_router.try_handle_nlq(
                question, {}, {}, lambda: "", lambda: 1,
                logging.getLogger("salesman-forecast-nlq-offline"),
            )
        finally:
            io_nlq.resolve_io_nlq = original_io_resolver

    # A standalone budget term is not a blanket alias for a forecast.
    assert nlq_router._resolve_analytics_action("매출예산") is None

    # Keep the existing IO detail route and its explicit salesperson filter.
    detail = "출고명세 영업사원명 김 조회"
    assert nlq_router.resolve_new_sims_nlq_candidate(detail) == {
        "route": "io", "action": "출고명세 조회",
    }
    assert io_nlq.resolve_io_nlq(detail)["params"]["sales_man_nm"] == "김"

    # This unregistered grouping must not become a forecast through the new aliases.
    assert nlq_router._resolve_analytics_action("영업사원별 매출 조회") is None
    print("PASS: budget guidance, forecast aliases and IO boundary (offline)")


if __name__ == "__main__":
    main()

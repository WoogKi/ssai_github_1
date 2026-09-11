"""Focused no-DB routing checks for real-user NLQ misrouting cases."""

from __future__ import annotations

import logging
import sys
import ast
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.sims.nlq import nlq_router  # noqa: E402


def _fail(message: str) -> None:
    raise AssertionError(message)


def _check_candidates() -> None:
    unsupported_cases = (
        "재고 회전율이 가장 낮은 제품군은 무엇이야?",
        "지난달 대비 매출이 가장 많이 상승한 품목 TOP 5 알려줘",
        "지난달 대비 매출 상승 TOP 5",
        "지난달 대비 매출이 가장 많이 오른 품목 5개",
        "전월 대비 매출 증가 품목 TOP 5",
    )
    forbidden_actions = {"제품코드 목록", "거래처 목록", "출고명세 조회"}
    for query in unsupported_cases:
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate.get("action") in forbidden_actions:
            _fail(f"unsupported analysis misrouted: {query} -> {candidate}")
        if candidate.get("action") != "analytics_metric_unsupported":
            _fail(f"unsupported analysis guard missing: {query} -> {candidate}")

    for query in (
        "신규 거래처에 보낼 제품 소개 이메일 초안을 작성해줘",
        "거래처에 보낼 제품 소개 이메일 작성해줘",
        "신규 고객에게 보낼 안내문 초안 만들어줘",
    ):
        if nlq_router.resolve_new_sims_nlq_candidate(query) is not None:
            _fail(f"writing request became SIMS candidate: {query}")
        if not nlq_router._is_general_explanation_request(query):
            _fail(f"writing request did not use general route: {query}")

    protected = {
        "신규 거래처 조회": "거래처 목록",
        "지난달 출고명세 조회": "출고명세 조회",
        "품목별 매출 추세 분석 조회": "품목별 매출 추세 분석",
        "품목별 매출 예상 조회": "품목별 매출 예상",
    }
    for query, expected_action in protected.items():
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate.get("action") != expected_action:
            _fail(f"protected query route changed: {query} -> {candidate}")


def _check_unsupported_delivery_without_service() -> None:
    pushed: list[dict] = []

    def push(payload, _action):
        pushed.append(payload)

    with patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=push), patch.object(
        nlq_router, "_get_analytics_handler", side_effect=AssertionError("analytics service must not load")
    ):
        handled = nlq_router._try_handle_analytics_nlq(
            "지난달 대비 매출이 가장 많이 상승한 품목 TOP 5 알려줘",
            room={"messages": []},
            session_state={},
            make_ts=lambda: "2026-09-10T00:00:00",
            next_seq=lambda: 1,
            logger=logging.getLogger("check_nlq_real_usage_misrouting"),
        )

    if not handled or len(pushed) != 1:
        _fail("unsupported analytics request did not finish through the no-DB guard")
    meta = pushed[0].get("meta") or {}
    if meta.get("result_status") != "unsupported" or meta.get("source_call_count") != 0:
        _fail(f"unsupported analytics result contract changed: {meta}")


def _check_writing_delivery_without_sims_handler() -> None:
    for query in (
        "신규 거래처에 보낼 제품 소개 이메일 초안을 작성해줘",
        "거래처에 보낼 제품 소개 이메일 작성해줘",
        "신규 고객에게 보낼 안내문 초안 만들어줘",
    ):
        handled = nlq_router.try_handle_nlq(
            query,
            room={"messages": []},
            session_state={},
            make_ts=lambda: "2026-09-10T00:00:00",
            next_seq=lambda: 1,
            logger=logging.getLogger("check_nlq_real_usage_misrouting"),
        )
        if handled:
            _fail(f"writing request unexpectedly used a SIMS handler: {query}")


def _check_writing_context_boundary() -> None:
    main_source = (PROJECT_ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_source)
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_has_explicit_sims_context_reference", "is_sims_related_question"}
    }
    if set(wanted) != {"_has_explicit_sims_context_reference", "is_sims_related_question"}:
        _fail("writing context boundary helpers are missing")
    namespace = {
        "re": __import__("re"),
        "is_general_writing_request": nlq_router.is_general_writing_request,
        "is_sims_result_followup_question": lambda _text: False,
    }
    exec(compile(ast.Module(body=[wanted["_has_explicit_sims_context_reference"], wanted["is_sims_related_question"]], type_ignores=[]), "context_boundary", "exec"), namespace)
    if namespace["is_sims_related_question"]("신규 거래처에 보낼 제품 소개 이메일 초안을 작성해줘"):
        _fail("standalone writing request attached stale SIMS context")
    if not namespace["is_sims_related_question"]("현재표를 참고해서 이메일 초안 작성"):
        _fail("explicit current-table writing request lost SIMS context")


def _check_manufacturer_grouping_suffix() -> None:
    from app.services.io_nlq import extract_params
    from app.ui.current_table_followups.action_dispatcher import classify_current_table_followup_intent

    for query in (
        "제조사별 매출 추세",
        "제약사별 매출 추세",
        "제조사별 집계",
        "제약사별 집계",
        "제조사 별 집계",
        "제약사 별 집계",
    ):
        params = extract_params(query)
        for field in ("maker_nm", "product_ven_nm"):
            if str(params.get(field) or "").strip().startswith("별"):
                _fail(f"manufacturer grouping suffix became a filter: {query} -> {params}")

    for query in ("제조사별 매출 추세", "제약사별 매출 추세"):
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate.get("action") != "제약사별 매출 추세 분석" or candidate.get("route") != "analytics":
            _fail(f"manufacturer grouping became an IO fallback: {query} -> {candidate}")

    for query in ("제조사별 집계", "제약사별 집계"):
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate != {"route": "analytics", "action": "analytics_metric_unsupported"}:
            _fail(f"grouping-only query must not become an IO fallback: {query} -> {candidate}")

    for query in ("제조사별 집계", "제약사별 집계"):
        if classify_current_table_followup_intent(f"현재표에서 {query}") != "dataframe_table":
            _fail(f"manufacturer grouping lost current-table semantics: {query}")

    manufacturer_params = nlq_router._build_analytics_params(
        "제조사 삼진 품목별 매출 추세",
        "품목별 매출 추세 분석",
    )
    if manufacturer_params.get("maker_nm") != "삼진" or manufacturer_params.get("product_ven_nm") != "삼진":
        _fail(f"explicit manufacturer filter changed: {manufacturer_params}")

    protected = {
        "매출 거래처 조회": "거래처 목록",
        "매입 거래처 조회": "거래처 목록",
        "거래처 조회": "거래처 목록",
        "신규 거래처 조회": "거래처 목록",
        "매출처별 매출 예상": "매출처별 매출 예상",
        "지난달 출고명세 조회": "출고명세 조회",
    }
    for query, expected_action in protected.items():
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate.get("action") != expected_action:
            _fail(f"manufacturer suffix guard changed protected route: {query} -> {candidate}")


def _check_sales_trend_axis_detail_summary_matrix() -> None:
    cases = {
        "품목별 추세분석": "품목별 매출 추세 분석",
        "품목별 추세분석 요약": "품목별 매출 추세 요약표",
        "제약사별 추세": "제약사별 매출 추세 분석",
        "제약사별 추세분석": "제약사별 매출 추세 분석",
        "제약사별 추세분석 요약": "제약사별 매출 추세 분석 요약표",
        "제약사별 추세 분석 요약": "제약사별 매출 추세 분석 요약표",
        "제조사별 추세": "제약사별 매출 추세 분석",
        "제조사별 추세분석 요약": "제약사별 매출 추세 분석 요약표",
        "제조사 삼진 품목별 매출 추세": "품목별 매출 추세 분석",
        "제약사별 매출 추세 분석 요약표": "제약사별 매출 추세 분석 요약표",
    }
    for query, expected_action in cases.items():
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query) or {}
        if candidate != {"route": "analytics", "action": expected_action}:
            _fail(f"sales-trend axis/detail matrix mismatch: {query} -> {candidate}")

        params = nlq_router._build_analytics_params(query, expected_action)
        if query.startswith(("제약사별", "제조사별")):
            for field in ("maker_nm", "product_ven_nm"):
                if str(params.get(field) or "").strip().startswith("별"):
                    _fail(f"sales-trend grouping suffix became a filter: {query} -> {params}")

    product_params = nlq_router._build_analytics_params(
        "제조사 삼진 품목별 매출 추세",
        "품목별 매출 추세 분석",
    )
    if product_params.get("maker_nm") != "삼진" or product_params.get("product_ven_nm") != "삼진":
        _fail(f"product-axis manufacturer filter was lost: {product_params}")

    handlers = {
        "품목별 매출 추세 분석": "get_sales_trend_result",
        "품목별 매출 추세 요약표": "get_sales_trend_summary_result",
        "제약사별 매출 추세 분석": "get_manufacturer_sales_trend_result",
        "제약사별 매출 추세 분석 요약표": "get_manufacturer_sales_trend_summary_result",
    }
    for action, expected_name in handlers.items():
        handler = nlq_router._get_analytics_handler(action)
        if getattr(handler, "__name__", "") != expected_name:
            _fail(f"sales-trend canonical handler/grain contract changed: {action} -> {handler}")


def main() -> int:
    _check_candidates()
    _check_unsupported_delivery_without_service()
    _check_writing_delivery_without_sims_handler()
    _check_writing_context_boundary()
    _check_manufacturer_grouping_suffix()
    _check_sales_trend_axis_detail_summary_matrix()
    print("RESULT: PASS - real usage misrouting guard contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

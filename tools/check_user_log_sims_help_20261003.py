"""Offline regression for user-log routing, help, period, status, and page UX."""

from __future__ import annotations

import ast
from datetime import date, datetime
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.datetime_tool import operating_llm_context
from app.services.io_nlq import has_unconsumed_detail_condition, resolve_io_nlq
from app.services.nlq_case_log_service import _result_status
from app.services.query_result_status import query_result_status
from app.sims.nlq.action_inventory import implemented_actions
from app.sims.nlq.nlq_router import _try_handle_io_nlq
from app.services.knowledge_document_service import ContextCitation, ContextPacket
from app.ui.knowledge_chat_adapter import (
    build_sims_help_choice_prompt,
    parse_incomplete_sims_help_request,
    parse_sims_help_choice,
    verified_sims_help_examples,
    verified_sims_help_text,
)


def main() -> None:
    today = date(2026, 10, 3)
    unresolved = resolve_io_nlq("매출분석 오가논", today=today)
    assert unresolved["action"] == "출고명세 조회"
    assert has_unconsumed_detail_condition("매출분석 오가논", unresolved["action"], unresolved["params"])
    pushed = []
    with (patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: pushed.append(payload)),
          patch("app.services.rddbc120_service.get_rddbc120_result") as outbound_source,
          patch("app.services.io_nlq.resolve_unlabeled_io_entity_condition") as entity_source):
        handled = _try_handle_io_nlq(
            "매출분석 오가논", room={}, session_state={}, make_ts=lambda: "",
            next_seq=lambda: 1, logger=logging.getLogger(__name__),
        )
    outbound_source.assert_not_called()
    entity_source.assert_not_called()
    assert handled and len(pushed) == 1
    assert pushed[0]["meta"]["service_call_skipped"] and pushed[0]["meta"]["source_call_count"] == 0
    assert pushed[0]["meta"]["result_status"] == "input_required"
    for question in ("출고명세 오가논 조회", "오늘 출고명세 조회", "출고명세 조회 전체"):
        parsed = resolve_io_nlq(question, today=today)
        assert not has_unconsumed_detail_condition(question, parsed["action"], parsed["params"]), question
    mixed = "거래처 한올바이오파마 매출분석 입고명세 조회"
    mixed_parsed = resolve_io_nlq(mixed, today=today)
    assert has_unconsumed_detail_condition(mixed, mixed_parsed["action"], mixed_parsed["params"])
    same_action = "거래처 한올바이오파마 매출분석 출고명세 조회"
    same_parsed = resolve_io_nlq(same_action, today=today)
    assert has_unconsumed_detail_condition(same_action, same_parsed["action"], same_parsed["params"])
    with (patch("app.ui.chat_middleware.push_sims_result_to_chat"),
          patch("app.services.rddbc110_service.get_rddbc110_result") as inbound_source,
          patch("app.services.io_nlq.resolve_unlabeled_io_entity_condition") as entity_source):
        assert _try_handle_io_nlq(mixed, room={}, session_state={}, make_ts=lambda: "",
                                  next_seq=lambda: 1, logger=logging.getLogger(__name__))
    inbound_source.assert_not_called()
    entity_source.assert_not_called()

    # Exercise the same preprocessor, parser, guard and router service boundary.
    # A parsed code/location must reach its source; an extra unbound subject must not.
    detail_cases = (
        ("제품코드 63431 매입조회", "app.services.rddbc110_service.get_rddbc110_result", "63431"),
        ("제품코드 63431 매출조회", "app.services.rddbc120_service.get_rddbc120_result", "63431"),
        ("제품코드 37935 매입현황 2026", "app.services.rddbc110_service.get_rddbc110_result", "37935"),
        ("재고위치 00001 제품코드 63431 매입조회", "app.services.rddbc110_service.get_rddbc110_result", "63431"),
        ("재고위치 본사창고 제품코드 63431 매입조회", "app.services.rddbc110_service.get_rddbc110_result", "63431"),
    )
    for question, source_path, product_code in detail_cases:
        source_result = {"final": True, "type": "text", "data": "조회 결과 없음", "message": "조회 결과 없음",
                         "meta": {"result_status": "no_data", "source_call_count": 1, "row_count": 0}}
        with (patch("app.ui.chat_middleware.push_sims_result_to_chat"),
              patch(source_path, return_value=source_result) as source):
            assert _try_handle_io_nlq(question, room={}, session_state={}, make_ts=lambda: "",
                                      next_seq=lambda: 1, logger=logging.getLogger(__name__))
        source.assert_called_once()
        called_params = source.call_args.kwargs.get("params") or source.call_args.args[0]
        assert called_params["physic_cd"] == product_code, (question, called_params)
        if "재고위치 00001" in question:
            assert called_params["stock_cd"] == "00001"
            assert not called_params.get("stock_nm")
        if "본사창고" in question:
            assert called_params["stock_nm"] == "본사창고"
        if "2026" in question:
            assert (called_params["date_from"], called_params["date_to"]) == ("20260101", "20261231")
    for question in ("제품코드 63431 오가논 매입조회", "제품코드 63431 단가적용처 50002 매입조회",
                     "재고위치 00001,00002 제품코드 63431 매입조회"):
        with (patch("app.ui.chat_middleware.push_sims_result_to_chat"),
              patch("app.services.rddbc110_service.get_rddbc110_result") as source):
            assert _try_handle_io_nlq(question, room={}, session_state={}, make_ts=lambda: "",
                                      next_seq=lambda: 1, logger=logging.getLogger(__name__))
        source.assert_not_called()
    with (patch("app.ui.chat_middleware.push_sims_result_to_chat"),
          patch("app.services.order_calculation_service.get_order_calculation_result",
                return_value={"final": True, "type": "text", "data": "offline", "message": "offline",
                              "meta": {"result_status": "no_data", "source_call_count": 0}}) as order_source):
        assert _try_handle_io_nlq("재고위치 00001 단가적용처 50002 재고적용처 50001 발주계산",
                                  room={}, session_state={}, make_ts=lambda: "", next_seq=lambda: 1,
                                  logger=logging.getLogger(__name__))
    order_source.assert_called_once()
    order_params = order_source.call_args.kwargs["params"]
    assert (order_params["stock_cd"], order_params["cost_apply_cd"], order_params["stock_apply_cd"]) == (
        "00001", "50002", "50001",
    )

    for question in (
        "거래처 한올바이오파마 이달 입고명세 조회",
        "이달 입고명세 거래처 한올바이오파마 조회",
        "거래처 한올바이오파마 이번달 입고명세 조회",
        "거래처 한올바이오파마 이번 달 입고명세 조회",
        "거래처 한올바이오파마, 이달 입고명세 조회",
        "이달, 거래처 한올바이오파마 입고명세 조회",
    ):
        parsed = resolve_io_nlq(question, today=today)
        params = parsed["params"]
        assert params["ven_nm"] == "한올바이오파마", params
        assert (params["date_from"], params["date_to"]) == ("20261001", "20261031"), params
    explicit = resolve_io_nlq("거래처 한올바이오파마 이달 20260901 입고명세 조회", today=today)["params"]
    assert explicit["date_from"] == "20260901"
    previous = resolve_io_nlq("거래처 한올바이오파마 지난 달 입고명세 조회", today=today)["params"]
    assert previous["ven_nm"] == "한올바이오파마"
    assert (previous["date_from"], previous["date_to"]) == ("20260901", "20260930")

    assert _result_status({"result_status": "query_error"}, total_rows=0, candidate_count=0)[1] == "query_error"
    assert query_result_status({"result_status": "query_error"}) == "query_error"
    assert _result_status({"result_status": "no_data"}, total_rows=0, candidate_count=0)[1] == "no_data"
    assert _result_status({}, total_rows=0, candidate_count=0)[1] == "no_data"

    cases = (
        "sims 발주담당자 윤정아", "매입처 비아다빈치 상세",
        "한올제약 월별재고현황", "63431",
    )
    actions = {spec.canonical_action for spec in implemented_actions()}
    for question in cases:
        intent = parse_incomplete_sims_help_request(question, today=today)
        assert intent is not None, question
        answer = verified_sims_help_text(intent, allowed_actions=actions, today=today)
        assert answer and "SQL" not in answer and question in answer
        limited = verified_sims_help_text(intent, allowed_actions=set(), today=today)
        assert "예시:" not in limited
        assert "SIMS 질문 예시" not in limited
    assert parse_incomplete_sims_help_request("sims 거래처 조회") is None
    maker = parse_incomplete_sims_help_request("sims 제약사 오가논 이달", today=today)
    maker_help = verified_sims_help_text(maker, allowed_actions=actions, today=today)
    assert maker.subject_value == "오가논" and maker.period == "202610"
    assert "발주담당자 홍길동" not in maker_help and "제약사 오가논" in maker_help
    assert "기간이 적용되지 않습니다" in maker_help
    for clock_day, month in ((date(2026, 10, 3), "202610"), (date(2027, 2, 4), "202702")):
        period_intent = parse_incomplete_sims_help_request("매입처 비아다빈치 이달 상세", today=clock_day)
        period_example = verified_sims_help_examples(period_intent, allowed_actions=actions, today=clock_day)[0][1]
        assert month in period_example
        if month != "202610":
            assert "202610" not in period_example
    for question, expected in (
        ("매입처 비아다빈치 202609 상세", ("month_from", "202609", "month_to", "202609")),
        ("매입처 비아다빈치 20260901~20260930 상세", ("date_from", "20260901", "date_to", "20260930")),
    ):
        period_intent = parse_incomplete_sims_help_request(question, today=today)
        assert period_intent.subject_value == "비아다빈치"
        examples_for_period = verified_sims_help_examples(period_intent, allowed_actions=actions, today=today)
        assert len(examples_for_period) == 1
        parsed_example = resolve_io_nlq(examples_for_period[0][1], today=today)
        assert (parsed_example["params"][expected[0]], parsed_example["params"][expected[2]]) == (expected[1], expected[3])
    order_period = parse_incomplete_sims_help_request("sims 발주담당자 윤정아 202609", today=today)
    order_examples = verified_sims_help_examples(order_period, allowed_actions=actions, today=today)
    assert len(order_examples) == 1 and order_examples[0][0] == "발주조회"
    assert resolve_io_nlq(order_examples[0][1], today=today)["params"]["month_from"] == "202609"
    assert "발주계산" not in order_examples[0][1]
    stock_help = verified_sims_help_text(parse_incomplete_sims_help_request("한올제약 월별재고현황"),
                                         allowed_actions=actions)
    assert "한올제약 (대상 종류 미확정)" in stock_help
    code_help = verified_sims_help_text(parse_incomplete_sims_help_request("63431"), allowed_actions=actions)
    assert "63431" in code_help and "제품코드 63431" not in code_help
    assert parse_incomplete_sims_help_request("37935 매입현황 2026") is None
    numeric_action = resolve_io_nlq("37935 매입현황 2026", today=today)
    assert numeric_action["action"] == "입고명세 조회"
    assert numeric_action["params"]["date_from"] == "20260101"
    for question in ("오늘 입고명세 조회", "발주담당자 윤정아 발주계산", "일반 날씨 질문"):
        assert parse_incomplete_sims_help_request(question) is None, question
    for question in cases:
        intent = parse_incomplete_sims_help_request(question)
        for action, example in verified_sims_help_examples(intent, allowed_actions=actions):
            parsed = resolve_io_nlq(example, today=today)
            assert parsed is not None and parsed["action"] == action, example
    assert not verified_sims_help_examples(maker, allowed_actions=set())

    packet = ContextPacket("승인된 업무 도움말", (ContextCitation("d", "help.md", 1, "s", "도움말"),), "ready", 1)
    examples = verified_sims_help_examples(maker, allowed_actions=actions)
    assert "오가논" in build_sims_help_choice_prompt(intent=maker, examples=examples, packet=packet)[1]["content"]
    assert parse_sims_help_choice('{"action": "제품정보 조회"}', examples=examples) == "제품정보 조회"
    assert not parse_sims_help_choice('{"action": "발주 계산"}', examples=examples)
    assert not parse_sims_help_choice('제품정보 조회를 실행하세요', examples=examples)

    helper_node = next(node for node in ast.parse((ROOT / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")).body
                       if isinstance(node, ast.FunctionDef) and node.name == "_select_approved_sims_help_action")
    model = Mock(return_value='{"action": "제품정보 조회"}')
    repository = Mock()
    repository.retrieve_for_chat.return_value = packet
    namespace = {
        "_knowledge_request_context_for_room": lambda _room, technical_detail_mode: "scoped-context",
        "_knowledge_repository_for_chat": lambda: repository,
        "build_sims_help_choice_prompt": build_sims_help_choice_prompt,
        "parse_sims_help_choice": parse_sims_help_choice,
        "call_chat_protected": model,
        "extract_chat_completion_text": lambda response: {"content": response},
        "EXPECTED_LM_MODEL": "offline-model", "st": SimpleNamespace(session_state={}),
        "log": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[helper_node], type_ignores=[])),
                 "<sims-help-choice>", "exec"), namespace)
    select = namespace["_select_approved_sims_help_action"]
    assert select(maker, examples=examples, room={}) == ("제품정보 조회", 1)
    repository.retrieve_for_chat.assert_called_with(query="업무질문 도움말", request_context="scoped-context")
    model.return_value = '{"action": "unsupported"}'
    assert select(maker, examples=examples, room={}) == ("", 1)
    repository.retrieve_for_chat.return_value = ContextPacket("", (), "no_match", 0)
    model.reset_mock()
    assert select(maker, examples=examples, room={}) == ("", 0)
    model.assert_not_called()
    repository.retrieve_for_chat.return_value = packet
    model.side_effect = RuntimeError("offline model failure")
    assert select(maker, examples=examples, room={}) == ("", 1)
    assert verified_sims_help_text(maker, allowed_actions=actions)

    clock = operating_llm_context(now=datetime(2026, 10, 2, 20, tzinfo=ZoneInfo("Asia/Seoul")))
    assert "오늘 2026-10-02" in clock and "내일 2026-10-03" in clock
    assert "실시간 날씨" in clock

    main_source = (ROOT / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    page_source = (ROOT / "app/ui/order_calculation_editor.py").read_text(encoding="utf-8")
    assert 'base_system + "\\n" + operating_llm_context()' in main_source
    assert "format_func=lambda number: f\"{number} / {page_count}\"" in page_source
    assert "width=120" in page_source
    assert 'st.caption(f"/ {page_count}")' not in page_source
    print("user log SIMS help focused PASS")


if __name__ == "__main__":
    main()

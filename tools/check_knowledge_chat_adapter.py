from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import ContextPacket
from app.ui.knowledge_chat_adapter import (
    build_knowledge_followup_queue_request,
    build_knowledge_prompt,
    parse_business_help_knowledge_request,
    parse_knowledge_followup_queue_request,
    parse_explicit_knowledge_request,
)


def main() -> None:
    document = parse_explicit_knowledge_request("/knowledge 반품 규정")
    technical = parse_explicit_knowledge_request("/knowledge-tech storage helper")
    assert document and document.query == "반품 규정" and not document.technical_detail_mode
    assert technical and technical.query == "storage helper" and technical.technical_detail_mode
    for value in ("/knowledge", "/knowledge-tech", "/knowledgeful 질문", "일반 질문", None):
        assert parse_explicit_knowledge_request(value) is None

    business_help_cases = (
        "SSAI에서 어떤 질문을 할 수 있어?",
        "SIMS AI에서 어떤 질문을 할 수 있어?",
        "어떤 업무를 조회할 수 있어?",
        "계약단가는 어떻게 물어보면 돼?",
        "계약단가 조회 예시 보여줘",
        "최종 매입단가는 어떻게 조회해?",
        "발주 조회는 어떻게 질문해?",
        "입고예정은 어떻게 물어봐?",
        "단가적용처로 조회할 수 있어?",
        "재고적용처로 조회하는 방법 알려줘",
        "전문약만 조회하려면 어떻게 해?",
        "SSAI 관련 프롬프트 알려줘",
        "SIMS 관련 프롬프트 알려줘",
        "SSAI에서 뭘 물어볼 수 있어?",
        "SIMS에서 뭘 물어볼 수 있어?",
        "질문 예시 알려줘",
        "SSAI 사용법 알려줘",
        "SIMS 사용법 알려줘",
    )
    for value in business_help_cases:
        route = parse_business_help_knowledge_request(value)
        assert route and route.query == value and not route.technical_detail_mode
        assert route.retrieval_query == "업무질문 도움말"

    erp_query_cases = (
        "계약단가 조회",
        "단가적용처 50002 계약단가 조회",
        "전문약 최종 매입단가 조회",
        "종근당 전문약 발주 조회",
        "최근 발주내역 조회",
        "종근당 입고예정 조회",
        "제품코드 목록",
        "거래처 목록",
    )
    for value in erp_query_cases:
        assert parse_business_help_knowledge_request(value) is None
    for value in (None, "일반 대화", "/knowledge 계약단가 사용법"):
        assert parse_business_help_knowledge_request(value) is None

    queued = build_knowledge_followup_queue_request(
        query="그 근거의 예외는?",
        parent_message_id="knowledge-1",
        room_id="room-4",
    )
    followup = parse_knowledge_followup_queue_request(
        queued,
        current_room_id="room-4",
        last_user_text="그 근거의 예외는?",
    )
    assert followup and followup.parent_message_id == "knowledge-1"
    assert parse_knowledge_followup_queue_request(
        queued, current_room_id="room-6", last_user_text="그 근거의 예외는?"
    ) is None
    assert parse_knowledge_followup_queue_request(
        queued, current_room_id="room-4", last_user_text="다른 질문"
    ) is None
    assert parse_knowledge_followup_queue_request(
        {**queued, "extra": "unsafe"},
        current_room_id="room-4",
        last_user_text="그 근거의 예외는?",
    ) is None

    raw_cases = (
        ("/knowledge K-SMOKE-GENERAL-20260822", "K-SMOKE-GENERAL-20260822", False),
        ("/knowledge-tech 사용자 파일 경로를 안전하게 만드는 함수", "사용자 파일 경로를 안전하게 만드는 함수", True),
        ("/knowledge-tech K-SMOKE-ERP-20260822", "K-SMOKE-ERP-20260822", True),
    )
    for raw, expected_query, expected_technical in raw_cases:
        route = parse_explicit_knowledge_request(raw)
        assert route and route.query == expected_query and route.technical_detail_mode is expected_technical
    from app.sims.nlq.nlq_router import keyboard_fix
    assert keyboard_fix(raw_cases[0][0]) != raw_cases[0][0]
    assert keyboard_fix(raw_cases[2][0]) != raw_cases[2][0]
    assert keyboard_fix("dkssudgktpdy") != "dkssudgktpdy"

    packet = ContextPacket(
        text="[official.md v1 §정책]\n반품은 승인 후 처리합니다.",
        citations=(),
        reason_code="ready",
        candidate_count=1,
    )
    try:
        build_knowledge_prompt(query="반품", packet=packet)
    except ValueError:
        pass
    else:
        raise AssertionError("citation-free packet was accepted")

    from app.services.knowledge_document_service import ContextCitation

    packet = ContextPacket(
        text="[official.md v1 §정책]\n반품은 승인 후 처리합니다.",
        citations=(ContextCitation("d", "official.md", 1, "s", "정책"),),
        reason_code="ready",
        candidate_count=1,
    )
    prompt = build_knowledge_prompt(query="반품 규정", packet=packet)
    assert len(prompt) == 2 and "반품 규정" in prompt[1]["content"] and packet.text in prompt[1]["content"]

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    module = ast.parse(main_source)
    partition_allow_keys = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_CHAT_PARTITION_MESSAGE_ALLOW_KEYS" for target in node.targets)
    )
    assert "knowledge_evidence" in partition_allow_keys
    json.dumps({"knowledge_evidence": {"citations": [{"document_id": "d"}]}}, ensure_ascii=False)
    assert "parse_explicit_knowledge_request(last_user_text)" in main_source
    assert "raw_knowledge_route = parse_explicit_knowledge_request(user_input)" in main_source
    assert "parse_business_help_knowledge_request(user_input)" in main_source
    assert "parse_business_help_knowledge_request(last_user_text)" in main_source
    assert main_source.index("raw_knowledge_route = parse_explicit_knowledge_request(user_input)") < main_source.index("fixed = keyboard_fix(user_input)")
    assert "explicit_command_queued" in main_source
    assert main_source.index("explicit_command_queued") < main_source.index("current_table_followup_input = _normalize_current_table_followup_input(user_input)")
    assert main_source.index("explicit_command_queued") < main_source.index("resolve_new_sims_nlq_candidate(user_input)")
    assert "_run_explicit_knowledge_chat(knowledge_route, room=current_room)" in main_source
    assert "_run_business_help_knowledge_chat(" in main_source
    assert main_source.index("if business_help_knowledge_route is not None:") < main_source.index("resolve_new_sims_nlq_candidate(user_input)")
    assert "fall_through_on_no_match=True" in main_source
    assert "result=fall_through" in main_source
    assert 'getattr(route, "retrieval_query", "") or route.query' in main_source
    assert "citation_display_limit=3 if fall_through_on_no_match else None" in main_source
    assert 'parent_meta.get("knowledge_citation_display_limit")' in main_source
    assert "_run_knowledge_followup_chat(knowledge_followup_route, room=current_room)" in main_source
    assert "raw_knowledge_followup is not None" in main_source
    assert main_source.index("if raw_knowledge_followup is not None:") < main_source.index("elif knowledge_route is not None:")
    assert "build_knowledge_answer_message(" in main_source
    assert "return _render_knowledge_answer_message(" in main_source
    assert "allow_followup=(" in main_source
    assert "generic raw-message search surface" in main_source
    assert "room compaction" in main_source
    service_source = (ROOT / "app" / "services" / "knowledge_document_service.py").read_text(encoding="utf-8")
    assert "Never read a stale" in service_source and "_project_source_is_current(source)" in service_source
    print("RESULT OK tests=62")


if __name__ == "__main__":
    main()

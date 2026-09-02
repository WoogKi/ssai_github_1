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
    assert main_source.index("raw_knowledge_route = parse_explicit_knowledge_request(user_input)") < main_source.index("fixed = keyboard_fix(user_input)")
    assert "explicit_command_queued" in main_source
    assert main_source.index("explicit_command_queued") < main_source.index("current_table_followup_input = _normalize_current_table_followup_input(user_input)")
    assert main_source.index("explicit_command_queued") < main_source.index("resolve_new_sims_nlq_candidate(user_input)")
    assert "_run_explicit_knowledge_chat(knowledge_route, room=current_room)" in main_source
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
    print("RESULT OK tests=34")


if __name__ == "__main__":
    main()

"""Focused offline gate for new official NLQ cases and the generated user guide."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import sys
import tempfile

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq  # noqa: E402
from app.services.knowledge_document_service import (  # noqa: E402
    KnowledgeDocumentRepository,
    build_knowledge_chat_request_context,
)
from app.ui.knowledge_chat_adapter import parse_business_help_knowledge_request  # noqa: E402
from app.ui.knowledge_chat_evidence import (  # noqa: E402
    build_knowledge_answer_display,
    build_knowledge_answer_message,
    build_knowledge_followup_packet,
)
from tools.build_nlq_user_rag_guide import DEFAULT_CASEBOOK, DEFAULT_OUTPUT  # noqa: E402
from tools.knowledge_document_manage_cli import apply_plan, validate_plan  # noqa: E402


CASES = (
    ("일반의약품 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "non_insurance"}, "PASS"),
    ("전문약 제약사 삼진 계약단가 조회", "최종 계약단가 조회", {"product_di_semantic_group": "insurance", "maker_nm": "삼진"}, "PASS"),
    ("제품 아라바정 계약단가 조회", "최종 계약단가 조회", {"physic_nm": "아라바정"}, "PASS"),
    ("단가적용처 50002 계약단가 조회", "최종 계약단가 조회", {"ven_cd": "50002"}, "PASS"),
    ("전문약 최종 매입단가 조회", "최종 매입단가 조회", {"product_di_semantic_group": "insurance"}, "PASS"),
    ("OTC 최종 매입가 조회", "최종 매입단가 조회", {"product_di_semantic_group": "non_insurance"}, "PASS"),
    ("제품 아라바정 최종 매입단가 조회", "최종 매입단가 조회", {"physic_nm": "아라바정"}, "PASS"),
    ("재고적용처 50001 최종 매입단가 조회", "최종 매입단가 조회", {"stock_apply_cd": "50001"}, "PASS"),
    ("어제 발주 조회", "발주조회", {"date_from": "20260907", "date_to": "20260907"}, "PASS"),
    ("최근 발주내역 조회", "발주조회", {}, "PASS"),
    ("아라바정 제품 발주 조회", "발주조회", {"physic_nm": "아라바정"}, "PASS"),
    ("입고중 발주조회", "발주조회", {"status_code": "2"}, "PASS"),
    ("발주상태 입고중 조회", "발주조회", {"status_code": "2"}, "PASS"),
    ("종근당 전문약 발주 조회", "발주조회", {"order_vendor_nm": "종근당", "product_di_semantic_group": "insurance"}, "PASS"),
    ("9월 종근당 전문약 발주 조회", "발주조회", {"date_from": "20260901", "date_to": "20260930", "order_vendor_nm": "종근당", "product_di_semantic_group": "insurance"}, "PASS"),
    ("단가적용처 50002 발주 조회", "발주조회", {"cost_apply_cd": "50002"}, "PASS"),
    ("재고적용처 50001 발주 조회", "발주조회", {"stock_apply_cd": "50001"}, "PASS"),
    ("입고 예정 조회", "입고예정조회", {}, "PASS"),
    ("전문약 입고 예정 조회", "입고예정조회", {"product_di_semantic_group": "insurance"}, "PASS"),
    ("종근당 입고예정 조회", "입고예정조회", {"order_vendor_nm": "종근당"}, "PASS"),
    ("종근당 전문약 입고예정 조회", "입고예정조회", {"order_vendor_nm": "종근당", "product_di_semantic_group": "insurance"}, "PASS"),
)

EXPECTED_SECTION_COUNTS = {
    "계약단가": 4,
    "최종 매입단가": 4,
    "발주 조회": 9,
    "입고예정 조회": 4,
}

HELP_QUERIES = (
    "계약단가는 어떻게 물어보면 돼?",
    "계약단가 조회 예시 보여줘",
    "최종 매입단가는 어떻게 조회해?",
    "발주 조회 질문 예시 보여줘",
    "입고예정 조회 방법 알려줘",
    "입고예정은 어떻게 물어봐?",
    "단가적용처로 조회할 수 있어?",
    "재고적용처로 조회하는 방법 알려줘",
    "전문약만 조회하려면 어떻게 해?",
    "SSAI에서 어떤 질문을 할 수 있어?",
    "SIMS AI에서 어떤 질문을 할 수 있어?",
    "SSAI에서 뭘 물어볼 수 있어?",
    "SIMS에서 뭘 물어볼 수 있어?",
    "질문 예시 알려줘",
    "SSAI 사용법 알려줘",
    "SIMS 사용법 알려줘",
    "어떤 업무를 조회할 수 있어?",
)


def main() -> None:
    frame = pd.read_excel(DEFAULT_CASEBOOK, sheet_name="NLQ 사례", dtype=str).fillna("")
    assert len(frame) == 206, len(frame)
    focused = frame[frame["질문"].isin(question for question, *_ in CASES)]
    assert len(focused) == len(CASES)

    observed = {"PASS": 0, "FAIL": 0, "REVIEW": 0}
    for question, action, expected_params, expected_status in CASES:
        rows = focused[focused["질문"] == question]
        assert len(rows) == 1
        row = rows.iloc[0]
        assert row["기준 기능 (action)"] == action
        assert row["의도일치"] == expected_status
        result = resolve_registered_erp_table_nlq(question, today=date(2026, 9, 8))
        assert result and result["action"] == action
        params = result["params"]
        matches = all(params.get(key) == value for key, value in expected_params.items())
        if expected_status == "PASS":
            assert matches, (question, expected_params, params)
        else:
            assert not matches, (question, expected_params, params)
        observed[expected_status] += 1

    content = DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert content.startswith("# SSAI 업무질문 사용 예시")
    assert "SIMS AI에서는" not in content and "SIMS AI에서 어떤 질문" not in content
    for heading, expected_count in EXPECTED_SECTION_COUNTS.items():
        assert f"## {heading}" in content
        section = content.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
        assert "### 사용할 수 있는 주요 조건" in section
        assert "### 여러 조건으로 조회하기" in section
        assert "### 질문 예시" in section
        assert "### 비슷한 조회와의 차이" in section
        assert section.count("- `") == expected_count
    for question, _action, _params, status in CASES:
        assert (question in content) is (status == "PASS")
    forbidden = re.compile(
        r"\bNLQ\b|\baction\b|canonical|parser|registry|source_call_count|"
        r"R(?:ddbc)?(?:070|230|170|180)|Rd\d+_|\bDB\b|\bSQL\b",
        re.IGNORECASE,
    )
    assert not forbidden.search(content)

    plan = validate_plan(DEFAULT_OUTPUT.with_suffix(".knowledge.json"))
    assert len(plan) == 1 and plan[0].scope == "GLOBAL" and plan[0].knowledge_classification == "GENERAL"
    assert plan[0].source_key == "document:sims-ai-business-question-examples"
    assert plan[0].source_name == "SSAI_업무질문_사용_예시.md" and plan[0].version == 2
    with tempfile.TemporaryDirectory(prefix="nlq-user-rag-") as temp:
        manifest_root = Path(temp) / "manifest"
        applied = apply_plan(
            plan,
            manifest_root=manifest_root,
            actor_user_id=1,
            selected_company_id=4,
            permission_resolver=lambda **_: ("KNOWLEDGE_GLOBAL_MANAGE",),
        )
        assert applied[0]["status"] == "ACTIVE"
        repository = KnowledgeDocumentRepository(root=manifest_root)
        request_context = build_knowledge_chat_request_context(
            user_id=1,
            company_id=4,
            permission_codes=("RAG_USE",),
            room_owner_user_id=1,
            room_company_id=4,
            technical_detail_mode=False,
        )
        for query in HELP_QUERIES:
            route = parse_business_help_knowledge_request(query)
            assert route and route.query == query and not route.technical_detail_mode
            packet = repository.retrieve(
                query=route.retrieval_query,
                current_user_id=1,
                current_company_id=4,
                permission_codes=("RAG_USE",),
            )
            assert packet.reason_code == "ready" and packet.citations, (
                query,
                packet.reason_code,
                packet.candidate_count,
            )

        for query in (
            "SSAI에서 어떤 질문을 할 수 있어?",
            "SIMS AI에서 어떤 질문을 할 수 있어?",
            "계약단가는 어떻게 물어보면 돼?",
        ):
            route = parse_business_help_knowledge_request(query)
            assert route is not None
            packet = repository.retrieve_for_chat(
                query=route.retrieval_query,
                request_context=request_context,
            )
            assert len(packet.citations) == 21
            message = build_knowledge_answer_message(
                repository=repository,
                answer="승인된 업무질문 도움말 답변",
                packet=packet,
                request_context=request_context,
                message_id="business-help",
                timestamp="2026-09-08T12:00:00+09:00",
                citation_display_limit=3,
            )
            display = build_knowledge_answer_display(
                repository=repository,
                message=message,
                request_context=request_context,
            )
            assert display.visible and len(display.citation_labels) == 1
            assert len(display.citations) == len(packet.citations)
            assert len(message["knowledge_evidence"]["citations"]) == len(packet.citations)
            for followup_query in (
                "예를 들어줘",
                "다른 예도 보여줘",
                "그럼 발주는?",
                "계약단가는?",
            ):
                followup = build_knowledge_followup_packet(
                    repository=repository,
                    parent_message=message,
                    query=followup_query,
                    request_context=request_context,
                )
                assert followup.reason_code == "ready" and followup.citations, (
                    query,
                    followup_query,
                    followup.reason_code,
                )

    print(json.dumps({
        "gate": "PASS",
        "official_total": len(frame),
        "new_cases": len(CASES),
        "focused": observed,
        "section_examples": EXPECTED_SECTION_COUNTS,
        "help_queries": len(HELP_QUERIES),
        "business_help_followups": 12,
        "citation_display_max": 3,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

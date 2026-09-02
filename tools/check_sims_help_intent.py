"""Focused regression for deterministic, permission-filtered SIMS help."""

from __future__ import annotations

import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.sims_help_adapter import (  # noqa: E402
    build_sims_help_text,
    parse_sims_help_request,
)
from app.services.knowledge_document_service import (  # noqa: E402
    APPROVAL_APPROVED,
    DOCUMENT_ACTIVE,
    SOURCE_KIND_PROJECT_SOURCE,
    DocumentSource,
    KnowledgeDocumentRepository,
)
from app.services.knowledge_scope_policy import KNOWLEDGE_ERP_DB_READ  # noqa: E402


class _MetadataOnlyRepository(KnowledgeDocumentRepository):
    def _read_artifact(self, content_hash):  # pragma: no cover - failure guard
        raise AssertionError(f"help availability must not read artifact {content_hash}")


def _approved(repo: KnowledgeDocumentRepository, **kwargs):
    source, created = repo._register_text_trusted(**kwargs)
    assert created
    return repo._approve_trusted(document_id=source.document_id)


def _assert_knowledge_help_availability() -> None:
    root = Path(tempfile.mkdtemp(prefix="sims-help-knowledge-"))
    repo = _MetadataOnlyRepository(root=root)
    try:
        _approved(
            repo,
            source_name="general.md",
            source_key="general-help",
            content="재고 관리 기준",
            scope="GLOBAL",
            search_aliases=("재고 관리 기준",),
        )
        _approved(
            repo,
            source_name="Rddbc110.txt",
            source_key="erp-rddbc110-help",
            content="Rddbc110 기술 문서",
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
            search_aliases=("Rddbc110",),
        )

        wholesale_without_general = build_sims_help_text(
            ("RAG_USE", "IO_READ"),
            knowledge_availability={"general": "", "erp_technical": "", "project_source_technical": ""},
        )
        assert "/knowledge" not in wholesale_without_general and "오늘 입고현황" in wholesale_without_general

        wholesale_available = repo.get_help_example_queries(
            current_user_id=11,
            current_company_id=4,
            permission_codes=("RAG_USE",),
        )
        assert wholesale_available == {
            "general": "재고 관리 기준",
            "erp_technical": "",
            "project_source_technical": "",
        }
        wholesale_help = build_sims_help_text(("RAG_USE",), knowledge_availability=wholesale_available)
        assert "/knowledge 재고 관리 기준" in wholesale_help and "/knowledge-tech" not in wholesale_help

        manager_available = repo.get_help_example_queries(
            current_user_id=12,
            current_company_id=4,
            permission_codes=("RAG_USE", KNOWLEDGE_ERP_DB_READ),
        )
        assert manager_available["erp_technical"] == "Rddbc110"
        manager_help = build_sims_help_text(
            ("RAG_USE", KNOWLEDGE_ERP_DB_READ),
            knowledge_availability=manager_available,
        )
        assert "/knowledge-tech Rddbc110" in manager_help

        readonly_available = repo.get_help_example_queries(
            current_user_id=13,
            current_company_id=4,
            permission_codes=(),
        )
        assert readonly_available == {
            "general": "",
            "erp_technical": "",
            "project_source_technical": "",
        }
        readonly_help = build_sims_help_text((), knowledge_availability=readonly_available)
        assert "Knowledge 자료" not in readonly_help and "/knowledge" not in readonly_help

        stale_project = DocumentSource(
            document_id="stale-project",
            source_name="stale.py",
            source_key="project-source:app/services/stale.py#sample",
            content_hash="a" * 64,
            scope="GLOBAL",
            company_id=None,
            user_id=None,
            version=1,
            status=DOCUMENT_ACTIVE,
            approval_status=APPROVAL_APPROVED,
            created_at="2026-09-02T00:00:00+00:00",
            source_kind=SOURCE_KIND_PROJECT_SOURCE,
            source_revision="b" * 40,
            source_content_hash="c" * 64,
            search_aliases=("stale project source",),
        )
        repo._read_manifest = lambda: [stale_project]  # type: ignore[method-assign]
        repo._project_source_is_current = lambda source: False  # type: ignore[method-assign]
        stale_available = repo.get_help_example_queries(
            current_user_id=14,
            current_company_id=4,
            permission_codes=("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ"),
        )
        assert stale_available["project_source_technical"] == ""
    finally:
        shutil.rmtree(root)


def main() -> None:
    _assert_knowledge_help_availability()
    phrases = (
        "SIMS 관련 프롬프트 알려줘",
        "SIMS 질문 예시 알려줘",
        "SIMS에서 뭘 물어볼 수 있어?",
        "질문 예시 알려줘",
        "SIMS 사용법 알려줘",
        "SIMS 사용법 상세히 알려줘",
        "SIMS 사용법 자세히 알려줘",
    )
    for phrase in phrases:
        assert parse_sims_help_request(phrase) is not None

    for ordinary_input in (
        "오늘 입고현황",
        "현재표 거래처별 매출금액 분석",
        "SIMS 일일점검",
        "/knowledge 사용법 알려줘",
        "/mcp-resource official-recall-notice 안내",
        "현재 시간 알려줘",
        "SIMS 관련 질문이 있어요",
    ):
        assert parse_sims_help_request(ordinary_input) is None

    admin_allowed = build_sims_help_text((
        "MASTER_READ", "IO_READ", "KPI_READ", "RAG_USE",
        "KNOWLEDGE_PROJECT_SOURCE_READ", "KNOWLEDGE_ERP_DB_READ",
        "KNOWLEDGE_GLOBAL_MANAGE", "KNOWLEDGE_COMPANY_MANAGE",
    ), knowledge_availability={
        "general": "재고 관리 기준",
        "erp_technical": "Rddbc110",
        "project_source_technical": "사용자 파일 경로 함수",
    })
    for expected in (
        "재고", "입고/출고", "매출/매입", "오늘 매출현황", "KPI/예측", "현재표 분석", "SIMS 일일점검",
        "품목별 매출 추세 요약표", "품목별 매출 예상", "영업사원별 매출 예상",
        "거래처별 매출 예상", "품목별 재고부족현황", "매입처별 재고부족 현황",
        "현재표 <칼럼명>별 집계", "현재표 <칼럼명>별 분석", "현재표 <칼럼명>별 요약",
        "현재표 <칼럼명> TOP 10", "현재표에서 금액 TOP 10", "/knowledge-tech Rddbc110",
    ):
        assert expected in admin_allowed
    assert "SIMS_JSON" not in admin_allowed and "CONTEXT" not in admin_allowed and "SQL" not in admin_allowed

    io_only = build_sims_help_text(("IO_READ",))
    assert "재고" in io_only and "입고" in io_only and "출고" in io_only and "매입" in io_only
    assert "품목별 매출 예상" not in io_only and "SIMS 일일점검" not in io_only and "/knowledge" not in io_only

    staff = build_sims_help_text(("RAG_USE",), knowledge_availability={"general": "", "erp_technical": "", "project_source_technical": ""})
    assert "/knowledge 재고 관리 기준" not in staff
    assert "/knowledge-tech" not in staff

    readonly = build_sims_help_text(())
    assert "Knowledge 자료" not in readonly and "/knowledge" not in readonly

    assert "권한으로 안내할 수 있는" in readonly

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "parse_sims_help_request(user_input)" in main_source
    assert "_run_sims_help_chat(sims_help_route" in main_source
    assert main_source.index("sims_help_route = (") < main_source.index("resolve_datetime_question(user_input)")
    assert "or sims_help_route is not None else resolve_datetime_question(user_input)" in main_source
    assert main_source.index("if sims_help_route is not None:") < main_source.index("current_table_followup_input = _normalize_current_table_followup_input(user_input)")
    run_start = main_source.index("def _run_sims_help_chat(")
    run_end = main_source.index("\ndef _run_explicit_mcp_resource_chat(", run_start)
    help_block = main_source[run_start:run_end]
    assert "get_current_permissions()" in help_block
    assert not any(
        token in help_block
        for token in (
            "call_chat(",
            "stream_and_append_assistant(",
            "search_web(",
            "retrieve_for_chat(",
            "try_handle_nlq(",
        )
    )
    print("RESULT OK tests=8 llm_call_count=0")


if __name__ == "__main__":
    main()

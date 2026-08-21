"""Evaluate lexical Knowledge retrieval against a small, explicit corpus of real project Markdown.

The files are read only.  They are registered only in a temporary evaluation
repository and never promoted to the operating Knowledge manifest.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (  # noqa: E402
    ContextPacket,
    KnowledgeDocumentRepository,
)
from app.services.llm_health import extract_chat_completion_text  # noqa: E402
from app.utils.env_config import read_project_env_file  # noqa: E402


RAG_USE = ("RAG_USE",)
ERP_READ = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ", "KNOWLEDGE_ERP_DB_READ")
GLOBAL_MANAGE = ("KNOWLEDGE_GLOBAL_MANAGE",)
COMPANY_MANAGE = ("KNOWLEDGE_COMPANY_MANAGE",)
INSUFFICIENT_ANSWER = "자료가 부족합니다."


@dataclass(frozen=True)
class DocumentSpec:
    relative_path: str
    source_name: str
    source_key: str
    classification: str = "GENERAL"


@dataclass(frozen=True)
class EvalCase:
    name: str
    query: str
    expected_sources: tuple[str, ...] = ()
    expected_sections: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    expected_no_match: bool = False
    company_id: int = 4
    user_id: int = 11
    permissions: tuple[str, ...] = RAG_USE
    technical_detail_mode: bool = False
    max_chars: int = 900


DOCUMENTS = (
    DocumentSpec("docs/README.md", "docs-readme.md", "docs-readme"),
    DocumentSpec(
        "docs/02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md",
        "nlq-current-stock-contract.md",
        "nlq-current-stock-contract",
    ),
    DocumentSpec(
        "docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.md",
        "nlq-period-policy.md",
        "nlq-period-policy",
    ),
    DocumentSpec(
        "docs/02_design/DASHBOARD_LITE_V01_DESIGN.md",
        "dashboard-lite-design.md",
        "dashboard-lite-design",
    ),
    DocumentSpec(
        "docs/02_design/DASHBOARD_KPI_NLQ_공통조회조건_확정안_ver03.md",
        "dashboard-kpi-nlq-policy.md",
        "dashboard-kpi-nlq-policy",
    ),
    DocumentSpec(
        "docs/02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md",
        "lm-studio-extension-plan.md",
        "lm-studio-extension-plan",
    ),
    DocumentSpec(
        "docs/AUTH_DB_SCHEMA.md",
        "auth-db-schema.md",
        "auth-db-schema",
        "ERP_DB_INTERNAL",
    ),
)
SCOPE_CONTROL_SOURCE = "company-scope-control.md"


def _register_global(repo: KnowledgeDocumentRepository, spec: DocumentSpec, *, version: int = 1):
    content = (ROOT / spec.relative_path).read_text(encoding="utf-8")
    source, created = repo.register_text_checked(
        source_name=spec.source_name,
        source_key=spec.source_key,
        content=content,
        scope="GLOBAL",
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
        version=version,
        knowledge_classification=spec.classification,
    )
    assert created
    return repo.approve_checked(
        document_id=source.document_id,
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
    )


def build_real_document_corpus(repo: KnowledgeDocumentRepository) -> dict[str, Any]:
    registered: dict[str, Any] = {}
    for spec in DOCUMENTS:
        if spec.source_key == "docs-readme":
            _register_global(repo, spec, version=1)
            registered[spec.source_name] = _register_global(repo, spec, version=2)
        else:
            registered[spec.source_name] = _register_global(repo, spec)

    # No real company-specific official document exists in the selected set.
    # This read-only README excerpt is mounted only as an explicit temporary
    # scope control, never as a proposed production classification.
    scope_text = "# 회사 범위 평가 제어\nSCOPE-CO-4는 company=4 전용 평가 격리 식별자입니다."
    scope_source, created = repo.register_text_checked(
        source_name=SCOPE_CONTROL_SOURCE,
        source_key="evaluation-company-scope-control",
        content=scope_text,
        scope="COMPANY",
        company_id=4,
        current_company_id=4,
        permission_codes=COMPANY_MANAGE,
    )
    assert created
    registered[SCOPE_CONTROL_SOURCE] = repo.approve_checked(
        document_id=scope_source.document_id,
        current_company_id=4,
        permission_codes=COMPANY_MANAGE,
    )
    return {
        "selected_documents": [
            {"path": spec.relative_path, "source_name": spec.source_name, "classification": spec.classification}
            for spec in DOCUMENTS
        ],
        "scope_control": "temporary_company_scope_probe_from_current_readme_policy",
        "document_count": len(repo._read_manifest()),
        "active_versions": {name: source.version for name, source in registered.items()},
    }


def evaluation_cases() -> tuple[EvalCase, ...]:
    no_match = dict(expected_no_match=True)
    return (
        EvalCase("markdown_source_of_truth", "Markdown 공식 source of truth", ("docs-readme.md",), ("Source Of Truth",)),
        EvalCase("archive_policy", "90 archive 대상", ("docs-readme.md",), ("개정과 보관 원칙",)),
        EvalCase("ambiguous_baseline", "기준일 branch commit 불명확", ("docs-readme.md",), ("개정과 보관 원칙",)),
        EvalCase("stock_ignores_io_profile", "현재고 저장 io gu list 적용", ("nlq-current-stock-contract.md",), ("3.2 저장조건과 재고 계산",)),
        EvalCase("current_table_isolation", "회사 변경 current table export cache 재사용", ("nlq-current-stock-contract.md",), ("2. 공통 원칙",)),
        EvalCase("display_export_boundary", "display copy export 원본", ("nlq-current-stock-contract.md",), ("3.4 display와 원본 분리",)),
        EvalCase("month_calendar_policy", "8월 2026 08 01 2026 08 31", ("nlq-period-policy.md",), ("4. 월 지정 / 이번달",)),
        EvalCase("rolling_month_policy", "최근 한달 기준일 30일 종료일", ("nlq-period-policy.md",), ("5. 한달 / 최근 한달 / 최근 1개월",)),
        EvalCase("period_priority", "직접 지정 기간 자동 기본기간 우선", ("nlq-period-policy.md",), ("2. 기간 해석 우선순위",)),
        EvalCase("current_month_policy", "이번달 현재일까지 자르지 않고", ("nlq-period-policy.md",), ("4. 월 지정 / 이번달",)),
        EvalCase("dashboard_llm_role", "Dashboard LLM 숫자 재계산", ("dashboard-lite-design.md",), ("1. 목적",)),
        EvalCase("dashboard_order", "상태 근거 조치 상세표", ("dashboard-lite-design.md",), ("1. 목적",)),
        EvalCase("explicit_filter_priority", "명시 조건 저장 profile 우선", ("dashboard-kpi-nlq-policy.md",), ("2. 조건 우선순위",)),
        EvalCase("profile_stock_exception", "현재고 재고위치 io gu list", ("dashboard-kpi-nlq-policy.md",), ("4. 공통 profile 조건",)),
        EvalCase("supplier_canonical", "supplier 매입처 자동 치환", ("dashboard-kpi-nlq-policy.md",), ("5. 공급 역할 canonical 구분",)),
        EvalCase("rag_vector_first", "처음부터 자체 RAG Vector DB", ("lm-studio-extension-plan.md",), ("1. 목적",)),
        EvalCase("rag_document_candidate", "최신 공식 Markdown archive", ("lm-studio-extension-plan.md",), ("4.1 1차 문서 후보",)),
        EvalCase("spacing_variant", "회사별 현재 표 export cache 재사용", ("nlq-current-stock-contract.md",), ("2. 공통 원칙",)),
        EvalCase("auth_role_permission_table", "SSAI ROLE PERMISSIONS 역할별 권한", ("auth-db-schema.md",), ("1. 테이블 목록",), permissions=ERP_READ, technical_detail_mode=True),
        EvalCase("auth_user_company_table", "SSAI USER COMPANIES 사용자별 접근 가능 회사", ("auth-db-schema.md",), ("1. 테이블 목록",), permissions=ERP_READ, technical_detail_mode=True),
        EvalCase("scope_company_allow", "SCOPE CO 4", (SCOPE_CONTROL_SOURCE,), ("회사 범위 평가 제어",)),
        EvalCase("scope_company_deny", "SCOPE CO 4", excluded_sources=(SCOPE_CONTROL_SOURCE,), company_id=6, **no_match),
        EvalCase("erp_system_admin_allow", "SSAI ROLE PERMISSIONS 역할별 권한", ("auth-db-schema.md",), ("1. 테이블 목록",), permissions=ERP_READ, technical_detail_mode=True),
        EvalCase("erp_ssart_manager_allow", "SSAI USERS 사용자 기본 테이블", ("auth-db-schema.md",), ("2. SSAI_USERS",), permissions=ERP_READ, technical_detail_mode=True),
        EvalCase("erp_ssart_staff_deny", "SSAI ROLE PERMISSIONS 역할별 권한", excluded_sources=("auth-db-schema.md",), **no_match),
        EvalCase("erp_wholesale_manager_deny", "SSAI USERS 사용자 기본 테이블", excluded_sources=("auth-db-schema.md",), **no_match),
        EvalCase("unrelated_leave", "직원 휴가 신청 승인 절차", **no_match),
        EvalCase("partial_false_positive", "SSAI ROLES 휴가 규정", **no_match),
        EvalCase("unrelated_tax_invoice", "세금계산서 발행 정책", **no_match),
        EvalCase("unknown_code", "COLD 99 품질 규격", **no_match),
    )
def _sources(packet: ContextPacket) -> set[str]:
    return {citation.source_name for citation in packet.citations}


def evaluate_retrieval(repo: KnowledgeDocumentRepository, cases: tuple[EvalCase, ...], protected_hashes_by_source: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, ContextPacket]]:
    results: list[dict[str, Any]] = []
    packets: dict[str, ContextPacket] = {}
    original_read = repo._read_artifact
    reads: list[str] = []

    def tracked_read(content_hash: str):
        reads.append(content_hash)
        return original_read(content_hash)

    repo._read_artifact = tracked_read  # type: ignore[method-assign]
    try:
        for case in cases:
            before = len(reads)
            started = time.perf_counter()
            packet = repo.retrieve(
                query=case.query,
                current_user_id=case.user_id,
                current_company_id=case.company_id,
                permission_codes=case.permissions,
                technical_detail_mode=case.technical_detail_mode,
                max_chars=case.max_chars,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            packets[case.name] = packet
            sources = _sources(packet)
            sections = {citation.section_title for citation in packet.citations}
            new_reads = set(reads[before:])
            checks = {
                "expected_sources": set(case.expected_sources).issubset(sources),
                "expected_sections": set(case.expected_sections).issubset(sections),
                "excluded_sources": not (set(case.excluded_sources) & sources),
                "no_match": (not sources) if case.expected_no_match else True,
                "bounded_context": len(packet.text) <= case.max_chars,
                "citation_in_context": all(citation.label in packet.text for citation in packet.citations),
                "protected_artifact_read_zero": not (new_reads & {protected_hashes_by_source[name] for name in case.excluded_sources if name in protected_hashes_by_source}),
            }
            results.append(
                {
                    "name": case.name,
                    "status": "pass" if all(checks.values()) else "fail",
                    "reason_code": packet.reason_code,
                    "candidate_count": packet.candidate_count,
                    "context_chars": len(packet.text),
                    "sources": sorted(sources),
                    "sections": sorted(sections),
                    "checks": checks,
                    "elapsed_ms": elapsed_ms,
                }
            )
    finally:
        repo._read_artifact = original_read  # type: ignore[method-assign]
    return results, packets


def _grounded_messages(question: str, packet: ContextPacket) -> list[dict[str, str]]:
    return [{"role": "user", "content": (
        "아래 CONTEXT만 근거로 한국어로 한두 문장으로 답하세요. "
        "근거가 있는 문장 끝에는 CONTEXT의 citation을 정확히 그대로 붙이세요. "
        f"근거가 부족하면 정확히 '{INSUFFICIENT_ANSWER}'라고만 답하세요.\n\n"
        f"QUESTION:\n{question}\n\nCONTEXT:\n{packet.text}"
    )}]


def evaluate_llm(packets: dict[str, ContextPacket], *, timeout_seconds: int) -> list[dict[str, Any]]:
    env = read_project_env_file()
    base_url = str(env.get("LMSTUDIO_BASE_URL") or "").strip()
    model = str(env.get("LLM_MODEL_DEFAULT") or env.get("LMSTUDIO_MODEL") or "").strip()
    if not base_url or not model:
        raise RuntimeError("llm_configuration_missing")
    client = OpenAI(
        base_url=base_url,
        api_key=str(env.get("LMSTUDIO_API_KEY") or "lm-studio"),
        timeout=max(1, int(timeout_seconds)),
        max_retries=0,
    )
    live_cases = (
        ("markdown_source_of_truth", "공식 문서의 source of truth를 알려줘."),
        ("display_export_boundary", "화면 display와 export 원본의 관계를 알려줘."),
        ("month_calendar_policy", "2026년 8월의 NLQ 기간을 간단히 알려줘."),
        ("dashboard_order", "Dashboard의 고정 화면 순서를 알려줘."),
        ("auth_role_permission_table", "역할별 권한 grant 관계를 저장하는 테이블을 알려줘."),
        ("unrelated_leave", "직원 휴가 신청 승인 절차를 알려줘."),
    )
    results: list[dict[str, Any]] = []
    for name, question in live_cases:
        packet = packets[name]
        if not packet.text:
            results.append({"name": name, "llm_called": False, "reason_code": "insufficient_context", "quality_pass": True, "elapsed_ms": 0})
            continue
        started = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=_grounded_messages(question, packet),
            temperature=0.1,
            stream=False,
            max_tokens=220,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        extracted = extract_chat_completion_text(response)
        answer = str(extracted.get("content") or "").strip()
        citations = {citation.label for citation in packet.citations}
        found = {label for label in citations if label in answer}
        results.append({
            "name": name,
            "llm_called": True,
            "reason_code": str(extracted.get("code") or ""),
            "quality_pass": bool(answer) and bool(found) and found.issubset(citations),
            "elapsed_ms": elapsed_ms,
            "citation_count": len(found),
            "answer_chars": len(answer),
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate lexical RAG using a temporary corpus of current project documents.")
    parser.add_argument("--live-llm", action="store_true", help="Run six bounded LM Studio answer checks after retrieval.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="knowledge-real-docs-eval-"))
    try:
        repo = KnowledgeDocumentRepository(root=root)
        corpus = build_real_document_corpus(repo)
        sources = {source.source_name: source for source in repo._read_manifest()}
        protected_hashes_by_source = {"auth-db-schema.md": sources["auth-db-schema.md"].content_hash, SCOPE_CONTROL_SOURCE: sources[SCOPE_CONTROL_SOURCE].content_hash}
        cases = evaluation_cases()
        retrieval, packets = evaluate_retrieval(repo, cases, protected_hashes_by_source)
        llm = evaluate_llm(packets, timeout_seconds=args.timeout_seconds) if args.live_llm else []
        result = {
            "ok": all(row["status"] == "pass" for row in retrieval) and all(row["quality_pass"] for row in llm),
            "mode": "live_llm" if args.live_llm else "retrieval_only",
            "corpus": corpus,
            "retrieval": {
                "case_count": len(retrieval),
                "pass_count": sum(row["status"] == "pass" for row in retrieval),
                "false_positive_count": sum(row["status"] != "pass" and row["candidate_count"] > 0 for row in retrieval),
                "no_match_pass_count": sum(row["status"] == "pass" for row in retrieval if row["reason_code"] == "no_authorized_match"),
                "average_elapsed_ms": round(sum(row["elapsed_ms"] for row in retrieval) / len(retrieval), 3),
                "rows": retrieval,
            },
            "llm": {"case_count": len(llm), "pass_count": sum(row["quality_pass"] for row in llm), "total_elapsed_ms": round(sum(row["elapsed_ms"] for row in llm), 3), "rows": llm},
            "retry_count": 0,
            "persistent_knowledge_written": False,
            "embedding_called": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
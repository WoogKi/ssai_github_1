"""Evaluate the Knowledge retrieval PoC and optionally call the configured LM Studio.

The corpus is synthetic and lives in a temporary directory. Nothing from the
attachment upload area is imported, and no database operation is performed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re
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


RAG_PERMISSION = ["RAG_USE"]
GLOBAL_MANAGE = ["KNOWLEDGE_GLOBAL_MANAGE"]
COMPANY_MANAGE = ["KNOWLEDGE_COMPANY_MANAGE"]
INSUFFICIENT_ANSWER = "자료가 부족합니다."
CITATION_PATTERN = re.compile(r"\[[^\[\]\n]+ v\d+ §[^\[\]\n]+\]")


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    query: str
    current_user_id: int
    current_company_id: int
    permission_codes: tuple[str, ...] = ("RAG_USE",)
    max_chars: int = 1200
    expected_reason: str = "ready"
    expected_sources: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    expected_sections: tuple[str, ...] = ()
    known_limitation: str = ""


class _LegacyKeywordRepository(KnowledgeDocumentRepository):
    @staticmethod
    def _score(query: str, source: Any, section: dict[str, str]) -> int:
        terms = [term for term in re.split(r"\s+", query.casefold().strip()) if term]
        haystack = " ".join((source.source_name, source.source_key, section["title"], section["text"])).casefold()
        return sum(haystack.count(term) for term in terms)


def _register_approved(repo: KnowledgeDocumentRepository, **kwargs: Any):
    scope = str(kwargs.get("scope") or "").upper()
    if scope == "GLOBAL":
        source, created = repo.register_text_checked(
            **kwargs,
            current_company_id=4,
            permission_codes=GLOBAL_MANAGE,
        )
        assert created
        return repo.approve_checked(
            document_id=source.document_id,
            current_company_id=4,
            permission_codes=GLOBAL_MANAGE,
        )
    if scope == "COMPANY":
        source, created = repo.register_text_checked(
            **kwargs,
            current_company_id=int(kwargs["company_id"]),
            permission_codes=COMPANY_MANAGE,
        )
        assert created
        return repo.approve_checked(
            document_id=source.document_id,
            current_company_id=int(kwargs["company_id"]),
            permission_codes=COMPANY_MANAGE,
        )
    # USER management is intentionally undefined. Explicit synthetic fixtures
    # use the private trusted boundary; no attachment or user upload is read.
    source, created = repo._register_text_trusted(**kwargs)
    assert created
    return repo._approve_trusted(document_id=source.document_id)


def build_fixture_corpus(repo: KnowledgeDocumentRepository) -> dict[str, Any]:
    v1 = _register_approved(
        repo,
        source_name="operations-policy.md",
        source_key="operations-policy",
        version=1,
        scope="GLOBAL",
        content="# 주문 마감\n평일 주문 마감 시각은 16시입니다.\n# 반품\n반품 접수 코드는 RTN-7입니다.",
    )
    v2 = _register_approved(
        repo,
        source_name="operations-policy.md",
        source_key="operations-policy",
        version=2,
        scope="GLOBAL",
        content="# 주문 마감\n평일 주문 마감 시각은 17시입니다.\n# 반품\n반품 접수 코드는 RTN-8입니다.",
    )
    duplicate_text = "# 공통 복제 지침\nALPHA-42 코드는 검수 완료를 뜻합니다."
    duplicate_a = _register_approved(
        repo,
        source_name="alpha-primary.md",
        source_key="alpha-primary",
        scope="GLOBAL",
        content=duplicate_text,
    )
    duplicate_b = _register_approved(
        repo,
        source_name="alpha-copy.md",
        source_key="alpha-copy",
        scope="GLOBAL",
        content=duplicate_text,
    )
    _register_approved(
        repo,
        source_name="product-code.md",
        source_key="product-code",
        scope="GLOBAL",
        content="# Product Code\nSKU-Z9 제품의 보관 온도는 2~8C입니다.",
    )
    _register_approved(
        repo,
        source_name="company-four.md",
        source_key="company-four",
        scope="COMPANY",
        company_id=4,
        content="# 냉장 배송\n회사 4의 냉장 배송 확인 코드는 COLD-4입니다.\n# 정산\n월 정산일은 25일입니다.",
    )
    _register_approved(
        repo,
        source_name="company-six.md",
        source_key="company-six",
        scope="COMPANY",
        company_id=6,
        content="# 냉장 배송\n회사 6의 냉장 배송 확인 코드는 COLD-6입니다.",
    )
    _register_approved(
        repo,
        source_name="user-eleven.md",
        source_key="user-eleven",
        scope="USER",
        company_id=4,
        user_id=11,
        content="# 개인 검토\nBLUE-11은 사용자 11의 개인 검토 코드입니다.",
    )
    _register_approved(
        repo,
        source_name="user-twelve.md",
        source_key="user-twelve",
        scope="USER",
        company_id=4,
        user_id=12,
        content="# 개인 검토\nRED-12는 사용자 12의 개인 검토 코드입니다.",
    )
    _register_approved(
        repo,
        source_name="bounded.md",
        source_key="bounded",
        scope="GLOBAL",
        content="# 긴 문맥\nBOUND-KEY " + "문맥길이검증 " * 120,
    )
    pending, _ = repo._register_text_trusted(
        source_name="pending.md",
        source_key="pending",
        scope="GLOBAL",
        content="# 승인 전\nPENDING-SECRET은 검색되면 안 됩니다.",
    )
    return {
        "document_count": len(repo._read_manifest()),
        "artifact_count": len(list(repo.artifact_dir.glob("*.json"))),
        "superseded_document_id": v1.document_id,
        "active_document_id": v2.document_id,
        "pending_document_id": pending.document_id,
        "duplicate_artifact_shared": duplicate_a.content_hash == duplicate_b.content_hash,
    }


def evaluation_cases() -> tuple[RetrievalCase, ...]:
    return (
        RetrievalCase("exact_keyword", "RTN-8", 11, 4, expected_sources=("operations-policy.md",), expected_sections=("반품",)),
        RetrievalCase("korean_spacing", "냉장 배송", 11, 4, expected_sources=("company-four.md",), excluded_sources=("company-six.md",)),
        RetrievalCase("korean_partial", "냉장", 11, 4, expected_sources=("company-four.md",), excluded_sources=("company-six.md",)),
        RetrievalCase("code_english_mixed", "SKU-Z9 보관", 11, 4, expected_sources=("product-code.md",), expected_sections=("Product Code",)),
        RetrievalCase("same_content_provenance", "ALPHA-42", 11, 4, expected_sources=("alpha-primary.md", "alpha-copy.md")),
        RetrievalCase("latest_active", "마감 시각 17시", 11, 4, expected_sources=("operations-policy.md",), expected_sections=("주문 마감",)),
        RetrievalCase("superseded_excluded", "16시", 11, 4, expected_reason="no_authorized_match", excluded_sources=("operations-policy.md",)),
        RetrievalCase("company_exact_allow", "COLD-6", 11, 6, expected_sources=("company-six.md",), excluded_sources=("company-four.md",)),
        RetrievalCase("cross_company_deny", "COLD-4", 11, 6, expected_reason="no_authorized_match", excluded_sources=("company-four.md",)),
        RetrievalCase("same_user_allow", "BLUE-11", 11, 4, expected_sources=("user-eleven.md",), excluded_sources=("user-twelve.md",)),
        RetrievalCase("cross_user_deny", "BLUE-11", 12, 4, expected_reason="no_authorized_match", excluded_sources=("user-eleven.md",)),
        RetrievalCase("missing_rag_use", "RTN-8", 11, 4, permission_codes=(), expected_reason="no_authorized_match"),
        RetrievalCase("pending_excluded", "PENDING-SECRET", 11, 4, expected_reason="no_authorized_match", excluded_sources=("pending.md",)),
        RetrievalCase("unrelated_question", "우주선 연료 규정", 11, 4, expected_reason="no_authorized_match"),
        RetrievalCase("bounded_context", "BOUND-KEY", 11, 4, max_chars=140, expected_sources=("bounded.md",), expected_sections=("긴 문맥",)),
        RetrievalCase(
            "korean_no_space_known_miss",
            "냉장배송",
            11,
            4,
            expected_sources=("company-four.md",),
            excluded_sources=("company-six.md",),
            known_limitation="legacy whitespace-sensitive matching missed 냉장 배송",
        ),
        RetrievalCase(
            "partial_term_known_false_positive",
            "배송 외계행성",
            11,
            4,
            expected_reason="no_authorized_match",
            excluded_sources=("company-four.md",),
            known_limitation="legacy OR-like scoring accepted a document matching only 배송",
        ),
    )


def evaluate_retrieval(repo: KnowledgeDocumentRepository) -> tuple[list[dict[str, Any]], dict[str, ContextPacket]]:
    results: list[dict[str, Any]] = []
    packets: dict[str, ContextPacket] = {}
    for case in evaluation_cases():
        started = time.perf_counter()
        packet = repo.retrieve(
            query=case.query,
            current_user_id=case.current_user_id,
            current_company_id=case.current_company_id,
            permission_codes=case.permission_codes,
            max_chars=case.max_chars,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        packets[case.name] = packet
        sources = {citation.source_name for citation in packet.citations}
        sections = {citation.section_title for citation in packet.citations}
        checks = {
            "reason": packet.reason_code == case.expected_reason,
            "expected_sources": set(case.expected_sources).issubset(sources),
            "excluded_sources": not (set(case.excluded_sources) & sources),
            "expected_sections": set(case.expected_sections).issubset(sections),
            "bounded": len(packet.text) <= case.max_chars,
            "citation_labels_in_context": all(citation.label in packet.text for citation in packet.citations),
        }
        results.append(
            {
                "name": case.name,
                "query": case.query,
                "status": "pass" if all(checks.values()) else "fail",
                "reason_code": packet.reason_code,
                "candidate_count": packet.candidate_count,
                "context_chars": len(packet.text),
                "citations": [citation.label for citation in packet.citations],
                "checks": checks,
                "known_limitation": case.known_limitation,
                "elapsed_ms": elapsed_ms,
            }
        )
    return results, packets


def retrieval_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(row["elapsed_ms"]) for row in results]
    return {
        "case_count": len(results),
        "pass_count": sum(row["status"] == "pass" for row in results),
        "total_elapsed_ms": round(sum(elapsed), 3),
        "average_elapsed_ms": round(sum(elapsed) / len(elapsed), 3) if elapsed else 0,
        "failed_cases": [row["name"] for row in results if row["status"] != "pass"],
    }


def scoring_benchmark(repo: KnowledgeDocumentRepository, *, iterations: int = 200) -> dict[str, Any]:
    rows: list[tuple[Any, dict[str, str]]] = []
    for source in repo._read_manifest():
        if source.status != "ACTIVE" or source.approval_status != "APPROVED":
            continue
        artifact = repo._read_artifact(source.content_hash)
        rows.extend((source, section) for section in artifact.sections)
    queries = [case.query for case in evaluation_cases()]
    elapsed = {"legacy": 0.0, "improved": 0.0}
    scorers = {
        "legacy": _LegacyKeywordRepository._score,
        "improved": KnowledgeDocumentRepository._score,
    }
    for iteration in range(iterations):
        order = ("legacy", "improved") if iteration % 2 == 0 else ("improved", "legacy")
        for name in order:
            started = time.perf_counter()
            scorer = scorers[name]
            for query in queries:
                for source, section in rows:
                    scorer(query, source, section)
            elapsed[name] += (time.perf_counter() - started) * 1000
    denominator = max(1, iterations * len(queries))
    return {
        "iterations": iterations,
        "query_count": len(queries),
        "section_count": len(rows),
        "legacy_total_ms": round(elapsed["legacy"], 3),
        "improved_total_ms": round(elapsed["improved"], 3),
        "legacy_ms_per_query": round(elapsed["legacy"] / denominator, 6),
        "improved_ms_per_query": round(elapsed["improved"] / denominator, 6),
    }


def evaluate_semantic_gaps(repo: KnowledgeDocumentRepository) -> list[dict[str, Any]]:
    cases = (
        ("cold_chain_synonym", "저온 유통 확인값"),
        ("return_paraphrase", "되돌려 보내는 상품의 접수 번호"),
    )
    results: list[dict[str, Any]] = []
    for name, query in cases:
        packet = repo.retrieve(
            query=query,
            current_user_id=11,
            current_company_id=4,
            permission_codes=RAG_PERMISSION,
            max_chars=1200,
        )
        results.append(
            {
                "name": name,
                "query": query,
                "reason_code": packet.reason_code,
                "context_chars": len(packet.text),
                "citations": [citation.label for citation in packet.citations],
                "semantic_miss_confirmed": packet.reason_code == "no_authorized_match",
            }
        )
    return results


def _grounded_messages(question: str, packet: ContextPacket) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": (
                "아래 CONTEXT만 근거로 한국어로 간결하게 답하세요.\n"
                "근거가 있는 문장 끝에는 CONTEXT에 적힌 citation을 정확히 그대로 붙이세요.\n"
                f"근거가 부족하면 다른 지식을 사용하지 말고 정확히 '{INSUFFICIENT_ANSWER}'라고 답하세요.\n\n"
                f"QUESTION:\n{question}\n\nCONTEXT:\n{packet.text}"
            ),
        }
    ]


def _validate_grounded_answer(answer: str, packet: ContextPacket) -> dict[str, Any]:
    allowed = {citation.label for citation in packet.citations}
    found = set(CITATION_PATTERN.findall(answer))
    return {
        "has_answer": bool(answer.strip()),
        "has_citation": bool(found),
        "citations_valid": bool(found) and found.issubset(allowed),
        "citation_labels": sorted(found),
    }


def evaluate_llm(packets: dict[str, ContextPacket], *, timeout_seconds: int) -> list[dict[str, Any]]:
    project_env = read_project_env_file()
    base_url = str(project_env.get("LMSTUDIO_BASE_URL") or "").strip()
    api_key = str(project_env.get("LMSTUDIO_API_KEY") or "lm-studio").strip()
    model = str(project_env.get("LLM_MODEL_DEFAULT") or project_env.get("LMSTUDIO_MODEL") or "").strip()
    if not base_url or not model:
        raise RuntimeError("LM Studio base URL or model is not configured")
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0).with_options(
        timeout=max(1, int(timeout_seconds)),
        max_retries=0,
    )
    live_cases = {
        "exact_keyword": "반품 접수 코드를 알려줘.",
        "company_exact": "회사 4의 냉장 배송 확인 코드를 알려줘.",
        "same_content_provenance": "ALPHA-42 의미와 근거 출처를 모두 알려줘.",
        "unrelated_question": "우주선 연료 규정을 알려줘.",
    }
    packet_names = {
        "exact_keyword": "exact_keyword",
        "company_exact": "korean_spacing",
        "same_content_provenance": "same_content_provenance",
        "unrelated_question": "unrelated_question",
    }
    results: list[dict[str, Any]] = []
    for name, question in live_cases.items():
        packet = packets[packet_names[name]]
        if not packet.text:
            results.append(
                {
                    "name": name,
                    "answer": INSUFFICIENT_ANSWER,
                    "reason_code": "insufficient_context",
                    "llm_called": False,
                    "elapsed_ms": 0,
                    "quality_pass": True,
                    "citation_labels": [],
                }
            )
            continue
        started = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=_grounded_messages(question, packet),
            temperature=0.1,
            stream=False,
            max_tokens=320,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        extracted = extract_chat_completion_text(response)
        answer = str(extracted.get("content") or "").strip()
        validation = _validate_grounded_answer(answer, packet)
        quality_pass = all(validation[key] for key in ("has_answer", "has_citation", "citations_valid"))
        results.append(
            {
                "name": name,
                "answer": answer,
                "reason_code": str(extracted.get("code") or ""),
                "llm_called": True,
                "elapsed_ms": elapsed_ms,
                "quality_pass": quality_pass,
                "citation_labels": validation["citation_labels"],
                "usage": extracted.get("usage"),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate synthetic Knowledge RAG retrieval and citations.")
    parser.add_argument("--live-llm", action="store_true", help="Call the configured LM Studio for four selected cases.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="knowledge-rag-eval-"))
    try:
        repo = KnowledgeDocumentRepository(root=root)
        corpus = build_fixture_corpus(repo)
        benchmark = scoring_benchmark(repo)
        legacy_repo = _LegacyKeywordRepository(root=root)
        baseline_retrieval, _ = evaluate_retrieval(legacy_repo)
        retrieval, packets = evaluate_retrieval(repo)
        retrieval_pass = all(row["status"] == "pass" for row in retrieval)
        semantic_gaps = evaluate_semantic_gaps(repo)
        llm_results = evaluate_llm(packets, timeout_seconds=args.timeout_seconds) if args.live_llm else []
        llm_pass = all(row["quality_pass"] for row in llm_results) if llm_results else None
        result = {
            "ok": retrieval_pass and (llm_pass is not False),
            "mode": "live-llm" if args.live_llm else "retrieval-only",
            "corpus": corpus,
            "legacy_baseline": retrieval_summary(baseline_retrieval),
            "improved_lexical": retrieval_summary(retrieval),
            "scoring_benchmark": benchmark,
            "retrieval_case_count": len(retrieval),
            "retrieval_pass_count": sum(row["status"] == "pass" for row in retrieval),
            "known_limitation_count": sum(bool(row["known_limitation"]) for row in retrieval),
            "retrieval": retrieval,
            "semantic_gaps": semantic_gaps,
            "llm_case_count": len(llm_results),
            "llm_quality_pass_count": sum(bool(row["quality_pass"]) for row in llm_results),
            "llm": llm_results,
            "retry_count": 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())

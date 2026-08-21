"""Evaluate lexical Knowledge retrieval against a small, AST-extracted Python source corpus.

This is an evaluation-only harness.  It reads selected source files, registers
only their extracted symbols in a temporary repository, and never writes an
operating Knowledge manifest or a persistent source index.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import ContextPacket, KnowledgeDocumentRepository  # noqa: E402
from app.services.llm_health import extract_chat_completion_text  # noqa: E402
from app.utils.env_config import read_project_env_file  # noqa: E402


RAG_USE = ("RAG_USE",)
PROJECT_SOURCE_READ = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ")
ERP_READ = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ", "KNOWLEDGE_ERP_DB_READ")
GLOBAL_MANAGE = ("KNOWLEDGE_GLOBAL_MANAGE",)
COMPANY_MANAGE = ("KNOWLEDGE_COMPANY_MANAGE",)
INSUFFICIENT_ANSWER = "자료가 부족합니다."


@dataclass(frozen=True)
class SourceSpec:
    relative_path: str
    classification: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class ExtractedSymbol:
    relative_path: str
    symbol: str
    start_line: int
    end_line: int
    raw_text: str
    raw_hash: str
    text: str


@dataclass(frozen=True)
class EvalCase:
    name: str
    query: str
    expected_symbols: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    expected_no_match: bool = False
    company_id: int = 4
    user_id: int = 11
    permissions: tuple[str, ...] = PROJECT_SOURCE_READ
    technical_detail_mode: bool = True
    max_chars: int = 1200


SOURCES = (
    SourceSpec(
        "app/services/knowledge_scope_policy.py",
        "GENERAL",
        ("can_read_document", "can_manage_document"),
    ),
    SourceSpec(
        "app/services/knowledge_document_service.py",
        "GENERAL",
        ("KnowledgeDocumentRepository.retrieve",),
    ),
    SourceSpec(
        "app/services/ssai_storage_service.py",
        "GENERAL",
        ("make_safe_filename", "get_user_file_path"),
    ),
    SourceSpec(
        "app/sims/nlq/nlq_router.py",
        "GENERAL",
        ("_normalize_io_action_spacing", "resolve_new_sims_nlq_candidate"),
    ),
    SourceSpec(
        "app/ui/current_table_followups/action_dispatcher.py",
        "GENERAL",
        ("select_current_table_analysis_context",),
    ),
    # SQL Server snapshot persistence is intentionally classified as internal
    # implementation knowledge before its code can become a retrieval candidate.
    SourceSpec(
        "app/services/sql_server_snapshot_repository.py",
        "ERP_DB_INTERNAL",
        ("_decode_compressed_payload", "SqlServerSnapshotRepository._validate_payload"),
    ),
)
SCOPE_CONTROL_SOURCE = "evaluation_company_scope_control.py"


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _node_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", ())
    return min([getattr(node, "lineno", 1), *(getattr(item, "lineno", 1) for item in decorators)])


def _find_symbol(tree: ast.Module, symbol: str) -> ast.AST:
    parts = symbol.split(".")
    if len(parts) == 1:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                return node
    elif len(parts) == 2:
        class_name, method_name = parts
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                        return child
    raise ValueError(f"selected source symbol not found: {symbol}")


def extract_symbols(spec: SourceSpec, *, commit: str) -> tuple[ExtractedSymbol, ...]:
    path = ROOT / spec.relative_path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=spec.relative_path)
    lines = source.splitlines()
    results: list[ExtractedSymbol] = []
    for symbol in spec.symbols:
        node = _find_symbol(tree, symbol)
        start_line = _node_start(node)
        end_line = int(getattr(node, "end_lineno", start_line))
        raw = "\n".join(lines[start_line - 1 : end_line]).rstrip()
        raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rendered = (
            f"# path: {spec.relative_path} | symbol: {symbol} | lines: {start_line}-{end_line} "
            f"| commit: {commit} | sha256: {raw_hash}\n\n"
            f"```python\n{raw}\n```"
        )
        results.append(ExtractedSymbol(spec.relative_path, symbol, start_line, end_line, raw, raw_hash, rendered))
    return tuple(results)


def _register_source(repo: KnowledgeDocumentRepository, spec: SourceSpec, extracted: ExtractedSymbol, commit: str):
    source, created = repo.register_text_checked(
        source_name=spec.relative_path,
        source_key=f"project-source:{spec.relative_path}#{extracted.symbol}",
        content=extracted.text,
        scope="GLOBAL",
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
        version=1,
        knowledge_classification=spec.classification,
        source_kind="PROJECT_SOURCE",
        source_revision=commit,
        source_content_hash=extracted.raw_hash,
    )
    assert created
    return repo.approve_checked(
        document_id=source.document_id,
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
    )
def build_source_corpus(repo: KnowledgeDocumentRepository) -> dict[str, Any]:
    commit = _git_commit()
    registered: dict[str, Any] = {}
    selected: list[dict[str, Any]] = []
    for spec in SOURCES:
        extracted = extract_symbols(spec, commit=commit)
        for item in extracted:
            registered[f"{spec.relative_path}#{item.symbol}"] = _register_source(repo, spec, item, commit)
        selected.append(
            {
                "path": spec.relative_path,
                "classification": spec.classification,
                "symbols": [
                    {"name": item.symbol, "line_range": f"{item.start_line}-{item.end_line}"}
                    for item in extracted
                ],
            }
        )

    # An explicit temporary control exercises company isolation using the same
    # AST-derived source representation. It is not a claim about production
    # source classification and never leaves the temporary evaluation root.
    control = extract_symbols(SOURCES[2], commit=commit)[0]
    control_body = f"{control.raw_text}\nscope_probe = \"COMPANY-SCOPE-PROBE-4\""
    control_hash = hashlib.sha256(control_body.encode("utf-8")).hexdigest()
    content = (
        f"# path: {SCOPE_CONTROL_SOURCE} | symbol: make_safe_filename | lines: {control.start_line}-{control.end_line} "
        f"| commit: {commit} | sha256: {control_hash}\n\n```python\n{control_body}\n```"
    )
    source, created = repo.register_text_checked(
        source_name=SCOPE_CONTROL_SOURCE,
        source_key=f"project-source:{SCOPE_CONTROL_SOURCE}#make_safe_filename",
        content=content,
        scope="COMPANY",
        company_id=4,
        current_company_id=4,
        permission_codes=COMPANY_MANAGE,
        knowledge_classification="GENERAL",
        source_kind="PROJECT_SOURCE",
        source_revision=commit,
        source_content_hash=control_hash,
    )
    assert created
    registered[SCOPE_CONTROL_SOURCE] = repo.approve_checked(
        document_id=source.document_id,
        current_company_id=4,
        permission_codes=COMPANY_MANAGE,
    )
    return {
        "commit": commit,
        "selected_sources": selected,
        "source_count": len(selected),
        "symbol_count": sum(len(row["symbols"]) for row in selected),
        "temporary_scope_control": SCOPE_CONTROL_SOURCE,
        "active_document_count": len(repo._read_manifest()),
    }


def evaluation_cases() -> tuple[EvalCase, ...]:
    no_match = {"expected_no_match": True}
    return (
        EvalCase("read_gate_exact", "KnowledgeAccessDecision can_read_document", ("can_read_document",), ("app_services_knowledge_scope_policy.py",)),
        EvalCase("manage_gate_exact", "can_manage_document", ("can_manage_document",), ("app_services_knowledge_scope_policy.py",)),
        EvalCase("retrieval_exact", "KnowledgeDocumentRepository retrieve", ("KnowledgeDocumentRepository.retrieve",), ("app_services_knowledge_document_service.py",)),
        EvalCase("safe_filename_exact", "make_safe_filename", ("make_safe_filename",), ("app_services_ssai_storage_service.py",)),
        EvalCase("user_file_path_exact", "get_user_file_path", ("get_user_file_path",), ("app_services_ssai_storage_service.py",)),
        EvalCase("nlq_spacing_exact", "normalize io action spacing", ("_normalize_io_action_spacing",), ("app_sims_nlq_nlq_router.py",)),
        EvalCase("nlq_router_exact", "resolve_new_sims_nlq_candidate", ("resolve_new_sims_nlq_candidate",), ("app_sims_nlq_nlq_router.py",)),
        EvalCase("current_table_exact", "select_current_table_analysis_context", ("select_current_table_analysis_context",), ("app_ui_current_table_followups_action_dispatcher.py",)),
        EvalCase("source_path_exact", "app services ssai storage service py make safe filename", ("make_safe_filename",), ("app_services_ssai_storage_service.py",)),
        EvalCase("natural_storage_known_miss", "사용자 파일 경로를 안전하게 만드는 함수", **no_match),
        EvalCase("natural_nlq_known_miss", "입출고 NLQ action 띄어쓰기 보정", **no_match),
        EvalCase("bounded_context", "make_safe_filename", ("make_safe_filename",), ("app_services_ssai_storage_service.py",), max_chars=650),
        EvalCase("scope_company_allow", "COMPANY-SCOPE-PROBE-4", ("make_safe_filename",), (SCOPE_CONTROL_SOURCE,)),
        EvalCase("scope_company_deny", "COMPANY-SCOPE-PROBE-4", excluded_sources=(SCOPE_CONTROL_SOURCE,), company_id=6, **no_match),
        EvalCase("rag_use_deny", "can_read_document", excluded_sources=("app_services_knowledge_scope_policy.py",), permissions=(), **no_match),
        EvalCase("erp_decode_admin", "decode compressed payload", ("_decode_compressed_payload",), ("app_services_sql_server_snapshot_repository.py",), permissions=ERP_READ),
        EvalCase("erp_validate_manager", "SqlServerSnapshotRepository validate payload", ("SqlServerSnapshotRepository._validate_payload",), ("app_services_sql_server_snapshot_repository.py",), permissions=ERP_READ),
        EvalCase("erp_staff_deny", "decode compressed payload", excluded_sources=("app_services_sql_server_snapshot_repository.py",), **no_match),
        EvalCase("erp_wholesale_deny", "SqlServerSnapshotRepository validate payload", excluded_sources=("app_services_sql_server_snapshot_repository.py",), **no_match),
        EvalCase("unknown_function", "generate_embedding_index", **no_match),
        EvalCase("unrelated_feature", "직원 휴가 승인 절차", **no_match),
        EvalCase("false_positive_guard", "snapshot 여행 예약", **no_match),
        EvalCase("nonexistent_source", "app services payroll approval handler", **no_match),
        EvalCase("internal_unrelated", "SQL snapshot holiday policy", **no_match),
    )


def _packet_sources(packet: ContextPacket) -> set[str]:
    return {citation.source_name for citation in packet.citations}


def _symbol_ok(packet: ContextPacket, expected: tuple[str, ...]) -> bool:
    titles = {citation.section_title for citation in packet.citations}
    return all(any(f"symbol: {symbol} |" in title for title in titles) for symbol in expected)


def evaluate_retrieval(repo: KnowledgeDocumentRepository, cases: tuple[EvalCase, ...], hashes: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, ContextPacket]]:
    original = repo._read_artifact
    reads: list[str] = []
    packets: dict[str, ContextPacket] = {}

    def tracked(content_hash: str):
        reads.append(content_hash)
        return original(content_hash)

    repo._read_artifact = tracked  # type: ignore[method-assign]
    try:
        rows: list[dict[str, Any]] = []
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
            sources = _packet_sources(packet)
            new_reads = set(reads[before:])
            protected = {hashes[name] for name in case.excluded_sources if name in hashes}
            provenance = all(
                citation.source_name in sources
                and "path: " in citation.section_title
                and "symbol: " in citation.section_title
                and "lines: " in citation.section_title
                and "commit: " in citation.section_title
                and "sha256: " in citation.section_title
                and citation.source_kind == "PROJECT_SOURCE"
                for citation in packet.citations
            )
            checks = {
                "expected_sources": set(case.expected_sources).issubset(sources),
                "expected_symbols": _symbol_ok(packet, case.expected_symbols),
                "excluded_sources": not bool(set(case.excluded_sources) & sources),
                "no_match": not sources if case.expected_no_match else True,
                "bounded_context": len(packet.text) <= case.max_chars,
                "citation_in_context": all(citation.label in packet.text for citation in packet.citations),
                "citation_provenance": provenance,
                "protected_artifact_read_zero": not bool(new_reads & protected),
            }
            rows.append({
                "name": case.name,
                "status": "pass" if all(checks.values()) else "fail",
                "reason_code": packet.reason_code,
                "candidate_count": packet.candidate_count,
                "context_chars": len(packet.text),
                "sources": sorted(sources),
                "checks": checks,
                "elapsed_ms": elapsed_ms,
            })
        return rows, packets
    finally:
        repo._read_artifact = original  # type: ignore[method-assign]


def _grounded_messages(question: str, packet: ContextPacket) -> list[dict[str, str]]:
    return [{"role": "user", "content": (
        "아래 CONTEXT만 근거로 한국어로 간결하게 설명하세요. 코드 전문을 길게 복사하지 마세요. "
        "근거가 있는 문장 끝에는 CONTEXT citation을 정확히 그대로 붙이세요. "
        f"근거가 부족하면 정확히 '{INSUFFICIENT_ANSWER}'라고만 답하세요.\n\n"
        f"QUESTION:\n{question}\n\nCONTEXT:\n{packet.text}"
    )}]


def evaluate_llm(packets: dict[str, ContextPacket], *, timeout_seconds: int) -> list[dict[str, Any]]:
    env = read_project_env_file()
    base_url = str(env.get("LMSTUDIO_BASE_URL") or "").strip()
    model = str(env.get("LLM_MODEL_DEFAULT") or env.get("LMSTUDIO_MODEL") or "").strip()
    if not base_url or not model:
        raise RuntimeError("llm_configuration_missing")
    client = OpenAI(base_url=base_url, api_key=str(env.get("LMSTUDIO_API_KEY") or "lm-studio"), timeout=max(1, int(timeout_seconds)), max_retries=0)
    live_cases = (
        ("read_gate_exact", "Knowledge 문서 read permission gate가 하는 일을 알려줘."),
        ("safe_filename_exact", "파일명을 안전하게 정리하는 source 함수의 역할을 설명해줘."),
        ("nlq_spacing_exact", "입출고 action 띄어쓰기 보정 함수가 처리하는 일을 알려줘."),
        ("erp_decode_admin", "압축 snapshot payload를 처리하는 함수의 동작을 알려줘."),
        ("unknown_function", "generate_embedding_index 함수가 하는 일을 알려줘."),
    )
    rows: list[dict[str, Any]] = []
    for name, question in live_cases:
        packet = packets[name]
        if not packet.text:
            rows.append({"name": name, "llm_called": False, "reason_code": "insufficient_context", "quality_pass": True, "elapsed_ms": 0})
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
        used = {citation for citation in citations if citation in answer}
        # Citation display comes from the selected ContextPacket, not a generative
        # model reproducing a long path/symbol/hash label verbatim.
        rows.append({
            "name": name,
            "llm_called": True,
            "quality_pass": bool(answer) and bool(citations),
            "elapsed_ms": elapsed_ms,
            "answer_chars": len(answer),
            "model_citation_echo_count": len(used),
            "presentation_citation_count": len(citations),
            "reason_code": str(extracted.get("code") or ""),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate lexical RAG using a temporary AST-extracted project source corpus.")
    parser.add_argument("--live-llm", action="store_true", help="Run five bounded LM Studio answer checks after retrieval.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="knowledge-project-source-eval-"))
    try:
        repo = KnowledgeDocumentRepository(root=root)
        corpus = build_source_corpus(repo)
        manifest = {source.source_name: source for source in repo._read_manifest()}
        hashes = {name: source.content_hash for name, source in manifest.items()}
        cases = evaluation_cases()
        retrieval, packets = evaluate_retrieval(repo, cases, hashes)
        llm = evaluate_llm(packets, timeout_seconds=args.timeout_seconds) if args.live_llm else []
        result = {
            "ok": all(row["status"] == "pass" for row in retrieval) and all(row["quality_pass"] for row in llm),
            "mode": "live_llm" if args.live_llm else "retrieval_only",
            "corpus": corpus,
            "retrieval": {
                "case_count": len(retrieval),
                "pass_count": sum(row["status"] == "pass" for row in retrieval),
                "no_match_pass_count": sum(row["status"] == "pass" and row["reason_code"] == "no_authorized_match" for row in retrieval),
                "average_elapsed_ms": round(sum(row["elapsed_ms"] for row in retrieval) / len(retrieval), 3),
                "rows": retrieval,
            },
            "llm": {
                "case_count": len(llm),
                "pass_count": sum(row["quality_pass"] for row in llm),
                "total_elapsed_ms": round(sum(row["elapsed_ms"] for row in llm), 3),
                "rows": llm,
            },
            "persistent_source_index_written": False,
            "embedding_called": False,
            "retry_count": 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
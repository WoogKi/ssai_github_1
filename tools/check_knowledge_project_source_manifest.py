"""Validate approved Project Source Knowledge manifest policy in a temporary repository only."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import ContextPacket, KnowledgeDocumentRepository  # noqa: E402
from check_knowledge_project_source_rag_poc import SOURCES, _git_commit, extract_symbols  # noqa: E402


GLOBAL_MANAGE = ("KNOWLEDGE_GLOBAL_MANAGE",)
RAG_USE = ("RAG_USE",)
PROJECT_SOURCE_READ = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ")
ERP_READ = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ", "KNOWLEDGE_ERP_DB_READ")
DOC_PATH = "docs/02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md"


@dataclass(frozen=True)
class Case:
    name: str
    query: str
    expected_kinds: tuple[str, ...] = ()
    expected_symbols: tuple[str, ...] = ()
    no_match: bool = False
    permissions: tuple[str, ...] = PROJECT_SOURCE_READ
    technical_detail_mode: bool = True


def _source_spec(relative_path: str):
    return next(spec for spec in SOURCES if spec.relative_path == relative_path)


def _symbol_content(relative_path: str, symbol: str, commit: str) -> str:
    spec = _source_spec(relative_path)
    selected = next(item for item in extract_symbols(spec, commit=commit) if item.symbol == symbol)
    return selected.text


def _source_hash(content: str) -> str:
    return content.split("| sha256: ", 1)[1].splitlines()[0].strip()


def _changed_source_content(relative_path: str, symbol: str, commit: str) -> str:
    body = _symbol_content(relative_path, symbol, commit).split("```python\n", 1)[1].rsplit("\n```", 1)[0]
    body = f"{body}\n# evaluation-only changed source version"
    raw_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (
        f"# path: {relative_path} | symbol: {symbol} | lines: 1-1 | commit: {commit} | sha256: {raw_hash}\n\n"
        f"```python\n{body}\n```"
    )

def _approve_source(
    repo: KnowledgeDocumentRepository,
    *,
    relative_path: str,
    symbol: str,
    commit: str,
    version: int,
    aliases: tuple[str, ...] = (),
    classification: str = "GENERAL",
    content: str | None = None,
):
    content = content or _symbol_content(relative_path, symbol, commit)
    source_key = f"project-source:{relative_path}#{symbol}"
    source, created = repo.register_text_checked(
        source_name=f"source_{hashlib.sha256(source_key.encode()).hexdigest()[:12]}.py",
        source_key=source_key,
        content=content,
        scope="GLOBAL",
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
        version=version,
        knowledge_classification=classification,
        source_kind="PROJECT_SOURCE",
        source_revision=commit,
        source_content_hash=_source_hash(content),
        search_aliases=aliases,
    )
    assert created
    return repo.approve_checked(document_id=source.document_id, current_company_id=4, permission_codes=GLOBAL_MANAGE)


def _approve_document(repo: KnowledgeDocumentRepository):
    source, created = repo.register_text_checked(
        source_name="official_current_table_contract.md",
        source_key=f"official-document:{DOC_PATH}",
        content=(ROOT / DOC_PATH).read_text(encoding="utf-8"),
        scope="GLOBAL",
        current_company_id=4,
        permission_codes=GLOBAL_MANAGE,
        version=1,
        source_kind="DOCUMENT",
    )
    assert created
    return repo.approve_checked(document_id=source.document_id, current_company_id=4, permission_codes=GLOBAL_MANAGE)


def _citation_symbols(packet: ContextPacket) -> set[str]:
    labels: set[str] = set()
    for citation in packet.citations:
        title = citation.section_title
        if "symbol: " in title:
            labels.add(title.split("symbol: ", 1)[1].split(" |", 1)[0])
    return labels



def build(repo: KnowledgeDocumentRepository) -> dict[str, Any]:
    commit = _git_commit()
    source_current_table = _approve_source(
        repo,
        relative_path="app/ui/current_table_followups/action_dispatcher.py",
        symbol="select_current_table_analysis_context",
        commit=commit,
        version=1,
    )
    source_storage = _approve_source(
        repo,
        relative_path="app/services/ssai_storage_service.py",
        symbol="get_user_file_path",
        commit=commit,
        version=1,
        aliases=("사용자 파일 경로를 안전하게 만드는 함수",),
    )
    source_nlq = _approve_source(
        repo,
        relative_path="app/sims/nlq/nlq_router.py",
        symbol="_normalize_io_action_spacing",
        commit=commit,
        version=1,
        aliases=("입출고 NLQ action 띄어쓰기 보정",),
    )
    source_erp = _approve_source(
        repo,
        relative_path="app/services/sql_server_snapshot_repository.py",
        symbol="_decode_compressed_payload",
        commit=commit,
        version=1,
        classification="ERP_DB_INTERNAL",
    )
    document = _approve_document(repo)

    # A commit/hash change cannot overwrite an approved source at the same
    # logical version. The next version must be separately approved.
    changed = _changed_source_content("app/services/ssai_storage_service.py", "get_user_file_path", "f" * 40)
    try:
        _approve_source(
            repo,
            relative_path="app/services/ssai_storage_service.py",
            symbol="get_user_file_path",
            commit="ffffffffffffffffffffffffffffffffffffffff",
            version=1,
            content=changed,
        )
    except ValueError as exc:
        same_version_rejected = "version already has different" in str(exc)
    else:
        same_version_rejected = False
    v2 = _approve_source(
        repo,
        relative_path="app/services/ssai_storage_service.py",
        symbol="get_user_file_path",
        commit="ffffffffffffffffffffffffffffffffffffffff",
        version=2,
        content=changed,
        aliases=("사용자 파일 경로를 안전하게 만드는 함수",),
    )
    v1 = next(item for item in repo._read_manifest() if item.document_id == source_storage.document_id)
    return {
        "commit": commit,
        "documents": {
            "current_table_source": source_current_table.document_id,
            "storage_source_v1": source_storage.document_id,
            "storage_source_v2": v2.document_id,
            "nlq_source": source_nlq.document_id,
            "erp_source": source_erp.document_id,
            "official_document": document.document_id,
        },
        "same_version_changed_content_rejected": same_version_rejected,
        "v1_superseded_after_v2_approval": v1.status == "SUPERSEDED",
        "stable_source_key": source_storage.source_key,
    }


def evaluate(repo: KnowledgeDocumentRepository) -> list[dict[str, Any]]:
    cases = (
        Case("approved_symbol", "select_current_table_analysis_context", ("PROJECT_SOURCE",), ("select_current_table_analysis_context",)),
        Case("unapproved_symbol", "get_storage_root", no_match=True),
        Case("korean_storage_alias", "사용자 파일 경로를 안전하게 만드는 함수", ("PROJECT_SOURCE",), ("get_user_file_path",)),
        Case("korean_nlq_alias", "입출고 NLQ action 띄어쓰기 보정", ("PROJECT_SOURCE",), ("_normalize_io_action_spacing",)),
        Case("exact_symbol_still_works", "get_user_file_path", ("PROJECT_SOURCE",), ("get_user_file_path",)),
        Case("source_document_simultaneous", "current table source action", ("DOCUMENT", "PROJECT_SOURCE")),
        Case("erp_allow", "decode compressed payload", ("PROJECT_SOURCE",), ("_decode_compressed_payload",), permissions=ERP_READ),
        Case("erp_deny", "decode compressed payload", no_match=True),
    )
    rows: list[dict[str, Any]] = []
    original = repo._read_artifact
    reads: list[str] = []

    def tracked(content_hash: str):
        reads.append(content_hash)
        return original(content_hash)

    repo._read_artifact = tracked  # type: ignore[method-assign]
    try:
        protected = next(item.content_hash for item in repo._read_manifest() if item.knowledge_classification == "ERP_DB_INTERNAL")
        for case in cases:
            before = len(reads)
            packet = repo.retrieve(
                query=case.query,
                current_user_id=11,
                current_company_id=4,
                permission_codes=case.permissions,
                technical_detail_mode=case.technical_detail_mode,
                max_chars=4500,
            )
            kinds = {citation.source_kind for citation in packet.citations}
            symbols = _citation_symbols(packet)
            new_reads = set(reads[before:])
            checks = {
                "expected_kinds": set(case.expected_kinds).issubset(kinds),
                "expected_symbols": set(case.expected_symbols).issubset(symbols),
                "no_match": not packet.citations if case.no_match else True,
                "bounded": len(packet.text) <= 4500,
                "citation_kind_present": all(citation.source_kind in {"DOCUMENT", "PROJECT_SOURCE"} for citation in packet.citations),
                "erp_deny_read_zero": protected not in new_reads if case.name == "erp_deny" else True,
            }
            rows.append({
                "name": case.name,
                "status": "pass" if all(checks.values()) else "fail",
                "reason_code": packet.reason_code,
                "candidate_count": packet.candidate_count,
                "kinds": sorted(kinds),
                "symbols": sorted(symbols),
                "checks": checks,
                "conflict_notice_count": len(packet.conflict_notices),
                "unconfirmed_simultaneous_has_no_notice": (
                    not packet.conflict_notices
                    if case.name == "source_document_simultaneous" else True
                ),
            })
    finally:
        repo._read_artifact = original  # type: ignore[method-assign]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Project Source Knowledge manifest/alias/conflict policy in a temporary repository.")
    parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="knowledge-source-manifest-"))
    try:
        repo = KnowledgeDocumentRepository(root=root)
        manifest = build(repo)
        rows = evaluate(repo)
        result = {
            "ok": manifest["same_version_changed_content_rejected"] and manifest["v1_superseded_after_v2_approval"] and all(row["status"] == "pass" for row in rows),
            "manifest": manifest,
            "evaluation": rows,
            "persistent_manifest_written": False,
            "embedding_called": False,
            "retry_count": 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
"""Fixture regression for the filesystem-backed Document Artifact / RAG PoC."""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (  # noqa: E402
    APPROVAL_APPROVED,
    KnowledgeEvidenceSnapshot,
    KnowledgeDocumentRepository,
    KnowledgeManagementDenied,
    _knowledge_query_terms,
    build_knowledge_chat_request_context,
    extract_text_artifact,
)
from app.services.knowledge_scope_policy import (  # noqa: E402
    KNOWLEDGE_ERP_DB_READ,
    KNOWLEDGE_PROJECT_SOURCE_READ,
)


def _repo() -> tuple[KnowledgeDocumentRepository, Path]:
    root = Path(tempfile.mkdtemp(prefix="knowledge-poc-"))
    return KnowledgeDocumentRepository(root=root), root


def _approved(repo: KnowledgeDocumentRepository, **kwargs):
    source, created = repo._register_text_trusted(**kwargs)
    assert created is True
    return repo._approve_trusted(document_id=source.document_id)


def test_python_source_representation_is_supported() -> None:
    artifact = extract_text_artifact(
        source_name="app_services_example.py",
        content="# path: app/services/example.py | symbol: sample | lines: 1-2 | commit: test | sha256: test\n\n```python\ndef sample():\n    return 1\n```",
    )
    assert artifact.sections and "symbol: sample" in artifact.sections[0]["title"]

def test_source_manifest_alias_and_reapproval_contract() -> None:
    repo, root = _repo()
    try:
        def source_artifact(revision: str, body: str) -> tuple[str, str]:
            raw_hash = __import__("hashlib").sha256(body.encode("utf-8")).hexdigest()
            return (
                f"# path: app/services/example.py | symbol: sample | lines: 1-2 | commit: {revision} | sha256: {raw_hash}\n\n```python\n{body}\n```",
                raw_hash,
            )

        initial_content, initial_hash = source_artifact("c" * 40, "def sample():\n    return 1")
        source = _approved(
            repo,
            source_name="project_source.py",
            source_key="project-source:app/services/example.py#sample",
            content=initial_content,
            scope="GLOBAL",
            source_kind="PROJECT_SOURCE",
            source_revision="c" * 40,
            source_content_hash=initial_hash,
            search_aliases=("사용자 파일 경로를 안전하게 만드는 함수",),
        )
        packet = repo.retrieve(
            query="사용자 파일 경로를 안전하게 만드는 함수",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ],
            technical_detail_mode=True,
        )
        assert packet.reason_code == "ready"
        assert packet.citations[0].source_kind == "PROJECT_SOURCE"
        assert "사용자 파일 경로" not in packet.text
        persisted = repo._read_manifest()[0]
        assert persisted.search_aliases == ("사용자 파일 경로를 안전하게 만드는 함수",)
        changed_content, changed_hash = source_artifact("c" * 40, "def sample():\n    return 9")
        try:
            repo._register_text_trusted(
                source_name="project_source.py",
                source_key=source.source_key,
                content=changed_content,
                scope="GLOBAL",
                version=1,
                source_kind="PROJECT_SOURCE",
                source_revision="c" * 40,
                source_content_hash=changed_hash,
            )
        except ValueError as exc:
            assert "version already has different" in str(exc)
        else:
            raise AssertionError("same source version content mismatch must fail closed")
        v2_content, v2_hash = source_artifact("d" * 40, "def sample():\n    return 2")
        v2, created = repo._register_text_trusted(
            source_name="project_source.py",
            source_key=source.source_key,
            content=v2_content,
            scope="GLOBAL",
            version=2,
            source_kind="PROJECT_SOURCE",
            source_revision="d" * 40,
            source_content_hash=v2_hash,
        )
        assert created
        approved_v2 = repo._approve_trusted(document_id=v2.document_id)
        assert approved_v2.version == 2
        older = next(item for item in repo._read_manifest() if item.document_id == source.document_id)
        assert older.status == "SUPERSEDED"
    finally:
        shutil.rmtree(root)
def test_scope_status_and_citation() -> None:
    repo, root = _repo()
    try:
        global_doc = _approved(repo, source_name="global.md", source_key="global", content="# 안내\n공통 운영 규칙", scope="GLOBAL")
        company_doc = _approved(repo, source_name="company.md", source_key="company", content="# 회사\n회사4 전용 규칙", scope="COMPANY", company_id=4)
        user_doc = _approved(repo, source_name="user.txt", source_key="user", content="개인 전용 절차", scope="USER", company_id=4, user_id=11)
        pending, _ = repo._register_text_trusted(source_name="pending.md", source_key="pending", content="승인 전 문서", scope="GLOBAL")
        assert pending.approval_status != APPROVAL_APPROVED

        packet = repo.retrieve(query="규칙", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        assert packet.reason_code == "ready" and global_doc.document_id in {c.document_id for c in packet.citations}
        assert all(" v" in citation.label and "§" in citation.label for citation in packet.citations)
        assert "승인 전" not in packet.text

        company = repo.retrieve(query="회사4", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        assert company.reason_code == "ready" and company_doc.document_id in {c.document_id for c in company.citations}
        cross_company = repo.retrieve(query="회사4", current_user_id=11, current_company_id=6, permission_codes=["RAG_USE"], max_chars=1000)
        assert cross_company.reason_code == "no_authorized_match"
        same_user = repo.retrieve(query="개인", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        assert user_doc.document_id in {c.document_id for c in same_user.citations}
        cross_user = repo.retrieve(query="개인", current_user_id=12, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        assert cross_user.reason_code == "no_authorized_match"
        denied = repo.retrieve(query="규칙", current_user_id=11, current_company_id=4, permission_codes=[], max_chars=1000)
        assert denied.reason_code == "no_authorized_match"
    finally:
        shutil.rmtree(root)


def test_hash_dedupe_version_and_bounds() -> None:
    repo, root = _repo()
    try:
        v1 = _approved(repo, source_name="policy.md", source_key="policy", content="# 규칙\n첫 번째 내용", scope="GLOBAL", version=1)
        same_version, created = repo._register_text_trusted(source_name="policy.md", source_key="policy", content="# 규칙\n첫 번째 내용", scope="GLOBAL", version=1)
        assert created is False and same_version.document_id == v1.document_id

        copy, created = repo._register_text_trusted(source_name="copy.md", source_key="copy", content="# 규칙\n첫 번째 내용", scope="GLOBAL", version=1)
        assert created is True and copy.document_id != v1.document_id
        assert copy.content_hash == v1.content_hash and len(list(repo.artifact_dir.glob("*.json"))) == 1
        copy = repo._approve_trusted(document_id=copy.document_id)
        provenance = repo.retrieve(query="첫 번째", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        citation_names = {citation.source_name for citation in provenance.citations}
        assert {"policy.md", "copy.md"}.issubset(citation_names)

        try:
            repo._register_text_trusted(source_name="policy.md", source_key="policy", content="# 규칙\n같은 버전의 다른 내용", scope="GLOBAL", version=1)
        except ValueError as exc:
            assert "version" in str(exc).lower()
        else:
            raise AssertionError("different content for an existing version must fail closed")
        v2 = _approved(repo, source_name="policy.md", source_key="policy", content="# 규칙\n두 번째 최신 내용", scope="GLOBAL", version=2)
        packet = repo.retrieve(query="두 번째", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=38)
        assert len(packet.text) <= 38 and packet.citations and packet.citations[0].document_id == v2.document_id
        assert v1.document_id not in {citation.document_id for citation in packet.citations}
        try:
            repo._approve_trusted(document_id=v1.document_id)
        except ValueError as exc:
            assert "downgrade" in str(exc).lower()
        else:
            raise AssertionError("re-approving an older version must fail closed")
    finally:
        shutil.rmtree(root)



def test_truncated_context_never_emits_unrendered_citation() -> None:
    repo, root = _repo()
    try:
        _approved(repo, source_name="short.md", source_key="short", content="# 규칙\n공통 규칙", scope="GLOBAL")
        packet = repo.retrieve(query="규칙", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1)
        assert packet.text == "" and packet.citations == () and packet.reason_code == "no_authorized_match"
    finally:
        shutil.rmtree(root)
def test_fail_closed_before_artifact_read() -> None:
    repo, root = _repo()
    try:
        source = _approved(repo, source_name="private.md", source_key="private", content="비공개 내용", scope="COMPANY", company_id=4)
        artifact_path = repo.artifact_dir / f"{source.content_hash}.json"
        artifact_path.write_text("not json", encoding="utf-8")
        packet = repo.retrieve(query="비공개", current_user_id=11, current_company_id=6, permission_codes=["RAG_USE"], max_chars=1000)
        assert packet.reason_code == "no_authorized_match"
        try:
            repo.retrieve(query="비공개", current_user_id=11, current_company_id=4, permission_codes=["RAG_USE"], max_chars=1000)
        except ValueError as exc:
            assert "artifact" in str(exc).lower()
        else:
            raise AssertionError("authorized corrupt artifact must fail closed")
    finally:
        shutil.rmtree(root)


def test_lexical_normalization_and_all_terms() -> None:
    repo, root = _repo()
    try:
        source = _approved(
            repo,
            source_name="cold-chain.md",
            source_key="cold-chain",
            content="# 냉장 배송\n냉장 배송 확인 코드는 COLD-4입니다.",
            scope="GLOBAL",
        )
        no_space = repo.retrieve(
            query="냉장배송",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE"],
        )
        assert {citation.document_id for citation in no_space.citations} == {source.document_id}
        mixed_code = repo.retrieve(
            query="COLD 4 확인",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE"],
        )
        assert {citation.document_id for citation in mixed_code.citations} == {source.document_id}
        partial_false_positive = repo.retrieve(
            query="배송 외계행성",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE"],
        )
        assert partial_false_positive.reason_code == "no_authorized_match"
    finally:
        shutil.rmtree(root)


def test_technical_natural_language_query_preserves_authorization_boundary() -> None:
    repo, root = _repo()
    try:
        document = _approved(
            repo,
            source_name="Rddbc120.txt",
            source_key="erp-rddbc120",
            content="# Rddbc120\n출고 입출고구분은 Rd12_Io_Gu 필드입니다.",
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
        )
        context = build_knowledge_chat_request_context(
            user_id=8,
            company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
        )
        allowed = repo.retrieve_for_chat(
            query="Rddbc120의 출고 입출고구분 필드는 무엇인가?",
            request_context=context,
        )
        assert allowed.reason_code == "ready"
        assert {citation.document_id for citation in allowed.citations} == {document.document_id}

        denied_context = build_knowledge_chat_request_context(
            user_id=8,
            company_id=4,
            permission_codes=["RAG_USE"],
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
        )
        denied = repo.retrieve_for_chat(
            query="Rddbc120의 출고 입출고구분 필드는 무엇인가?",
            request_context=denied_context,
        )
        assert denied.reason_code == "no_authorized_match"
        assert denied.citations == ()
    finally:
        shutil.rmtree(root)


def test_technical_detail_wrapper_normalization_is_narrow_and_fail_closed() -> None:
    repo, root = _repo()
    try:
        document = _approved(
            repo,
            source_name="Rddbc110.txt",
            source_key="erp-rddbc110",
            content="# Rddbc110\n입고 상세 테이블입니다. Rd11_Io_Gu 필드를 사용합니다.",
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
        )
        technical_context = build_knowledge_chat_request_context(
            user_id=8,
            company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
        )
        wrapped = repo.retrieve_for_chat(
            query="Rddbc110 관련 기술 내용을 알려줘",
            request_context=technical_context,
        )
        assert wrapped.reason_code == "ready"
        assert {citation.document_id for citation in wrapped.citations} == {document.document_id}

        direct = repo.retrieve_for_chat(query="Rddbc110", request_context=technical_context)
        assert direct.reason_code == "ready"

        terms, _ = _knowledge_query_terms(
            "Rddbc110 Rd11_Io_Gu 관련 기술 내용을 알려줘",
            technical_detail_mode=True,
        )
        assert terms == ("rddbc110", "rd11", "io", "gu")
        wrapper_only, _ = _knowledge_query_terms("기술", technical_detail_mode=True)
        assert wrapper_only == ("기술",)

        missing = repo.retrieve_for_chat(
            query="Rddbc999 관련 기술 내용을 알려줘",
            request_context=technical_context,
        )
        assert missing.reason_code == "no_authorized_match"

        general = repo.retrieve(
            query="Rddbc110 관련 기술 내용을 알려줘",
            current_user_id=8,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
            technical_detail_mode=False,
        )
        assert general.reason_code == "no_authorized_match"

        denied_context = build_knowledge_chat_request_context(
            user_id=8,
            company_id=4,
            permission_codes=["RAG_USE"],
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
        )
        denied = repo.retrieve_for_chat(query="Rddbc110", request_context=denied_context)
        assert denied.reason_code == "no_authorized_match" and denied.citations == ()
    finally:
        shutil.rmtree(root)


def test_citation_bound_table_layout_followup_is_scoped_and_fail_closed() -> None:
    root = Path(tempfile.mkdtemp(prefix="knowledge-followup-table-layout-"))
    repo = _ProbeRepository(root)
    try:
        table = _approved(
            repo,
            source_name="Rddbc110.txt",
            source_key="erp-rddbc110",
            content=(
                "CREATE TABLE [dbo].[Rddbc110](\n"
                "    [Rd11_In_YyMmDd] [char](8) NOT NULL,\n"
                "    [Rd11_Ven_Cd] [char](5) NOT NULL,\n"
                "    [Rd11_Io_Gu] [char](6) NOT NULL,\n"
                "    [Rd11_Quantity] [decimal](18, 0) NOT NULL,\n"
                "    [Rd11_Supply_Price] [decimal](18, 0) NOT NULL,\n"
                "    [Rd11_Tax_Price] [decimal](18, 0) NOT NULL\n"
                ")"
            ),
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
        )
        outside = _approved(
            repo,
            source_name="outside.txt",
            source_key="erp-outside",
            content="CREATE TABLE [dbo].[Rddbc999]([Outside_Field] [char](8) NOT NULL)",
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
        )
        context = build_knowledge_chat_request_context(
            user_id=8,
            company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
        )
        parent = repo.retrieve_for_chat(query="Rddbc110", request_context=context)
        assert parent.reason_code == "ready" and {item.document_id for item in parent.citations} == {table.document_id}
        snapshot = KnowledgeEvidenceSnapshot(
            user_id=8,
            company_id=4,
            room_owner_user_id=8,
            room_company_id=4,
            technical_detail_mode=True,
            answer_hash="fixture",
            citations=parent.citations,
            conflict_notices=(),
        )
        for query in (
            "필드명 알려줘",
            "컬럼 알려줘",
            "항목 알려줘",
            "입고구분 알려줘",
            "입고 관련 필드 알려줘",
            "수량 관련 필드 알려줘",
            "금액 관련 필드 알려줘",
            "수량과 금액 관련 필드만 알려줘",
            "날짜 필드 알려줘",
            "거래처 관련 필드 알려줘",
        ):
            packet = repo.retrieve_for_followup(
                query=query,
                parent_snapshot=snapshot,
                request_context=context,
            )
            assert packet.reason_code == "ready" and packet.citations
            assert {item.document_id for item in packet.citations} == {table.document_id}
            assert {item.version for item in packet.citations} == {table.version}
            assert len(packet.text) < 6000

        repo.reset_counts()
        unknown = repo.retrieve_for_followup(
            query="Rddbc999 필드명 알려줘",
            parent_snapshot=snapshot,
            request_context=context,
        )
        assert unknown.reason_code == "no_authorized_match" and unknown.citations == ()
        assert outside.content_hash not in repo.artifact_read_hashes

        for denied_context in (
            build_knowledge_chat_request_context(
                user_id=8, company_id=4, permission_codes=["RAG_USE"],
                room_owner_user_id=8, room_company_id=4, technical_detail_mode=True,
            ),
            build_knowledge_chat_request_context(
                user_id=8, company_id=6, permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
                room_owner_user_id=8, room_company_id=4, technical_detail_mode=True,
            ),
            build_knowledge_chat_request_context(
                user_id=9, company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
                room_owner_user_id=9, room_company_id=4, technical_detail_mode=True,
            ),
        ):
            repo.reset_counts()
            denied = repo.retrieve_for_followup(
                query="필드명 알려줘",
                parent_snapshot=snapshot,
                request_context=denied_context,
            )
            assert denied.reason_code != "ready" and repo.artifact_reads == 0
    finally:
        shutil.rmtree(root)


def test_knowledge_query_term_normalizer_preserves_ambiguous_word_endings() -> None:
    for term in ("효과", "허가", "경로"):
        normalized, _ = _knowledge_query_terms(term)
        assert normalized == (term,)

    normalized, _ = _knowledge_query_terms("Rddbc120의 필드는 무엇인가?")
    assert normalized == ("rddbc120",)


def test_checked_manage_scope_contract() -> None:
    repo, root = _repo()
    try:
        global_doc, created = repo.register_text_checked(
            source_name="global.md",
            source_key="global-checked",
            content="공통 지식",
            scope="GLOBAL",
            current_company_id=4,
            permission_codes=["KNOWLEDGE_GLOBAL_MANAGE"],
        )
        assert created is True
        approved = repo.approve_checked(
            document_id=global_doc.document_id,
            current_company_id=4,
            permission_codes=["KNOWLEDGE_GLOBAL_MANAGE"],
        )
        assert approved.approval_status == APPROVAL_APPROVED

        company_doc, created = repo.register_text_checked(
            source_name="company.md",
            source_key="company-checked",
            content="회사 지식",
            scope="COMPANY",
            company_id=4,
            current_company_id=4,
            permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
        )
        assert created is True
        try:
            repo.approve_checked(
                document_id=company_doc.document_id,
                current_company_id=6,
                permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
            )
        except KnowledgeManagementDenied as exc:
            assert exc.reason_code == "company_mismatch"
        else:
            raise AssertionError("cross-company approval must fail closed")
        approved = repo.approve_checked(
            document_id=company_doc.document_id,
            current_company_id=4,
            permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
        )
        assert approved.approval_status == APPROVAL_APPROVED
    finally:
        shutil.rmtree(root)


class _ExplodingContent:
    def __str__(self) -> str:
        raise AssertionError("denied content must not be decoded or normalized")


class _ProbeRepository(KnowledgeDocumentRepository):
    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.manifest_reads = 0
        self.manifest_writes = 0
        self.artifact_reads = 0
        self.artifact_writes = 0
        self.artifact_read_hashes: list[str] = []

    def reset_counts(self) -> None:
        self.manifest_reads = 0
        self.manifest_writes = 0
        self.artifact_reads = 0
        self.artifact_writes = 0
        self.artifact_read_hashes = []

    def _read_manifest(self):
        self.manifest_reads += 1
        return super()._read_manifest()

    def _write_manifest(self, documents):
        self.manifest_writes += 1
        return super()._write_manifest(documents)

    def _read_artifact(self, content_hash):
        self.artifact_reads += 1
        self.artifact_read_hashes.append(content_hash)
        return super()._read_artifact(content_hash)

    def _write_artifact_once(self, artifact):
        self.artifact_writes += 1
        return super()._write_artifact_once(artifact)


def _assert_manage_denied(call, reason: str) -> None:
    try:
        call()
    except KnowledgeManagementDenied as exc:
        assert exc.reason_code == reason
    else:
        raise AssertionError(f"management must be denied: {reason}")


def test_checked_deny_has_no_storage_side_effects() -> None:
    root = Path(tempfile.mkdtemp(prefix="knowledge-poc-gate-"))
    repo = _ProbeRepository(root)
    try:
        denied_registers = [
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="denied-global",
                    content=_ExplodingContent(),
                    scope="GLOBAL",
                    current_company_id=4,
                    permission_codes=[],
                ),
                "missing_global_manage",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="denied-company",
                    content=_ExplodingContent(),
                    scope="COMPANY",
                    company_id=4,
                    current_company_id=6,
                    permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
                ),
                "company_mismatch",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="denied-user",
                    content=_ExplodingContent(),
                    scope="USER",
                    company_id=4,
                    user_id=11,
                    current_company_id=4,
                    permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
                ),
                "user_scope_management_not_defined",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="malformed-global",
                    content=_ExplodingContent(),
                    scope="GLOBAL",
                    company_id="abc",
                    current_company_id=4,
                    permission_codes=["KNOWLEDGE_GLOBAL_MANAGE"],
                ),
                "global_scope_has_owner",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="malformed-company",
                    content=_ExplodingContent(),
                    scope="COMPANY",
                    company_id="abc",
                    current_company_id=4,
                    permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
                ),
                "company_scope_invalid_company",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="invalid-scope",
                    content=_ExplodingContent(),
                    scope="UNKNOWN",
                    current_company_id=4,
                    permission_codes=["KNOWLEDGE_GLOBAL_MANAGE"],
                ),
                "invalid_scope",
            ),
            (
                lambda: repo.register_text_checked(
                    source_name="denied.md",
                    source_key="malformed-classification",
                    content=_ExplodingContent(),
                    scope="GLOBAL",
                    knowledge_classification="SECRET",
                    current_company_id=4,
                    permission_codes=["KNOWLEDGE_GLOBAL_MANAGE"],
                ),
                "invalid_knowledge_classification",
            ),
        ]
        for call, reason in denied_registers:
            _assert_manage_denied(call, reason)
        assert (repo.manifest_reads, repo.manifest_writes, repo.artifact_reads, repo.artifact_writes) == (0, 0, 0, 0)

        pending, _ = repo._register_text_trusted(
            source_name="pending.md",
            source_key="pending-approval",
            content="승인 대기",
            scope="COMPANY",
            company_id=4,
        )
        user_pending, _ = repo._register_text_trusted(
            source_name="user.md",
            source_key="user-pending",
            content="개인 승인 대기",
            scope="USER",
            company_id=4,
            user_id=11,
        )
        global_pending, _ = repo._register_text_trusted(
            source_name="global.md",
            source_key="global-pending",
            content="전체 승인 대기",
            scope="GLOBAL",
        )
        repo.reset_counts()
        _assert_manage_denied(
            lambda: repo.approve_checked(
                document_id=global_pending.document_id,
                current_company_id=4,
                permission_codes=[],
            ),
            "missing_global_manage",
        )
        _assert_manage_denied(
            lambda: repo.approve_checked(
                document_id=pending.document_id,
                current_company_id=6,
                permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
            ),
            "company_mismatch",
        )
        _assert_manage_denied(
            lambda: repo.approve_checked(
                document_id=user_pending.document_id,
                current_company_id=4,
                permission_codes=["KNOWLEDGE_COMPANY_MANAGE"],
            ),
            "user_scope_management_not_defined",
        )
        assert repo.manifest_reads == 3
        assert (repo.manifest_writes, repo.artifact_reads, repo.artifact_writes) == (0, 0, 0)
    finally:
        shutil.rmtree(root)


def test_erp_db_read_gate_precedes_artifact_and_citation() -> None:
    root = Path(tempfile.mkdtemp(prefix="knowledge-poc-erp-read-"))
    repo = _ProbeRepository(root)
    try:
        erp = _approved(
            repo,
            source_name="internal-schema.md",
            source_key="internal-schema",
            content="# ERP DB schema\nRddbc999 내부 연결 키는 INTERNAL-KEY입니다.",
            scope="GLOBAL",
            knowledge_classification="ERP_DB_INTERNAL",
        )
        repo.reset_counts()
        for permissions in (["RAG_USE"], [], [KNOWLEDGE_ERP_DB_READ]):
            packet = repo.retrieve(
                query="INTERNAL-KEY",
                current_user_id=11,
                current_company_id=4,
                permission_codes=permissions,
            )
            assert packet.reason_code == "no_authorized_match"
            assert packet.candidate_count == 0 and packet.text == "" and packet.citations == ()
            assert erp.source_name not in packet.text
        assert repo.artifact_reads == 0

        allowed = repo.retrieve(
            query="INTERNAL-KEY",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_ERP_DB_READ],
            technical_detail_mode=True,
        )
        assert allowed.reason_code == "ready"
        assert {citation.document_id for citation in allowed.citations} == {erp.document_id}
        assert repo.artifact_reads == 1

        company_erp = _approved(
            repo,
            source_name="company-schema.md",
            source_key="company-schema",
            content="# 회사 ERP DB\n회사 전용 내부 코드는 COMPANY-INTERNAL입니다.",
            scope="COMPANY",
            company_id=4,
            knowledge_classification="ERP_DB_INTERNAL",
        )
        repo.reset_counts()
        cross_company = repo.retrieve(
            query="COMPANY-INTERNAL",
            current_user_id=11,
            current_company_id=6,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ, KNOWLEDGE_ERP_DB_READ],
            technical_detail_mode=True,
        )
        assert cross_company.reason_code == "no_authorized_match" and cross_company.citations == ()
        assert company_erp.content_hash not in repo.artifact_read_hashes

        documents = repo._read_manifest()
        malformed = [
            replace(source, knowledge_classification="SECRET")
            if source.document_id == company_erp.document_id
            else source
            for source in documents
        ]
        repo._write_manifest(malformed)
        repo.reset_counts()
        invalid = repo.retrieve(
            query="COMPANY-INTERNAL",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ, KNOWLEDGE_ERP_DB_READ],
            technical_detail_mode=True,
        )
        assert invalid.reason_code == "no_authorized_match" and invalid.citations == ()
        # The unrelated authorized GLOBAL document is read, but the malformed
        # company artifact itself must remain unread.
        assert repo.artifact_reads == 1
        assert company_erp.content_hash not in repo.artifact_read_hashes

        repo._write_manifest(documents)
        general = _approved(
            repo,
            source_name="general.md",
            source_key="general",
            content="# 일반 업무\n일반 업무 코드는 GENERAL-OK입니다.",
            scope="GLOBAL",
        )
        packet = repo.retrieve(
            query="GENERAL-OK",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE"],
        )
        assert {citation.document_id for citation in packet.citations} == {general.document_id}
    finally:
        shutil.rmtree(root)


def test_confirmed_source_document_conflict_notice_contract() -> None:
    repo, root = _repo()
    try:
        def project_source(*, key: str, term: str, group_id: str = "", confirmed: bool = False, classification: str = "GENERAL"):
            body = f"def sample():\n    return '{term}'"
            source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            revision = "e" * 40
            content = (
                f"# path: app/services/{key}.py | symbol: sample | lines: 1-2 | "
                f"commit: {revision} | sha256: {source_hash}\n\n```python\n{body}\n```"
            )
            return _approved(
                repo, source_name=f"{key}.py", source_key=f"project-source:app/services/{key}.py#sample",
                content=content, scope="GLOBAL", source_kind="PROJECT_SOURCE", source_revision=revision,
                source_content_hash=source_hash, knowledge_classification=classification,
                conflict_group_id=group_id, conflict_confirmed=confirmed,
            )

        plain_document = _approved(repo, source_name="plain.md", source_key="plain-document", content="# plain\nplain simultaneous evidence", scope="GLOBAL")
        plain_source = project_source(key="plain_source", term="plain simultaneous evidence")
        plain_packet = repo.retrieve(query="plain simultaneous evidence", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True)
        assert {citation.document_id for citation in plain_packet.citations} == {plain_document.document_id, plain_source.document_id}
        assert plain_packet.conflict_notices == ()

        confirmed_document = _approved(repo, source_name="confirmed.md", source_key="confirmed-document", content="# confirmed\nconfirmed conflict evidence", scope="GLOBAL", conflict_group_id="policy.current-state", conflict_confirmed=True)
        confirmed_source = project_source(key="confirmed_source", term="confirmed conflict evidence", group_id="policy.current-state", confirmed=True)
        confirmed_packet = repo.retrieve(query="confirmed conflict evidence", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True)
        assert len(confirmed_packet.conflict_notices) == 1
        notice = confirmed_packet.conflict_notices[0]
        assert notice.conflict_group_id == "policy.current-state"
        assert notice.document_citation_ids and notice.project_source_citation_ids
        assert {citation.document_id for citation in confirmed_packet.citations} == {confirmed_document.document_id, confirmed_source.document_id}

        one_sided_document = _approved(repo, source_name="one-sided.md", source_key="one-sided-document", content="# one\none sided conflict metadata", scope="GLOBAL", conflict_group_id="policy.one-sided", conflict_confirmed=True)
        one_sided_source = project_source(key="one_sided_source", term="one sided conflict metadata", group_id="policy.one-sided")
        one_sided_packet = repo.retrieve(query="one sided conflict metadata", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True)
        assert {citation.document_id for citation in one_sided_packet.citations} == {one_sided_document.document_id, one_sided_source.document_id}
        assert one_sided_packet.conflict_notices == ()

        document_only = _approved(
            repo, source_name="document-only.md", source_key="document-only",
            content="# only\ndocument only confirmed conflict metadata", scope="GLOBAL",
            conflict_group_id="policy.document-only", conflict_confirmed=True,
        )
        document_only_packet = repo.retrieve(
            query="document only confirmed conflict metadata", current_user_id=1,
            current_company_id=4, permission_codes=["RAG_USE"],
        )
        assert {citation.document_id for citation in document_only_packet.citations} == {document_only.document_id}
        assert document_only_packet.conflict_notices == ()

        mismatch_document = _approved(repo, source_name="mismatch.md", source_key="mismatch-document", content="# mismatch\ngroup mismatch evidence", scope="GLOBAL", conflict_group_id="policy.document", conflict_confirmed=True)
        mismatch_source = project_source(key="mismatch_source", term="group mismatch evidence", group_id="policy.source", confirmed=True)
        mismatch_packet = repo.retrieve(query="group mismatch evidence", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True)
        assert {citation.document_id for citation in mismatch_packet.citations} == {mismatch_document.document_id, mismatch_source.document_id}
        assert mismatch_packet.conflict_notices == ()

        protected_document = _approved(repo, source_name="protected.md", source_key="protected-document", content="# protected\nprotected conflict evidence", scope="GLOBAL", conflict_group_id="policy.erp", conflict_confirmed=True)
        protected_source = project_source(key="protected_source", term="protected conflict evidence", group_id="policy.erp", confirmed=True, classification="ERP_DB_INTERNAL")
        reads: list[str] = []
        original_read = repo._read_artifact
        repo._read_artifact = lambda content_hash: (reads.append(content_hash), original_read(content_hash))[1]  # type: ignore[method-assign]
        try:
            denied_packet = repo.retrieve(query="protected conflict evidence", current_user_id=1, current_company_id=4, permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ], technical_detail_mode=True)
        finally:
            repo._read_artifact = original_read  # type: ignore[method-assign]
        assert {citation.document_id for citation in denied_packet.citations} == {protected_document.document_id}
        assert denied_packet.conflict_notices == ()
        assert protected_source.content_hash not in reads

        try:
            repo._register_text_trusted(source_name="invalid.md", source_key="invalid-conflict", content="invalid conflict metadata", scope="GLOBAL", conflict_group_id="", conflict_confirmed=True)
        except ValueError as exc:
            assert "conflict_confirmed" in str(exc)
        else:
            raise AssertionError("malformed conflict metadata must fail closed")
    finally:
        shutil.rmtree(root)

def test_project_source_technical_detail_gate_precedes_artifact_read() -> None:
    root = Path(tempfile.mkdtemp(prefix="knowledge-poc-project-source-read-"))
    repo = _ProbeRepository(root)
    try:
        document = _approved(
            repo,
            source_name="official.md",
            source_key="official",
            content="# 운영 규칙\nTECHNICAL-GATE 정책의 공식 설명입니다.",
            scope="GLOBAL",
        )
        body = "def technical_gate():\n    return 'TECHNICAL-GATE'"
        source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        revision = "a" * 40
        source = _approved(
            repo,
            source_name="technical_gate.py",
            source_key="project-source:app/services/technical_gate.py#technical_gate",
            content=(
                f"# path: app/services/technical_gate.py | symbol: technical_gate | lines: 1-2 | "
                f"commit: {revision} | sha256: {source_hash}\n\n```python\n{body}\n```"
            ),
            scope="GLOBAL",
            source_kind="PROJECT_SOURCE",
            source_revision=revision,
            source_content_hash=source_hash,
        )
        repo.reset_counts()
        default_packet = repo.retrieve(
            query="TECHNICAL-GATE",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ],
        )
        assert {citation.document_id for citation in default_packet.citations} == {document.document_id}
        assert source.content_hash not in repo.artifact_read_hashes
        assert default_packet.conflict_notices == ()

        repo.reset_counts()
        staff_packet = repo.retrieve(
            query="TECHNICAL-GATE",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE"],
            technical_detail_mode=True,
        )
        assert {citation.document_id for citation in staff_packet.citations} == {document.document_id}
        assert source.content_hash not in repo.artifact_read_hashes

        repo.reset_counts()
        technical_packet = repo.retrieve(
            query="TECHNICAL-GATE",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ],
            technical_detail_mode=True,
        )
        assert {citation.document_id for citation in technical_packet.citations} == {
            document.document_id,
            source.document_id,
        }
        assert source.content_hash in repo.artifact_read_hashes

        erp_body = "def erp_gate():\n    return 'ERP-TECHNICAL-GATE'"
        erp_hash = hashlib.sha256(erp_body.encode("utf-8")).hexdigest()
        erp = _approved(
            repo,
            source_name="erp_gate.py",
            source_key="project-source:app/services/erp_gate.py#erp_gate",
            content=(
                f"# path: app/services/erp_gate.py | symbol: erp_gate | lines: 1-2 | "
                f"commit: {revision} | sha256: {erp_hash}\n\n```python\n{erp_body}\n```"
            ),
            scope="GLOBAL",
            source_kind="PROJECT_SOURCE",
            source_revision=revision,
            source_content_hash=erp_hash,
            knowledge_classification="ERP_DB_INTERNAL",
        )
        repo.reset_counts()
        missing_erp = repo.retrieve(
            query="ERP-TECHNICAL-GATE",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ],
            technical_detail_mode=True,
        )
        assert missing_erp.citations == () and erp.content_hash not in repo.artifact_read_hashes
        allowed_erp = repo.retrieve(
            query="ERP-TECHNICAL-GATE",
            current_user_id=11,
            current_company_id=4,
            permission_codes=["RAG_USE", KNOWLEDGE_PROJECT_SOURCE_READ, KNOWLEDGE_ERP_DB_READ],
            technical_detail_mode=True,
        )
        assert {citation.document_id for citation in allowed_erp.citations} == {erp.document_id}
    finally:
        shutil.rmtree(root)
def main() -> None:
    tests = [
        test_python_source_representation_is_supported,
        test_source_manifest_alias_and_reapproval_contract,
        test_scope_status_and_citation,
        test_hash_dedupe_version_and_bounds,
        test_truncated_context_never_emits_unrendered_citation,
        test_fail_closed_before_artifact_read,
        test_lexical_normalization_and_all_terms,
        test_technical_natural_language_query_preserves_authorization_boundary,
        test_technical_detail_wrapper_normalization_is_narrow_and_fail_closed,
        test_citation_bound_table_layout_followup_is_scoped_and_fail_closed,
        test_knowledge_query_term_normalizer_preserves_ambiguous_word_endings,
        test_checked_manage_scope_contract,
        test_checked_deny_has_no_storage_side_effects,
        test_erp_db_read_gate_precedes_artifact_and_citation,
        test_confirmed_source_document_conflict_notice_contract,
        test_project_source_technical_detail_gate_precedes_artifact_read,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"RESULT OK tests={len(tests)}")


if __name__ == "__main__":
    main()

from __future__ import annotations
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (
    ContextPacket,
    KnowledgeClassification,
    KnowledgeDocumentRepository,
    SOURCE_KIND_PROJECT_SOURCE,
    build_knowledge_chat_request_context,
)
from app.ui.knowledge_chat_evidence import (
    build_knowledge_answer_display,
    build_knowledge_answer_message,
    build_knowledge_followup_packet,
)

RAG = ("RAG_USE",)
TECH = ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ")
ERP_TECH = TECH + ("KNOWLEDGE_ERP_DB_READ",)


class Probe(KnowledgeDocumentRepository):
    def __init__(self, root, source_repo_root):
        super().__init__(root, source_repo_root=source_repo_root)
        self.reads = 0
        self.read_hashes = []

    def _read_artifact(self, content_hash):
        self.reads += 1
        self.read_hashes.append(content_hash)
        return super()._read_artifact(content_hash)

    def reset_reads(self):
        self.reads = 0
        self.read_hashes = []


def approve(repo, **kwargs):
    source, created = repo._register_text_trusted(**kwargs)
    assert created
    return repo._approve_trusted(document_id=source.document_id)


def source(symbol, phrase, commit):
    raw = f"def {symbol}():\n    return {phrase!r}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    fence = chr(96) * 3
    return (
        f"# path: app/services/{symbol}.py | symbol: {symbol} | lines: 1-2 | commit: {commit} | sha256: {digest}\n\n{fence}python\n{raw}\n{fence}",
        digest,
    )


def context(perms=RAG, company=4, user=11, room_user=11, room_company=None, detail=False):
    return build_knowledge_chat_request_context(
        user_id=user, company_id=company, permission_codes=perms,
        room_owner_user_id=room_user,
        room_company_id=company if room_company is None else room_company,
        technical_detail_mode=detail,
    )


def hidden(display):
    assert not display.visible
    assert display.content == ""
    assert display.citations == ()
    assert display.conflict_notices == ()


def main():
    root = Path(tempfile.mkdtemp(prefix="knowledge-chat-auth-"))
    try:
        source_repo = root / "source_repo"
        source_dir = source_repo / "app" / "services"
        source_dir.mkdir(parents=True)
        source_rows = {
            "technical_rule": "TECH-77",
            "erp_rule": "ERP-77",
            "conflict_source": "CONFLICT-77 source",
        }
        for symbol, phrase in source_rows.items():
            (source_dir / f"{symbol}.py").write_text(
                f"def {symbol}():\n    return {phrase!r}", encoding="utf-8"
            )
        for args in (("init",), ("add", "."), ("-c", "user.email=test@example.invalid", "-c", "user.name=Knowledge Test", "commit", "-m", "fixture")):
            subprocess.run(["git", *args], cwd=source_repo, capture_output=True, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_repo, capture_output=True, check=True, text=True
        ).stdout.strip()
        repo = Probe(root / "manifest", source_repo)
        approve(repo, source_name="official.md", source_key="official", scope="GLOBAL",
                content="# 공식\nOFFICIAL-77 공식 문서 답변\nFOLLOWUP-SHARED 허용된 후속 근거")
        outside = approve(
            repo,
            source_name="outside.md",
            source_key="outside",
            scope="GLOBAL",
            content="# 외부\nFOLLOWUP-SHARED FOLLOWUP-OUTSIDE 범위 밖 근거",
        )
        tech_text, tech_hash = source("technical_rule", "TECH-77", commit)
        approve(repo, source_name="technical_rule.py",
                source_key="project-source:app/services/technical_rule.py#technical_rule",
                scope="GLOBAL", source_kind=SOURCE_KIND_PROJECT_SOURCE,
                source_revision=commit, source_content_hash=tech_hash, content=tech_text)
        erp_text, erp_hash = source("erp_rule", "ERP-77", commit)
        approve(repo, source_name="erp_rule.py",
                source_key="project-source:app/services/erp_rule.py#erp_rule",
                scope="GLOBAL", source_kind=SOURCE_KIND_PROJECT_SOURCE,
                source_revision=commit, source_content_hash=erp_hash,
                knowledge_classification=KnowledgeClassification.ERP_DB_INTERNAL, content=erp_text)
        approve(repo, source_name="conflict.md", source_key="conflict-doc", scope="GLOBAL",
                content="# 충돌\nCONFLICT-77 문서", conflict_group_id="conflict-77",
                conflict_confirmed=True)
        conflict_text, conflict_hash = source("conflict_source", "CONFLICT-77 source", commit)
        approve(repo, source_name="conflict_source.py",
                source_key="project-source:app/services/conflict_source.py#conflict_source",
                scope="GLOBAL", source_kind=SOURCE_KIND_PROJECT_SOURCE,
                source_revision=commit, source_content_hash=conflict_hash, content=conflict_text,
                conflict_group_id="conflict-77", conflict_confirmed=True)

        for bad in (context(room_user=12), context(room_company=6), context(perms=())):
            repo.reset_reads()
            packet = repo.retrieve_for_chat(query="OFFICIAL-77", request_context=bad)
            assert not packet.text and packet.citations == () and repo.reads == 0

        regular = context()
        official = repo.retrieve_for_chat(query="OFFICIAL-77", request_context=regular)
        assert official.reason_code == "ready" and official.citations[0].source_kind == "DOCUMENT"
        repo.reset_reads()
        assert repo.retrieve_for_chat(query="TECH-77", request_context=regular).reason_code == "no_authorized_match"
        assert tech_hash not in repo.read_hashes
        assert repo.retrieve_for_chat(query="TECH-77", request_context=context(detail=True)).reason_code == "no_authorized_match"
        source_packet = repo.retrieve_for_chat(query="TECH-77", request_context=context(TECH, detail=True))
        assert source_packet.citations[0].source_kind == SOURCE_KIND_PROJECT_SOURCE
        source_message = build_knowledge_answer_message(
            repository=repo, answer="TECH-77 기술상세 답변", packet=source_packet,
            request_context=context(TECH, detail=True),
            message_id="knowledge-source", timestamp="2026-08-21T10:00:01+00:00")
        assert build_knowledge_answer_display(
            repository=repo, message=source_message, request_context=context(TECH)
        ).visible
        hidden(build_knowledge_answer_display(
            repository=repo, message=source_message, request_context=regular
        ))
        repo.reset_reads()
        assert repo.retrieve_for_chat(query="ERP-77", request_context=context(TECH, detail=True)).reason_code == "no_authorized_match"
        assert erp_hash not in repo.read_hashes
        erp_packet = repo.retrieve_for_chat(query="ERP-77", request_context=context(ERP_TECH, detail=True))
        assert erp_packet.reason_code == "ready"
        erp_message = build_knowledge_answer_message(
            repository=repo, answer="ERP-77 기술상세 답변", packet=erp_packet,
            request_context=context(ERP_TECH, detail=True),
            message_id="knowledge-erp", timestamp="2026-08-21T10:00:02+00:00")
        hidden(build_knowledge_answer_display(
            repository=repo, message=erp_message, request_context=context(TECH)
        ))

        for invalid_packet in (
            ContextPacket(text="", citations=(), reason_code="no_authorized_match", candidate_count=0),
            ContextPacket(text="", citations=official.citations, reason_code="ready", candidate_count=1),
            ContextPacket(text=official.text, citations=(), reason_code="ready", candidate_count=1),
        ):
            try:
                build_knowledge_answer_message(
                    repository=repo, answer="근거 없는 답변", packet=invalid_packet,
                    request_context=regular, message_id="invalid", timestamp="2026-08-21T10:00:00+00:00",
                )
            except ValueError:
                pass
            else:
                raise AssertionError("invalid ContextPacket was accepted")
        try:
            build_knowledge_answer_message(
                repository=repo, answer="권한 밖 source 답변", packet=source_packet,
                request_context=regular, message_id="denied-source", timestamp="2026-08-21T10:00:00+00:00",
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("unauthorized citation packet was accepted")

        message = build_knowledge_answer_message(
            repository=repo, answer="OFFICIAL-77 답변", packet=official, request_context=regular,
            message_id="knowledge-1", timestamp="2026-08-21T10:00:00+00:00")
        live = build_knowledge_answer_display(repository=repo, message=message, request_context=regular)
        history = build_knowledge_answer_display(repository=repo, message=message, request_context=regular)
        assert live == history and live.visible
        hidden(build_knowledge_answer_display(repository=repo, message=message,
                                               request_context=context(company=6)))
        hidden(build_knowledge_answer_display(repository=repo, message=message,
                                               request_context=context(perms=())))
        hidden(build_knowledge_answer_display(repository=repo, message=message,
                                               request_context=context(room_company=6)))

        followup = build_knowledge_followup_packet(
            repository=repo,
            parent_message=message,
            query="FOLLOWUP-SHARED",
            request_context=regular,
        )
        assert followup.reason_code == "ready"
        assert {citation.document_id for citation in followup.citations} == {
            official.citations[0].document_id
        }
        assert outside.document_id not in {citation.document_id for citation in followup.citations}
        repo.reset_reads()
        no_match = build_knowledge_followup_packet(
            repository=repo,
            parent_message=message,
            query="FOLLOWUP-OUTSIDE",
            request_context=regular,
        )
        assert no_match.reason_code == "no_authorized_match" and not no_match.citations
        assert outside.content_hash not in repo.read_hashes
        for denied_context in (
            context(company=6),
            context(user=12, room_user=12),
            context(room_company=6),
            context(perms=()),
        ):
            repo.reset_reads()
            denied = build_knowledge_followup_packet(
                repository=repo,
                parent_message=message,
                query="FOLLOWUP-SHARED",
                request_context=denied_context,
            )
            assert denied.reason_code != "ready" and repo.reads == 0
        citationless = dict(message)
        citationless["knowledge_evidence"] = dict(message["knowledge_evidence"])
        citationless["knowledge_evidence"]["citations"] = []
        hidden(build_knowledge_answer_display(
            repository=repo, message=citationless, request_context=regular
        ))
        repo.reset_reads()
        assert build_knowledge_answer_display(repository=repo, message=message,
                                              request_context=regular).visible
        assert repo.reads == 0
        source_live = build_knowledge_answer_display(
            repository=repo, message=source_message, request_context=context(TECH, detail=True)
        )
        assert source_live.visible
        source_followup = build_knowledge_followup_packet(
            repository=repo,
            parent_message=source_message,
            query="TECH-77",
            request_context=context(TECH, detail=True),
        )
        assert source_followup.reason_code == "ready"
        repo.reset_reads()
        denied_source_followup = build_knowledge_followup_packet(
            repository=repo,
            parent_message=source_message,
            query="TECH-77",
            request_context=regular,
        )
        assert denied_source_followup.reason_code != "ready" and repo.reads == 0
        (source_repo / "app" / "services" / "technical_rule.py").write_text(
            "def technical_rule():\n    return 'changed'", encoding="utf-8"
        )
        hidden(build_knowledge_answer_display(
            repository=repo, message=source_message, request_context=context(TECH, detail=True)
        ))
        repo.reset_reads()
        stale_followup = build_knowledge_followup_packet(
            repository=repo,
            parent_message=source_message,
            query="TECH-77",
            request_context=context(TECH, detail=True),
        )
        assert stale_followup.reason_code == "evidence_project_source_stale" and repo.reads == 0
        (source_repo / "app" / "services" / "technical_rule.py").write_text(
            "def technical_rule():\n    return 'TECH-77'", encoding="utf-8"
        )

        default_conflict = repo.retrieve_for_chat(query="CONFLICT-77", request_context=regular)
        assert len(default_conflict.citations) == 1 and not default_conflict.conflict_notices
        full_conflict = repo.retrieve_for_chat(query="CONFLICT-77", request_context=context(TECH, detail=True))
        assert len(full_conflict.citations) == 2 and len(full_conflict.conflict_notices) == 1
        conflict_message = build_knowledge_answer_message(
            repository=repo, answer="CONFLICT-77 충돌 답변", packet=full_conflict,
            request_context=context(TECH, detail=True),
            message_id="knowledge-conflict", timestamp="2026-08-21T10:00:03+00:00")
        conflict_display = build_knowledge_answer_display(
            repository=repo, message=conflict_message, request_context=context(TECH)
        )
        assert conflict_display.visible and len(conflict_display.conflict_notices) == 1
        (source_repo / "revision_marker.txt").write_text("next revision", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=source_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Knowledge Test", "commit", "-m", "head-change"],
            cwd=source_repo, capture_output=True, check=True,
        )
        hidden(build_knowledge_answer_display(
            repository=repo, message=source_message, request_context=context(TECH, detail=True)
        ))

        approve(
            repo,
            source_name="official-v2.md",
            source_key="official",
            scope="GLOBAL",
            version=2,
            content="# 공식 v2\nOFFICIAL-77 새 승인본",
        )
        repo.reset_reads()
        superseded = build_knowledge_followup_packet(
            repository=repo,
            parent_message=message,
            query="FOLLOWUP-SHARED",
            request_context=regular,
        )
        assert superseded.reason_code == "evidence_document_mismatch" and repo.reads == 0
        print("RESULT OK tests=43")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

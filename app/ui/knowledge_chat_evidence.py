"""Typed Knowledge evidence model shared by future live and history renderers.

This module intentionally has no Streamlit or LLM dependency.  Existing chat
and SIMS rendering remain unchanged until an explicit operating-chat gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from app.services.knowledge_document_service import (
    ContextCitation,
    ContextConflictNotice,
    ContextPacket,
    KnowledgeChatRequestContext,
    KnowledgeEvidenceSnapshot,
    KnowledgeDocumentRepository,
    authorize_knowledge_chat_request,
)


KNOWLEDGE_ANSWER_MESSAGE_TYPE = "knowledge_answer"


@dataclass(frozen=True)
class KnowledgeAnswerDisplay:
    visible: bool
    content: str = ""
    citations: tuple[ContextCitation, ...] = ()
    citation_labels: tuple[str, ...] = ()
    conflict_notices: tuple[ContextConflictNotice, ...] = ()
    reason_code: str = "knowledge_answer_hidden"


def _answer_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_knowledge_citation_labels(
    citations: tuple[ContextCitation, ...],
    *,
    display_limit: int | None = None,
) -> tuple[str, ...]:
    """Keep full evidence while optionally condensing user-facing provenance."""
    if display_limit is None:
        return tuple(citation.label for citation in citations)
    if (
        isinstance(display_limit, bool)
        or not isinstance(display_limit, int)
        or not 1 <= display_limit <= 3
    ):
        raise ValueError("Knowledge citation display limit is invalid")
    labels: list[str] = []
    seen: set[tuple[str, int]] = set()
    for citation in citations:
        identity = (citation.document_id, citation.version)
        if identity in seen:
            continue
        seen.add(identity)
        labels.append(f"{citation.source_name} v{citation.version}")
        if len(labels) >= display_limit:
            break
    return tuple(labels)


def build_knowledge_answer_message(
    *,
    repository: KnowledgeDocumentRepository,
    answer: str,
    packet: ContextPacket,
    request_context: KnowledgeChatRequestContext,
    message_id: str,
    timestamp: str,
    citation_display_limit: int | None = None,
) -> dict[str, Any]:
    """Build the only persisted shape for a future Knowledge chat answer."""
    decision = authorize_knowledge_chat_request(request_context)
    if not decision.allowed:
        raise PermissionError(f"Knowledge chat request denied: {decision.reason_code}")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Knowledge answer must be non-empty")
    if not isinstance(packet, ContextPacket):
        raise ValueError("Knowledge answer requires a ContextPacket")
    if packet.reason_code != "ready" or not packet.text or not packet.citations:
        raise ValueError("Knowledge answer requires a ready ContextPacket with evidence")
    snapshot = KnowledgeEvidenceSnapshot(
        user_id=request_context.user_id,
        company_id=request_context.company_id,
        room_owner_user_id=request_context.room_owner_user_id,
        room_company_id=request_context.room_company_id,
        technical_detail_mode=request_context.technical_detail_mode,
        answer_hash=_answer_hash(answer),
        citations=packet.citations,
        conflict_notices=packet.conflict_notices,
    )
    evidence = repository.authorize_evidence_snapshot(
        snapshot=snapshot,
        request_context=request_context,
    )
    if not evidence.allowed:
        raise PermissionError(f"Knowledge evidence denied: {evidence.reason_code}")
    meta = {
        "kind": KNOWLEDGE_ANSWER_MESSAGE_TYPE,
        "user_id": request_context.user_id,
        "company_id": request_context.company_id,
    }
    if citation_display_limit is not None:
        build_knowledge_citation_labels(packet.citations, display_limit=citation_display_limit)
        meta["knowledge_citation_display_limit"] = citation_display_limit
    return {
        "id": str(message_id or "").strip(),
        "role": "assistant",
        "type": KNOWLEDGE_ANSWER_MESSAGE_TYPE,
        "content": answer,
        "time": str(timestamp or "").strip(),
        "knowledge_evidence": snapshot.to_dict(),
        "meta": meta,
    }


def _snapshot_from_message(message: dict[str, Any]) -> KnowledgeEvidenceSnapshot:
    if not isinstance(message, dict) or message.get("type") != KNOWLEDGE_ANSWER_MESSAGE_TYPE:
        raise ValueError("invalid Knowledge answer message")
    payload = message.get("knowledge_evidence")
    if not isinstance(payload, dict):
        raise ValueError("missing Knowledge evidence snapshot")

    raw_citations = payload.get("citations")
    raw_notices = payload.get("conflict_notices")
    if not isinstance(raw_citations, (list, tuple)) or not isinstance(raw_notices, (list, tuple)):
        raise ValueError("invalid Knowledge evidence collections")
    citations: list[ContextCitation] = []
    for row in raw_citations:
        if not isinstance(row, dict):
            raise ValueError("invalid Knowledge citation")
        citations.append(ContextCitation(**row))
    notices: list[ContextConflictNotice] = []
    for row in raw_notices:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("document_citation_ids"), (list, tuple))
            or not isinstance(row.get("project_source_citation_ids"), (list, tuple))
        ):
            raise ValueError("invalid Knowledge conflict notice")
        notices.append(
            ContextConflictNotice(
                conflict_group_id=row["conflict_group_id"],
                document_citation_ids=tuple(row["document_citation_ids"]),
                project_source_citation_ids=tuple(row["project_source_citation_ids"]),
                message=row.get(
                    "message",
                    "공식 문서와 현재 구현 source의 근거가 다릅니다. 두 citation을 구분해 확인하세요.",
                ),
            )
        )
    if not isinstance(payload.get("technical_detail_mode"), bool):
        raise ValueError("invalid Knowledge technical detail mode")
    return KnowledgeEvidenceSnapshot(
        user_id=int(payload["user_id"]),
        company_id=int(payload["company_id"]),
        room_owner_user_id=int(payload["room_owner_user_id"]),
        room_company_id=int(payload["room_company_id"]),
        technical_detail_mode=payload["technical_detail_mode"],
        answer_hash=str(payload["answer_hash"]),
        citations=tuple(citations),
        conflict_notices=tuple(notices),
    )


def build_knowledge_answer_display(
    *,
    repository: KnowledgeDocumentRepository,
    message: dict[str, Any],
    request_context: KnowledgeChatRequestContext,
) -> KnowledgeAnswerDisplay:
    """Build one safe model for both live and history rendering.

    The helper never retrieves, calls an LLM, or reads an artifact. A denied or
    malformed historical message exposes neither its answer nor evidence.
    """
    try:
        snapshot = _snapshot_from_message(message)
    except (KeyError, TypeError, ValueError):
        return KnowledgeAnswerDisplay(False, reason_code="invalid_evidence_snapshot")
    content = message.get("content")
    if not isinstance(content, str) or _answer_hash(content) != snapshot.answer_hash:
        return KnowledgeAnswerDisplay(False, reason_code="answer_snapshot_mismatch")
    decision = repository.authorize_evidence_snapshot(
        snapshot=snapshot,
        request_context=request_context,
    )
    if not decision.allowed:
        return KnowledgeAnswerDisplay(False, reason_code=decision.reason_code)
    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    display_limit = meta.get("knowledge_citation_display_limit")
    try:
        citation_labels = build_knowledge_citation_labels(
            decision.citations,
            display_limit=display_limit,
        )
    except ValueError:
        return KnowledgeAnswerDisplay(False, reason_code="invalid_citation_display_limit")
    return KnowledgeAnswerDisplay(
        True,
        content=content,
        citations=decision.citations,
        citation_labels=citation_labels,
        conflict_notices=decision.conflict_notices,
        reason_code="ready",
    )


def build_knowledge_followup_packet(
    *,
    repository: KnowledgeDocumentRepository,
    parent_message: dict[str, Any],
    query: str,
    request_context: KnowledgeChatRequestContext,
    max_chars: int = 6000,
) -> ContextPacket:
    """Re-authorize one parent and retrieve only from its cited versions."""
    try:
        snapshot = _snapshot_from_message(parent_message)
    except (KeyError, TypeError, ValueError):
        return ContextPacket("", (), "invalid_evidence_snapshot", 0)
    content = parent_message.get("content")
    if not isinstance(content, str) or _answer_hash(content) != snapshot.answer_hash:
        return ContextPacket("", (), "answer_snapshot_mismatch", 0)
    return repository.retrieve_for_followup(
        query=query,
        parent_snapshot=snapshot,
        request_context=request_context,
        max_chars=max_chars,
    )

"""Thin, explicit Knowledge Chat routing helpers.

This module does not call an LLM or access Streamlit session state.  The main
chat owns lifecycle and persistence; the adapter only recognizes an explicit
request and builds a bounded, evidence-only prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.knowledge_document_service import ContextPacket


_DOCUMENT_PREFIX = "/knowledge"
_TECHNICAL_PREFIX = "/knowledge-tech"


@dataclass(frozen=True)
class KnowledgeChatRoute:
    query: str
    technical_detail_mode: bool


def parse_explicit_knowledge_request(value: object) -> KnowledgeChatRoute | None:
    """Route only explicit commands; ordinary Chat/NLQ input remains untouched."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    for prefix, technical_detail_mode in (
        (_TECHNICAL_PREFIX, True),
        (_DOCUMENT_PREFIX, False),
    ):
        if not lowered.startswith(prefix):
            continue
        if len(text) == len(prefix) or not text[len(prefix)].isspace():
            return None
        query = text[len(prefix):].strip()
        return KnowledgeChatRoute(query=query, technical_detail_mode=technical_detail_mode) if query else None
    return None


def build_knowledge_prompt(*, query: str, packet: ContextPacket) -> list[dict[str, str]]:
    """Keep the model bounded to the already-authorized ContextPacket."""
    if packet.reason_code != "ready" or not packet.text or not packet.citations:
        raise ValueError("Knowledge prompt requires ready evidence")
    return [
        {
            "role": "system",
            "content": (
                "승인된 Knowledge 근거만 사용해 한국어로 간결히 답하세요. "
                "근거에 없는 내용은 추측하지 말고 자료 부족이라고 답하세요. "
                "citation은 시스템이 별도로 표시하므로 본문에 임의 citation을 만들지 마세요."
            ),
        },
        {
            "role": "user",
            "content": f"질문:\n{query}\n\n승인된 Knowledge 근거:\n{packet.text}",
        },
    ]

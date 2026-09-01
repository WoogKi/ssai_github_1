"""Pure explicit-command and safe-message helpers for the MCP Resource PoC."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.mcp_adapter import McpResourceRequest, McpResourceResponse, MCP_DEFAULT_TIMEOUT_S


MCP_RESOURCE_PREFIX = "/mcp-resource"


@dataclass(frozen=True)
class McpChatRoute:
    resource_id: str
    query: str
    reason_code: str = "ready"

    @property
    def valid(self) -> bool:
        return self.reason_code == "ready"


def parse_explicit_mcp_resource_request(value: object) -> McpChatRoute | None:
    """Recognize only the dedicated command; ordinary chat remains untouched."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    if not lowered.startswith(MCP_RESOURCE_PREFIX):
        return None
    if len(text) > len(MCP_RESOURCE_PREFIX) and not text[len(MCP_RESOURCE_PREFIX)].isspace():
        return None
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        return McpChatRoute(resource_id=parts[1] if len(parts) == 2 else "", query="", reason_code="malformed_command")
    resource_id = parts[1].strip()
    query = parts[2].strip()
    if not resource_id or not query:
        return McpChatRoute(resource_id=resource_id, query=query, reason_code="malformed_command")
    return McpChatRoute(resource_id=resource_id, query=query)


def build_mcp_resource_request(route: McpChatRoute) -> McpResourceRequest | None:
    if not isinstance(route, McpChatRoute) or not route.valid:
        return None
    return McpResourceRequest(resource_id=route.resource_id, query=route.query, timeout_s=MCP_DEFAULT_TIMEOUT_S)


def build_mcp_chat_message(response: McpResourceResponse) -> dict[str, Any]:
    """Return safe, bounded chat data without Knowledge/Web evidence shapes."""
    safe = response.to_safe_dict()
    status = safe["status"]
    if status == "success":
        content = "\n\n".join((safe["title"], safe["text"], f"출처: {safe['source_uri']}"))
    else:
        messages = {
            "permission_denied": "MCP 외부 Resource를 읽을 권한이 없습니다.",
            "unsupported": "요청한 MCP Resource는 현재 PoC에서 지원하지 않습니다.",
            "timeout": "MCP 외부 Resource 응답 시간이 초과되었습니다.",
            "invalid_response": "MCP 외부 Resource 응답 형식이 안전하지 않습니다.",
            "external_error": "MCP 외부 Resource를 안전하게 읽지 못했습니다.",
        }
        content = messages.get(status, "MCP Resource 요청을 처리하지 못했습니다.")
    return {
        "type": "mcp_external_resource",
        "content": content,
        "meta": {
            "mcp_external_resource": True,
            "result_status": status,
            "reason_code": safe["reason_code"],
            "mcp_resource": {
                key: safe[key]
                for key in ("resource_id", "title", "source_uri", "retrieved_at")
                if safe[key]
            },
            "mcp_elapsed_ms": safe["elapsed_ms"],
            "mcp_retry_count": safe["retry_count"],
            "mcp_physical_call_count": safe["physical_call_count"],
            "mcp_transport_kind": safe["transport_kind"],
        },
    }


def mcp_poc_enabled(environ: Mapping[str, str] | None) -> bool:
    raw = str((environ or {}).get("SIMS_MCP_RESOURCE_POC") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}

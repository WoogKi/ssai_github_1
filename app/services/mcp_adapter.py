"""Read-only MCP Resource adapter for the explicit MCP PoC.

This module deliberately owns no Streamlit, LLM, DB, Knowledge, or Web state.
It validates an already-explicit request and calls an injected transport at
most once. Remote production transports remain intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote


MCP_EXTERNAL_RESOURCE_READ = "MCP_EXTERNAL_RESOURCE_READ"
OFFICIAL_RECALL_NOTICE_RESOURCE_ID = "official-recall-notice"
MCP_RESOURCE_ALLOWLIST = frozenset({OFFICIAL_RECALL_NOTICE_RESOURCE_ID})
MCP_DEFAULT_TIMEOUT_S = 10
MCP_MAX_TIMEOUT_S = 30
MCP_MAX_QUERY_CHARS = 240
MCP_MAX_TEXT_CHARS = 1200
MCP_MAX_TITLE_CHARS = 200
MCP_MAX_URI_CHARS = 500


@dataclass(frozen=True)
class McpResourceRequest:
    resource_id: str
    query: str
    timeout_s: int = MCP_DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class McpResourceResponse:
    status: str
    reason_code: str
    resource_id: str
    title: str = ""
    source_uri: str = ""
    retrieved_at: str = ""
    text: str = ""
    elapsed_ms: int = 0
    retry_count: int = 0
    physical_call_count: int = 0
    transport_kind: str = ""

    def to_safe_dict(self) -> dict[str, Any]:
        value = {
            "status": self.status,
            "reason_code": self.reason_code,
            "resource_id": self.resource_id,
            "title": self.title,
            "source_uri": self.source_uri,
            "retrieved_at": self.retrieved_at,
            "text": self.text,
            "elapsed_ms": self.elapsed_ms,
            "retry_count": self.retry_count,
            "physical_call_count": self.physical_call_count,
            "transport_kind": self.transport_kind,
        }
        validate_mcp_resource_response(value)
        return value


class McpResourceTransport(Protocol):
    kind: str

    def read_resource(self, request: McpResourceRequest) -> Mapping[str, Any]: ...


def _text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= limit else ""


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _failed(
    status: str,
    reason_code: str,
    *,
    resource_id: Any = "",
    started: float | None = None,
    physical_call_count: int = 0,
    transport_kind: str = "",
) -> McpResourceResponse:
    return McpResourceResponse(
        status=status,
        reason_code=reason_code,
        resource_id=_text(resource_id, limit=80),
        elapsed_ms=_elapsed_ms(started) if started is not None else 0,
        retry_count=0,
        physical_call_count=physical_call_count,
        transport_kind=_text(transport_kind, limit=20),
    )


def validate_mcp_resource_request(request: McpResourceRequest | None) -> None:
    if not isinstance(request, McpResourceRequest):
        raise ValueError("invalid_request")
    if request.resource_id not in MCP_RESOURCE_ALLOWLIST:
        raise ValueError("unsupported_resource")
    if not _text(request.query, limit=MCP_MAX_QUERY_CHARS):
        raise ValueError("invalid_query")
    if not isinstance(request.timeout_s, int) or not (1 <= request.timeout_s <= MCP_MAX_TIMEOUT_S):
        raise ValueError("invalid_timeout")


def validate_mcp_resource_response(value: Mapping[str, Any] | None) -> None:
    raw = value if isinstance(value, Mapping) else {}
    required = {
        "status",
        "reason_code",
        "resource_id",
        "title",
        "source_uri",
        "retrieved_at",
        "text",
        "elapsed_ms",
        "retry_count",
        "physical_call_count",
        "transport_kind",
    }
    if set(raw) != required:
        raise ValueError("invalid_response_shape")
    if raw.get("status") not in {
        "success",
        "permission_denied",
        "unsupported",
        "timeout",
        "invalid_response",
        "external_error",
    }:
        raise ValueError("invalid_response_status")
    for key, limit in (("reason_code", 80), ("resource_id", 80), ("title", MCP_MAX_TITLE_CHARS), ("source_uri", MCP_MAX_URI_CHARS), ("retrieved_at", 80), ("text", MCP_MAX_TEXT_CHARS), ("transport_kind", 20)):
        value_text = raw.get(key)
        if not isinstance(value_text, str) or len(value_text) > limit:
            raise ValueError("invalid_response_text")
    for key in ("elapsed_ms", "retry_count", "physical_call_count"):
        if not isinstance(raw.get(key), int) or raw[key] < 0:
            raise ValueError("invalid_response_number")
    if raw["retry_count"] != 0 or raw["physical_call_count"] > 1:
        raise ValueError("invalid_call_contract")
    if raw["status"] == "success":
        if raw["resource_id"] not in MCP_RESOURCE_ALLOWLIST:
            raise ValueError("invalid_response_resource")
        expected_scheme = {"mock": "mock://", "stdio": "local-mcp://"}.get(raw["transport_kind"])
        if not expected_scheme or not raw["title"] or not raw["source_uri"].startswith(expected_scheme) or not raw["retrieved_at"] or not raw["text"]:
            raise ValueError("incomplete_success_response")
    json.dumps(dict(raw), ensure_ascii=False, allow_nan=False)


def _validated_success(
    raw: Mapping[str, Any],
    *,
    request: McpResourceRequest,
    started: float,
    transport_kind: str,
) -> McpResourceResponse:
    resource_id = _text(raw.get("resource_id"), limit=80)
    response = McpResourceResponse(
        status="success",
        reason_code="ready",
        resource_id=resource_id,
        title=_text(raw.get("title"), limit=MCP_MAX_TITLE_CHARS),
        source_uri=_text(raw.get("source_uri"), limit=MCP_MAX_URI_CHARS),
        retrieved_at=_text(raw.get("retrieved_at"), limit=80),
        text=_text(raw.get("text"), limit=MCP_MAX_TEXT_CHARS),
        elapsed_ms=_elapsed_ms(started),
        retry_count=0,
        physical_call_count=1,
        transport_kind=transport_kind,
    )
    if response.resource_id != request.resource_id:
        raise ValueError("resource_identity_mismatch")
    response.to_safe_dict()
    return response


class DefaultMockRecallNoticeTransport:
    """Static fixture transport; it never opens STDIO, HTTP, or a remote session."""

    kind = "mock"

    def __init__(self) -> None:
        self.call_count = 0

    def read_resource(self, request: McpResourceRequest) -> Mapping[str, Any]:
        self.call_count += 1
        return {
            "resource_id": request.resource_id,
            "title": "Mock official recall notice",
            "source_uri": "mock://official-recall-notice/sample",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "text": "Mock transport only. This result is not an external official notice.",
        }


class LocalStdioMcpResourceTransport:
    """Official MCP SDK transport for the controlled local test server only."""

    kind = "stdio"
    _RESOURCE_TEMPLATE = "local-mcp://official-recall-notice/{query}"

    def __init__(
        self,
        *,
        server_script: str | Path,
        python_executable: str | Path | None = None,
        server_env: Mapping[str, str] | None = None,
    ) -> None:
        self.server_script = Path(server_script).resolve()
        self.python_executable = str(Path(python_executable or sys.executable).resolve())
        self.server_env = {str(key): str(value) for key, value in dict(server_env or {}).items()}
        self.call_count = 0

    async def _read_resource_async(self, request: McpResourceRequest) -> Mapping[str, Any]:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=self.python_executable,
            args=[str(self.server_script)],
            env=self.server_env or None,
            cwd=str(self.server_script.parent),
        )
        async with asyncio.timeout(request.timeout_s):
            async with Client(params, read_timeout_seconds=float(request.timeout_s)) as client:
                if client.server_capabilities is None or client.server_capabilities.resources is None:
                    raise ValueError("resource_capability_missing")
                discovery = await client.list_resource_templates(cache_mode="refresh")
                templates = {
                    str(getattr(item, "uri_template", ""))
                    for item in discovery.resource_templates
                }
                if self._RESOURCE_TEMPLATE not in templates:
                    raise ValueError("resource_not_discovered")
                uri = self._RESOURCE_TEMPLATE.format(query=quote(request.query, safe=""))
                result = await client.read_resource(uri, cache_mode="refresh")
                if len(result.contents) != 1:
                    raise ValueError("invalid_content_count")
                content = result.contents[0]
                text = getattr(content, "text", None)
                if not isinstance(text, str):
                    raise ValueError("invalid_content_type")
                decoded = json.loads(text)
                if not isinstance(decoded, Mapping):
                    raise ValueError("invalid_content_shape")
                return decoded

    def read_resource(self, request: McpResourceRequest) -> Mapping[str, Any]:
        if not self.server_script.is_file():
            raise FileNotFoundError("local_mcp_server_missing")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("running_event_loop_not_supported")
        self.call_count += 1
        return asyncio.run(self._read_resource_async(request))


class UnsupportedMcpResourceTransport:
    """Configuration sentinel that never starts a process."""

    kind = "unsupported"
    call_count = 0

    def read_resource(self, request: McpResourceRequest) -> Mapping[str, Any]:
        raise AssertionError("unsupported transport must not be called")


def default_local_mcp_server_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tools" / "mcp_servers" / "official_recall_notice_server.py"


def build_mcp_resource_transport(environ: Mapping[str, str] | None) -> McpResourceTransport:
    kind = str((environ or {}).get("SIMS_MCP_TRANSPORT") or "mock").strip().lower()
    if kind == "mock":
        return DefaultMockRecallNoticeTransport()
    if kind == "stdio":
        return LocalStdioMcpResourceTransport(server_script=default_local_mcp_server_path())
    return UnsupportedMcpResourceTransport()


def run_mcp_resource_poc(
    request: McpResourceRequest | None,
    *,
    feature_enabled: bool,
    permission_allowed: bool,
    transport: McpResourceTransport,
) -> McpResourceResponse:
    """Execute one allowed resource read, otherwise fail closed without a call."""
    resource_id = getattr(request, "resource_id", "")
    transport_kind = _text(getattr(transport, "kind", ""), limit=20)
    if not feature_enabled:
        return _failed("unsupported", "feature_disabled", resource_id=resource_id, transport_kind=transport_kind)
    if not permission_allowed:
        return _failed("permission_denied", "permission_denied", resource_id=resource_id, transport_kind=transport_kind)
    if transport_kind not in {"mock", "stdio"}:
        return _failed("unsupported", "unsupported_transport", resource_id=resource_id, transport_kind=transport_kind)
    try:
        validate_mcp_resource_request(request)
    except ValueError as exc:
        reason = str(exc)
        status = "unsupported" if reason == "unsupported_resource" else "invalid_response"
        return _failed(status, reason, resource_id=resource_id, transport_kind=transport_kind)

    assert request is not None
    started = time.perf_counter()
    try:
        raw = transport.read_resource(request)
    except TimeoutError:
        return _failed("timeout", "timeout", resource_id=request.resource_id, started=started, physical_call_count=1, transport_kind=transport_kind)
    except Exception:
        return _failed("external_error", "external_error", resource_id=request.resource_id, started=started, physical_call_count=1, transport_kind=transport_kind)
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("non_mapping_response")
        return _validated_success(raw, request=request, started=started, transport_kind=transport_kind)
    except (TypeError, ValueError):
        return _failed("invalid_response", "invalid_response", resource_id=request.resource_id, started=started, physical_call_count=1, transport_kind=transport_kind)

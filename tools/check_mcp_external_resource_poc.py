"""No-network regression checks for the mock-only MCP Resource PoC."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mcp_adapter import (  # noqa: E402
    MCP_EXTERNAL_RESOURCE_READ,
    McpResourceRequest,
    run_mcp_resource_poc,
)
from app.services.datetime_tool import resolve_datetime_question  # noqa: E402
from app.services.ssai_permission_policy import get_required_permission  # noqa: E402
from app.ui.mcp_chat_adapter import (  # noqa: E402
    build_mcp_chat_message,
    build_mcp_resource_request,
    mcp_poc_enabled,
    parse_explicit_mcp_resource_request,
)


class MockTransport:
    kind = "mock"

    def __init__(self, response: object | None = None, error: BaseException | None = None) -> None:
        self.calls = 0
        self.response = response
        self.error = error

    def read_resource(self, request: McpResourceRequest):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response if self.response is not None else {
            "resource_id": request.resource_id,
            "title": "Mock notice",
            "source_uri": "mock://official-recall-notice/test",
            "retrieved_at": "2026-09-01T00:00:00+00:00",
            "text": "bounded mock response",
        }


def _request() -> McpResourceRequest:
    return McpResourceRequest("official-recall-notice", "recall notice", timeout_s=10)


def _assert_no_fallback_payload(message: dict) -> None:
    encoded = json.dumps(message, ensure_ascii=False, allow_nan=False)
    meta = message["meta"]
    if any(key in meta for key in ("web_search", "knowledge_evidence", "table_key", "source_table_key", "download_table_key")):
        raise AssertionError("MCP result leaked into an existing result/evidence contract")
    if "recall notice" in encoded:
        raise AssertionError("raw MCP query was persisted")
    if "secret" in encoded.lower():
        raise AssertionError("secret-like mock value was persisted")


def check_command_parser() -> None:
    if mcp_poc_enabled({}) or not mcp_poc_enabled({"SIMS_MCP_RESOURCE_POC": "1"}):
        raise AssertionError("MCP feature flag default/enable contract changed")
    route = parse_explicit_mcp_resource_request("/mcp-resource official-recall-notice 안전 공지")
    if route is None or not route.valid or route.resource_id != "official-recall-notice":
        raise AssertionError("valid explicit MCP command was not parsed")
    if build_mcp_resource_request(route) is None:
        raise AssertionError("valid route did not create a request")
    malformed = parse_explicit_mcp_resource_request("/mcp-resource official-recall-notice")
    if malformed is None or malformed.valid:
        raise AssertionError("malformed explicit MCP command was not retained")
    if parse_explicit_mcp_resource_request("/mcp-resourcex official-recall-notice query") is not None:
        raise AssertionError("ordinary slash text was captured as MCP")


def check_adapter_contract() -> None:
    off = MockTransport()
    response = run_mcp_resource_poc(_request(), feature_enabled=False, permission_allowed=True, transport=off)
    if response.status != "unsupported" or response.reason_code != "feature_disabled" or off.calls != 0:
        raise AssertionError("feature OFF called transport")

    denied = MockTransport()
    response = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=False, transport=denied)
    if response.status != "permission_denied" or response.physical_call_count != 0 or denied.calls != 0:
        raise AssertionError("permission denial called transport")

    unsupported = MockTransport()
    response = run_mcp_resource_poc(McpResourceRequest("not-allowed", "query", 10), feature_enabled=True, permission_allowed=True, transport=unsupported)
    if response.status != "unsupported" or unsupported.calls != 0:
        raise AssertionError("unsupported resource called transport")

    malformed = MockTransport()
    response = run_mcp_resource_poc(None, feature_enabled=True, permission_allowed=True, transport=malformed)
    if response.status != "invalid_response" or malformed.calls != 0:
        raise AssertionError("malformed request called transport")

    allowed = MockTransport()
    response = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=allowed)
    if response.status != "success" or allowed.calls != 1 or response.physical_call_count != 1 or response.retry_count != 0:
        raise AssertionError("allowed request call contract changed")
    message = build_mcp_chat_message(response)
    _assert_no_fallback_payload(message)
    if message["meta"]["mcp_physical_call_count"] != 1:
        raise AssertionError("physical call count was not retained in MCP-only metadata")

    timeout = MockTransport(error=TimeoutError())
    response = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=timeout)
    if response.status != "timeout" or timeout.calls != 1 or response.retry_count != 0:
        raise AssertionError("timeout contract changed")
    _assert_no_fallback_payload(build_mcp_chat_message(response))

    invalid = MockTransport(response={"resource_id": "official-recall-notice", "title": "x", "source_uri": "mock://x", "retrieved_at": "now", "text": "x" * 1201, "secret": "never-save"})
    response = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=invalid)
    if response.status != "invalid_response" or invalid.calls != 1 or response.retry_count != 0:
        raise AssertionError("invalid response contract changed")
    _assert_no_fallback_payload(build_mcp_chat_message(response))


def check_runtime_boundary() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    run_start = source.index("def _run_explicit_mcp_resource_chat(")
    run_end = source.index("\ndef _run_web_search_chat(", run_start)
    block = source[run_start:run_end]
    if any(token in block for token in ("search_web(", "retrieve_for_chat(", "try_handle_nlq(", "stream_and_append_assistant(")):
        raise AssertionError("MCP execution block reached an existing route owner")
    if "build_mcp_resource_transport(os.environ)" not in block or "run_mcp_resource_poc(" not in block:
        raise AssertionError("MCP runtime block does not use the explicit transport selector")
    queue = source.index("if raw_mcp_route is not None:")
    web = source.index("if web_search_route is not None:", queue)
    if queue >= web:
        raise AssertionError("MCP command was not isolated before Web routing")
    if "require_permission(MCP_EXTERNAL_RESOURCE_READ, show_error=False)" not in block:
        raise AssertionError("MCP permission is not checked at execution boundary")

    collision = "/mcp-resource official-recall-notice 현재 시간"
    route = parse_explicit_mcp_resource_request(collision)
    if route is None or not route.valid:
        raise AssertionError("datetime collision fixture is not a valid MCP command")
    if resolve_datetime_question(collision) is None:
        raise AssertionError("datetime collision fixture no longer exercises precedence")
    datetime_guard = (
        "None if raw_mcp_route is not None or sims_help_route is not None "
        "else resolve_datetime_question(user_input)"
    )
    if datetime_guard not in source:
        raise AssertionError("explicit MCP command is not protected from datetime routing")
    if resolve_datetime_question("현재 시간") is None:
        raise AssertionError("ordinary datetime routing contract changed")


def main() -> int:
    if MCP_EXTERNAL_RESOURCE_READ in {"RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ", "KNOWLEDGE_ERP_DB_READ"}:
        raise AssertionError("MCP reused a Knowledge permission")
    if get_required_permission(special="mcp_external_resource") != MCP_EXTERNAL_RESOURCE_READ:
        raise AssertionError("MCP special permission mapping is missing")
    check_command_parser()
    check_adapter_contract()
    check_runtime_boundary()
    print("MCP_EXTERNAL_RESOURCE_POC_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

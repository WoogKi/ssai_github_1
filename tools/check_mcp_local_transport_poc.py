"""Focused local-STDIO checks for the official MCP SDK Phase 3 PoC."""

from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SERVER_PATH = ROOT / "tools" / "mcp_servers" / "official_recall_notice_server.py"

import mcp.client.stdio as mcp_stdio  # noqa: E402

from app.services.mcp_adapter import (  # noqa: E402
    LocalStdioMcpResourceTransport,
    McpResourceRequest,
    build_mcp_resource_transport,
    default_local_mcp_server_path,
    run_mcp_resource_poc,
)
from app.ui.mcp_chat_adapter import build_mcp_chat_message  # noqa: E402


def _request(timeout_s: int = 10) -> McpResourceRequest:
    return McpResourceRequest("official-recall-notice", "sample recall", timeout_s)


def _process_exists(pid: int) -> bool:
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _wait_for_process_exit(pid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


def _transport(*, server_script: Path | None = None, server_env: dict[str, str] | None = None):
    return LocalStdioMcpResourceTransport(
        server_script=server_script or default_local_mcp_server_path(),
        python_executable=sys.executable,
        server_env=server_env,
    )


def check_no_call_boundaries() -> None:
    off = _transport()
    result = run_mcp_resource_poc(_request(), feature_enabled=False, permission_allowed=True, transport=off)
    if result.reason_code != "feature_disabled" or off.call_count != 0:
        raise AssertionError("feature OFF started the STDIO transport")
    denied = _transport()
    result = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=False, transport=denied)
    if result.status != "permission_denied" or denied.call_count != 0:
        raise AssertionError("permission denial started the STDIO transport")
    unsupported = build_mcp_resource_transport({"SIMS_MCP_TRANSPORT": "remote"})
    result = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=unsupported)
    if result.reason_code != "unsupported_transport" or unsupported.call_count != 0:
        raise AssertionError("unsupported transport was called")


def check_stdio_success() -> None:
    transport = _transport()
    result = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=transport)
    if result.status != "success" or result.transport_kind != "stdio":
        raise AssertionError(f"local STDIO read failed: {result.status}/{result.reason_code}")
    if transport.call_count != 1 or result.physical_call_count != 1 or result.retry_count != 0:
        raise AssertionError("local STDIO call-count contract changed")
    safe = build_mcp_chat_message(result)
    encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    if "sample recall" in encoded or "table_key" in encoded or "source_call_count" in encoded:
        raise AssertionError("MCP persistence leaked query or table semantics")


def check_unavailable_and_invalid() -> None:
    missing = _transport(server_script=ROOT / "tools" / "mcp_servers" / "missing_server.py")
    result = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=missing)
    if result.status != "external_error" or result.retry_count != 0 or missing.call_count != 0:
        raise AssertionError("missing server contract changed")

    invalid = _transport(server_env={"SIMS_MCP_TEST_INVALID_RESPONSE": "1"})
    result = run_mcp_resource_poc(_request(), feature_enabled=True, permission_allowed=True, transport=invalid)
    if result.status != "invalid_response" or invalid.call_count != 1 or result.retry_count != 0:
        raise AssertionError("invalid server response contract changed")


def check_timeout_cleanup() -> None:
    captured_pids: list[int] = []
    original_create_process = mcp_stdio._create_platform_compatible_process

    async def _capture_process(*args, **kwargs):
        process = await original_create_process(*args, **kwargs)
        captured_pids.append(int(process.pid))
        return process

    mcp_stdio._create_platform_compatible_process = _capture_process
    try:
        transport = _transport(
            server_env={"SIMS_MCP_TEST_DELAY_S": "3"}
        )
        result = run_mcp_resource_poc(_request(timeout_s=1), feature_enabled=True, permission_allowed=True, transport=transport)
    finally:
        mcp_stdio._create_platform_compatible_process = original_create_process

    if result.status != "timeout" or transport.call_count != 1 or result.retry_count != 0:
        raise AssertionError("STDIO timeout contract changed")
    if len(captured_pids) != 1:
        raise AssertionError("timeout probe did not observe exactly one MCP child process")
    pid = captured_pids[0]
    if not _wait_for_process_exit(pid):
        raise AssertionError(f"MCP child process leaked: pid={pid}")


def check_server_read_only_surface() -> None:
    source = SERVER_PATH.read_text(encoding="utf-8")
    forbidden = (
        "SIMS_MCP_TEST_PID_FILE",
        ".write_text(",
        ".write_bytes(",
        "open(",
        "Path(",
    )
    leaked = [marker for marker in forbidden if marker in source]
    if leaked:
        raise AssertionError(f"local MCP server exposes filesystem writes: {leaked}")


def main() -> int:
    check_server_read_only_surface()
    check_no_call_boundaries()
    check_stdio_success()
    check_unavailable_and_invalid()
    check_timeout_cleanup()
    print("MCP_LOCAL_STDIO_POC_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

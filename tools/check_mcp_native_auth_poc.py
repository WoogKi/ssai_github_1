"""Exercise the isolated MCP HTTP server with synthetic signed company contexts."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.mcp_servers.native_auth_poc_server import (  # noqa: E402
    ACTIVE_CONTEXT,
    AUTH_STATS,
    EXECUTIONS,
    HTTP_STATS,
    PERMISSION,
    SERVICE,
    fixture_token,
    make_app,
)
from app.services.ssai_permission_policy import get_required_permission  # noqa: E402


def _claims(company_id: int = 2, user_id: str = "user-2", tools: list[str] | None = None):
    return {
        "company_id": company_id,
        "user_id": user_id,
        "service": SERVICE,
        "permission": PERMISSION,
        "allowed_tools": ["notice_read"] if tools is None else tools,
        "session_epoch": 1,
        "exp": int(time.time()) + 60,
        "nonce": secrets.token_hex(16),
    }


async def _call(url: str, token: str, tool: str, company_id: int, service: str = SERVICE):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {item.name for item in tools.tools} == {"notice_read", "admin_read"}
                return await session.call_tool(tool, {"company_id": company_id, "service": service})


async def _checks(url: str, secret: bytes) -> dict[str, str]:
    results: dict[str, str] = {}

    def expect_denied(name: str, result, before: dict[str, int]):
        assert result.is_error, name
        assert EXECUTIONS == before, f"{name}: tool body executed"
        results[name] = "PASS"

    good = fixture_token(secret, _claims())
    result = await _call(url, good, "notice_read", 2)
    assert not result.is_error and '"company_id": 2' in str(result.content)
    assert EXECUTIONS["notice_read"] == 1
    assert HTTP_STATS["requests"] > 0 and AUTH_STATS["accepted"] > 0
    results["authorized_company_user_service"] = "PASS"

    before = EXECUTIONS.copy()
    expect_denied("token_replay", await _call(url, good, "notice_read", 2), before)
    good = fixture_token(secret, _claims())
    expect_denied("company_argument_tampering", await _call(url, good, "notice_read", 3), before)
    ACTIVE_CONTEXT["user-2"] = (3, 2)
    try:
        expect_denied("company_switch_stale_token", await _call(url, good, "notice_read", 2), before)
    finally:
        ACTIVE_CONTEXT["user-2"] = (2, 1)
    expect_denied("unallowed_tool", await _call(url, good, "admin_read", 2), before)
    expect_denied("unallowed_service", await _call(url, good, "notice_read", 2, "other"), before)
    expect_denied("unallowed_user", await _call(url, fixture_token(secret, _claims(user_id="intruder")), "notice_read", 2), before)
    expect_denied("unallowed_company", await _call(url, fixture_token(secret, _claims(company_id=4)), "notice_read", 4), before)
    expect_denied("forged_company_identifier", await _call(url, fixture_token(secret, _claims(company_id=3)), "notice_read", 2), before)
    no_permission = _claims()
    no_permission["permission"] = "OTHER"
    expect_denied("missing_permission", await _call(url, fixture_token(secret, no_permission), "notice_read", 2), before)

    result = await _call(url, good, "notice_read", 2)
    assert not result.is_error and EXECUTIONS["notice_read"] == 2
    results["fresh_token_after_denials"] = "PASS"

    result = await _call(url, fixture_token(secret, _claims(3, "user-3")), "notice_read", 3)
    assert not result.is_error and EXECUTIONS["notice_read"] == 3
    results["shared_physical_db_distinct_company_id"] = "PASS"

    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
    for name, bearer in (("tampered_token", tampered), ("missing_token", ""),
                         ("expired_token", fixture_token(secret, {**_claims(), "exp": int(time.time()) - 1})),
                         ("malformed_claims", fixture_token(secret, {**_claims(), "user_id": []})),
                         ("string_tool_allowlist", fixture_token(secret, {**_claims(), "allowed_tools": "notice_read"}))):
        before = EXECUTIONS.copy()
        headers = {"Accept": "application/json, text/event-stream"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            response = await http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401, f"{name}: expected authentication rejection"
        assert EXECUTIONS == before, name
        results[name] = "PASS"
    results["denial_tool_execution_count"] = "0"
    return results


def _powershell_checks() -> dict[str, str]:
    if sys.platform != "win32":
        return {"powershell_51": "NOT_RUN_NON_WINDOWS"}
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    wrapper = ROOT / "tools" / "run_mcp_native_2ho_verify.ps1"
    script = r'''
param($Wrapper, $PythonExe, $Stub)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'Expected PowerShell 5.1' }
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Wrapper, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Wrapper parse failed' }
$tryBlock = $ast.Find({param($node) $node -is [Management.Automation.Language.TryStatementAst]}, $true)
$invoke = $tryBlock.Body.Statements | Where-Object { $_.Extent.Text -eq '& $PythonExe @arguments' }
if (@($invoke).Count -ne 1) { throw 'Invocation contract changed' }
$start = $tryBlock.Body.Statements[0].Extent.StartOffset
$stop = $invoke.Extent.EndOffset
$source = [IO.File]::ReadAllText($Wrapper)
$block = [scriptblock]::Create($source.Substring($start, $stop - $start))
$checker = $Stub; $ApiBase = 'http://127.0.0.1:1234'; $ModelKey = 'synthetic model with spaces'
foreach ($Mode in @('Plan', 'Preflight', 'Run')) {
    $actual = (& $block) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Argument probe failed' }
    $expected = @($Mode.ToLowerInvariant())
    if ($Mode -ne 'Plan') { $expected += @('--api-base', $ApiBase, '--model-key', $ModelKey) }
    if (@($actual).Count -ne $expected.Count) { throw 'argv count differs' }
    for ($i = 0; $i -lt $expected.Count; $i++) {
        if (@($actual)[$i] -cne $expected[$i]) { throw 'argv item differs' }
    }
}
$prior = $env:PYTHONDONTWRITEBYTECODE
& $Wrapper -Mode Plan -PythonExe $PythonExe | Out-Null
if ($env:PYTHONDONTWRITEBYTECODE -cne $prior) { throw 'Environment was not restored' }
$blocked = $false
try { & $Wrapper -Mode Plan -PythonExe 'venv\Scripts\python.exe' } catch { $blocked = $true }
if (-not $blocked) { throw 'Relative Python path accepted' }
$blocked = $false
try { & $Wrapper -Mode Preflight -PythonExe $PythonExe -ModelKey 'fixture' } catch { $blocked = $true }
if (-not $blocked) { throw 'Preflight escaped diagnostics path gate' }
Write-Output 'POWERSHELL_51_PLAN_ARGV_PATH_ENV_PASS'
'''
    with tempfile.TemporaryDirectory(prefix="mcp argv ") as tmp:
        folder = Path(tmp)
        probe = folder / "argv probe.py"
        probe.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        gate = folder / "gate.ps1"
        gate.write_text(script, encoding="utf-8")
        # Process-only policy for this reviewed synthetic gate; no machine policy is changed.
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(gate),
                                 "-Wrapper", str(wrapper), "-PythonExe", sys.executable, "-Stub", str(probe)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and "POWERSHELL_51_PLAN_ARGV_PATH_ENV_PASS" in result.stdout, "PowerShell 5.1 gate failed"
    return {"powershell_51_plan_argv_path_env": "PASS"}


def main() -> int:
    assert PERMISSION == get_required_permission(special="mcp_external_resource")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    secret = secrets.token_bytes(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(secret, port), host="127.0.0.1", port=port, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "local MCP server did not start"
        results = asyncio.run(_checks(f"http://127.0.0.1:{port}/mcp", secret))
        results.update(_powershell_checks())
        print(json.dumps(results, sort_keys=True))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "local MCP server did not stop"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

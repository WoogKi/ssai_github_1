"""DB-free registered STDIO MCP gate and optional user-run LM Studio probe."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from app.services.ssai_permission_policy import get_required_permission
from tools.mcp_native_2ho_verify import _base_url, _safe_http_error

from tools.mcp_servers.native_auth_poc_server import (
    ACTIVE_CONTEXT, PERMISSION, SERVICE, fixture_token,
)
from tools.mcp_servers.registered_auth_poc_server import (
    ATTEMPTS, EXECUTIONS, PLUGIN_ID, _secret_from_file, make_server,
)


SERVER_PATH = Path(__file__).resolve().parent / "mcp_servers" / "registered_auth_poc_server.py"


def _claims(*, company: int = 2, user: str = "user-2", tool: str = "notice_read",
            epoch: int = 1, nonce: str | None = None) -> dict:
    return {"company_id": company, "user_id": user, "service": SERVICE,
            "permission": PERMISSION, "allowed_tools": [tool], "session_epoch": epoch,
            "aud": PLUGIN_ID, "nonce": nonce or secrets.token_hex(16),
            "exp": int(time.time()) + 90}


def _receipt(secret: bytes, claims: dict, tool: str = "notice_read") -> str:
    return hmac.digest(secret, f"{claims['nonce']}:{claims['company_id']}:{SERVICE}:{tool}".encode(),
                       "sha256").hex()


async def _direct_gate() -> dict[str, str]:
    assert PERMISSION == get_required_permission(special="mcp_external_resource")
    for counters in (ATTEMPTS, EXECUTIONS):
        for key in counters:
            counters[key] = 0
    checks = {}
    secret = secrets.token_bytes(32)
    server = make_server(secret)
    good = _claims()
    token = fixture_token(secret, good)
    arguments = {"company_id": 2, "service": SERVICE, "capability": token}
    result = await server.call_tool("notice_read", arguments)
    assert not result.is_error and _receipt(secret, good) in str(result.content)
    assert EXECUTIONS["notice_read"] == 1

    async def denied(name: str, tool: str, args: dict) -> None:
        before = EXECUTIONS.copy()
        attempts = ATTEMPTS[tool]
        try:
            response = await server.call_tool(tool, args)
        except UnexpectedToolError:
            raise
        except ToolError as exc:
            assert "permission_denied" in str(exc), name
        else:
            assert response.is_error and "permission_denied" in str(response.content), name
        assert EXECUTIONS == before, name
        assert ATTEMPTS[tool] == attempts + 1, name
        checks[name] = "PASS"

    await denied("replay", "notice_read", arguments)
    await denied("missing_capability", "notice_read", {**arguments, "capability": ""})
    await denied("altered_company_argument", "notice_read", {**arguments, "company_id": 3,
                  "capability": fixture_token(secret, _claims())})
    await denied("unlisted_tool", "admin_read", {**arguments,
                  "capability": fixture_token(secret, _claims())})
    await denied("unlisted_service", "notice_read", {**arguments, "service": "other",
                  "capability": fixture_token(secret, _claims())})
    await denied("expired", "notice_read", {**arguments,
                  "capability": fixture_token(secret, {**_claims(), "exp": int(time.time()) - 1})})
    for name, changed in (
        ("excessive_ttl", {"exp": int(time.time()) + 300}),
        ("malformed_allowlist", {"allowed_tools": "notice_read"}),
        ("malformed_company", {"company_id": "2"}),
        ("missing_nonce", {"nonce": None}),
        ("short_nonce", {"nonce": "short"}),
        ("oversized_nonce", {"nonce": "x" * 129}),
        ("same_nonce_new_signature", {"nonce": good["nonce"], "exp": int(time.time()) + 60}),
    ):
        await denied(name, "notice_read", {**arguments,
                      "capability": fixture_token(secret, {**_claims(), **changed})})
    await denied("wrong_audience", "notice_read", {**arguments,
                  "capability": fixture_token(secret, {**_claims(), "aud": "other"})})
    await denied("missing_permission", "notice_read", {**arguments,
                  "capability": fixture_token(secret, {**_claims(), "permission": "other"})})
    await denied("unlisted_user", "notice_read", {**arguments,
                  "capability": fixture_token(secret, _claims(user="intruder"))})
    await denied("unlisted_company", "notice_read", {**arguments, "company_id": 4,
                  "capability": fixture_token(secret, _claims(company=4))})
    tampered = fixture_token(secret, _claims())
    await denied("tampered_signature", "notice_read", {**arguments,
                  "capability": tampered[:-1] + ("A" if tampered[-1] != "A" else "B")})
    old = ACTIVE_CONTEXT["user-2"]
    ACTIVE_CONTEXT["user-2"] = (3, 2)
    try:
        await denied("company_switch_old_epoch", "notice_read", {**arguments,
                      "capability": fixture_token(secret, _claims())})
        await denied("company_switch_same_company_stale_epoch", "notice_read", {
            **arguments, "company_id": 3,
            "capability": fixture_token(secret, _claims(company=3))})
        switched = _claims(company=3, epoch=2)
        accepted = await server.call_tool("notice_read", {"company_id": 3, "service": SERVICE,
                        "capability": fixture_token(secret, switched)})
        assert not accepted.is_error and _receipt(secret, switched) in str(accepted.content)
        checks["company_switch_new_session"] = "PASS"
    finally:
        ACTIVE_CONTEXT["user-2"] = old
    other = _claims(company=3, user="user-3")
    accepted = await server.call_tool("notice_read", {
        "company_id": 3, "service": SERVICE, "capability": fixture_token(secret, other)})
    assert not accepted.is_error and _receipt(secret, other) in str(accepted.content)
    assert EXECUTIONS["notice_read"] == 3 and EXECUTIONS["admin_read"] == 0
    assert ATTEMPTS["notice_read"] >= 2
    checks["allowed_company_user_tool"] = "PASS"
    # A full ledger must fail closed; only expired entries may be reclaimed.
    with patch("tools.mcp_servers.registered_auth_poc_server.MAX_USED_NONCES", 3):
        await denied("replay_ledger_capacity", "notice_read", {
            **arguments, "capability": fixture_token(secret, _claims())})
    with patch("time.time", return_value=time.time() + 121), patch(
            "tools.mcp_servers.registered_auth_poc_server.MAX_USED_NONCES", 3):
        fresh = _claims()
        accepted = await server.call_tool("notice_read", {
            **arguments, "capability": fixture_token(secret, fresh)})
        assert not accepted.is_error
        await denied("expired_after_ledger_cleanup", "notice_read", arguments)
    checks["replay_ledger_expiry_cleanup"] = "PASS"
    checks["shared_permission_contract"] = "PASS"
    for failure in (RuntimeError("synthetic transport failure"), UnexpectedToolError("synthetic crash")):
        with patch.object(server, "call_tool", side_effect=failure):
            try:
                await denied("must_not_pass", "notice_read", arguments)
            except type(failure):
                pass
            else:
                raise AssertionError("unexpected failure counted as authorization denial")
    checks["unexpected_failures_do_not_pass"] = "PASS"
    return checks


async def _stdio_gate() -> None:
    secret = secrets.token_bytes(32)
    with tempfile.TemporaryDirectory() as temp:
        secret_path = Path(temp) / "secret.bin"
        secret_path.write_bytes(secret)
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER_PATH)],
                                       env={"SIMS_MCP_POC_SECRET_FILE": str(secret_path)},
                                       cwd=str(SERVER_PATH.parent))
        async with asyncio.timeout(15):
            async with Client(params, read_timeout_seconds=10) as client:
                tools = await client.list_tools()
                assert {item.name for item in tools.tools} == {"notice_read", "admin_read"}
                claims = _claims()
                response = await client.call_tool("notice_read", {
                    "company_id": 2, "service": SERVICE,
                    "capability": fixture_token(secret, claims)})
                assert not response.is_error and _receipt(secret, claims) in str(response.content)
                replay = await client.call_tool("notice_read", {
                    "company_id": 2, "service": SERVICE,
                    "capability": fixture_token(secret, claims)})
                assert replay.is_error and _receipt(secret, claims) not in str(replay.content)
                for tool, args in (
                    ("notice_read", {"company_id": 3, "service": SERVICE,
                                     "capability": fixture_token(secret, _claims())}),
                    ("admin_read", {"company_id": 2, "service": SERVICE,
                                    "capability": fixture_token(secret, _claims())}),
                    ("notice_read", {"company_id": 2, "service": SERVICE, "capability": ""}),
                ):
                    denied = await client.call_tool(tool, args)
                    assert denied.is_error and "permission_denied" in str(denied.content)
                    assert "receipt" not in str(denied.content)


def _registered_payload(model: str, capability: str, company: int) -> dict:
    return {
        "model": model,
        "input": ("Call the notice_read tool exactly once with company_id=" + str(company) +
                  ", service=" + SERVICE + ", capability=" + capability +
                  ". Return only the tool result."),
        "integrations": [{"type": "plugin", "id": PLUGIN_ID,
                          "allowed_tools": ["notice_read"]}],
        "stream": False, "store": False, "temperature": 0, "max_output_tokens": 256,
    }


def _native_probe(api_base: str, model: str) -> dict:
    import httpx

    api_base = _base_url(api_base)
    api_token = os.environ.get("LMSTUDIO_TEST_API_TOKEN", "")
    if not api_token:
        raise RuntimeError("LM Studio test API token is required; authentication is not bypassed")
    secret = _secret_from_file()
    claims = _claims()
    capability = fixture_token(secret, claims)
    request = _registered_payload(model, capability, 2)
    assert request["integrations"] == [{"type": "plugin", "id": PLUGIN_ID,
                                        "allowed_tools": ["notice_read"]}]
    try:
        with httpx.Client(timeout=120, trust_env=False) as client:
            response = client.post(api_base + "/api/v1/chat", json=request,
                                   headers={"Authorization": "Bearer " + api_token})
    except httpx.RequestError:
        return {"native_verified": False, "reason": "LM_STUDIO_TRANSPORT_ERROR"}
    if response.status_code != 200:
        return {"http_status": response.status_code, "native_verified": False,
                "reason": "LM_STUDIO_API_REJECTED_OR_PLUGIN_UNAVAILABLE",
                "error": _safe_http_error(response, (api_token, capability, secret.hex()))}
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("output"), list):
        return {"http_status": 200, "native_verified": False, "reason": "INVALID_RESPONSE"}
    all_calls = [item for item in body["output"] if isinstance(item, dict)
                 and item.get("type") == "tool_call"]
    calls = [item for item in all_calls if isinstance(item.get("provider_info"), dict)
             and item.get("type") == "tool_call" and item.get("tool") == "notice_read"
             and (item.get("provider_info") or {}).get("type") == "plugin"
             and (item.get("provider_info") or {}).get("plugin_id") == PLUGIN_ID]
    signed = [item for item in calls if _receipt(secret, claims) in str(item.get("output", ""))]
    return {"http_status": response.status_code, "plugin_tool_calls": len(calls),
            "total_tool_calls": len(all_calls), "signed_receipts": len(signed),
            "native_verified": len(all_calls) == len(calls) == len(signed) == 1}


def _diagnostic_gate() -> None:
    import httpx

    secret = secrets.token_bytes(32)
    claims = _claims()
    capability = fixture_token(secret, claims)
    api_token = secrets.token_hex(24)
    call = {"type": "tool_call", "tool": "notice_read",
            "provider_info": {"type": "plugin", "plugin_id": PLUGIN_ID},
            "output": {"receipt": _receipt(secret, claims)}}
    client_class = httpx.Client
    cases = (
        (200, {"output": [call]}, True),
        (200, {"output": [{"type": "message", "content": "tool succeeded"}]}, False),
        (200, {"output": [{**call, "output": "unsigned"}]}, False),
        (200, {"output": [call, {**call, "tool": "admin_read"}]}, False),
        (200, {"output": [{**call, "provider_info": "invalid"}]}, False),
        (200, {"output": [{**call, "provider_info": {"type": "plugin", "plugin_id": "other"}}]}, False),
        (200, [], False),
        (400, {"error": {"code": "INVALID_REQUEST", "message":
                        "invalid capability=" + capability + " Bearer " + api_token}}, False),
    )
    with patch.dict(os.environ, {"LMSTUDIO_TEST_API_TOKEN": api_token}), patch(
            __name__ + "._secret_from_file", return_value=secret), patch(
            __name__ + "._claims", return_value=claims):
        for status, body, expected in cases:
            requests = []

            def respond(request):
                requests.append(request)
                payload = json.loads(request.content)
                assert request.headers["Authorization"] == "Bearer " + api_token
                assert capability in payload["input"] and api_token not in payload["input"]
                assert payload["store"] is False
                assert payload["integrations"] == [{"type": "plugin", "id": PLUGIN_ID,
                                                     "allowed_tools": ["notice_read"]}]
                return httpx.Response(status, json=body)

            with patch.object(httpx, "Client", side_effect=lambda **kwargs:
                              client_class(transport=httpx.MockTransport(respond), **kwargs)):
                result = _native_probe("http://127.0.0.1:1234", "synthetic-model")
            assert len(requests) == 1 and result["native_verified"] is expected
            assert capability not in json.dumps(result) and api_token not in json.dumps(result)
            if status == 400:
                assert result["error"]["code"] == "INVALID_REQUEST"
        for response in (httpx.Response(200, text="not JSON"), None):
            def failure(request):
                if response is None:
                    raise httpx.ConnectError("private transport detail " + api_token, request=request)
                return response
            with patch.object(httpx, "Client", side_effect=lambda **kwargs:
                              client_class(transport=httpx.MockTransport(failure), **kwargs)):
                result = _native_probe("http://127.0.0.1:1234", "synthetic-model")
            assert not result["native_verified"] and api_token not in json.dumps(result)
        with patch.dict(os.environ, {"LMSTUDIO_TEST_API_TOKEN": ""}):
            try:
                _native_probe("http://127.0.0.1:1234", "synthetic-model")
            except RuntimeError as exc:
                assert "token is required" in str(exc)
            else:
                raise AssertionError("missing API authentication accepted")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "secret.bin"
        with patch.dict(os.environ, {"SIMS_MCP_POC_SECRET_FILE": str(path)}):
            for size in (31, 32, 33, 4096):
                path.write_bytes(b"x" * size)
                try:
                    value = _secret_from_file()
                except RuntimeError:
                    assert size != 32
                else:
                    assert size == 32 and len(value) == 32
    from contextlib import redirect_stdout
    from io import StringIO
    for verified, exit_code in ((True, 0), (False, 2)):
        with patch.object(sys, "argv", ["gate", "native"]), patch(
                __name__ + "._native_probe", return_value={"native_verified": verified}), redirect_stdout(StringIO()):
            assert main() == exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("self-test", "native"))
    parser.add_argument("--api-base", default="http://127.0.0.1:1234")
    parser.add_argument("--model", default="google/gemma-4-26b-a4b-qat")
    args = parser.parse_args()
    if args.mode == "self-test":
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        checks = asyncio.run(_direct_gate())
        asyncio.run(_stdio_gate())
        _diagnostic_gate()
        print(json.dumps({"direct_authorization": checks, "stdio_transport": "PASS",
                          "mock_native_diagnostics": "PASS",
                          "denied_tool_executions": 0}))
    else:
        result = _native_probe(args.api_base, args.model)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["native_verified"] else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

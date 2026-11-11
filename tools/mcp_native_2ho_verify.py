"""One-shot, synthetic LM Studio Native MCP check for the second host.

Copy this file and native_auth_poc_server.py beside each other under the
diagnostics directory. No ERP, KPIS, Streamlit, or server configuration access.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn


DEFAULT_DIAGNOSTICS = Path(r"D:\SSAI_DATA\diagnostics\mcp_native_phase1")


def _server_module():
    path = Path(__file__).with_name("native_auth_poc_server.py")
    if not path.is_file():
        path = Path(__file__).parent / "mcp_servers" / "native_auth_poc_server.py"
    if not path.is_file():
        raise RuntimeError("native_auth_poc_server.py missing")
    spec = importlib.util.spec_from_file_location("native_auth_poc_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_url(value: str) -> str:
    parts = urlsplit(value.rstrip("/"))
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or not parts.port:
        raise ValueError("API URL must be explicit loopback http://127.0.0.1:<port>")
    if parts.path not in {"", "/"} or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("API URL must not contain path, credentials, or query")
    return f"http://127.0.0.1:{parts.port}"


def _api_headers() -> dict[str, str]:
    token = os.environ.get("LMSTUDIO_TEST_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _preflight(client: httpx.Client, base: str, model_key: str) -> dict:
    response = client.get(base + "/api/v1/models", headers=_api_headers())
    if response.status_code == 401:
        return {"status": "AUTH_REQUIRED", "http_status": 401}
    if response.status_code != 200:
        return {"status": "API_UNAVAILABLE", "http_status": response.status_code}
    data = response.json()
    models = data.get("models", []) if isinstance(data, dict) else []
    if not isinstance(models, list):
        return {"status": "INVALID_MODEL_RESPONSE"}
    matches = [m for m in models if isinstance(m, dict) and m.get("key") == model_key]
    if len(matches) != 1:
        return {"status": "MODEL_NOT_FOUND", "model_key": model_key}
    model = matches[0]
    instances = model.get("loaded_instances")
    capabilities = model.get("capabilities")
    loaded = isinstance(instances, list) and bool(instances)
    tool_ready = capabilities.get("trained_for_tool_use") if isinstance(capabilities, dict) else None
    return {
        "status": "READY" if loaded and tool_ready is True else "MODEL_NOT_READY",
        "model_key": model_key,
        "loaded": loaded,
        "trained_for_tool_use": tool_ready,
        "loaded_instance_count": len(instances) if isinstance(instances, list) else 0,
        "per_request_mcp_setting": "NOT_READABLE_BY_API",
    }


def _claims(server, company_id=2, user_id="user-2") -> dict:
    return {
        "company_id": company_id,
        "user_id": user_id,
        "service": server.SERVICE,
        "permission": server.PERMISSION,
        "allowed_tools": ["notice_read"],
        "session_epoch": 1,
        "exp": int(time.time()) + 60,
        "nonce": secrets.token_hex(16),
    }


def _native_payload(model_key: str, mcp_url: str, token: str | None, tool: str, company_id: int) -> dict:
    integration = {
        "type": "ephemeral_mcp",
        "server_label": "sims-synthetic-poc",
        "server_url": mcp_url,
        "allowed_tools": [tool],
    }
    if token is not None:
        integration["headers"] = {"Authorization": f"Bearer {token}"}
    return {
        "model": model_key,
        "input": f"Call the {tool} tool once with company_id={company_id} and service=synthetic-notice. Use the tool; do not answer from memory.",
        "integrations": [integration],
        "stream": False,
        "store": False,
        "temperature": 0,
        "max_output_tokens": 192,
    }


def _tool_calls(body: dict, tool: str) -> list[dict]:
    output = body.get("output")
    if not isinstance(output, list):
        return []
    return [
        item for item in output
        if isinstance(item, dict)
        and item.get("type") == "tool_call"
        and item.get("tool") == tool
        and isinstance(item.get("provider_info"), dict)
        and (item.get("provider_info") or {}).get("type") == "ephemeral_mcp"
        and (item.get("provider_info") or {}).get("server_label") == "sims-synthetic-poc"
    ]


def _assess(tool: str, before: dict, after: dict, attempts_before: dict, attempts_after: dict, body: dict, expect_allow: bool) -> str:
    calls = _tool_calls(body, tool)
    executed = after[tool] - before[tool]
    attempted = attempts_after[tool] - attempts_before[tool]
    total_executed = sum(after.values()) - sum(before.values())
    total_attempted = sum(attempts_after.values()) - sum(attempts_before.values())
    output = body.get("output")
    all_calls = sum(isinstance(item, dict) and item.get("type") == "tool_call" for item in output) if isinstance(output, list) else 0
    if expect_allow:
        return "PASS" if len(calls) == all_calls == attempted == total_attempted == executed == total_executed == 1 else "INCONCLUSIVE_OR_FAIL"
    if total_executed != 0:
        return "FAIL_UNAUTHORIZED_EXECUTION"
    if len(calls) == all_calls == attempted == total_attempted == 1 and "permission_denied" in str(calls[0].get("output", "")):
        return "PASS_SERVER_DENIED"
    return "NO_TOOL_CALL_OR_PRE_DISCOVERY_BLOCK"


def _redact(value: object, secrets_to_redact: tuple[str, ...]) -> str:
    raw = str(value)
    # Redact before truncation: a long URL can precede a partly truncated secret.
    for secret in sorted((secret for secret in secrets_to_redact if secret), key=len, reverse=True):
        raw = raw.replace(secret, "[REDACTED]")
    raw = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", raw)
    raw = re.sub(r"https?://[^\s\"'<>]+", "[URL]", raw)
    raw = re.sub(r"(?i)(?:[A-Z]:\\|\\\\)[^\s\"'<>]+", "[PATH]", raw)
    raw = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", raw)
    raw = re.sub(r'''(?i)\b(api[_-]?key|authorization|client_secret|password|secret|access_token|refresh_token|jwt|token)["']?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''', r"\1=[REDACTED]", raw)
    raw = re.sub(r"\b[A-Za-z0-9_-]{24,}(?:\.[A-Za-z0-9_-]{8,})*\b", "[REDACTED_OPAQUE]", raw)
    return " ".join(raw.split())


def _safe_http_error(response: httpx.Response, secrets_to_redact: tuple[str, ...]) -> dict[str, str]:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    error = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or body.get("code") or "HTTP_ERROR"
        message = error.get("message") or error.get("detail") or body.get("message") or ""
    else:
        code = body.get("code", "HTTP_ERROR") if isinstance(body, dict) else "HTTP_ERROR"
        message = error
    lowered = str(message).lower()
    if "non-public" in lowered or "only allow public addresses" in lowered:
        category = "MCP_NON_PUBLIC_URL"
    elif "unauthorized" in lowered or "forbidden" in lowered or "authentication" in lowered or "bearer" in lowered:
        category = "MCP_AUTH_OR_API_AUTH"
    elif "initializ" in lowered:
        category = "MCP_INITIALIZATION"
    elif "connect" in lowered or "timed out" in lowered:
        category = "MCP_CONNECTION"
    elif "invalid" in lowered or "unrecognized" in lowered or "field" in lowered:
        category = "REQUEST_VALIDATION"
    else:
        category = "UNCLASSIFIED_HTTP_ERROR"
    raw = _redact(message, secrets_to_redact)
    clean_code = _redact(code, secrets_to_redact)
    safe_code = clean_code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", clean_code) else "REDACTED_CODE"
    return {"code": safe_code, "message": raw[:500], "category": category}


def _failure_stage(result: dict) -> str:
    if result["mcp_http_requests"] == 0:
        category = (result.get("http_error") or {}).get("category")
        if category == "MCP_NON_PUBLIC_URL":
            return "LM_STUDIO_NON_PUBLIC_URL_POLICY"
        if category == "REQUEST_VALIDATION":
            return "API_REQUEST_VALIDATION_BEFORE_MCP"
        return "BEFORE_MCP_CONNECTION_UNRESOLVED"
    if result["mcp_auth_checks"] and not result["mcp_auth_accepted"]:
        return "MCP_BEARER_REJECTED"
    if result["mcp_http_4xx"] and not result["mcp_auth_checks"]:
        return "MCP_HTTP_AUTH_BEFORE_VERIFIER" if result.get("mcp_http_401") else "MCP_HTTP_BEFORE_VERIFIER_UNRESOLVED"
    if result["server_tool_attempts"]:
        return "MCP_TOOL_STAGE"
    return "MCP_INITIALIZATION_OR_DISCOVERY_UNRESOLVED"


def _call(client, base, model_key, mcp_url, server, secret, name, tool, company_id, claims, expect_allow, token_override="signed") -> dict:
    token = server.fixture_token(secret, claims) if token_override == "signed" else token_override
    if token_override == "tampered":
        original = server.fixture_token(secret, claims)
        token = original[:-1] + ("A" if original[-1] != "A" else "B")
    if token_override == "missing":
        token = None
    before, attempts_before = server.EXECUTIONS.copy(), server.ATTEMPTS.copy()
    http_before, auth_before = server.HTTP_STATS.copy(), server.AUTH_STATS.copy()
    payload = _native_payload(model_key, mcp_url, token, tool, company_id)
    error = None
    body = {}
    try:
        response = client.post(base + "/api/v1/chat", headers=_api_headers(), json=payload)
        status = response.status_code
        if status != 200:
            error = _safe_http_error(response, (token or "", os.environ.get("LMSTUDIO_TEST_API_TOKEN", "").strip()))
        else:
            body = response.json()
        if not isinstance(body, dict):
            body = {}
        outcome = _assess(tool, before, server.EXECUTIONS, attempts_before, server.ATTEMPTS, body, expect_allow)
        if status != 200 and outcome == "NO_TOOL_CALL_OR_PRE_DISCOVERY_BLOCK":
            auth_rejected = (
                server.HTTP_STATS["http_401"] > http_before["http_401"]
                and server.AUTH_STATS["accepted"] == auth_before["accepted"]
            )
            outcome = "BLOCKED_BEFORE_TOOL" if name.endswith("direct_api") and auth_rejected else "API_REJECTED"
    except (httpx.HTTPError, ValueError):
        status, outcome = None, "REQUEST_ERROR"
    if not expect_allow and server.EXECUTIONS != before:
        outcome = "FAIL_UNAUTHORIZED_EXECUTION"
    result = {
        "case": name,
        "http_status": status,
        "http_error": error,
        "result": outcome,
        "mcp_tool_call_count": len(_tool_calls(body, tool)) if status == 200 else 0,
        "server_tool_attempts": server.ATTEMPTS[tool] - attempts_before[tool],
        "server_tool_executions": server.EXECUTIONS[tool] - before[tool],
        "server_total_executions": sum(server.EXECUTIONS.values()) - sum(before.values()),
        "mcp_http_requests": server.HTTP_STATS["requests"] - http_before["requests"],
        "mcp_http_4xx": server.HTTP_STATS["http_4xx"] - http_before["http_4xx"],
        "mcp_http_401": server.HTTP_STATS["http_401"] - http_before["http_401"],
        "mcp_auth_checks": server.AUTH_STATS["checked"] - auth_before["checked"],
        "mcp_auth_accepted": server.AUTH_STATS["accepted"] - auth_before["accepted"],
    }
    result["diagnostic_stage"] = _failure_stage(result) if result["result"] != "PASS" else "MCP_TOOL_EXECUTED"
    return result


def _run(client, base, model_key, server) -> dict:
    secret = secrets.token_bytes(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    app = server.make_app(secret, port)
    local = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=local.run, daemon=True)
    thread.start()
    results = []
    try:
        deadline = time.monotonic() + 8
        while not local.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not local.started:
            return {"status": "LOCAL_MCP_START_FAILED", "cases": []}
        cases = [
            ("allowed", "notice_read", 2, "user-2", True, "signed"),
            ("reused_token", "notice_read", 2, "user-2", False, "replay"),
            ("other_company", "notice_read", 4, "user-2", False, "signed"),
            ("other_user", "notice_read", 2, "intruder", False, "signed"),
            ("other_tool", "admin_read", 2, "user-2", False, "signed"),
            ("stale_after_company_switch", "notice_read", 2, "user-2", False, "signed"),
            ("tampered_direct_api", "notice_read", 2, "user-2", False, "tampered"),
            ("missing_auth_direct_api", "notice_read", 2, "user-2", False, "missing"),
        ]
        first_token = None
        for name, tool, company, user, allow, token_kind in cases:
            if results and results[0]["result"] != "PASS":
                break
            claims = _claims(server, company, user)
            if name == "allowed":
                first_token = server.fixture_token(secret, claims)
                token_kind = first_token
            elif token_kind == "replay":
                token_kind = first_token
            if name == "stale_after_company_switch":
                server.ACTIVE_CONTEXT["user-2"] = (3, 2)
            try:
                results.append(_call(client, base, model_key, mcp_url, server, secret, name, tool, company, claims, allow, token_kind))
            finally:
                if name == "stale_after_company_switch":
                    server.ACTIVE_CONTEXT["user-2"] = (2, 1)
            if results[-1]["result"] in {"FAIL_UNAUTHORIZED_EXECUTION", "REQUEST_ERROR", "API_REJECTED"}:
                break
        expected = {
            "allowed": "PASS",
            "reused_token": "PASS_SERVER_DENIED",
            "other_company": "PASS_SERVER_DENIED",
            "other_user": "PASS_SERVER_DENIED",
            "other_tool": "PASS_SERVER_DENIED",
            "stale_after_company_switch": "PASS_SERVER_DENIED",
        }
        passed = len(results) == len(cases) and all(
            next((r["result"] for r in results if r["case"] == name), None) == value
            for name, value in expected.items()
        ) and all(
            r["server_tool_executions"] == 0 and r["result"] in {"PASS_SERVER_DENIED", "BLOCKED_BEFORE_TOOL"}
            for r in results if r["case"].endswith("direct_api")
        )
        return {"status": "PASS_SYNTHETIC_NATIVE_NOT_PRODUCTION_APPROVED" if passed else "INCONCLUSIVE_OR_FAIL", "cases": results}
    finally:
        local.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("local MCP server did not stop")


def _self_test():
    import tempfile
    from unittest.mock import patch

    assert _base_url("http://localhost:1234") == "http://127.0.0.1:1234"
    for bad in ("https://example.com:1234", "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234@other:1234",
                "http://127.0.0.1:1234#fragment"):
        try:
            _base_url(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("non-loopback API accepted")
    before = {"notice_read": 0}
    good = {"output": [{"type": "tool_call", "tool": "notice_read", "provider_info": {"type": "ephemeral_mcp", "server_label": "sims-synthetic-poc"}, "output": "ok"}]}
    assert _assess("notice_read", before, {"notice_read": 1}, before, {"notice_read": 1}, good, True) == "PASS"
    assert _assess("notice_read", before, before, before, before, {"output": [{"type": "message", "content": "I called it"}]}, True) != "PASS"
    denied = {"output": [{**good["output"][0], "output": "permission_denied"}]}
    assert _assess("notice_read", before, before, before, {"notice_read": 1}, denied, False) == "PASS_SERVER_DENIED"
    assert _assess("notice_read", before, before, before, before, denied, False) != "PASS_SERVER_DENIED"
    counters = {"notice_read": 0, "admin_read": 0}
    extra = {"notice_read": 1, "admin_read": 1}
    assert _assess("notice_read", counters, extra, counters, extra, good, True) != "PASS"
    assert _assess("notice_read", counters, {"notice_read": 0, "admin_read": 1}, counters, extra, denied, False) == "FAIL_UNAUTHORIZED_EXECUTION"
    assert _tool_calls({"output": [{"type": "tool_call", "tool": "notice_read", "provider_info": "bad"}]}, "notice_read") == []
    assert _tool_calls({"output": "not-a-list"}, "notice_read") == []
    response = {"models": [{"key": "fixture", "type": "llm", "loaded_instances": [{"id": "fixture"}], "capabilities": {"trained_for_tool_use": True}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        assert _preflight(client, "http://127.0.0.1:1234", "fixture")["status"] == "READY"
        assert _preflight(client, "http://127.0.0.1:1234", "other")["status"] == "MODEL_NOT_FOUND"
    response["models"][0]["capabilities"] = {}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        assert _preflight(client, "http://127.0.0.1:1234", "fixture")["status"] == "MODEL_NOT_READY"
    payload = _native_payload("fixture", "http://127.0.0.1:5678/mcp", None, "notice_read", 2)
    assert payload["store"] is False and "headers" not in payload["integrations"][0]
    failure = httpx.Response(400, json={"error": {"code": "MCP_CONNECT", "message": "URL resolves to a non-public address http://127.0.0.1:5678/mcp Bearer test-secret"}})
    safe = _safe_http_error(failure, ("test-secret",))
    assert safe["category"] == "MCP_NON_PUBLIC_URL" and "test-secret" not in str(safe)
    assert "127.0.0.1" not in str(safe)
    secret = "short-test-secret"
    long_error = httpx.Response(400, json={"error": {"code": secret,
        "message": "http://localhost/" + "a" * 2014 + " " + secret + ' {"token": "small-value"}'}})
    safe = _safe_http_error(long_error, (secret,))
    assert "short-test" not in str(safe) and "small-value" not in str(safe)
    assert safe["code"] == "REDACTED_CODE" and len(safe["message"]) <= 500
    plain = _safe_http_error(httpx.Response(400, text='authorization="short-value"\nC:\\private\\file 192.0.2.1'), ())
    assert all(text not in str(plain) for text in ("short-value", "private", "192.0.2.1", "\n"))
    assert "small-value" not in _redact('password="small-value" access_token="small-value"', ())
    server = _server_module()
    server_source = Path(server.__file__).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="mcp layout ") as tmp:
        sidecar = Path(tmp) / "native_auth_poc_server.py"
        sidecar.write_text(server_source, encoding="utf-8")
        with patch.dict(globals(), {"__file__": str(Path(tmp) / "mcp_native_2ho_verify.py")}):
            assert _server_module().SERVICE == server.SERVICE
            sidecar.unlink()
            try:
                _server_module()
            except RuntimeError as exc:
                assert str(exc) == "native_auth_poc_server.py missing"
            else:
                raise AssertionError("missing diagnostic sidecar accepted")
    with httpx.Client(transport=httpx.MockTransport(lambda _: failure)) as client:
        rejected = _call(client, "http://127.0.0.1:1234", "fixture", "http://127.0.0.1:5678/mcp", server, b"fixture-secret", "tampered_direct_api", "notice_read", 2, _claims(server), False, "tampered")
    assert rejected["result"] == "API_REJECTED" and rejected["mcp_http_requests"] == 0
    assert rejected["http_error"]["category"] == "MCP_NON_PUBLIC_URL"
    assert rejected["diagnostic_stage"] == "LM_STUDIO_NON_PUBLIC_URL_POLICY"
    request_error = httpx.Response(400, json={"error": {"code": "bad_request", "message": "unrecognized field max_output_tokens"}})
    assert _safe_http_error(request_error, ())["category"] == "REQUEST_VALIDATION"
    sample = {**rejected, "http_error": {"category": "REQUEST_VALIDATION"}}
    assert _failure_stage(sample) == "API_REQUEST_VALIDATION_BEFORE_MCP"
    assert _failure_stage({**sample, "mcp_http_requests": 1, "mcp_http_4xx": 1, "mcp_auth_checks": 1}) == "MCP_BEARER_REJECTED"
    assert _failure_stage({**sample, "mcp_http_requests": 1, "mcp_auth_accepted": 1}) == "MCP_INITIALIZATION_OR_DISCOVERY_UNRESOLVED"
    def server_reject(status):
        def respond(_):
            server.HTTP_STATS["requests"] += 1
            server.HTTP_STATS["http_4xx"] += 1
            server.HTTP_STATS["http_401"] += int(status == 401)
            return httpx.Response(400, json={"error": {"code": "MCP_CONNECT", "message": "MCP request failed"}})
        return respond
    for status, expected in ((400, "API_REJECTED"), (401, "BLOCKED_BEFORE_TOOL")):
        with httpx.Client(transport=httpx.MockTransport(server_reject(status))) as client:
            result = _call(client, "http://127.0.0.1:1234", "fixture", "http://127.0.0.1:5678/mcp",
                           server, b"fixture-secret", "missing_auth_direct_api", "notice_read", 2, _claims(server), False, "missing")
        assert result["result"] == expected and result["server_total_executions"] == 0
    def executed_then_failed(_):
        server.EXECUTIONS["admin_read"] += 1
        return httpx.Response(200, text="not-json")
    with httpx.Client(transport=httpx.MockTransport(executed_then_failed)) as client:
        failed = _call(client, "http://127.0.0.1:1234", "fixture", "http://127.0.0.1:5678/mcp",
                       server, b"fixture-secret", "denied", "notice_read", 2, _claims(server), False)
    assert failed["result"] == "FAIL_UNAUTHORIZED_EXECUTION" and failed["server_total_executions"] == 1
    print("MCP_NATIVE_2HO_OFFLINE_GATE_PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "preflight", "run", "self-test"))
    parser.add_argument("--api-base", default="http://127.0.0.1:1234")
    parser.add_argument("--model-key")
    args = parser.parse_args()
    if args.mode == "self-test":
        _self_test()
        return 0
    if args.mode == "plan":
        print("Plan only: preflight reads loaded models; run performs one-shot local synthetic Native MCP checks.")
        return 0
    base = _base_url(args.api_base)
    if not args.model_key:
        parser.error("--model-key must name an already loaded tool-capable model")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with httpx.Client(timeout=httpx.Timeout(90, connect=5), trust_env=False) as client:
        try:
            preflight = _preflight(client, base, args.model_key)
        except (httpx.HTTPError, ValueError):
            preflight = {"status": "API_UNREACHABLE"}
        report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "preflight": preflight, "native": "NOT_RUN"}
        if args.mode == "run" and preflight["status"] == "READY":
            report["native"] = _run(client, base, args.model_key, _server_module())
    DEFAULT_DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_DIAGNOSTICS / ("native_check_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(3) + ".json")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"result_file": str(path), "preflight": preflight["status"], "native": report["native"]}, ensure_ascii=False))
    return 0 if preflight["status"] == "READY" and (args.mode == "preflight" or report["native"].get("status") == "PASS_SYNTHETIC_NATIVE_NOT_PRODUCTION_APPROVED") else 2


if __name__ == "__main__":
    raise SystemExit(main())

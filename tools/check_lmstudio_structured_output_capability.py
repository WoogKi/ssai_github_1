"""One-shot, sanitized LM Studio structured-output capability probe.

This tool intentionally does not import the Streamlit entry point or production
chat wrappers.  It loads the same project-root environment and recreates the
same OpenAI-compatible client settings with SDK retries disabled.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openai import OpenAI

from app.services.llm_health import classify_llm_exception
from app.utils.env_config import load_project_env


PROBE_TIMEOUT_DEFAULT_S = 90
PROBE_RETRY_COUNT = 0
JSON_OBJECT_EXPECTED = {"name": "probe", "count": 1, "ok": True}
JSON_SCHEMA = {
    "name": "lmstudio_probe",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "ok": {"type": "boolean"},
        },
        "required": ["name", "count", "ok"],
        "additionalProperties": False,
    },
}
DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_probe_value",
        "description": "Return one harmless fixed probe category.",
        "parameters": {
            "type": "object",
            "properties": {"category": {"type": "string", "enum": ["alpha", "beta"]}},
            "required": ["category"],
            "additionalProperties": False,
        },
    },
}
DUMMY_TOOL_CHOICE = {"type": "function", "function": {"name": "get_probe_value"}}


def _configured_timeout() -> int:
    try:
        value = int(str(os.getenv("LLM_TIMEOUT_S") or PROBE_TIMEOUT_DEFAULT_S))
    except (TypeError, ValueError):
        value = PROBE_TIMEOUT_DEFAULT_S
    return max(1, value)


def _safe_endpoint_form(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = parsed.scheme or "http"
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/v1"
    if host in {"localhost", "127.0.0.1", "::1"}:
        return f"{scheme}://localhost{path}"
    return f"{scheme}://configured-host{path}"


def _safe_status(exc: BaseException) -> dict[str, Any]:
    info = classify_llm_exception(exc)
    status = info.get("status")
    code = str(info.get("code") or "unknown_error")
    error_type = str(info.get("exception_type") or type(exc).__name__)
    lowered = str(exc).lower()
    if code == "timeout":
        capability = "TIMEOUT"
    elif code == "connection_error":
        capability = "CONNECTION_ERROR"
    elif "model" in lowered and (status in {400, 404, 422} or code == "model_not_found"):
        capability = "UNSUPPORTED_BY_MODEL"
    elif status == 501:
        capability = "UNSUPPORTED_BY_SERVER"
    elif status in {400, 404, 405, 422}:
        capability = "REQUEST_REJECTED"
    else:
        capability = "UNKNOWN_ERROR"
    return {
        "support": capability,
        "http_status": status if isinstance(status, int) else None,
        "exception_class": error_type,
        "safe_error_code": code,
    }


def _result(name: str, *, tested: bool, support: str, **values: Any) -> dict[str, Any]:
    support_value = "YES" if support == "SUPPORTED" else "NO" if support in {
        "UNSUPPORTED_BY_SERVER",
        "UNSUPPORTED_BY_MODEL",
        "SDK_INCOMPATIBLE",
        "INVALID_RESPONSE",
        "REQUEST_REJECTED",
    } else "UNKNOWN"
    result = {
        "name": name,
        "tested": tested,
        "support": support_value,
        "classification": support,
        "retry_count": PROBE_RETRY_COUNT,
    }
    result.update(values)
    return result


def _response_content(response: Any) -> tuple[str, str]:
    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    return str(getattr(message, "content", "") or ""), str(getattr(choice, "finish_reason", "") or "")


def _valid_probe_json(content: str) -> tuple[bool, bool]:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return False, False
    exact = (
        isinstance(value, dict)
        and set(value) == set(JSON_OBJECT_EXPECTED)
        and isinstance(value.get("name"), str)
        and isinstance(value.get("count"), int)
        and not isinstance(value.get("count"), bool)
        and isinstance(value.get("ok"), bool)
    )
    return True, exact


def _call_once(name: str, create: Callable[[], Any], validate: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = create()
        details = validate(response)
        # For streams, validate() consumes every chunk and validates the final result.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _result(name, tested=True, support="SUPPORTED" if details.pop("valid", False) else "INVALID_RESPONSE", elapsed_ms=elapsed_ms, **details)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _result(name, tested=True, elapsed_ms=elapsed_ms, **_safe_status(exc))


def _plain_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a capability probe. Reply only with the requested harmless value."},
        {"role": "user", "content": "Reply exactly: PLAIN_OK"},
    ]


def _json_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a capability probe. Return JSON only."},
        {"role": "user", "content": "Return a JSON object with name as a string, count as integer 1, and ok as true."},
    ]


def _tool_messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "Use the provided tool to request the alpha value."}]


def _stream_details(stream: Any, *, expect_json: bool = False, expect_tool: bool = False) -> dict[str, Any]:
    content_parts: list[str] = []
    finish_reason = ""
    tool_parts: dict[int, dict[str, str]] = {}
    content_chunks = 0
    tool_delta_count = 0
    for chunk in stream:
        choice = (getattr(chunk, "choices", None) or [None])[0]
        if choice is None:
            continue
        finish_reason = str(getattr(choice, "finish_reason", "") or finish_reason)
        delta = getattr(choice, "delta", None)
        content = str(getattr(delta, "content", "") or "")
        if content:
            content_parts.append(content)
            content_chunks += 1
        for call in getattr(delta, "tool_calls", None) or []:
            tool_delta_count += 1
            index = int(getattr(call, "index", 0) or 0)
            function = getattr(call, "function", None)
            state = tool_parts.setdefault(index, {"name": "", "arguments": ""})
            state["name"] += str(getattr(function, "name", "") or "")
            state["arguments"] += str(getattr(function, "arguments", "") or "")

    details: dict[str, Any] = {
        "finish_reason": finish_reason or None,
        "content_delta_count": content_chunks,
        "tool_call_delta_count": tool_delta_count,
    }
    if expect_json:
        json_valid, schema_valid = _valid_probe_json("".join(content_parts))
        details.update({"json_valid": json_valid, "schema_valid": schema_valid, "valid": json_valid and schema_valid})
    elif expect_tool:
        call = tool_parts.get(0, {})
        try:
            arguments = json.loads(call.get("arguments") or "")
        except (TypeError, ValueError):
            arguments = None
        valid = (
            call.get("name") == "get_probe_value"
            and isinstance(arguments, dict)
            and set(arguments) == {"category"}
            and arguments.get("category") in {"alpha", "beta"}
        )
        details.update({"tool_call_count": len(tool_parts), "arguments_valid_json": isinstance(arguments, dict), "valid": valid})
    else:
        details["valid"] = bool(content_parts) and bool(finish_reason)
    return details


def _tool_probe_result(
    request_client: Any,
    common: dict[str, Any],
    *,
    name: str,
    stream: bool,
    forced_choice: bool,
) -> dict[str, Any]:
    request_kwargs = {"messages": _tool_messages(), "stream": stream, "tools": [DUMMY_TOOL], **common}
    if forced_choice:
        request_kwargs["tool_choice"] = DUMMY_TOOL_CHOICE
    return _call_once(
        name,
        lambda: request_client.chat.completions.create(**request_kwargs),
        (lambda response: _stream_details(response, expect_tool=True)) if stream else _tool_response_details,
    )


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def run_probe() -> dict[str, Any]:
    env_result = load_project_env(override=True)
    base_url = str(os.getenv("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1").strip()
    api_key = str(os.getenv("LMSTUDIO_API_KEY") or "lm-studio")
    model = str(os.getenv("LLM_MODEL_DEFAULT") or os.getenv("LMSTUDIO_MODEL") or "").strip()
    timeout_s = _configured_timeout()
    if not model:
        return {
            "probe_version": "lmstudio-structured-capability.v1",
            "configuration": {"env_loaded": bool(env_result.loaded), "endpoint_form": _safe_endpoint_form(base_url), "model_configured": False, "timeout_s": timeout_s, "retry_count": PROBE_RETRY_COUNT},
            "results": [_result("configuration", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="model_not_configured")],
        }

    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=PROBE_RETRY_COUNT)
    request_client = client.with_options(timeout=timeout_s, max_retries=PROBE_RETRY_COUNT)
    create_signature = inspect.signature(request_client.chat.completions.create)
    sdk_parameters = set(create_signature.parameters)
    supports_response_format = "response_format" in sdk_parameters
    supports_tools = "tools" in sdk_parameters and "tool_choice" in sdk_parameters
    common = {"model": model, "temperature": 0, "max_tokens": 64}

    results: list[dict[str, Any]] = []
    results.append(
        _call_once(
            "plain_non_stream",
            lambda: request_client.chat.completions.create(messages=_plain_messages(), stream=False, **common),
            lambda response: {"valid": bool(_response_content(response)[0]), "finish_reason": _response_content(response)[1] or None, "content_kind": "text"},
        )
    )

    if supports_response_format:
        results.append(
            _call_once(
                "json_object",
                lambda: request_client.chat.completions.create(messages=_json_messages(), stream=False, response_format={"type": "json_object"}, **common),
                lambda response: {
                    "valid": _valid_probe_json(_response_content(response)[0])[0] and _valid_probe_json(_response_content(response)[0])[1],
                    "json_valid": _valid_probe_json(_response_content(response)[0])[0],
                    "schema_valid": _valid_probe_json(_response_content(response)[0])[1],
                    "finish_reason": _response_content(response)[1] or None,
                },
            )
        )
        results.append(
            _call_once(
                "json_schema_strict",
                lambda: request_client.chat.completions.create(messages=_json_messages(), stream=False, response_format={"type": "json_schema", "json_schema": JSON_SCHEMA}, **common),
                lambda response: {
                    "valid": _valid_probe_json(_response_content(response)[0])[0] and _valid_probe_json(_response_content(response)[0])[1],
                    "json_valid": _valid_probe_json(_response_content(response)[0])[0],
                    "schema_valid": _valid_probe_json(_response_content(response)[0])[1],
                    "finish_reason": _response_content(response)[1] or None,
                },
            )
        )
    else:
        results.extend(
            [_result(name, tested=False, support="SDK_INCOMPATIBLE", safe_error_code="response_format_parameter_missing") for name in ("json_object", "json_schema_strict")]
        )

    tool_cases = (
        ("tools_only_non_stream", False, False),
        ("tools_forced_tool_choice_non_stream", False, True),
        ("tools_only_stream", True, False),
        ("tools_forced_tool_choice_stream", True, True),
    )
    if supports_tools:
        results.extend(
            _tool_probe_result(request_client, common, name=name, stream=stream, forced_choice=forced_choice)
            for name, stream, forced_choice in tool_cases
        )
    else:
        results.extend(
            _result(name, tested=False, support="SDK_INCOMPATIBLE", safe_error_code="tools_parameter_missing")
            for name, _stream, _forced_choice in tool_cases
        )

    results.append(
        _call_once(
            "plain_stream",
            lambda: request_client.chat.completions.create(messages=_plain_messages(), stream=True, **common),
            lambda stream: _stream_details(stream),
        )
    )
    if supports_response_format:
        results.append(
            _call_once(
                "json_object_stream",
                lambda: request_client.chat.completions.create(messages=_json_messages(), stream=True, response_format={"type": "json_object"}, **common),
                lambda stream: _stream_details(stream, expect_json=True),
            )
        )
        results.append(
            _call_once(
                "json_schema_strict_stream",
                lambda: request_client.chat.completions.create(messages=_json_messages(), stream=True, response_format={"type": "json_schema", "json_schema": JSON_SCHEMA}, **common),
                lambda stream: _stream_details(stream, expect_json=True),
            )
        )
    else:
        results.append(_result("json_object_stream", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="response_format_parameter_missing"))
        results.append(_result("json_schema_strict_stream", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="response_format_parameter_missing"))

    return {
        "probe_version": "lmstudio-structured-capability.v1",
        "configuration": {
            "env_loaded": bool(env_result.loaded),
            "endpoint_form": _safe_endpoint_form(base_url),
            "model": model,
            "timeout_s": timeout_s,
            "retry_count": PROBE_RETRY_COUNT,
            "sdk_response_format_parameter": supports_response_format,
            "sdk_tools_parameter": supports_tools,
        },
        "results": results,
    }


def run_json_schema_stream_probe() -> dict[str, Any]:
    """Probe the one structured-stream variant not covered by JSON-object mode."""
    env_result = load_project_env(override=True)
    base_url = str(os.getenv("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1").strip()
    api_key = str(os.getenv("LMSTUDIO_API_KEY") or "lm-studio")
    model = str(os.getenv("LLM_MODEL_DEFAULT") or os.getenv("LMSTUDIO_MODEL") or "").strip()
    timeout_s = _configured_timeout()
    configuration = {
        "env_loaded": bool(env_result.loaded),
        "endpoint_form": _safe_endpoint_form(base_url),
        "model": model,
        "timeout_s": timeout_s,
        "retry_count": PROBE_RETRY_COUNT,
    }
    if not model:
        return {
            "probe_version": "lmstudio-structured-capability.v1",
            "configuration": configuration,
            "results": [_result("json_schema_strict_stream", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="model_not_configured")],
        }
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=PROBE_RETRY_COUNT)
    request_client = client.with_options(timeout=timeout_s, max_retries=PROBE_RETRY_COUNT)
    if "response_format" not in inspect.signature(request_client.chat.completions.create).parameters:
        return {
            "probe_version": "lmstudio-structured-capability.v1",
            "configuration": configuration,
            "results": [_result("json_schema_strict_stream", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="response_format_parameter_missing")],
        }
    result = _call_once(
        "json_schema_strict_stream",
        lambda: request_client.chat.completions.create(
            messages=_json_messages(),
            model=model,
            temperature=0,
            max_tokens=64,
            stream=True,
            response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
        ),
        lambda stream: _stream_details(stream, expect_json=True),
    )
    return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": [result]}


def run_tools_matrix_probe() -> dict[str, Any]:
    """Run each tools/tool_choice form once without repeating other capability calls."""
    env_result = load_project_env(override=True)
    base_url = str(os.getenv("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1").strip()
    api_key = str(os.getenv("LMSTUDIO_API_KEY") or "lm-studio")
    model = str(os.getenv("LLM_MODEL_DEFAULT") or os.getenv("LMSTUDIO_MODEL") or "").strip()
    timeout_s = _configured_timeout()
    configuration = {
        "env_loaded": bool(env_result.loaded),
        "endpoint_form": _safe_endpoint_form(base_url),
        "model": model,
        "timeout_s": timeout_s,
        "retry_count": PROBE_RETRY_COUNT,
    }
    tool_cases = (
        ("tools_only_non_stream", False, False),
        ("tools_forced_tool_choice_non_stream", False, True),
        ("tools_only_stream", True, False),
        ("tools_forced_tool_choice_stream", True, True),
    )
    if not model:
        results = [_result(name, tested=False, support="SDK_INCOMPATIBLE", safe_error_code="model_not_configured") for name, _stream, _forced_choice in tool_cases]
        return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": results}
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=PROBE_RETRY_COUNT)
    request_client = client.with_options(timeout=timeout_s, max_retries=PROBE_RETRY_COUNT)
    parameters = set(inspect.signature(request_client.chat.completions.create).parameters)
    if not {"tools", "tool_choice"}.issubset(parameters):
        results = [_result(name, tested=False, support="SDK_INCOMPATIBLE", safe_error_code="tools_parameter_missing") for name, _stream, _forced_choice in tool_cases]
        return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": results}
    common = {"model": model, "temperature": 0, "max_tokens": 64}
    results = [
        _tool_probe_result(request_client, common, name=name, stream=stream, forced_choice=forced_choice)
        for name, stream, forced_choice in tool_cases
    ]
    return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": results}


def run_stream_baseline_probe() -> dict[str, Any]:
    """Re-measure supported streams with elapsed time through final chunk validation."""
    env_result = load_project_env(override=True)
    base_url = str(os.getenv("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1").strip()
    api_key = str(os.getenv("LMSTUDIO_API_KEY") or "lm-studio")
    model = str(os.getenv("LLM_MODEL_DEFAULT") or os.getenv("LMSTUDIO_MODEL") or "").strip()
    timeout_s = _configured_timeout()
    configuration = {
        "env_loaded": bool(env_result.loaded),
        "endpoint_form": _safe_endpoint_form(base_url),
        "model": model,
        "timeout_s": timeout_s,
        "retry_count": PROBE_RETRY_COUNT,
    }
    names = ("plain_stream", "json_schema_strict_stream")
    if not model:
        results = [_result(name, tested=False, support="SDK_INCOMPATIBLE", safe_error_code="model_not_configured") for name in names]
        return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": results}
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=PROBE_RETRY_COUNT)
    request_client = client.with_options(timeout=timeout_s, max_retries=PROBE_RETRY_COUNT)
    common = {"model": model, "temperature": 0, "max_tokens": 64}
    results = [
        _call_once(
            "plain_stream",
            lambda: request_client.chat.completions.create(messages=_plain_messages(), stream=True, **common),
            lambda stream: _stream_details(stream),
        )
    ]
    if "response_format" not in inspect.signature(request_client.chat.completions.create).parameters:
        results.append(_result("json_schema_strict_stream", tested=False, support="SDK_INCOMPATIBLE", safe_error_code="response_format_parameter_missing"))
    else:
        results.append(
            _call_once(
                "json_schema_strict_stream",
                lambda: request_client.chat.completions.create(messages=_json_messages(), stream=True, response_format={"type": "json_schema", "json_schema": JSON_SCHEMA}, **common),
                lambda stream: _stream_details(stream, expect_json=True),
            )
        )
    return {"probe_version": "lmstudio-structured-capability.v1", "configuration": configuration, "results": results}


def _tool_response_details(response: Any) -> dict[str, Any]:
    choice = (getattr(response, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    calls = getattr(message, "tool_calls", None) or []
    call = calls[0] if calls else None
    function = getattr(call, "function", None)
    try:
        arguments = json.loads(str(getattr(function, "arguments", "") or ""))
    except (TypeError, ValueError):
        arguments = None
    valid = (
        getattr(function, "name", "") == "get_probe_value"
        and isinstance(arguments, dict)
        and set(arguments) == {"category"}
        and arguments.get("category") in {"alpha", "beta"}
    )
    return {
        "valid": valid,
        "finish_reason": str(getattr(choice, "finish_reason", "") or "") or None,
        "tool_call_count": len(calls),
        "arguments_valid_json": isinstance(arguments, dict),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a one-shot sanitized LM Studio capability probe.")
    parser.add_argument("--output", required=True, type=Path, help="Sanitized JSON evidence output path")
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--only-json-schema-stream", action="store_true", help="Run only the strict JSON Schema streaming probe")
    selected.add_argument("--only-tools-matrix", action="store_true", help="Run each tools/tool_choice form once")
    selected.add_argument("--only-stream-baseline", action="store_true", help="Re-measure supported streams through final chunk validation")
    args = parser.parse_args()
    if args.only_json_schema_stream:
        evidence = run_json_schema_stream_probe()
    elif args.only_tools_matrix:
        evidence = run_tools_matrix_probe()
    elif args.only_stream_baseline:
        evidence = run_stream_baseline_probe()
    else:
        evidence = run_probe()
    _write_atomic(args.output, evidence)
    print(json.dumps({"output_written": True, "result_count": len(evidence.get("results", [])), "retry_count": PROBE_RETRY_COUNT}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

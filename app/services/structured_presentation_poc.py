"""Feature-gated strict JSON Schema presentation for completed Analytics results.

This module never routes, queries, authorizes, or changes a legacy payload. It
only turns a small, already-renderable deterministic summary into optional
presentation text after the business result is complete.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from app.services.llm_health import classify_llm_exception


FEATURE_FLAG = "SIMS_STRUCTURED_PRESENTATION_POC"
MAX_SUMMARY_CHARS = 1800
MAX_LIST_ITEMS = 3
MAX_PRESENTATION_SUMMARY_CHARS = 240
MAX_KEY_POINT_CHARS = 72
MAX_NOTICE_CHARS = 48
PRESENTATION_MAX_TOKENS = 768
PRESENTATION_SCHEMA = {
    "name": "sims_structured_presentation_poc_v1",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": MAX_PRESENTATION_SUMMARY_CHARS},
            "key_points": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_KEY_POINT_CHARS},
                "maxItems": MAX_LIST_ITEMS,
            },
            "notices": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_NOTICE_CHARS},
                "maxItems": MAX_LIST_ITEMS,
            },
        },
        "required": ["summary", "key_points", "notices"],
        "additionalProperties": False,
    },
}
_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def structured_presentation_poc_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return the explicit opt-in flag; production is disabled by default."""
    env = environ if environ is not None else os.environ
    return str(env.get(FEATURE_FLAG) or "").strip().lower() in _TRUE_VALUES


def _safe_text(value: Any, *, limit: int) -> str:
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    return text[:limit].strip()


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def build_analytics_presentation_facts(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract only a narrow, UI-visible summary for the presentation prompt."""
    item = payload if isinstance(payload, Mapping) else {}
    meta_value = item.get("meta")
    meta = meta_value if isinstance(meta_value, Mapping) else {}
    if not bool(meta.get("analysis_nlq")) or bool(meta.get("current_table_followup")):
        return None

    # This optional overlay must follow the already-finalized business status.
    # Do not treat execution_status as a substitute: legacy routes can assign
    # it before they derive a terminal no_data result.
    result_status = _safe_text(meta.get("result_status"), limit=80)
    if result_status != "success":
        return None

    summary = _safe_text(meta.get("summary_md") or meta.get("summary"), limit=MAX_SUMMARY_CHARS)
    action = _safe_text(item.get("action") or meta.get("canonical_action") or meta.get("action"), limit=160)
    if not summary or not action:
        return None

    facts: dict[str, Any] = {"action": action, "deterministic_summary": summary}
    for key in ("row_count", "row_count_total"):
        value = _safe_number(meta.get(key))
        if value is not None:
            facts[key] = value
    return facts


def validate_structured_presentation(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate only derived LLM text; business facts are never revalidated."""
    raw = value if isinstance(value, Mapping) else {}
    if set(raw) != {"summary", "key_points", "notices"}:
        raise ValueError("structured presentation keys are invalid")
    summary = _safe_text(raw.get("summary"), limit=MAX_PRESENTATION_SUMMARY_CHARS)
    if not summary:
        raise ValueError("structured presentation summary is missing")

    result: dict[str, Any] = {"summary": summary}
    item_limits = {"key_points": MAX_KEY_POINT_CHARS, "notices": MAX_NOTICE_CHARS}
    for key in ("key_points", "notices"):
        values = raw.get(key)
        if not isinstance(values, list) or len(values) > MAX_LIST_ITEMS:
            raise ValueError("structured presentation list is invalid")
        normalized = [_safe_text(item, limit=item_limits[key]) for item in values]
        if any(not item for item in normalized):
            raise ValueError("structured presentation contains an empty item")
        result[key] = normalized
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def _response_content_details(response: Any) -> tuple[str, dict[str, Any]]:
    """Return content plus safe shape diagnostics, never the content itself."""
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None) if choice is not None else None
    raw_content = getattr(message, "content", None) if message is not None else None
    content = str(raw_content or "")
    stripped = content.strip()
    return content, {
        "finish_reason": _safe_text(getattr(choice, "finish_reason", ""), limit=80) or None,
        "content_type": type(raw_content).__name__,
        "content_length": len(content),
        "content_starts_json_object": stripped.startswith("{"),
        "content_ends_json_object": stripped.endswith("}"),
        "content_has_code_fence": "```" in stripped,
    }


def _safe_failure(
    exc: BaseException,
    *,
    elapsed_ms: int,
    failure_stage: str,
    response_present: bool = False,
    content_present: bool = False,
    response_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-sensitive failure diagnostics for the optional overlay."""
    info = classify_llm_exception(exc)
    exception_class = type(exc).__name__
    diagnostics = dict(response_diagnostics or {})
    reason_code = str(info.get("code") or "unknown_error")
    if failure_stage == "json_parse":
        if (
            diagnostics.get("finish_reason") == "length"
            and diagnostics.get("content_starts_json_object") is True
            and diagnostics.get("content_ends_json_object") is False
        ):
            reason_code = "output_truncated"
        else:
            reason_code = "json_parse_failed"
    elif failure_stage == "schema_validation":
        reason_code = "schema_validation_failed"
    elif failure_stage == "response_shape":
        reason_code = "response_shape_invalid"
    return {
        "status": "failed",
        "reason_code": reason_code,
        "elapsed_ms": elapsed_ms,
        "retry_count": 0,
        "tool_call_count": 0,
        "failure_stage": failure_stage,
        "exception_class": exception_class,
        "response_present": bool(response_present),
        "content_present": bool(content_present),
        "json_parse_failed": failure_stage == "json_parse",
        "schema_validation_failed": failure_stage == "schema_validation",
        "finish_reason": diagnostics.get("finish_reason"),
        "content_type": diagnostics.get("content_type"),
        "content_length": diagnostics.get("content_length"),
        "content_starts_json_object": diagnostics.get("content_starts_json_object"),
        "content_ends_json_object": diagnostics.get("content_ends_json_object"),
        "content_has_code_fence": diagnostics.get("content_has_code_fence"),
    }


def create_structured_presentation(
    facts: Mapping[str, Any],
    *,
    client: Any,
    model: str,
    timeout_s: int,
) -> dict[str, Any]:
    """Call LM Studio once, non-streaming, with strict JSON Schema only."""
    started = time.perf_counter()
    response_present = False
    content_present = False
    response_diagnostics: dict[str, Any] = {}
    try:
        request_client = getattr(client, "with_options", lambda **_kwargs: client)(
            timeout=max(1, int(timeout_s)),
            max_retries=0,
        )
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="client_options",
        )

    try:
        response = request_client.chat.completions.create(
            model=str(model),
            temperature=0,
            max_tokens=PRESENTATION_MAX_TOKENS,
            stream=False,
            response_format={"type": "json_schema", "json_schema": PRESENTATION_SCHEMA},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "주어진 확정 결과만 한국어로 짧게 정리하세요. 제공되지 않은 숫자·사실은 만들지 말고, "
                        "업무 결과·조건·권한·행 수를 바꾸거나 추론하지 마세요. JSON 외 텍스트는 쓰지 마세요."
                    ),
                },
                {"role": "user", "content": json.dumps(dict(facts), ensure_ascii=False)},
            ],
        )
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="request",
        )

    response_present = response is not None
    try:
        content, response_diagnostics = _response_content_details(response)
        content_present = bool(content.strip())
        if not content_present:
            raise ValueError("structured presentation response content is missing")
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="response_shape",
            response_present=response_present,
            content_present=content_present,
            response_diagnostics=response_diagnostics,
        )

    try:
        decoded = json.loads(content)
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="json_parse",
            response_present=response_present,
            content_present=content_present,
            response_diagnostics=response_diagnostics,
        )

    try:
        presentation = validate_structured_presentation(decoded)
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="schema_validation",
            response_present=response_present,
            content_present=content_present,
            response_diagnostics=response_diagnostics,
        )

    try:
        return {
            "status": "ready",
            "reason_code": "schema_valid",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "retry_count": 0,
            "tool_call_count": 0,
            "presentation": presentation,
            **response_diagnostics,
        }
    except Exception as exc:
        return _safe_failure(
            exc,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            failure_stage="response_build",
            response_present=response_present,
            content_present=content_present,
            response_diagnostics=response_diagnostics,
        )


def maybe_create_structured_presentation(
    payload: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Return an optional overlay without changing the supplied legacy payload."""
    env = environ if environ is not None else os.environ
    if not structured_presentation_poc_enabled(env):
        return {"status": "skipped", "reason_code": "feature_disabled", "retry_count": 0, "tool_call_count": 0}
    facts = build_analytics_presentation_facts(payload)
    if facts is None:
        return {"status": "skipped", "reason_code": "not_eligible", "retry_count": 0, "tool_call_count": 0}

    model = _safe_text(env.get("LLM_MODEL_DEFAULT") or env.get("LMSTUDIO_MODEL"), limit=200)
    if not model:
        return {"status": "failed", "reason_code": "model_not_configured", "retry_count": 0, "tool_call_count": 0}
    try:
        timeout_s = max(1, int(str(env.get("LLM_TIMEOUT_S") or "90")))
    except (TypeError, ValueError):
        timeout_s = 90

    try:
        if client_factory is None:
            from openai import OpenAI

            client = OpenAI(
                base_url=str(env.get("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1"),
                api_key=str(env.get("LMSTUDIO_API_KEY") or "lm-studio"),
                max_retries=0,
            )
        else:
            client = client_factory()
    except Exception as exc:
        return _safe_failure(exc, elapsed_ms=0, failure_stage="client_create")
    return create_structured_presentation(facts, client=client, model=model, timeout_s=timeout_s)

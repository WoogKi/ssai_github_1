# app/services/llm_health.py
from __future__ import annotations

import os
from typing import Any

from openai import OpenAI

try:  # OpenAI SDK versions differ a little; keep this module import-safe.
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        NotFoundError,
        RateLimitError,
    )
except Exception:  # pragma: no cover - defensive for old SDKs
    APIConnectionError = APITimeoutError = AuthenticationError = None  # type: ignore
    APIStatusError = NotFoundError = RateLimitError = None  # type: ignore


SAFE_MESSAGES = {
    "authentication_error": "LM Studio 인증에 실패했습니다. 관리자에게 API 토큰 설정을 확인해 달라고 요청하세요.",
    "connection_error": "LM Studio 서버에 연결할 수 없습니다. 서버 실행 상태를 확인해 주세요.",
    "timeout": "LM Studio 응답이 지연되고 있습니다. 다른 요청 처리 중이면 잠시 뒤 다시 시도해 주세요.",
    "rate_or_queue_busy": "LM Studio가 다른 요청을 처리 중입니다. 잠시 뒤 다시 시도해 주세요.",
    "model_not_found": "LM Studio에 지정된 모델이 로드되어 있지 않습니다. 운영 모델 로드 상태를 확인해 주세요.",
    "model_not_loaded": "LM Studio에 로드된 모델이 없습니다. 운영 모델을 먼저 로드해 주세요.",
    "model_mismatch": "LM Studio에 운영 모델과 다른 모델이 로드되어 있습니다. 모델 로드 상태를 확인해 주세요.",
    "server_error": "LM Studio 서버에서 일시적인 오류가 발생했습니다. 잠시 뒤 다시 시도해 주세요.",
    "empty_response": "LM Studio 응답이 비어 있습니다. 모델 설정과 Thinking 옵션을 확인해 주세요.",
    "reasoning_only": "LM Studio가 본문 없이 추론 내용만 반환했습니다. Gemma Thinking 설정이 꺼져 있는지 확인해 주세요.",
    "unknown_error": "LM Studio 호출 중 알 수 없는 오류가 발생했습니다.",
}

RETRYABLE_ERROR_CODES = {
    "connection_error",
    "timeout",
    "rate_or_queue_busy",
    "server_error",
}

DEFAULT_HEALTH_TIMEOUT_S = 4.0


def _status_code(exc: BaseException | None) -> int | None:
    if exc is None:
        return None
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def classify_llm_exception(exc: BaseException | None) -> dict[str, Any]:
    """Return a log-safe, user-safe classification for OpenAI/LM Studio errors."""
    preset_code = getattr(exc, "llm_error_code", None)
    if preset_code:
        code = str(preset_code)
        return {
            "code": code,
            "status": getattr(exc, "llm_status", None),
            "exception_type": str(getattr(exc, "llm_exception_type", None) or type(exc).__name__),
            "retryable": code in RETRYABLE_ERROR_CODES,
            "user_message": SAFE_MESSAGES.get(code, SAFE_MESSAGES["unknown_error"]),
        }

    status = _status_code(exc)
    exc_type = type(exc).__name__ if exc is not None else "None"

    if AuthenticationError is not None and isinstance(exc, AuthenticationError):
        code = "authentication_error"
    elif APITimeoutError is not None and isinstance(exc, APITimeoutError):
        code = "timeout"
    elif APIConnectionError is not None and isinstance(exc, APIConnectionError):
        code = "connection_error"
    elif RateLimitError is not None and isinstance(exc, RateLimitError):
        code = "rate_or_queue_busy"
    elif NotFoundError is not None and isinstance(exc, NotFoundError):
        code = "model_not_found"
    elif status == 401:
        code = "authentication_error"
    elif status == 404:
        code = "model_not_found"
    elif status == 408:
        code = "timeout"
    elif status == 429:
        code = "rate_or_queue_busy"
    elif status is not None and 500 <= status <= 599:
        code = "server_error"
    elif exc_type.lower().find("timeout") >= 0:
        code = "timeout"
    elif exc_type.lower().find("connect") >= 0:
        code = "connection_error"
    else:
        code = "unknown_error"

    return {
        "code": code,
        "status": status,
        "exception_type": exc_type,
        "retryable": code in RETRYABLE_ERROR_CODES,
        "user_message": SAFE_MESSAGES.get(code, SAFE_MESSAGES["unknown_error"]),
    }


def bounded_retry_count(value: Any) -> int:
    """Clamp configured retry counts to the operationally safe 0..1 range."""
    try:
        parsed = int(value)
    except Exception:
        parsed = 0
    return max(0, min(1, parsed))


def should_retry_llm_error(
    err_info: dict[str, Any],
    *,
    attempt: int,
    max_retries: Any,
    content_started: bool = False,
) -> bool:
    """Decide whether an LM request may be retried without duplicating output."""
    if content_started:
        return False
    retry_count = bounded_retry_count(max_retries)
    if attempt >= retry_count:
        return False
    return bool(err_info.get("retryable"))


def expected_model_from_env() -> str:
    """Keep the existing key scheme: LLM_MODEL_DEFAULT overrides LMSTUDIO_MODEL."""
    return str(os.getenv("LLM_MODEL_DEFAULT") or os.getenv("LMSTUDIO_MODEL") or "").strip()


def check_llm(
    *,
    expected_model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    client: Any | None = None,
    health_timeout_s: float | int | None = None,
) -> dict[str, Any]:
    """Check LM Studio availability and loaded model list without exposing secrets."""
    expected = str(expected_model or expected_model_from_env() or "").strip()
    resolved_base_url = base_url or os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    resolved_api_key = api_key if api_key is not None else os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    try:
        timeout_s = float(health_timeout_s if health_timeout_s is not None else DEFAULT_HEALTH_TIMEOUT_S)
    except Exception:
        timeout_s = DEFAULT_HEALTH_TIMEOUT_S
    if timeout_s <= 0:
        timeout_s = DEFAULT_HEALTH_TIMEOUT_S
    resolved_client = client or OpenAI(
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        timeout=timeout_s,
        max_retries=0,
    )
    list_client = getattr(resolved_client, "with_options", lambda **kw: resolved_client)(
        timeout=timeout_s,
        max_retries=0,
    )

    try:
        models = list_client.models.list()
        names = [str(m.id) for m in getattr(models, "data", []) if getattr(m, "id", None)]
    except Exception as exc:
        info = classify_llm_exception(exc)
        return {
            "ok": False,
            "code": info["code"],
            "status": info["status"],
            "exception_type": info["exception_type"],
            "user_message": info["user_message"],
            "detail": "models_list_failed",
            "expected_model": expected,
            "models": [],
            "count": 0,
        }

    if expected and expected not in names:
        code = "model_not_loaded" if not names else "model_mismatch"
        return {
            "ok": False,
            "code": code,
            "status": None,
            "exception_type": "",
            "user_message": SAFE_MESSAGES[code],
            "detail": "expected_model_missing",
            "expected_model": expected,
            "models": names[:20],
            "count": len(names),
        }

    return {
        "ok": True,
        "code": "ok",
        "status": None,
        "exception_type": "",
        "user_message": "",
        "detail": "ok",
        "expected_model": expected,
        "models": names[:20],
        "count": len(names),
    }


def extract_chat_completion_text(resp: Any) -> dict[str, Any]:
    """Extract only user-displayable content; never return reasoning text itself."""
    choice = None
    try:
        choice = resp.choices[0]
    except Exception:
        choice = None

    message = getattr(choice, "message", None)
    content = str(getattr(message, "content", "") or "").strip()
    reasoning = str(getattr(message, "reasoning_content", "") or "").strip()
    finish_reason = getattr(choice, "finish_reason", None)
    usage = getattr(resp, "usage", None)

    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)

    if content:
        code = "ok"
        user_message = ""
    elif reasoning:
        code = "reasoning_only"
        user_message = SAFE_MESSAGES[code]
    else:
        code = "empty_response"
        user_message = SAFE_MESSAGES[code]

    return {
        "content": content,
        "has_reasoning": bool(reasoning),
        "code": code,
        "user_message": user_message,
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }

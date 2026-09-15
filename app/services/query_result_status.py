from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_NO_DATA_STATUSES = frozenset({"no_data", "empty"})
_GUIDANCE_STATUSES = frozenset({
    "input_required",
    "candidate_required",
    "selection_required",
    "ambiguous",
    "confirmation_required",
    "resolution_unavailable",
    "not_found",
    "role_mismatch",
    "unsupported",
    "column_unavailable",
    "validation_error",
    "routing_error",
    "query_error",
    "timeout",
    "error",
})
_GENERIC_EMPTY_MESSAGES = frozenset({
    "",
    "None",
    "nan",
    "NaN",
    "조회 결과가 없습니다.",
    "조회 결과가 없습니다",
    "해당 자료가 없습니다.",
    "해당 자료가 없습니다",
    "해당 자료 없습니다.",
    "해당 자료 없습니다",
    "해당 조회조건의 자료가 없습니다.",
    "해당 조회조건의 자료가 없습니다",
})


def query_result_status(meta: Mapping[str, Any] | None) -> str:
    """Return the strongest query-level terminal status carried by metadata."""
    values = []
    source = meta if isinstance(meta, Mapping) else {}
    for key in ("result_status", "execution_status", "entity_resolution_status"):
        value = str(source.get(key) or "").strip().lower()
        if value:
            values.append(value)

    for value in values:
        if value in _GUIDANCE_STATUSES:
            return value
    for value in values:
        if value in _NO_DATA_STATUSES:
            return value
    return values[0] if values else ""


def is_guidance_status(meta: Mapping[str, Any] | None) -> bool:
    return query_result_status(meta) in _GUIDANCE_STATUSES


def _input_required_default(action: str) -> str:
    action_text = str(action or "").strip()
    if "현재고" in action_text:
        return (
            "조회할 제품명·제품코드·제조사 등을 입력해 주세요.\n\n"
            "예: 아스피린 현재고 조회"
        )
    if "제품재고" in action_text or "제품수불" in action_text:
        return (
            "조회할 제품명·제품코드 또는 재고 위치 등 조회 조건을 입력해 주세요.\n\n"
            "예: 아스피린 제품재고장 조회"
        )
    if "계약단가" in action_text:
        return (
            "조회할 제품명·제품코드·제조사 또는 단가적용처를 입력해 주세요.\n\n"
            "예: 아스피린 계약단가 조회"
        )
    if "구매원가" in action_text:
        return (
            "조회할 제품명·제품코드·매입처 또는 재고 위치를 입력해 주세요.\n\n"
            "예: 아스피린 구매원가 조회"
        )
    if "발주" in action_text:
        return (
            "조회할 제품·제조사·발주처 또는 기간 등 조회 조건을 입력해 주세요.\n\n"
            "예: 환인 발주 조회"
        )
    return "조회에 필요한 조건을 더 입력해 주세요."


def tableless_user_message(payload: Mapping[str, Any] | None, action: str = "") -> str:
    """Resolve a tableless result message without conflating skipped input with zero rows."""
    source = payload if isinstance(payload, Mapping) else {}
    meta = source.get("meta") if isinstance(source.get("meta"), Mapping) else {}
    status = query_result_status(meta)
    message = str(
        source.get("message")
        or source.get("data")
        or meta.get("message")
        or meta.get("empty_message")
        or ""
    ).strip()

    if status == "input_required" and message in _GENERIC_EMPTY_MESSAGES:
        return _input_required_default(action or source.get("action") or source.get("title"))
    if status in _GUIDANCE_STATUSES and message:
        return message
    if message not in _GENERIC_EMPTY_MESSAGES:
        return message
    if "검증" in str(action or source.get("action") or source.get("title") or ""):
        return "검증 결과 이상 자료가 없습니다."
    return "해당 조회조건의 자료가 없습니다."


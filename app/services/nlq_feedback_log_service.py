"""Append-only NLQ response feedback records for offline case review."""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.nlq_case_log_service import resolve_nlq_case_log_path


_KST = dt.timezone(dt.timedelta(hours=9))
_WRITE_LOCK = threading.Lock()
_FEEDBACK_VALUES = frozenset({"like", "dislike"})
_REASON_VALUES = frozenset(
    {
        "intent_wrong",
        "condition_missing",
        "result_wrong",
        "too_slow",
        "explanation_poor",
        "other",
    }
)


def resolve_nlq_feedback_log_path(*, environ: Mapping[str, str] | None = None) -> Path:
    """Store feedback beside the configured append-only NLQ case log."""
    return resolve_nlq_case_log_path(environ=environ).with_name("nlq_feedback.jsonl")


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_context_value(context: Mapping[str, Any] | None, field: str) -> str:
    if not isinstance(context, Mapping):
        return ""
    return str(context.get(field) or "").strip()


def append_nlq_feedback_event(
    *,
    request_id: Any,
    assistant_message_id: Any,
    feedback: Any,
    reason: Any = "",
    note: Any = "",
    runtime_context: Mapping[str, Any] | None = None,
    occurred_at: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and append one immutable feedback event without result payload data."""
    normalized_feedback = _required_text(feedback, field="feedback").lower()
    if normalized_feedback not in _FEEDBACK_VALUES:
        raise ValueError("feedback must be like or dislike")

    normalized_reason = str(reason or "").strip().lower()
    if normalized_reason and normalized_reason not in _REASON_VALUES:
        raise ValueError("reason is invalid")

    normalized_note = str(note or "").strip()
    if len(normalized_note) > 1000:
        raise ValueError("note is too long")

    event_time = occurred_at
    if event_time is None:
        event_time = dt.datetime.now(_KST).isoformat(timespec="milliseconds")
    else:
        event_time = _required_text(event_time, field="occurred_at")

    record = {
        "schema_version": "1.0",
        "feedback_id": str(uuid.uuid4()),
        "request_id": _required_text(request_id, field="request_id"),
        "assistant_message_id": _required_text(assistant_message_id, field="assistant_message_id"),
        "feedback": normalized_feedback,
        "reason": normalized_reason,
        "note": normalized_note,
        "occurred_at": event_time,
        "company_id": _optional_context_value(runtime_context, "company_id"),
        "user_id": _optional_context_value(runtime_context, "user_id"),
        "room_id": _optional_context_value(runtime_context, "room_id"),
    }
    path = resolve_nlq_feedback_log_path(environ=environ)
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def latest_feedback_events(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Select the latest immutable event per NLQ request and assistant message."""
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in events:
        request_id = str(event.get("request_id") or "").strip()
        message_id = str(event.get("assistant_message_id") or "").strip()
        if not request_id or not message_id:
            continue
        key = (request_id, message_id)
        if key not in latest or str(event.get("occurred_at") or "") >= str(latest[key].get("occurred_at") or ""):
            latest[key] = event
    return latest

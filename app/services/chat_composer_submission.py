from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


PENDING_ATTACHMENT_SUBMISSION_KEY = "__chat_composer_pending_submission"


def queue_attachment_submission_event(
    state: MutableMapping[str, Any],
    *,
    submission: Any,
    event_id: str,
    context: Mapping[str, str],
) -> None:
    """Store one composer attachment event until the next script pass claims it."""
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        raise ValueError("event_id is required")
    state[PENDING_ATTACHMENT_SUBMISSION_KEY] = {
        "event_id": normalized_event_id,
        "submission": submission,
        "context": {str(key): str(value or "") for key, value in context.items()},
    }


def consume_attachment_submission_event(
    state: MutableMapping[str, Any],
    *,
    context: Mapping[str, str],
) -> tuple[Any | None, str, str]:
    """Atomically remove and return a pending attachment submission event."""
    pending = state.pop(PENDING_ATTACHMENT_SUBMISSION_KEY, None)
    if pending is None:
        return None, "", "missing"

    # Consume legacy pending values once so an in-flight session survives a code reload.
    if not isinstance(pending, Mapping) or "submission" not in pending:
        return pending, "legacy_pending", "ready"

    event_id = str(pending.get("event_id") or "").strip()
    pending_context = pending.get("context")
    expected_context = {str(key): str(value or "") for key, value in context.items()}
    if not isinstance(pending_context, Mapping):
        return None, event_id, "invalid_context"
    normalized_context = {
        str(key): str(value or "") for key, value in pending_context.items()
    }
    if normalized_context != expected_context:
        return None, event_id, "stale_context"
    if not event_id:
        return None, "", "invalid_event"
    return pending.get("submission"), event_id, "ready"


def select_composer_submission(
    state: MutableMapping[str, Any],
    *,
    widget_submission: Any,
    context: Mapping[str, str],
) -> tuple[Any | None, str, str]:
    """Select a fresh callback event and suppress a consumed binary widget value."""
    pending_submission, event_id, status = consume_attachment_submission_event(
        state,
        context=context,
    )
    if status == "ready":
        return pending_submission, event_id, status
    if widget_submission is not None and not isinstance(widget_submission, str):
        files = list(getattr(widget_submission, "files", ()) or ())
        audio = getattr(widget_submission, "audio", None)
        if files or audio is not None:
            return None, event_id, f"{status}_binary_ignored"
    return widget_submission, event_id, status


def attachment_request_text(composer_text: str, sidebar_request: str) -> str:
    """The text submitted with files belongs to that batch, if supplied."""
    return str(composer_text or "").strip() or str(sidebar_request or "").strip()

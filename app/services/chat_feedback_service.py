from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from app.services.ssai_storage_service import get_storage_root, get_user_area_dir

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows is the production target.
    msvcrt = None

CHAT_FEEDBACK_REVIEW = "CHAT_FEEDBACK_REVIEW"
_KST = timezone(timedelta(hours=9))
FEEDBACK_SCHEMA_VERSION = 1
FEEDBACK_RETENTION_DAYS = 180
FEEDBACK_VALUES = frozenset({"like", "dislike"})
DISLIKE_REASON_CODES = (
    "incorrect_content",
    "misunderstood_question",
    "inconvenient_table",
    "too_slow",
    "insufficient_explanation",
    "other",
)
DISLIKE_REASON_LABELS = {
    "incorrect_content": "내용이 틀림",
    "misunderstood_question": "질문을 잘못 이해함",
    "inconvenient_table": "표가 불편함",
    "too_slow": "너무 느림",
    "insufficient_explanation": "설명이 부족함",
    "other": "기타",
}

_FEEDBACK_WRITE_LOCK = threading.RLock()
_REVIEW_LOG_WRITE_LOCK = threading.RLock()
_REVIEW_LOG_FILENAME = "feedback_review_log.jsonl"
_REVIEW_TEXT_MAX_CHARS = 4_000


class ChatFeedbackError(ValueError):
    pass


class ChatFeedbackAccessError(PermissionError):
    pass


def _positive_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ChatFeedbackAccessError(f"invalid_{field}") from exc
    if result <= 0:
        raise ChatFeedbackAccessError(f"invalid_{field}")
    return result


def _uuid_text(value: Any, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChatFeedbackAccessError(f"invalid_{field}") from exc


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kst_date(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return _utc_now(parsed).astimezone(_KST).date().isoformat()


def bounded_plain_text(message: Mapping[str, Any], *, max_chars: int = 12_000) -> str:
    """Return only bounded visible answer text; table bodies and metadata stay out."""
    candidates = (
        message.get("content"),
        message.get("message"),
        (message.get("meta") or {}).get("summary_md") if isinstance(message.get("meta"), Mapping) else None,
        message.get("title"),
    )
    raw = next((str(value) for value in candidates if value not in (None, "")), "")
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = unescape(raw)
    raw = re.sub(r"[\t\r ]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw[: max(0, int(max_chars))]


def _redact_review_text(value: str) -> str:
    value = re.sub(
        r"(?i)\b(?:api[_-]?key|password|passwd|secret|token)\b\s*[:=]\s*[^\s,;]+",
        "[REDACTED]",
        value,
    )
    return re.sub(r"(?i)\bsecret(?:[-_][a-z0-9]+)+\b", "[REDACTED]", value)


def _bounded_review_text(value: Any, *, max_chars: int = _REVIEW_TEXT_MAX_CHARS) -> str:
    return _redact_review_text(bounded_plain_text({"content": value}, max_chars=max_chars))


def _same_visible_text(left: str, right: str) -> bool:
    return bool(left) and bool(right) and re.sub(r"\s+", " ", left).strip() == re.sub(r"\s+", " ", right).strip()


def bounded_visible_answer_summary(message: Mapping[str, Any], *, max_chars: int = _REVIEW_TEXT_MAX_CHARS) -> str:
    """Keep review evidence to visible text; never include a table payload."""
    meta = message.get("meta") if isinstance(message.get("meta"), Mapping) else {}
    message_type = str(message.get("type") or meta.get("message_type") or "").strip().lower()
    structured = bool(
        message.get("action")
        or meta.get("action")
        or message_type in {"table", "sims_result", "dashboard_lite"}
    )
    if not structured:
        return _bounded_review_text(message.get("content") or message.get("message") or "", max_chars=max_chars)

    title = _bounded_review_text(
        message.get("title") or meta.get("title") or message.get("action") or meta.get("action") or "",
        max_chars=500,
    )
    row_count = next(
        (
            value
            for value in (
                meta.get("row_count_total"), meta.get("full_rows"), meta.get("row_count"),
                meta.get("rows"), message.get("row_count"), message.get("rows"),
            )
            if value not in (None, "")
        ),
        None,
    )
    query_summary = _bounded_review_text(
        meta.get("query_summary") or meta.get("condition_summary") or meta.get("condition") or "",
        max_chars=1_500,
    )
    summary = _bounded_review_text(
        meta.get("summary_md") or meta.get("summary") or message.get("summary") or "",
        max_chars=2_000,
    )
    if _same_visible_text(query_summary, summary):
        summary = ""
    parts = [title] if title else []
    if row_count is not None:
        try:
            parts.append(f"결과 건수: {int(row_count):,}건")
        except (TypeError, ValueError):
            pass
    if query_summary:
        parts.append(f"조회조건: {query_summary}")
    if summary:
        parts.append(summary)
    return _bounded_review_text("\n\n".join(parts), max_chars=max_chars)


def _message_route(message: Mapping[str, Any]) -> str:
    meta = message.get("meta") if isinstance(message.get("meta"), Mapping) else {}
    explicit = str(meta.get("route_kind") or message.get("route_kind") or "").strip()
    if explicit:
        return explicit
    message_type = str(
        message.get("type") or meta.get("message_type") or message.get("message_type") or ""
    ).strip().lower()
    if message_type == "knowledge_answer":
        return "knowledge_rag"
    if message_type == "web_search" or bool(meta.get("web_search")):
        return "web_latest"
    if message_type == "mcp_external_resource":
        return "mcp_external_resource"
    if message_type == "datetime_tool":
        return "datetime_tool"
    if message_type == "sims_help":
        return "sims_help"
    if message_type == "dashboard_lite":
        return "dashboard_lite"
    if bool(meta.get("current_table_followup")):
        return "current_table"
    if message.get("action") or meta.get("action"):
        return "sims_nlq"
    return "generic_llm"


def _message_contract(message: Mapping[str, Any]) -> dict[str, str]:
    meta = message.get("meta") if isinstance(message.get("meta"), Mapping) else {}
    message_id = _uuid_text(message.get("id") or message.get("message_id"), field="message_id")
    request_id = str(
        meta.get("request_id")
        or meta.get("nlq_trace_request_id")
        or message.get("request_id")
        or message_id
    ).strip()[:128]
    route_kind = _message_route(message)[:64]
    return {
        "message_id": message_id,
        "request_id": request_id,
        "request_id_source": (
            "request_id" if meta.get("request_id") or message.get("request_id")
            else "nlq_trace_request_id" if meta.get("nlq_trace_request_id")
            else "message_id"
        ),
        # route is kept for aggregate compatibility; route_kind is the public
        # current-state/review name and must match the actual message route.
        "route": route_kind,
        "route_kind": route_kind,
        "action": str(message.get("action") or meta.get("action") or "").strip()[:160],
        "result_status": str(
            meta.get("result_status") or message.get("result_status") or meta.get("status") or "unknown"
        ).strip()[:64],
    }


def _validate_owner(
    *,
    room: Mapping[str, Any],
    message: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
) -> tuple[int, int, str, Mapping[str, Any], dict[str, str]]:
    current_user_id = _positive_int(user_id, field="user_id")
    current_company_id = _positive_int(company_id, field="company_id")
    room_id = _uuid_text(room.get("id"), field="room_id")
    room_user_id = _positive_int(room.get("user_id"), field="room_user_id")
    room_company_id = _positive_int(room.get("company_id"), field="room_company_id")
    if room_user_id != current_user_id or room_company_id != current_company_id:
        raise ChatFeedbackAccessError("room_owner_mismatch")
    requested_message_id = _uuid_text(
        message.get("id") or message.get("message_id"),
        field="message_id",
    )
    canonical_message = next(
        (
            item
            for item in (room.get("messages") or [])
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip() == requested_message_id
        ),
        None,
    )
    if canonical_message is None:
        raise ChatFeedbackAccessError("canonical_message_missing")
    if str(canonical_message.get("role") or "").strip().lower() != "assistant":
        raise ChatFeedbackAccessError("assistant_message_required")
    for field, expected in (("user_id", current_user_id), ("company_id", current_company_id)):
        value = canonical_message.get(field)
        if value not in (None, "") and _positive_int(value, field=f"message_{field}") != expected:
            raise ChatFeedbackAccessError(f"message_{field}_mismatch")
    return (
        current_user_id,
        current_company_id,
        room_id,
        canonical_message,
        _message_contract(canonical_message),
    )


def _record_path(
    *,
    company_id: int,
    user_id: int,
    room_id: str,
    message_id: str,
    storage_root: Path | None,
    create: bool,
) -> Path:
    if storage_root is None:
        area = get_user_area_dir(
            company_id=company_id,
            user_id=user_id,
            area="feedback",
            create=create,
        )
    else:
        area = Path(storage_root) / f"company_{company_id}" / f"user_{user_id}" / "feedback"
        if create:
            area.mkdir(parents=True, exist_ok=True)
    room_dir = area / room_id
    if create:
        room_dir.mkdir(parents=True, exist_ok=True)
    return room_dir / f"{message_id}.json"


def _review_log_path(
    *,
    company_id: int,
    user_id: int,
    storage_root: Path | None,
    create: bool,
) -> Path:
    if storage_root is None:
        area = get_user_area_dir(
            company_id=company_id,
            user_id=user_id,
            area="feedback",
            create=create,
        )
    else:
        area = Path(storage_root) / f"company_{company_id}" / f"user_{user_id}" / "feedback"
        if create:
            area.mkdir(parents=True, exist_ok=True)
    return area / _REVIEW_LOG_FILENAME


def _read_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChatFeedbackError("feedback_record_invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != FEEDBACK_SCHEMA_VERSION:
        raise ChatFeedbackError("feedback_record_invalid")
    return value


def _atomic_write(path: Path, record: Mapping[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(record), handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


@contextmanager
def _record_write_lock(path: Path):
    """Serialize one record across Streamlit sessions without retries."""
    lock_path = path.with_suffix(".lock")
    with _FEEDBACK_WRITE_LOCK, lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if msvcrt is not None:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ChatFeedbackError("feedback_write_busy") from exc
        try:
            yield
        finally:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _review_log_write_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    with _REVIEW_LOG_WRITE_LOCK, lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if msvcrt is not None:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ChatFeedbackError("review_log_write_busy") from exc
        try:
            yield
        finally:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _previous_user_question(room: Mapping[str, Any], *, message_id: str) -> str:
    messages = room.get("messages") or []
    if not isinstance(messages, list):
        return ""
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping) or str(item.get("id") or "").strip() != message_id:
            continue
        for prior in reversed(messages[:index]):
            if not isinstance(prior, Mapping) or str(prior.get("role") or "").strip().lower() != "user":
                continue
            return _bounded_review_text(prior.get("content") or prior.get("message") or "", max_chars=2_000)
        break
    return ""


def _review_event_kind(existing: Mapping[str, Any] | None, record: Mapping[str, Any]) -> str | None:
    previous_feedback = str((existing or {}).get("feedback") or "")
    current_feedback = str(record.get("feedback") or "")
    if current_feedback == "dislike":
        return "dislike_created" if previous_feedback != "dislike" else "dislike_reason_changed"
    if previous_feedback == "dislike":
        return "dislike_cleared" if not current_feedback else "dislike_replaced"
    return None


def _append_review_log_event(
    *,
    room: Mapping[str, Any],
    canonical_message: Mapping[str, Any],
    record: Mapping[str, Any],
    previous_record: Mapping[str, Any] | None,
    event_kind: str,
    storage_root: Path | None,
) -> Path:
    log_path = _review_log_path(
        company_id=int(record["company_id"]),
        user_id=int(record["user_id"]),
        storage_root=storage_root,
        create=True,
    )
    event = {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "event_id": f"{record['feedback_id']}:{record['revision']}",
        "event_kind": event_kind,
        "timestamp": record["updated_at"],
        "company_id": record["company_id"],
        "user_id": record["user_id"],
        "room_id": record["room_id"],
        "message_id": record["message_id"],
        "request_id": record["request_id"],
        "route_kind": record["route_kind"],
        "action": record["action"],
        "result_status": record["result_status"],
        "question_text": _previous_user_question(room, message_id=str(record["message_id"])),
        "answer_summary": bounded_visible_answer_summary(canonical_message),
        "feedback": record["feedback"],
        "reason_code": record["reason_code"],
        "previous_feedback": (previous_record or {}).get("feedback"),
        "previous_reason_code": (previous_record or {}).get("reason_code"),
        "review_state": "unreviewed",
        "promoted_to_regression": False,
        "expires_at": record["expires_at"],
        "retention_days": FEEDBACK_RETENTION_DAYS,
    }
    with _review_log_write_lock(log_path):
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return log_path


def update_chat_feedback(
    *,
    room: Mapping[str, Any],
    message: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
    feedback: str | None,
    reason_code: str | None = None,
    now: datetime | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    current_user_id, current_company_id, room_id, canonical_message, contract = _validate_owner(
        room=room,
        message=message,
        user_id=user_id,
        company_id=company_id,
    )
    normalized_feedback = str(feedback or "").strip().lower() or None
    if normalized_feedback not in FEEDBACK_VALUES and normalized_feedback is not None:
        raise ChatFeedbackError("invalid_feedback")
    normalized_reason = str(reason_code or "").strip().lower() or None
    if normalized_feedback == "dislike" and normalized_reason not in (None, *DISLIKE_REASON_CODES):
        raise ChatFeedbackError("invalid_dislike_reason")
    if normalized_feedback != "dislike" and normalized_reason is not None:
        raise ChatFeedbackError("reason_requires_dislike")

    path = _record_path(
        company_id=current_company_id,
        user_id=current_user_id,
        room_id=room_id,
        message_id=contract["message_id"],
        storage_root=storage_root,
        create=True,
    )
    with _record_write_lock(path):
        existing = _read_record(path)
        if existing is not None:
            identity = ("user_id", "company_id", "room_id", "message_id")
            expected = (current_user_id, current_company_id, room_id, contract["message_id"])
            if tuple(existing.get(key) for key in identity) != expected:
                raise ChatFeedbackAccessError("stored_owner_mismatch")
            if existing.get("feedback") == normalized_feedback and existing.get("reason_code") == normalized_reason:
                return {"status": "unchanged", "write_count": 0, "record": existing, "path": path}

        current = _utc_now(now)
        plain_text = bounded_plain_text(canonical_message)
        record = {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "record_kind": "review_signal",
            "feedback_id": str(existing.get("feedback_id")) if existing else str(uuid.uuid4()),
            **contract,
            "room_id": room_id,
            "user_id": current_user_id,
            "company_id": current_company_id,
            "feedback": normalized_feedback,
            "reason_code": normalized_reason,
            "state": "active" if normalized_feedback else "cleared",
            "content_hash": hashlib.sha256(plain_text.encode("utf-8")).hexdigest(),
            "created_at": str(existing.get("created_at")) if existing else _iso_utc(current),
            "updated_at": _iso_utc(current),
            "expires_at": _iso_utc(current + timedelta(days=FEEDBACK_RETENTION_DAYS)),
            "retention_days": FEEDBACK_RETENTION_DAYS,
            "revision": int(existing.get("revision") or 0) + 1 if existing else 1,
            "review_state": "unreviewed",
        }
        _atomic_write(path, record)
        event_kind = _review_event_kind(existing, record)
        review_log_path = None
        if event_kind:
            review_log_path = _append_review_log_event(
                room=room,
                canonical_message=canonical_message,
                record=record,
                previous_record=existing,
                event_kind=event_kind,
                storage_root=storage_root,
            )
    return {
        "status": "created" if existing is None else "cleared" if normalized_feedback is None else "changed",
        "write_count": 1,
        "record": record,
        "path": path,
        "review_log_path": review_log_path,
        "review_log_write_count": 1 if review_log_path else 0,
    }


def read_chat_feedback(
    *,
    room: Mapping[str, Any],
    message: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
    storage_root: Path | None = None,
) -> dict[str, Any] | None:
    current_user_id, current_company_id, room_id, _canonical_message, contract = _validate_owner(
        room=room, message=message, user_id=user_id, company_id=company_id
    )
    path = _record_path(
        company_id=current_company_id,
        user_id=current_user_id,
        room_id=room_id,
        message_id=contract["message_id"],
        storage_root=storage_root,
        create=False,
    )
    record = _read_record(path)
    if record is None:
        return None
    if (
        record.get("user_id") != current_user_id
        or record.get("company_id") != current_company_id
        or record.get("room_id") != room_id
        or record.get("message_id") != contract["message_id"]
    ):
        raise ChatFeedbackAccessError("stored_owner_mismatch")
    return record


def can_review_chat_feedback(
    *,
    user_type: Any,
    user_grade: Any,
    permission_codes: Any = (),
) -> bool:
    permissions = {str(value or "").strip().upper() for value in (permission_codes or ())}
    return CHAT_FEEDBACK_REVIEW in permissions or (
        str(user_type or "").strip().upper() == "SSART_ADMIN"
        and str(user_grade or "").strip().upper() == "SUPER"
    )


def aggregate_chat_feedback(
    *,
    reviewer_user_type: Any,
    reviewer_user_grade: Any,
    permission_codes: Any = (),
    storage_root: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read only aggregate; identifiers and content never leave this boundary."""
    if not can_review_chat_feedback(
        user_type=reviewer_user_type,
        user_grade=reviewer_user_grade,
        permission_codes=permission_codes,
    ):
        raise ChatFeedbackAccessError("review_permission_denied")
    root = Path(storage_root) if storage_root is not None else get_storage_root()
    current = _utc_now(now)
    groups: MutableMapping[tuple[str, int, str, str, str, str], dict[str, int]] = defaultdict(
        lambda: {"like_count": 0, "dislike_count": 0}
    )
    for path in root.glob("company_*/user_*/feedback/*/*.json"):
        try:
            record = _read_record(path)
            if not record or record.get("state") != "active" or record.get("feedback") not in FEEDBACK_VALUES:
                continue
            expires_at = datetime.fromisoformat(str(record.get("expires_at") or "").replace("Z", "+00:00"))
            if _utc_now(expires_at) <= current:
                continue
            company = _positive_int(record.get("company_id"), field="company_id")
            day = _kst_date(record.get("updated_at"))
            route = str(record.get("route") or "unknown")[:64]
            action = str(record.get("action") or "")[:160]
            status = str(record.get("result_status") or "unknown")[:64]
            reason = str(record.get("reason_code") or "")[:64]
            key = (day, company, route, action, status, reason)
            groups[key][f"{record['feedback']}_count"] += 1
        except (ChatFeedbackError, ChatFeedbackAccessError, ValueError, TypeError, OSError):
            continue
    rows: list[dict[str, Any]] = []
    for key in sorted(groups):
        day, company, route, action, status, reason = key
        counts = groups[key]
        total = counts["like_count"] + counts["dislike_count"]
        rows.append(
            {
                "date": day,
                "company_id": company,
                "route": route,
                "action": action,
                "result_status": status,
                "reason_code": reason,
                **counts,
                "feedback_count": total,
                "dislike_rate": (counts["dislike_count"] / total) if total else 0.0,
            }
        )
    return rows


def list_chat_feedback_review_events(
    *,
    reviewer_user_type: Any,
    reviewer_user_grade: Any,
    permission_codes: Any = (),
    storage_root: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read the private dislike review queue; regular users never receive it."""
    if not can_review_chat_feedback(
        user_type=reviewer_user_type,
        user_grade=reviewer_user_grade,
        permission_codes=permission_codes,
    ):
        raise ChatFeedbackAccessError("review_permission_denied")
    root = Path(storage_root) if storage_root is not None else get_storage_root()
    current = _utc_now(now)
    events: list[dict[str, Any]] = []
    for path in root.glob(f"company_*/user_*/feedback/{_REVIEW_LOG_FILENAME}"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                expires_at = datetime.fromisoformat(str(event.get("expires_at") or "").replace("Z", "+00:00"))
                if _utc_now(expires_at) <= current:
                    continue
                events.append(event)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
    return sorted(events, key=lambda item: str(item.get("timestamp") or ""), reverse=True)


__all__ = [
    "CHAT_FEEDBACK_REVIEW",
    "DISLIKE_REASON_CODES",
    "DISLIKE_REASON_LABELS",
    "FEEDBACK_RETENTION_DAYS",
    "ChatFeedbackAccessError",
    "ChatFeedbackError",
    "aggregate_chat_feedback",
    "bounded_visible_answer_summary",
    "bounded_plain_text",
    "can_review_chat_feedback",
    "list_chat_feedback_review_events",
    "read_chat_feedback",
    "update_chat_feedback",
]

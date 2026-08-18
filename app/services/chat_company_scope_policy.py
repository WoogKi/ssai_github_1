"""Fail-closed ownership checks for user-scoped SSAI chat rooms."""

from __future__ import annotations

from typing import Any, Iterable


def normalize_company_id(value: Any) -> str:
    """Return a canonical positive company id, or an empty value on invalid input."""
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return ""
    try:
        numeric = int(text)
    except (TypeError, ValueError):
        return ""
    return str(numeric) if numeric > 0 else ""


def room_company_id(room: dict[str, Any] | None) -> str:
    """Read only explicit room ownership; message metadata never fills this value."""
    if not isinstance(room, dict):
        return ""
    return normalize_company_id(room.get("company_id"))


def room_matches_company(room: dict[str, Any] | None, company_id: Any) -> bool:
    """Allow a room only for its exact, explicit company owner."""
    selected = normalize_company_id(company_id)
    return bool(selected and room_company_id(room) == selected)


def rooms_for_company(
    rooms: Iterable[dict[str, Any]] | None,
    company_id: Any,
) -> list[dict[str, Any]]:
    """Return only rooms owned by the selected company; unknown legacy rooms stay hidden."""
    return [room for room in (rooms or []) if room_matches_company(room, company_id)]

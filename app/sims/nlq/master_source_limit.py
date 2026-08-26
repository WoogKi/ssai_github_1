"""채팅 master NLQ의 full-source 상한 계약."""

from __future__ import annotations

import os
from typing import Optional


def _positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_chat_source_limit(
    requested_top: object,
    *,
    action_cap: object = None,
) -> int:
    """Return the SQL TOP value for a chat master query.

    ``SIMS_MAX_ROWS_CHAT=0`` means no common source cap. A narrower
    action-specific performance cap remains in force in that case. Returning
    ``0`` is the explicit service contract for omitting SQL ``TOP``.
    ``requested_top`` remains a legacy caller hint, but cannot silently
    reintroduce the old 1000/2000 full-source caps.
    """
    del requested_top

    action_limit = _positive_int(action_cap)
    common_limit = _positive_int(os.getenv("SIMS_MAX_ROWS_CHAT", "0"))
    if common_limit is None:
        return action_limit or 0
    if action_limit is None:
        return common_limit
    return min(common_limit, action_limit)

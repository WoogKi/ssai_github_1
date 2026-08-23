"""Deterministic length planning for existing attachment extraction summaries."""
from __future__ import annotations

from dataclasses import dataclass
import re

DEFAULT_FALLBACK_TARGET = 1200
_MIN_TARGET = 300
_MAX_TARGET = 12000


@dataclass(frozen=True)
class AttachmentSummaryPlan:
    mode: str
    target_chars: int
    chunk_chars: int
    merge_batch_chars: int
    source_chars: int
    section_count: int
    user_override: str = ""


def _clamp_target(value: int) -> int:
    return max(_MIN_TARGET, min(_MAX_TARGET, int(value)))


def _request_target(user_request: str) -> tuple[int | None, str]:
    request = str(user_request or "").strip()
    if not request:
        return None, ""
    match = re.search(r"(?<!\d)(\d{3,5})\s*자(?:로|로만|정도|내외)?", request)
    if match:
        return _clamp_target(int(match.group(1))), "request_chars"
    compact = bool(re.search(r"간단히|짧게|핵심만|요약만", request))
    detailed = bool(re.search(r"자세히|상세히|충분히|상세", request))
    if compact and not detailed:
        return 900, "request_compact"
    if detailed and not compact:
        return None, "request_detailed"
    return None, ""


def _mode_for_source(source_chars: int) -> str:
    if source_chars <= 14000:
        return "single_pass"
    if source_chars <= 80000:
        return "chunked"
    return "hierarchical"


def build_attachment_summary_plan(
    text: str,
    *,
    section_count: int = 0,
    user_request: str = "",
    requested_target: int | None = None,
) -> AttachmentSummaryPlan:
    """Choose depth by extracted content, never by attachment file extension."""
    source_chars = len(str(text or "").strip())
    sections = max(1, int(section_count or 0))
    request_target, request_mode = _request_target(user_request)
    mode = _mode_for_source(source_chars)

    if request_target is not None:
        return AttachmentSummaryPlan(mode, request_target, 6000, 30000, source_chars, sections, request_mode)
    if requested_target is not None:
        return AttachmentSummaryPlan(mode, _clamp_target(requested_target), 6000, 30000, source_chars, sections, "widget_chars")

    structured_floor = min(3600, 1200 + min(sections, 20) * 120)
    if request_mode == "request_detailed":
        target = _clamp_target(max(structured_floor, min(8000, max(2400, source_chars // 2))))
        return AttachmentSummaryPlan(mode, target, 6000, 30000, source_chars, sections, request_mode)
    if source_chars <= 2500:
        return AttachmentSummaryPlan("preserve", max(source_chars, _MIN_TARGET), 6000, 30000, source_chars, sections)
    if source_chars <= 14000:
        target = _clamp_target(max(structured_floor, min(5000, int(source_chars * 0.55))))
        return AttachmentSummaryPlan(mode, target, 6000, 30000, source_chars, sections)
    if source_chars <= 80000:
        target = _clamp_target(max(structured_floor, min(7000, max(2400, int(source_chars * 0.22)))))
        return AttachmentSummaryPlan(mode, target, 6000, 30000, source_chars, sections)
    target = _clamp_target(max(structured_floor, min(10000, max(5000, int(source_chars * 0.08)))))
    return AttachmentSummaryPlan(mode, target, 6000, 30000, source_chars, sections)
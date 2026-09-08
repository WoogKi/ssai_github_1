from __future__ import annotations

import re


def looks_like_pasted_formatted_content(text: str) -> bool:
    """Return True for long, structured content that is not a single NLQ request."""
    value = str(text or "").strip()
    if len(value) < 400:
        return False

    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) < 8:
        return False

    heading_count = sum(bool(re.match(r"^\s*#{1,6}\s+", line)) for line in lines)
    table_row_count = sum(line.count("|") >= 2 for line in lines)
    list_count = sum(bool(re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line)) for line in lines)
    separator_count = sum(bool(re.match(r"^\s*-{3,}\s*$", line)) for line in lines)

    return bool(
        table_row_count >= 3
        or heading_count >= 2
        or (heading_count >= 1 and list_count >= 2)
        or (separator_count >= 1 and list_count >= 3)
    )


def looks_like_attachment_followup(
    text: str,
    *,
    has_active_image_reference: bool = False,
) -> bool:
    """Recognize image/document questions before ERP keyword routing."""
    value = str(text or "").strip()
    if not value:
        return False

    explicit_reference = bool(
        re.search(
            r"(?:이|그|첨부)?\s*(?:사진|이미지|화면|그림|캡처|스크린샷|처방전|문서|파일)",
            value,
            flags=re.IGNORECASE,
        )
    )
    visual_detail = bool(
        re.search(
            r"(?:작업자.*(?:어디|무엇)|물건.*(?:이동|흐름)|상부.*하부.*구조|중요한\s*부분.*(?:설명|알려)|무엇을\s*뜻|어떻게\s*다른)",
            value,
            flags=re.IGNORECASE,
        )
    )
    analysis_intent = bool(
        re.search(r"(?:추출|읽어|판독|분석|설명|알려|찾아|확인|보여)", value)
    )

    if visual_detail:
        return True
    if explicit_reference and analysis_intent:
        return True
    return bool(has_active_image_reference and analysis_intent)

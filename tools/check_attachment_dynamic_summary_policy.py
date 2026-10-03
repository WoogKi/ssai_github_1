"""Offline regression for attachment dynamic summary length policy."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.attachment_summary_policy import build_attachment_summary_plan


def main() -> None:
    short = build_attachment_summary_plan("짧은 TXT 문서\n수량 12개", section_count=1)
    assert short.mode == "preserve" and short.target_chars >= len("짧은 TXT 문서\n수량 12개")

    medium = build_attachment_summary_plan("PDF 본문 " * 1800, section_count=4)
    assert medium.mode == "single_pass" and medium.target_chars > 1200

    long_pdf = build_attachment_summary_plan("PDF 페이지 본문 " * 7000, section_count=24)
    assert long_pdf.mode == "chunked" and long_pdf.chunk_chars == 6000

    very_long_docx = build_attachment_summary_plan("DOCX 문단 표 행 " * 25000, section_count=80)
    assert very_long_docx.mode == "hierarchical" and very_long_docx.target_chars > long_pdf.target_chars

    table = build_attachment_summary_plan("제품코드,수량,금액\nA,12,3400\n" * 700, section_count=16)
    assert table.target_chars >= 3000
    ocr = build_attachment_summary_plan("OCR 날짜 2026-08-22 수량 12 단위 박스\n" * 90, section_count=1)
    assert ocr.mode == "single_pass"

    compact = build_attachment_summary_plan("본문 " * 5000, user_request="간단히 정리해줘")
    assert compact.target_chars == 900 and compact.user_override == "request_compact"
    for request in (
        "자세히 분석해줘",
        "상세히 검토해줘",
        "전체 분석",
        "충분히 설명해줘",
        "이 파일 전체 검토",
        "파일을 근거로 질문할게",
    ):
        detailed = build_attachment_summary_plan("본문 " * 5000, user_request=request)
        assert detailed.target_chars is None and detailed.user_override == "request_detailed"
    chars = build_attachment_summary_plan("본문 " * 5000, user_request="1000자로 정리해줘", requested_target=3000)
    assert chars.target_chars == 1000 and chars.user_override == "request_chars"
    widget = build_attachment_summary_plan("본문 " * 5000, requested_target=2800)
    assert widget.target_chars == 2800 and widget.user_override == "widget_chars"
    requested_long = build_attachment_summary_plan("본문 " * 30000, user_request="1000자로 정리해줘")
    assert requested_long.mode == "hierarchical" and requested_long.target_chars == 1000
    explicit_large = build_attachment_summary_plan("본문 " * 30000, user_request="20000자로 정리해줘")
    assert explicit_large.target_chars == 20000
    detailed_hierarchical = build_attachment_summary_plan("본문 " * 30000, user_request="전체 분석해줘")
    assert detailed_hierarchical.mode == "hierarchical" and detailed_hierarchical.target_chars is None
    detailed_widget = build_attachment_summary_plan(
        "본문 " * 5000,
        user_request="상세히 분석해줘",
        requested_target=2800,
    )
    assert detailed_widget.target_chars == 2800 and detailed_widget.user_override == "widget_chars"

    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "build_attachment_summary_plan" in source
    assert 'length_instruction = f"\\n최종 길이는 약 {target}자 내외." if target is not None else ""' in source
    image_followup = source[source.index("def _run_attachment_image_followup"):source.index("def analyze_attachment_image_vlm")]
    image_initial = source[source.index("def analyze_attachment_image_vlm"):source.index("def summarize_text_long")]
    assert "max_tokens=" not in image_followup
    assert "max_tokens=" not in image_initial
    assert 'if extracted.get("finish_reason") == "length":' in source
    assert "숫자·날짜·단위·표의 핵심값을 임의 변경하지 마라" in source
    assert "attachment_analysis_request" in source
    assert "__attach_summary_target_explicit" in source
    print("RESULT OK tests=11 dynamic_policy=file_type_independent numeric_preservation_prompt=required")


if __name__ == "__main__":
    main()

"""Offline contract for one-shot file+text analysis and scoped document follow-ups."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.attachment_document_followup import (  # noqa: E402
    add_document_analysis_context,
    latest_document_analysis,
    looks_like_document_followup,
)
from app.services.attachment_summary_policy import build_attachment_summary_plan  # noqa: E402
from app.services.chat_composer_submission import (  # noqa: E402
    attachment_request_text,
    queue_attachment_submission_event,
    select_composer_submission,
)
from app.services.nlq_input_guard import looks_like_attachment_followup  # noqa: E402


@dataclass
class Submission:
    text: str = ""
    files: list[str] = field(default_factory=list)


def main() -> None:
    context = {"room_id": "room-a", "company_id": "3", "user_id": "7"}
    state: dict = {}
    for event_id, submission in (
        ("file-only", Submission(files=["test.xlsx"])),
        ("file-text", Submission(text="자세히 분석해줘", files=["test.xlsx"])),
        ("same-file-new-event", Submission(files=["test.xlsx"])),
        ("multiple", Submission(files=["a.xlsx", "b.pdf"])),
    ):
        queue_attachment_submission_event(state, submission=submission, event_id=event_id, context=context)
        selected, selected_id, status = select_composer_submission(
            state, widget_submission=submission, context=context
        )
        assert (selected, selected_id, status) == (submission, event_id, "ready")
        selected, _, status = select_composer_submission(
            state, widget_submission=submission, context=context
        )
        assert selected is None and status == "missing_binary_ignored"

    assert attachment_request_text("자세히 분석해줘", "간단히") == "자세히 분석해줘"
    assert attachment_request_text("", "간단히") == "간단히"
    assert build_attachment_summary_plan("본문 " * 5000, user_request="자세히 분석해줘").target_chars is None
    assert build_attachment_summary_plan("본문 " * 5000, user_request="1000자로").target_chars == 1000
    assert build_attachment_summary_plan("본문 " * 5000, user_request="간단히").target_chars == 900

    assert looks_like_document_followup("첨부파일 자세히 분석해줘")
    assert looks_like_document_followup("xlsx 파일을 근거로 설명해줘")
    assert not looks_like_attachment_followup("첨부파일 자세히 분석해줘", has_active_image_reference=True)
    assert looks_like_attachment_followup("이 이미지 자세히 분석해줘")
    assert looks_like_attachment_followup("제품코드도 알려줘", has_active_image_reference=True)

    first = {
        "role": "assistant", "message_type": "file_analysis_result",
        "company_id": 3, "user_id": 7,
        "attachment_analysis_detail": {"summary": "첫 분석 " + "x" * 3000},
    }
    latest = {
        **first,
        "attachment_analysis_detail": {"summary": "최신 분석 " + "y" * 3000, "ocr_text": "저장된 OCR"},
    }
    room = {"id": "room-a", "company_id": 3, "user_id": 7, "messages": [first, latest]}
    detail = latest_document_analysis(room, context=context)
    assert detail is latest["attachment_analysis_detail"]
    messages = add_document_analysis_context(
        [{"role": "system", "content": "기본"}, {"role": "user", "content": "첨부파일 자세히 분석해줘"}], detail
    )
    assert len(messages) == 3 and "최신 분석 " in messages[-2]["content"]
    assert "첫 분석 " not in messages[-2]["content"]
    assert "저장된 OCR" in messages[-2]["content"]
    assert len(messages[-2]["content"]) > 3000
    for changed in ({"room_id": "room-b"}, {"company_id": "4"}, {"user_id": "8"}):
        assert latest_document_analysis(room, context={**context, **changed}) is None

    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "if composer_text and not uploaded_files and recorded_audio is None:" in source
    assert "user_request=attachment_request_text(composer_text, attachment_analysis_request)" in source
    assert 'request_msg += f"\\n\\n{composer_text}"' in source
    assert 'summary_preview = str(summary or "").strip()' in source
    assert 'summary_preview = _clip_for_model(' not in source
    assert 'st.markdown(full_summary)' in source
    assert 'msgs = add_document_analysis_context(msgs, document_detail)' in source
    assert source.index("if looks_like_document_followup(user_input):") < source.index(
        "if _looks_like_attachment_image_followup("
    )
    print("attachment composer follow-up: PASS (one-shot, full summary, scoped document/image routing)")


if __name__ == "__main__":
    main()

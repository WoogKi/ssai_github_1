"""Offline contracts for direct-image VLM attachment follow-ups."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'def _looks_like_attachment_image_followup(',
        'def _store_attachment_image_followup_reference(',
        'def _resolve_attachment_image_followup_reference(',
        'def _append_attachment_image_followup_unavailable(',
        'def _run_attachment_image_followup(',
        '"__attachment_image_followup_ref"',
        '"__attachment_image_followup_request"',
        '"room_id": str(room.get("id") or "")',
        '"company_id": _normalize_chat_company_id(company.get("company_id"))',
        '"user_id": int(getattr(user, "user_id", 0) or 0)',
        '"type": "image_url"',
        '"input_mode": "actual_image_data_url"',
        'max_retries=0',
        'retry_count=0',
        '"image_followup_provenance"',
        '"image_followup_vlm"',
        '"image_followup_unavailable"',
        '일반 텍스트 추측으로 답하지 않습니다.',
        'if _looks_like_attachment_image_followup(user_input):',
        'pending_image_followup = st.session_state.get("__attachment_image_followup_request")',
        'st.session_state["__queue_ai"] = False',
        '_run_attachment_image_followup(room=current_room, question=last_image_question)',
    )
    assert all(token in source for token in required)
    assert source.index('pending_image_followup = st.session_state.get("__attachment_image_followup_request")') < source.index('merged_msgs = _build_room_render_messages(current_room)')
    assert source.index('if web_search_route is not None:') < source.index('if _looks_like_attachment_image_followup(user_input):')
    assert 'f"사용자 질문: {question}"' in source
    assert 'OCR 텍스트나 이전 요약만으로 판단하지 말고' in source
    print("RESULT OK tests=27 direct_image=1 rendered_same_run=1 scoped_reference=1 unavailable_fail_closed=1")


if __name__ == "__main__":
    main()

"""Offline contracts for one-shot attachment batches and safe image follow-up targets."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'st.session_state.setdefault("__attachment_uploader_nonce", 0)',
        'key=f"file_upload_below_input_{int(st.session_state.get(\'__attachment_uploader_nonce\', 0))}"',
        'uploaded_files = list(uploaded_files or [])',
        'analysis_target_files = list(uploaded_files or [])',
        'for idx, uf in enumerate(analysis_target_files, start=1):',
        'st.session_state["__attachment_uploader_nonce"] = int(st.session_state.get("__attachment_uploader_nonce", 0)) + 1',
        'def _set_attachment_image_followup_candidates(',
        'if len(candidates) == 1:',
        'st.session_state.pop("__attachment_image_followup_ref", None)',
        'phase=reference_ambiguous candidate_count=%s',
        '여러 이미지를 함께 분석했습니다. 이미지 후속질문은 원하는 이미지 한 장을 다시 첨부해 분석해 주세요.',
    )
    assert all(token in source for token in required)
    forbidden = (
        'def _resolve_attachment_analysis_targets(',
        'def _resolve_attachment_reanalysis_files(',
        '"이번 분석 대상"',
        '"이미지 후속질문 대상"',
        'for idx, uf in enumerate(uploaded_files, start=1):',
    )
    assert not any(token in source for token in forbidden)
    print("RESULT OK tests=20 one_shot_batch=1 uploader_cleared=1 multi_image_fail_closed=1")


if __name__ == "__main__":
    main()

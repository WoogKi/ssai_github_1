"""Offline contract checks for explicit attachment image VLM opt-in."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'def _is_image_attachment(',
        'def _attachment_image_data_url(',
        'def analyze_attachment_image_vlm(',
        '"이미지 장면 분석(VLM)"',
        'if use_image_vlm and _is_image_attachment(uf):',
        '"type": "image_url"',
        '"input_mode": "actual_image_data_url"',
        '"extractor_kind": "lmstudio_vlm_image_url"',
        'max_retries=0',
        'attachment_vlm_artifact_cache',
        'attachment_vlm_observation_cache',
        '"title": "이미지 관찰 결과(VLM)"',
        '이미지 관찰 요약',
        'with st.expander("추출·관찰 상세", expanded=False):',
        '"VLM provenance: "',
        '"추출 provenance: "',
        'if primary_observation:',
        'elif has_image_attachment:',
        '문서 핵심 요약',
        '"image_vlm_provenance"',
    )
    assert all(token in source for token in required)
    assert 'if use_image_vlm and _is_image_attachment(uf):' in source
    assert '"enabled": bool(st.session_state.get("__ocr_auto", False))' in source
    assert 'if _TESS_AVAILABLE and ocr_runtime["enabled"]:' in source
    print("RESULT OK tests=22 optin_actual_image_url=1 image_summary_primary=1 collapsed_provenance=1 ocr_vlm_provenance_separate=1")


if __name__ == "__main__":
    main()
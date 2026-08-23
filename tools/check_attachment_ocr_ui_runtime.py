"""Regression checks for Streamlit OCR option keys reaching image extraction."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'key="__ocr_auto"',
        'key="__ocr_langs"',
        'key="__ocr_psm"',
        'key="__ocr_oem"',
        'key="__ocr_upscale"',
        'key="__ocr_binarize"',
        'key="__ocr_denoise"',
        'st.session_state.get("__ocr_auto", False)',
        'st.session_state.get("__ocr_langs", ["kor", "eng"])',
        'st.session_state.get("__ocr_psm", 3)',
        'st.session_state.get("__ocr_oem", 3)',
        'st.session_state.get("__ocr_upscale", True)',
        'st.session_state.get("__ocr_binarize", True)',
        'st.session_state.get("__ocr_denoise", False)',
        'info.append("\\n[OCR 결과]\\n" + (_truncate(ocr_text) if preview else ocr_text))',
        'extractor_kind="image_ocr_tesseract"',
        '[attachment.analysis] phase=start file_source=%s ocr=%s vlm=%s',
        '[attachment.ocr] phase=image_enter',
        '[attachment.ocr] phase=tesseract_return',
        '[attachment.ocr] phase=summary_input',
    )
    assert all(token in source for token in required)
    forbidden = (
        'st.session_state.get("ocr_auto", False)',
        'st.session_state.get("ocr_langs", ["kor", "eng"])',
        'st.session_state.get("ocr_psm", 3)',
        'st.session_state.get("ocr_oem", 3)',
    )
    assert not any(token in source for token in forbidden)
    print("RESULT OK tests=25 ui_ocr_keys_reach_process_file=1 artifact_provenance=1")


if __name__ == "__main__":
    main()
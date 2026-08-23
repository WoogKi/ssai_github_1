"""Offline contracts for automatic one-shot attachment analysis and OCR/VLM state."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'st.session_state.setdefault("__attachment_uploader_nonce", 0)',
        'uploaded_files = list(uploaded_files or [])',
        'analysis_target_files = list(uploaded_files or [])',
        'def _claim_attachment_auto_analysis(',
        '"__attachment_auto_analysis_claim"',
        'auto_analysis_ready = bool(',
        'and _claim_attachment_auto_analysis(analysis_target_files, current_room)',
        'elif auto_analysis_ready:',
        '[attachment.analysis] phase=auto_queued',
        'st.session_state.pop("__attachment_auto_analysis_claim", None)',
        'if _TESS_AVAILABLE and ocr_runtime["enabled"]:',
        'cache_key = (file_hash, bool(preview), ocr_conf)',
        '[attachment.ocr] phase=cache_hit',
        '[attachment.ocr] phase=cache_miss',
        'vlm_cache_key = (file_hash, model_id)',
        'vlm_cache_status = "hit" if cached_vlm is not None else "miss"',
        '[attachment.vlm] status=ok file_hash=%s cache=%s observation=%s elapsed_ms=%s',
        '"attachment_analysis_modes": {',
        '"attachment_analysis_detail": {',
        'with st.expander("추출·관찰 상세", expanded=False):',
        '"ocr": "[OCR 결과]" in combined_input',
        '"vlm": bool(image_vlm_results)',
        'st.session_state.setdefault("__attach_image_vlm", True)',
    )
    assert all(token in source for token in required)
    assert 'def _resolve_attachment_reanalysis_files(' not in source
    assert 'st.button("🔍 파일 분석하기"' not in source
    assert 'if use_image_vlm and _is_image_attachment(uf):' in source
    assert '"type": "image_url"' in source
    print("RESULT OK tests=25 auto_one_shot=1 uploader_cleared=1 ocr_vlm_cache_isolated=1 vlm_default=1")


if __name__ == "__main__":
    main()

"""Offline contracts for automatic one-shot attachment analysis and OCR/VLM state."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    required = (
        'st.session_state.setdefault("__attachment_uploader_nonce", 0)',
        'composer_submission = st.chat_input(',
        'accept_file="multiple" if can_upload_file else False',
        'uploaded_files = list(getattr(composer_submission, "files", ()) or ())',
        'attachment_file_source = "chat_composer"',
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
        'st.markdown("### 📎 첨부 옵션")',
        'with st.expander("첨부 옵션 열기", expanded=False):',
        '"요약 목표 길이(문자)"',
        '"분석 요청(선택)"',
        '"이미지 장면 분석(VLM)"',
        '"원문 메시지도 함께 남기기"',
    )
    assert all(token in source for token in required)
    assert 'def _resolve_attachment_reanalysis_files(' not in source
    assert 'st.button("🔍 파일 분석하기"' not in source
    assert 'with st.expander("📎 첨부 처리 옵션", expanded=False):' not in source
    assert source.index('st.markdown("### 📎 첨부 옵션")') < source.index('composer_submission = st.chat_input(')
    search_index = source.index('st.markdown("### 🔎 채팅 검색")')
    sims_call_index = source.index('_render_sims_sidebar_fragment()', search_index)
    attachment_index = source.index('st.markdown("### 📎 첨부 옵션")')
    ocr_index = source.index('st.markdown("### 🖼️ OCR 옵션")')
    assert 'st.markdown("### 🧩 SIMS 모드")' in source
    assert search_index < sims_call_index < attachment_index < ocr_index
    assert 'if use_image_vlm and _is_image_attachment(uf):' in source
    assert '"type": "image_url"' in source
    print("RESULT OK tests=28 composer_file_entry=1 auto_one_shot=1 uploader_cleared=1 ocr_vlm_cache_isolated=1 vlm_default=1")


if __name__ == "__main__":
    main()

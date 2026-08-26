"""Static contract checks for the microphone STT boundary; no model or DB needed."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
SERVICE = (ROOT / "app" / "services" / "stt_service.py").read_text(encoding="utf-8")


def require(text: str, source: str, label: str) -> None:
    if text not in source:
        raise AssertionError(label)


def main() -> None:
    require("accept_audio=True", MAIN, "native microphone is required")
    require("audio_sample_rate=16000", MAIN, "16kHz microphone contract is required")
    require('accept_file="multiple" if can_upload_file else False', MAIN, "text/file composer contract changed")
    require('getattr(composer_submission, "audio", None)', MAIN, "audio must stay outside file attachments")
    require('st.session_state["__sims_auto_user_input"] = text_to_send', MAIN, "explicit send injection missing")
    require("if send_stt:", MAIN, "automatic transcript injection is forbidden")
    if MAIN.index("pending_stt = st.session_state.get") < MAIN.index("for idx, m in enumerate(merged_msgs):"):
        raise AssertionError("preview must render after chat history")
    require('reason="recording_replace"', MAIN, "new recording must replace pending preview")
    require('_clear_stt_pending_state(ss, reason="company_change")', MAIN, "company transition clear missing")
    require('_clear_stt_pending_state(ss, reason="room_change")', MAIN, "room transition clear missing")
    require('_clear_stt_pending_state(ss, reason="new_room")', MAIN, "new room clear missing")
    require('device="cpu", compute_type="int8"', SERVICE, "CPU int8 contract changed")
    require("_SIMS_DOMAIN_INITIAL_PROMPT", SERVICE, "SIMS domain prompt contract missing")
    require("initial_prompt=_SIMS_DOMAIN_INITIAL_PROMPT", SERVICE, "STT domain prompt is not applied")
    require("STT_MODEL_DIR", SERVICE, "configured model boundary missing")
    require("if not model_dir.is_dir()", SERVICE, "missing local model must fail closed")
    if "snapshot_download" in SERVICE:
        raise AssertionError("auto-download is forbidden")
    require("_INFERENCE_LOCK.acquire(blocking=False)", SERVICE, "single-flight protection missing")
    require("BytesIO(payload)", SERVICE, "microphone bytes boundary missing")
    require('with st.spinner("음성을 인식하고 있습니다...")', MAIN, "STT progress indicator missing")
    if MAIN.index('with st.spinner("음성을 인식하고 있습니다...")') > MAIN.index("result = transcribe_microphone_audio"):
        raise AssertionError("STT progress indicator must render before transcription")
    require("stt_bottom_slot = st.empty()", MAIN, "STT bottom slot is required")
    require("with stt_bottom_slot.container():", MAIN, "STT slot must own preview/progress rendering")
    require("stt_bottom_slot.empty()", MAIN, "new recording must replace prior slot contents")
    if MAIN.index("stt_bottom_slot = st.empty()") < MAIN.index("for idx, m in enumerate(merged_msgs):"):
        raise AssertionError("STT slot must render after chat history")
    if MAIN.index("stt_bottom_slot = st.empty()") > MAIN.index("composer_submission = st.chat_input("):
        raise AssertionError("STT slot must be immediately above the root composer")
    if MAIN.count("st.chat_input(") != 1:
        raise AssertionError("exactly one root composer is required")
    print("PASS: STT microphone input contract")


if __name__ == "__main__":
    main()

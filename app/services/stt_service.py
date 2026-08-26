"""Local faster-whisper boundary for microphone transcription.

This module deliberately has no Streamlit dependency and never downloads a
model. The caller owns UI confirmation and chat routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging
import os
from pathlib import Path
from threading import Lock
import time
from typing import BinaryIO


_LOG = logging.getLogger("ssai.stt")
_DEFAULT_MODEL_DIR = r"C:\SSAI_DATA\models\stt\whisper-small"
_SIMS_DOMAIN_INITIAL_PROMPT = "SIMS 업무 용어: 재고, 현재고, 입고, 출고, 출고빈도, 제품재고장, SIMS 일일점검."
_MODEL_LOCK = Lock()
_INFERENCE_LOCK = Lock()
_MODEL = None


class STTServiceError(RuntimeError):
    """Base class for safe, user-facing STT failures."""


class STTUnavailableError(STTServiceError):
    pass


class STTBusyError(STTServiceError):
    pass


class STTUnsupportedAudioError(STTServiceError):
    pass


class STTInferenceError(STTServiceError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    detected_language: str
    language_probability: float | None
    segment_count: int
    elapsed_ms: int
    audio_duration_s: float | None


def configured_model_dir() -> Path:
    """Return the configured local model directory without creating it."""
    raw_path = str(os.getenv("STT_MODEL_DIR") or _DEFAULT_MODEL_DIR).strip()
    return Path(raw_path)


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        model_dir = configured_model_dir()
        if not model_dir.is_dir():
            raise STTUnavailableError("음성 인식 모델을 준비하지 못했습니다.")
        try:
            from faster_whisper import WhisperModel

            _MODEL = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
        except Exception as exc:
            _LOG.warning("[stt.model] load_failed error_class=%s", type(exc).__name__)
            raise STTUnavailableError("음성 인식 모델을 불러오지 못했습니다.") from exc
        _LOG.info("[stt.model] load_success device=cpu compute_type=int8")
        return _MODEL


def _read_audio_bytes(audio: bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(audio, (bytes, bytearray)):
        payload = bytes(audio)
    elif hasattr(audio, "read"):
        payload = audio.read()
    else:
        raise STTUnsupportedAudioError("녹음 데이터를 읽을 수 없습니다.")
    if not payload:
        raise STTUnsupportedAudioError("녹음 데이터가 비어 있습니다.")
    return payload


def _audio_duration_s(payload: bytes) -> float | None:
    try:
        import av

        with av.open(BytesIO(payload), mode="r") as container:
            if container.duration is not None:
                return round(float(container.duration / av.time_base), 3)
    except Exception:
        return None
    return None


def transcribe_microphone_audio(audio: bytes | bytearray | BinaryIO) -> TranscriptionResult:
    """Transcribe one microphone payload without retry or persistence."""
    payload = _read_audio_bytes(audio)
    if not _INFERENCE_LOCK.acquire(blocking=False):
        raise STTBusyError("다른 음성 인식 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요.")

    started = time.perf_counter()
    try:
        model = _load_model()
        try:
            segments, info = model.transcribe(
                BytesIO(payload),
                initial_prompt=_SIMS_DOMAIN_INITIAL_PROMPT,
            )
            values = list(segments)
        except Exception as exc:
            _LOG.warning("[stt.transcribe] failed error_class=%s", type(exc).__name__)
            raise STTInferenceError("음성을 인식하지 못했습니다. 다시 녹음해 주세요.") from exc

        text = "".join(str(segment.text or "") for segment in values).strip()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = TranscriptionResult(
            text=text,
            detected_language=str(getattr(info, "language", "") or ""),
            language_probability=getattr(info, "language_probability", None),
            segment_count=len(values),
            elapsed_ms=elapsed_ms,
            audio_duration_s=_audio_duration_s(payload),
        )
        _LOG.info(
            "[stt.transcribe] success elapsed_ms=%s language=%s segment_count=%s empty=%s",
            result.elapsed_ms,
            result.detected_language,
            result.segment_count,
            not bool(result.text),
        )
        return result
    finally:
        _INFERENCE_LOCK.release()

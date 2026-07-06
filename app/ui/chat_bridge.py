"""
# app/ui/chat_bridge.py

chat_bridge.py (compat shim)

✅ 단일 진실 소스(SSOT): app/ui/chat_middleware.py
이 파일은 과거 import 경로 호환용이며, 모든 기능은 chat_middleware로 위임한다.
레거시 구현(표-버블/컨텍스트/인박스 처리)은 두지 않는다.
"""
from __future__ import annotations

from typing import Any, Optional
import streamlit as st
import logging
log = logging.getLogger("ssai")

from app.ui.chat_middleware import (
    KEY_SIMS_CTX,
    wire_chat_context,
    drain_inbox_to_chat,
    push_sims_result_to_chat,
    render_sims_context_controls,
)

__all__ = [
    "KEY_SIMS_CTX",
    "wire_chat_context",
    "drain_inbox_to_chat",
    "push_sims_result_to_chat",
    "render_sims_context_controls",
    "get_sims_context_text",
    "get_sims_context_data",
    "clear_sims_context",
]
# ─────────────────────────────────────────────────────────────
# 채팅 입력 파이프라인에서 사용할 헬퍼
def get_sims_context_text(*args, **kwargs) -> str | None:
    """
    최신 SIMS 컨텍스트 텍스트(안내 버블용)를 반환.
    - 실제 저장/갱신은 chat_middleware가 담당(SSOT).
    - ✅ 호출부 호환: get_sims_context_text(max_age_sec=900) 등 키워드 인자 허용
    """
    wire_chat_context()
    ss = st.session_state

    # max_age_sec 등은 현재 세션 기반 구현에서는 사용하지 않지만,
    # 호출부 호환을 위해 받아만 둔다.
    _ = kwargs.get("max_age_sec", kwargs.get("ttl_sec", kwargs.get("max_age", None)))

    txt = ss.get("__sims_context_text")
    if isinstance(txt, str) and txt.strip():
        return txt

    ctx = ss.get(KEY_SIMS_CTX) or ss.get("__sims_ctx") or ss.get("__sims_context")
    if isinstance(ctx, dict):
        # chat_middleware가 preview/text/context_text 등 다양한 키로 넣을 수 있어 방어적으로 처리
        t2 = ctx.get("text") or ctx.get("preview") or ctx.get("context_text")
        if isinstance(t2, str) and t2.strip():
            return t2
    return None

def get_sims_context_data(max_age_sec: int = 300) -> Any:
    """
    ✅ 호환용: SIMS 컨텍스트 원본(dict/obj)을 반환
    - SSOT는 chat_middleware 세션키를 사용한다.
    - Lmstudio_SSAI_chat_main.py의 컨텍스트 주입(import) 호환 목적
    """
    wire_chat_context()
    ss = st.session_state
    ctx = ss.get(KEY_SIMS_CTX) or ss.get("__sims_ctx") or ss.get("__sims_context")
    return ctx


def clear_sims_context() -> None:
    """SIMS 컨텍스트를 제거한다(닫기)."""
    wire_chat_context()
    ss = st.session_state
    for k in (KEY_SIMS_CTX, "__sims_ctx", "__sims_context", "__sims_context_text", "__sims_context_obj"):
        if k in ss:
            del ss[k]
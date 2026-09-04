from __future__ import annotations

import json
import logging
import time
import uuid
from functools import partial
from typing import Any, Mapping, MutableMapping

import streamlit as st

from app.services.chat_feedback_service import (
    DISLIKE_REASON_CODES,
    DISLIKE_REASON_LABELS,
    ChatFeedbackAccessError,
    ChatFeedbackError,
    bounded_visible_answer_summary,
    read_chat_feedback,
    update_chat_feedback,
)

log = logging.getLogger("ssai")

_RENDERED_CONTROL_IDS_KEY = "__chat_feedback_control_ids_this_run"
_SYSTEM_MESSAGE_TYPES = frozenset({"company_change", "system_notice"})


def reset_chat_message_control_registry(session_state: MutableMapping[str, Any]) -> None:
    session_state[_RENDERED_CONTROL_IDS_KEY] = set()


def is_feedback_eligible_message(message: Mapping[str, Any]) -> bool:
    if str(message.get("role") or "").strip().lower() != "assistant":
        return False
    meta = message.get("meta") if isinstance(message.get("meta"), Mapping) else {}
    message_type = str(
        message.get("type")
        or meta.get("message_type")
        or message.get("message_type")
        or ""
    ).strip().lower()
    if message_type in _SYSTEM_MESSAGE_TYPES:
        return False
    try:
        uuid.UUID(str(message.get("id") or "").strip())
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def claim_chat_message_controls(
    session_state: MutableMapping[str, Any],
    message: Mapping[str, Any],
) -> bool:
    if not is_feedback_eligible_message(message):
        return False
    message_id = str(message.get("id") or "").strip()
    rendered = session_state.setdefault(_RENDERED_CONTROL_IDS_KEY, set())
    if not isinstance(rendered, set):
        rendered = set(rendered or ())
        session_state[_RENDERED_CONTROL_IDS_KEY] = rendered
    if message_id in rendered:
        return False
    rendered.add(message_id)
    return True


def bounded_copy_text(message: Mapping[str, Any], *, max_chars: int = 12_000) -> str:
    """Copy visible structured-result metadata without copying a table payload."""
    return bounded_visible_answer_summary(message, max_chars=max_chars)


def _render_copy_button(message: Mapping[str, Any]) -> None:
    text = bounded_copy_text(message)
    if not text:
        return
    element_id = f"chat-copy-{str(message.get('id') or '').replace('-', '')}"
    encoded_text = json.dumps(text, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    encoded_id = json.dumps(element_id)
    st.html(
        f"""
        <button id={encoded_id} type="button" title="답변 복사" aria-label="답변 복사"
          style="display:inline-flex;align-items:center;justify-content:center;width:30px;height:28px;
                 border:0;border-radius:8px;background:transparent;
                 color:#4b5563;cursor:pointer;padding:0;line-height:1;">
          <span aria-hidden="true" style="position:relative;display:block;width:18px;height:19px;">
            <span style="position:absolute;left:1px;top:1px;width:12px;height:14px;border:1.8px solid currentColor;border-radius:2px;"></span>
            <span style="position:absolute;left:5px;top:4px;width:12px;height:14px;border:1.8px solid currentColor;border-radius:2px;background:var(--background-color,#fff);"></span>
          </span>
        </button>
        <script>
          (() => {{
            const button = document.getElementById({encoded_id});
            if (!button) return;
            button.addEventListener("mouseenter", () => {{
              button.style.background = "rgba(49,51,63,0.07)";
            }});
            button.addEventListener("mouseleave", () => {{
              button.style.background = "transparent";
            }});
            button.addEventListener("click", async () => {{
              try {{
                await navigator.clipboard.writeText({encoded_text});
                button.style.background = "rgba(22,163,74,0.08)";
                window.setTimeout(() => {{ button.style.background = "transparent"; }}, 900);
              }} catch (_) {{
                button.style.background = "rgba(220,38,38,0.08)";
                window.setTimeout(() => {{ button.style.background = "transparent"; }}, 900);
              }}
            }});
          }})();
        </script>
        """,
        width="content",
        unsafe_allow_javascript=True,
    )


def _save_feedback(
    *,
    room: Mapping[str, Any],
    message: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
    feedback: str | None,
    reason_code: str | None = None,
) -> bool:
    try:
        result = update_chat_feedback(
            room=room,
            message=message,
            user_id=user_id,
            company_id=company_id,
            feedback=feedback,
            reason_code=reason_code,
        )
    except (ChatFeedbackAccessError, ChatFeedbackError, ValueError, OSError):
        log.warning(
            "[chat.feedback] write_failed message_id=%s",
            str(message.get("id") or "")[:36],
        )
        st.session_state["__chat_feedback_notice"] = "답변 반응을 저장하지 못했습니다."
        return False
    return bool(result["write_count"])


def _save_selected_dislike_reason(
    *,
    room: Mapping[str, Any],
    message: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
    reason_key: str,
    saved_notice_key: str,
) -> None:
    selected = str(st.session_state.get(reason_key) or "").strip() or None
    if _save_feedback(
        room=room,
        message=message,
        user_id=user_id,
        company_id=company_id,
        feedback="dislike",
        reason_code=selected,
    ):
        st.session_state[saved_notice_key] = time.monotonic()


def _render_toolbar_button_style(message_key: str, *, selected_feedback: str) -> None:
    """Scope the compact toolbar style to this message's two feedback buttons."""
    selected_selector = {
        "like": (
            f"div.st-key-__chat_feedback_like_{message_key} button {{"
            "background:#e8f1ff !important;color:#3b82f6 !important;}"
        ),
        "dislike": (
            f"div.st-key-__chat_feedback_dislike_{message_key} button {{"
            "background:#fdebec !important;color:#ef4444 !important;}"
        ),
    }.get(selected_feedback, "")
    st.html(
        f"""
        <style>
          div.st-key-__chat_feedback_like_{message_key} button,
          div.st-key-__chat_feedback_dislike_{message_key} button {{
            min-width:30px !important;width:30px !important;height:28px !important;
            padding:0 !important;border:0 !important;
            border-radius:8px !important;background:transparent !important;
            color:#1f2937 !important;
          }}
          div.st-key-__chat_feedback_like_{message_key} button span,
          div.st-key-__chat_feedback_dislike_{message_key} button span {{
            font-size:20px !important;
          }}
          div.st-key-__chat_feedback_like_{message_key} button:hover,
          div.st-key-__chat_feedback_dislike_{message_key} button:hover {{
            background:#f3f6fb !important;
          }}
          div.st-key-__chat_feedback_reason_{message_key} [data-baseweb="select"] {{
            width:116px !important;
          }}
          div.st-key-__chat_feedback_reason_{message_key} [data-baseweb="select"] > div {{
            min-height:28px !important;height:28px !important;
            border:1px solid #c7d0dd !important;border-radius:8px !important;
            background:transparent !important;
          }}
          div.st-key-__chat_feedback_reason_{message_key} [data-baseweb="select"] input {{
            font-size:13px !important;
          }}
          {selected_selector}
        </style>
        """,
    )


@st.fragment
def _render_feedback_toolbar_fragment(
    message: Mapping[str, Any],
    *,
    room: Mapping[str, Any],
    user_id: Any,
    company_id: Any,
) -> None:
    """Keep feedback clicks inside a small fragment, not the whole chat/table."""
    try:
        current = read_chat_feedback(
            room=room,
            message=message,
            user_id=user_id,
            company_id=company_id,
        )
    except (ChatFeedbackAccessError, ChatFeedbackError, ValueError, OSError):
        log.debug(
            "[chat.feedback] control_blocked message_id=%s",
            str(message.get("id") or "")[:36],
        )
        return

    selected_feedback = str((current or {}).get("feedback") or "")
    selected_reason = (current or {}).get("reason_code") if selected_feedback == "dislike" else None
    message_key = str(message.get("id") or "").replace("-", "")
    _render_toolbar_button_style(message_key, selected_feedback=selected_feedback)
    # The first two cells reserve the sample toolbar's four-to-six pixel gaps.
    copy_col, like_col, dislike_col, reason_col, saved_col, spacer = st.columns(
        [0.42, 0.42, 0.38, 1.52, 0.72, 20],
        gap=None,
    )
    with copy_col:
        _render_copy_button(message)
    with like_col:
        st.button(
            ":material/thumb_up:",
            help="좋아요 선택됨" if selected_feedback == "like" else "좋아요",
            type="secondary",
            key=f"__chat_feedback_like_{message_key}",
            on_click=partial(
                _save_feedback,
                room=room,
                message=message,
                user_id=user_id,
                company_id=company_id,
                feedback=None if selected_feedback == "like" else "like",
            ),
        )
    with dislike_col:
        st.button(
            ":material/thumb_down:",
            help="싫어요 선택됨" if selected_feedback == "dislike" else "싫어요",
            type="secondary",
            key=f"__chat_feedback_dislike_{message_key}",
            on_click=partial(
                _save_feedback,
                room=room,
                message=message,
                user_id=user_id,
                company_id=company_id,
                feedback=None if selected_feedback == "dislike" else "dislike",
            ),
        )
    if selected_feedback == "dislike":
        with reason_col:
            reason_key = f"__chat_feedback_reason_{message_key}"
            saved_notice_key = f"__chat_feedback_reason_saved_{message_key}"
            reason_options = ["", *DISLIKE_REASON_CODES]
            st.selectbox(
                "사유 선택 (선택)",
                options=reason_options,
                index=reason_options.index(selected_reason) if selected_reason in DISLIKE_REASON_CODES else 0,
                format_func=lambda code: "사유 선택" if not code else DISLIKE_REASON_LABELS[code],
                key=reason_key,
                label_visibility="collapsed",
                width=116,
                on_change=partial(
                    _save_selected_dislike_reason,
                    room=room,
                    message=message,
                    user_id=user_id,
                    company_id=company_id,
                    reason_key=reason_key,
                    saved_notice_key=saved_notice_key,
                ),
            )
        if st.session_state.pop(saved_notice_key, None):
            with saved_col:
                st.html(
                    f"""
                    <style>
                      @keyframes feedback-saved-{message_key} {{
                        0%, 70% {{ opacity:1; }} 100% {{ opacity:0; }}
                      }}
                    </style>
                    <span style="display:inline-block;white-space:nowrap;padding-top:5px;font-size:12px;
                                 color:#16a34a;animation:feedback-saved-{message_key} 1.5s ease-out forwards;">
                      ✓ 저장됨
                    </span>
                    """
                )
    del spacer
    notice = st.session_state.pop("__chat_feedback_notice", "")
    if notice:
        st.toast(notice)


def render_chat_message_controls(
    message: Mapping[str, Any],
    *,
    room: Mapping[str, Any],
    current_user: Any,
    current_company_id: Any,
) -> bool:
    """Render one control footer for one canonical assistant message."""
    if not claim_chat_message_controls(st.session_state, message):
        return False
    user_id = getattr(current_user, "user_id", None)
    _render_feedback_toolbar_fragment(
        message,
        room=room,
        user_id=user_id,
        company_id=current_company_id,
    )
    return True


__all__ = [
    "claim_chat_message_controls",
    "bounded_copy_text",
    "is_feedback_eligible_message",
    "render_chat_message_controls",
    "reset_chat_message_control_registry",
]

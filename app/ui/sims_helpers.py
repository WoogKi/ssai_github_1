# app/ui/sims_helpers.py
from __future__ import annotations
import uuid
from contextlib import contextmanager
import streamlit as st
import io
import pandas as pd
from typing import List, Dict, Any


def _get_current_room():
    """현재 선택된 채팅방(dict) 반환. 없으면 None."""
    rid = st.session_state.get("current_room")
    for r in st.session_state.get("chat_rooms", []):
        if r.get("id") == rid:
            return r
    return None

_CHAT_KEYS: List[str] = [
    "chat_messages", "__chat_messages", "messages", "__messages",
    "__chat_store", "__chat_history"   # ← 호환 키 추가
]

def _append_markdown_fallback(df: pd.DataFrame, title: str) -> None:
    """
    rooms 렌더가 메타(table_key)를 인식 못 하는 경우 대비.
    ⚠️ LLM이 '샘플 CSV'로 오인하지 않도록 표/CSV 텍스트는 채팅에 넣지 않는다.
    """
    if not isinstance(df, pd.DataFrame):
        return
    md = f"**{title}** — SIMS 결과가 JSON 데이터 컨테이너로 LLM에 전달되었습니다."

    for k in _CHAT_KEYS:
        lst = st.session_state.get(k)
        if isinstance(lst, list):
            lst.append({"role": "assistant", "content": md})
            st.session_state[k] = lst  # 재할당로 변경 인지 보장
            st.session_state["__chat_has_new"] = True
            return
    # 후보 스토어가 없으면 임시 인박스에 쌓기(있으면 미들웨어가 드레인)
    inbox = st.session_state.setdefault("__chat_inbox", [])
    if not isinstance(inbox, list):
        inbox = []
        st.session_state["__chat_inbox"] = inbox
    inbox.append({"role": "assistant", "content": md})
    st.session_state["__chat_has_new"] = True

def push_sims_table_message(df, title: str = "SIMS 조회 결과"):
    """
    표(DataFrame)를 세션에 저장하고, 채팅 메시지(assistant)로 '표 버블'을 추가.
    - st.session_state["sims_tables"][key] = df
    - 메시지 메타: {"source":"SIMS","kind":"table","table_key":key}
    """
    # DF 보정(혹시 records/columns가 들어올 수도 있으니 안전 보정)
    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return

    key = f"tbl_{uuid.uuid4().hex[:8]}"
    sims_tables = st.session_state.setdefault("sims_tables", {})
    sims_tables[key] = df
    st.session_state["sims_tables"] = sims_tables  # 재할당(변경 인지 보장)
    st.session_state["__sims_flash"] = {
        "table_key": key, 
        "title": title,
        "action": st.session_state.get("__sims_selected_action") or st.session_state.get("__sims_action"),
        }

    # 현재 방 획득(없으면 생성)
    rooms: List[Dict[str, Any]] = st.session_state.setdefault("chat_rooms", [])
    room = _get_current_room()

    if room is None:
        new_room = {"id": str(uuid.uuid4()), "name": "새 대화", "messages": []}
        rooms = rooms + [new_room]  # 새 리스트로 재할당
        st.session_state["chat_rooms"] = rooms
        st.session_state["current_room"] = new_room["id"]
        room = new_room

    # 메시지 추가(rooms 리스트 안에 있는 dict를 갱신 → 리스트 재할당로 변경 인지)
    messages = room.setdefault("messages", [])
    messages = messages + [{
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": f"📊 {title}",
        "meta": {"source": "SIMS", "kind": "table", "table_key": key},
    }]
    room["messages"] = messages
    # rooms 안에서 해당 room을 찾아 교체 후 재할당
    new_rooms = []
    replaced = False
    for r in rooms:
        if r is room:
            new_rooms.append(room)
            replaced = True
        else:
            new_rooms.append(r)
    if not replaced:
        new_rooms.append(room)
    st.session_state["chat_rooms"] = new_rooms

    # 일반 채팅 스토어에도 마크다운 미러(어떤 렌더러든 즉시 보이게)
    _append_markdown_fallback(df, title)

    # 새 메시지 도착 알림 플래그(채팅 렌더에서 사용할 수 있음)
    st.session_state["__chat_has_new"] = True

    # (선택) 채팅 저장 함수가 있으면 호출
    try:
        from app.Lmstudio_SSAI_chat_main import save_chat_rooms  # 순환문제 없으면 OK
        if callable(save_chat_rooms):
            save_chat_rooms()
    except Exception:
        pass

# ── 옵션 A에서는 캡처 컨텍스트를 사용하지 않으므로 no-op으로 둡니다.
@contextmanager
def sims_capture_context(default_title: str = "SIMS 조회 결과"):
    yield

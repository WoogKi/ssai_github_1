# app/utils/sims_result.py
from __future__ import annotations
from typing import Any
import pandas as pd

def _pick_df(result: dict) -> pd.DataFrame | None:
    """result에서 DataFrame 하나 고르기 (없으면 None)."""
    if not isinstance(result, dict):
        return None
    v = result.get("df_display")
    if isinstance(v, pd.DataFrame):
        return v
    v = result.get("df_pretty")
    if isinstance(v, pd.DataFrame):
        return v
    v = result.get("df")
    if isinstance(v, pd.DataFrame):
        return v
    v = result.get("data")              # ← 뷰가 data=df만 주는 경우
    if isinstance(v, pd.DataFrame):
        return v    
    recs = result.get("records")
    if isinstance(recs, list) and len(recs) > 0:
        cols = result.get("columns") if isinstance(result.get("columns"), list) else None
        try:
            return pd.DataFrame(recs, columns=cols)
        except Exception:
            return None
    return None

def _is_final_result(result: Any) -> bool:
    """
    최종 결과인지 판별.
    - 명시 플래그: final=True 또는 ready=True → 최종
    - 표 데이터 존재: df_display/df_pretty/df 또는 records가 비어있지 않으면 최종
    - 중간 단계 힌트: need_input/in_progress 가 True면 비최종
    - 메시지(text/message/status)만 있어도 최종(안내문 표출용)
    """
    if result is None:
        return False
    if isinstance(result, dict):
        if result.get("need_input") or result.get("in_progress"):
            return False
        if result.get("final") is True or result.get("ready") is True:
            return True
        df = _pick_df(result)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return True
        recs = result.get("records")
        if isinstance(recs, list) and len(recs) > 0:
            return True
        if any(result.get(k) for k in ("text", "message", "status")):
            return True
        return False

    if isinstance(result, pd.DataFrame):
        return not result.empty
    if isinstance(result, (list, tuple)):
        return len(result) > 0
    # 기타 타입(문자열 등)은 내용이 있으면 최종으로 간주
    return True

def _normalize_result_for_chat(result: Any) -> Any:
    """
    채팅 버블/상단 미리보기에서 안전하게 표시되도록 최소 정규화.
    - 한글 컬럼명/값은 그대로 두고, DataFrame이면 인덱스 초기화
    """
    if isinstance(result, pd.DataFrame):
        return result.reset_index(drop=True)
    if isinstance(result, dict):
        # df_display 비었으면 records/df로부터 구성
        if not isinstance(result.get("df_display"), pd.DataFrame):
            df = _pick_df(result)
            if isinstance(df, pd.DataFrame):
                result["df_display"] = df
        return result
    return result

def push_sims_result_to_chat(result: Any, action: str | None = None) -> None:
    """
    채팅 상단(또는 메인)으로 결과를 넘기는 공통 훅.
    - __chat_inbox 에 미리보기 마크다운을 적재
    - __sims_context 저장 및 __sims_ctx_dirty=True 트리거
    - 채팅 렌더(별도)에서 drain_inbox_to_chat() / wire_chat_context()가 실행되어 rooms로 이동

    """
    try:
        import streamlit as st
    except Exception:
        return
    st.session_state["__sims_last_df"] = result
    st.session_state["__sims_last_action"] = action or ""
    # 정규화
    norm = _normalize_result_for_chat(result)

    # 미리보기 마크다운 생성
    title = (norm.get("title") if isinstance(norm, dict) else None) or (action or "SIMS 결과")
    lines = [f"### 📊 {title}"]
    msg = (norm.get("message") if isinstance(norm, dict) else None) \
          or (norm.get("status") if isinstance(norm, dict) else None) \
          or (norm.get("text") if isinstance(norm, dict) else None)
    if msg:
        lines.append(f"\n> {msg}\n")
    df = _pick_df(norm) if isinstance(norm, dict) else (norm if isinstance(norm, pd.DataFrame) else None)
    if isinstance(df, pd.DataFrame) and not df.empty:
        try:
            lines += ["\n", df.head(5).to_markdown(index=False), "\n", f"*…총 {len(df)}행*"]
        except Exception:
            pass
    md = "\n".join(lines)

    # 인박스 적재
    inbox = st.session_state.setdefault("__chat_inbox", [])
    inbox.append({"role": "assistant", "content": md, "meta": {"source": "SIMS", "kind": "result"}})
    st.session_state["__chat_inbox"] = inbox
    st.session_state["__chat_has_new"] = True

    # 컨텍스트 저장 + 트리거
    cols = []
    if isinstance(norm, dict):
        if isinstance(norm.get("columns"), list):
            cols = norm["columns"]
        elif isinstance(df, pd.DataFrame):
            cols = list(df.columns)
    elif isinstance(df, pd.DataFrame):
        cols = list(df.columns)
    st.session_state["__sims_context"] = {
        "origin": "SIMS",
        "action": action or (norm.get("action") if isinstance(norm, dict) else "") or "",
        "columns": cols,
    }
    st.session_state["__sims_ctx_dirty"] = True

    # 레거시 호환(원래 하던 저장도 유지)
    st.session_state["__sims_last_df"] = df if isinstance(df, pd.DataFrame) else norm
    st.session_state["__sims_last_action"] = action or (norm.get("action") if isinstance(norm, dict) else "") or ""
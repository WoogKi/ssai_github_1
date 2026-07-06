# app/ui/sims_hub.py
from __future__ import annotations

import re
from uuid import uuid4
import streamlit as st
import pandas as pd

import logging
log = logging.getLogger("ssai")

# 프로젝트 라우터(카테고리/액션/실행)
from app.sims.router import run_sims_action, all_categories, actions_of
# (선택) 채팅 푸시가 필요하면 사용
#from app.ui.chat_bridge import push_sims_result_to_chat

# 결과 보존용 세션 키
KEY_LAST_RESULT = "__sims_last_result"
KEY_LAST_ACTION = "__sims_last_action"
KEY_MSG         = "__sims_msg"
# ✅ 채팅으로 전달된 상태 여부를 표시하는 세션 키
KEY_ROUTED_TO_CHAT = "__sims_routed_to_chat"
# 사이드바에서 렌더 중임을 표시하는 키(메인 패널은 이 키가 True면 즉시 리턴)
KEY_IN_SIDEBAR = "__sims_in_sidebar"
KEY_RUN_FLAG   = "__sims_run_flag"
KEY_RUN_SEQ    = "__sims_run_seq"
KEY_LAST_KEYS  = "__sims_widget_keys"   # (있다면) 위젯키 정리용

st.session_state.setdefault(KEY_LAST_RESULT, None)
st.session_state.setdefault(KEY_MSG, "")

def _pick_df(d: dict) -> pd.DataFrame | None:
    """
    여러 후보 키 중 첫 번째 DataFrame을 안전하게 선택한다.
    주의) DataFrame을 `or`로 연결하면 불리언 평가가 발생해 ValueError가 나므로
         반드시 타입 체크로 골라야 한다.
    """
    if not isinstance(d, dict):
        return None
    for k in ("df_display", "df_pretty", "df"):
        v = d.get(k, None)
        if isinstance(v, pd.DataFrame):
            return v
    return None

def _is_final_result(result: dict | None) -> bool:
    if not result or not isinstance(result, dict):
        return False
    if result.get("ready") is True:
        return True
    if isinstance(result.get("df_display"), pd.DataFrame):
        return True
    if isinstance(result.get("df_pretty"), pd.DataFrame):
        return True
    if isinstance(result.get("df"), pd.DataFrame):
        return True
    if isinstance(result.get("df"), list) and len(result["df"]) > 0:
        return True
    if "df" in result and "columns" in result:
        return True
    return False

def _normalize_result_for_chat(result: dict) -> dict:
    if not isinstance(result, dict):
        return result
    df = result.get("df_display")
    if isinstance(df, pd.DataFrame):
        return result

    df = result.get("df_pretty") if isinstance(result.get("df_pretty"), pd.DataFrame) else None
    if df is None:
        if isinstance(result.get("df"), pd.DataFrame):
            df = result["df"]
        elif isinstance(result.get("df"), list):
            cols = result.get("columns")
            try:
                if isinstance(cols, list) and all(isinstance(x, str) for x in cols):
                    df = pd.DataFrame(result["df"], columns=cols)
                else:
                    df = pd.DataFrame(result["df"])
            except Exception:
                df = None
    if df is None:
        return result

    alias = (
        result.get("columns_alias")
        or result.get("labels")
        or result.get("headers_kor")
        or result.get("headers")
        or result.get("display_columns")
    )
    try:
        if isinstance(alias, dict):
            df = df.rename(columns={str(k): str(v) for k, v in alias.items()})
        elif isinstance(alias, list) and len(alias) == len(df.columns) and all(isinstance(x, str) for x in alias):
            df = df.set_axis([str(x) for x in alias], axis=1)
        elif isinstance(alias, list) and all(isinstance(x, dict) for x in alias):
            mapping = {}
            for item in alias:
                field = item.get("field") or item.get("name") or item.get("col")
                label = item.get("label") or item.get("title") or item.get("text")
                if field is not None and label is not None:
                    mapping[str(field)] = str(label)
            if mapping:
                df = df.rename(columns=mapping)
        else:
            cols = result.get("columns")
            if isinstance(cols, list) and len(cols) == len(df.columns):
                if [str(c) for c in cols] != [str(c) for c in df.columns]:
                    df = df.set_axis([str(x) for x in cols], axis=1)
    except Exception:
        pass

    result["df_display"] = df
    return result

def _slug(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s)
    return s or "x"

def render():
    st.subheader("🧩 SIMS Hub", divider="gray")

    # ⚙️ 초기화 및 자동 실행 방지
    if "__sims_initialized" not in st.session_state:
        st.session_state["__sims_initialized"] = True
        st.session_state["__sims_selected_action"] = None
        st.session_state["__sims_run_flag"] = False
        st.session_state["__sims_panel_active"] = False
        st.session_state["__sims_last_clicked"] = None
        st.session_state["__sims_first_render"] = True
    else:
        # 초기 렌더 이후에는 자동 실행 방지 플래그 해제
        st.session_state["__sims_first_render"] = False

    # 🧩 앱 시작 직후(초기 렌더)에는 SIMS 자동 실행 금지
    if st.session_state.get("__sims_first_render", False):
        st.session_state["__sims_selected_action"] = None
        st.session_state["__sims_run_flag"] = False
        st.session_state["__sims_panel_active"] = False

    # 유령 실행 방지: 버튼 누르기 전에는 실행 금지
    if not st.session_state.get("__sims_last_clicked"):
        st.session_state["__sims_run_flag"] = False

    # 좌: 액션/옵션/실행, 우: 결과
    col_left, col_right = st.columns([1, 2], gap="large")

    # == 좌측: 액션 선택 + 실행 ===========================================
    with col_left:
        cat = st.selectbox("카테고리", all_categories(), key="__sims_hub_cat")
        action = st.selectbox("작업 선택", actions_of(cat), key="__sims_hub_action")

        # 실행 트리거/상태 플래그
        st.session_state.setdefault("__sims_panel_active", False)
        st.session_state.setdefault("__sims_run_flag", False)
        def _run_from_hub():
            st.session_state["__sims_selected_action"] = action
            st.session_state["__sims_run_flag"] = True
            # 🔹 메인 패널이 새로운 실행임을 인식하도록 리셋 요청
            st.session_state["__sims_reset_requested"] = True
            st.session_state["__sims_was_final"] = False
            st.session_state["__sims_rendered"] = False
            st.session_state["__sims_panel_active"] = True
            st.session_state["__sims_last_clicked"] = str(action)
            log.info("[hub] button clicked -> run_flag=True, action=%r", action)

        # ✅ 버튼 클릭 이벤트 후 rerun 직전까지 유지 (연속 실행 가능)
        if st.session_state.get("__sims_run_flag") and not st.session_state.get("__sims_last_clicked"):
            st.session_state["__sims_run_flag"] = False

        st.button("▶ 실행", type="primary", key="__sims_hub_run", on_click=_run_from_hub)

        # (선택) 최근 액션 표시
        if st.session_state.get(KEY_LAST_ACTION):
            st.caption(f"최근 실행: {st.session_state[KEY_LAST_ACTION]}")

        # 컨텍스트 도구는 메인 패널에서만 노출 (사이드바 중복 표시 방지)


    # == 우측: 항상-렌더(마지막 결과를 지속 표시) ==========================
    with col_right:

        # ▼ 대시보드 갤러리: 실행 전, 혹은 결과가 없을 때 카테고리 내 모든 액션을 카드로 보여줌
        def _render_dashboard_gallery(selected_cat: str):
            """선택한 카테고리의 액션들을 카드 그리드(4열)로 보여줌."""
            try:
                acts = list(actions_of(selected_cat)) or []
            except Exception:
                acts = []
            if not acts:
                st.info("이 카테고리에 등록된 대시보드/작업이 없습니다.")
                return
            st.markdown("#### 📊 대시보드")
            cols = st.columns(4, gap="small")
            # 현재 렌더 사이클 식별자(버튼 key 유니크 보장용)
            call_id = st.session_state.setdefault("__sims_hub_gallery_id", 0) + 1
            st.session_state["__sims_hub_gallery_id"] = call_id
            for idx, a in enumerate(acts[:24]):  # 최대 24개 표시 (필요시 숫자 조정)
                label = str(a)
                kslug = _slug(label)
                with cols[idx % 4]:
                    # 카드 스타일: 버튼 하나로 간단히 구성 (유니크 key 보장)
                    if st.button(f"📈 {label}", key=f"dash_btn_{call_id}_{idx}_{kslug}"):
                        # 버튼 클릭 → 해당 액션 즉시 실행되도록 플래그 세팅
                        st.session_state["__sims_selected_action"] = a
                        st.session_state["__sims_run_flag"] = True
                        st.session_state["__sims_reset_requested"] = True
                        st.session_state["__sims_panel_active"] = True
                        # 여기서 st.rerun()은 호출하지 않음(버튼 클릭 자체가 rerun 트리거)
        # 실행 조건
        current_action = st.session_state.get("__sims_selected_action")
        # ✅ 초기 렌더에서 selectbox 기본값에 의해 자동 실행되지 않도록 hub_action 완전 무시
        if not current_action or not st.session_state.get("__sims_run_flag"):
            # 액션이 아직 선택되지 않았다면, 선택된 카테고리의 대시보드 갤러리를 먼저 보여줌
            _render_dashboard_gallery(st.session_state.get("__sims_hub_cat"))
            st.write("작업을 선택하거나 위의 **대시보드 카드** 중 하나를 클릭하세요.")
            return

        # 버튼에서 세팅된 실행플래그를 유지하여 연속 실행 가능
        run_flag = bool(st.session_state.get("__sims_run_flag"))
        panel_active = bool(st.session_state.get("__sims_panel_active"))

        # 두 번째 실행이 안 되는 현상 방지: 실행 완료 후 flag 재활성화
        # ✅ run_flag가 True인데 last_clicked가 None이면 다시 활성화
        if run_flag and not st.session_state.get("__sims_last_clicked"):
            st.session_state["__sims_last_clicked"] = st.session_state.get("__sims_selected_action")

            run_flag = True

            # ✅ 클릭 이력 복구 직후 panel_active도 True로 복구 (rerun 시 끊김 방지)
            st.session_state["__sims_panel_active"] = True

        if run_flag:
            st.session_state[KEY_ROUTED_TO_CHAT] = False

        log.info("[hub] decide-run: run_flag=%s, panel_active=%s, action=%r",
                 run_flag, panel_active, st.session_state.get("__sims_selected_action") or st.session_state.get("__sims_hub_action"))

        # 위젯 key 충돌 방지용 프리픽스 (옵션)
        call_id = st.session_state.setdefault("__sims_hub_call_id", 0) + 1
        st.session_state["__sims_hub_call_id"] = call_id
        prefix = f"__simshub_{call_id}_{_slug(current_action)}__"

        # (필요 시) 위젯 자동 key 패치 — 현재 허브에 폼이 없다면 생략돼도 무방
        orig_button = st.button
        def patched_button(label, *args, key=None, **kwargs):
            if key is None:
                key = f"{prefix}btn_{uuid4().hex[:6]}"
            return orig_button(label, *args, key=key, **kwargs)
        st.button = patched_button

        try:
            # 🔒 Hub 모드에선 '버튼 눌림(run_flag)'일 때만 실행합니다.
            if run_flag:
                with st.spinner("SIMS 실행 중…"):
                    log.info("[hub] RUN action=%r", current_action)
                    result = run_sims_action(current_action)
            else:
                # 버튼이 안 눌렸으면 절대 자동 실행하지 않음
                result = None
                # 패널 모드 잔존 상태로 인한 유령 실행을 차단
                if panel_active:
                    st.session_state["__sims_panel_active"] = False
                    log.info("[hub] suppress ghost run: panel_active->False")

        except Exception as e:
            # 에러를 예외창으로 보여주고, 상단 메시지에도 남김
            st.exception(e)
            st.session_state[KEY_MSG] = f"오류: {type(e).__name__}: {e}"
            result = None
        finally:
            st.button = orig_button

        # 최종 결과 보존 (rerun/즉시 초기화 금지)
        if _is_final_result(result):
            try:
                result = _normalize_result_for_chat(result)
            except Exception:
                pass
            # (선택) 채팅 푸시
            # push_sims_result_to_chat(result, current_action)
            st.session_state[KEY_LAST_RESULT] = result
            st.session_state[KEY_LAST_ACTION] = current_action
            st.session_state[KEY_MSG] = "실행 완료"

            # ✅ 루프 정상 종료 후 다음 실행 허용
            st.session_state.update({
                "__sims_panel_active": False,
                "__sims_last_clicked": None,
                "__sims_run_flag": True,   # 다음 실행 가능하게 유지
            })
            log.info("[hub] run completed -> flags ready for next run")

        # 항상-렌더: 마지막 결과 표시
        last = st.session_state.get(KEY_LAST_RESULT)
        if isinstance(last, dict):
            # 채팅으로 라우팅된 경우(=타임라인에 이미 알림이 떠 있음)엔 패널 안내는 숨김
            if st.session_state.get(KEY_MSG) and not st.session_state.get(KEY_ROUTED_TO_CHAT, False):
                st.info(st.session_state[KEY_MSG])
            # DataFrame을 불리언 평가 없이 안전하게 선택
            df = _pick_df(last)
            if isinstance(df, pd.DataFrame) and not df.empty:
                st.dataframe(df, use_container_width=True, height=520)
                return
            recs = last.get("records")
            cols = last.get("columns")
            if isinstance(recs, list):
                try:
                    _df = pd.DataFrame(recs, columns=cols if isinstance(cols, list) else None)
                    st.dataframe(_df, use_container_width=True, height=520)
                    return
                except Exception:
                    pass

        # 결과가 아직 없다면 다시 갤러리를 보여줌(사용자가 바로 다른 대시보드를 선택 가능)
        _render_dashboard_gallery(st.session_state.get("__sims_hub_cat"))
        st.write("표시할 데이터가 없습니다. 조건을 선택하거나 **대시보드 카드**를 클릭하세요.")

    # ===================================================================================
    # 🔧 디버그 스냅샷
    if st.checkbox("🔧 SIMS 디버그 보기", key="__sims_dbg_hub"):
        last = st.session_state.get(KEY_LAST_RESULT)
        st.write("• LAST_ACTION:", st.session_state.get(KEY_LAST_ACTION))
        st.write("• LAST_RESULT 타입:", type(last).__name__)
        if isinstance(last, dict):
            st.write("• LAST_RESULT keys:", list(last.keys()))
            df1 = last.get("df_display"); df2 = last.get("df_pretty"); df3 = last.get("df")
            st.write("• DF 존재 여부:", {
                "df_display": isinstance(df1, pd.DataFrame) and not df1.empty,
                "df_pretty":  isinstance(df2, pd.DataFrame) and not df2.empty,
                "df":         isinstance(df3, pd.DataFrame) and not df3.empty,
                "records":    isinstance(last.get("records"), list),
                "columns":    isinstance(last.get("columns"), list),
            })
        chat_keys = ["chat_messages", "__chat_messages", "messages", "__messages", "__chat_inbox", "__sims_context"]
        st.write("• 채팅/컨텍스트 키 존재:", {k: type(st.session_state.get(k)).__name__ for k in chat_keys if k in st.session_state})

# (레거시 호환) 예전 코드가 render_sims_hub 를 임포트하는 경우를 위한 래퍼
def render_sims_hub():
    """예전 엔트리 호환용: 내부의 render()를 호출합니다."""
    return render()       

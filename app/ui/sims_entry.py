# app/ui/sims_entry.py
# VERSION = "chat_middleware/2025-11-11T-fixed"
# 2025/12/12 rddbc030 추가
from __future__ import annotations

from typing import Dict, Any
import logging
import streamlit as st

from app.sims.config import DEFAULT_MODE
# 실행(메인 패널)은 패널 모듈의 단일 소스만 사용
from app.ui.sims_panel import render_sims_main
# Hub(집중 뷰) 메인은 허브 모듈에서
from app.ui.sims_hub import render_sims_hub as _hub_main

# (선택) 채팅 브리지: 여기서 임포트만 하고, 호출은 다른 곳에서 해도 됨
from app.ui.chat_middleware import drain_inbox_to_chat, wire_chat_context

log = logging.getLogger("ssai")


def sims_mode_selector(key: str = "__sims_mode") -> str:
    """
    Panel(A) / Hub(B) UI 모드 선택 (중복 렌더 대비: key 주입 가능)
    - 위젯 생성 '이전'에만 기본값을 세팅하고,
    - 위젯 생성 '이후'에는 session_state를 직접 덮어쓰지 않는다.
    """
    if key not in st.session_state:
        st.session_state[key] = (
            DEFAULT_MODE if str(DEFAULT_MODE).startswith("Panel") else "Hub (B)"
        )

    st.markdown("### SIMS 모드")
    current = st.session_state.get(key, "Panel (A)")
    idx = 0 if str(current).startswith("Panel") else 1
    mode: str = st.radio(
        "UI 모드 선택",
        ["Panel (A)", "Hub (B)"],
        index=idx,
        key=key,  # ← 위젯이 session_state[key]를 관리
    )
    log.debug("[entry.mode] mode=%r (key=%r)", mode, key)
    return mode

# Panel(A) 사이드바: 카테고리/작업 선택 UI 렌더링 및 선택값 반환
# - Hub(B)에서는 이 함수를 호출하더라도 안내문만 표시하고 실제 선택 UI는 렌더링하지 않는다.
# - Panel(A): 카테고리/작업선택 UI 렌더링 + 선택값 반환 (메인에서 실행)
# - Hub(B):   안내문만 표시 (메인에서 허브 UI 실행)
def _panel_sidebar() -> Dict[str, str]:
    """
    Panel(A) 전용: 사이드바에서 업무그룹/대상/작업 선택만 수행하고 결과를 반환.
    실제 실행은 메인 영역의 render_sims_main()에서 처리한다.

    화면 구조:
      1) 업무그룹
      2) 관리대상 또는 작업그룹
      3) 작업선택

    내부 라우팅은 기존 category/action 구조를 유지한다.
    """
    st.markdown("### SIMS 옵션")

    # ------------------------------------------------------------------
    # 선택 옵션 정의
    # ------------------------------------------------------------------
    business_group_options = [
        "마스터관리",
        "입출고/명세서/재고",
        "분석/KPI",
    ]

    master_target_options = [
        "사용자",
        "코드마스터",
        "거래처",
        "도로명주소",
        "제품",
    ]

    master_action_options_map = {
        "사용자": [
            "사용자목록 + 부서명",
            "부서별 사용자 수",
            "최근 입사자",
        ],
        "코드마스터": [
            "그룹코드조회",
            "코드명 검색",
        ],
        "거래처": [
            "거래처 목록",
            "거래처 상세",
        ],
        "도로명주소": [
            "도로명주소 조회",
        ],
        "제품": [
            "제품코드 목록",
            "제품코드 상세",
        ],
    }

    io_action_group_map = {
        "명세/공통": [
            "입고명세 조회",
            "출고명세 조회",
            "거래명세서 공통 조회",
            "세금계산서 공통 조회",
        ],
        "재고/수불": [
            "제품수불현황 조회",
            "제품재고현황 조회",
        ],
        "월집계": [
            "실재고월집계 조회",
            "장부재고월집계 조회",
        ],
        "검증": [
            "입고↔거래명세서 검증",
            "입고↔세금계산서 검증",
            "출고↔거래명세서 검증",
            "출고↔세금계산서 검증",
        ],
    }

    analytics_action_group_map = {
        "매출분석": [
            "제약사별 매출 추세 분석",
            "제약사별 매출 추세 분석 요약표",
            "품목별 매출 추세 분석",
            "품목별 매출 추세 요약표",
        ],
        "매출예상": [
            "품목별 매출 예상",
        ],
        "재고부족": [
            "품목별 재고부족현황",
        ],
    }

    master_key_map = {
        "사용자": "users",
        "코드마스터": "codes",
        "거래처": "vendors",
        "도로명주소": "road",
        "제품": "goods",
    }

    io_group_key_map = {
        "명세/공통": "detail",
        "재고/수불": "stock",
        "월집계": "month",
        "검증": "check",
    }

    analytics_group_key_map = {
        "매출분석": "sales",
        "매출예상": "forecast",
        "재고부족": "shortage",
    }

    # ------------------------------------------------------------------
    # 내부 helper
    # ------------------------------------------------------------------
    def _clean(v: object) -> str:
        return str(v or "").strip()

    def _ensure_select_value(key: str, options: list[str], default: str) -> None:
        """
        Streamlit selectbox key의 기존 값이 현재 options에 없으면 안전하게 초기화한다.
        카테고리/그룹 전환 시 선택값 튐을 줄이기 위한 장치.
        """
        if not options:
            st.session_state[key] = ""
            return

        default_value = default if default in options else options[0]
        current = _clean(st.session_state.get(key, default_value))

        if current not in options:
            st.session_state[key] = default_value

    def _find_group_by_action(action: str, group_map: dict[str, list[str]], fallback: str) -> str:
        action = _clean(action)
        for group_name, actions in group_map.items():
            if action in actions:
                return group_name
        return fallback

    def _business_group_from_selected(selected: dict) -> str:
        cat = _clean(selected.get("category"))
        action = _clean(selected.get("action"))

        if cat in master_target_options:
            return "마스터관리"
        if cat == "입출고/명세서/재고":
            return "입출고/명세서/재고"
        if cat == "분석/KPI":
            return "분석/KPI"

        # action만 남아 있는 경우 보정
        for actions in io_action_group_map.values():
            if action in actions:
                return "입출고/명세서/재고"

        for actions in analytics_action_group_map.values():
            if action in actions:
                return "분석/KPI"

        return "마스터관리"

    # ------------------------------------------------------------------
    # 현재 선택값 기반 기본값 보정
    # ------------------------------------------------------------------
    current_selected = st.session_state.get("__sims_selected") or {}
    current_category = _clean(current_selected.get("category") or st.session_state.get("__sims_cat"))
    current_action = _clean(current_selected.get("action") or st.session_state.get("__sims_action"))

    default_business_group = _business_group_from_selected({
        "category": current_category,
        "action": current_action,
    })

    _ensure_select_value(
        "__sims_business_group",
        business_group_options,
        default_business_group,
    )

    # ------------------------------------------------------------------
    # 화면 렌더
    # ------------------------------------------------------------------
    with st.expander("카테고리 / 작업 선택", expanded=True):
        business_group = st.selectbox(
            "업무그룹",
            business_group_options,
            key="__sims_business_group",
        )

        if business_group == "마스터관리":
            default_target = current_category if current_category in master_target_options else "제품"
            _ensure_select_value(
                "__sims_master_target",
                master_target_options,
                default_target,
            )

            category = st.selectbox(
                "관리대상",
                master_target_options,
                key="__sims_master_target",
            )

            action_options = master_action_options_map.get(category) or []
            action_key = f"__sims_action_master_{master_key_map.get(category, 'default')}"

            default_action = current_action if current_action in action_options else (action_options[0] if action_options else "")
            _ensure_select_value(action_key, action_options, default_action)

            action = st.selectbox(
                "작업선택",
                action_options,
                key=action_key,
            )

        elif business_group == "입출고/명세서/재고":
            category = "입출고/명세서/재고"

            io_group_options = list(io_action_group_map.keys())
            default_group = _find_group_by_action(
                current_action,
                io_action_group_map,
                "명세/공통",
            )

            _ensure_select_value(
                "__sims_io_action_group",
                io_group_options,
                default_group,
            )

            action_group = st.selectbox(
                "작업그룹",
                io_group_options,
                key="__sims_io_action_group",
            )

            action_options = io_action_group_map.get(action_group) or []
            group_key = io_group_key_map.get(action_group, "default")
            action_key = f"__sims_action_io_{group_key}"

            default_action = current_action if current_action in action_options else (action_options[0] if action_options else "")
            _ensure_select_value(action_key, action_options, default_action)

            action = st.selectbox(
                "작업선택",
                action_options,
                key=action_key,
            )

        else:
            category = "분석/KPI"

            analytics_group_options = list(analytics_action_group_map.keys())
            default_group = _find_group_by_action(
                current_action,
                analytics_action_group_map,
                "매출분석",
            )

            _ensure_select_value(
                "__sims_analytics_action_group",
                analytics_group_options,
                default_group,
            )

            action_group = st.selectbox(
                "작업그룹",
                analytics_group_options,
                key="__sims_analytics_action_group",
            )

            action_options = analytics_action_group_map.get(action_group) or []
            group_key = analytics_group_key_map.get(action_group, "default")
            action_key = f"__sims_action_analytics_{group_key}"

            default_action = current_action if current_action in action_options else (action_options[0] if action_options else "")
            _ensure_select_value(action_key, action_options, default_action)

            action = st.selectbox(
                "작업선택",
                action_options,
                key=action_key,
            )

    # ------------------------------------------------------------------
    # 기존 코드 호환용 session_state 유지
    # ------------------------------------------------------------------
    selected = {
        "category": category,
        "action": action,
    }

    st.session_state["__sims_cat"] = category
    st.session_state["__sims_action"] = action
    st.session_state["__sims_selected"] = selected

    log.debug("[entry.sidebar] saved selection=%r", selected)

    return selected

# 사이드바 컨트롤 렌더링: Panel(A)에서는 선택값 반환, Hub(B)에서는 안내문만 표시
# - Panel(A): 카테고리/작업선택만 하고 선택 결과를 반환(메인에서 실행)
# - Hub(B):   안내만 표시(메인에서 허브 UI 실행)
def render_sims_sidebar_controls(parent: Any | None = None, **kwargs) -> Dict[str, str]:
    """
    사이드바 컨트롤 렌더링.
    Panel(A): 카테고리/작업선택만 하고 선택 결과를 반환(메인에서 실행)
    Hub(B):   안내만 표시(메인에서 허브 UI 실행)

    parent:
        - Lmstudio_SSAI_chat_main.py 에서 fallback 호출 시 parent=st 를 넘기는 경우가 있어
          시그니처를 맞추기 위한 용도이며, 현재 구현에서는 사용하지 않는다.
    기타 인자(**kwargs)는 무시한다.
    """
    # 현재 구현에서는 parent, kwargs 를 사용하지 않지만
    # 시그니처 호환을 위해 인자만 받아 둔다.
    mode = st.session_state.get("__sims_mode") or DEFAULT_MODE

    if str(mode).startswith("Panel"):
        selected = _panel_sidebar()
        st.session_state["__sims_selected"] = selected  # 메인에서 소비
        log.debug("[entry.sidebar] saved selection=%r", selected)
        return selected
    else:
        st.caption("Hub 모드는 메인 영역에서 바로 선택/실행합니다.")
        return {}

__all__ = [
    "sims_mode_selector",
    "render_sims_sidebar_controls",
    # 아래는 외부에서 그대로 가져다 쓰도록 노출(재수출)
    "render_sims_main",
    "_hub_main",
]

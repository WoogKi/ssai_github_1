"""Streamlit view for the registry-backed Rddbc230 stock cost state."""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from app.services.rddbc230_service import get_rddbc230_result
from app.sims.views.product_master_filters import render_product_master_filters
from app.sims.views.rddbc_io_shared import _trigger_panel_run


def view_rddbc230(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    defaults = dict(params or {})
    prefix = "__io230"
    payload_key = "__io230_last_payload"

    with st.form(f"{prefix}_form", clear_on_submit=False, enter_to_submit=False):
        st.caption("조회조건 · Rddbc230 최종 매입단가")
        product_filters = render_product_master_filters(
            prefix=f"{prefix}_product",
            ns="state",
            defaults=defaults,
            only_use_default=False,
            display_caption="제품코드(040) 목록과 동일한 제품 조건을 적용합니다.",
            include_price_filters=False,
            include_audit_filters=False,
            include_only_use=False,
        )

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            buy_cd = st.text_input("매입처코드", value=str(defaults.get("buy_cd") or ""), key=f"{prefix}_buy_cd")
        with c2:
            buy_nm = st.text_input("매입처명", value=str(defaults.get("buy_nm") or ""), key=f"{prefix}_buy_nm")
        with c3:
            stock_cd = st.text_input("재고위치", value=str(defaults.get("stock_cd") or ""), key=f"{prefix}_stock_cd")
        with c4:
            stock_nm = st.text_input("재고위치명", value=str(defaults.get("stock_nm") or ""), key=f"{prefix}_stock_nm")

        c5, c6, c7, c8 = st.columns(4)
        with c5:
            cost_apply_cd = st.text_input("단가적용코드", value=str(defaults.get("cost_apply_cd") or ""), key=f"{prefix}_cost_apply_cd")
        with c6:
            cost_apply_nm = st.text_input("단가적용처명", value=str(defaults.get("cost_apply_nm") or ""), key=f"{prefix}_cost_apply_nm")
        with c7:
            stock_apply_cd = st.text_input("재고적용코드", value=str(defaults.get("stock_apply_cd") or ""), key=f"{prefix}_stock_apply_cd")
        with c8:
            stock_apply_nm = st.text_input("재고적용처명", value=str(defaults.get("stock_apply_nm") or ""), key=f"{prefix}_stock_apply_nm")

        submitted = st.form_submit_button(
            "조회", type="primary", width="stretch", on_click=_trigger_panel_run,
        )

    request_params = {
        **product_filters,
        "buy_cd": str(buy_cd or "").strip(),
        "buy_nm": str(buy_nm or "").strip(),
        "stock_cd": str(stock_cd or "").strip(),
        "stock_nm": str(stock_nm or "").strip(),
        "stock_apply_cd": str(stock_apply_cd or "").strip(),
        "stock_apply_nm": str(stock_apply_nm or "").strip(),
        "cost_apply_cd": str(cost_apply_cd or "").strip(),
        "cost_apply_nm": str(cost_apply_nm or "").strip(),
        "_display_context": "panel",
    }
    if submitted:
        payload = get_rddbc230_result(request_params)
        st.session_state[payload_key] = payload
        st.session_state.pop(f"{payload_key}_fresh", None)
        return payload

    st.session_state.pop(f"{payload_key}_fresh", None)
    return {
        "title": "최종 매입단가 조회",
        "action": "최종 매입단가 조회",
        "params": request_params,
        "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "final": False,
    }

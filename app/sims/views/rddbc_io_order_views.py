"""Registry-backed Rddbc170/Rddbc180 order and expected-inbound views."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import streamlit as st

from app.services.business_calendar_service import kst_today
from app.services.rddbc170_rddbc180_order_service import (
    EXPECTED_ACTION,
    ORDER_ACTION,
    get_expected_inbound_result,
    get_order_result,
)
from app.sims.views.product_master_filters import render_product_master_filters
from app.sims.views.rddbc_io_shared import _trigger_panel_run


def _date_text(value: Any, fallback: date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text if len(text) == 8 else fallback.strftime("%Y%m%d")


def _date_value(value: Any, fallback: date) -> date:
    text = _date_text(value, fallback)
    try:
        return date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:]}")
    except ValueError:
        return fallback


def _order_vendor_filters(prefix: str, defaults: dict[str, Any]) -> dict[str, Any]:
    c1, c2 = st.columns(2)
    with c1:
        order_vendor_cd = st.text_input("발주처코드", value=str(defaults.get("order_vendor_cd") or ""), key=f"{prefix}_order_vendor_cd")
    with c2:
        order_vendor_nm = st.text_input("발주처명", value=str(defaults.get("order_vendor_nm") or ""), key=f"{prefix}_order_vendor_nm")
    return {
        "order_vendor_cd": str(order_vendor_cd or "").strip(),
        "order_vendor_nm": str(order_vendor_nm or "").strip(),
    }


def _application_filters(prefix: str, defaults: dict[str, Any]) -> dict[str, Any]:
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        cost_apply_cd = st.text_input("단가적용처코드", value=str(defaults.get("cost_apply_cd") or ""), key=f"{prefix}_cost_apply_cd")
    with c2:
        cost_apply_nm = st.text_input("단가적용처명", value=str(defaults.get("cost_apply_nm") or ""), key=f"{prefix}_cost_apply_nm")
    with c3:
        stock_apply_cd = st.text_input("재고적용처코드", value=str(defaults.get("stock_apply_cd") or ""), key=f"{prefix}_stock_apply_cd")
    with c4:
        stock_apply_nm = st.text_input("재고적용처명", value=str(defaults.get("stock_apply_nm") or ""), key=f"{prefix}_stock_apply_nm")
    c5, c6 = st.columns(2)
    with c5:
        stock_cd = st.text_input("재고위치코드", value=str(defaults.get("stock_cd") or ""), key=f"{prefix}_stock_cd")
    with c6:
        stock_nm = st.text_input("재고위치명", value=str(defaults.get("stock_nm") or ""), key=f"{prefix}_stock_nm")
    return {
        "cost_apply_cd": str(cost_apply_cd or "").strip(),
        "cost_apply_nm": str(cost_apply_nm or "").strip(),
        "stock_apply_cd": str(stock_apply_cd or "").strip(),
        "stock_apply_nm": str(stock_apply_nm or "").strip(),
        "stock_cd": str(stock_cd or "").strip(),
        "stock_nm": str(stock_nm or "").strip(),
    }


def _status_filters(prefix: str, defaults: dict[str, Any], *, expected: bool) -> dict[str, Any]:
    status_labels = {"전체": "", "발주": "1", "입고중": "2"}
    if not expected:
        status_labels["입고완료"] = "3"
    current_status = str(defaults.get("status_code") or "")
    status_default = next((label for label, code in status_labels.items() if code == current_status), "전체")
    c9, c10 = st.columns(2)
    with c9:
        status_label = st.selectbox("발주상태", tuple(status_labels), index=tuple(status_labels).index(status_default), key=f"{prefix}_status")
    with c10:
        has_outstanding = st.checkbox("미입고 존재", value=bool(defaults.get("has_outstanding", False)), key=f"{prefix}_outstanding")
    return {
        "status_code": status_labels[status_label],
        "has_outstanding": bool(has_outstanding),
    }


def _merge_filter_params(*groups: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for group in groups:
        overlap = set(merged).intersection(group)
        if overlap:
            raise ValueError(f"중복 조회조건 키: {sorted(overlap)}")
        merged.update(group)
    return merged


def _view(*, params: Optional[dict[str, Any]], mode: str) -> dict[str, Any]:
    defaults = dict(params or {})
    expected = mode == "expected"
    prefix = "__io180_expected" if expected else "__io170_order"
    payload_key = f"{prefix}_last_payload"
    action = EXPECTED_ACTION if expected else ORDER_ACTION

    with st.form(f"{prefix}_form", clear_on_submit=False, enter_to_submit=False):
        st.caption("오늘 + 직전 3영업일의 발주 잔량" if expected else "Rddbc170/Rddbc180 발주 상세")
        date_filters: dict[str, Any] = {}
        if not expected:
            today = kst_today()
            d1, d2 = st.columns(2)
            with d1:
                date_from = st.date_input("발주일자 시작", value=_date_value(defaults.get("date_from"), today - timedelta(days=30)), key=f"{prefix}_date_from")
            with d2:
                date_to = st.date_input("발주일자 종료", value=_date_value(defaults.get("date_to"), today), key=f"{prefix}_date_to")
            date_filters.update({"date_from": date_from.strftime("%Y%m%d"), "date_to": date_to.strftime("%Y%m%d")})

        vendor_filters = _order_vendor_filters(prefix, defaults)
        product_filters = render_product_master_filters(
            prefix=f"{prefix}_product", ns="state", defaults=defaults,
            only_use_default=False, include_price_filters=False,
            include_audit_filters=False, include_only_use=False,
        )
        application_filters = _application_filters(prefix, defaults)
        status_filters = _status_filters(prefix, defaults, expected=expected)
        request_params = _merge_filter_params(
            date_filters,
            vendor_filters,
            product_filters,
            application_filters,
            status_filters,
        )
        if not expected:
            due_enabled = st.checkbox(
                "납기일자 기간 적용",
                value=bool(defaults.get("due_date_from") or defaults.get("due_date_to")),
                key=f"{prefix}_due_enabled",
            )
            if due_enabled:
                due1, due2 = st.columns(2)
                with due1:
                    due_from = st.date_input("납기일자 시작", value=_date_value(defaults.get("due_date_from"), today), key=f"{prefix}_due_from")
                with due2:
                    due_to = st.date_input("납기일자 종료", value=_date_value(defaults.get("due_date_to"), today), key=f"{prefix}_due_to")
                request_params.update({"due_date_from": due_from.strftime("%Y%m%d"), "due_date_to": due_to.strftime("%Y%m%d")})
        submitted = st.form_submit_button("조회", type="primary", width="stretch", on_click=_trigger_panel_run)

    request_params["_display_context"] = "panel"
    if submitted:
        payload = get_expected_inbound_result(request_params) if expected else get_order_result(request_params)
        st.session_state[payload_key] = payload
        st.session_state.pop(f"{payload_key}_fresh", None)
        return payload
    st.session_state.pop(f"{payload_key}_fresh", None)
    return {"title": action, "action": action, "params": request_params, "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.", "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.", "final": False}


def view_order_query(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _view(params=params, mode="order")


def view_expected_inbound_query(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _view(params=params, mode="expected")

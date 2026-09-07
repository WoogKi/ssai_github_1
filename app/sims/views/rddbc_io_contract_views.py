"""Streamlit views for registered contract-price table features."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Optional

import streamlit as st

from app.services.rddbc070_service import (
    get_rddbc070_current_result,
    get_rddbc070_history_result,
)
from app.sims.views.rddbc_io_shared import _trigger_panel_run
from app.sims.views.product_master_filters import render_product_master_filters


def _parse_date(value: Any, fallback: date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 8:
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            pass
    return fallback


def _view_rddbc070(
    *,
    title: str,
    mode: str,
    payload_key: str,
    service_fn: Callable[[dict[str, Any]], dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    defaults = dict(params or {})
    today = date.today()
    default_from = _parse_date(defaults.get("date_from"), today.replace(month=1, day=1))
    default_to = _parse_date(defaults.get("date_to"), today)
    default_as_of = _parse_date(defaults.get("as_of"), today)
    prefix = "__io070_current" if mode == "current" else "__io070_history"

    with st.form(f"{prefix}_form", clear_on_submit=False, enter_to_submit=False):
        st.caption("조회조건 · Rddbc070 계약단가")
        c1, c2 = st.columns(2)
        with c1:
            ven_cd = st.text_input(
                "단가적용거래처 코드", value=str(defaults.get("ven_cd") or ""),
                key=f"{prefix}_ven_cd",
            )
        with c2:
            ven_nm = st.text_input(
                "단가적용거래처명", value=str(defaults.get("ven_nm") or ""),
                key=f"{prefix}_ven_nm",
            )

        product_filters = render_product_master_filters(
            prefix=f"{prefix}_product",
            ns=mode,
            defaults=defaults,
            only_use_default=False,
            display_caption="제품코드(040) 목록과 동일한 제품 조건을 적용합니다.",
        )

        use_start_range = st.checkbox(
            "계약시작일 범위 사용",
            value=bool(defaults.get("date_from") or defaults.get("date_to")),
            key=f"{prefix}_use_start_range",
        )
        d1, d2, d3 = st.columns(3)
        with d1:
            date_from_value = st.date_input(
                "계약시작일 시작", value=default_from,
                disabled=not use_start_range, key=f"{prefix}_date_from",
            )
        with d2:
            date_to_value = st.date_input(
                "계약시작일 종료", value=default_to,
                disabled=not use_start_range, key=f"{prefix}_date_to",
            )
        with d3:
            as_of_value = None
            if mode == "current":
                as_of_value = st.date_input(
                    "기준일", value=default_as_of, key=f"{prefix}_as_of",
                )
            else:
                st.caption("최종 계약단가 조회에서 기준일을 사용합니다.")
        submitted = st.form_submit_button(
            "조회", type="primary", width="stretch", on_click=_trigger_panel_run,
        )

    request_params = {
        "ven_cd": str(ven_cd or "").strip(),
        "ven_nm": str(ven_nm or "").strip(),
        **product_filters,
        "date_from": date_from_value.strftime("%Y%m%d") if use_start_range else "",
        "date_to": date_to_value.strftime("%Y%m%d") if use_start_range else "",
        "_display_context": "panel",
    }
    if mode == "current" and as_of_value is not None:
        request_params["as_of"] = as_of_value.strftime("%Y%m%d")

    if submitted:
        payload = service_fn(request_params)
        st.session_state[payload_key] = payload
        # The submitted run already returns this payload to the panel renderer.
        # Do not reserve the same result for the next Streamlit rerun.
        st.session_state.pop(f"{payload_key}_fresh", None)
        return payload

    st.session_state.pop(f"{payload_key}_fresh", None)
    return {
        "title": title,
        "action": title,
        "params": request_params,
        "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "final": False,
    }


def view_rddbc070_history(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _view_rddbc070(
        title="계약단가 이력 조회",
        mode="history",
        payload_key="__io070_history_last_payload",
        service_fn=get_rddbc070_history_result,
        params=params,
    )


def view_rddbc070_current(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _view_rddbc070(
        title="최종 계약단가 조회",
        mode="current",
        payload_key="__io070_current_last_payload",
        service_fn=get_rddbc070_current_result,
        params=params,
    )

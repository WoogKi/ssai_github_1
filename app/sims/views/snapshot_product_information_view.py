"""Panel form for approved Snapshot v2.1 product statistics."""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from app.services.snapshot_product_information_service import (
    ACTION,
    STATUS_LABELS,
    get_snapshot_product_information_result,
)
from app.sims.views.rddbc_io_shared import _trigger_panel_run
from app.sims.views.product_master_filters import render_product_master_filters


def _index(options: list[str], value: Any) -> int:
    text = str(value or "").strip()
    return options.index(text) if text in options else 0


def view_snapshot_product_information(
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    defaults = dict(params or {})
    prefix = "__snapshot_product_information"
    payload_key = "__snapshot_product_information_last_payload"
    grade_options = ["전체", "A", "B", "C", "D", "E", "F", "X", "unavailable"]
    lifecycle_options = ["전체", "new_product", "established_product", "unknown_lifecycle"]

    with st.form(f"{prefix}_form", clear_on_submit=False, enter_to_submit=False):
        st.caption("조회조건 · 제품정보 및 승인된 제품 통계")
        product_filters = render_product_master_filters(
            prefix=prefix, ns="information", defaults=defaults, only_use_default=False,
            include_price_filters=False, include_audit_filters=False, include_only_use=False,
        )
        c2, c3, c4, c5 = st.columns([1, 1, 1, 1.3])
        with c2:
            frequency_grade = st.selectbox(
                "출고빈도등급",
                grade_options,
                index=_index(grade_options, defaults.get("frequency_grade") or "전체"),
                format_func=lambda value: STATUS_LABELS.get(value, value),
                key=f"{prefix}_frequency_grade",
            )
        with c3:
            profit_grade = st.selectbox(
                "손익등급",
                ["전체", "A", "B", "C", "D", "E", "unavailable"],
                index=_index(["전체", "A", "B", "C", "D", "E", "unavailable"], defaults.get("profit_grade") or "전체"),
                format_func=lambda value: STATUS_LABELS.get(value, value),
                key=f"{prefix}_profit_grade",
            )
        with c4:
            contribution_grade = st.selectbox(
                "기여도등급",
                ["전체", "A", "B", "C", "D", "E", "unavailable"],
                index=_index(["전체", "A", "B", "C", "D", "E", "unavailable"], defaults.get("contribution_grade") or "전체"),
                format_func=lambda value: STATUS_LABELS.get(value, value),
                key=f"{prefix}_contribution_grade",
            )
        with c5:
            lifecycle_status = st.selectbox(
                "제품수명주기",
                lifecycle_options,
                index=_index(lifecycle_options, defaults.get("lifecycle_status") or "전체"),
                format_func=lambda value: STATUS_LABELS.get(value, value),
                key=f"{prefix}_lifecycle_status",
            )
        submitted = st.form_submit_button("조회", type="primary", width="stretch")

    request_params = {
        **defaults,
        **product_filters,
        "frequency_grade": "" if frequency_grade == "전체" else frequency_grade,
        "profit_grade": "" if profit_grade == "전체" else profit_grade,
        "contribution_grade": "" if contribution_grade == "전체" else contribution_grade,
        "lifecycle_status": "" if lifecycle_status == "전체" else lifecycle_status,
        "top": int(defaults.get("top") or 500),
        "_display_context": "panel",
    }
    if submitted:
        payload = get_snapshot_product_information_result(request_params)
        st.session_state[payload_key] = payload
        _trigger_panel_run()
        return payload
    previous = st.session_state.get(payload_key)
    if isinstance(previous, dict) and previous.get("final"):
        return previous
    return {
        "title": ACTION,
        "action": ACTION,
        "params": request_params,
        "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "message": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
        "final": False,
    }

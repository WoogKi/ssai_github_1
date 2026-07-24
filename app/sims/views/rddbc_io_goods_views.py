# app/sims/views/rddbc_io_goods_views.py

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

from app.services.rddbc040_service import search_goods_full
from app.services.utils import apply_labels
from app.sims.views.rddbc_io_shared import (
    _top_value,
    _trigger_panel_run,
    _txt,
)


def view_rddbc040(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title = "제품코드 목록"
    payload_key = "__m040_last_payload"
    prefix = "__m040"

    ss = st.session_state
    defaults = dict(params or {})

    with st.form("__m040_form", clear_on_submit=False, enter_to_submit=False):
        st.caption("조회조건 · 제품마스터")

        c1, c2 = st.columns(2)
        with c1:
            p_physic_cd = _txt(
                f"{prefix}_physic_cd",
                "제품코드",
                str(defaults.get("physic_cd", "")),
            )
        with c2:
            p_physic_nm = _txt(
                f"{prefix}_physic_nm",
                "제품명",
                str(defaults.get("physic_nm", "")),
            )

        c3, c4 = st.columns(2)
        with c3:
            p_insu_cd = _txt(
                f"{prefix}_insu_cd",
                "보험코드",
                str(defaults.get("insu_cd", "")),
            )
        with c4:
            p_barcode = _txt(
                f"{prefix}_barcode",
                "바코드",
                str(defaults.get("barcode", "")),
            )

        c5, c6 = st.columns(2)
        with c5:
            p_ven_nm_kw = _txt(
                f"{prefix}_ven_nm_kw",
                "제약사명",
                str(defaults.get("ven_nm_kw", "")),
            )
        with c6:
            p_top = _top_value(f"{prefix}_top", int(defaults.get("top", 200)))

        p_only_use = st.checkbox(
            "사용(Use_Gu=0)만",
            value=bool(defaults.get("only_use", True)),
            key=f"{prefix}_only_use",
        )

        submitted = st.form_submit_button(
            "조회",
            type="primary",
            width="stretch",
            on_click=_trigger_panel_run,
        )

    if submitted:
        physic_cd = str(p_physic_cd or "").strip()
        physic_nm = str(p_physic_nm or "").strip()
        insu_cd = str(p_insu_cd or "").strip()
        barcode = str(p_barcode or "").strip()
        ven_nm_kw = str(p_ven_nm_kw or "").strip()
        only_use = bool(p_only_use)
        top = int(p_top)

        # 제품코드가 있으면 정확조회 우선
        keyword = "" if physic_cd else physic_nm

        df = search_goods_full(
            top=top,
            keyword=keyword,
            physic_cd=physic_cd,
            insu_cd=insu_cd,
            barcode=barcode,
            ven_nm_kw=ven_nm_kw,
            only_use=only_use,
        )

        if df is None:
            df = pd.DataFrame()
        elif not df.empty:
            df = apply_labels(df, "rddbc040")

        if df.empty:
            payload = {
                "final": True,
                "type": "text",
                "title": "제품코드 목록 (0건)",
                "action": title,
                "params": {
                    "제품코드": physic_cd,
                    "제품명": physic_nm,
                    "보험코드": insu_cd,
                    "바코드": barcode,
                    "제약사명": ven_nm_kw,
                    "사용만": only_use,
                    "조회상한": top,
                },
                "df": pd.DataFrame(),
                "df_display": pd.DataFrame(),
                "meta": {
                    "row_count": 0,
                    "row_count_total": 0,
                    "조회상한": top,
                    "only_use": only_use,
                    "keyword": keyword,
                    "physic_cd": physic_cd,
                    "physic_nm": physic_nm,
                    "insu_cd": insu_cd,
                    "barcode": barcode,
                    "ven_nm_kw": ven_nm_kw,
                    "source": "제품코드마스터(Rddbc040)",
                    "empty_result": True,
                    "_force_push": True,
                },
                "data": "해당 조회조건의 자료가 없습니다.",
                "message": "해당 조회조건의 자료가 없습니다.",
            }
        else:
            prefer = [
                c
                for c in [
                    "제품코드",
                    "보험코드",
                    "제품명",
                    "제약사명",
                    "제품그룹명",
                    "구분명",
                    "제품플래그명",
                    "함량명",
                    "제품분류명",
                    "규격",
                    "단위",
                    "보험적용일",
                    "보험약가",
                    "바코드1",
                    "바코드2",
                    "바코드3",
                    "바코드4",
                    "바코드5",
                    "사용구분",
                    "삭제/사용여부",
                    "등록자명",
                    "등록일자",
                    "수정자명",
                    "수정일자",
                ]
                if c in df.columns
            ]

            df_display = df[prefer].copy() if prefer else df.copy()

            payload = {
                "final": True,
                "type": "table",
                "title": title,
                "action": title,
                "params": {
                    "제품코드": physic_cd,
                    "제품명": physic_nm,
                    "보험코드": insu_cd,
                    "바코드": barcode,
                    "제약사명": ven_nm_kw,
                    "사용만": only_use,
                    "조회상한": top,
                },
                "df": df,
                "df_display": df_display,
                "meta": {
                    "row_count": int(len(df)),
                    "row_count_total": int(df.attrs.get("row_count_total", len(df))),
                    "column_count": int(len(df.columns)),
                    "조회상한": top,
                    "only_use": only_use,
                    "keyword": keyword,
                    "physic_cd": physic_cd,
                    "physic_nm": physic_nm,
                    "insu_cd": insu_cd,
                    "barcode": barcode,
                    "ven_nm_kw": ven_nm_kw,
                    "source": "제품코드마스터(Rddbc040)",
                },
            }

        ss[payload_key] = payload
        return payload

    if payload_key not in ss:
        return {
            "title": title,
            "action": title,
            "params": {},
            "data": "조회 조건을 입력한 뒤 [조회] 버튼을 누르세요.",
            "final": False,
        }

    return ss[payload_key]
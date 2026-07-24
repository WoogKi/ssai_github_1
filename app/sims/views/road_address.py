# app/sims/views/road_address.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Dict, Any, Optional
import logging
import io

import pandas as pd
import streamlit as st

from app.services import rddbc021_service as R21
from app.services.utils import apply_labels, make_unique_columns

log = logging.getLogger("ssai")


def _master_max_rows(default: int = 30000) -> int:
    """
    마스터 조회 공통 상한.
    새 env를 만들지 않고 기존 SIMS_PANEL_DISPLAY_MAX_ROWS /
    SIMS_CHAT_DISPLAY_MAX_ROWS 값을 사용한다.
    """
    try:
        raw = (
            os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS")
            or os.getenv("SIMS_CHAT_DISPLAY_MAX_ROWS")
            or str(default)
        )
        v = int(str(raw or default).strip())
    except Exception:
        v = int(default)

    if v < 1:
        v = int(default)

    return v



def _ns() -> str:
    return str(st.session_state.get("__sims_widget_ns", "0"))


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _xlsx_bytes(df: pd.DataFrame) -> Optional[bytes]:
    try:
        import xlsxwriter  # noqa: F401
        engine = "xlsxwriter"
    except Exception:
        try:
            import openpyxl  # noqa: F401
            engine = "openpyxl"
        except Exception:
            return None

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine=engine) as w:
        df.to_excel(w, index=False, sheet_name="RoadAddress")
    return buf.getvalue()


def _prepare_road_address_display(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_df(df_raw)
    if df.empty:
        return df

    df = apply_labels(df, "rddbc021", table_name_in_db="Rddbc021")
    df = make_unique_columns(df)

    front_cols = [c for c in [
        "도로명코드",
        "도로명코드상세번호",
        "시도명",
        "시구군명",
        "법정읍면동명",
        "도로명",
        "도로명(영문)",
        "동여부",
        "지번본번",
    ] if c in df.columns]

    rest_cols = [c for c in df.columns if c not in front_cols]
    out = df[front_cols + rest_cols].copy()

    for col in out.columns:
        out[col] = (
            out[col]
            .fillna("")
            .astype(str)
            .replace({"None": "", "nan": "", "<NA>": ""})
            .str.strip()
        )

    # 화면/NLQ 공통 조회 순번
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))

    display_cols = [
        "순번",
        "도로명코드",
        "도로명코드상세번호",
        "시도명",
        "시구군명",
        "법정읍면동명",
        "도로명",
        "도로명(영문)",
        "동여부",
        "지번본번",
    ]
    display_cols = [c for c in display_cols if c in out.columns]

    return out[display_cols].copy()

def _build_road_query_summary(params: Dict[str, Any]) -> str:
    parts = []

    sido_nm = _clean_text(params.get("시도명"))
    gugun_nm = _clean_text(params.get("시구군명"))
    dong_nm = _clean_text(params.get("법정읍면동명"))
    road_nm = _clean_text(params.get("도로명"))
    keyword = _clean_text(params.get("통합검색"))

    if sido_nm:
        parts.append(f"시도명 {sido_nm}")
    if gugun_nm:
        parts.append(f"시구군명 {gugun_nm}")
    if dong_nm:
        parts.append(f"법정읍면동명 {dong_nm}")
    if road_nm:
        parts.append(f"도로명 {road_nm}")
    if keyword:
        parts.append(f"도로명주소 {keyword}")

    return " / ".join(parts) if parts else "전체"


def _road_summary_line(query_summary: str) -> str:
    qs = _clean_text(query_summary)
    return f"조회조건: {qs}" if qs else "조회조건: 전체"


def render_road_address_list() -> Dict[str, Any]:
    master_max_rows = _master_max_rows()
    st.subheader("도로명주소 조회")
    st.caption("Rddbc021 도로명주소 테이블을 기준으로 조회합니다.")

    ns = _ns()
    form_key = f"__road_addr_form__{ns}"

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        c1, c2, c3 = st.columns(3)
        with c1:
            sido_nm = st.text_input("시도명", value="", key=f"__road_addr_sido__{ns}", placeholder="예: 서울")
        with c2:
            gugun_nm = st.text_input("시구군명", value="", key=f"__road_addr_gugun__{ns}", placeholder="예: 강남")
        with c3:
            dong_nm = st.text_input("법정읍면동명", value="", key=f"__road_addr_dong__{ns}", placeholder="예: 역삼")

        c4, c5, c6 = st.columns(3)
        with c4:
            road_nm = st.text_input("도로명", value="", key=f"__road_addr_road__{ns}", placeholder="예: 테헤란로")
        with c5:
            keyword = st.text_input("통합검색", value="", key=f"__road_addr_keyword__{ns}")
        with c6:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        submitted = st.form_submit_button("조회", type="primary", width="stretch")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "도로명주소 조회",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    params = {
        "시도명": _clean_text(sido_nm),
        "시구군명": _clean_text(gugun_nm),
        "법정읍면동명": _clean_text(dong_nm),
        "도로명": _clean_text(road_nm),
        "통합검색": _clean_text(keyword),
        "조회상한": int(master_max_rows),
    }

    try:
        df_raw = _ensure_df(
            R21.search_road_address(
                sido_nm=_clean_text(sido_nm),
                gugun_nm=_clean_text(gugun_nm),
                dong_nm=_clean_text(dong_nm),
                road_nm=_clean_text(road_nm),
                keyword=_clean_text(keyword),
                top=int(master_max_rows),
            )
        )

        df_display_all = _prepare_road_address_display(df_raw)

        total = int(len(df_raw))
        display_top = int(master_max_rows)
        # 화면 표시 제한은 chat/sims_panel 공통 표시 제한에서 처리한다.
        df_display = df_display_all.copy()
        display_count = int(len(df_display))

        query_summary = _build_road_query_summary(params)
        summary_line = _road_summary_line(query_summary)

        note = (
            f"조회결과: **{total:,}건** (전부 표시)"
            if display_count >= total
            else f"조회결과: **{total:,}건** (표시는 상위 {display_count:,}건)"
        )

        return {
            "final": True,
            "type": "table",
            "title": "도로명주소 조회",
            "action": "도로명주소 조회",
            "params": params,
            "df": df_display_all,
            "df_display": df_display,
            "meta": {
                "총건수": total,
                "row_count": total,
                "row_count_total": total,
                "display_row_count": display_count,
                "show_n": display_count,
                "row_count_loaded": total,
                "download_row_count": total,
                "fetch_limit": int(master_max_rows),
                "domain": "road_address",
                "source": "도로명주소마스터(Rddbc021)",
                "query_summary": query_summary,
                "condition": query_summary,
                "summary_md": summary_line,
                "note": note,
                "analysis_type": "road_address_master",
                "summary_basis": "전체 조회결과 기준",
                "table_profile": "road_address",
                "hide_meta_expander": True,
                "field_notes": (
                    "도로명주소 마스터 분석은 전체 조회결과 기준으로 답합니다. "
                    "화면 표시는 일부 행으로 제한될 수 있습니다."
                ),
            },
        }

    except Exception as e:
        log.exception("[view.road_address] search failed")
        return {
            "final": False,
            "type": "text",
            "title": "도로명주소 조회 오류",
            "action": "도로명주소 조회",
            "params": params,
            "data": str(e),
            "message": str(e),
        }
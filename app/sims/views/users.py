# app/sims/views/users.py
# 뷰는 입력 UI + payload 반환만 담당합니다.
# 표 렌더/다운로드/채팅 푸시는 sims_panel.py 에서 처리합니다.
# 사용자 조회
# 2026/05/17

from __future__ import annotations

import os
from typing import Any, Dict, Optional
import logging
import re

import pandas as pd
import streamlit as st

from app.services import rddbc060_service as U
from app.services.utils import apply_labels, make_unique_columns
from app.sims.views.master_advanced_filters import (
    render_master_audit_filter,
    build_master_query_condition,
    build_master_llm_summary,
)

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



# ------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------
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


def _norm_series(sr: pd.Series) -> pd.Series:
    return (
        sr.fillna("")
        .astype(str)
        .replace({"None": "", "nan": "", "<NA>": ""})
        .str.strip()
    )


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _load_code_options(loader_name: str) -> list[tuple[str, str]]:
    fn = getattr(U, loader_name, None)
    if not callable(fn):
        return [("전체", "")]

    try:
        df = _ensure_df(fn(top=500))
    except Exception:
        log.exception("[view.users] code option load failed loader=%s", loader_name)
        return [("전체", "")]

    if df.empty:
        return [("전체", "")]

    code_col = _pick_col(df, ["Rd01_Tcode", "항목코드", "상세코드"])
    name_col = _pick_col(df, ["Rd01_Hnm", "한글명", "코드명"])
    if not code_col or not name_col:
        return [("전체", "")]

    work = df[[code_col, name_col]].copy()
    work[code_col] = _norm_series(work[code_col])
    work[name_col] = _norm_series(work[name_col])
    work = work[(work[code_col] != "") & (work[name_col] != "")]
    work = work.drop_duplicates(subset=[code_col, name_col]).sort_values([code_col], kind="stable")

    options: list[tuple[str, str]] = [("전체", "")]
    for _, row in work.iterrows():
        options.append((str(row[name_col]).strip(), str(row[code_col]).strip()))
    return options


def _department_options() -> list[tuple[str, str]]:
    return _load_code_options("list_department_codes")


def _duty_options() -> list[tuple[str, str]]:
    return _load_code_options("list_duty_codes")


def _district_options() -> list[tuple[str, str]]:
    return _load_code_options("list_district_codes")


def _stock_options() -> list[tuple[str, str]]:
    return _load_code_options("list_stock_codes")


def _prepare_users_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_df(df_raw)

    # 거래처(vendors.py)와 동일한 원칙:
    # - Rd06_Add_Cd / Rd06_Mod_Cd 는 코드
    # - add_user_nm / mod_user_nm 은 표시용 이름
    # apply_labels()가 코드 컬럼을 "등록자/수정자"로 먼저 잡아도
    # 이름/코드 의미가 섞이지 않도록 사전에 별도 컬럼으로 보관한다.
    if "Rd06_Add_Cd" in df.columns and "등록자코드" not in df.columns:
        df["등록자코드"] = df["Rd06_Add_Cd"]
    if "Rd06_Mod_Cd" in df.columns and "수정자코드" not in df.columns:
        df["수정자코드"] = df["Rd06_Mod_Cd"]

    if "add_user_nm" in df.columns and "등록자명" not in df.columns:
        df["등록자명"] = df["add_user_nm"]
    if "mod_user_nm" in df.columns and "수정자명" not in df.columns:
        df["수정자명"] = df["mod_user_nm"]

    try:
        df = apply_labels(df.copy(), "rddbc060", table_name_in_db="Rddbc060")
    except Exception:
        pass

    try:
        df = make_unique_columns(df)
    except Exception:
        pass

    manual = {
        "Rd06_User_Cd": "사용자코드",
        "Rd06_User_ID": "사용자ID",
        "사용자 ID": "사용자ID",
        "사용자ID": "사용자ID",
        "Rd06_Sabun": "사번",
        "Rd06_User_Nm": "사용자명",
        "사용자 명": "사용자명",
        "사용자명": "사용자명",

        "부서명": "부서명",
        "직책": "직책",
        "영업지역": "영업지역",
        "재고위치": "재고위치",

        "Rd06_Add_Date": "등록일자",
        "등록 일자": "등록일자",
        "Rd06_Mod_Date": "수정일자",
        "수정 일자": "수정일자",
        "Rd06_Del_Flag": "삭제여부",
        "삭제 여부": "삭제여부",

        # 코드/이름 분리
        "Rd06_Add_Cd": "등록자코드",
        "등록자 코드": "등록자코드",
        "등록 자 코드": "등록자코드",
        "Rd06_Mod_Cd": "수정자코드",
        "수정자 코드": "수정자코드",
        "수정 자 코드": "수정자코드",

        "add_user_nm": "등록자명",
        "등록 자명": "등록자명",
        "등록자 이름": "등록자명",
        "mod_user_nm": "수정자명",
        "수정 자명": "수정자명",
        "수정자 이름": "수정자명",
    }

    for src, dst in manual.items():
        if src in df.columns and dst not in df.columns:
            df = df.rename(columns={src: dst})

    # 최종 표시 컬럼은 이름 우선, 없으면 코드 fallback.
    # 이 구조가 거래처의 등록자/수정자 처리 방식과 같다.
    if "등록자명" in df.columns:
        df["등록자"] = _norm_series(df["등록자명"])
    elif "등록자코드" in df.columns:
        df["등록자"] = _norm_series(df["등록자코드"])

    if "수정자명" in df.columns:
        df["수정자"] = _norm_series(df["수정자명"])
    elif "수정자코드" in df.columns:
        df["수정자"] = _norm_series(df["수정자코드"])

    for col in df.columns:
        try:
            df[col] = _norm_series(df[col])
        except Exception:
            pass

    return df

def _build_user_list_view(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df).copy()

    rename_candidates = {
        "Rd06_User_Cd": "사용자코드",
        "사용자 코드": "사용자코드",
        "Rd06_User_ID": "사용자ID",
        "사용자 ID": "사용자ID",
        "Rd06_User_Nm": "사용자명",
        "사용자 명": "사용자명",

        "Rd06_Department_Gcode": "부서대분류코드",
        "부서 대분류코드": "부서대분류코드",
        "Rd06_Department": "부서상세코드",
        "부서 상세코드": "부서상세코드",

        "Rd06_Duty_Gcode": "직책대분류코드",
        "직책 대분류코드": "직책대분류코드",
        "Rd06_Duty": "직책상세코드",
        "직책 상세코드": "직책상세코드",

        "Rd06_District_Gcode": "영업지역대분류코드",
        "영업지역 대분류코드": "영업지역대분류코드",
        "Rd06_District": "영업지역상세코드",
        "영업지역 상세코드": "영업지역상세코드",

        "Rd06_Stock_Cd_Gcode": "재고위치대분류코드",
        "재고위치 그룹코드": "재고위치대분류코드",
        "재고위치 대분류코드": "재고위치대분류코드",
        "Rd06_Stock_Cd": "재고위치상세코드",
        "재고위치 코드": "재고위치상세코드",
        "재고위치 상세코드": "재고위치상세코드",

        "Rd06_Add_Cd": "등록자코드",
        "등록자 코드": "등록자코드",
        "등록 자 코드": "등록자코드",
        "Rd06_Mod_Cd": "수정자코드",
        "수정자 코드": "수정자코드",
        "수정 자 코드": "수정자코드",

        "add_user_nm": "등록자명",
        "등록 자명": "등록자명",
        "등록자 이름": "등록자명",
        "mod_user_nm": "수정자명",
        "수정 자명": "수정자명",
        "수정자 이름": "수정자명",

        "Rd06_Add_Date": "등록일자",
        "등록 일자": "등록일자",
        "Rd06_Mod_Date": "수정일자",
        "수정 일자": "수정일자",


        "Rd06_Email": "이메일",
        "이메일주소": "이메일",
        "Rd06_Cellular_Phone": "휴대폰",
        "휴대폰번호": "휴대폰",
        "Rd06_Phone": "전화번호",
        "Rd06_Office_Phone": "사무실전화",
        "사무실 전화": "사무실전화",
        "Rd06_Remark": "비고",
        "Rd06_Del_Flag": "삭제여부",
        "삭제 여부": "삭제여부",
    }

    for src, dst in rename_candidates.items():
        if src in out.columns and dst not in out.columns:
            out = out.rename(columns={src: dst})

    # 거래처와 동일하게 최종 표시 컬럼은 이름 우선, 없으면 코드 fallback.
    if "등록자명" in out.columns:
        out["등록자"] = _norm_series(out["등록자명"])
    elif "등록자코드" in out.columns:
        out["등록자"] = _norm_series(out["등록자코드"])

    if "수정자명" in out.columns:
        out["수정자"] = _norm_series(out["수정자명"])
    elif "수정자코드" in out.columns:
        out["수정자"] = _norm_series(out["수정자코드"])

    front_cols = [                
        c for c in [
            "사용자코드",
            "사용자ID",
            "사번",
            "사용자명",
            "부서명",
            "직책",
            "영업지역",
            "재고위치",
            "이메일",
            "휴대폰",
            "전화번호",
            "사무실전화",
            "등록자",
            "등록일자",
            "수정자",
            "수정일자",
            "삭제여부",
            "비고",
        ]
        if c in out.columns
    ]

    rest_cols = [c for c in out.columns if c not in front_cols]
    view_df = out[front_cols + rest_cols].copy() if (front_cols or rest_cols) else out.copy()

    def _safe_norm_cell(v: Any) -> str:
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except Exception:
            pass

        if isinstance(v, memoryview):
            v = v.tobytes()

        if isinstance(v, (bytes, bytearray)):
            b = bytes(v)
            for enc in ("cp949", "euc-kr", "utf-8", "latin1"):
                try:
                    s = b.decode(enc).strip()
                    return "" if s in ("None", "nan", "<NA>") else s
                except Exception:
                    continue
            return b.hex()

        try:
            s = str(v).strip()
            return "" if s in ("None", "nan", "<NA>") else s
        except Exception:
            try:
                return repr(v)
            except Exception:
                return ""

    def _fmt_char8_date(v: Any) -> str:
        s = _safe_norm_cell(v)
        if not s:
            return ""
        if s in ("0", "00000000", "19000101", "20010101", "99999999"):
            return ""
        if re.fullmatch(r"\d{8}", s):
            try:
                dt = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
                if pd.isna(dt):
                    return ""
                return dt.strftime("%Y-%m-%d")
            except Exception:
                return s
        return s

    for col in view_df.columns:
        try:
            view_df[col] = _norm_series(view_df[col])
        except Exception:
            view_df[col] = view_df[col].map(_safe_norm_cell)

    for col in ("등록일자", "수정일자"):
        if col in view_df.columns:
            view_df[col] = view_df[col].map(_fmt_char8_date)

    # 화면 조회 순번
    if "순번" in view_df.columns:
        view_df = view_df.drop(columns=["순번"])
    view_df.insert(0, "순번", range(1, len(view_df) + 1))

    # 화면 표시는 한글 표시 컬럼만 사용한다.
    # 원본 Rd06_* 스키마 컬럼은 df 원본에 남겨두므로 Excel/CSV 다운로드와 LLM 분석에는 영향 없다.
    display_cols = [
        "순번",
        "사용자코드",
        "사용자ID",
        "사번",
        "사용자명",        
        "부서명",
        "직책",
        "영업지역",
        "재고위치",
        "이메일",
        "휴대폰",
        "전화번호",
        "사무실전화",
        "등록자코드",
        "등록자",
        "등록일자",
        "수정자코드",
        "수정자",
        "수정일자",
        "삭제여부",
        "비고",

        # 필요 시 우측에 붙일 한글 코드 컬럼
        "부서대분류코드",
        "부서상세코드",
        "직책대분류코드",
        "직책상세코드",
        "영업지역대분류코드",
        "영업지역상세코드",
        "재고위치대분류코드",
        "재고위치상세코드",
    ]

    display_cols = [c for c in display_cols if c in view_df.columns]
    return view_df[display_cols].copy()

def _build_users_query_condition(params: Dict[str, Any], total: int, display_count: int) -> str:
    field_specs = [
        ("text", "사용자코드", "사용자코드"),
        ("text", "사용자ID", "사용자ID"),
        ("text", "사번", "사번"),
        ("text", "사용자명", "사용자명"),
        ("text", "부서명", "부서명"),
        ("text", "직책", "직책"),
        ("text", "영업지역", "영업지역"),
        ("text", "재고위치", "재고위치"),
        ("text", "통합검색", "통합검색"),
        ("text", "등록자", "등록자"),
        ("date_range", "등록일자", "등록일자From", "등록일자To"),
        ("text", "수정자", "수정자"),
        ("date_range", "수정일자", "수정일자From", "수정일자To"),
    ]

    return build_master_query_condition(
        params,
        total=total,
        display_count=display_count,
        field_specs=field_specs,
        active_key="사용중만",
        active_text="사용구분 사용중",
    )


def _split_condition_and_note(query_condition: str) -> tuple[str, str]:
    lines = [x.strip() for x in str(query_condition or "").splitlines() if x.strip()]
    if not lines:
        return "전체", ""
    return lines[0], "\n".join(lines[1:]).strip()


def _build_users_master_llm_summary(
    df_all_display: pd.DataFrame,
    *,
    query_condition: str,
    total: int,
    display_count: int,
) -> dict[str, Any]:
    return build_master_llm_summary(
        df_all_display,
        master_name="사용자마스터",
        query_condition=query_condition,
        total=total,
        display_count=display_count,
        count_specs=[
            ("부서별 상위", "부서명", "dept_top"),
            ("직책별 상위", "직책", "duty_top"),
            ("영업지역별 상위", "영업지역", "district_top"),
            ("재고위치별 상위", "재고위치", "stock_top"),
            ("등록자별 상위", "등록자", "add_user_top"),
            ("수정자별 상위", "수정자", "mod_user_top"),
            ("사용구분별 상위", "삭제여부", "active_top"),
        ],
        year_specs=[
            ("등록연도별 상위", "등록일자", "add_year_top"),
            ("수정연도별 상위", "수정일자", "mod_year_top"),
        ],
        top_n=10,
        answer_rule="화면에는 일부만 표시될 수 있지만, 위 집계는 전체 조회결과 기준입니다.",
    )

def _service_search_users(
    *,
    user_cd: str = "",
    user_id: str = "",
    sabun: str = "",
    user_nm: str = "",
    department: str = "",
    duty: str = "",
    district: str = "",
    stock_cd: str = "",
    add_user_nm: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    keyword: str = "",
    only_active: bool = True,
    top: int = 200,    
) -> pd.DataFrame:
    fn = getattr(U, "search_users_full", None)
    if callable(fn):
        return _ensure_df(
            fn(
                top=top,
                only_active=only_active,
                user_cd=user_cd,
                user_id=user_id,
                sabun=sabun,
                user_nm=user_nm,
                department=department,
                duty=duty,
                district=district,
                stock_cd=stock_cd,
                add_user_nm=add_user_nm,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_nm=mod_user_nm,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
                keyword=keyword,
            )
        )

    fn2 = getattr(U, "search_rows", None)
    if callable(fn2):
        return _ensure_df(
            fn2(
                user_cd=user_cd,
                user_id=user_id,
                sabun=sabun,
                user_nm_kw=user_nm,
                department=department,
                duty=duty,
                district=district,
                stock_cd=stock_cd,
                add_user_nm_kw=add_user_nm,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_nm_kw=mod_user_nm,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
                keyword=keyword,
                only_active=only_active,
                top=top,
            )            
        )

    raise AttributeError("rddbc060_service 에 search_users_full / search_rows 가 없습니다.")


# ------------------------------------------------------------------------------
# 1) 사용자목록 + 부서명
# ------------------------------------------------------------------------------
def render_user_list_with_dept() -> Dict[str, Any]:
    master_max_rows = _master_max_rows()
    st.subheader("👥 사용자목록 + 부서명")
    st.caption("부서/직책/영업지역/재고위치는 업무코드 기준 선택형 필터를 사용합니다.")

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_users_list_{ns}"

    dept_options = _department_options()
    duty_options = _duty_options()
    district_options = _district_options()
    stock_options = _stock_options()

    dept_labels = [label for label, _ in dept_options]
    duty_labels = [label for label, _ in duty_options]
    district_labels = [label for label, _ in district_options]
    stock_labels = [label for label, _ in stock_options]

    dept_code_by_label = {label: code for label, code in dept_options}
    duty_code_by_label = {label: code for label, code in duty_options}
    district_code_by_label = {label: code for label, code in district_options}
    stock_code_by_label = {label: code for label, code in stock_options}

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):


        c1, c2, c3, c4 = st.columns(4)
        with c1:
            user_cd = st.text_input("사용자코드", value="", key=f"__users_user_cd__{ns}")
        with c2:
            user_id = st.text_input("사용자ID", value="", key=f"__users_user_id__{ns}")
        with c3:
            sabun = st.text_input("사번", value="", key=f"__users_sabun__{ns}")
        with c4:
            user_nm = st.text_input("사용자명", value="", key=f"__users_user_nm__{ns}")

        c5, c6, c7, c8 = st.columns(4)
        with c5:
            dept_label = st.selectbox("부서명", options=dept_labels, index=0, key=f"__users_dept__{ns}")
        with c6:
            duty_label = st.selectbox("직책", options=duty_labels, index=0, key=f"__users_duty__{ns}")
        with c7:
            district_label = st.selectbox("영업지역", options=district_labels, index=0, key=f"__users_district__{ns}")
        with c8:
            stock_label = st.selectbox("재고위치", options=stock_labels, index=0, key=f"__users_stock__{ns}")

        c9, c10, c11 = st.columns([3, 1, 2])
        with c9:
            keyword = st.text_input("통합검색", value="", key=f"__users_keyword__{ns}")
        with c10:
            only_active = st.checkbox("사용중만", value=True, key=f"__users_only_active__{ns}")
        with c11:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        audit_filter = render_master_audit_filter(
            prefix="users",
            ns=ns,
            expanded=False,
        )

        add_user_nm = audit_filter["add_user_nm"]
        add_date_from = audit_filter["add_date_from"]
        add_date_to = audit_filter["add_date_to"]
        mod_user_nm = audit_filter["mod_user_nm"]
        mod_date_from = audit_filter["mod_date_from"]
        mod_date_to = audit_filter["mod_date_to"]

        submitted = st.form_submit_button("조회", type="primary")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "사용자목록 + 부서명",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    department = dept_code_by_label.get(dept_label, "")
    duty = duty_code_by_label.get(duty_label, "")
    district = district_code_by_label.get(district_label, "")
    stock_cd = stock_code_by_label.get(stock_label, "")

    params = {
        "사용자코드": _clean_text(user_cd),
        "사용자ID": _clean_text(user_id),
        "사번": _clean_text(sabun),
        "사용자명": _clean_text(user_nm),
        "부서명": dept_label,
        "부서코드": department,
        "직책": duty_label,
        "직책코드": duty,
        "영업지역": district_label,
        "영업지역코드": district,
        "재고위치": stock_label,
        "재고위치코드": stock_cd,
        "통합검색": _clean_text(keyword),
        "등록자": add_user_nm,
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}"
            if (add_date_from or add_date_to)
            else ""
        ),
        "등록일자From": add_date_from,
        "등록일자To": add_date_to,
        "수정자": mod_user_nm,
        "수정일자": (
            mod_date_from
            if (mod_date_from and mod_date_from == mod_date_to)
            else f"{mod_date_from}~{mod_date_to}"
            if (mod_date_from or mod_date_to)
            else ""
        ),
        "수정일자From": mod_date_from,
        "수정일자To": mod_date_to,
        "사용중만": bool(only_active),
        "조회상한": int(master_max_rows),        
    }

    try:
        display_top = int(master_max_rows)
        fetch_top = int(master_max_rows)

        df_raw = _service_search_users(
            user_cd=_clean_text(user_cd),
            user_id=_clean_text(user_id),
            sabun=_clean_text(sabun),
            user_nm=_clean_text(user_nm),
            department=department,
            duty=duty,
            district=district,
            stock_cd=stock_cd,
            add_user_nm=add_user_nm,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm=mod_user_nm,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            keyword=_clean_text(keyword),
            only_active=bool(only_active),
            top=fetch_top,
        )

        df = _prepare_users_df(df_raw)

        total = int(len(df))
        df_display_all = _build_user_list_view(df)
        # 화면 표시 제한은 chat/sims_panel 공통 표시 제한에서 처리한다.
        df_display = df_display_all.copy()
        display_count = int(len(df_display))

        query_condition = _build_users_query_condition(params, total, display_count)
        condition_text, note = _split_condition_and_note(query_condition)

        users_master_summary = _build_users_master_llm_summary(
            df_display_all,
            query_condition=query_condition,
            total=total,
            display_count=display_count,
        )
        llm_summary_md = str(users_master_summary.get("llm_summary_md") or "")
        summary_parts = []
        if str(note or "").strip():
            summary_parts.append(str(note).strip())
        if str(llm_summary_md or "").strip():
            summary_parts.append(str(llm_summary_md).strip())
        summary_md = "\n\n".join(summary_parts).strip()

        dept_col = _pick_col(df_display_all, ["부서명"])
        dept_list = df_display_all[dept_col].dropna().unique().tolist() if dept_col else []

        return {
            "final": True,
            "type": "table",
            "title": "사용자목록 + 부서명",
            "action": "사용자목록 + 부서명",
            "params": params,
            "df": df,
            "df_display": df_display,
            "meta": {
                "조회상한": int(fetch_top),
                "총사용자수": total,
                "row_count": total,
                "row_count_total": total,
                "display_row_count": display_count,
                "show_n": display_count,
                "row_count_loaded": total,
                "download_row_count": total,
                "fetch_limit": fetch_top,
                "부서목록": dept_list,
                "부서코드": department,
                "직책코드": duty,
                "영업지역코드": district,
                "재고위치코드": stock_cd,
                "source": "사용자마스터(Rddbc060)",
                "query_summary": condition_text,
                "condition": condition_text,
                "summary_md": summary_md,
                "analysis_type": "users_master",
                "llm_summary_kind": "users_master_summary",
                "llm_summary_md": llm_summary_md,
                "users_master_summary": users_master_summary,
                "analysis_row_count": total,
                "row_count_total_for_analysis": total,
                "summary_basis": "전체 조회결과 기준",
                "field_notes": (
                    "사용자 마스터 분석은 전체 조회결과 기준 집계요약을 우선 근거로 답합니다. "
                    "화면 표시는 일부 행으로 제한될 수 있습니다."
                ),
            },
        }
    

    except Exception as e:
        log.exception("[view.users] render_user_list_with_dept failed")
        return {
            "final": False,
            "type": "text",
            "title": "사용자목록 + 부서명 오류",
            "data": str(e),
        }


# ------------------------------------------------------------------------------
# 2) 부서별 사용자 수
# ------------------------------------------------------------------------------
def render_user_count_by_dept() -> Dict[str, Any]:
    master_max_rows = _master_max_rows()
    st.subheader("🏢 부서별 사용자 수")

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_users_dept_{ns}"

    dept_options = _department_options()
    dept_labels = [label for label, _ in dept_options]
    dept_code_by_label = {label: code for label, code in dept_options}

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            dept_label = st.selectbox("부서명", options=dept_labels, index=0, key=f"__users_count_dept__{ns}")
        with c2:
            only_active = st.checkbox("사용중만", value=True, key=f"__users_count_only_active__{ns}")
        with c3:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        submitted = st.form_submit_button("집계", type="primary")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "부서별 사용자 수",
            "data": "[집계] 버튼을 눌러 실행하세요.",
        }

    department = dept_code_by_label.get(dept_label, "")

    try:
        df_raw = _service_search_users(
            department=department,
            only_active=bool(only_active),
            top=int(master_max_rows),
        )
        df = _prepare_users_df(df_raw)

        if "부서명" in df.columns:
            out = df.groupby("부서명", dropna=False, as_index=False).size()
            if "size" in out.columns:
                out = out.rename(columns={"size": "인원수"})
        else:
            out = df.copy()

        return {
            "final": True,
            "type": "table",
            "title": "부서별 사용자 수",
            "action": "부서별 사용자 수",
            "params": {
                "부서명": dept_label,
                "부서코드": department,
                "사용중만": bool(only_active),
                "조회상한": int(master_max_rows),
            },
            "df": out,
            "df_display": out,
            "meta": {
                "부서수": int(len(out)),
                "총사용자수": int(out["인원수"].sum()) if "인원수" in out.columns else None,
                "부서코드": department,
            },
        }

    except Exception as e:
        log.exception("[view.users] render_user_count_by_dept failed")
        return {
            "final": False,
            "type": "text",
            "title": "부서별 사용자 수 오류",
            "data": str(e),
        }


# ------------------------------------------------------------------------------
# 3) 최근 입사자 (등록일자 기준)
# ------------------------------------------------------------------------------
def render_recent_hires() -> Dict[str, Any]:
    from datetime import date, timedelta

    st.subheader("🆕 최근 입사자(등록기준)")
    st.caption("기준일자 이후 등록된 사용자를 조회합니다. 등록일자가 비정상인 경우에만 수정일자를 대체 사용합니다.")

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_users_recent_{ns}"
    cache_key = "__users_recent_last_payload"

    def _parse_char8_date(sr: pd.Series) -> pd.Series:
        if sr is None or len(sr) == 0:
            return pd.Series(dtype="datetime64[ns]")

        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace(
                {
                    "": None,
                    "0": None,
                    "00000000": None,
                    "19000101": None,
                    "20010101": None,
                    "99999999": None,
                    "None": None,
                    "nan": None,
                    "<NA>": None,
                }
            )
        )
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")

    def _parse_any_datetime(sr: pd.Series) -> pd.Series:
        if sr is None or len(sr) == 0:
            return pd.Series(dtype="datetime64[ns]")

#   2026-04-04: 날짜 형식 보정 추가 
#   - 기존 _parse_char8_date 로는 등록일자/수정일자 컬럼의 날짜 형식이 제각각인 경우에 대응이 어려워서,
#   날짜 형식 보정 로직을 강화한 _fmt_char8_date_series 함수를 새로 추가합니다.   

    def _fmt_char8_date_series(sr: pd.Series) -> pd.Series:
        if sr is None or len(sr) == 0:
            return pd.Series(dtype="object")

        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace(
                {
                    "": None,
                    "0": None,
                    "00000000": None,
                    "19000101": None,
                    "20010101": None,
                    "99999999": None,
                    "None": None,
                    "nan": None,
                    "<NA>": None,
                }
            )
        )

        dt = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        out = dt.dt.strftime("%Y-%m-%d")
        return out.fillna("")
#  2026-04-04: 날짜 형식 보정 추가 - 끝
        s = sr.fillna("").astype(str).str.strip().replace({"": None, "None": None, "nan": None, "<NA>": None})
        return pd.to_datetime(s, errors="coerce")

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):
        c1, c2 = st.columns([2, 1])
        with c1:
            base_date = st.date_input(
                "기준일자",
                value=date.today() - timedelta(days=30),
                key=f"__users_recent_base_date__{ns}",
            )
        with c2:
            # 조회 화면 원칙상 E도 보여야 하므로 기본값은 False
            only_active = st.checkbox(
                "사용중만",
                value=False,
                key=f"__users_recent_only_active__{ns}",
            )

        submitted = st.form_submit_button("조회", type="primary")

    if not submitted:
        last_payload = st.session_state.get(cache_key)
        if isinstance(last_payload, dict):
            return last_payload
        return {
            "final": False,
            "type": "text",
            "title": "최근 입사자(등록기준)",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    try:
        # 기존 호환 래퍼 사용
        df_raw = _service_search_users(
            only_active=bool(only_active),
            top=_master_max_rows(),
        )
        df = _prepare_users_df(df_raw)

        if df.empty:
            payload = {
                "final": True,
                "type": "table",
                "title": "최근 입사자(등록기준)",
                "action": "최근 입사자",
                "params": {
                    "기준일자": base_date.strftime("%Y-%m-%d"),
                    "판정기준": "등록우선/수정대체",
                    "사용중만": bool(only_active),
                },
                "df": df,
                "df_display": df,
                "meta": {
                    "선택된인원수": 0,
                    "기준일자": base_date.strftime("%Y-%m-%d"),
                    "판정기준": "등록우선/수정대체",
                },
            }
            st.session_state[cache_key] = payload
            return payload

        add_date_col = _pick_col(df, ["등록일자", "등록 일자", "Rd06_Add_Date"])
        mod_date_col = _pick_col(df, ["수정일자", "수정 일자", "Rd06_Mod_Date"])
        add_date = _parse_char8_date(df[add_date_col]) if add_date_col else pd.Series(pd.NaT, index=df.index)
        mod_date = _parse_char8_date(df[mod_date_col]) if mod_date_col else pd.Series(pd.NaT, index=df.index)

        # 최근 입사자 판정:
        # 1) 등록일자 우선
        # 2) 등록일자가 비정상이면 수정일자 대체
        effective_date = add_date.combine_first(mod_date)

        effective_ts = pd.to_datetime(effective_date, errors="coerce")

        basis = pd.Series("", index=df.index, dtype="object")
        basis.loc[add_date.notna()] = "등록"
        basis.loc[add_date.isna() & mod_date.notna()] = "수정대체"

        mask = effective_date.notna() & (effective_date.dt.date >= base_date)
        out = df.loc[mask].copy()

        if out.empty:
            df_display = pd.DataFrame(
                columns=[
                    "순번",
                    "사용자코드",
                    "사용자ID",
                    "사번",
                    "사용자명",
                    "부서명",
                    "직책",
                    "영업지역",
                    "재고위치",
                    "삭제여부",
                    "판정기준구분",
                    "판정기준일자",
                    "판정기준일시",
                    "등록일자",
                    "수정일자",
                ]
            )
        else:
            out["판정기준구분"] = basis.loc[out.index]
            out["판정기준일자"] = effective_date.loc[out.index].dt.strftime("%Y-%m-%d")
            out["판정기준일시"] = effective_ts.loc[out.index].dt.strftime("%Y-%m-%d %H:%M:%S")
            out["_sort_ts"] = effective_ts.loc[out.index]

            out = out.sort_values("_sort_ts", ascending=False, na_position="last").drop(columns=["_sort_ts"])

            display_order = [
                "순번",
                "사용자코드",
                "사용자ID",
                "사번",
                "사용자명",
                "부서명",
                "직책",
                "영업지역",
                "재고위치",
                "삭제여부",
                "판정기준구분",
                "판정기준일자",
                "판정기준일시",
                "등록일자",
                "수정일자",
            ]

            if "순번" in out.columns:
                out = out.drop(columns=["순번"])
            out.insert(0, "순번", range(1, len(out) + 1))

#             2026-04-04: 날짜 형식 보정 추가
            display_cols = [c for c in display_order if c in out.columns]
            df_display = out[display_cols].copy()

            for col in ("등록일자", "수정일자"):
                if col in df_display.columns:
                    df_display[col] = _fmt_char8_date_series(df_display[col])
#  2026-04-04: 날짜 형식 보정 추가 - 끝
        payload = {
            "final": True,
            "type": "table",
            "title": "최근 입사자(등록기준)",
            "action": "최근 입사자",
            "params": {
                "기준일자": base_date.strftime("%Y-%m-%d"),
                "판정기준": "등록우선/수정대체",
                "사용중만": bool(only_active),
            },
            "df": out,
            "df_display": df_display,
            "meta": {
                "선택된인원수": int(len(out)),
                "기준일자": base_date.strftime("%Y-%m-%d"),
                "등록일자컬럼": add_date_col,
                "수정일자컬럼": mod_date_col,
                "등록시간컬럼": add_time_col,
                "수정시간컬럼": mod_time_col,
                "판정기준": "등록우선/수정대체",
            },
        }

        st.session_state[cache_key] = payload
        return payload

    except Exception as e:
        log.exception("[view.users] render_recent_hires failed")
        return {
            "final": False,
            "type": "text",
            "title": "최근 입사자 오류",
            "data": str(e),
        }
    
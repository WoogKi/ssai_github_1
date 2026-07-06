# app/sims/views/goods.py

from __future__ import annotations

import os
from typing import Any, Dict
import logging

import pandas as pd
import streamlit as st

from app.services.rddbc040_service import search_goods_full, get_goods_detail_full
from app.services.utils import apply_labels

from app.db.mssql_client import read_df

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


_GOODS_LIST_PREFER_COLS = [
    "제품코드", "보험코드", "제품명", "제약사명",
    "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
    "규격", "단위",
    "계산단위",
    "보험수가변경일자", "보험가격", "보험단가",
    "이전보험수가변경일자", "이전보험가격", "이전보험단가",
    "최종단가변경일자", "단가",
    "특수관리제품코드", "특수관리제품",
    "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
    "사용구분", "삭제/사용여부",
    "등록자명", "등록일자", "수정자명", "수정일자",
]

_GOODS_DETAIL_PREFER_COLS = [
    "제품코드", "보험코드", "제품명", "출력명", "약어명", "제약사명",
    "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
    "규격", "단위",
    "계산단위",
    "보험수가변경일자", "보험가격", "보험단가",
    "이전보험수가변경일자", "이전보험가격", "이전보험단가",
    "최종단가변경일자", "단가",
    "특수관리제품코드", "특수관리제품",
    "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
    "사용구분", "삭제/사용여부",
    "등록자명", "등록일자", "수정자명", "수정일자",
]

def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()

def _build_goods_display_df(df: pd.DataFrame, *, detail: bool = False) -> pd.DataFrame:
    work = _ensure_df(df)
    if work.empty:
        return work

    def _pick_col(df_src: pd.DataFrame, candidates: list[str]) -> str:
        for c in candidates:
            if c in df_src.columns:
                return c
        return ""

    def _norm_series(sr: pd.Series) -> pd.Series:
        return (
            sr.fillna("")
            .astype(str)
            .replace({"None": "", "nan": "", "<NA>": ""})
            .str.strip()
        )

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .replace({
                "": None,
                "0": None,
                "00000000": None,
                "19000101": None,
                "20010101": None,
                "99999999": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dt = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        return dt.dt.strftime("%Y-%m-%d").fillna("")

    def _fmt_datetime(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace({
                "": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dt = pd.to_datetime(s, errors="coerce")
        return dt.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    out = work.copy()

    # 이름 컬럼을 코드 컬럼보다 먼저 우선 사용
    add_name_col = _pick_col(out, ["__raw_add_user_nm", "add_user_nm", "등록자명", "등록자"])
    add_code_col = _pick_col(out, ["등록자코드", "Rd04_Add_Cd"])
    mod_name_col = _pick_col(out, ["__raw_mod_user_nm", "mod_user_nm", "수정자명", "수정자"])
    mod_code_col = _pick_col(out, ["수정자코드", "Rd04_Mod_Cd"])

    if add_name_col:
        out["등록자"] = _norm_series(out[add_name_col])
    elif add_code_col:
        out["등록자"] = _norm_series(out[add_code_col])
    else:
        out["등록자"] = ""

    if mod_name_col:
        out["수정자"] = _norm_series(out[mod_name_col])
    elif mod_code_col:
        out["수정자"] = _norm_series(out[mod_code_col])
    else:
        out["수정자"] = ""

    if "등록일자" not in out.columns and "Rd04_Add_Date" in out.columns:
        out["등록일자"] = out["Rd04_Add_Date"]
    if "수정일자" not in out.columns and "Rd04_Mod_Date" in out.columns:
        out["수정일자"] = out["Rd04_Mod_Date"]
    for col in ["보험수가변경일자", "이전보험수가변경일자", "최종단가변경일자", "등록일자", "수정일자"]:
        if col in out.columns:
            out[col] = _fmt_char8_date(out[col])


    if "계산단위" in out.columns:
        num = pd.to_numeric(out["계산단위"], errors="coerce")
        non_na = num.dropna()
        if not non_na.empty and ((non_na % 1) == 0).all():
            out["계산단위"] = num.round(0).astype("Int64")
        else:
            out["계산단위"] = num.round(3)

    for col in ["보험가격", "보험단가", "이전보험가격", "이전보험단가", "단가"]:
        if col in out.columns:
            num = pd.to_numeric(out[col], errors="coerce").round(0)
            out[col] = num.astype("Int64")

    if detail:
        preferred = [
            "제품코드", "보험코드", "제품명", "출력명", "약어명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
                ]
    else:
        preferred = [
            "제품코드", "보험코드", "제품명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
        ]

    preferred = [c for c in preferred if c in out.columns]
    return out[preferred].copy() if preferred else out.copy()

def _format_goods_full_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .replace({
                "": None,
                "0": None,
                "00000000": None,
                "19000101": None,
                "20010101": None,
                "99999999": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dt = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        return dt.dt.strftime("%Y-%m-%d").fillna("")

    def _fmt_datetime(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace({
                "": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dt = pd.to_datetime(s, errors="coerce")
        return dt.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    for col in ["보험적용일", "등록일자", "수정일자"]:
        if col in out.columns:
            out[col] = _fmt_char8_date(out[col])


    return out


def _build_goods_params(
    *,
    physic_cd: str = "",
    keyword: str = "",
    insu_cd: str = "",
    barcode: str = "",
    ven_nm_kw: str = "",
    group_name_kw: str = "",
    di_name_kw: str = "",

    physic_gu_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",

    unit_price_kw: str = "",
    final_price_date_kw: str = "",
    only_use: bool = True,
    top: int = 2000,
) -> Dict[str, Any]:
    return {
        "제품코드": physic_cd,
        "제품명": keyword,
        "보험코드": insu_cd,
        "바코드": barcode,
        "제약사명": ven_nm_kw,
        "제품그룹명": group_name_kw,
        "구분명": di_name_kw,
        "제품분류명": physic_gu_name_kw,

        "등록자": add_user_nm_kw,
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}"
            if (add_date_from or add_date_to)
            else ""
        ),

        "수정자": mod_user_nm_kw,
        "수정일자": mod_date_from if (mod_date_from and mod_date_from == mod_date_to) else f"{mod_date_from}~{mod_date_to}" if (mod_date_from or mod_date_to) else "",
        "단가": unit_price_kw,
        "최종단가변경일자": final_price_date_kw,
        "사용만": bool(only_use),
        "TopN": int(top),
    }

def _build_goods_query_summary(
    *,
    physic_cd: str = "",
    keyword: str = "",
    insu_cd: str = "",
    barcode: str = "",
    ven_nm_kw: str = "",
    group_name_kw: str = "",
    di_name_kw: str = "",

    physic_gu_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",

    unit_price_kw: str = "",
    final_price_date_kw: str = "",
) -> str:
    parts = []
    if physic_cd:
        parts.append(f"제품코드 {physic_cd}")
    if keyword:
        parts.append(f"제품명 {keyword}")
    if insu_cd:
        parts.append(f"보험코드 {insu_cd}")
    if barcode:
        parts.append(f"바코드 {barcode}")
    if ven_nm_kw:
        parts.append(f"제약사명 {ven_nm_kw}")
    if group_name_kw:
        parts.append(f"제품그룹명 {group_name_kw}")
    if di_name_kw:
        parts.append(f"구분명 {di_name_kw}")
    if physic_gu_name_kw:
        parts.append(f"제품분류명 {physic_gu_name_kw}")

    if add_user_nm_kw:
        parts.append(f"등록자 {add_user_nm_kw}")
    if add_date_from or add_date_to:
        if add_date_from and add_date_from == add_date_to:
            parts.append(f"등록일자 {add_date_from}")
        else:
            parts.append(f"등록일자 {add_date_from or ''}~{add_date_to or ''}")

    if mod_user_nm_kw:
        parts.append(f"수정자 {mod_user_nm_kw}")
    if mod_date_from or mod_date_to:
        if mod_date_from and mod_date_from == mod_date_to:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")

    if unit_price_kw:
        parts.append(f"단가 {unit_price_kw}")
    if final_price_date_kw:
        parts.append(f"최종단가변경일자 {final_price_date_kw}")
    return " / ".join(parts)

def _build_goods_query_condition(params: Dict[str, Any], total: int, display_count: int) -> str:
    field_specs = [
        ("text", "제품코드", "제품코드"),
        ("text", "제품명", "제품명"),
        ("text", "보험코드", "보험코드"),
        ("text", "바코드", "바코드"),
        ("text", "제약사명", "제약사명"),
        ("text", "제품그룹명", "제품그룹명"),
        ("text", "구분명", "구분명"),
        ("text", "제품분류명", "제품분류명"),
        ("text", "등록자", "등록자"),
        ("date_range", "등록일자", "등록일자From", "등록일자To"),
        ("text", "수정자", "수정자"),
        ("date_range", "수정일자", "수정일자From", "수정일자To"),
        ("text", "단가", "단가"),
        ("text", "최종단가변경일자", "최종단가변경일자"),
    ]

    return build_master_query_condition(
        params,
        total=total,
        display_count=display_count,
        field_specs=field_specs,
        active_key="사용만",
        active_text="사용구분 사용중",
    )


def _split_condition_and_note(query_condition: str) -> tuple[str, str]:
    lines = [x.strip() for x in str(query_condition or "").splitlines() if x.strip()]
    if not lines:
        return "전체", ""
    condition = lines[0]
    note = "\n".join(lines[1:]).strip()
    return condition, note


def _build_goods_master_llm_summary(
    df_all_display: pd.DataFrame,
    *,
    query_condition: str,
    total: int,
    display_count: int,
) -> dict[str, Any]:
    return build_master_llm_summary(
        df_all_display,
        master_name="제품마스터",
        query_condition=query_condition,
        total=total,
        display_count=display_count,
        count_specs=[
            ("제품그룹별 상위", "제품그룹명", "group_top"),
            ("제품구분별 상위", "구분명", "di_top"),
            ("제품플래그별 상위", "제품플래그명", "flag_top"),
            ("함량별 상위", "함량명", "cons_top"),
            ("제품분류별 상위", "제품분류명", "class_top"),
            ("제약사별 상위", "제약사명", "maker_top"),
            ("특수관리제품별 상위", "특수관리제품", "special_top"),
            ("사용구분별 상위", "사용구분", "use_top"),
            ("등록자별 상위", "등록자", "add_user_top"),
            ("수정자별 상위", "수정자", "mod_user_top"),
        ],
        year_specs=[
            ("등록연도별 상위", "등록일자", "add_year_top"),
            ("수정연도별 상위", "수정일자", "mod_year_top"),
        ],
        top_n=10,
        answer_rule="화면에는 일부만 표시될 수 있지만, 위 집계는 전체 조회결과 기준입니다.",
    )


def view_goods_list(widget_ns: str = "0") -> Dict[str, Any]:
    title = "제품코드 목록"
    master_max_rows = _master_max_rows()
    st.subheader("제품코드(040) 목록")
    st.caption("제약사명/제품그룹명/구분명/제품분류명은 업무코드 기준 선택형 필터를 사용합니다.")

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_code_name_options(gcode: str) -> list[str]:
        sql = """
SELECT DISTINCT LTRIM(RTRIM(Rd01_Hnm)) AS code_name
FROM dbo.Rddbc010 WITH (NOLOCK)
WHERE Rd01_Gcode = ?
  AND ISNULL(Rd01_Hnm, '') <> ''
ORDER BY LTRIM(RTRIM(Rd01_Hnm))
""".strip()
        try:
            df_code = read_df(sql, (gcode,))
        except Exception:
            log.exception("[view.goods] code option load failed gcode=%s", gcode)
            return ["전체"]

        if df_code is None or df_code.empty or "code_name" not in df_code.columns:
            return ["전체"]

        vals: list[str] = []
        seen: set[str] = set()
        for v in df_code["code_name"].tolist():
            s = str(v or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            vals.append(s)

        return ["전체"] + vals

    group_options = _load_code_name_options("0013")
    di_options = _load_code_name_options("0004")
    class_options = _load_code_name_options("0028")

    with st.form(
        f"goods_list_form_{widget_ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        c1, c2, c3 = st.columns(3)
        with c1:
            physic_cd = st.text_input("제품코드(정확)", value="")
        with c2:
            keyword = st.text_input("키워드(제품명/제품코드/보험코드)", value="")
        with c3:
            insu_cd = st.text_input("보험코드(정확)", value="")

        c4, c5, c6 = st.columns(3)
        with c4:
            ven_nm_kw = st.text_input("제약사명 포함", value="")
        with c5:
            barcode = st.text_input("바코드(정확, 1~5)", value="")
        with c6:
            unit_price_kw = st.text_input("단가(정확 또는 범위)", value="")

        c7, c8, c9 = st.columns(3)
        with c7:
            group_name_sel = st.selectbox("제품그룹명", options=group_options, index=0, key=f"goods_group_name_{widget_ns}")
        with c8:
            di_name_sel = st.selectbox("구분명", options=di_options, index=0, key=f"goods_di_name_{widget_ns}")
        with c9:
            physic_gu_name_sel = st.selectbox("제품분류명", options=class_options, index=0, key=f"goods_physic_gu_name_{widget_ns}")

        c10, c11, c12 = st.columns(3)
        with c10:
            final_price_date_kw = st.text_input("최종단가변경일자(YYYYMMDD 또는 범위)", value="")
        with c11:
            only_use = st.checkbox("사용(Use_Gu=0)만", value=True)
        with c12:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        audit_filter = render_master_audit_filter(
            prefix="goods",
            ns=widget_ns,
            expanded=False,
        )

        add_user_nm = audit_filter["add_user_nm"]
        add_date_from = audit_filter["add_date_from"]
        add_date_to = audit_filter["add_date_to"]
        mod_user_nm = audit_filter["mod_user_nm"]
        mod_date_from = audit_filter["mod_date_from"]
        mod_date_to = audit_filter["mod_date_to"]

        submitted = st.form_submit_button("조회", type="primary")

    log.info("[view.goods] submitted=%s", submitted)

    if not submitted:
        return {
            "final": False,
            "title": title,
            "action": title,
            "params": {},
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    physic_cd = str(physic_cd or "").strip()
    keyword = str(keyword or "").strip()
    insu_cd = str(insu_cd or "").strip()
    barcode = str(barcode or "").strip()
    ven_nm_kw = str(ven_nm_kw or "").strip()
    unit_price_kw = str(unit_price_kw or "").strip()
    final_price_date_kw = str(final_price_date_kw or "").strip()

    group_name_kw = "" if str(group_name_sel or "").strip() == "전체" else str(group_name_sel or "").strip()
    di_name_kw = "" if str(di_name_sel or "").strip() == "전체" else str(di_name_sel or "").strip()
    physic_gu_name_kw = "" if str(physic_gu_name_sel or "").strip() == "전체" else str(physic_gu_name_sel or "").strip()

    only_use = bool(only_use)
    display_top = int(master_max_rows)
    fetch_top = int(master_max_rows)

    df = search_goods_full(
        top=fetch_top,

        keyword=keyword,
        physic_cd=physic_cd,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,

        add_user_nm_kw=add_user_nm,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,

        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
        only_use=only_use,

    )
    df = _ensure_df(df)
    loaded_total = int(len(df))
    try:
        db_total_count = int(df.attrs.get("row_count_total", loaded_total) or loaded_total)
    except Exception:
        db_total_count = loaded_total

    params_out = _build_goods_params(
        physic_cd=physic_cd,
        keyword=keyword,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,
        add_user_nm_kw=add_user_nm,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
        only_use=only_use,
        top=fetch_top,
    )
    params_out.pop("TopN", None)
    params_out["조회상한"] = fetch_top

    params_out["등록일자From"] = add_date_from
    params_out["등록일자To"] = add_date_to
    params_out["수정일자From"] = mod_date_from
    params_out["수정일자To"] = mod_date_to

    query_summary = _build_goods_query_summary(
        physic_cd=physic_cd,
        keyword=keyword,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,
        add_user_nm_kw=add_user_nm,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
    )

    if df.empty:
        st.warning("조회 결과가 없습니다.")
        return {
            "final": True,
            "type": "text",
            "title": f"{title} (0건)",
            "action": title,
            "params": params_out,
            "df": pd.DataFrame(),
            "df_display": pd.DataFrame(),
            "meta": {

                "row_count": 0,
                "row_count_total": 0,
                "display_row_count": 0,
                "show_n": 0,
                "조회상한": fetch_top,
                "db_total_count": db_total_count,
                "row_count_loaded": 0,
                "download_row_count": 0,
                "fetch_limit": fetch_top,
                "fetch_limited": bool(db_total_count > 0),
                "empty_result": True,
                "_force_push": True,

                "only_use": only_use,
                "keyword": keyword,
                "ven_nm_kw": ven_nm_kw,
                "physic_cd": physic_cd,
                "insu_cd": insu_cd,
                "barcode": barcode,
                "group_name_kw": group_name_kw,
                "di_name_kw": di_name_kw,
                "physic_gu_name_kw": physic_gu_name_kw,
                "unit_price_kw": unit_price_kw,
                "final_price_date_kw": final_price_date_kw,
                "add_user_nm": add_user_nm,
                "add_date_from": add_date_from,
                "add_date_to": add_date_to,
                "mod_user_nm": mod_user_nm,
                "mod_date_from": mod_date_from,
                "mod_date_to": mod_date_to,

                "source": "제품코드마스터(Rddbc040)",
                "query_summary": query_summary,
                "condition": query_summary or "전체",
                "summary_md": "해당 조회조건의 자료가 없습니다.",
                "analysis_type": "goods_master",
                "summary_basis": "전체 조회결과 기준",

            },
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
        }

    df = apply_labels(df, "rddbc040")

    total = loaded_total
    df_display_all = _build_goods_display_df(df, detail=False)
    # 화면 표시 제한은 chat/sims_panel 공통 표시 제한에서 처리한다.
    df_display = df_display_all.copy()
    display_count = int(len(df_display))

    query_condition = _build_goods_query_condition(params_out, total, display_count)
    condition_text, note = _split_condition_and_note(query_condition)

    goods_master_summary = _build_goods_master_llm_summary(
        df_display_all,
        query_condition=query_condition,
        total=total,
        display_count=display_count,
    )
    llm_summary_md = str(goods_master_summary.get("llm_summary_md") or "")
    summary_parts = []
    if str(note or "").strip():
        summary_parts.append(str(note).strip())
    if str(llm_summary_md or "").strip():
        summary_parts.append(str(llm_summary_md).strip())
    summary_md = "\n\n".join(summary_parts).strip()

    meta = {
        "row_count": total,
        "row_count_total": total,
        "row_count_loaded": total,
        "download_row_count": total,
        "display_row_count": display_count,
        "show_n": display_count,

        "db_total_count": db_total_count,
        "fetch_limit": fetch_top,
        "fetch_limited": bool(db_total_count > total),
        "column_count": int(len(df.columns)),
        "조회상한": fetch_top,
        "only_use": only_use,
        "keyword": keyword,
        "ven_nm_kw": ven_nm_kw,
        "physic_cd": physic_cd,
        "insu_cd": insu_cd,
        "barcode": barcode,
        "group_name_kw": group_name_kw,
        "di_name_kw": di_name_kw,
        "physic_gu_name_kw": physic_gu_name_kw,
        "add_user_nm": add_user_nm,
        "add_date_from": add_date_from,
        "add_date_to": add_date_to,
        "mod_user_nm": mod_user_nm,
        "mod_date_from": mod_date_from,
        "mod_date_to": mod_date_to,
        "unit_price_kw": unit_price_kw,
        "final_price_date_kw": final_price_date_kw,
        "source": "제품코드마스터(Rddbc040)",
        "query_summary": condition_text,
        "condition": condition_text,
        "summary_md": summary_md,
        "analysis_type": "goods_master",
        "llm_summary_kind": "goods_master_summary",
        "llm_summary_md": llm_summary_md,
        "goods_master_summary": goods_master_summary,
        "analysis_row_count": total,
        "row_count_total_for_analysis": total,
        "summary_basis": "전체 조회결과 기준",
    }

    return {
        "final": True,
        "type": "table",
        "title": title,
        "action": title,
        "params": params_out,
        "df": df,
        "df_display": df_display,
        "meta": meta,
    }


def view_goods_detail(widget_ns: str = "0") -> Dict[str, Any]:
    st.subheader("제품코드(040) 상세")

    with st.form(
        f"goods_detail_form_{widget_ns}",
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        physic_cd = st.text_input("제품코드", value="")
        submitted = st.form_submit_button("조회")

    physic_cd = str(physic_cd or "").strip()
    log.info("[view.goods.detail] submitted=%s physic_cd=%r", submitted, physic_cd)

    if not submitted:
        st.caption("제품코드를 입력하고 ‘조회’를 눌러주세요.")
        return {
            "final": False,
            "title": "제품코드 상세",
            "action": "제품코드 상세",
            "params": {},
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    df = get_goods_detail_full(physic_cd=physic_cd)
    df = _ensure_df(df)

    query_summary = _build_goods_query_summary(physic_cd=physic_cd)
    params_out = {
        "제품코드": physic_cd,
    }

    if df.empty:
        st.warning("해당 제품코드의 상세가 없습니다.")
        return {
            "final": True,
            "type": "table",
            "title": f"제품코드 상세 ({physic_cd}) (0건)",
            "action": f"제품코드 상세 ({physic_cd})",
            "params": params_out,
            "df": pd.DataFrame(),
            "df_display": pd.DataFrame(),
            "meta": {
                "제품코드": physic_cd,
                "row_count": 0,
                "row_count_total": 0,
                "source": "제품코드마스터(Rddbc040)",
                "query_summary": query_summary,
            },
            "data": "조회 결과가 없습니다.",
        }

    df = apply_labels(df, "rddbc040")
    df_display = _build_goods_display_df(df, detail=True)

    if "보험적용일" in df_display.columns:
        log.info("[view.goods.detail] formatted 보험적용일 sample=%s", df_display["보험적용일"].head(5).tolist())
    if "등록일자" in df_display.columns:
        log.info("[view.goods.detail] formatted 등록일자 sample=%s", df_display["등록일자"].head(5).tolist())
    if "수정일자" in df_display.columns:
        log.info("[view.goods.detail] formatted 수정일자 sample=%s", df_display["수정일자"].head(5).tolist())

    return {
        "final": True,
        "type": "table",
        "title": f"제품코드 상세 ({physic_cd})",
        "action": f"제품코드 상세 ({physic_cd})",
        "params": params_out,
        "df": df,
        "df_display": df_display,
        "meta": {
            "제품코드": physic_cd,
            "row_count": int(len(df_display)),
            "row_count_total": int(df.attrs.get("row_count_total", len(df))),
            "column_count": int(len(df.columns)),
            "source": "제품코드마스터(Rddbc040)",
            "query_summary": query_summary,
        },
    }

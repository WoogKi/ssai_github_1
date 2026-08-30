#   2025-12-12 09:59
# app/sims/views/vendors.py
# -*- coding: utf-8 -*-
#VERSION = "vendors/2025-12-14-001"
# VERSION = "vendors/2025-12-18-001"
# SIMS - 거래처마스터(Rddbc030) 조회 뷰

from __future__ import annotations

import os
from typing import Dict, Any, Optional, Tuple
import logging

import io
import re

import datetime as dt

import pandas as pd
import streamlit as st

from app.services import rddbc030_service as R03
from app.services import rddbc010_service as C01
from app.services.utils import apply_labels, make_unique_columns

from app.sims.views.master_advanced_filters import (
    render_master_audit_filter,
    build_master_query_condition,
    build_master_llm_summary,
)

log = logging.getLogger("ssai")


def _panel_display_max_rows(default: int = 1000) -> int:
    """패널 렌더링 행수만 결정한다. SQL source 상한에는 사용하지 않는다."""
    try:
        raw = os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS") or str(default)
        v = int(str(raw or default).strip())
    except Exception:
        v = int(default)

    if v < 1:
        v = int(default)

    return v



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
        df.to_excel(w, index=False, sheet_name="Vendors")
    return buf.getvalue()


def _ns() -> str:
    """패널에서 부여한 폼 네임스페이스를 그대로 사용"""
    return str(st.session_state.get("__sims_widget_ns", "0"))

def _vendor_ranges() -> Dict[str, Tuple[int, int]]:
    return {
        "전체": None,
        "제약사(10001~18999)": (10001, 18999),
        "영업매입처(20000~39999)": (20000, 39999),
        "영업외 매입처(40000~49999)": (40000, 49999),
        "영업매출처(50000~89999)": (50000, 89999),
        "영업외 매출처(90000~99999)": (90000, 99999),
    }

_SCOPE_OPTIONS = {
    "전체": "",
    "제약사": "maker",
    "매입처 전체": "purchase",
    "회계매입처": "account_purchase",
    "매출처 전체": "sales",
    "회계매출처": "account_sales",
    "단가적용처": "cost_apply",
    "재고적용처": "stock_apply",
}


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


def _is_sql_like_pushdown_safe(value: Any) -> bool:
    """Keep the screen's literal contains contract for SQL LIKE metacharacters."""
    text = _clean_text(value)
    return bool(text) and not any(token in text for token in ("%", "_", "["))


def _normalize_vendor_display_series(series: pd.Series) -> pd.Series:
    """Vectorized equivalent of the legacy per-cell display normalization."""
    return (
        series.fillna("")
        .astype(str)
        .replace({"None": "", "nan": "", "<NA>": "", "NaT": ""})
        .str.strip()
    )


def _format_vendor_char8_date_series(series: pd.Series) -> pd.Series:
    source = _normalize_vendor_display_series(series)
    digits = source.str.replace(r"\D", "", regex=True)
    sentinel = digits.isin(("", "0", "00000000", "19000101", "20010101", "99999999"))
    candidate = digits.str.len().eq(8) & ~sentinel
    parsed = pd.to_datetime(digits.where(candidate), format="%Y%m%d", errors="coerce")
    result = source.mask(sentinel, "")
    result.loc[candidate] = parsed.dt.strftime("%Y-%m-%d").fillna("")
    return result


def _format_vendor_datetime_series(series: pd.Series) -> pd.Series:
    source = _normalize_vendor_display_series(series)
    parsed = pd.to_datetime(source, errors="coerce")
    result = source.copy()
    valid = parsed.notna()
    result.loc[valid] = parsed.loc[valid].dt.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _load_code_options_by_gcode(gcode: str) -> list[tuple[str, str]]:
    """
    업무코드 Rddbc010 에서 gcode 기준 상세코드 목록을 읽어
    [(표시명, 실제코드)] 형태로 반환.
    """
    try:
        fn = getattr(C01, "list_by_group", None)
        if callable(fn):
            df = _ensure_df(fn(gcode=gcode, top=2000))
        else:
            df = _ensure_df(C01.search_rows(gcode=gcode, top=2000, only_active=True))
    except Exception:
        log.exception("[view.vendors] code options load failed gcode=%s", gcode)
        return [("전체", "")]

    if df.empty:
        return [("전체", "")]

    tcol = _pick_col(df, ["Rd01_Tcode", "항목코드", "상세코드"])
    ncol = _pick_col(df, ["Rd01_Hnm", "한글명", "코드명"])
    if not tcol or not ncol:
        return [("전체", "")]

    work = df[[tcol, ncol]].copy()
    work[tcol] = _norm_series(work[tcol])
    work[ncol] = _norm_series(work[ncol])
    work = work[(work[tcol] != "") & (work[ncol] != "")]
    work = work.drop_duplicates(subset=[tcol, ncol]).sort_values([tcol], kind="stable")

    options: list[tuple[str, str]] = [("전체", "")]
    for _, row in work.iterrows():
        options.append((str(row[ncol]).strip(), str(row[tcol]).strip()))
    return options


def _vendor_group_options() -> list[tuple[str, str]]:
    # Rddbc030 schema default: Ven_Group_Gcode = 0019
    return _load_code_options_by_gcode("0019")


def _vendor_kind_options() -> list[tuple[str, str]]:
    # Rddbc030 schema default: Ven_Kind_Gcode = 0009
    return _load_code_options_by_gcode("0009")


def _search_vendors_service(**kwargs) -> pd.DataFrame:
    """
    rddbc030_service 호환 호출
    """
    fn = getattr(R03, "search_vendors_full", None)
    if callable(fn):
        return _ensure_df(fn(**kwargs))

    fn2 = getattr(R03, "search_rows", None)
    if callable(fn2):
        return _ensure_df(
            fn2(
                scope=kwargs.get("scope", ""),
                ven_cd=kwargs.get("ven_cd", ""),
                ven_nm_kw=kwargs.get("ven_nm", ""),
                owner_nm_kw=kwargs.get("owner_nm", ""),
                biz_no_kw=kwargs.get("biz_no", ""),

                keyword=kwargs.get("keyword", ""),
                sido_nm=kwargs.get("sido_nm", ""),
                gugun_nm=kwargs.get("gugun_nm", ""),
                dong_nm=kwargs.get("dong_nm", ""),
                road_nm=kwargs.get("road_nm", ""),
                road_addr_kw=kwargs.get("road_addr_kw", ""),
                cost_apply_cd=kwargs.get("cost_apply_cd", ""),
                                
                stock_apply_cd=kwargs.get("stock_apply_cd", ""),
                ven_group=kwargs.get("ven_group", ""),

                ven_kind=kwargs.get("ven_kind", ""),

                add_user_nm_kw=kwargs.get("add_user_nm", "") or kwargs.get("add_user_nm_kw", ""),
                add_date_from=kwargs.get("add_date_from", ""),
                add_date_to=kwargs.get("add_date_to", ""),
                mod_user_nm_kw=kwargs.get("mod_user_nm", "") or kwargs.get("mod_user_nm_kw", ""),
                mod_date_from=kwargs.get("mod_date_from", ""),
                mod_date_to=kwargs.get("mod_date_to", ""),

                only_active=kwargs.get("only_active", True),
                top=kwargs.get("top", 200),
            )
        )

    raise AttributeError("rddbc030_service 에 search_vendors_full / search_rows 가 없습니다.")


def _prepare_vendor_display(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_df(df_raw)
    if df.empty:
        return df

    # NLQ와 동일하게 라벨 적용 + 중복 컬럼 정리
    df = apply_labels(df, "rddbc030", table_name_in_db="Rddbc030")
    df = make_unique_columns(df)

    def _rename_first(candidates: list[str], target: str) -> None:
        if target in df.columns:
            return
        for src in candidates:
            if src in df.columns:
                df.rename(columns={src: target}, inplace=True)
                return

    # ── sample query 기준 alias 정리 ──────────────────────────────
    _rename_first(["sales_man_nm"], "영업사원명")
    _rename_first(["Rd03_Sales_Man"], "영업사원")

    _rename_first(["ven_group_nm", "ven_group_name"], "거래처그룹명")
    _rename_first(["Rd03_Ven_Group_Gcode"], "거래처그룹 대분류코드")
    _rename_first(["Rd03_Ven_Group"], "거래처그룹 상세코드")

    _rename_first(["ven_kind_nm", "ven_kind_name"], "거래처종류명")
    _rename_first(["Rd03_Ven_Kind_Gcode"], "거래처종류 대분류코드")
    _rename_first(["Rd03_Ven_Kind"], "거래처종류 상세코드")

    _rename_first(["cost_apply_nm", "unit_price_apply_nm"], "단가적용처명")
    _rename_first(["Rd03_Cost_Apply_Cd"], "단가적용처코드")

    _rename_first(["stock_apply_nm"], "재고적용처명")
    _rename_first(["Rd03_Stock_Apply_Cd"], "재고적용처코드")

    _rename_first(["delivery_di_nm"], "배송구분명")
    _rename_first(["Rd03_Delivery_Di_Gcode"], "배송구분 대분류코드")
    _rename_first(["Rd03_Delivery_Di"], "배송구분 상세코드")

    _rename_first(["unify_ven_nm"], "대표거래처명")
    _rename_first(["Rd03_Unify_Ven_Cd"], "대표거래처")

    _rename_first(["ven_rank_nm", "ven_rank_name"], "거래처등급명")
    _rename_first(["Rd03_Ven_Rank_Gcode"], "거래처등급 대분류")
    _rename_first(["Rd03_Ven_Rank"], "거래처등급")

    _rename_first(["printer_kind_nm"], "프린터종류명")
    _rename_first(["Rd03_Printer_Kind_Gcode"], "프린터종류 대분류")
    _rename_first(["Rd03_Printer_Kind"], "프린터종류")

    _rename_first(["contract_cd_nm"], "계약구분명")
    _rename_first(["Rd03_Contract_Cd_Gcode"], "계약구분 대분류")
    _rename_first(["Rd03_Contract_Cd"], "계약구분")

    _rename_first(["supply_cd_nm"], "공급구분명")
    _rename_first(["Rd03_Supply_Cd_Gcode"], "공급구분 대분류")
    _rename_first(["Rd03_Supply_Cd"], "공급구분")

    _rename_first(["unity_gu_nm"], "통합출고사용구분명")
    _rename_first(["Rd03_Unity_Gu_Gcode"], "통합출고사용구분 대분류")
    _rename_first(["Rd03_Unity_Gu"], "통합출고사용구분")

    _rename_first(["ven_tax_nm"], "세금계산서종류명")
    _rename_first(["Rd03_Ven_Tax_Gcode"], "세금계산서종류 대분류코드")
    _rename_first(["Rd03_Ven_Tax"], "세금계산서종류")

    _rename_first(["tax_type_nm"], "세금계산서발행구분명")
    _rename_first(["Rd03_Tax_Type_Gcode"], "세금계산서발행구분 대분류코드")
    _rename_first(["Rd03_Tax_Type"], "세금계산서발행구분")

    _rename_first(["add_user_nm"], "등록자명")
    _rename_first(["mod_user_nm"], "수정자명")
    _rename_first(["Rd03_Add_Cd"], "등록자코드")
    _rename_first(["Rd03_Mod_Cd"], "수정자코드")

    # 기타 자주 쓰는 표시명 정리
    _rename_first(["Rd03_Ven_PRT"], "거래처 출력명")

    _rename_first(["Rd03_RoadCd"], "도로명코드")
    _rename_first(["Rd03_DongSeq"], "도로명코드상세번호")
    _rename_first(["Rd03_BuildingNum"], "건물본번")
    _rename_first(["RD03_BUILDINGSUBNUM"], "건물부번")
    _rename_first(["Rd03_BuildingDetailNm"], "건물상세명")
    _rename_first(["Rd03_RoadArea"], "도로명주소지역코드")

    _rename_first(["road_sido_nm"], "시도명")
    _rename_first(["road_gugun_nm"], "시구군명")
    _rename_first(["road_dong_nm"], "법정읍면동명")
    _rename_first(["road_nm"], "도로명")
    _rename_first(["road_enm"], "도로명(영문)")
    _rename_first(["road_full_addr"], "도로명주소")


    _rename_first(["Rd03_Buss_Status"], "업태")
    _rename_first(["Rd03_Item"], "종목")
    _rename_first(["Rd03_Take_Nm"], "담당자")
    _rename_first(["Rd03_EMail"], "담당자 Email")
    _rename_first(["Rd03_HP"], "담당자 핸드폰번호")
    _rename_first(["Rd03_CorpReg_Num"], "법인등록번호")
    _rename_first(["Rd03_Ven_Sm"], "거래처약어명")

    # 등록자/수정자는 sample query 기준으로 "코드 + 이름" 구조
    if "등록자명" in df.columns:
        df["등록자"] = _norm_series(df["등록자명"])
    elif "등록자코드" in df.columns:
        df["등록자"] = _norm_series(df["등록자코드"])

    if "수정자명" in df.columns:
        df["수정자"] = _norm_series(df["수정자명"])
    elif "수정자코드" in df.columns:
        df["수정자"] = _norm_series(df["수정자코드"])

    # 이름 컬럼이 없을 경우 fallback
    if "단가적용처명" not in df.columns and "단가적용처코드" in df.columns:
        df["단가적용처명"] = _norm_series(df["단가적용처코드"])
    if "재고적용처명" not in df.columns and "재고적용처코드" in df.columns:
        df["재고적용처명"] = _norm_series(df["재고적용처코드"])
    if "대표거래처명" not in df.columns and "대표거래처" in df.columns:
        df["대표거래처명"] = _norm_series(df["대표거래처"])

    # sample query 순서에 맞춘 전면 컬럼
    front_cols = [c for c in [
        "거래처코드",
        "거래처명",
        "거래처 출력명",
        "대표자명",

        "우편번호",
        "우편번호순번",
        "상세주소",
        "상세주소2",
        "시도명",
        "시구군명",
        "법정읍면동명",
        "도로명",
        "건물본번",
        "건물부번",
        "건물상세명",
        "도로명주소",
        "사업자등록번호",

        "업태",
        "종목",
        "담당자",
        "전화번호",
        "팩스번호",
        "비고",
        "영업사원",
        "영업사원명",
        "거래처그룹 대분류코드",
        "거래처그룹 상세코드",
        "거래처그룹명",
        "거래처종류 대분류코드",
        "거래처종류 상세코드",
        "거래처종류명",
        "단가적용처코드",
        "단가적용처명",
        "재고적용처코드",
        "재고적용처명",
        "삭제여부",
        "등록일자",
        "등록자코드",
        "등록자",
        "수정일자",
        "수정자코드",
        "수정자",
        "거래처약어명",
        "배송구분 대분류코드",
        "배송구분 상세코드",
        "배송구분명",
        "대표거래처",
        "대표거래처명",
        "거래처등급",
        "거래처등급 대분류",
        "거래처등급명",
        "프린터종류 대분류",
        "프린터종류",
        "프린터종류명",
        "계약구분 대분류",
        "계약구분",
        "계약구분명",
        "공급구분 대분류",
        "공급구분",
        "공급구분명",
        "통합출고사용구분 대분류",
        "통합출고사용구분",
        "통합출고사용구분명",
        "담당자 Email",
        "담당자 핸드폰번호",
        "세금계산서종류 대분류코드",
        "세금계산서종류",
        "세금계산서종류명",
        "세금계산서발행구분 대분류코드",
        "세금계산서발행구분",
        "세금계산서발행구분명",
        "등록일시",
        "수정일시",
    ] if c in df.columns]

    hidden_dup_cols = {
        "등록자명",
        "수정자명",
        "cost_apply_nm",
        "stock_apply_nm",
        "delivery_di_nm",
        "unify_ven_nm",
        "ven_rank_nm",
        "printer_kind_nm",
        "contract_cd_nm",
        "supply_cd_nm",
        "unity_gu_nm",
        "ven_tax_nm",
        "tax_type_nm",
        "add_user_nm",
        "mod_user_nm",
    }

    rest_cols = [c for c in df.columns if c not in front_cols and c not in hidden_dup_cols]
    ordered_cols = front_cols + rest_cols
    out = df[ordered_cols].copy() if ordered_cols else df.copy()

    for col in out.columns:
        out[col] = _normalize_vendor_display_series(out[col])

    for col in ("등록일자", "수정일자"):
        if col in out.columns:
            out[col] = _format_vendor_char8_date_series(out[col])

    for col in ("등록일시", "수정일시"):
        if col in out.columns:
            out[col] = _format_vendor_datetime_series(out[col])

    return out

def _date_to_yyyymmdd(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    s = str(value or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _parse_yyyymmdd_or_none(value: Any):
    s = str(value or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 8:
        return None
    try:
        return dt.datetime.strptime(digits, "%Y%m%d").date()
    except Exception:
        return None


def _append_condition(parts: list[str], label: str, value: Any) -> None:
    s = _clean_text(value)
    if s and s != "전체":
        parts.append(f"{label} {s}")


def _append_date_range_condition(parts: list[str], label: str, date_from: str, date_to: str) -> None:
    a = _clean_text(date_from)
    b = _clean_text(date_to)
    if a and b:
        parts.append(f"{label} {a}~{b}" if a != b else f"{label} {a}")
    elif a:
        parts.append(f"{label} {a}~")
    elif b:
        parts.append(f"{label} ~{b}")


def _build_vendor_query_condition(params: Dict[str, Any], total: int, display_count: int) -> str:
    field_specs = [
        ("text", "거래처코드구분", "거래처코드구분"),
        ("text", "거래처코드", "거래처코드"),
        ("text", "거래처명", "거래처명"),
        ("text", "대표자명", "대표자명"),
        ("text", "사업자등록번호", "사업자등록번호"),
        ("text", "통합검색", "통합검색"),

        ("text", "시도명", "시도명"),
        ("text", "시구군명", "시구군명"),
        ("text", "법정읍면동명", "법정읍면동명"),
        ("text", "도로명", "도로명"),
        ("text", "도로명주소", "도로명주소"),

        ("text", "거래처그룹명", "거래처그룹명"),
        ("text", "거래처종류명", "거래처종류명"),
        ("text", "단가적용처명", "단가적용처명"),
        ("text", "재고적용처명", "재고적용처명"),

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

def _top_count_records(df: pd.DataFrame, col: str, top_n: int = 10) -> list[dict[str, Any]]:
    if df is None or df.empty or col not in df.columns:
        return []

    s = _norm_series(df[col])
    s = s[(s != "") & (s != "전체")]
    if s.empty:
        return []

    vc = s.value_counts(dropna=True).head(top_n)
    return [
        {"name": str(idx), "count": int(cnt)}
        for idx, cnt in vc.items()
    ]


def _year_count_records(df: pd.DataFrame, col: str, top_n: int = 10) -> list[dict[str, Any]]:
    if df is None or df.empty or col not in df.columns:
        return []

    s = _norm_series(df[col])
    if s.empty:
        return []

    # "2025-01-01", "20250101" 모두 대응
    years = (
        s.str.replace("-", "", regex=False)
         .str.extract(r"^([0-9]{4})", expand=False)
         .fillna("")
         .astype(str)
         .str.strip()
    )
    years = years[years != ""]
    if years.empty:
        return []

    vc = years.value_counts(dropna=True).head(top_n)
    return [
        {"year": str(idx), "count": int(cnt)}
        for idx, cnt in vc.items()
    ]


def _fmt_top_records(items: list[dict[str, Any]], name_key: str = "name") -> str:
    if not items:
        return "없음"
    return ", ".join(
        f"{str(x.get(name_key, '')).strip()} {int(x.get('count', 0)):,}건"
        for x in items
        if str(x.get(name_key, "")).strip()
    ) or "없음"


def _build_vendor_master_llm_summary(
    df_all_display: pd.DataFrame,
    *,
    query_condition: str,
    total: int,
    display_count: int,
) -> dict[str, Any]:
    """
    거래처 마스터 LLM 분석용 전체 집계 요약.
    화면 표시건수와 무관하게 df_all_display 전체 기준으로 집계한다.
    """
    return build_master_llm_summary(
        df_all_display,
        master_name="거래처마스터",
        query_condition=query_condition,
        total=total,
        display_count=display_count,
        count_specs=[
            ("거래처그룹별 상위", "거래처그룹명", "group_top"),
            ("거래처종류별 상위", "거래처종류명", "kind_top"),
            ("영업사원별 상위", "영업사원명", "sales_man_top"),
            ("시도별 상위", "시도명", "sido_top"),
            ("시구군별 상위", "시구군명", "gugun_top"),
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

#
def _post_filter_vendor_refs(
    df_raw: pd.DataFrame,
    *,
    cost_apply_nm_kw: str = "",
    stock_apply_nm_kw: str = "",
) -> pd.DataFrame:
    df = _ensure_df(df_raw).copy()
    if df.empty:
        return df

    cost_apply_nm_kw = _clean_text(cost_apply_nm_kw)
    stock_apply_nm_kw = _clean_text(stock_apply_nm_kw)

    def _contains(series: pd.Series, kw: str) -> pd.Series:
        if not kw:
            return pd.Series([True] * len(series), index=series.index)
        return (
            series.fillna("")
            .astype(str)
            .str.strip()
            .str.contains(kw, case=False, na=False)
        )

    if cost_apply_nm_kw:
        col = _pick_col(df, ["cost_apply_nm", "단가적용처명"])
        if col:
            df = df[_contains(df[col], cost_apply_nm_kw)]

    if stock_apply_nm_kw:
        col = _pick_col(df, ["stock_apply_nm", "재고적용처명"])
        if col:
            df = df[_contains(df[col], stock_apply_nm_kw)]

    return df.reset_index(drop=True)

# ─────────────────────────
# 거래처 기본 목록
# ─────────────────────────
def render_vendor_list() -> Dict[str, Any]:
    panel_display_max_rows = _panel_display_max_rows()
    st.subheader("🏢 거래처 목록")
    st.caption("거래처그룹명/거래처종류명은 업무코드 기준 선택형 필터를 사용합니다.")

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_vendor_list_{ns}"

    group_options = _vendor_group_options()
    kind_options = _vendor_kind_options()

    group_labels = [label for label, _ in group_options]
    kind_labels = [label for label, _ in kind_options]

    group_code_by_label = {label: code for label, code in group_options}
    kind_code_by_label = {label: code for label, code in kind_options}

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):        
        c1, c2, c3 = st.columns(3)
        with c1:
            scope_label = st.selectbox(
                "거래처코드구분",
                options=list(_SCOPE_OPTIONS.keys()),
                index=0,
                key=f"__vendors_scope__{ns}",
            )
        with c2:
            ven_cd = st.text_input("거래처코드", value="", key=f"__vendors_ven_cd__{ns}")
        with c3:
            ven_nm = st.text_input("거래처명", value="", key=f"__vendors_ven_nm__{ns}")

        c4, c5, c6 = st.columns(3)
        with c4:
            owner_nm = st.text_input("대표자명", value="", key=f"__vendors_owner_nm__{ns}")
        with c5:
            biz_no = st.text_input("사업자등록번호", value="", key=f"__vendors_biz_no__{ns}")
        with c6:
            keyword = st.text_input("통합검색", value="", key=f"__vendors_keyword__{ns}")

        c7, c8 = st.columns(2)
        with c7:
            selected_group_label = st.selectbox(
                "거래처그룹명",
                options=group_labels,
                index=0,
                key=f"__vendors_group_name__{ns}",
            )
        with c8:
            selected_kind_label = st.selectbox(
                "거래처종류명",
                options=kind_labels,
                index=0,
                key=f"__vendors_kind_name__{ns}",
            )

        c8a, c8b, c8c, c8d = st.columns(4)
        with c8a:
            sido_nm = st.text_input("시도명", value="", key=f"__vendors_sido_nm__{ns}", placeholder="예: 서울")
        with c8b:
            gugun_nm = st.text_input("시구군명", value="", key=f"__vendors_gugun_nm__{ns}", placeholder="예: 강남")
        with c8c:
            dong_nm = st.text_input("법정읍면동명", value="", key=f"__vendors_dong_nm__{ns}", placeholder="예: 역삼")
        with c8d:
            road_nm = st.text_input("도로명", value="", key=f"__vendors_road_nm__{ns}", placeholder="예: 테헤란로")

        c8e, _ = st.columns([2, 2])
        with c8e:
            road_addr_kw = st.text_input("도로명주소", value="", key=f"__vendors_road_addr_kw__{ns}", placeholder="예: 강남대로")

        c9, c10, c11, c12 = st.columns(4)
        with c9:
            cost_apply_nm = st.text_input("단가적용처명", value="", key=f"__vendors_cost_apply_nm__{ns}")
        with c10:
            stock_apply_nm = st.text_input("재고적용처명", value="", key=f"__vendors_stock_apply_nm__{ns}")
        with c11:
            only_active = st.checkbox("사용중만", value=True, key=f"__vendors_only_active__{ns}")
        with c12:
            st.caption(f"화면 표시: 최대 {panel_display_max_rows:,}건")

        
#   고급조회
        audit_filter = render_master_audit_filter(
            prefix="vendors",
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
            "title": "거래처 목록",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    scope = _SCOPE_OPTIONS.get(scope_label, "")
    ven_group = group_code_by_label.get(selected_group_label, "")
    ven_kind = kind_code_by_label.get(selected_kind_label, "")

    params = {
        "거래처코드구분": scope_label,
        "scope": scope,
        "거래처코드": _clean_text(ven_cd),
        "거래처명": _clean_text(ven_nm),
        "대표자명": _clean_text(owner_nm),
        "사업자등록번호": _clean_text(biz_no),

        "통합검색": _clean_text(keyword),
        "시도명": _clean_text(sido_nm),
        "시구군명": _clean_text(gugun_nm),
        "법정읍면동명": _clean_text(dong_nm),
        "도로명": _clean_text(road_nm),
        "도로명주소": _clean_text(road_addr_kw),
        "거래처그룹명": selected_group_label,

        "거래처그룹코드": ven_group,
        "거래처종류명": selected_kind_label,
        "거래처종류코드": ven_kind,
        "단가적용처명": _clean_text(cost_apply_nm),
        "재고적용처명": _clean_text(stock_apply_nm),
        "사용중만": bool(only_active),

        "등록자": _clean_text(add_user_nm),
        "수정자": _clean_text(mod_user_nm),

        "등록일자From": add_date_from,
        "등록일자To": add_date_to,
        "수정일자From": mod_date_from,
        "수정일자To": mod_date_to,

        "화면표시상한": int(panel_display_max_rows),
    }

    try:
        display_top = int(panel_display_max_rows)
        fetch_top = 0

        df_raw = _search_vendors_service(
            top=fetch_top,
            only_active=bool(only_active),
            scope=scope,
            ven_cd=_clean_text(ven_cd),
            ven_nm=_clean_text(ven_nm),
            owner_nm=_clean_text(owner_nm),

            biz_no=_clean_text(biz_no),
            keyword=_clean_text(keyword),
            sido_nm=_clean_text(sido_nm),
            gugun_nm=_clean_text(gugun_nm),
            dong_nm=_clean_text(dong_nm),
            road_nm=_clean_text(road_nm),
            road_addr_kw=_clean_text(road_addr_kw),
            ven_group=ven_group,
            ven_kind=ven_kind,
            cost_apply_nm=_clean_text(cost_apply_nm) if _is_sql_like_pushdown_safe(cost_apply_nm) else "",
            stock_apply_nm=_clean_text(stock_apply_nm) if _is_sql_like_pushdown_safe(stock_apply_nm) else "",

            add_user_nm=_clean_text(add_user_nm),
            mod_user_nm=_clean_text(mod_user_nm),

            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,

        )

        # 단가적용처명 / 재고적용처명은 화면단 후필터
        df_raw = _post_filter_vendor_refs(
            df_raw,
            cost_apply_nm_kw=_clean_text(cost_apply_nm),
            stock_apply_nm_kw=_clean_text(stock_apply_nm),
        )

        total = int(len(df_raw))

        df_display_all = _prepare_vendor_display(df_raw)
        df_display = df_display_all.head(display_top).copy()

        display_count = int(len(df_display))

        query_condition = _build_vendor_query_condition(params, total, display_count)

        vendor_master_summary = _build_vendor_master_llm_summary(
            df_display_all,
            query_condition=query_condition,
            total=total,
            display_count=display_count,
        )
        llm_summary_md = str(vendor_master_summary.get("llm_summary_md") or "")
        summary_parts = []
        if str(llm_summary_md or "").strip():
            summary_parts.append(str(llm_summary_md).strip())
        summary_md = "\n\n".join(summary_parts).strip()

        meta = {
            "총건수": total,
            "row_count": total,
            "row_count_total": total,
            "row_count_loaded": total,
            "download_row_count": total,
            "display_row_count": display_count,
            "fetch_limit": fetch_top,
            "scope": scope,
            "scope_label": scope_label,
            "거래처그룹코드": ven_group,
            "거래처종류코드": ven_kind,
            "단가적용처명": _clean_text(cost_apply_nm),
            "재고적용처명": _clean_text(stock_apply_nm),
            "등록자": _clean_text(add_user_nm),
            "등록일자From": _date_to_yyyymmdd(add_date_from),
            "등록일자To": _date_to_yyyymmdd(add_date_to),
            "수정자": _clean_text(mod_user_nm),
            "수정일자From": _date_to_yyyymmdd(mod_date_from),
            "수정일자To": _date_to_yyyymmdd(mod_date_to),
            "시도명": _clean_text(sido_nm),
            "시구군명": _clean_text(gugun_nm),
            "법정읍면동명": _clean_text(dong_nm),
            "도로명": _clean_text(road_nm),
            "도로명주소": _clean_text(road_addr_kw),
            "query_summary": query_condition,
            "condition": query_condition,

            "summary_md": summary_md,            
            "master_nlq": True,
            "domain": "vendors",
            "source": "거래처마스터(Rddbc030)",

            "analysis_type": "vendor_master",
            "llm_summary_kind": "vendor_master_summary",
            "llm_summary_md": llm_summary_md,
            "vendor_master_summary": vendor_master_summary,
            "analysis_row_count": total,
            "row_count_total_for_analysis": total,
            "summary_basis": "전체 조회결과 기준",
            "field_notes": (
                "거래처 마스터 분석은 전체 조회결과 기준 집계요약을 우선 근거로 답합니다. "
                "화면 표시는 일부 행으로 제한될 수 있습니다."
            ),

        }

        return {
            "final": True,
            "type": "table",
            "title": "거래처 목록",
            "action": "거래처 목록",
            "params": params,
            "df": df_raw,
            "df_display": df_display,
            "meta": meta,
        }

    except Exception as e:
        log.exception("[view.vendors] search failed")
        return {
            "final": False,
            "type": "text",
            "title": "거래처 목록 오류",
            "data": str(e),
        }
    
def render_vendor_detail() -> Dict[str, Any]:
    """거래처 상세: 거래처코드 1건 조회"""
    ns = _ns()
    form_key = f"__vendors_detail_form__{ns}"

    st.markdown("### 🔎 거래처 상세")
    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):        
        ven_cd = st.text_input("거래처코드", key=f"__vendors_detail_cd__{ns}", placeholder="예: 00077")
        submitted_btn = st.form_submit_button("조회", type="primary")

    if not submitted_btn:
        return {"final": False}

    ven_cd = (ven_cd or "").strip()
    if not ven_cd:
        st.warning("거래처코드를 입력하세요.")
        return {"final": False}

    try:
        df_raw = pd.DataFrame()

        fn_detail = getattr(R03, "get_vendor_detail_full", None)
        if callable(fn_detail):
            try:
                df_raw = _ensure_df(fn_detail(ven_cd=ven_cd))
            except TypeError:
                df_raw = _ensure_df(fn_detail(ven_cd))

        if df_raw.empty:
            fn_one = getattr(R03, "get_vendor_full", None)
            if callable(fn_one):
                try:
                    df_raw = _ensure_df(fn_one(ven_cd=ven_cd, only_active=False))
                except TypeError:
                    df_raw = _ensure_df(fn_one(ven_cd=ven_cd))

        if df_raw.empty:
            fn_search = getattr(R03, "search_vendors_full", None)
            if callable(fn_search):
                df_raw = _ensure_df(fn_search(ven_cd=ven_cd, top=1, only_active=False))

    except Exception as e:
        st.error(f"조회 오류: {e}")
        log.exception("[view.vendors.detail] failed")
        return {"final": False}

    if df_raw is None or len(df_raw) == 0:
        st.info("조회 결과가 없습니다.")
        return {
            "final": True,
            "type": "text",
            "title": "거래처 상세",
            "action": "거래처 상세",
            "params": {"거래처코드": ven_cd},
            "data": f"거래처코드 {ven_cd} 결과 없음",
            "message": f"거래처코드 {ven_cd} 결과 없음",
            "meta": {"거래처코드": ven_cd},
        }

    # 목록과 동일한 표시 로직 사용
    df_display = _prepare_vendor_display(df_raw)

    # 상세는 1건 기준
    if len(df_display) > 1:
        df_display = df_display.head(1).copy()
    if len(df_raw) > 1:
        df_raw = df_raw.head(1).copy()

    return {
        "final": True,
        "type": "table",
        "title": f"거래처 상세 ({ven_cd})",
        "action": "거래처 상세",
        "params": {"거래처코드": ven_cd},
        "df": df_raw,
        "df_display": df_display,
        "meta": {
            "거래처코드": ven_cd,
            "총건수": int(len(df_raw)),
        },
    }

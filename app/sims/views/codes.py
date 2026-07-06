# app/sims/views/codes.py
# 뷰는 '표시 + payload 반환'만 담당합니다. (컨텍스트 푸시는 패널/브리지에서 1회만)

from __future__ import annotations

from typing import Dict, Any, Optional, Iterable, List
import io
import logging
import os

import pandas as pd
import streamlit as st

from app.services import rddbc010_service as C
from app.services.utils import apply_labels, make_unique_columns
from app.sims.views.master_advanced_filters import (
    render_master_audit_filter,
    build_master_query_condition,
    build_master_llm_summary,
)


log = logging.getLogger("ssai")

# 고정 규칙: Rddbc010 에서 Gcode='9999' 는 코드종류 사전
GCODE_KIND = "9999"


def _xlsx_bytes(df: pd.DataFrame) -> Optional[bytes]:
    """xlsxwriter 또는 openpyxl이 있으면 XLSX 바이트를 반환, 없으면 None."""
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
    with pd.ExcelWriter(buf, engine=engine) as writer:
        df.to_excel(writer, index=False, sheet_name="SIMS")
    return buf.getvalue()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _master_max_rows(default: int = 30000) -> int:
    """
    마스터 조회 공통 상한.
    별도 env를 만들지 않고 기존 SIMS_PANEL_DISPLAY_MAX_ROWS를 사용한다.
    없으면 SIMS_CHAT_DISPLAY_MAX_ROWS, 그것도 없으면 default.
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


def _normalize_top(value: Any, default: int = 200, max_value: int | None = None) -> int:
    try:
        v = int(value)
    except Exception:
        v = default
    if v < 1:
        v = default
    if max_value is None:
        max_value = _master_max_rows()
    return min(v, int(max_value))


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _pick_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _contains_mask(series: pd.Series, keyword: str) -> pd.Series:
    kw = _clean_text(keyword)
    if not kw:
        return pd.Series([True] * len(series), index=series.index)
    return series.fillna("").astype(str).str.contains(kw, case=False, na=False)


def _apply_manual_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    labels_map 이 아직 덜 정리된 환경에서도 화면 표시는 최대한 한글 컬럼명으로 보이도록 보강.
    """
    manual = {}

    # service alias
    if "kind_name" in df.columns and "코드종류" not in df.columns:
        manual["kind_name"] = "코드종류"
    if "add_user_nm" in df.columns and "등록자" not in df.columns:
        manual["add_user_nm"] = "등록자"
    if "mod_user_nm" in df.columns and "수정자" not in df.columns:
        manual["mod_user_nm"] = "수정자"

    # raw columns
    if "Rd01_Gcode" in df.columns and "그룹코드" not in df.columns:
        manual["Rd01_Gcode"] = "그룹코드"
    if "Rd01_Tcode" in df.columns and "항목코드" not in df.columns:
        manual["Rd01_Tcode"] = "항목코드"
    if "Rd01_Hnm" in df.columns and "한글명" not in df.columns:
        manual["Rd01_Hnm"] = "한글명"
    if "Rd01_Enm" in df.columns and "영문명" not in df.columns:
        manual["Rd01_Enm"] = "영문명"
    if "Rd01_Snm" in df.columns and "약칭" not in df.columns:
        manual["Rd01_Snm"] = "약칭"
    if "Rd01_Other1" in df.columns and "기타1" not in df.columns:
        manual["Rd01_Other1"] = "기타1"
    if "Rd01_Other2" in df.columns and "기타2" not in df.columns:
        manual["Rd01_Other2"] = "기타2"
    if "Rd01_Other3" in df.columns and "기타3" not in df.columns:
        manual["Rd01_Other3"] = "기타3"
    if "Rd01_Del_Flag" in df.columns and "삭제여부" not in df.columns:
        manual["Rd01_Del_Flag"] = "삭제여부"
    if "Rd01_Add_Date" in df.columns and "등록일자" not in df.columns:
        manual["Rd01_Add_Date"] = "등록일자"
    if "Rd01_Mod_Date" in df.columns and "수정일자" not in df.columns:
        manual["Rd01_Mod_Date"] = "수정일자"
    if "Rd01_Add_Cd" in df.columns and "등록자코드" not in df.columns:
        manual["Rd01_Add_Cd"] = "등록자코드"
    if "Rd01_Mod_Cd" in df.columns and "수정자코드" not in df.columns:
        manual["Rd01_Mod_Cd"] = "수정자코드"

    if manual:
        df = df.rename(columns=manual)

    # 요청상 '항목코드'와 '상세코드'를 둘 다 쓰고 싶어 하므로 복제 컬럼 제공
    if "항목코드" in df.columns and "상세코드" not in df.columns:
        df["상세코드"] = df["항목코드"]
    elif "상세코드" in df.columns and "항목코드" not in df.columns:
        df["항목코드"] = df["상세코드"]

    return make_unique_columns(df)

def _norm_series(sr: pd.Series) -> pd.Series:
    return (
        sr.fillna("")
        .astype(str)
        .replace({"None": "", "nan": "", "<NA>": ""})
        .str.strip()
    )


def _stash_raw_code_cols(df: pd.DataFrame) -> pd.DataFrame:
    """
    apply_labels / make_unique_columns 전에 원본 컬럼을 안전한 내부 컬럼으로 백업.
    화면에서는 이 내부 컬럼을 최우선 사용한다.
    """
    out = _ensure_df(df).copy()
    if out.empty:
        return out

    mapping = {
        "__raw_gcode": ["Rd01_Gcode", "그룹코드"],
        "__raw_tcode": ["Rd01_Tcode", "항목코드", "상세코드"],
        "__raw_kind_name": ["kind_name", "코드종류", "코드종류명"],
        "__raw_hnm": ["Rd01_Hnm", "한글명", "한글 이름", "코드명"],
        "__raw_enm": ["Rd01_Enm", "영문명"],
        "__raw_snm": ["Rd01_Snm", "약칭", "짧은이름"],
        "__raw_add_user_nm": ["add_user_nm", "등록자", "등록자명"],
        "__raw_add_date": ["Rd01_Add_Date", "등록일자"],
        "__raw_mod_user_nm": ["mod_user_nm", "수정자", "수정자명"],
        "__raw_mod_date": ["Rd01_Mod_Date", "수정일자"],
        "__raw_del_flag": ["Rd01_Del_Flag", "삭제여부"],
    }

    for raw_col, candidates in mapping.items():
        if raw_col in out.columns:
            continue

        s = pd.Series([""] * len(out), index=out.index, dtype=object)
        for col in candidates:
            if col not in out.columns:
                continue
            src = _norm_series(out[col])
            s = s.where(s != "", src)

        out[raw_col] = s

    return out

def _prepare_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_df(df_raw)

    # 핵심: 원본 컬럼을 먼저 백업
    df = _stash_raw_code_cols(df)

    try:
        df = apply_labels(df.copy(), "rddbc010", table_name_in_db="Rddbc010")
    except Exception:
        pass

    try:
        df = make_unique_columns(df)
    except Exception:
        pass

    df = _apply_manual_labels(df)
    return df


def _post_filter_df(
    df: pd.DataFrame,
    *,
    gcode: str = "",
    tcode: str = "",
    keyword: str = "",
    kind_name_kw: str = "",
    hnm_kw: str = "",
    enm_kw: str = "",
    snm_kw: str = "",
    other_kw: str = "",
    only_active: bool = True,
) -> pd.DataFrame:
    """
    서비스가 구형 API(search_name/list_by_group/get_one_ex)인 경우를 대비한 화면단 후처리 필터.
    """
    out = _ensure_df(df)
    if out.empty:
        return out

    gcode_col = _pick_col(out, ["그룹코드", "Rd01_Gcode"])
    tcode_col = _pick_col(out, ["항목코드", "상세코드", "Rd01_Tcode"])
    kind_col = _pick_col(out, ["코드종류", "kind_name"])
    hnm_col = _pick_col(out, ["한글명", "코드명", "Rd01_Hnm"])
    enm_col = _pick_col(out, ["영문명", "Rd01_Enm"])
    snm_col = _pick_col(out, ["약칭", "짧은이름", "Rd01_Snm"])
    other_cols = [
        c for c in ["기타1", "기타2", "기타3", "Rd01_Other1", "Rd01_Other2", "Rd01_Other3"]
        if c in out.columns
    ]
    del_col = _pick_col(out, ["삭제여부", "Rd01_Del_Flag"])

    if only_active and del_col:
        out = out[~out[del_col].fillna("").astype(str).str.strip().isin(["1", "Y", "y"])]

    if gcode and gcode_col:
        out = out[out[gcode_col].fillna("").astype(str).str.strip() == gcode]

    if tcode and tcode_col:
        out = out[out[tcode_col].fillna("").astype(str).str.strip() == tcode]

    if kind_name_kw and kind_col:
        out = out[_contains_mask(out[kind_col], kind_name_kw)]

    if keyword:
        cols = [c for c in [kind_col, hnm_col, enm_col, snm_col] if c]
        cols.extend(other_cols)
        if cols:
            mask = pd.Series(False, index=out.index)
            for col in cols:
                mask = mask | _contains_mask(out[col], keyword)
            out = out[mask]

    if hnm_kw and hnm_col:
        out = out[_contains_mask(out[hnm_col], hnm_kw)]

    if enm_kw and enm_col:
        out = out[_contains_mask(out[enm_col], enm_kw)]

    if snm_kw and snm_col:
        out = out[_contains_mask(out[snm_col], snm_kw)]

    if other_kw and other_cols:
        mask = pd.Series(False, index=out.index)
        for col in other_cols:
            mask = mask | _contains_mask(out[col], other_kw)
        out = out[mask]

    sort_cols = [c for c in [gcode_col, tcode_col] if c]
    if sort_cols:
        try:
            out = out.sort_values(sort_cols, kind="stable")
        except Exception:
            pass

    return out.reset_index(drop=True)


def _service_search(
    *,
    gcode: str = "",
    tcode: str = "",
    keyword: str = "",
    kind_name_kw: str = "",
    hnm_kw: str = "",
    enm_kw: str = "",
    snm_kw: str = "",
    other_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    only_active: bool = True,
    top: int = 200,
) -> pd.DataFrame:
        
    """
    서비스 호환 계층
    1) 신버전: search_rows(...)
    2) 구버전: search_name / list_by_group / get_one_ex
    """
    top = _normalize_top(top, default=200)

    search_rows = getattr(C, "search_rows", None)
    if callable(search_rows):
        try:
            df = _ensure_df(
                search_rows(
                    gcode=gcode,
                    tcode=tcode,
                    keyword=keyword,
                    kind_name_kw=kind_name_kw,
                    hnm_kw=hnm_kw,
                    enm_kw=enm_kw,
                    snm_kw=snm_kw,
                    other_kw=other_kw,
                    add_user_nm_kw=add_user_nm_kw,
                    add_date_from=add_date_from,
                    add_date_to=add_date_to,
                    mod_user_nm_kw=mod_user_nm_kw,
                    mod_date_from=mod_date_from,
                    mod_date_to=mod_date_to,
                    only_active=only_active,
                    top=top,
                )                
            )
            return _prepare_df(df)
        except TypeError:
            # 구버전 search_rows 시그니처 보정
            try:
                df = _ensure_df(
                    search_rows(
                        gcode=gcode,
                        tcode=tcode,
                        keyword=keyword,
                        hnm_kw=hnm_kw,
                        enm_kw=enm_kw,
                        snm_kw=snm_kw,
                        other_kw=other_kw,
                        only_active=only_active,
                        top=top,
                    )
                )
                df = _prepare_df(df)
                return _post_filter_df(
                    df,
                    gcode=gcode,
                    tcode=tcode,
                    keyword=keyword,
                    kind_name_kw=kind_name_kw,
                    hnm_kw=hnm_kw,
                    enm_kw=enm_kw,
                    snm_kw=snm_kw,
                    other_kw=other_kw,
                    only_active=only_active,
                )
            except Exception:
                pass

    q = _clean_text(keyword or hnm_kw or enm_kw or snm_kw or other_kw or kind_name_kw)

    if gcode and tcode and callable(getattr(C, "get_one_ex", None)):
        df = _ensure_df(C.get_one_ex(gcode, tcode))
    elif q and callable(getattr(C, "search_name", None)):
        df = _ensure_df(C.search_name(q, gcode=(gcode or None), top=top))
    elif q and callable(getattr(C, "search_by_name", None)):
        df = _ensure_df(C.search_by_name(q, top=top))
    elif gcode and callable(getattr(C, "list_by_group", None)):
        df = _ensure_df(C.list_by_group(gcode, top=top))
    elif callable(getattr(C, "search_name", None)):
        df = _ensure_df(C.search_name("", gcode=(gcode or None), top=top))
    else:
        raise AttributeError(
            "rddbc010_service 에 조회 함수(search_rows/search_name/list_by_group/get_one_ex)가 없습니다."
        )

    df = _prepare_df(df)
    return _post_filter_df(
        df,
        gcode=gcode,
        tcode=tcode,
        keyword=keyword,
        kind_name_kw=kind_name_kw,
        hnm_kw=hnm_kw,
        enm_kw=enm_kw,
        snm_kw=snm_kw,
        other_kw=other_kw,
        only_active=only_active,
    )


def _service_group_kinds(top: int = 500, only_active: bool = True) -> pd.DataFrame:
    """
    코드종류 사전(Gcode='9999') 조회
    """
    top = _normalize_top(top, default=500)

    fn = getattr(C, "list_group_kinds", None)
    if callable(fn):
        try:
            df = _ensure_df(fn(top=top, only_active=only_active))
        except TypeError:
            df = _ensure_df(fn(top=top))
    elif callable(getattr(C, "list_by_group", None)):
        df = _ensure_df(C.list_by_group(GCODE_KIND, top=top))
    else:
        df = _service_search(gcode=GCODE_KIND, only_active=only_active, top=top)

    df = _prepare_df(df)
    return _post_filter_df(df, gcode=GCODE_KIND, only_active=only_active)


def _service_find_group_codes_by_kind_name(kind_name: str, top: int = 50, only_active: bool = True) -> pd.DataFrame:
    """
    코드종류명(한글) -> 그룹코드 후보 조회
    """
    kind_name = _clean_text(kind_name)
    if not kind_name:
        return pd.DataFrame()

    fn = getattr(C, "find_group_codes_by_kind_name", None)
    if callable(fn):
        try:
            df = _ensure_df(fn(kind_name=kind_name, top=top, only_active=only_active))
        except TypeError:
            df = _ensure_df(fn(kind_name=kind_name, top=top))
        df = _prepare_df(df)
    else:
        # fallback: 9999 사전 직접 조회 후 화면단 필터
        df = _service_group_kinds(top=max(top, 500), only_active=only_active)

    # 사전 조회 결과에서 kind name 필터
    kind_col = _pick_col(df, ["코드종류", "한글명", "Dict_Hnm", "Rd01_Hnm"])
    gcode_col = _pick_col(df, ["그룹코드", "Rd01_Gcode", "Dict_Tcode"])

    if df.empty or not kind_col or not gcode_col:
        return pd.DataFrame()

    out = df[_contains_mask(df[kind_col], kind_name)].copy()

    # 9999 사전 조회 결과는 항목코드가 실제 그룹코드이므로 보정
    if "그룹코드" not in out.columns:
        if "항목코드" in out.columns:
            out["그룹코드"] = out["항목코드"]
        elif "Rd01_Tcode" in out.columns:
            out["그룹코드"] = out["Rd01_Tcode"]
        elif gcode_col in out.columns:
            out["그룹코드"] = out[gcode_col]

    if "코드종류" not in out.columns:
        if "한글명" in out.columns:
            out["코드종류"] = out["한글명"]
        elif kind_col in out.columns:
            out["코드종류"] = out[kind_col]

    # exact match 우선
    exact = out[out["코드종류"].fillna("").astype(str).str.strip() == kind_name]
    if len(exact) > 0:
        out = exact

    if "그룹코드" in out.columns:
        out = out.sort_values(["그룹코드"], kind="stable")

    return out.reset_index(drop=True)


def _download_buttons(df: pd.DataFrame, *, prefix: str, ns: str) -> None:
    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "CSV 다운로드",
        data=csv_bytes,
        file_name=f"{prefix}.csv",
        mime="text/csv",
        key=f"__{prefix}_csv__{ns}",
    )

    xlsx_bytes = _xlsx_bytes(df)
    if xlsx_bytes is not None:
        st.download_button(
            "엑셀 다운로드",
            data=xlsx_bytes,
            file_name=f"{prefix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"__{prefix}_xlsx__{ns}",
        )

def _coalesce_display_col(
    df: pd.DataFrame,
    target: str,
    candidates: list[str],
    default: str = "",
) -> pd.DataFrame:
    """
    후보 컬럼을 순서대로 보면서 값이 있는 것을 target 으로 통합.
    핵심:
    - 기존 target 값은 '우선값'이 아니라 후보 중 하나로만 취급한다.
    - 따라서 target 에 코드값이 먼저 들어 있어도,
      앞쪽 후보(__raw_add_user_nm 등)에 이름값이 있으면 이름으로 덮인다.
    """
    out = _ensure_df(df).copy()
    if out.empty:
        if target not in out.columns:
            out[target] = []
        return out

    def _norm(v):
        if pd.isna(v):
            return ""
        text = str(v).strip()
        if text.lower() in {"none", "nan", "<na>"}:
            return ""
        return text

    # 기존 target 값을 먼저 유지하지 말고, 빈 시리즈에서 시작
    s = pd.Series([default] * len(out), index=out.index, dtype=object).map(_norm)

    # 후보를 순서대로 반영
    for col in candidates:
        if col not in out.columns:
            continue
        src = out[col].map(_norm)
        mask = (s == "") & (src != "")
        s.loc[mask] = src.loc[mask]

    # 그래도 비어 있으면 마지막으로 기존 target 컬럼 사용
    if target in out.columns:
        src = out[target].map(_norm)
        mask = (s == "") & (src != "")
        s.loc[mask] = src.loc[mask]

    out[target] = s.fillna(default)
    return out

def _merge_group_results(group_codes: List[str], *, only_active: bool, top: int) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    seen = set()

    for gcode in group_codes:
        g = _clean_text(gcode)
        if not g or g in seen:
            continue
        seen.add(g)
        try:
            frames.append(_service_search(gcode=g, only_active=only_active, top=top))
        except Exception:
            log.exception("[view.codes] group result fetch failed gcode=%s", g)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates()

    sort_cols = [c for c in ["그룹코드", "항목코드", "상세코드"] if c in out.columns]
    if sort_cols:
        try:
            out = out.sort_values(sort_cols, kind="stable")
        except Exception:
            pass

    return out.head(top).reset_index(drop=True)


def _build_group_result_view(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df).copy()

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
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
                "NaT": None,
            })
        )
        dt = pd.to_datetime(s, errors="coerce")
        return dt.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    out = _coalesce_display_col(out, "그룹코드", ["__raw_gcode", "그룹코드", "Rd01_Gcode"])
    out = _coalesce_display_col(out, "코드종류", ["__raw_kind_name", "코드종류", "kind_name", "코드종류명"])
    out = _coalesce_display_col(out, "항목코드", ["__raw_tcode", "항목코드", "상세코드", "Rd01_Tcode"])
    out = _coalesce_display_col(out, "한글명", ["__raw_hnm", "Rd01_Hnm", "한글명", "한글 이름", "코드명"])
    out = _coalesce_display_col(out, "기타1", ["기타1", "Rd01_Other1"])
    out = _coalesce_display_col(out, "기타2", ["기타2", "Rd01_Other2"])
    out = _coalesce_display_col(out, "기타3", ["기타3", "Rd01_Other3"])
    add_code_col = _pick_col(out, ["Rd01_Add_Cd", "등록자코드", "등록자 코드"])
    mod_code_col = _pick_col(out, ["Rd01_Mod_Cd", "수정자코드", "수정자 코드"])

    out = _coalesce_display_col(out, "등록자", ["__raw_add_user_nm", "등록자", "등록자명", "add_user_nm"])
    out = _coalesce_display_col(out, "등록일자", ["__raw_add_date", "등록일자", "Rd01_Add_Date"])

    out = _coalesce_display_col(out, "수정자", ["__raw_mod_user_nm", "수정자", "수정자명", "mod_user_nm"])
    out = _coalesce_display_col(out, "수정일자", ["__raw_mod_date", "수정일자", "Rd01_Mod_Date"])

    if "등록자" in out.columns and add_code_col:
        reg_name = _norm_series(out["등록자"])
        reg_code = _norm_series(out[add_code_col])
        mask = (reg_name == "") & (reg_code != "")
        out.loc[mask, "등록자"] = reg_code.loc[mask]

    if "수정자" in out.columns and mod_code_col:
        mod_name = _norm_series(out["수정자"])
        mod_code = _norm_series(out[mod_code_col])
        mask = (mod_name == "") & (mod_code != "")
        out.loc[mask, "수정자"] = mod_code.loc[mask]
        
    out = _coalesce_display_col(out, "삭제여부", ["__raw_del_flag", "삭제여부", "Rd01_Del_Flag"])

    if {"그룹코드", "한글명"}.issubset(out.columns):
        g = _norm_series(out["그룹코드"])
        h = _norm_series(out["한글명"])
        k = _norm_series(out["코드종류"]) if "코드종류" in out.columns else pd.Series([""] * len(out), index=out.index)
        t = _norm_series(out["항목코드"]) if "항목코드" in out.columns else pd.Series([""] * len(out), index=out.index)

        mask_9999_blank = (g == "9999") & (h == "")
        out.loc[mask_9999_blank, "한글명"] = k.loc[mask_9999_blank]

        h2 = _norm_series(out["한글명"])
        mask_9999_blank2 = (g == "9999") & (h2 == "")
        out.loc[mask_9999_blank2, "한글명"] = t.loc[mask_9999_blank2]

    for col in [
        "그룹코드",
        "코드종류",
        "항목코드",
        "한글명",
        "기타1",
        "기타2",
        "기타3",
        "등록자",
        "수정자",
        "삭제여부",
    ]:
        if col in out.columns:
            out[col] = _norm_series(out[col])

    if "등록일자" in out.columns:
        out["등록일자"] = _fmt_char8_date(out["등록일자"])
    if "수정일자" in out.columns:
        out["수정일자"] = _fmt_char8_date(out["수정일자"])

    preferred = [
        "그룹코드",
        "코드종류",
        "항목코드",
        "한글명",
        "기타1",
        "기타2",
        "기타3",
        "등록자",
        "등록일자",
        "수정자",
        "수정일자",
        "삭제여부",
    ]

    for col in preferred:
        if col not in out.columns:
            out[col] = ""

    view_df = out[preferred].copy()

    for col in view_df.columns:
        if col not in {"등록일자", "수정일자"}:
            view_df[col] = _norm_series(view_df[col])

    return view_df

# 코드마스터 조회 결과는 그룹코드/코드종류/항목코드 조합이 중복될 수 있으므로, 화면에서는 원본 컬럼 우선 + 보정 로직으로 최대한 중복 제거 + 일관된 날짜 포맷 적용해서 표시.
# (서비스가 신버전으로 개선되면 이 뷰 로직도 간소화 가능할 것으로 기대)

def _build_code_master_view(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df).copy()

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
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
                "NaT": None,
            })
        )
        dt = pd.to_datetime(s, errors="coerce")
        return dt.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    out = _coalesce_display_col(out, "그룹코드", ["__raw_gcode", "그룹코드", "Rd01_Gcode"])
    out = _coalesce_display_col(out, "코드종류", ["__raw_kind_name", "코드종류", "kind_name", "코드종류명"])
    out = _coalesce_display_col(out, "항목코드", ["__raw_tcode", "항목코드", "상세코드", "Rd01_Tcode"])
    out = _coalesce_display_col(out, "한글명", ["__raw_hnm", "Rd01_Hnm", "한글명", "한글 이름", "코드명"])
    out = _coalesce_display_col(out, "영문명", ["__raw_enm", "영문명", "Rd01_Enm"])
    out = _coalesce_display_col(out, "약칭", ["__raw_snm", "약칭", "짧은이름", "Rd01_Snm"])

    out = _coalesce_display_col(out, "기타1", ["기타1", "Rd01_Other1"])
    out = _coalesce_display_col(out, "기타2", ["기타2", "Rd01_Other2"])
    out = _coalesce_display_col(out, "기타3", ["기타3", "Rd01_Other3"])

    out = _coalesce_display_col(
        out,
        "등록자",
        ["__raw_add_user_nm", "등록자", "등록자명", "add_user_nm", "등록자코드", "Rd01_Add_Cd"],
    )
    out = _coalesce_display_col(out, "등록일자", ["__raw_add_date", "등록일자", "Rd01_Add_Date"])

    out = _coalesce_display_col(
        out,
        "수정자",
        ["__raw_mod_user_nm", "수정자", "수정자명", "mod_user_nm", "수정자코드", "Rd01_Mod_Cd"],
    )
    out = _coalesce_display_col(out, "수정일자", ["__raw_mod_date", "수정일자", "Rd01_Mod_Date"])

    out = _coalesce_display_col(out, "삭제여부", ["__raw_del_flag", "삭제여부", "Rd01_Del_Flag"])

    if {"그룹코드", "한글명"}.issubset(out.columns):
        g = _norm_series(out["그룹코드"])
        h = _norm_series(out["한글명"])
        k = _norm_series(out["코드종류"]) if "코드종류" in out.columns else pd.Series([""] * len(out), index=out.index)
        t = _norm_series(out["항목코드"]) if "항목코드" in out.columns else pd.Series([""] * len(out), index=out.index)

        mask_9999_blank = (g == "9999") & (h == "")
        out.loc[mask_9999_blank, "한글명"] = k.loc[mask_9999_blank]

        h2 = _norm_series(out["한글명"])
        mask_9999_blank2 = (g == "9999") & (h2 == "")
        out.loc[mask_9999_blank2, "한글명"] = t.loc[mask_9999_blank2]

    for col in [
        "그룹코드",
        "코드종류",
        "항목코드",
        "한글명",
        "영문명",
        "약칭",
        "기타1",
        "기타2",
        "기타3",
        "등록자",
        "수정자",
        "삭제여부",
    ]:
        if col in out.columns:
            out[col] = _norm_series(out[col])

    if "등록일자" in out.columns:
        out["등록일자"] = _fmt_char8_date(out["등록일자"])
    if "수정일자" in out.columns:
        out["수정일자"] = _fmt_char8_date(out["수정일자"])
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))

    preferred = [
        "순번",        
        "그룹코드",
        "코드종류",
        "항목코드",
        "한글명",
        "영문명",
        "약칭",
        "기타1",
        "기타2",
        "기타3",
        "등록자",
        "등록일자",
        "수정자",
        "수정일자",
        "삭제여부",
    ]

    for col in preferred:
        if col not in out.columns:
            out[col] = ""

    view_df = out[preferred].copy()

    for col in view_df.columns:
        if col not in {"등록일자", "수정일자"}:
            view_df[col] = _norm_series(view_df[col])

    return view_df

def _audit_date_text(value: Any) -> str:
    s = _clean_text(value)
    if not s:
        return ""

    digits = "".join(ch for ch in s if ch.isdigit())

    if len(digits) >= 8:
        return digits[:8]
    if len(digits) == 6:
        return digits
    if len(digits) == 4:
        return digits

    return s


def _audit_date_range_text(date_from: Any, date_to: Any) -> str:
    a = _audit_date_text(date_from)
    b = _audit_date_text(date_to)

    if a and b:
        return a if a == b else f"{a}~{b}"
    if a:
        return f"{a}~"
    if b:
        return f"~{b}"
    return ""

def _build_codes_query_condition(params: Dict[str, Any], total: int, display_count: int) -> str:
    params2 = dict(params or {})

    # date_input / 문자열 모두 YYYYMMDD 표시로 정리
    params2["등록일자From"] = _audit_date_text(params2.get("등록일자From"))
    params2["등록일자To"] = _audit_date_text(params2.get("등록일자To"))
    params2["수정일자From"] = _audit_date_text(params2.get("수정일자From"))
    params2["수정일자To"] = _audit_date_text(params2.get("수정일자To"))

    field_specs = [
        ("text", "그룹코드", "그룹코드"),
        ("text", "상세코드", "상세코드"),
        ("text", "통합검색", "통합검색"),
        ("text", "코드종류명", "코드종류명"),
        ("text", "한글명", "한글명"),
        ("text", "영문명", "영문명"),
        ("text", "약칭", "약칭"),
        ("text", "기타", "기타"),
        ("text", "등록자", "등록자"),
        ("date_range", "등록일자", "등록일자From", "등록일자To"),
        ("text", "수정자", "수정자"),
        ("date_range", "수정일자", "수정일자From", "수정일자To"),
    ]

    query_condition = build_master_query_condition(
        params2,
        total=total,
        display_count=display_count,
        field_specs=field_specs,
        active_key="사용중만",
        active_text="사용구분 사용중",
    )

    # 안전장치:
    # 공용 build_master_query_condition 이 날짜조건을 놓치면 여기서 반드시 보강한다.
    lines = [x.strip() for x in str(query_condition or "").splitlines() if x.strip()]
    first = lines[0] if lines else ""
    rest = lines[1:] if len(lines) > 1 else []

    forced_parts = []

    add_range = _audit_date_range_text(
        params2.get("등록일자From"),
        params2.get("등록일자To"),
    )
    if add_range and "등록일자" not in first:
        forced_parts.append(f"등록일자 {add_range}")

    mod_range = _audit_date_range_text(
        params2.get("수정일자From"),
        params2.get("수정일자To"),
    )
    if mod_range and "수정일자" not in first:
        forced_parts.append(f"수정일자 {mod_range}")

    if forced_parts:
        if first and first != "전체":
            first = " / ".join(forced_parts + [first])
        else:
            first = " / ".join(forced_parts)

    if not first:
        first = "전체"

    return "\n".join([first] + rest)

def _split_condition_and_note(query_condition: str) -> tuple[str, str]:
    lines = [x.strip() for x in str(query_condition or "").splitlines() if x.strip()]
    if not lines:
        return "전체", ""
    return lines[0], "\n".join(lines[1:]).strip()


def _build_codes_master_llm_summary(
    df_all_display: pd.DataFrame,
    *,
    query_condition: str,
    total: int,
    display_count: int,
) -> dict[str, Any]:
    return build_master_llm_summary(
        df_all_display,
        master_name="업무코드마스터",
        query_condition=query_condition,
        total=total,
        display_count=display_count,
        count_specs=[
            ("그룹코드별 상위", "그룹코드", "gcode_top"),
            ("코드종류별 상위", "코드종류", "kind_top"),
            ("등록자별 상위", "등록자", "add_user_top"),
            ("수정자별 상위", "수정자", "mod_user_top"),
            ("삭제여부별 상위", "삭제여부", "del_flag_top"),
        ],
        year_specs=[
            ("등록연도별 상위", "등록일자", "add_year_top"),
            ("수정연도별 상위", "수정일자", "mod_year_top"),
        ],
        top_n=10,
        answer_rule="화면에는 일부만 표시될 수 있지만, 위 집계는 전체 조회결과 기준입니다.",
    )


# ------------------------------------------------------------------------------
# 1) 업무코드 마스터 직접 조회
# ------------------------------------------------------------------------------
def render_code_master_search() -> Dict[str, Any]:
    st.subheader("🧩 업무코드 마스터 조회")
    st.caption("업무코드 마스터 화면은 후보검색 없이 직접 조회합니다.")
    master_max_rows = _master_max_rows()

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_codes_master_{ns}"

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):

        c1, c2 = st.columns(2)
        with c1:
            gcode = st.text_input("그룹코드", value="", key=f"__codes_gcode__{ns}")
        with c2:
            tcode = st.text_input("상세코드", value="", key=f"__codes_tcode__{ns}")

        c3, c4 = st.columns(2)
        with c3:
            keyword = st.text_input(
                "통합검색",
                value="",
                placeholder="코드종류 / 한글명 / 영문명 / 약칭 / 기타",
                key=f"__codes_keyword__{ns}",
            )
        with c4:
            kind_name_kw = st.text_input("코드종류명", value="", key=f"__codes_kind_name__{ns}")

        c5, c6, c7 = st.columns(3)
        with c5:
            hnm_kw = st.text_input("한글명", value="", key=f"__codes_hnm__{ns}")
        with c6:
            enm_kw = st.text_input("영문명", value="", key=f"__codes_enm__{ns}")
        with c7:
            snm_kw = st.text_input("약칭", value="", key=f"__codes_snm__{ns}")

        c8, c9, c10 = st.columns([2, 1, 2])
        with c8:
            other_kw = st.text_input("기타", value="", key=f"__codes_other__{ns}")
        with c9:
            only_active = st.checkbox("사용중만", value=True, key=f"__codes_active__{ns}")
        with c10:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        audit_filter = render_master_audit_filter(
            prefix="codes",
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
            "title": "업무코드 마스터 조회",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    params = {
        "그룹코드": _clean_text(gcode),
        "상세코드": _clean_text(tcode),
        "통합검색": _clean_text(keyword),
        "코드종류명": _clean_text(kind_name_kw),
        "한글명": _clean_text(hnm_kw),
        "영문명": _clean_text(enm_kw),
        "약칭": _clean_text(snm_kw),
        "기타": _clean_text(other_kw),
        "등록자": _clean_text(add_user_nm),
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}"
            if (add_date_from or add_date_to)
            else ""
        ),
        "등록일자From": add_date_from,
        "등록일자To": add_date_to,
        "수정자": _clean_text(mod_user_nm),
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
        fetch_top = int(master_max_rows)

        df = _service_search(
            gcode=params["그룹코드"],
            tcode=params["상세코드"],
            keyword=params["통합검색"],
            kind_name_kw=params["코드종류명"],
            hnm_kw=params["한글명"],
            enm_kw=params["영문명"],
            snm_kw=params["약칭"],
            other_kw=params["기타"],
            add_user_nm_kw=params["등록자"],
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm_kw=params["수정자"],
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            only_active=params["사용중만"],
            top=fetch_top,
        )

        total = int(len(df))
        view_df_all = _build_code_master_view(df)
        # 화면 표시 제한은 chat/sims_panel 공통 표시 제한에서 처리한다.
        view_df = view_df_all.copy()
        display_count = int(len(view_df))

        query_condition = _build_codes_query_condition(params, total, display_count)
        condition_text, note = _split_condition_and_note(query_condition)

#
#       임시 Start
#
#        log.info(
#            "[view.codes] condition=%r add_date=%r~%r mod_date=%r~%r total=%s display=%s",
#            condition_text,
#            add_date_from,
#            add_date_to,
#            mod_date_from,
#            mod_date_to,
#            total,
#            display_count,
#        )
#
#       임시 End
#


        codes_master_summary = _build_codes_master_llm_summary(
            view_df_all,
            query_condition=query_condition,
            total=total,
            display_count=display_count,
        )
        llm_summary_md = str(codes_master_summary.get("llm_summary_md") or "")

        summary_parts = []
        if str(note or "").strip():
            summary_parts.append(str(note).strip())
        if str(llm_summary_md or "").strip():
            summary_parts.append(str(llm_summary_md).strip())
        summary_md = "\n\n".join(summary_parts).strip()

        meta = {
            "총건수": total,
            "row_count": total,
            "row_count_total": total,
            "display_row_count": display_count,
            "show_n": display_count,
            "코드종류사전Gcode": GCODE_KIND,
            "그룹코드목록": (
                sorted(df["그룹코드"].dropna().astype(str).unique().tolist())
                if "그룹코드" in df.columns
                else []
            ),
            "source": "업무코드마스터(Rddbc010)",
            "query_summary": condition_text,
            "condition": condition_text,
            "summary_md": summary_md,
            "analysis_type": "codes_master",
            "llm_summary_kind": "codes_master_summary",
            "llm_summary_md": llm_summary_md,
            "codes_master_summary": codes_master_summary,
            "analysis_row_count": total,
            "row_count_total_for_analysis": total,
            "summary_basis": "전체 조회결과 기준",
            "field_notes": (
                "업무코드 마스터 분석은 전체 조회결과 기준 집계요약을 우선 근거로 답합니다. "
                "화면 표시는 일부 행으로 제한될 수 있습니다."
            ),
        }

        return {
            "final": True,
            "type": "table",
            "title": "업무코드 마스터 조회",
            "action": "업무코드 마스터 조회",
            "params": params,
            "df": df,
            "df_display": view_df,
            "meta": meta,
        }

    except Exception as e:
        log.exception("[view.codes] render_code_master_search failed")
        return {
            "final": False,
            "type": "text",
            "title": "업무코드 마스터 조회 오류",
            "data": str(e),
        }
    
# ------------------------------------------------------------------------------
# 2) 그룹코드별 상세코드 조회
# ------------------------------------------------------------------------------
def render_codes_by_group() -> Dict[str, Any]:
    st.subheader("🗂 그룹코드별 상세코드 조회")
    st.caption("그룹코드 또는 코드종류명으로 해당 그룹의 상세코드 목록을 조회합니다.")
    master_max_rows = _master_max_rows()

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_codes_by_group_{ns}"

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):        
        c1, c2 = st.columns(2)
        with c1:
            gcode = st.text_input("그룹코드", value="", key=f"__codes_by_group_gcode__{ns}")
        with c2:
            kind_name = st.text_input("코드종류명", value="", key=f"__codes_by_group_kind_name__{ns}")

        c3, c4 = st.columns([1, 2])
        with c3:
            only_active = st.checkbox("사용중만", value=True, key=f"__codes_by_group_active__{ns}")
        with c4:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")

        submitted = st.form_submit_button("조회", type="primary")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "그룹코드별 상세코드 조회",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    gcode = _clean_text(gcode)
    kind_name = _clean_text(kind_name)

    if not gcode and not kind_name:
        return {
            "final": False,
            "type": "text",
            "title": "그룹코드별 상세코드 조회",
            "data": "그룹코드 또는 코드종류명을 입력하세요.",
        }

    try:
        resolved_group_codes: List[str] = []
        matched_kinds_df = pd.DataFrame()

        if gcode:
            resolved_group_codes = [gcode]
        else:
            matched_kinds_df = _service_find_group_codes_by_kind_name(
                kind_name=kind_name,
                top=max(50, int(master_max_rows)),
                only_active=only_active,
            )
            if matched_kinds_df.empty or "그룹코드" not in matched_kinds_df.columns:
                return {
                    "final": True,
                    "type": "table",
                    "title": "그룹코드별 상세코드 조회",
                    "action": "그룹코드조회",
                    "params": {
                        "그룹코드": gcode,
                        "코드종류명": kind_name,
                        "사용중만": bool(only_active),
                        "조회상한": int(master_max_rows),
                    },
                    "df": pd.DataFrame(),
                    "df_display": pd.DataFrame(columns=[
                        "코드종류", "항목코드", "상세코드", "한글명",
                        "등록자", "등록일자", "수정자", "수정일자", "삭제여부"
                    ]),
                    "meta": {
                        "입력그룹코드": gcode,
                        "입력코드종류명": kind_name,
                        "해석그룹코드목록": [],
                        "총건수": 0,
                        "사용중만": bool(only_active),
                        "조회상한": int(master_max_rows),
                    },
                }

            resolved_group_codes = (
                matched_kinds_df["그룹코드"]
                .fillna("")
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .unique()
                .tolist()
            )

        df = _merge_group_results(
            resolved_group_codes,
            only_active=only_active,
            top=int(master_max_rows),
        )

        view_df = _build_group_result_view(df)

        meta = {
            "입력그룹코드": gcode,
            "입력코드종류명": kind_name,
            "해석그룹코드목록": resolved_group_codes,
            "총건수": int(len(df)),
            "사용중만": bool(only_active),
            "조회상한": int(master_max_rows),
        }

        if not matched_kinds_df.empty:
            meta["코드종류후보건수"] = int(len(matched_kinds_df))

        return {
            "final": True,
            "type": "table",
            "title": "그룹코드별 상세코드 조회",
            "action": "그룹코드조회",
            "params": {
                "그룹코드": gcode,
                "코드종류명": kind_name,
                "사용중만": bool(only_active),
                "조회상한": int(master_max_rows),
            },
            "df": df,
            "df_display": view_df,
            "meta": meta,
        }

    except Exception as e:
        log.exception("[view.codes] render_codes_by_group failed")
        return {
            "final": False,
            "type": "text",
            "title": "그룹코드별 상세코드 조회 오류",
            "data": str(e),
        }
    
# ------------------------------------------------------------------------------
# 3) 코드종류 사전(9999)
# ------------------------------------------------------------------------------
def render_code_kind_dictionary() -> Dict[str, Any]:
    st.subheader("🗂 코드종류 사전(9999)")
    master_max_rows = _master_max_rows()

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_code_kinds_{ns}"

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):        
        
        keyword = st.text_input("코드종류명", value="", key=f"__code_kinds_keyword__{ns}")
        c1, c2 = st.columns([1, 2])
        with c1:
            only_active = st.checkbox("사용중만", value=True, key=f"__code_kinds_active__{ns}")
        with c2:
            st.caption(f"조회상한: 최대 {master_max_rows:,}건")
        submitted = st.form_submit_button("조회", type="primary")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "코드종류 사전",
            "data": "[조회] 버튼을 눌러 실행하세요.",
        }

    try:
        df = _service_group_kinds(top=int(master_max_rows), only_active=only_active)
        if keyword:
            kind_col = _pick_col(df, ["한글명", "코드종류"])
            if kind_col:
                df = df[_contains_mask(df[kind_col], keyword)].reset_index(drop=True)

        view_df = _build_code_master_view(df)

        meta = {
            "총건수": int(len(df)),
            "코드종류사전Gcode": GCODE_KIND,
            "코드종류코드목록": (
                sorted(df["항목코드"].dropna().astype(str).unique().tolist())
                if "항목코드" in df.columns
                else []
            ),
        }

        return {
            "final": True,
            "type": "table",
            "title": "코드종류 사전",
            "action": "코드종류 사전",
            "params": {"코드종류명": _clean_text(keyword), "사용중만": bool(only_active), "조회상한": int(master_max_rows)},
            "df": df,
            "df_display": view_df,
            "meta": meta,
        }

    except Exception as e:
        log.exception("[view.codes] render_code_kind_dictionary failed")
        return {
            "final": False,
            "type": "text",
            "title": "코드종류 사전 오류",
            "data": str(e),
        }

# ------------------------------------------------------------------------------
# 4) 그룹코드별 건수
# ------------------------------------------------------------------------------
def render_code_count_by_group() -> Dict[str, Any]:
    st.subheader("📊 그룹코드별 건수")
    master_max_rows = _master_max_rows()

    ns = str(st.session_state.get("__sims_widget_ns", "0"))
    form_key = f"sims_code_count_by_group_{ns}"

    with st.form(
        key=form_key,
        clear_on_submit=False,
        enter_to_submit=False,
    ):        
        only_active = st.checkbox("사용중만", value=True, key=f"__code_count_active__{ns}")
        st.caption(f"조회상한: 최대 {master_max_rows:,}건")
        submitted = st.form_submit_button("집계", type="primary")

    if not submitted:
        return {
            "final": False,
            "type": "text",
            "title": "그룹코드별 건수",
            "data": "[집계] 버튼을 눌러 실행하세요.",
        }

    try:
        df = _service_search(only_active=only_active, top=int(master_max_rows))

        group_col = _pick_col(df, ["그룹코드", "Rd01_Gcode"])
        kind_col = _pick_col(df, ["코드종류", "kind_name"])

        if group_col:
            group_keys = [group_col]
            if kind_col and kind_col != group_col:
                group_keys.append(kind_col)
            out = df.groupby(group_keys, dropna=False, as_index=False).size()
            if "size" in out.columns:
                out = out.rename(columns={"size": "건수"})
        else:
            out = df.copy()

        meta = {
            "그룹수": int(len(out)),
            "총코드건수": int(len(df)),
            "그룹컬럼": group_col,
            "코드종류컬럼": kind_col,
        }

        return {
            "final": True,
            "type": "table",
            "title": "그룹코드별 건수",
            "action": "그룹코드별 건수",
            "params": {"사용중만": bool(only_active), "조회상한": int(master_max_rows)},
            "df": out,
            "df_display": out,
            "meta": meta,
        }

    except Exception as e:
        log.exception("[view.codes] render_code_count_by_group failed")
        return {
            "final": False,
            "type": "text",
            "title": "그룹코드별 건수 오류",
            "data": str(e),
        }
    
# ------------------------------------------------------------------------------
# legacy compatibility : 코드명 검색
# ------------------------------------------------------------------------------
def render_search_codes() -> Dict[str, Any]:
    """
    sims_panel.py 호환용.
    기존 '코드명 검색' 메뉴를 현재의 업무코드 마스터 조회 화면으로 연결한다.
    """
    result = render_code_master_search()
    if isinstance(result, dict):
        result = dict(result)
        result["title"] = "코드명 검색"
        result["action"] = "코드명 검색"
    return result


def render_code_name_search() -> Dict[str, Any]:
    return render_search_codes()


def render_search_code_names() -> Dict[str, Any]:
    return render_search_codes()


def render_codes_search() -> Dict[str, Any]:
    return render_search_codes()


# ------------------------------------------------------------------------------
# compatibility aliases
# ------------------------------------------------------------------------------
def render_codes() -> Dict[str, Any]:
    return render_code_master_search()


def render_code_list() -> Dict[str, Any]:
    return render_code_master_search()


def render_rddbc010() -> Dict[str, Any]:
    return render_code_master_search()


def view_rddbc010() -> Dict[str, Any]:
    return render_code_master_search()


def render_group_codes() -> Dict[str, Any]:
    return render_codes_by_group()
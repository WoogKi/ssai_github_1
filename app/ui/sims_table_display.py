# app/ui/sims_table_display.py
# -*- coding: utf-8 -*-
#   2026/05/19 Create
# - 컬럼 폭 계산
# - table_width 계산
# - table_height 계산
# - pinned 컬럼 판정
# - Streamlit pinned fallback 처리
#

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple
from decimal import Decimal
import inspect
import logging
import os

import numpy as np
import pandas as pd
import streamlit as st

log = logging.getLogger("ssai")

_DISPLAY_LITERAL_NULL_STRINGS = {
    "None",
    "none",
    "NONE",
    "nan",
    "NaN",
    "NAN",
    "<NA>",
    "NaT",
    "NULL",
    "null",
}

_DISPLAY_FIELD_DIAGNOSTIC_COLS = (
    "재고기준",
    "수요예상기준",
    "분석자료원",
    "현재고원천",
)


def resolve_sims_table_mode(
    df: pd.DataFrame,
    *,
    action: Any = "",
    render_path: str = "chat",
) -> Dict[str, Any]:
    rows = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    cols = int(len(df.columns)) if isinstance(df, pd.DataFrame) else 0
    cells = rows * cols
    path = str(render_path or "").strip().lower()
    if path == "chat":
        env_key = "SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"
        raw_value = os.getenv(env_key)
        fallback_key = "SIMS_FAST_TABLE_CELL_THRESHOLD"
        if raw_value is None:
            raw_value = os.getenv(fallback_key, "6000")
            used_key = fallback_key
        else:
            used_key = env_key
    else:
        used_key = "SIMS_FAST_TABLE_CELL_THRESHOLD"
        raw_value = os.getenv(used_key, "6000")
    try:
        threshold = int(raw_value)
    except Exception:
        threshold = 6000
    mode = "fast" if threshold > 0 and cells >= threshold else "small"
    reason = "cells>=threshold" if mode == "fast" else "cells<threshold"
    return {
        "action": str(action or ""),
        "render_path": path or "chat",
        "rows": rows,
        "cols": cols,
        "cells": cells,
        "env_key": used_key,
        "env_value": raw_value,
        "resolved_threshold": threshold,
        "mode": mode,
        "reason": reason,
    }


def log_sims_table_mode(df: pd.DataFrame, *, action: Any = "", render_path: str = "chat") -> Dict[str, Any]:
    info = resolve_sims_table_mode(df, action=action, render_path=render_path)
    log.info(
        "[sims.table_mode] action=%s render_path=%s rows=%s cols=%s cells=%s env_key=%s env_value=%s resolved_threshold=%s mode=%s reason=%s",
        info["action"],
        info["render_path"],
        info["rows"],
        info["cols"],
        info["cells"],
        info["env_key"],
        info["env_value"],
        info["resolved_threshold"],
        info["mode"],
        info["reason"],
    )
    return info


def log_sims_table_render(
    df: pd.DataFrame,
    *,
    action: Any = "",
    render_path: str = "chat",
    mode: str = "",
    renderer: str = "",
    height: Any = "",
    visible_rows: Any = "",
    width_mode: str = "",
    column_config_count: Any = "",
    full_rows: Any = "",
    display_rows: Any = "",
    render_truncated: Any = "",
    display_limit: Any = "",
) -> None:
    rows = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    cols = int(len(df.columns)) if isinstance(df, pd.DataFrame) else 0
    if full_rows == "":
        full_rows = rows
    if display_rows == "":
        display_rows = rows
    if render_truncated == "":
        render_truncated = False
    if visible_rows == "":
        try:
            visible_rows = min(rows, max(int((int(height) - 48) / 32), 0)) if height not in ("", None) else ""
        except Exception:
            visible_rows = ""
    log.info(
        "[sims.table_render] action=%s render_path=%s mode=%s renderer=%s height=%s visible_rows=%s width_mode=%s column_config_count=%s rows=%s cols=%s full_rows=%s display_rows=%s render_truncated=%s display_limit=%s",
        str(action or ""),
        str(render_path or "chat"),
        str(mode or ""),
        str(renderer or ""),
        height,
        visible_rows,
        str(width_mode or ""),
        column_config_count,
        rows,
        cols,
        full_rows,
        display_rows,
        bool(render_truncated),
        display_limit,
    )


def _is_display_missing_token(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() in _DISPLAY_LITERAL_NULL_STRINGS
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return False


def _display_non_null_count(series: pd.Series) -> int:
    try:
        return int(series.astype("object").map(lambda v: not _is_display_missing_token(v)).sum())
    except Exception:
        return 0


def _display_literal_none_count(series: pd.Series) -> int:
    try:
        return int(series.astype("object").map(lambda v: isinstance(v, str) and v.strip() in _DISPLAY_LITERAL_NULL_STRINGS).sum())
    except Exception:
        return 0


def log_sims_display_fields(
    source_df: pd.DataFrame,
    display_df: pd.DataFrame,
    *,
    action: Any = "",
    render_path: str = "",
    mode: str = "",
) -> None:
    """Log display-only null handling for selected business fields without row data."""
    if not isinstance(source_df, pd.DataFrame) or not isinstance(display_df, pd.DataFrame):
        return

    for col in _DISPLAY_FIELD_DIAGNOSTIC_COLS:
        column_present = col in source_df.columns or col in display_df.columns
        if not column_present:
            continue

        src = source_df[col] if col in source_df.columns else pd.Series(dtype="object")
        disp = display_df[col] if col in display_df.columns else pd.Series(dtype="object")
        try:
            actual_null_count = int(disp.isna().sum())
        except Exception:
            actual_null_count = 0
        try:
            source_actual_null_count = int(src.isna().sum())
        except Exception:
            source_actual_null_count = 0
        try:
            source_dtype = str(src.dtype)
        except Exception:
            source_dtype = ""
        try:
            display_dtype = str(disp.dtype)
        except Exception:
            display_dtype = ""
        try:
            source_first_type = type(src.iloc[0]).__name__ if len(src) else ""
        except Exception:
            source_first_type = ""
        try:
            display_first_type = type(disp.iloc[0]).__name__ if len(disp) else ""
        except Exception:
            display_first_type = ""

        log.info(
            "[sims.table.display_fields] action=%s render_path=%s mode=%s rows=%s field=%s column_present=%s source_dtype=%s display_dtype=%s source_non_null_count=%s display_non_null_count=%s source_actual_null_count=%s display_actual_null_count=%s literal_none_count=%s first_source_type=%s first_display_type=%s",
            str(action or ""),
            str(render_path or ""),
            str(mode or ""),
            int(len(display_df)),
            col,
            bool(column_present),
            source_dtype,
            display_dtype,
            _display_non_null_count(src),
            _display_non_null_count(disp),
            source_actual_null_count,
            actual_null_count,
            _display_literal_none_count(disp),
            source_first_type,
            display_first_type,
        )


def _clean_text(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value or "").strip()


def _is_explicit_code_display_name(col: Any) -> bool:
    s = _clean_text(col)
    s_lower = s.lower()
    if s in {"거래명세서구분", "Rd13_Trans_Di"}:
        return True
    code_words = (
        "제품코드",
        "제조사코드",
        "거래처코드",
        "매입처코드",
        "재고적용처코드",
        "보험코드",
        "표준코드",
        "바코드",
        "stock_cd",
        "buy_cd",
        "physic_cd",
        "ven_cd",
        "_cd",
        "코드",
    )
    return any(w in s or w in s_lower for w in code_words)


_INTEGER_IDENTIFIER_DISPLAY_NAMES = {
    "거래명세서순번",
    "세금계산서구분",
    "세금계산서순번",
    "전표순번",
    "회계전표순번",
    "배송순번",
    "피킹출력순번",
    "Rd13_Trans_Seq",
    "Rd13_Slip_Seq",
    "Rd13_Print_Seq",
    "Rd13_Picking_PrinterSeq",
    "Rd13_Delivery_PrinterSeq",
    "Rd14_Tax_Di",
    "Rd14_Tax_Seq",
    "Rd14_Slip_Seq",
    "Rd14_Print_Seq",
    "Rd14_Move_Seq",
}


def _is_numeric_display_name(col: Any) -> bool:
    s = _clean_text(col)

    if s == "명세서번호":
        return True

    # 입출고일자는 '출고'라는 단어를 포함해도 날짜 텍스트다.
    if "일자" in s or "날짜" in s or "년월" in s:
        return False

    if _is_explicit_code_display_name(s):
        return False

    text_words = (
        "기준",
        "판정",
        "등급",
        "원천",
        "자료원",
        "설명",
        "결과",
    )
    text_numeric_exceptions = (
        "예상기준월수량",
        "수요예상수량",
        "평가월 예상수요수량",
        "당월 예상출고수량",
        "당월 잔여예상출고수량",
    )
    if any(w in s for w in text_words) and not any(w in s for w in text_numeric_exceptions):
        return False

    # 분석/KPI 명시 숫자 컬럼
    # payload가 JSON/records에서 복원되어 object 문자열이 되어도
    # 화면 렌더 전에 숫자형으로 복구한다.
    explicit_numeric_cols = {
        "완료월총매출",
        "월평균매출",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "전월대비매출",
        "총매출공급가액",
        "매출공급가액",
        "매출세액",
        "매출합계",
        "평가월 현재매출",
        "평가월 예상매출",
        "평가월 잔여예상",
        "다음월예상매출",
        "3개월예상매출",
        "6개월예상매출",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "평균공급단가",
        "당월 진척률",
        "평가월 진척률",
        "전월대비매출증감률",
        "최근3개월증감률",
        "월시점 최근3개월증감률",
        "적용증감률",
        "월시점 증감률",
        "월시점 적용증감률",
        "월시점 달성률",
        "최근3개월수량증감률",
        "수요증감률",
        "수요적용증감률",
        "평가월 수요진척률",
        "당월 출고진척률",
        "당월 재고충족률",
    }

    if s in explicit_numeric_cols:
        return True

    numeric_words = (
        "장부재고평가단가",
        "실재고평가단가",
        "현재재고수량",
        "예상기준월수량",
        "재고커버월수",
        "매출수량",
        "매출금액",
        "공급가액",
        "세액",
        "합계금액",
        "단가",
        "평가금액",
        "재고금액",
        "수량",
        "금액",
        "평가단가",
        "커버월수",
    )
    return any(w in s for w in numeric_words)


def _normalize_display_scalar(value: Any) -> Any:
    if _is_display_missing_token(value):
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    if isinstance(value, bytearray):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    if isinstance(value, Decimal):
        try:
            if value == value.to_integral_value():
                return int(value)
        except Exception:
            pass
        return float(value)
    return value


def _format_display_yyyymm(value: Any) -> Any:
    if _is_display_missing_token(value):
        return ""
    s = str(value).strip()
    if not s:
        return value
    if len(s) >= 2 and s.endswith(".0"):
        s = s[:-2]
    if len(s) == 6 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}"
    return s


def _format_display_yyyymmdd(value: Any) -> Any:
    if _is_display_missing_token(value):
        return ""
    s = str(value).strip()
    if not s:
        return value
    if len(s) >= 2 and s.endswith(".0"):
        s = s[:-2]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def normalize_display_df_for_streamlit(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a display-only DataFrame that is safer for Streamlit/Arrow conversion.
    The source DataFrame is not mutated.
    """
    if not isinstance(df, pd.DataFrame):
        return df

    out = df.copy()
    for col in list(out.columns):
        try:
            sr = out[col]
        except Exception:
            continue

        col_name = _clean_text(col)
        if col_name == "기준월":
            out[col] = sr.map(_format_display_yyyymm)
            continue
        # Date facts do not always use the generic '일자'/'날짜' suffix.
        # Keep the explicit set narrow: period and count fields such as
        # '입고 경과일' and '정상 입고 거래일수' must remain numeric cells.
        if (
            "일자" in col_name
            or "날짜" in col_name
            or col_name in {
                "최근 정상 입고일",
                "최근 정상 출고일",
                "최근 입고일",
                "최근 출고일",
            }
        ):
            out[col] = sr.map(_format_display_yyyymmdd)
            continue

        if pd.api.types.is_datetime64_any_dtype(sr):
            # A fully missing datetime column can have no date-like name (for
            # example an optional detail reason).  Keep real values intact,
            # but make NaT a visual blank like every other display null.
            out[col] = sr.map(lambda value: "" if _is_display_missing_token(value) else value)
            continue

        if _is_numeric_display_col(out, col):
            numeric_source = sr.map(
                lambda value: None if _is_display_missing_token(value) else value
            )
            # The chat renderer turns display-only numeric nulls into empty
            # cells immediately before st.dataframe().  Keep ordinary numeric
            # values here so that final renderer step can use the established
            # object-plus-empty-cell Streamlit contract.
            out[col] = pd.to_numeric(numeric_source, errors="coerce")
            continue

        if pd.api.types.is_object_dtype(sr) or pd.api.types.is_string_dtype(sr):
            cleaned = sr.map(_normalize_display_scalar)
            sample = cleaned.dropna().head(80).tolist()
            mixed_binary_or_decimal = any(
                isinstance(v, (bytes, bytearray, Decimal)) for v in sample
            )
            mixed_types = {
                type(v)
                for v in sample
                if v is not None and not isinstance(v, str)
            }
            if mixed_binary_or_decimal or len(mixed_types) > 1:
                out[col] = cleaned.map(lambda v: "" if v is None else str(v))
            else:
                out[col] = cleaned

    return out


def _make_arrow_safe_df(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_display_df_for_streamlit(df)


def _has_korean(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in str(text or ""))


def _display_len(text: Any) -> int:
    """
    폭 계산용 대략 길이.
    한글은 영문보다 넓게 본다.
    """
    s = _clean_text(text)
    n = 0
    for ch in s:
        if "가" <= ch <= "힣":
            n += 2
        elif ord(ch) > 127:
            n += 2
        else:
            n += 1
    return n


def _supports_pinned() -> bool:
    """
    Streamlit column_config pinned 지원 여부.
    사용자님 환경 1.48.1은 지원 가능하지만, 안전하게 검사한다.
    """
    try:
        sig = inspect.signature(st.column_config.TextColumn)
        return "pinned" in sig.parameters
    except Exception:
        return False


def _is_row_no_col(col: Any) -> bool:
    return _clean_text(col) in {"순번", "조회순번", "번호"}


def _is_code_like_col(col: Any) -> bool:
    s = _clean_text(col)

    if _is_row_no_col(s):
        return False

    code_words = [
        "코드",
        "ID",
        "아이디",
        "번호",
        "순번",
        "사업자등록번호",
        "보험코드",
        "표준코드",
        "바코드",
        "우편번호",
        "일자",
        "날짜",
        "월",
        "년월",
        "시간",
    ]

    return any(w in s for w in code_words)


def _is_numeric_display_col(df: pd.DataFrame, col: Any) -> bool:
    s = _clean_text(col)

    # Validation status is textual even when an all-empty export column is
    # inferred as float by pandas.
    if s == "상세합계일치":
        return False

    if s in _INTEGER_IDENTIFIER_DISPLAY_NAMES:
        return True

    if _is_row_no_col(s):
        return True

    if s == "명세서번호":
        return True

    explicit_numeric_cols = {
        "완료월총매출",
        "월평균매출",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "평가월 현재매출",
        "평가월 예상매출",
        "평가월 잔여예상",
        "다음월예상매출",
        "3개월예상매출",
        "6개월예상매출",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "평균공급단가",
        "당월 진척률",
        "평가월 진척률",
        "최근3개월증감률",
        "적용증감률",
        "월시점 증감률",
        "월시점 적용증감률",
        "월시점 달성률",
        "최근3개월수량증감률",
        "수요증감률",
        "수요적용증감률",
        "평가월 수요진척률",
        "당월 출고진척률",
        "당월 재고충족률",
        "부족예상금액",
        # Dashboard inbound facts: these names remain numeric even after the
        # blank-safe display copy has changed a nullable column to strings.
        # Without this, a later legacy NumberColumn override can turn visual
        # blanks back into Streamlit's literal None.
        "입고 경과일",
        "정상 입고 거래일수",
        "평균 입고간격일",
    }
    if s in explicit_numeric_cols:
        return True

    # 재고부족/예상 관련 수량 컬럼은 이름에 '기준'이 들어가도 숫자 컬럼이다.
    force_numeric_words = (
        "예상기준월수량",
        "최근3개월평균수량",
        "최근6개월평균수량",
        "월평균출고수량",
        "재고커버월수",
        "평균재고커버월수",
        "필요수량",
        "부족수량",
        "현재재고수량",
        "현재재고금액",
    )
    if any(w in s for w in force_numeric_words):
        return True

    # Semantic code/date names always win over a driver-provided numeric dtype
    # so leading zeros remain intact.  For every other column, the service
    # DataFrame dtype is stronger evidence than a Korean display-name fragment:
    # document sequences, quantities, and statement totals are often numeric
    # even when their label does not contain a conventional amount keyword.
    if _is_explicit_code_display_name(s):
        return False

    try:
        if pd.api.types.is_numeric_dtype(df[col]):
            return True
    except Exception:
        pass

    # 명칭/구분/판정/기준/자료원 계열은 숫자처럼 보여도 문자 표시
    text_words = [
        "명",
        "이름",
        "주소",
        "비고",
        "구분",
        "분류",
        "그룹",
        "등급",
        "판정",
        "기준",
        "원천",
        "자료원",
        "설명",
        "결과",
    ]

    if any(w in s for w in text_words) or s.endswith("처"):
        return False

    # 숫자 표시 대상 단어
    # 중요:
    # - code-like 판정보다 먼저 수행한다.
    # - 월평균매출, 매출발생월수, 2026-01 매출 같은 컬럼이
    #   "월" 때문에 문자 컬럼으로 빠지는 것을 방지한다.
    numeric_words = [
        "수량",
        "금액",
        "단가",
        "가격",
        "세액",
        "공급가액",
        "합계",
        "잔액",
        "율",
        "비율",
        "증감률",
        "건수",
        "품목수",
        "거래처수",
        "매입처수",
        "재고적용처수",
        "개월",
        "월수",
        "발생월수",
        "분석월수",
        "커버",
        "평균",
    ]

    if any(w in s for w in numeric_words):
        return True

    # 제품코드/거래처코드/기준월/일자/번호 등은 문자 유지
    if _is_code_like_col(s):
        return False

    try:
        return pd.api.types.is_numeric_dtype(df[col])
    except Exception:
        return False
    
def _numeric_display_kind(col: Any) -> str:
    """
    숫자 컬럼 표시 자릿수 판정.

    반환:
    - "int"      : 천단위 콤마, 소수점 없음
    - "decimal2" : 천단위 콤마, 소수점 2자리
    """
    s = _clean_text(col)

    if _is_row_no_col(s):
        return "int"

    if s == "명세서번호":
        return "int"

    # Transaction-document sequence identifiers are numeric display values,
    # not amounts.  Keep their Excel/UI surface integer-formatted.
    if s in _INTEGER_IDENTIFIER_DISPLAY_NAMES:
        return "int"

    if _is_stock_shortage_quantity_int_col(s):
        return "int"

    int_money_cols = {
        "완료월총매출",
        "월평균매출",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "다음월예상매출",
        "3개월예상매출",
        "6개월예상매출",
        "부족예상금액",
    }
    if s in int_money_cols:
        return "int"

    decimal_money_cols = {
        "최근3개월평균매출",
        "최근6개월평균매출",
        "평균공급단가",
        "완료월평균출고수량",
        "최근3개월평균출고수량",
        "최근6개월평균출고수량",
        "당월 예상출고수량",
        "당월 잔여예상출고수량",
        "예상월말재고수량",
        "부족예상수량",
    }
    if s in decimal_money_cols:
        return "decimal2"

    percent_cols = {
        "당월 진척률",
        "평가월 진척률",
        "최근3개월증감률",
        "적용증감률",
        "월시점 증감률",
        "월시점 적용증감률",
        "월시점 달성률",
        "최근3개월수량증감률",
        "수요증감률",
        "수요적용증감률",
        "평가월 수요진척률",
        "당월 출고진척률",
        "당월 재고충족률",
    }
    # Fast chat KPI tables use these established percent labels as well.
    percent_cols.update({"전월대비매출증감률", "월시점 최근3개월증감률"})
    if s in percent_cols:
        return "percent2"

    # 소수점 2자리 우선 판정
    decimal_words = [
        "평균",
        "단가",
        "율",
        "비율",
        "증감률",
        "커버",
        "예상기준월수량",
    ]
    if any(w in s for w in decimal_words):
        return "decimal2"

    # 정수 표시
    int_words = [
        "건수",
        "행수",
        "품목수",
        "거래처수",
        "매입처수",
        "재고적용처수",
        "수량",
        "금액",
        "공급가액",
        "세액",
        "합계",
        "잔액",
        "매출",
        "매입",
        "출고",
        "입고",
        "재고",
        "부족",
        "필요",
        "월수",
        "발생월수",
        "분석월수",
        "집계건수",
        "전월대비",
        "총매출액",
        "총출고수량",
        "총출고할증수량",
        "총집계건수",
        "증가품목수",
        "안정품목수",
        "감소품목수",
        "자료부족품목수",
        "증가행수",
        "안정행수",
        "감소행수",
        "자료부족행수",
    ]
    if any(w in s for w in int_words):
        return "int"

    return "decimal2"


def is_sims_numeric_display_col(df: pd.DataFrame, col: Any) -> bool:
    """Public, side-effect-free numeric-column resolver for every SIMS table path."""
    return _is_numeric_display_col(df, col)


def resolve_sims_numeric_display_kind(col: Any) -> str:
    """Return the shared NumberColumn display kind without creating widgets."""
    return _numeric_display_kind(col)


def resolve_sims_excel_number_format(col: Any) -> str:
    """Return the shared Excel number format for a SIMS numeric display column."""
    kind = resolve_sims_numeric_display_kind(col)
    if kind == "int":
        return "#,##0"
    if kind == "percent2":
        return "0.00\\%"
    return "#,##0.##"


def _normalize_product_inventory_display_values(df: pd.DataFrame, action_name: str) -> pd.DataFrame:
    if "제품재고" not in _clean_text(action_name):
        return df
    out = df.copy()
    for qty_col, amount_col in (("이월수량", "이월금액"), ("입고수량", "입고금액"), ("출고수량", "출고금액"), ("재고수량", "재고금액")):
        if qty_col in out.columns:
            qty = pd.to_numeric(out[qty_col], errors="coerce").fillna(0.0).astype("float64")
            out[qty_col] = qty
            if amount_col in out.columns:
                amount = pd.to_numeric(out[amount_col], errors="coerce").astype("float64")
                out[amount_col] = amount.mask(amount.isna() & qty.eq(0), 0.0)
    for col in ("보험금액", "이월단가", "입고단가", "출고단가", "재고단가", "현보험약가", "이월DC율", "입고DC율", "출고DC율", "재고DC율"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


def _is_product_table_display_surface(df: pd.DataFrame, action_name: str) -> bool:
    action = _clean_text(action_name)
    if "제품수불" in action or "제품재고" in action:
        return True
    if not isinstance(df, pd.DataFrame):
        return False
    columns = {_clean_text(column) for column in df.columns}
    return (
        {"입출고일자", "입고수량", "출고수량", "재고수량"}.issubset(columns)
        or {"이월수량", "입고수량", "출고수량", "재고수량"}.issubset(columns)
    )


def _normalize_product_table_display_values(df: pd.DataFrame, action_name: str) -> pd.DataFrame:
    """Apply the common product-table null policy to a display-only copy.

    Product flow and inventory tables contain synthetic summary/carry rows and
    transaction-dependent text.  Missing text is blank; missing numeric values
    remain float64 ``np.nan`` so Streamlit NumberColumn keeps numeric formatting.
    """
    if not _is_product_table_display_surface(df, action_name):
        return df

    out = df.copy()
    for col in out.columns:
        col_name = _clean_text(col)
        if "일자" in col_name or "날짜" in col_name:
            out[col] = out[col].map(
                lambda value: "" if _is_display_missing_token(value) else value
            )
            continue
        if _is_numeric_display_col(out, col):
            values = out[col].map(
                lambda value: np.nan if _is_display_missing_token(value) else value
            )
            out[col] = pd.to_numeric(values, errors="coerce").astype("float64")
            continue

        # This scope keeps generic analysis business text such as literal
        # "None" unchanged while product-table missing text becomes blank.
        out[col] = out[col].map(
            lambda value: "" if _is_display_missing_token(value) else value
        )
    return out


def prepare_sims_table_display_df(df: pd.DataFrame, *, action_name: str = "") -> pd.DataFrame:
    """Create a display-only shared SIMS table view without mutating its source."""
    out = normalize_display_df_for_streamlit(df)
    out = _normalize_product_inventory_display_values(out, action_name)
    return _normalize_product_table_display_values(out, action_name)


def _format_numeric_display_value(value: Any, kind: str) -> str:
    """Format one display-only numeric value while leaving missing values blank."""
    if _is_display_missing_token(value):
        return ""

    try:
        numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    except Exception:
        return ""
    if _is_display_missing_token(numeric):
        return ""

    try:
        if kind == "int":
            return f"{float(numeric):,.0f}"
        if kind == "percent2":
            return f"{float(numeric):,.2f}%"
        return f"{float(numeric):,.2f}"
    except (TypeError, ValueError, OverflowError):
        return ""


def _format_numeric_null_columns_for_display(
    df: pd.DataFrame,
    *,
    preserve_numeric_nulls: bool = False,
) -> tuple[pd.DataFrame, set[Any]]:
    """Convert only numeric columns with missing values for the display copy.

    Streamlit 1.59 renders numeric nulls in ``NumberColumn`` cells as the
    literal ``None``.  Keep native numeric columns when they have no missing
    values; only the affected display columns become formatted text so their
    missing cells can remain visually blank.
    """
    if not isinstance(df, pd.DataFrame):
        return df, set()

    if preserve_numeric_nulls:
        return df, set()

    out = df.copy()
    text_columns: set[Any] = set()
    for col in out.columns:
        if not _is_numeric_display_col(df, col):
            continue

        source = df[col]
        try:
            has_missing = bool(source.map(_is_display_missing_token).any())
        except Exception:
            has_missing = False
        if not has_missing:
            continue

        kind = _numeric_display_kind(col)
        out[col] = source.map(
            lambda value: _format_numeric_display_value(value, kind)
        ).astype("string")
        text_columns.add(col)

    return out, text_columns


def _normalize_current_stock_numeric_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """Keep current-stock numeric blanks as nullable numbers for NumberColumn."""
    if not isinstance(df, pd.DataFrame):
        return df

    out = df.copy()
    for col in ("재고수량", "현보험약가", "보험금액"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Float64")
    return out


def _prepare_current_stock_number_cells(df: pd.DataFrame) -> tuple[pd.DataFrame, set[Any]]:
    """Build right-aligned text cells only where current-stock needs blanks."""
    if not isinstance(df, pd.DataFrame):
        return df, set()

    out = df.copy()
    text_columns: set[Any] = set()
    for col in ("순번", "현보험약가", "보험금액"):
        if col not in out.columns:
            continue
        numeric = pd.to_numeric(out[col], errors="coerce")
        if not bool(numeric.isna().any()):
            continue
        kind = _numeric_display_kind(col)
        def _display(value: Any) -> str:
            if _is_display_missing_token(value):
                return ""
            numeric_value = float(value)
            if numeric_value == 0:
                return "0"
            return _format_numeric_display_value(value, kind)
        out[col] = numeric.map(_display).astype("string")
        text_columns.add(col)
    return out, text_columns


def _is_stock_shortage_quantity_int_col(name: Any) -> bool:
    """재고부족 수량 계열은 화면에서 정수 천단위로 표시한다."""
    s = _clean_text(name)
    if not s:
        return False
    if any(w in s for w in ("률", "율", "%", "금액", "단가", "매출", "세액", "공급가액")):
        return False
    return any(w in s for w in ("수량", "수요", "출고", "재고", "부족", "필요"))

def _infer_width_px(df: pd.DataFrame, col: Any, *, sample_n: int = 80) -> int:
    """
    조회 화면 느낌에 맞춘 컬럼 폭 계산.
    너무 좁거나 너무 넓지 않게 min/max를 둔다.
    """
    name = _clean_text(col)

    # 1) 명시 기준
    fixed = {
        "순번": 60,
        "조회순번": 70,
        "번호": 60,

        # IO / 명세 / 재고 주요 컬럼
        "입고일자": 105,
        "출고일자": 105,
        "거래일자": 105,
        "거래명세서일자": 130,
        "세금계산서일자": 130,
        "입출고일자": 115,
        "수불일자": 105,
        "일자": 105,
        "날짜": 105,
        "재고년월": 95,
        "년월": 85,
        "월": 75,
        "입출고구분": 115,
        "재고위치": 115,
        "재고위치명": 125,        
        "동여부": 70,
        "사용여부": 80,
        "삭제여부": 80,
        "도로명코드": 125,
        "도로명코드상세번호": 130,
        "시도명": 105,
        "시구군명": 115,
        "법정읍면동명": 135,
        "도로명": 150,
        "도로명(영문)": 185,
        "지번본번": 90,

        # 분석/KPI 주요 고정 후보
        "기준월": 90,
        "제품코드": 85,
        "제품명": 260,
        "규격": 120,
        "제조사명": 160,
        "매입처명": 180,
        "추세판정": 110,
        "예상등급": 110,
        "부족등급": 130,
        "판정결과": 160,

        "거래처코드": 90,
        "사용자코드": 90,
        "사용자ID": 120,
        "그룹코드": 90,
        "상세코드": 90,
        "항목코드": 90,
    }
    if name in fixed:
        return fixed[name]

    # 2) 컬럼명/샘플 기준
    max_len = _display_len(name)

    try:
        sr = df[col].head(sample_n)
        for v in sr:
            max_len = max(max_len, _display_len(v))
    except Exception:
        pass

    # 한글 중심이면 글자당 폭을 더 크게 본다.
    char_px = 8
    if _has_korean(name):
        char_px = 9

    width = int(max_len * char_px + 34)

    # 3) 성격별 보정
    if _is_row_no_col(name):
        return max(55, min(width, 75))

    if _is_code_like_col(name):
        return max(80, min(width, 150))

    if any(w in name for w in ["명", "이름", "상호", "제품명", "거래처명", "도로명"]):
        return max(120, min(width, 360))

    if any(w in name for w in ["주소", "비고", "메모", "Remark", "상세"]):
        return max(180, min(width, 520))

    if _is_numeric_display_col(df, col):
        return max(90, min(width, 150))

    return max(90, min(width, 260))


def _make_column_config(
    df: pd.DataFrame,
    col: Any,
    *,
    width: int,
    pinned: bool,
    force_text: bool = False,
    force_number: bool = False,
) -> Any:
    name = _clean_text(col)

    kwargs = {
        "label": name,
        "width": width,
    }

    if pinned and _supports_pinned():
        kwargs["pinned"] = True

    if (_is_numeric_display_col(df, col) or force_number) and not force_text:
        kind = _numeric_display_kind(name)

        if kind == "int":
            return st.column_config.NumberColumn(
                **kwargs,
                format="localized",
                step=1,
            )

        if kind == "percent2":
            return st.column_config.NumberColumn(
                **kwargs,
                format="%.2f%%",
                step=0.01,
            )

        return st.column_config.NumberColumn(
            **kwargs,
            format="localized",
            step=0.01,
        )
    
    if force_text and _numeric_display_kind(name):
        kwargs["alignment"] = "right"
    return st.column_config.TextColumn(**kwargs)


def _action_text(action_name: str, meta: Dict[str, Any]) -> str:
    return " ".join(
        [
            _clean_text(action_name),
            _clean_text(meta.get("action")),
            _clean_text(meta.get("domain")),
            _clean_text(meta.get("analysis_type")),
            _clean_text(meta.get("table_profile")),
        ]
    )


def _default_pinned_cols(
    columns: Iterable[Any],
    *,
    action_name: str = "",
    meta: Dict[str, Any] | None = None,
    max_pinned_cols: int | None = None,
) -> list[str]:
    meta = meta or {}
    cols = [_clean_text(c) for c in columns]
    text = _action_text(action_name, meta)

    pins: list[str] = []

    def add_existing(*names: str) -> None:
        for n in names:
            if n and n in cols and n not in pins:
                pins.append(n)

    def add_first_existing(*names: str) -> None:
        for n in names:
            if n and n in cols and n not in pins:
                pins.append(n)
                return

    # 1) 순번 계열은 항상 최우선
    for c in ["순번", "조회순번", "번호"]:
        if c in cols:
            pins.append(c)
            break

    # 2) IO/명세/재고 계열 판정
    is_io_like = any(
        w in text
        for w in (
            "입고명세",
            "출고명세",
            "매입명세",
            "매출명세",
            "거래명세서",
            "세금계산서",
            "실재고월집계",
            "장부재고월집계",
            "제품수불",
            "수불",
            "제품재고",
            "재고현황",
            "재고장",
            "입출고",
            "명세서",
            "월집계",
        )
    )

    if is_io_like:
        # 입고/출고 상세
        if any(w in text for w in ("입고명세", "매입명세")):
            add_first_existing("입고일자", "입출고일자", "일자", "날짜")
            add_first_existing("거래처명", "매입처명", "실납처명", "납품처명")
            add_first_existing("제품명", "품목명", "상품명")

        elif any(w in text for w in ("출고명세", "매출명세")):
            add_first_existing("출고일자", "입출고일자", "일자", "날짜")
            add_first_existing("거래처명", "매출처명", "실납처명", "납품처명")
            add_first_existing("제품명", "품목명", "상품명")

        # 거래명세서/세금계산서 공통
        elif "거래명세서" in text:
            add_first_existing("거래명세서일자", "거래일자", "일자", "날짜")
            add_first_existing("거래처명", "매입처명", "매출처명")

        elif "세금계산서" in text:
            add_first_existing("세금계산서일자", "계산서일자", "일자", "날짜")
            add_first_existing("거래처명", "매입처명", "매출처명")

        # 월집계 재고
        elif any(w in text for w in ("실재고월집계", "장부재고월집계", "월집계")):
            add_first_existing("재고년월", "년월", "월")
            add_first_existing("제품명", "품목명", "상품명")
            add_first_existing("재고위치", "재고위치명")

        # 제품수불
        elif "수불" in text:
            add_first_existing("일자", "입출고일자", "수불일자", "출고일자", "입고일자")
            add_first_existing("거래처명", "매입처명", "매출처명", "입출고구분")
            add_first_existing("제품명", "품목명", "상품명")

        # 제품재고 / 재고현황
        elif any(w in text for w in ("제품재고", "재고현황", "재고장")):
            add_first_existing("제품코드")
            add_first_existing("제품명", "품목명", "상품명")
            add_first_existing("재고위치", "재고위치명")

    # 3) 분석/KPI 계열
    #    품목별 KPI 표는 후속질문/검토 시 좌측 기준 컬럼이 중요하다.
    is_analysis_like = (
        any(
            w in text
            for w in (
                "분석",
                "kpi",
                "매출 추세",
                "매출추세",
                "매출 예상",
                "매출예상",
                "재고부족",
                "sales_trend",
                "sales_forecast",
                "customer_sales_forecast",
                "salesperson_sales_forecast",
                "region_sales_forecast",
                "manufacturer_sales_trend",
                "manufacturer_sales_trend_summary",
                "stock_shortage",
                "product_summary",
                "product_forecast",
                "manufacturer_trend_detail",
                "manufacturer_trend_summary",
                "product_stock_shortage",
                "current_table_followup",
                "현재표",
            )
        )
        or str(meta.get("analysis_type") or "").strip() in {"sales_trend", "sales_forecast", "customer_sales_forecast", "salesperson_sales_forecast", "region_sales_forecast", "manufacturer_sales_trend", "manufacturer_sales_trend_summary", "stock_shortage", "supplier_stock_shortage"}
        or str(meta.get("summary_type") or "").strip() in {
            "product_summary",
            "product_forecast",
            "manufacturer_trend_detail",
            "manufacturer_trend_summary",
            "product_stock_shortage",
        }
    )

    if is_analysis_like:
        add_first_existing("제약사명")
        add_first_existing("기준월", "년월", "월")
        add_first_existing("제품코드", "상품코드", "품목코드")
        add_first_existing("제품명", "상품명", "품목명")
        add_first_existing("규격", "규격명")
        add_first_existing("제조사명", "제약사명", "매입처명")
        add_first_existing("추세판정", "예상등급", "부족등급", "판정결과")

        desired_max_pinned = 5 if "기준월" in cols else 4
        if max_pinned_cols is None:
            max_pinned_cols = desired_max_pinned
        else:
            max_pinned_cols = min(int(max_pinned_cols), desired_max_pinned)

        return pins[:max(1, int(max_pinned_cols))]
    
    # 4) 마스터/분석 계열 기존 규칙

    elif "road_address" in text or "도로명주소" in text:
        add_existing("도로명코드")

    elif "거래처" in text or "vendors" in text:
        add_existing("거래처코드", "거래처명")

    elif "제품" in text or "goods" in text:
        add_existing("제품코드", "제품명")

    elif "사용자" in text or "users" in text:
        add_existing("사용자코드", "사용자ID", "사용자명")

    elif "코드" in text or "codes" in text:
        add_existing("그룹코드", "상세코드", "항목코드")

    else:
        # 일반 fallback: 코드/명 첫 쌍만 고정
        first_code = next((c for c in cols if "코드" in c), "")
        first_name = next((c for c in cols if c.endswith("명") or "명" in c), "")
        add_existing(first_code, first_name)

    # IO 상세표는 순번 + 일자 + 거래처명 + 제품명까지 필요하므로 4개 허용
    if max_pinned_cols is None:
        max_pinned_cols = 4 if is_io_like else 3

    return pins[:max(1, int(max_pinned_cols))]


def ensure_row_no(df: pd.DataFrame, *, col_name: str = "순번") -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return df

    out = df.copy()

    if "순번" in out.columns or "조회순번" in out.columns or "번호" in out.columns:
        return out

    out.insert(0, col_name, range(1, len(out) + 1))
    return out


def build_sims_table_display_config(
    df: pd.DataFrame,
    *,
    action_name: str = "",
    meta: Dict[str, Any] | None = None,
    add_row_no: bool = True,
    row_no_name: str = "순번",
    enable_pinning: bool = True,
    max_pinned_cols: int | None = None,
    min_width: int = 720,
    max_width: int = 1650,
    min_height: int = 170,
    max_height: int = 520,
    row_height: int = 32,
) -> Tuple[pd.DataFrame, Dict[str, Any], int, int]:
    """
    SIMS 공용 표 표시 설정.

    반환:
        view_df, column_config, table_width, table_height
    """
    if not isinstance(df, pd.DataFrame):
        return df, {}, min_width, min_height

    meta = meta or {}
    # Current-stock already arrives as a display copy.  The generic IO
    # normalizer fills numeric nulls with zero, which is incorrect for its
    # repeated-location and subtotal rows.
    view_df = df.copy() if bool(meta.get("current_stock_query")) else prepare_sims_table_display_df(df, action_name=action_name)

    if add_row_no:
        view_df = ensure_row_no(view_df, col_name=row_no_name)

    pinned_cols = set(
        _default_pinned_cols(
            view_df.columns,
            action_name=action_name,
            meta=meta,
            max_pinned_cols=max_pinned_cols,    
        )
        if enable_pinning
        else []
    )

    current_stock_text_columns: set[Any] = set()
    if bool(meta.get("current_stock_query")):
        view_df = _normalize_current_stock_numeric_nulls(view_df)
        view_df, current_stock_text_columns = _prepare_current_stock_number_cells(view_df)

    final_view_df, formatted_numeric_columns = _format_numeric_null_columns_for_display(
        view_df,
        preserve_numeric_nulls=bool(meta.get("current_stock_query")),
    )
    column_config: Dict[str, Any] = {}
    total_width = 60

    for col in final_view_df.columns:
        width = _infer_width_px(final_view_df, col)
        total_width += width

        try:
            column_config[col] = _make_column_config(
                final_view_df,
                col,
                width=width,
                pinned=_clean_text(col) in pinned_cols,
                force_text=col in formatted_numeric_columns or col in current_stock_text_columns,
            )
        except TypeError:
            # 혹시 pinned 미지원/인자 차이가 있으면 pinned 없이 재시도
            try:
                if _is_numeric_display_col(final_view_df, col) and col not in formatted_numeric_columns:
                    column_config[col] = st.column_config.NumberColumn(
                        _clean_text(col),
                        width=width,
                        format="localized",
                    )
                else:
                    column_config[col] = st.column_config.TextColumn(
                        _clean_text(col),
                        width=width,
                    )
            except Exception:
                pass
        except Exception:
            pass

    table_width = int(min(max(total_width, min_width), max_width))
    table_height = int(min(max(min_height, row_height * (len(final_view_df) + 1) + 42), max_height))
    return final_view_df, column_config, table_width, table_height

# SIMS 공용 표 렌더러.
# 채팅창, SIMS 조회 화면, 향후 분석표가 같은 규칙을 쓰도록 하는 wrapper.
def render_sims_table(
    df: pd.DataFrame,
    *,
    action_name: str = "",
    meta: Dict[str, Any] | None = None,
    add_row_no: bool = True,
    row_no_name: str = "순번",
    enable_pinning: bool = True,
    max_pinned_cols: int | None = None,
    min_width: int = 720,
    max_width: int = 1650,
    min_height: int = 170,
    max_height: int = 520,
    row_height: int = 32,
    key: str | None = None,
    use_container_width: bool = True,
    hide_index: bool = True,
) -> pd.DataFrame:
    """
    SIMS 공용 표 렌더러.

    채팅창, SIMS 조회 화면, 향후 분석표가 같은 규칙을 쓰도록 하는 wrapper.
    반환값은 실제 화면에 표시한 view_df이다.
    """
    view_df, column_config, table_width, table_height = build_sims_table_display_config(
        df,
        action_name=action_name,
        meta=meta,
        add_row_no=add_row_no,
        row_no_name=row_no_name,
        enable_pinning=enable_pinning,
        max_pinned_cols=max_pinned_cols,
        min_width=min_width,
        max_width=max_width,
        min_height=min_height,
        max_height=max_height,
        row_height=row_height,
    )

    log.debug(
        "[sims.table_display] render action=%s rows=%s cols=%s width=%s height=%s pinned=%s",
        action_name,
        len(view_df) if isinstance(view_df, pd.DataFrame) else 0,
        len(view_df.columns) if isinstance(view_df, pd.DataFrame) else 0,
        table_width,
        table_height,
        bool(enable_pinning),
    )

    kwargs = {
        "width": "stretch" if use_container_width else "content",
        "hide_index": hide_index,
        "height": table_height,
    }

    if column_config:
        kwargs["column_config"] = column_config

    if key:
        kwargs["key"] = key

    st.dataframe(view_df, **kwargs)

    return view_df

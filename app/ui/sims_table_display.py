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

import pandas as pd
import streamlit as st

log = logging.getLogger("ssai")


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


def _is_numeric_display_name(col: Any) -> bool:
    s = _clean_text(col)
    if _is_explicit_code_display_name(s):
        return False
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
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
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
    if value is None:
        return value
    try:
        if pd.isna(value):
            return value
    except Exception:
        pass
    s = str(value).strip()
    if not s:
        return value
    if len(s) >= 2 and s.endswith(".0"):
        s = s[:-2]
    if len(s) == 6 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}"
    return s


def _format_display_yyyymmdd(value: Any) -> Any:
    if value is None:
        return value
    try:
        if pd.isna(value):
            return value
    except Exception:
        pass
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
        if "일자" in col_name or "날짜" in col_name:
            out[col] = sr.map(_format_display_yyyymmdd)
            continue

        if _is_numeric_display_name(col):
            out[col] = pd.to_numeric(sr, errors="coerce")
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

    if _is_row_no_col(s):
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

    if _is_explicit_code_display_name(s):
        return False

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
        "자료원",
        "설명",
        "결과",
    ]

    if any(w in s for w in text_words):
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
        "매출",
        "매입",
        "출고",
        "입고",
        "재고",
        "부족",
        "필요",
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
) -> Any:
    name = _clean_text(col)

    kwargs = {
        "label": name,
        "width": width,
    }

    if pinned and _supports_pinned():
        kwargs["pinned"] = True

    if _is_numeric_display_col(df, col):
        kind = _numeric_display_kind(name)

        if kind == "int":
            return st.column_config.NumberColumn(
                **kwargs,
                format="localized",
                step=1,
            )

        return st.column_config.NumberColumn(
            **kwargs,
            format="localized",
            step=0.01,
        )
    
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
                "stock_shortage",
                "product_summary",
                "product_forecast",
                "product_stock_shortage",
                "current_table_followup",
                "현재표",
            )
        )
        or str(meta.get("analysis_type") or "").strip() in {"sales_trend", "sales_forecast", "stock_shortage"}
        or str(meta.get("summary_type") or "").strip() in {
            "product_summary",
            "product_forecast",
            "product_stock_shortage",
        }
    )

    if is_analysis_like:
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

    view_df = normalize_display_df_for_streamlit(df)

    if add_row_no:
        view_df = ensure_row_no(view_df, col_name=row_no_name)

    meta = meta or {}
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

    column_config: Dict[str, Any] = {}
    total_width = 60

    for col in view_df.columns:
        width = _infer_width_px(view_df, col)
        total_width += width

        try:
            column_config[col] = _make_column_config(
                view_df,
                col,
                width=width,
                pinned=_clean_text(col) in pinned_cols,
            )
        except TypeError:
            # 혹시 pinned 미지원/인자 차이가 있으면 pinned 없이 재시도
            try:
                if _is_numeric_display_col(view_df, col):
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
    table_height = int(min(max(min_height, row_height * (len(view_df) + 1) + 42), max_height))

    return view_df, column_config, table_width, table_height

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
        "use_container_width": use_container_width,
        "hide_index": hide_index,
        "height": table_height,
    }

    if column_config:
        kwargs["column_config"] = column_config

    if key:
        kwargs["key"] = key

    st.dataframe(view_df, **kwargs)

    return view_df
